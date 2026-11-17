#!/usr/bin/env python3
"""CMSIS-SVD register decoding: turn a raw dump into named bitfields.

`fault` / `regs` explain the Cortex-M core registers. Everything at a peripheral
address is still a bare hex number, so "why is the clock not starting" ends in a
manual bit-peeling exercise against a PDF. A CMSIS-SVD file already describes
those bits; this module reads it and labels them.

Nothing is invented: bits the SVD does not define are reported as ``reserved``
with their exact range, and an address that falls outside every peripheral is
reported as ``unknown`` (exit 4) rather than silently dropped.

Exit codes: 0 decoded, 4 evidence ambiguity (address outside every peripheral),
6 configuration error (missing/unparsable SVD, bad arguments).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_AMBIGUOUS = 4
EXIT_CONFIG = 6

DUMP_ROW_RE = re.compile(r"0x(?P<address>[0-9a-fA-F]+)\s*(?:=|:|\s)\s*0x(?P<value>[0-9a-fA-F]+)")
# A peripheral occupies a fixed window; 0x1000 is the ARM convention and the
# widest practice, so a register lookup never reaches into a neighbour.
PERIPHERAL_WINDOW = 0x1000


class SvdError(Exception):
    """The SVD or the arguments cannot be used; report instead of guessing."""


def _text(node: ET.Element | None, tag: str, default: str | None = None) -> str | None:
    if node is None:
        return default
    found = node.find(tag)
    if found is None or found.text is None:
        return default
    return found.text.strip()


def _int_of(node: ET.Element | None, tag: str) -> int | None:
    raw = _text(node, tag)
    if raw is None:
        return None
    try:
        return int(raw, 0)
    except ValueError:
        return None


def _field_bounds(field: ET.Element) -> tuple[int, int]:
    """Return (lsb, width) supporting both bitOffset/bitWidth and lsb/msb forms."""
    offset = _int_of(field, "bitOffset")
    width = _int_of(field, "bitWidth")
    if offset is not None and width is not None:
        return offset, width
    lsb = _int_of(field, "lsb")
    msb = _int_of(field, "msb")
    if lsb is not None and msb is not None:
        return min(lsb, msb), abs(msb - lsb) + 1
    raise SvdError(f"field {_text(field, 'name')!r} has no usable bit range")


def parse_enumerated_values(field: ET.Element) -> dict[int, str]:
    values: dict[int, str] = {}
    container = field.find("enumeratedValues")
    if container is None:
        return values
    for candidate in container.findall("enumeratedValue"):
        raw = _int_of(candidate, "value")
        if raw is None:
            continue
        values[raw] = _text(candidate, "name") or f"value_{raw}"
    return values


def parse_fields(register: ET.Element) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    container = register.find("fields")
    if container is None:
        return fields
    for field in container.findall("field"):
        lsb, width = _field_bounds(field)
        fields.append(
            {
                "name": _text(field, "name") or "unnamed",
                "lsb": lsb,
                "msb": lsb + width - 1,
                "width": width,
                "access": _text(field, "access"),
                "description": _text(field, "description"),
                "enumerated": parse_enumerated_values(field),
            }
        )
    fields.sort(key=lambda item: item["lsb"])
    return fields


def parse_register(register: ET.Element) -> dict[str, Any] | None:
    name = _text(register, "name")
    offset = _int_of(register, "addressOffset")
    if name is None or offset is None:
        return None
    return {
        "name": name,
        "address_offset": offset,
        "size": _int_of(register, "size") or 32,
        "access": _text(register, "access"),
        "reset_value": _int_of(register, "resetValue"),
        "description": _text(register, "description"),
        "fields": parse_fields(register),
        "derived_from": register.get("derivedFrom"),
    }


def _apply_derived_from(registers: dict[str, dict[str, Any]], warnings: list[str]) -> None:
    """Copy fields from a base register when a register only says derivedFrom."""
    for name, register in registers.items():
        parent = register.get("derived_from")
        if not parent:
            continue
        base = registers.get(parent)
        if base is None:
            warnings.append(f"{name} derives from unknown register {parent}; fields left empty")
            continue
        if not register["fields"]:
            register["fields"] = [dict(field) for field in base["fields"]]
        for key in ("size", "access", "reset_value"):
            if register[key] is None:
                register[key] = base[key]


def parse_svd(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SvdError(f"SVD not found: {path}")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise SvdError(f"SVD is not valid XML: {exc}") from exc
    if root.tag != "device":
        raise SvdError(f"{path} root element is <{root.tag}>, expected <device>")

    warnings: list[str] = []
    peripherals: dict[str, dict[str, Any]] = {}
    container = root.find("peripherals")
    if container is None:
        raise SvdError(f"{path} has no <peripherals> element")

    for node in container.findall("peripheral"):
        name = _text(node, "name")
        base = _int_of(node, "baseAddress")
        if name is None or base is None:
            warnings.append("skipped a peripheral without a name or baseAddress")
            continue
        registers: dict[str, dict[str, Any]] = {}
        reg_container = node.find("registers")
        if reg_container is not None:
            for register_node in reg_container.findall("register"):
                register = parse_register(register_node)
                if register is None:
                    warnings.append(f"{name}: skipped a register without name/addressOffset")
                    continue
                registers[register["name"]] = register
            _apply_derived_from(registers, warnings)
        peripherals[name] = {
            "name": name,
            "base_address": base,
            "description": _text(node, "description"),
            "registers": registers,
        }

    if not peripherals:
        raise SvdError(f"{path} contains no usable peripheral definitions")
    return {"device": _text(root, "name"), "peripherals": peripherals, "warnings": warnings}


def find_peripheral(model: dict[str, Any], address: int) -> dict[str, Any] | None:
    """Pick the peripheral whose base + window contains the address, or None."""
    best = None
    for peripheral in model["peripherals"].values():
        base = peripheral["base_address"]
        if base <= address < base + PERIPHERAL_WINDOW:
            if best is None or base > best["base_address"]:
                best = peripheral
    return best


def decode_register_bytes(value: int, size_bits: int) -> int:
    mask = (1 << size_bits) - 1
    return value & mask


def decode_value(register: dict[str, Any], value: int) -> dict[str, Any]:
    """Split a register value into named fields plus explicit reserved gaps."""
    size = register["size"] or 32
    masked = decode_register_bytes(value, size)
    decoded: list[dict[str, Any]] = []
    covered = 0
    for field in register["fields"]:
        lsb, width = field["lsb"], field["width"]
        raw = (masked >> lsb) & ((1 << width) - 1)
        entry = {
            "name": field["name"],
            "lsb": lsb,
            "msb": field["msb"],
            "value": raw,
            "hex": f"0x{raw:x}",
            "access": field["access"],
            "description": field["description"],
        }
        enumerated = field.get("enumerated") or {}
        if enumerated:
            entry["meaning"] = enumerated.get(raw, "undefined")
            entry["enumerated"] = {str(key): label for key, label in sorted(enumerated.items())}
        decoded.append(entry)
        covered |= ((1 << width) - 1) << lsb

    # Gaps: bits the SVD never defined. Reported, never dropped.
    reserved_mask = ((1 << size) - 1) & ~covered
    reserved_regions = []
    bit = 0
    while bit < size:
        if reserved_mask & (1 << bit):
            start = bit
            while bit < size and reserved_mask & (1 << bit):
                bit += 1
            reserved_regions.append({"lsb": start, "msb": bit - 1, "name": "reserved"})
            continue
        bit += 1

    return {
        "raw": masked,
        "hex": f"0x{masked:0{max(2, size // 4)}x}",
        "fields": decoded,
        "reserved": reserved_regions,
        "has_undefined_bits": bool(reserved_mask),
    }


def decode_dump_line(model: dict[str, Any], address: int, value: int) -> dict[str, Any]:
    peripheral = find_peripheral(model, address)
    if peripheral is None:
        return {
            "address": f"0x{address:08x}",
            "value": f"0x{value:08x}",
            "status": "unknown",
            "reason": "address falls outside every peripheral declared in the SVD",
        }
    offset = address - peripheral["base_address"]
    match = None
    for register in peripheral["registers"].values():
        if register["address_offset"] == offset:
            match = register
            break
    if match is None:
        return {
            "address": f"0x{address:08x}",
            "value": f"0x{value:08x}",
            "peripheral": peripheral["name"],
            "offset": f"0x{offset:03x}",
            "status": "unknown",
            "reason": "offset matches no register declared for this peripheral",
        }
    return {
        "address": f"0x{address:08x}",
        "value": f"0x{value:08x}",
        "peripheral": peripheral["name"],
        "register": match["name"],
        "offset": f"0x{offset:03x}",
        "size": match["size"],
        "access": match["access"],
        "description": match["description"],
        "status": "decoded",
        "decoded": decode_value(match, value),
    }


def parse_dump(text: str) -> list[tuple[int, int]]:
    rows: list[tuple[int, int]] = []
    for line in text.splitlines():
        match = DUMP_ROW_RE.search(line)
        if match:
            rows.append((int(match.group("address"), 16), int(match.group("value"), 16)))
    return rows


def parse_inline_values(spec: str) -> list[tuple[int, int]]:
    rows: list[tuple[int, int]] = []
    for chunk in spec.split(","):
        item = chunk.strip()
        if not item:
            continue
        match = DUMP_ROW_RE.search(item)
        if not match:
            raise SvdError(f"cannot parse --values entry {item!r}; use ADDRESS=VALUE in hex")
        rows.append((int(match.group("address"), 16), int(match.group("value"), 16)))
    return rows


def search_definitions(model: dict[str, Any], needle: str) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    lowered = needle.lower()
    for peripheral in model["peripherals"].values():
        for register in peripheral["registers"].values():
            if lowered in register["name"].lower():
                hits.append(
                    {
                        "peripheral": peripheral["name"],
                        "base_address": f"0x{peripheral['base_address']:08x}",
                        "register": register["name"],
                        "address_offset": f"0x{register['address_offset']:03x}",
                        "size": register["size"],
                        "access": register["access"],
                        "reset_value": register["reset_value"],
                        "description": register["description"],
                        "fields": [
                            {
                                "name": field["name"],
                                "lsb": field["lsb"],
                                "msb": field["msb"],
                                "enumerated": {str(k): v for k, v in sorted((field.get("enumerated") or {}).items())},
                            }
                            for field in register["fields"]
                        ],
                    }
                )
                continue
            for field in register["fields"]:
                if lowered in field["name"].lower():
                    hits.append(
                        {
                            "peripheral": peripheral["name"],
                            "base_address": f"0x{peripheral['base_address']:08x}",
                            "register": register["name"],
                            "field": field["name"],
                            "lsb": field["lsb"],
                            "msb": field["msb"],
                            "description": field["description"],
                        }
                    )
    return hits


def format_hits(payload: dict[str, Any]) -> str:
    lines = []
    for row in payload["results"]:
        if row["status"] != "decoded":
            lines.append(f"{row['address']} {row['value']}  UNKNOWN: {row['reason']}")
            continue
        lines.append(
            f"{row['peripheral']}.{row['register']} @ {row['address']} = {row['value']}"
            f"  ({row.get('description') or 'no description'})"
        )
        for field in row["decoded"]["fields"]:
            meaning = field.get("meaning")
            suffix = f"  -> {meaning}" if meaning else ""
            lines.append(
                f"    [{field['msb']:>2}:{field['lsb']:<2}] {field['name']:<16} = {field['hex']:<6}{suffix}"
            )
        for region in row["decoded"]["reserved"]:
            lines.append(f"    [{region['msb']:>2}:{region['lsb']:<2}] reserved (undefined in SVD)")
    if payload["warnings"]:
        lines.append("")
        for warning in payload["warnings"]:
            lines.append(f"warning: {warning}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass

    parser = argparse.ArgumentParser(
        prog="svd_decode.py",
        description=(
            "Decode peripheral register dumps into named bitfields using a CMSIS-SVD file. "
            "Undefined bits are reported as reserved; unknown addresses are an ambiguity, not a silence."
        ),
    )
    parser.add_argument("--svd", required=True, help="CMSIS-SVD XML file")
    parser.add_argument("--dump", help="file of ADDRESS VALUE rows (0x... 0x... or 0x...=0x...)")
    parser.add_argument("--values", help="inline rows, e.g. '0x40021000=0x03020001,0x40021004=0x0'")
    parser.add_argument("--peripheral", help="restrict --values decoding to this peripheral")
    parser.add_argument("--find", help="search register and field names for this substring")
    parser.add_argument("--output", help="also write the JSON result to this path")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a text summary")
    parser.add_argument("--version", action="version", version="embedded-dev svd_decode 0.8.0")
    args = parser.parse_args(argv)

    try:
        model = parse_svd(Path(args.svd))
        if args.find:
            hits = search_definitions(model, args.find)
            payload = {
                "schema_version": "svd-lookup.v1",
                "svd": str(args.svd),
                "device": model["device"],
                "query": args.find,
                "hits": hits,
                "hit_count": len(hits),
                "warnings": model["warnings"],
            }
            rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            if args.output:
                Path(args.output).write_text(rendered + "\n", encoding="utf-8")
            if args.json:
                print(rendered)
            else:
                if not hits:
                    print(f"no register or field matches {args.find!r}")
                for hit in hits:
                    if "field" in hit:
                        print(f"  {hit['peripheral']}.{hit['register']}.{hit['field']} [{hit['msb']}:{hit['lsb']}]")
                    else:
                        print(f"  {hit['peripheral']}.{hit['register']} @ {hit['address_offset']} ({hit['size']} bit)")
            return EXIT_OK if hits else EXIT_AMBIGUOUS

        if args.dump:
            dump_path = Path(args.dump)
            if not dump_path.is_file():
                raise SvdError(f"dump file not found: {dump_path}")
            rows = parse_dump(dump_path.read_text(encoding="utf-8", errors="replace"))
        elif args.values:
            rows = parse_inline_values(args.values)
        else:
            raise SvdError("give one of --dump, --values or --find")

        if not rows:
            raise SvdError("no ADDRESS VALUE rows found; refusing to report an empty decode")

        if args.peripheral:
            known = {name.lower() for name in model["peripherals"]}
            if args.peripheral.lower() not in known:
                raise SvdError(
                    f"peripheral {args.peripheral!r} is not in the SVD"
                    f" (have: {', '.join(sorted(model['peripherals']))})"
                )

        results = [decode_dump_line(model, address, value) for address, value in rows]
        unknown = [row for row in results if row["status"] != "decoded"]
        payload = {
            "schema_version": "svd-decode.v1",
            "svd": str(args.svd),
            "device": model["device"],
            "results": results,
            "decoded_count": len(results) - len(unknown),
            "unknown_count": len(unknown),
            "warnings": model["warnings"],
        }
    except SvdError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered if args.json else format_hits(payload))
    return EXIT_AMBIGUOUS if unknown else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())