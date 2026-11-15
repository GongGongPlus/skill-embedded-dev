#!/usr/bin/env python3
"""Protocol decoding for UART frame specs, CAN/ISO-TP, UDS, OBD-II and J1939.

This module turns a *captured byte stream* or a CAN log into structured evidence
instead of prose guesses. It is deliberately data-driven: a UART protocol is
described by a JSON spec (``embedded-protocol.v1``) and decoded byte-for-byte;
unknown bytes are reported raw, never invented.

Design rules (see SKILL.md and references/protocols.md):
- No guessing. A missing spec, checksum rule or length rule is a config error
  (exit 6), not a heuristic.
- Every decoded field keeps its raw bytes, so a wrong scale/offset hypothesis can
  be corrected without re-capturing the bus.
- Frame-level problems (bad checksum, truncated frame, stray bytes) are returned
  as data with offsets; ``--strict`` turns them into a non-zero exit for CI.

Exit codes (same contract as embedded_dev.py):
  0 decoded, 4 decode errors with --strict, 6 spec/config error.

The design is inspired by public projects (ideas only, no code copied):
- serial-protocol-tester-skill (PolyForm Noncommercial): spec-driven UART decode.
- Lissio (Apache-2.0): CAN ISO-TP / UDS / OBD-II / J1939 operator tooling.
See THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_DECODE = 4
EXIT_CONFIG = 6
SCHEMA_VERSION = "embedded-protocol.v1"
DECODE_SCHEMA = "embedded-decode.v1"

UART_TYPES = ("uint8", "uint16", "uint32", "uint64", "int8", "int16", "int32", "bytes", "ascii", "bool")
CHECKSUMS = {
    "none": 0,
    "sum8": 1,
    "xor8": 1,
    "crc8": 1,
    "crc16_modbus": 2,
    "crc16_ccitt": 2,
}

UDS_SERVICES = {
    0x10: "DiagnosticSessionControl",
    0x11: "ECUReset",
    0x14: "ClearDiagnosticInformation",
    0x19: "ReadDTCInformation",
    0x22: "ReadDataByIdentifier",
    0x23: "ReadMemoryByAddress",
    0x27: "SecurityAccess",
    0x28: "CommunicationControl",
    0x2E: "WriteDataByIdentifier",
    0x2F: "InputOutputControlByIdentifier",
    0x31: "RoutineControl",
    0x34: "RequestDownload",
    0x35: "RequestUpload",
    0x36: "TransferData",
    0x37: "RequestTransferExit",
    0x3E: "TesterPresent",
    0x7F: "NegativeResponse",
    0x85: "ControlDTCSetting",
}

UDS_NRC = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x14: "responseTooLong",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x25: "noResponseFromSubnetComponent",
    0x26: "failurePreventsExecutionOfRequestedAction",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x36: "exceedNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired",
    0x70: "uploadDownloadNotAccepted",
    0x71: "transferDataSuspended",
    0x72: "generalProgrammingFailure",
    0x73: "wrongBlockSequenceCounter",
    0x78: "requestCorrectlyReceivedResponsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}

OBD2_PIDS = {
    0x04: ("engine_load", "%", lambda b: b[2] * 100.0 / 255.0),
    0x05: ("coolant_temp", "degC", lambda b: b[2] - 40),
    0x0C: ("engine_rpm", "rpm", lambda b: ((b[2] << 8) | b[3]) / 4.0),
    0x0D: ("vehicle_speed", "km/h", lambda b: b[2]),
    0x0F: ("intake_air_temp", "degC", lambda b: b[2] - 40),
    0x10: ("maf_rate", "g/s", lambda b: ((b[2] << 8) | b[3]) / 100.0),
    0x11: ("throttle", "%", lambda b: b[2] * 100.0 / 255.0),
    0x2F: ("fuel_level", "%", lambda b: b[2] * 100.0 / 255.0),
    0x42: ("control_module_voltage", "V", lambda b: ((b[2] << 8) | b[3]) / 1000.0),
    0x46: ("ambient_air_temp", "degC", lambda b: b[2] - 40),
    0x51: ("fuel_type", "", lambda b: b[2]),
}

J1939_PGNS = {
    0xF004: "EEC1 (electronic engine controller 1)",
    0xFEF1: "CCVS (cruise control / vehicle speed)",
    0xFECA: "DM1 (active diagnostic trouble codes)",
}

CANDUMP_RE = re.compile(
    r"^\s*(?:\((\d+(?:\.\d+)?)\)\s+)?(\S+)\s+([0-9A-Fa-f]{3,8})(##?)([0-9A-Fa-f]*)\s*$"
)


class ProtocolError(ValueError):
    """Spec or argument problem; mapped to exit code 6 by the CLI."""


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------

def _crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def _crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def _crc8(data: bytes) -> int:
    crc = 0x00
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def checksum_size(algorithm: str) -> int:
    if algorithm not in CHECKSUMS:
        raise ProtocolError(f"unsupported checksum algorithm: {algorithm!r}")
    return CHECKSUMS[algorithm]


def compute_checksum(data: bytes, algorithm: str) -> int:
    if algorithm == "none":
        return 0
    if algorithm == "sum8":
        return sum(data) & 0xFF
    if algorithm == "xor8":
        value = 0
        for byte in data:
            value ^= byte
        return value
    if algorithm == "crc8":
        return _crc8(data)
    if algorithm == "crc16_modbus":
        return _crc16_modbus(data)
    if algorithm == "crc16_ccitt":
        return _crc16_ccitt(data)
    raise ProtocolError(f"unsupported checksum algorithm: {algorithm!r}")


# ---------------------------------------------------------------------------
# Spec loading and validation
# ---------------------------------------------------------------------------

def load_spec(path: str | Path) -> dict:
    try:
        spec = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProtocolError(f"cannot read spec: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"spec is not valid JSON: {exc}") from exc
    if not isinstance(spec, dict):
        raise ProtocolError("spec must be a JSON object")
    return spec


def _parse_hex_bytes(text: str, what: str) -> bytes:
    cleaned = re.sub(r"[\s,\-_:]+", "", text or "")
    if len(cleaned) % 2:
        raise ProtocolError(f"{what} must contain an even number of hex digits")
    try:
        return bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ProtocolError(f"{what} is not valid hex: {exc}") from exc


def validate_spec(spec: dict) -> list[str]:
    errors: list[str] = []
    if spec.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION!r}")
    if not isinstance(spec.get("name"), str) or not spec.get("name", "").strip():
        errors.append("name must be a non-empty string")
    transport = spec.get("transport")
    if not isinstance(transport, dict) or transport.get("type") != "uart":
        errors.append("transport.type must be 'uart' for this decoder")
    framing = spec.get("framing")
    if not isinstance(framing, dict):
        return errors + ["framing must be an object"]
    try:
        header = _parse_hex_bytes(str(framing.get("header", "")), "framing.header")
        if not header:
            errors.append("framing.header must not be empty")
    except ProtocolError as exc:
        errors.append(str(exc))
        header = b""
    length = framing.get("length")
    if not isinstance(length, dict):
        errors.append("framing.length must be an object")
    else:
        size = length.get("size")
        if not isinstance(size, int) or not 1 <= size <= 4:
            errors.append("framing.length.size must be an integer 1..4")
        if length.get("endian", "big") not in ("big", "little"):
            errors.append("framing.length.endian must be 'big' or 'little'")
        if not isinstance(length.get("offset", 0), int):
            errors.append("framing.length.offset must be an integer")
    checksum = framing.get("checksum")
    if not isinstance(checksum, dict):
        errors.append("framing.checksum must be an object")
        checksum = {"algorithm": "none"}
    algorithm = checksum.get("algorithm", "none")
    if algorithm not in CHECKSUMS:
        errors.append(f"unsupported checksum algorithm: {algorithm!r}")
    elif checksum.get("over", "all_before_checksum") not in ("all_before_checksum", "payload"):
        errors.append("checksum.over must be 'all_before_checksum' or 'payload'")
    if framing.get("byte_order", "little") not in ("little", "big"):
        errors.append("framing.byte_order must be 'little' or 'big'")
    max_frame = framing.get("max_frame", 256)
    if not isinstance(max_frame, int) or max_frame < 8:
        errors.append("framing.max_frame must be an integer >= 8")
    fields = spec.get("fields")
    if not isinstance(fields, list) or not fields:
        errors.append("fields must be a non-empty list")
        fields = []
    names: set[str] = set()
    for index, field in enumerate(fields):
        prefix = f"fields[{index}]"
        if not isinstance(field, dict):
            errors.append(f"{prefix} must be an object")
            continue
        name = field.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{prefix}.name must be a non-empty string")
        elif name in names:
            errors.append(f"{prefix}.name {name!r} is duplicated")
        else:
            names.add(name)
        field_type = field.get("type", "uint8")
        if field_type not in UART_TYPES:
            errors.append(f"{prefix}.type must be one of {', '.join(UART_TYPES)}")
        size = field.get("size")
        if not isinstance(size, int) or size < 1:
            errors.append(f"{prefix}.size must be a positive integer")
        elif field_type in ("uint8", "int8") and size != 1:
            errors.append(f"{prefix}.size must be 1 for {field_type}")
        enum = field.get("enum")
        if enum is not None:
            if not isinstance(enum, dict):
                errors.append(f"{prefix}.enum must be an object")
            else:
                for key in enum:
                    try:
                        int(str(key), 0)
                    except ValueError:
                        errors.append(f"{prefix}.enum key {key!r} is not an integer")
        for numeric_key in ("scale", "value_offset"):
            if field.get(numeric_key) is not None and not isinstance(field[numeric_key], (int, float)):
                errors.append(f"{prefix}.{numeric_key} must be a number")
        bits = field.get("bits")
        if bits is not None:
            if not isinstance(bits, dict):
                errors.append(f"{prefix}.bits must be an object")
            else:
                for key in bits:
                    try:
                        bit = int(str(key), 0)
                    except ValueError:
                        errors.append(f"{prefix}.bits key {key!r} is not an integer")
                        continue
                    if isinstance(size, int) and not 0 <= bit < size * 8:
                        errors.append(f"{prefix}.bits key {key!r} exceeds field width")
    commands = spec.get("commands", [])
    if not isinstance(commands, list):
        errors.append("commands must be a list")
    else:
        for index, command in enumerate(commands):
            prefix = f"commands[{index}]"
            if not isinstance(command, dict) or not isinstance(command.get("name"), str):
                errors.append(f"{prefix} must be an object with a name")
                continue
            for key in ("request_type", "response_type"):
                if key in command and not isinstance(command[key], int):
                    errors.append(f"{prefix}.{key} must be an integer")
    return errors


# ---------------------------------------------------------------------------
# Field decoding
# ---------------------------------------------------------------------------

def _apply_scale(raw: int | float, field: dict):
    value = raw
    if field.get("scale") is not None:
        value = raw * float(field["scale"])
    # ``offset`` is the byte offset inside the payload; the numeric bias is
    # ``value_offset`` so the two can never be confused.
    if field.get("value_offset") is not None:
        value = value + float(field["value_offset"])
    if isinstance(value, float):
        value = round(value, 6)
    return value


def decode_fields(payload: bytes, fields: list[dict], byte_order: str = "little") -> tuple[dict, list[dict], list[str]]:
    decoded: dict = {}
    raw_fields: list[dict] = []
    warnings: list[str] = []
    cursor = 0
    for field in fields:
        name = field.get("name", "unnamed")
        offset = field.get("offset", cursor)
        if not isinstance(offset, int) or offset < 0:
            warnings.append(f"{name}: invalid offset {offset!r}; field skipped")
            continue
        size = field.get("size", 1)
        field_type = field.get("type", "uint8")
        chunk = payload[offset:offset + size]
        if len(chunk) != size:
            warnings.append(f"{name}: needs {size} byte(s) at offset {offset}, payload has {len(payload)}")
            continue
        cursor = max(cursor, offset + size)
        raw_hex = chunk.hex()
        if field_type == "bytes":
            value = chunk.hex()
        elif field_type == "ascii":
            value = chunk.decode("utf-8", errors="replace").rstrip("\x00")
        elif field_type == "bool":
            value = chunk != b"\x00" * size
            raw = int.from_bytes(chunk, byte_order)
        else:
            raw = int.from_bytes(chunk, byte_order, signed=field_type.startswith("int"))
            value = _apply_scale(raw, field)
        entry = {"name": name, "type": field_type, "offset": offset, "size": size, "raw_hex": raw_hex, "value": value}
        enum = field.get("enum")
        if isinstance(enum, dict) and field_type not in ("bytes", "ascii", "bool"):
            label = enum.get(str(raw))
            if label is None:
                warnings.append(f"{name}: value {raw} is not in the enum (unknown label)")
                entry["enum"] = None
            else:
                entry["enum"] = label
        bits = field.get("bits")
        if isinstance(bits, dict) and field_type not in ("bytes", "ascii"):
            bit_value = int.from_bytes(chunk, byte_order)
            set_names = [label for key, label in bits.items() if bit_value & (1 << int(str(key), 0))]
            entry["bits"] = set_names
        if field.get("unit"):
            entry["unit"] = field["unit"]
        decoded[name] = value if "enum" not in entry or entry["enum"] is None else entry["enum"]
        raw_fields.append(entry)
    return decoded, raw_fields, warnings


# ---------------------------------------------------------------------------
# UART stream decoding
# ---------------------------------------------------------------------------

def decode_uart_frame(spec: dict, blob: bytes, offset: int) -> tuple[dict | None, int, list[str]]:
    """Decode one frame starting at ``offset``; returns (frame, next_offset, errors)."""
    framing = spec["framing"]
    header = _parse_hex_bytes(str(framing["header"]), "framing.header")
    length = framing["length"]
    length_size = int(length["size"])
    length_offset = int(length.get("offset", 0))
    length_endian = length.get("endian", "big")
    checksum = framing.get("checksum", {"algorithm": "none"})
    algorithm = checksum.get("algorithm", "none")
    checksum_len = checksum_size(algorithm)
    includes_checksum = bool(length.get("includes_checksum", True))
    byte_order = framing.get("byte_order", "little")
    errors: list[str] = []
    start = offset
    if blob[offset:offset + len(header)] != header:
        return None, offset, [f"header not found at offset {offset}"]
    pos = offset + len(header) + length_offset
    if pos + length_size > len(blob):
        return None, len(blob), [f"truncated length field at offset {start}"]
    length_value = int.from_bytes(blob[pos:pos + length_size], length_endian)
    lf_end = pos + length_size
    if includes_checksum:
        total = lf_end + length_value
        payload_end = total - checksum_len
    else:
        payload_end = lf_end + length_value
        total = payload_end + checksum_len
    if total > len(blob):
        return None, len(blob), [f"truncated frame at offset {start}: need {total - start} byte(s), have {len(blob) - start}"]
    raw = blob[start:total]
    payload = blob[lf_end:payload_end]
    frame_checksum_raw = blob[payload_end:total]
    checksum_ok = None
    checksum_expected = None
    checksum_actual = None
    if algorithm != "none":
        actual = int.from_bytes(frame_checksum_raw, "little")
        checksum_actual = f"0x{actual:0{checksum_len * 2}x}"
        if checksum.get("over") == "payload":
            expected = compute_checksum(payload, algorithm)
        else:
            expected = compute_checksum(blob[start:payload_end], algorithm)
        checksum_expected = f"0x{expected:0{checksum_len * 2}x}"
        checksum_ok = expected == actual
        if not checksum_ok:
            errors.append(f"checksum mismatch at offset {start}: expected {checksum_expected}, got {checksum_actual}")
    decoded, field_entries, warnings = decode_fields(payload, spec.get("fields", []), byte_order)
    covered = set()
    for entry in field_entries:
        covered.update(range(entry["offset"], entry["offset"] + entry["size"]))
    unknown = bytes(byte for index, byte in enumerate(payload) if index not in covered)
    direction = "unknown"
    command = None
    for candidate in spec.get("commands", []):
        probe = decoded.get("msg_type", decoded.get("type"))
        if candidate.get("request_type") == probe:
            command, direction = candidate.get("name"), "request"
        elif candidate.get("response_type") == probe:
            command, direction = candidate.get("name"), "response"
    frame = {
        "offset": start,
        "length": total - start,
        "length_value": length_value,
        "raw_hex": raw.hex(),
        "payload_hex": payload.hex(),
        "command": command,
        "direction": direction,
        "fields": decoded,
        "field_details": field_entries,
        "unknown_payload_hex": unknown.hex(),
        "checksum": {"algorithm": algorithm, "ok": checksum_ok, "expected": checksum_expected, "actual": checksum_actual},
        "errors": errors + warnings,
    }
    return frame, total, errors


def decode_uart_stream(spec: dict, data: bytes) -> dict:
    framing = spec["framing"]
    header = _parse_hex_bytes(str(framing["header"]), "framing.header")
    frames: list[dict] = []
    errors: list[str] = []
    skipped: list[str] = []
    pos = 0
    guard = 0
    while pos < len(data) and guard < 100000:
        guard += 1
        found = data.find(header, pos)
        if found < 0:
            tail = data[pos:]
            if tail:
                skipped.append(tail.hex())
                errors.append(f"trailing {len(tail)} byte(s) after last frame: {tail.hex()}")
            break
        if found > pos:
            skipped.append(data[pos:found].hex())
        frame, next_pos, frame_errors = decode_uart_frame(spec, data, found)
        if frame is None:
            errors.extend(frame_errors)
            pos = max(next_pos, found + 1)
            continue
        frames.append(frame)
        pos = next_pos
    result = {
        "schema_version": DECODE_SCHEMA,
        "kind": "uart",
        "protocol": spec.get("name"),
        "bytes": len(data),
        "frames": frames,
        "skipped_hex": skipped,
        "errors": errors,
        "summary": {
            "frames": len(frames),
            "checksum_failures": sum(1 for frame in frames if frame["checksum"]["ok"] is False),
            "frames_with_errors": sum(1 for frame in frames if frame["errors"]),
            "decode_errors": len(errors),
        },
    }
    return result


# ---------------------------------------------------------------------------
# CAN / ISO-TP / UDS / OBD-II / J1939
# ---------------------------------------------------------------------------

def parse_candump_line(line: str) -> dict | None:
    match = CANDUMP_RE.match(line)
    if not match:
        return None
    timestamp, interface, can_id_hex, sep, data_hex = match.groups()
    can_id = int(can_id_hex, 16)
    extended = len(can_id_hex) > 3
    data = bytes.fromhex(data_hex) if data_hex else b""
    if sep == "##" and len(data) and len(data_hex) % 2:
        # CAN FD candump may prefix a flags nibble; keep it visible instead of guessing.
        pass
    return {
        "timestamp": float(timestamp) if timestamp else None,
        "interface": interface,
        "id": can_id,
        "id_hex": f"0x{can_id:0{8 if extended else 3}X}",
        "extended": extended,
        "fd": sep == "##",
        "data": data,
        "data_hex": data.hex(),
    }


def _isotp_decode(frames: list[dict]) -> tuple[list[dict], list[dict]]:
    """Reassemble ISO-TP messages; return (messages, raw_frames)."""
    messages: list[dict] = []
    raw: list[dict] = []
    pending: dict[tuple, dict] = {}
    for frame in frames:
        key = (frame["interface"], frame["id"])
        data = frame["data"]
        if not data:
            raw.append({**frame, "transport": "raw", "note": "empty CAN frame"})
            continue
        pci = data[0] >> 4
        if pci == 0x0:
            length = data[0] & 0x0F
            payload = data[1:1 + length]
            messages.append({"interface": frame["interface"], "id": frame["id"], "id_hex": frame["id_hex"], "extended": frame["extended"], "length": length, "payload_hex": payload.hex(), "status": "complete" if len(payload) == length else "truncated", "frames": 1})
        elif pci == 0x1:
            length = ((data[0] & 0x0F) << 8) | data[1]
            pending[key] = {"length": length, "data": bytearray(data[2:]), "frames": 1, "interface": frame["interface"], "id": frame["id"], "id_hex": frame["id_hex"], "extended": frame["extended"]}
        elif pci == 0x2:
            state = pending.get(key)
            if state is None:
                raw.append({**frame, "transport": "raw", "note": "ISO-TP consecutive frame without a first frame"})
                continue
            state["data"].extend(data[1:])
            state["frames"] += 1
            if len(state["data"]) >= state["length"]:
                payload = bytes(state["data"][:state["length"]])
                messages.append({"interface": state["interface"], "id": state["id"], "id_hex": state["id_hex"], "extended": state["extended"], "length": state["length"], "payload_hex": payload.hex(), "status": "complete", "frames": state["frames"]})
                del pending[key]
        elif pci == 0x3:
            raw.append({**frame, "transport": "isotp-flow-control", "note": "flow control frame"})
        else:
            raw.append({**frame, "transport": "raw", "note": "PCI nibble does not look like ISO-TP"})
    messages.extend(
        {"interface": state["interface"], "id": state["id"], "id_hex": state["id_hex"], "extended": state["extended"], "length": state["length"], "payload_hex": bytes(state["data"]).hex(), "status": "pending" if len(state["data"]) < state["length"] else "complete", "frames": state["frames"]}
        for state in pending.values()
    )
    return messages, raw


def decode_uds(payload: bytes) -> dict | None:
    if not payload:
        return None
    service = payload[0]
    if service == 0x7F and len(payload) >= 3:
        nrc = payload[2]
        return {
            "protocol": "uds",
            "service": "0x7F",
            "service_name": "NegativeResponse",
            "request_service": UDS_SERVICES.get(payload[1], f"0x{payload[1]:02X}"),
            "nrc": f"0x{nrc:02X}",
            "nrc_name": UDS_NRC.get(nrc, "unknownNRC"),
            "payload_hex": payload.hex(),
        }
    if service not in UDS_SERVICES:
        return None
    record = {"protocol": "uds", "service": f"0x{service:02X}", "service_name": UDS_SERVICES[service], "payload_hex": payload.hex()}
    if service in (0x22, 0x2E, 0x2F) and len(payload) >= 3:
        record["did"] = f"0x{int.from_bytes(payload[1:3], 'big'):04X}"
    if service in (0x10, 0x11, 0x19, 0x27, 0x28, 0x31, 0x3E, 0x85) and len(payload) >= 2:
        record["sub_function"] = f"0x{payload[1]:02X}"
    if service == 0x31 and len(payload) >= 4:
        record["routine_id"] = f"0x{int.from_bytes(payload[2:4], 'big'):04X}"
    if service == 0x19 and len(payload) >= 3:
        record["status_mask"] = f"0x{payload[2]:02X}"
    return record


def decode_obd2(payload: bytes) -> dict | None:
    if not payload or not 0x41 <= payload[0] <= 0x4A:
        return None
    mode = payload[0] - 0x40
    if len(payload) < 2:
        return {"protocol": "obd2", "mode": mode, "payload_hex": payload.hex(), "note": "missing PID"}
    pid = payload[1]
    name, unit, formula = OBD2_PIDS.get(pid, (f"pid_0x{pid:02X}", "", None))
    record = {"protocol": "obd2", "mode": mode, "pid": f"0x{pid:02X}", "name": name, "payload_hex": payload.hex()}
    if formula is not None:
        try:
            record["value"] = round(float(formula(payload)), 6)
            record["unit"] = unit
        except (IndexError, ValueError):
            record["note"] = "payload too short for this PID"
    return record


def decode_j1939(message: dict) -> dict | None:
    can_id = message["id"]
    if not message["extended"] or can_id > 0x1FFFFFFF:
        return None
    priority = (can_id >> 26) & 0x07
    dp = (can_id >> 24) & 0x01
    pf = (can_id >> 16) & 0xFF
    ps = (can_id >> 8) & 0xFF
    sa = can_id & 0xFF
    pgn = (dp << 16) | (pf << 8) | (ps if pf >= 0xF0 else 0)
    payload = bytes.fromhex(message["payload_hex"])
    return {
        "protocol": "j1939",
        "priority": priority,
        "pgn": f"0x{pgn:04X}",
        "pgn_name": J1939_PGNS.get(pgn, "unknown PGN (check the engine/vehicle spec)"),
        "source_address": f"0x{sa:02X}",
        "destination": "broadcast" if pf < 0xF0 else f"0x{ps:02X}",
        "payload_hex": message["payload_hex"],
        "note": "single-frame decode; BAM/TP reassembly is not attempted" if len(payload) > 8 else None,
    }


def decode_can_log(text: str) -> dict:
    frames: list[dict] = []
    errors: list[str] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        frame = parse_candump_line(line)
        if frame is None:
            errors.append(f"line {line_number}: not candump format: {raw_line!r}")
            continue
        frames.append(frame)
    messages, raw = _isotp_decode(frames)
    decoded_messages: list[dict] = []
    for message in messages:
        payload = bytes.fromhex(message["payload_hex"])
        record = dict(message)
        uds = decode_uds(payload)
        obd = decode_obd2(payload)
        j1939 = decode_j1939(message) if message["extended"] else None
        record["decoded"] = uds or obd or j1939
        decoded_messages.append(record)
    result = {
        "schema_version": DECODE_SCHEMA,
        "kind": "can",
        "frames": len(frames),
        "messages": decoded_messages,
        "raw_frames": raw,
        "errors": errors,
        "summary": {
            "can_frames": len(frames),
            "isotp_messages": len(messages),
            "uds": sum(1 for m in decoded_messages if m.get("decoded", {}) and m["decoded"].get("protocol") == "uds"),
            "obd2": sum(1 for m in decoded_messages if m.get("decoded", {}) and m["decoded"].get("protocol") == "obd2"),
            "j1939": sum(1 for m in decoded_messages if m.get("decoded", {}) and m["decoded"].get("protocol") == "j1939"),
            "parse_errors": len(errors),
        },
    }
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _emit(data: object, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def _read_input(hex_text: str | None, file_path: str | None, fmt: str) -> bytes:
    if hex_text:
        return _parse_hex_bytes(_strip_comments(hex_text), "--hex")
    if not file_path:
        raise ProtocolError("provide --hex or --file")
    raw = Path(file_path).read_bytes()
    if fmt == "bin":
        return raw
    text = _strip_comments(raw.decode("utf-8", errors="replace"))
    return _parse_hex_bytes(text, "--file (hex mode)")


def _strip_comments(text: str) -> str:
    return "\n".join(line for line in (text or "").splitlines() if not line.strip().startswith("#"))


def cmd_validate(args: argparse.Namespace) -> int:
    spec = load_spec(args.spec)
    errors = validate_spec(spec)
    _emit({"valid": not errors, "errors": errors, "schema_version": SCHEMA_VERSION}, args.json)
    return EXIT_OK if not errors else EXIT_CONFIG


def cmd_uart(args: argparse.Namespace) -> int:
    spec = load_spec(args.spec)
    errors = validate_spec(spec)
    if errors:
        _emit({"valid": False, "errors": errors}, args.json)
        return EXIT_CONFIG
    data = _read_input(args.hex, args.file, args.format)
    result = decode_uart_stream(spec, data)
    _emit(result, args.json)
    has_errors = result["summary"]["decode_errors"] or result["summary"]["checksum_failures"]
    return EXIT_DECODE if (args.strict and has_errors) else EXIT_OK


def cmd_can(args: argparse.Namespace) -> int:
    try:
        text = Path(args.log).read_text(encoding="utf-8")
    except OSError as exc:
        raise ProtocolError(f"cannot read --log: {exc}") from exc
    result = decode_can_log(text)
    _emit(result, args.json)
    return EXIT_DECODE if (args.strict and result["errors"]) else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="protocol_decode.py", description="Spec-driven UART / CAN / UDS / J1939 decoder.")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="validate an embedded-protocol.v1 JSON spec")
    validate.add_argument("--spec", required=True)
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(func=cmd_validate)
    uart = sub.add_parser("uart", help="decode a captured UART byte stream with a spec")
    uart.add_argument("--spec", required=True)
    uart.add_argument("--hex")
    uart.add_argument("--file")
    uart.add_argument("--format", default="hex", choices=["hex", "bin"])
    uart.add_argument("--strict", action="store_true", help="exit 4 when frame/checksum errors exist")
    uart.add_argument("--json", action="store_true")
    uart.set_defaults(func=cmd_uart)
    can = sub.add_parser("can", help="decode a candump log (ISO-TP, UDS, OBD-II, J1939)")
    can.add_argument("--log", required=True)
    can.add_argument("--strict", action="store_true", help="exit 4 when unparseable lines exist")
    can.add_argument("--json", action="store_true")
    can.set_defaults(func=cmd_can)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ProtocolError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG


if __name__ == "__main__":
    sys.exit(main())
