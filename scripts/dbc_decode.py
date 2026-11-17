#!/usr/bin/env python3
"""Vector DBC signal decoding for candump logs.

`decode can` handles diagnostic traffic (ISO-TP/UDS/OBD-II/J1939) but needs a
hand-written JSON spec. In practice the bus layout already exists as a DBC file,
which is the de-facto standard in every CAN tool. This module reads it and turns
raw frames into physical signal values.

Correctness note that drives the whole implementation: Intel (``@1``) and
Motorola (``@0``) do not share a bit-numbering convention. For Intel the start
bit is the LSB and positions increase linearly. For Motorola the start bit is the
MSB and the sequence walks *down* within a byte and then jumps to the top of the
next byte. Getting this wrong silently produces plausible-but-wrong numbers, so
both orders have hand-computable tests.

Exit codes: 0 decoded, 4 evidence ambiguity (unknown CAN ID, DLC/length
mismatch), 6 configuration error (missing or unparsable DBC, bad arguments).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_AMBIGUOUS = 4
EXIT_CONFIG = 6

BO_RE = re.compile(
    r"^BO_\s+(?P<id>\d+)\s+(?P<name>\w+)\s*:\s*(?P<dlc>\d+)\s+(?P<transmitter>\S+)"
)
SG_RE = re.compile(
    r"^SG_\s+(?P<name>\w+)\s*(?P<mux>M|m\d+M?)?\s*:\s*"
    r"(?P<start>\d+)\s*\|\s*(?P<length>\d+)\s*@\s*(?P<order>[01])\s*(?P<sign>[+-])\s*"
    r"\(\s*(?P<factor>[^,]+)\s*,\s*(?P<offset>[^)]+)\s*\)\s*"
    r"\[\s*(?P<min>[^|]+)\s*\|\s*(?P<max>[^]]+)\s*\]\s*"
    r'"(?P<unit>[^"]*)"'
)
VAL_RE = re.compile(r"^VAL_\s+(?P<id>\d+)\s+(?P<signal>\w+)\s+(?P<body>.*);\s*$")
VAL_ENTRY_RE = re.compile(r'(-?\d+)\s+"([^"]*)"')
CANDUMP_RE = re.compile(
    r"^\s*\((?P<ts>[0-9.]+)\)\s+(?P<channel>\S+)\s+(?P<frame>\S+)\s*$"
)
FRAME_RE = re.compile(r"^(?P<can_id>[0-9A-Fa-f]+)(?P<ext>#|#0|#1)(?P<data>[0-9A-Fa-f]*)$")


class DbcError(Exception):
    """The DBC or the arguments cannot be used; report instead of guessing."""


def _number(raw: str) -> float:
    text = raw.strip()
    try:
        return float(text)
    except ValueError:
        # Some DBC writers use fractions or commas; report rather than default to 0.
        raise DbcError(f"cannot parse numeric field {raw!r}") from None


def parse_dbc(text: str) -> dict[str, Any]:
    """Parse BO_ / SG_ / VAL_ from a DBC file. Other sections are ignored."""
    messages: dict[int, dict[str, Any]] = {}
    pending: dict[str, Any] | None = None
    val_blocks: list[tuple[int, str, dict[int, str]]] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("BO_ "):
            match = BO_RE.match(line)
            if not match:
                raise DbcError(f"unparsable BO_ line: {line}")
            can_id = int(match.group("id"))
            pending = {
                "id": can_id,
                "name": match.group("name"),
                "dlc": int(match.group("dlc")),
                "transmitter": match.group("transmitter"),
                "signals": {},
            }
            messages[can_id] = pending
            continue
        if line.startswith("SG_ "):
            if pending is None:
                raise DbcError(f"SG_ line before any BO_: {line}")
            match = SG_RE.match(line)
            if not match:
                raise DbcError(f"unparsable SG_ line: {line}")
            mux = match.group("mux")
            mux_kind = None
            mux_value = None
            if mux:
                if mux == "M":
                    mux_kind = "selector"
                else:
                    mux_kind = "multiplexed"
                    mux_value = int(mux[1:].rstrip("M"))
            signal = {
                "name": match.group("name"),
                "start": int(match.group("start")),
                "length": int(match.group("length")),
                "order": "intel" if match.group("order") == "1" else "motorola",
                "signed": match.group("sign") == "-",
                "factor": _number(match.group("factor")),
                "offset": _number(match.group("offset")),
                "minimum": _number(match.group("min")),
                "maximum": _number(match.group("max")),
                "unit": match.group("unit"),
                "mux_kind": mux_kind,
                "mux_value": mux_value,
                "enumerated": {},
            }
            pending["signals"][signal["name"]] = signal
            continue
        if line.startswith("VAL_ "):
            match = VAL_RE.match(line)
            if match:
                entries = {
                    int(value): description
                    for value, description in VAL_ENTRY_RE.findall(match.group("body"))
                }
                val_blocks.append((int(match.group("id")), match.group("signal"), entries))
            continue
        # BO_TX_BU_, CM_, BA_, BA_DEF_, SIG_VALTYPE_ and friends: intentionally skipped.

    if not messages:
        raise DbcError("no BO_ message definitions found; this does not look like a DBC file")

    for can_id, signal_name, entries in val_blocks:
        message = messages.get(can_id)
        if message and signal_name in message["signals"]:
            message["signals"][signal_name]["enumerated"] = entries

    return {"messages": messages, "message_count": len(messages)}


def _bit_at(data: bytes, position: int) -> int:
    byte_index, bit_index = divmod(position, 8)
    if byte_index >= len(data):
        return 0
    return (data[byte_index] >> bit_index) & 1


def intel_positions(start: int, length: int) -> list[int]:
    """Intel: the start bit is the LSB, positions increase linearly."""
    return [start + index for index in range(length)]


def motorola_positions(start: int, length: int) -> list[int]:
    """Motorola: the start bit is the MSB, positions walk down within a byte.

    The DBC bit numbering is "sawtooth": byte N covers bits 8N..8N+7 with bit 8N
    being the least significant bit of that byte. From the MSB the sequence
    steps -1, except that when a byte's bit 0 is passed it jumps to the next
    byte's bit 7 (i.e. +15 in this numbering).
    """
    positions = [start]
    current = start
    for _ in range(length - 1):
        if current % 8 == 0:
            current += 15
        else:
            current -= 1
        positions.append(current)
    return positions


def extract_raw(data: bytes, signal: dict[str, Any]) -> int:
    """Extract a signal's raw integer, honouring the two different bit orders.

    Intel (``@1``): positions are ordered LSB-first, so position ``i`` carries
    weight ``2**i`` and the bits are accumulated with a shift.

    Motorola (``@0``): ``motorola_positions`` already returns the bits MSB-first,
    so accumulating left-to-right reproduces the value. Using one accumulation
    order for both silently bit-reverses every Intel signal.
    """
    if signal["order"] == "intel":
        raw = 0
        for index, bit in enumerate(intel_positions(signal["start"], signal["length"])):
            raw |= _bit_at(data, bit) << index
        return raw
    raw = 0
    for bit in motorola_positions(signal["start"], signal["length"]):
        raw = (raw << 1) | _bit_at(data, bit)
    return raw


def apply_signed(raw: int, length: int) -> int:
    if raw & (1 << (length - 1)):
        return raw - (1 << length)
    return raw


def decode_message(message: dict[str, Any], data: bytes) -> dict[str, Any]:
    signals: list[dict[str, Any]] = []
    selector_value = None
    for signal in message["signals"].values():
        if signal["mux_kind"] == "selector":
            selector_value = apply_signed(extract_raw(data, signal), signal["length"]) if signal["signed"] else extract_raw(data, signal)

    for signal in message["signals"].values():
        if signal["mux_kind"] == "multiplexed":
            if selector_value is None:
                signals.append({"name": signal["name"], "status": "skipped", "reason": "no multiplexer selector in this message"})
                continue
            if signal["mux_value"] != selector_value:
                signals.append(
                    {
                        "name": signal["name"],
                        "status": "skipped",
                        "reason": f"multiplexer value {selector_value} does not select m{signal['mux_value']}",
                    }
                )
                continue
        raw = extract_raw(data, signal)
        signed_value = apply_signed(raw, signal["length"]) if signal["signed"] else raw
        physical = signed_value * signal["factor"] + signal["offset"]
        entry: dict[str, Any] = {
            "name": signal["name"],
            "status": "decoded",
            "raw": signed_value,
            "factor": signal["factor"],
            "offset": signal["offset"],
            "unit": signal["unit"],
            "physical": round(physical, 6),
            "start": signal["start"],
            "length": signal["length"],
            "order": signal["order"],
            "mux": signal["mux_kind"],
        }
        if signal["enumerated"].get(signed_value) is not None:
            entry["meaning"] = signal["enumerated"][signed_value]
        if physical < signal["minimum"] or physical > signal["maximum"]:
            entry["out_of_range"] = True
        signals.append(entry)

    return {
        "id": message["id"],
        "id_hex": f"0x{message['id']:03x}",
        "name": message["name"],
        "dlc": message["dlc"],
        "length": len(data),
        "data": data.hex().upper(),
        "selector": selector_value,
        "signals": signals,
        "status": "decoded" if len(data) == message["dlc"] else "dlc_mismatch",
    }


def parse_candump(text: str) -> list[tuple[int, bytes]]:
    frames: list[tuple[int, bytes]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = CANDUMP_RE.match(line)
        if not match:
            continue
        frame = match.group("frame")
        frame_match = FRAME_RE.match(frame)
        if not frame_match:
            continue
        can_id = int(frame_match.group("can_id"), 16)
        payload = bytes.fromhex(frame_match.group("data"))
        frames.append((can_id, payload))
    return frames


def parse_inline_hex(spec: str) -> list[tuple[int, bytes]]:
    frames: list[tuple[int, bytes]] = []
    for chunk in spec.split(","):
        item = chunk.strip()
        if not item:
            continue
        match = FRAME_RE.match(item)
        if not match:
            raise DbcError(f"cannot parse --hex entry {item!r}; use ID#DATA (hex)")
        frames.append((int(match.group("can_id"), 16), bytes.fromhex(match.group("data"))))
    return frames


def format_report(payload: dict[str, Any]) -> str:
    lines = []
    for frame in payload["frames"]:
        if frame["status"] == "unknown":
            lines.append(f"{frame['id_hex']} {frame['data']}  UNKNOWN ID: {frame['reason']}")
            continue
        header = f"{frame['id_hex']} {frame['name']} [{frame['data']}] dlc={frame['dlc']}"
        if frame["status"] == "dlc_mismatch":
            header += f"  WARNING: frame has {frame['length']} bytes, DBC declares {frame['dlc']}"
        lines.append(header)
        for signal in frame["signals"]:
            if signal["status"] != "decoded":
                lines.append(f"    {signal['name']:<18} skipped: {signal['reason']}")
                continue
            meaning = f"  -> {signal['meaning']}" if signal.get("meaning") else ""
            flag = "  OUT OF RANGE" if signal.get("out_of_range") else ""
            lines.append(
                f"    {signal['name']:<18} {signal['physical']:>12.3f} {signal['unit']:<6}"
                f"(raw {signal['raw']}, {signal['order']}){meaning}{flag}"
            )
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
        prog="dbc_decode.py",
        description=(
            "Decode candump frames into physical signal values using a Vector DBC file. "
            "Unknown IDs and DLC mismatches are reported as ambiguity, never silently skipped."
        ),
    )
    parser.add_argument("--dbc", required=True, help="Vector DBC file")
    parser.add_argument("--log", help="candump log file ((timestamp) can0 ID#DATA)")
    parser.add_argument("--hex", help="inline frames, e.g. '7E0#0322F190,1A0#11223344'")
    parser.add_argument("--filter-id", dest="filter_id", help="only decode this CAN ID (hex like 1A0)")
    parser.add_argument("--output", help="also write the JSON result to this path")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a text summary")
    parser.add_argument("--version", action="version", version="embedded-dev dbc_decode 0.8.0")
    args = parser.parse_args(argv)

    try:
        dbc_path = Path(args.dbc)
        if not dbc_path.is_file():
            raise DbcError(f"DBC not found: {dbc_path}")
        model = parse_dbc(dbc_path.read_text(encoding="utf-8", errors="replace"))

        if args.log:
            log_path = Path(args.log)
            if not log_path.is_file():
                raise DbcError(f"candump log not found: {log_path}")
            frames = parse_candump(log_path.read_text(encoding="utf-8", errors="replace"))
        elif args.hex:
            frames = parse_inline_hex(args.hex)
        else:
            raise DbcError("give one of --log or --hex")

        if not frames:
            raise DbcError("no CAN frames found; refusing to report an empty decode")

        wanted = int(args.filter_id, 16) if args.filter_id else None
        if wanted is not None:
            frames = [(can_id, data) for can_id, data in frames if can_id == wanted]
            if not frames:
                raise DbcError(f"no frames with ID 0x{wanted:03x} in the input")

        results: list[dict[str, Any]] = []
        ambiguous = 0
        for can_id, data in frames:
            message = model["messages"].get(can_id)
            if message is None:
                results.append(
                    {
                        "id": can_id,
                        "id_hex": f"0x{can_id:03x}",
                        "data": data.hex().upper(),
                        "length": len(data),
                        "status": "unknown",
                        "reason": "CAN ID is not defined in the DBC",
                    }
                )
                ambiguous += 1
                continue
            decoded = decode_message(message, data)
            if decoded["status"] == "dlc_mismatch":
                ambiguous += 1
            results.append(decoded)

        payload = {
            "schema_version": "can-signals.v1",
            "dbc": str(args.dbc),
            "frame_count": len(results),
            "ambiguous_count": ambiguous,
            "frames": results,
        }
    except DbcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered if args.json else format_report(payload))
    return EXIT_AMBIGUOUS if ambiguous else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())