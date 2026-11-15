#!/usr/bin/env python3
"""Hardware fact tools: controller pin maps from KiCad netlists and .ioc pinmaps.

The goal is to make pin claims checkable. A netlist says which net a pin is on;
this tool traces through series passives and reports where each signal can go.
It never guesses: an ambiguous controller selection is an error that asks for
--main-controller, and an unsupported file format is refused instead of parsed.

Exit codes:
    0  success
    4  ambiguous or unverifiable evidence (e.g. two plausible controllers)
    6  configuration error (unsupported/malformed file, bad option)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable, Sequence

EXIT_OK = 0
EXIT_DATA = 4
EXIT_CONFIG = 6

MAP_SCHEMA = "controller-map.v1"
IOC_SCHEMA = "board-ioc-map.v1"

POWER_PIN_RE = re.compile(
    r"^(VDD|VSS|VBAT|VCAP|VREF|VDDA|VSSA|VCC|AVDD|DVDD|PGND|AGND|GND|GNDA|VBUS|VIN|3V3|5V)$",
    re.IGNORECASE,
)
POWER_NET_RE = re.compile(
    r"(^|[^A-Za-z0-9])(GND|AGND|DGND|PGND|VSS|VDD|VCC|VBAT|VBUS|VIN|\+?\d+V\d*|3V3|5V)([^A-Za-z0-9]|$)",
    re.IGNORECASE,
)
MCU_RE = re.compile(
    r"(STM32|STM8|ESP32|ESP8266|NRF5|NRF52|RP2040|RP2350|ATSAM|SAMD|SAM3|SAM4|SAME|"
    r"MSP430|GD32|CH32|CH57|APM32|HC32|PIC\d|ATMEGA|ATTINY|AVR|CORTEX|RISC-V|GD32VF)",
    re.IGNORECASE,
)
PIN_BASE_RE = re.compile(r"^P([A-I])(\d+)$", re.IGNORECASE)
IOC_PIN_KEY_RE = re.compile(r"^(P[A-I]\d+)(?:-[A-Za-z0-9_\-]+)?\.(Signal|Mode|GPIO_Label)$")
IOC_PIN_VALUE_RE = re.compile(r"^P([A-I])(\d+)$", re.IGNORECASE)

# Reference-prefix classification, longest prefix first.
_CLASS_PREFIXES = [
    ("LED", "led"),
    ("FID", "mechanical"),
    ("XTAL", "crystal"),
    ("ANT", "mechanical"),
    ("NTC", "passive"),
    ("CON", "connector"),
    ("JP", "bridge"),
    ("SB", "bridge"),
    ("LK", "bridge"),
    ("NT", "net_tie"),
    ("TP", "test_point"),
    ("BT", "battery"),
    ("MH", "mechanical"),
    ("MP", "mechanical"),
    ("FL", "passive"),
    ("FB", "passive"),
    ("XT", "crystal"),
    ("SW", "switch"),
    ("CN", "connector"),
    ("IC", "ic"),
    ("Y", "crystal"),
    ("X", "crystal"),
    ("OSC", "crystal"),
    ("R", "passive"),
    ("L", "passive"),
    ("C", "passive"),
    ("F", "fuse"),
    ("D", "diode"),
    ("Q", "transistor"),
    ("K", "relay"),
    ("S", "switch"),
    ("U", "ic"),
    ("A", "ic"),
    ("J", "connector"),
    ("P", "connector"),
    ("H", "mechanical"),
    ("M", "motor"),
]
_SERIES_PASSIVES = {"passive", "fuse"}
_UNMOUNTED_RE = re.compile(r"(^|[^A-Za-z])(DNP|DNF|N\.?C\.?)([^A-Za-z]|$)", re.IGNORECASE)


class MapError(Exception):
    """Input is understood but cannot be mapped without guessing."""


class ConfigError(Exception):
    """Input or options are unusable."""


def classify_ref(ref: str) -> str:
    upper = (ref or "").strip().upper()
    for prefix, kind in _CLASS_PREFIXES:
        if upper.startswith(prefix):
            return kind
    return "other"


def is_unmounted(attrs: dict[str, str], value: str) -> bool:
    for key in ("dnp", "dnf", "dnp_status", "populate"):
        raw = (attrs.get(key) or "").strip().lower()
        if raw in ("1", "true", "yes", "dnp", "dnf"):
            return True
    return bool(_UNMOUNTED_RE.search(value or ""))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(node: ET.Element, name: str) -> Iterable[ET.Element]:
    for child in node:
        if _local(child.tag) == name:
            yield child


def _text(node: ET.Element | None) -> str:
    return (node.text or "").strip() if node is not None else ""


def parse_kicad_netlist(text: str) -> dict[str, Any]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ConfigError(f"netlist is not valid KiCad XML: {exc}") from exc
    if _local(root.tag) != "export":
        raise ConfigError("netlist root is not <export>; only KiCad XML netlists are supported")
    components: dict[str, dict[str, Any]] = {}
    for group in _children(root, "components"):
        for comp in _children(group, "comp"):
            ref = (comp.get("ref") or "").strip()
            if not ref:
                continue
            libsource = next(_children(comp, "libsource"), None)
            footprint = next(_children(comp, "footprint"), None)
            value = _text(next(_children(comp, "value"), None))
            components[ref] = {
                "ref": ref,
                "value": value,
                "footprint": _text(footprint),
                "library": libsource.get("lib") if libsource is not None else None,
                "part": libsource.get("part") if libsource is not None else None,
                "attrs": dict(comp.attrib),
                "mounted": not is_unmounted(comp.attrib, value),
            }
    nets: dict[str, dict[str, Any]] = {}
    for group in _children(root, "nets"):
        for net in _children(group, "net"):
            name = (net.get("name") or "").strip() or f"net-{net.get('code', '?')}"
            nodes = []
            for node in _children(net, "node"):
                ref = (node.get("ref") or "").strip()
                if not ref or ref not in components:
                    continue
                nodes.append(
                    {
                        "ref": ref,
                        "pin": (node.get("pin") or "").strip(),
                        "pinfunction": (node.get("pinfunction") or "").strip(),
                        "pintype": (node.get("pintype") or "").strip(),
                    }
                )
            nets[name] = {"code": net.get("code"), "name": name, "nodes": nodes}
    if not components or not nets:
        raise ConfigError("netlist has no components or no nets")
    return {"components": components, "nets": nets}


def _net_kind(name: str, nodes: Sequence[dict[str, str]]) -> str:
    if any((node.get("pintype") or "").lower().startswith("power") for node in nodes):
        return "power"
    if POWER_NET_RE.search(name or ""):
        return "power"
    return "signal"


def _is_power_pin(node: dict[str, str]) -> bool:
    pintype = (node.get("pintype") or "").lower()
    if pintype in ("power_in", "power_out", "power"):
        return True
    return bool(POWER_PIN_RE.match((node.get("pinfunction") or node.get("pin") or "").strip()))


def _node_signature(node: dict[str, str], components: dict[str, dict[str, Any]]) -> dict[str, Any]:
    comp = components.get(node["ref"], {})
    return {
        "ref": node["ref"],
        "pin": node["pin"],
        "pin_name": node.get("pinfunction") or node.get("pin_name") or "",
        "pin_type": node.get("pintype") or "",
        "value": comp.get("value", ""),
        "class": classify_ref(node["ref"]),
    }


def _mcu_score(component: dict[str, Any], pin_count: int) -> int:
    score = pin_count
    if classify_ref(component["ref"]) == "ic":
        score += 40
    haystack = " ".join(
        str(component.get(key) or "") for key in ("ref", "value", "footprint", "part", "library")
    )
    if MCU_RE.search(haystack):
        score += 50
    return score


def _series_edges(mapdata: dict[str, Any]) -> list[dict[str, Any]]:
    components = mapdata["components"]
    nets = mapdata["nets"]
    pin_nets: dict[tuple[str, str], list[str]] = {}
    for name, net in nets.items():
        for node in net["nodes"]:
            pin_nets.setdefault((node["ref"], node["pin"]), []).append(name)
    edges: list[dict[str, Any]] = []
    for ref, comp in components.items():
        kind = classify_ref(ref)
        pins = [node for net in nets.values() for node in net["nodes"] if node["ref"] == ref]
        pin_names = sorted({node["pin"] for node in pins})
        if len(pin_names) != 2:
            continue
        nets_a = pin_nets.get((ref, pin_names[0]), [])
        nets_b = pin_nets.get((ref, pin_names[1]), [])
        if len(nets_a) != 1 or len(nets_b) != 1 or nets_a[0] == nets_b[0]:
            continue
        net_a, net_b = nets_a[0], nets_b[0]
        kind_a = _net_kind(net_a, nets[net_a]["nodes"])
        kind_b = _net_kind(net_b, nets[net_b]["nodes"])
        traversable = kind in _SERIES_PASSIVES or kind == "passive"
        if kind == "passive" and ref.upper().startswith("C"):
            traversable = kind_a == "signal" and kind_b == "signal"
        if not traversable:
            continue
        edges.append(
            {
                "ref": ref,
                "value": comp.get("value", ""),
                "class": kind,
                "dnf": not comp.get("mounted", True),
                "nets": [net_a, net_b],
            }
        )
    return edges


def trace_pins(mapdata: dict[str, Any], controller_ref: str) -> list[dict[str, Any]]:
    components = mapdata["components"]
    nets = mapdata["nets"]
    edges = _series_edges(mapdata)
    adjacency: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        net_a, net_b = edge["nets"]
        adjacency.setdefault(net_a, []).append({"edge": edge, "other": net_b})
        adjacency.setdefault(net_b, []).append({"edge": edge, "other": net_a})
    controller_nets: dict[str, list[str]] = {}
    for name, net in nets.items():
        for node in net["nodes"]:
            if node["ref"] == controller_ref:
                controller_nets.setdefault(node["pin"], []).append(name)

    results = []
    for net_name, net in nets.items():
        for node in net["nodes"]:
            if node["ref"] != controller_ref:
                continue
            if _is_power_pin(node):
                continue
            direct: list[dict[str, Any]] = []
            for other in net["nodes"]:
                if other is node:
                    continue
                direct.append(_node_signature(other, components))
            if _net_kind(net_name, net["nodes"]) != "signal":
                continue
            traced: dict[tuple[str, str, str], dict[str, Any]] = {}
            queue: list[tuple[str, list[dict[str, Any]], int]] = [(net_name, [], 0)]
            visited = {net_name}
            while queue:
                current, via, depth = queue.pop(0)
                if depth >= 4:
                    continue
                for step in adjacency.get(current, []):
                    edge, other_net = step["edge"], step["other"]
                    if other_net in visited:
                        continue
                    next_via = via + [
                        {
                            "ref": edge["ref"],
                            "value": edge["value"],
                            "class": edge["class"],
                            "dnf": edge["dnf"],
                        }
                    ]
                    visited.add(other_net)
                    other = nets.get(other_net)
                    if other is None:
                        continue
                    kind = _net_kind(other_net, other["nodes"])
                    if kind == "power":
                        key = ("", other_net, other_net)
                        traced.setdefault(
                            key,
                            {
                                "via": next_via,
                                "endpoint": None,
                                "rail": _rail_name(other_net),
                                "dnf": any(item["dnf"] for item in next_via),
                            },
                        )
                        continue
                    for candidate in other["nodes"]:
                        if _is_power_pin(candidate):
                            continue
                        if any(item["ref"] == candidate["ref"] for item in next_via):
                            continue
                        signature = _node_signature(candidate, components)
                        key = (signature["ref"], signature["pin"], other_net)
                        traced.setdefault(
                            key,
                            {
                                "via": next_via,
                                "endpoint": signature,
                                "rail": None,
                                "dnf": any(item["dnf"] for item in next_via),
                            },
                        )
                    queue.append((other_net, next_via, depth + 1))
            results.append(
                {
                    "pin": node["pin"],
                    "pin_name": node.get("pinfunction") or node.get("pin_name") or node["pin"],
                    "net": net_name,
                    "direct_nodes": direct,
                    "traced": list(traced.values()),
                }
            )
    results.sort(key=lambda item: (item["pin_name"] or "", item["pin"]))
    return results


def _rail_name(net_name: str) -> str:
    return (net_name or "").strip() or "power-net"


def build_controller_map(
    text: str, path: str, main_controller: str | None = None
) -> tuple[dict[str, Any], bool]:
    mapdata = parse_kicad_netlist(text)
    components = mapdata["components"]
    nets = mapdata["nets"]
    pin_count: dict[str, int] = {}
    for net in nets.values():
        for node in net["nodes"]:
            pin_count[node["ref"]] = pin_count.get(node["ref"], 0) + 1
    candidates = []
    for ref, comp in components.items():
        if classify_ref(ref) not in ("ic", "other"):
            continue
        score = _mcu_score(comp, pin_count.get(ref, 0))
        haystack = " ".join(str(comp.get(key) or "") for key in ("value", "footprint", "part", "library"))
        candidates.append(
            {
                "ref": ref,
                "value": comp.get("value", ""),
                "footprint": comp.get("footprint", ""),
                "score": score,
                "mcu_like": bool(MCU_RE.search(haystack)) or bool(MCU_RE.search(ref)),
                "pin_count": pin_count.get(ref, 0),
            }
        )
    if not candidates:
        raise MapError("no IC-like component was found; cannot pick a controller without guessing")
    candidates.sort(key=lambda item: (-item["score"], item["ref"]))
    if main_controller:
        chosen = next((item for item in candidates if item["ref"].upper() == main_controller.upper()), None)
        if chosen is None:
            raise ConfigError(f"--main-controller {main_controller!r} is not an IC component in this netlist")
        ambiguous = False
    else:
        chosen = candidates[0]
        runner_up = candidates[1] if len(candidates) > 1 else None
        ambiguous = bool(
            runner_up
            and runner_up["mcu_like"]
            and chosen["mcu_like"]
            and chosen["score"] - runner_up["score"] < 15
        )
        if ambiguous:
            refs = ", ".join(f"{item['ref']}({item['score']})" for item in candidates[:3])
            raise MapError(
                "controller selection is ambiguous between " + refs + "; pass --main-controller REF"
            )
    pins = trace_pins(mapdata, chosen["ref"])
    runner_up = next((item for item in candidates if item["ref"] != chosen["ref"]), None)
    confidence = "high"
    if runner_up is not None:
        gap = chosen["score"] - runner_up["score"]
        confidence = "high" if gap >= 15 else "low"
    payload = {
        "schema_version": MAP_SCHEMA,
        "source": {"path": path, "format": "kicad-xml"},
        "controller": {
            "ref": chosen["ref"],
            "value": chosen["value"],
            "footprint": chosen["footprint"],
            "confidence": confidence,
            "scores": candidates[:3],
        },
        "pins": pins,
        "limits": [
            "netlist topology only: a mapped pin is not proof that the firmware configures it",
            "series passives are traced through; decoupling capacitors are visible as direct nodes only",
            "Altium/OrCAD .NET and PDF netlists are refused rather than guessed",
        ],
        "claims": "pin connectivity as declared by the netlist; verify against the schematic and datasheet",
    }
    return payload, ambiguous


# ---------------------------------------------------------------------------
# .ioc pinmap
# ---------------------------------------------------------------------------


def parse_ioc(text: str) -> dict[str, Any]:
    properties: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        properties[key.strip()] = value.strip()
    mcu = (
        properties.get("Mcu.UserName")
        or properties.get("Mcu.Name")
        or properties.get("Mcu.PartNumber")
        or ""
    )
    family = properties.get("Mcu.Family", "")
    package = properties.get("Mcu.Package", "")
    peripherals = []
    for key, value in properties.items():
        match = re.match(r"^Mcu\.IP(\d+)$", key)
        if match:
            peripherals.append(value)
    pins: dict[str, dict[str, Any]] = {}
    for key, value in properties.items():
        match = IOC_PIN_KEY_RE.match(key)
        if not match:
            continue
        base = match.group(1).upper()
        field = match.group(2)
        entry = pins.setdefault(base, {"pin": base, "signal": None, "mode": None, "label": None})
        if field == "Signal":
            entry["signal"] = value or None
        elif field == "Mode":
            entry["mode"] = value or None
        else:
            entry["label"] = value or None
    if not mcu and not pins:
        raise ConfigError("file has neither Mcu.UserName nor P*.Signal keys; is this a CubeMX .ioc file?")
    if not pins:
        raise ConfigError("no pin assignments (P*.Signal) were found in the .ioc file")
    warnings: list[str] = []
    if mcu and not MCU_RE.search(mcu):
        warnings.append(f"MCU name '{mcu}' does not look like a known MCU family; confirm the part number")
    if not package:
        warnings.append("Mcu.Package is missing; package-specific pin behaviour is unverified")
    ordered = sorted(pins.values(), key=lambda item: IOC_PIN_SORT_RE.sub(lambda m: f"{m.group(1)}{int(m.group(2)):02d}", item["pin"]))
    for pin in ordered:
        pin["risk"] = _pin_risk(pin["pin"], pin["signal"])
        if pin["risk"]:
            warnings.append(f"{pin['pin']}: {pin['risk']}")
    return {
        "schema_version": IOC_SCHEMA,
        "source": {"format": "cubemx-ioc"},
        "mcu": {"name": mcu, "family": family, "package": package, "peripherals": sorted(set(peripherals))},
        "pins": ordered,
        "warnings": warnings,
        "limits": [
            "the .ioc records configuration, not the netlist itself; verify wiring against the schematic",
            "assigned signal names are CubeMX labels and may differ from the board net names",
        ],
        "claims": "configured pins as declared by CubeMX; not evidence that the generated code ran",
    }


IOC_PIN_SORT_RE = re.compile(r"^P([A-I])(\d+)$", re.IGNORECASE)

_STRAPPING_RISK = {
    "PA13": "SWDIO: debug access is disabled if this pin is reconfigured as GPIO",
    "PA14": "SWCLK: debug access is disabled if this pin is reconfigured as GPIO",
    "PB3": "JTDO: must release JTAG (SWD-only) before using as GPIO",
    "PB4": "JNTRST: must release JTAG (SWD-only) before using as GPIO",
    "PB2": "BOOT1: sampled at reset; do not drive externally during boot",
    "PC13": "low drive capability (3 mA); do not drive a heavy load",
    "PC14": "OSC32_IN: shared with the 32 kHz crystal",
    "PC15": "OSC32_OUT: shared with the 32 kHz crystal",
}


def _pin_risk(pin: str, signal: str | None) -> str | None:
    if pin in _STRAPPING_RISK and (signal or "").startswith("GPIO"):
        return _STRAPPING_RISK[pin]
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _summary_map(payload: dict[str, Any]) -> str:
    controller = payload["controller"]
    lines = [
        f"controller: {controller['ref']} {controller['value']} "
        f"(confidence {controller['confidence']})",
    ]
    for pin in payload["pins"]:
        endpoints = []
        for item in pin["traced"]:
            if item["endpoint"]:
                endpoint = item["endpoint"]
                endpoints.append(f"{endpoint['ref']}.{endpoint['pin']} ({endpoint['pin_name']})")
            elif item["rail"]:
                endpoints.append(item["rail"])
        targets = ", ".join(sorted(set(endpoints))) or "no traced destination"
        lines.append(f"  {pin['pin_name']} (pin {pin['pin']}, net {pin['net']}) -> {targets}")
    return "\n".join(lines)


def _summary_ioc(payload: dict[str, Any]) -> str:
    mcu = payload["mcu"]
    lines = [f"mcu: {mcu['name'] or 'unknown'} {mcu['package'] or ''}".strip()]
    for pin in payload["pins"]:
        lines.append(f"  {pin['pin']}: {pin['signal'] or 'unassigned'} ({pin['mode'] or 'mode unset'})")
    return "\n".join(lines)


def cmd_controller(args: Any) -> int:
    path = Path(args.netlist)
    if path.suffix.lower() == ".pdf":
        print(
            "error: PDF netlists are not supported; export a KiCad XML netlist "
            "(Altium .NET and scanned PDFs are refused rather than guessed)",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    try:
        text = _read_text(path)
        if not text.lstrip().startswith("<"):
            raise ConfigError(
                "only KiCad XML netlists are supported; export the netlist from KiCad "
                "and pass the .kicadxml/.xml file (Altium .NET and PDFs are refused, not guessed)"
            )
        payload, _ambiguous = build_controller_map(text, str(path), args.main_controller)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except MapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_DATA
    if args.output:
        try:
            Path(args.output).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            print(f"error: cannot write {args.output}: {exc}", file=sys.stderr)
            return EXIT_CONFIG
    print(_summary_map(payload) if not args.json else json.dumps(payload, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_from_ioc(args: Any) -> int:
    path = Path(args.file)
    try:
        payload = parse_ioc(_read_text(path))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    payload["source"]["path"] = str(path)
    if args.output:
        try:
            Path(args.output).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            print(f"error: cannot write {args.output}: {exc}", file=sys.stderr)
            return EXIT_CONFIG
    print(_summary_ioc(payload) if not args.json else json.dumps(payload, ensure_ascii=False, indent=2))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="board_map.py",
        description=(
            "Hardware fact tools: derive a controller pin map from a KiCad XML netlist "
            "(traced through series passives) or a CubeMX .ioc pinmap. Ambiguous "
            "controller selection and unsupported formats are errors, never guesses."
        ),
    )
    parser.add_argument("--version", action="version", version="embedded-dev board_map 0.6.0")
    subparsers = parser.add_subparsers(dest="action", required=True)
    controller = subparsers.add_parser("controller", help="map controller pins from a KiCad XML netlist")
    controller.add_argument("--netlist", required=True, help="KiCad XML netlist export")
    controller.add_argument("--main-controller", default=None, help="controller reference, e.g. U1")
    controller.add_argument("--output", default=None, help="optional JSON output path")
    controller.add_argument("--json", action="store_true", help="emit JSON instead of a text summary")
    ioc = subparsers.add_parser("from-ioc", help="report configured pins from a CubeMX .ioc file")
    ioc.add_argument("--file", required=True, help="CubeMX .ioc project file")
    ioc.add_argument("--output", default=None, help="optional JSON output path")
    ioc.add_argument("--json", action="store_true", help="emit JSON instead of a text summary")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    handlers = {"controller": cmd_controller, "from-ioc": cmd_from_ioc}
    try:
        return handlers[args.action](args)
    except KeyboardInterrupt:
        return EXIT_DATA


if __name__ == "__main__":
    sys.exit(main())
