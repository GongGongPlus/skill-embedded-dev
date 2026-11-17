#!/usr/bin/env python3
"""Minimal MCP (Model Context Protocol) stdio server for the embedded-dev tool layer.

Why a wrapper instead of a new tool implementation: MCP clients (Claude Code,
Codex, DSH-style hosts and other agent runtimes) can already call tools, but the
hardware rules must stay in one place. This server exposes exactly the same
commands and exit-code contract as ``scripts/embedded_dev.py``; it adds no
guessing and no silent fallbacks.

Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout (MCP "stdio" transport).
Logs go to stderr so they never corrupt the protocol stream.

Supported methods: ``initialize``, ``notifications/initialized``, ``ping``,
``tools/list``, ``tools/call``.

Run without arguments to serve; ``--self-test`` performs an offline smoke test
(used by CI, no hardware required).

The server design (single tool layer, JSON results, offline self-test) was
inspired by public projects such as TaoCosmo-Dev/STM32_AutoDebug_Universal_Kit
(MIT) and Lissio (Apache-2.0); no code was copied. See THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
EMBEDDED_DEV = SCRIPT_DIR / "embedded_dev.py"
PROTOCOL_DECODE = SCRIPT_DIR / "protocol_decode.py"
PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "embedded-dev"


def read_version() -> str:
    try:
        return (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


# name -> (description, {argument: (flag, kind)}, append --json, positional prefix)
TOOLS: dict[str, dict] = {
    "env": {
        "description": "Detect build/flash backends, probes, serial ports and optional tools. Read-only.",
        "flags": {"strict": ("--strict", "bool")},
        "json": True,
        "prefix": ["env"],
    },
    "build": {
        "description": "Configure and build a firmware project (cmake | platformio | idf). Use dry_run first.",
        "flags": {
            "project": ("--project", "value"),
            "backend": ("--backend", "value"),
            "build_dir": ("--build-dir", "value"),
            "target": ("--target", "value"),
            "platformio_env": ("--platformio-env", "value"),
            "dry_run": ("--dry-run", "bool"),
        },
        "required": ["project"],
        "json": True,
        "prefix": ["build"],
    },
    "flash": {
        "description": "Flash an image with an explicit backend; every backend requires its identifying options.",
        "flags": {
            "image": ("--image", "value"),
            "backend": ("--backend", "value"),
            "chip": ("--chip", "value"),
            "target": ("--target", "value"),
            "port": ("--port", "value"),
            "baud": ("--baud", "value"),
            "probe": ("--probe", "value"),
            "openocd_interface": ("--openocd-interface", "value"),
            "openocd_target": ("--openocd-target", "value"),
            "jlink_device": ("--jlink-device", "value"),
            "snr": ("--snr", "value"),
            "project": ("--project", "value"),
            "dry_run": ("--dry-run", "bool"),
        },
        "required": ["image"],
        "json": True,
        "prefix": ["flash"],
    },
    "monitor": {
        "description": "Read a serial port until the expected token appears; mock_file enables offline runs.",
        "flags": {
            "port": ("--port", "value"),
            "baud": ("--baud", "value"),
            "expect": ("--expect", "value"),
            "send": ("--send", "value"),
            "timeout": ("--timeout", "value"),
            "mock_file": ("--mock-file", "value"),
            "dry_run": ("--dry-run", "bool"),
        },
        "json": True,
        "prefix": ["monitor"],
    },
    "regs": {
        "description": "Decode a register dump or fault registers, optionally resolving PC/LR through an ELF.",
        "flags": {
            "values": ("--values", "value"),
            "from_text": ("--from-text", "value"),
            "elf": ("--elf", "value"),
            "dry_run": ("--dry-run", "bool"),
        },
        "json": True,
        "prefix": ["regs"],
    },
    "symbolize": {
        "description": (
            "Resolve crash addresses to function:line with addr2line for Cortex-M, Xtensa or RISC-V builds. "
            "Requires the ELF from the exact build on the board."
        ),
        "flags": {"elf": ("--elf", "value"), "addr": ("--addr", "value")},
        "required": ["elf", "addr"],
        "json": True,
        "prefix": ["symbolize"],
    },
    "stack": {
        "description": (
            "Report FreeRTOS task stack watermarks from vTaskList / uxTaskGetSystemState output; "
            "exits 3 when a task falls below the threshold."
        ),
        "flags": {
            "tasklist": ("--tasklist", "value"),
            "min_free": ("--min-free", "value"),
            "word_bytes": ("--word-bytes", "value"),
        },
        "json": True,
        "required": ["tasklist"],
        "ok_exit": [0, 3],
        "prefix": ["stack"],
    },
    "fault": {
        "description": "Decode CFSR/HFSR/MMFAR/BFAR and fault addresses; optional ELF symbol resolution (DWARF gate).",
        "flags": {
            "cfsr": ("--cfsr", "value"),
            "hfsr": ("--hfsr", "value"),
            "mmfar": ("--mmfar", "value"),
            "bfar": ("--bfar", "value"),
            "pc": ("--pc", "value"),
            "lr": ("--lr", "value"),
            "psp": ("--psp", "value"),
            "msp": ("--msp", "value"),
            "elf": ("--elf", "value"),
        },
        "json": True,
        "prefix": ["fault"],
        # A decoded fault bit is a successful diagnosis, not a tool failure:
        # embedded_dev.py returns EXIT_FAULT=3 and the JSON payload is the result.
        "ok_exit": [0, 3],
    },
    "decode": {
        "description": "Decode UART captures from an embedded-protocol.v1 spec, or candump logs (ISO-TP/UDS/OBD-II/J1939).",
        "flags": {
            "kind": ("--kind", "value"),
            "spec": ("--spec", "value"),
            "hex": ("--hex", "value"),
            "file": ("--file", "value"),
            "format": ("--format", "value"),
            "log": ("--log", "value"),
            "strict": ("--strict", "bool"),
        },
        "required": ["kind"],
        "json": True,
        "prefix": ["decode"],
    },
    "report": {
        "description": "Validate a completion report against the evidence schema (or emit an example).",
        "flags": {"file": ("--file", "value"), "emit_example": ("--emit-example", "bool")},
        "json": True,
        "prefix": ["report"],
    },
    "hil": {
        "description": "Hardware-in-the-loop loop: build -> flash -> monitor token -> diagnostic_report.json; detects repeated failures.",
        "flags": {
            "project": ("--project", "value"),
            "image": ("--image", "value"),
            "expect": ("--expect", "value"),
            "port": ("--port", "value"),
            "baud": ("--baud", "value"),
            "timeout": ("--timeout", "value"),
            "build_backend": ("--build-backend", "value"),
            "flash_backend": ("--flash-backend", "value"),
            "mock_file": ("--mock-file", "value"),
            "report_out": ("--report-out", "value"),
            "previous": ("--previous", "value"),
            "dry_run": ("--dry-run", "bool"),
        },
        "required": ["project"],
        "json": True,
        "prefix": ["hil"],
    },
    "tune": {
        "description": (
            "Control-loop evidence: analyze telemetry CSV metrics, validate a bounded PID update, "
            "simulate the first-order thermal demo, or validate a trial record. Never certifies convergence."
        ),
        "action": ["analyze", "check-update", "simulate", "record"],
        "flags": {
            "csv": ("--csv", "value"),
            "control_limits": ("--control-limits", "value"),
            "settling_band": ("--settling-band", "value"),
            "tail_fraction": ("--tail-fraction", "value"),
            "file": ("--file", "value"),
            "config": ("--config", "value"),
            "out": ("--out", "value"),
            "force": ("--force", "bool"),
        },
        "required": ["action"],
        "json": True,
        "prefix": ["tune"],
    },
    "board": {
        "description": (
            "Hardware facts: extract a controller pin map from a KiCad XML netlist, or read pins and "
            "alternate functions from a CubeMX .ioc file. Never guesses the controller."
        ),
        "action": ["controller", "from-ioc"],
        "flags": {
            "netlist": ("--netlist", "value"),
            "main_controller": ("--main-controller", "value"),
            "file": ("--file", "value"),
            "output": ("--output", "value"),
        },
        "required": ["action"],
        "json": True,
        "prefix": ["board"],
    },
    "memory": {
        "description": (
            "Project archive and evidence-graded memory: detect the toolchain/chip, initialize, show, "
            "remember with provenance, search, list and forget. Local evidence, not hardware acceptance."
        ),
        "action": ["detect", "init", "show", "remember", "search", "list", "forget"],
        "flags": {
            "project": ("--project", "value"),
            "title": ("--title", "value"),
            "content": ("--content", "value"),
            "scope": ("--scope", "value"),
            "evidence_level": ("--evidence-level", "value"),
            "source": ("--source", "value"),
            "tags": ("--tags", "value"),
            "expected_version": ("--expected-version", "value"),
            "allow_recreate": ("--allow-recreate", "bool"),
            "query": ("--query", "value"),
            "limit": ("--limit", "value"),
            "recent": ("--recent", "value"),
            "all": ("--all", "bool"),
            "id": ("--id", "value"),
            "force": ("--force", "bool"),
        },
        "required": ["action"],
        "json": True,
        "prefix": ["memory"],
    },
    "map": {
        "description": (
            "Flash/RAM usage from a GNU ld map file, with optional budget and growth gates. "
            "Exits 1 when over budget; an unrecognised map is a configuration error."
        ),
        "flags": {
            "map": ("--map", "value"),
            "top": ("--top", "value"),
            "max_flash": ("--max-flash", "value"),
            "max_ram": ("--max-ram", "value"),
            "baseline": ("--baseline", "value"),
            "write_baseline": ("--write-baseline", "value"),
            "max_growth": ("--max-growth", "value"),
            "output": ("--output", "value"),
        },
        "required": ["map"],
        "json": True,
        "ok_exit": [0, 1],
        "prefix": ["map"],
    },
    "svd": {
        "description": (
            "Decode peripheral registers into named bitfields using a CMSIS-SVD file. Undefined bits "
            "are reported as reserved; addresses outside every peripheral exit 4."
        ),
        "flags": {
            "svd": ("--svd", "value"),
            "dump": ("--dump", "value"),
            "values": ("--values", "value"),
            "peripheral": ("--peripheral", "value"),
            "find": ("--find", "value"),
            "output": ("--output", "value"),
        },
        "required": ["svd"],
        "json": True,
        "ok_exit": [0, 4],
        "prefix": ["svd"],
    },
    "dbc": {
        "description": (
            "Decode candump frames into physical signal values using a Vector DBC file. Handles Intel "
            "and Motorola bit order, mux branches and VAL_ enumerations."
        ),
        "flags": {
            "dbc": ("--dbc", "value"),
            "log": ("--log", "value"),
            "hex": ("--hex", "value"),
            "filter_id": ("--filter-id", "value"),
            "output": ("--output", "value"),
        },
        "required": ["dbc"],
        "json": True,
        "ok_exit": [0, 4],
        "prefix": ["dbc"],
    },
    "rtt": {
        "description": (
            "Capture a SEGGER RTT channel from a control-block dump or over J-Link. A missing token "
            "exits 4; the live path needs a probe and should be dry-run first."
        ),
        "flags": {
            "control_block_file": ("--control-block-file", "value"),
            "buffer_file": ("--buffer-file", "value"),
            "buffer_index": ("--buffer-index", "value"),
            "jlink": ("--jlink", "value"),
            "device": ("--device", "value"),
            "control_block_address": ("--control-block-address", "value"),
            "expect": ("--expect", "value"),
            "dry_run": ("--dry-run", "bool"),
            "output": ("--output", "value"),
        },
        "json": True,
        "ok_exit": [0, 4],
        "prefix": ["rtt"],
    },
    "size": {
        "description": (
            "Text/data/bss budget gate from an ELF or a saved `size -A` transcript, with baseline "
            "growth detection. Exits 1 when over budget."
        ),
        "flags": {
            "elf": ("--elf", "value"),
            "size_output_file": ("--size-output-file", "value"),
            "max_text": ("--max-text", "value"),
            "max_data": ("--max-data", "value"),
            "max_bss": ("--max-bss", "value"),
            "baseline": ("--baseline", "value"),
            "write_baseline": ("--write-baseline", "value"),
            "max_growth": ("--max-growth", "value"),
            "output": ("--output", "value"),
        },
        "json": True,
        "ok_exit": [0, 1],
        "prefix": ["size"],
    },
}


def build_argv(name: str, arguments: dict) -> list[str]:
    spec = TOOLS.get(name)
    if spec is None:
        raise ValueError(f"unknown tool: {name}")
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    for required in spec.get("required", []):
        if required not in arguments:
            raise ValueError(f"missing required argument: {required}")
    argv = list(spec["prefix"])
    action_enum = spec.get("action")
    if action_enum:
        action = arguments.get("action")
        if action not in action_enum:
            raise ValueError(f"{name}.action must be one of: {', '.join(action_enum)}")
        argv.append(str(action))
    if name == "decode":
        kind = arguments.get("kind")
        if kind not in ("uart", "can", "validate"):
            raise ValueError("decode.kind must be one of: uart, can, validate")
        argv.append(kind)
    for key, value in arguments.items():
        if key == "kind" and name == "decode":
            continue
        if key == "action" and action_enum:
            continue
        mapping = spec["flags"].get(key)
        if mapping is None:
            raise ValueError(f"unsupported argument for {name}: {key}")
        flag, kind = mapping
        if kind == "bool":
            if value:
                argv.append(flag)
        else:
            argv.extend([flag, str(value)])
    if spec.get("json"):
        argv.append("--json")
    return argv


def call_tool(name: str, arguments: dict, timeout: float = 180.0) -> dict:
    argv = build_argv(name, arguments)
    ok_exit = set(TOOLS.get(name, {}).get("ok_exit", [0]))
    try:
        proc = subprocess.run(
            [sys.executable, str(EMBEDDED_DEV), *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"content": [{"type": "text", "text": f"tool {name} timed out after {timeout}s"}], "isError": True}
    text = (proc.stdout or "").strip()
    if proc.returncode not in ok_exit:
        detail = (proc.stderr or "").strip() or text
        return {"content": [{"type": "text", "text": detail or f"exit code {proc.returncode}"}], "isError": True}
    return {"content": [{"type": "text", "text": text}], "isError": False}


def tools_list() -> list[dict]:
    result = []
    for name, spec in TOOLS.items():
        properties = {}
        required = list(spec.get("required", []))
        action_enum = spec.get("action")
        if action_enum:
            properties["action"] = {"type": "string", "enum": list(action_enum)}
            if "action" not in required:
                required.append("action")
        for argument, (_, kind) in spec["flags"].items():
            if argument == "kind":
                properties[argument] = {"type": "string", "enum": ["uart", "can", "validate"]}
            elif kind == "bool":
                properties[argument] = {"type": "boolean"}
            else:
                properties[argument] = {"type": "string"}
        result.append(
            {
                "name": name,
                "description": spec["description"],
                "inputSchema": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
            }
        )
    return result


def handle_request(request: object) -> dict | None:
    if not isinstance(request, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
    method = request.get("method")
    request_id = request.get("id")
    is_notification = "id" not in request
    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": read_version()},
        }
    elif method == "notifications/initialized":
        return None
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tools_list()}
    elif method == "tools/call":
        params = request.get("params") or {}
        try:
            result = call_tool(str(params.get("name", "")), params.get("arguments") or {})
        except ValueError as exc:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": str(exc)}}
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"method not found: {method}"}}
    if is_notification:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def self_test() -> int:
    failures: list[str] = []
    listed = tools_list()
    names = {tool["name"] for tool in listed}
    for required in (
        "env",
        "fault",
        "decode",
        "hil",
        "build",
        "flash",
        "monitor",
        "regs",
        "symbolize",
        "stack",
        "report",
        "tune",
        "board",
        "memory",
        "map",
        "svd",
        "dbc",
        "rtt",
        "size",
    ):
        if required not in names:
            failures.append(f"tool {required} missing from tools/list")
    if len(listed) != 19:
        failures.append(f"tools/list has {len(listed)} tools, expected 19")
    env = call_tool("env", {})
    if env.get("isError"):
        failures.append("tools/call env failed: " + env["content"][0]["text"][:200])
    else:
        try:
            payload = json.loads(env["content"][0]["text"])
            if "version" not in payload or "tools" not in payload:
                failures.append("env JSON lacks version/tools")
        except json.JSONDecodeError:
            failures.append("env did not return JSON")
    fault = call_tool("fault", {"cfsr": "0x02000000"})
    if fault.get("isError") or "DIVBYZERO" not in fault["content"][0]["text"]:
        failures.append("fault decode did not report DIVBYZERO for CFSR bit 25")
    spec = REPO_ROOT / "examples" / "protocols" / "uart-env-sensor.json"
    if spec.is_file():
        decoded = call_tool("decode", {"kind": "validate", "spec": str(spec)})
        if decoded.get("isError"):
            failures.append("decode validate failed on the bundled example spec")
    proposal = REPO_ROOT / "examples" / "control" / "proposal.json"
    if proposal.is_file():
        tune = call_tool("tune", {"action": "check-update", "file": str(proposal)})
        if tune.get("isError"):
            failures.append("tune check-update failed on the bundled proposal")
    board_ioc = REPO_ROOT / "examples" / "board" / "demo.ioc"
    if board_ioc.is_file():
        board = call_tool("board", {"action": "from-ioc", "file": str(board_ioc)})
        if board.get("isError"):
            failures.append("board from-ioc failed on the bundled .ioc example")
    memory = call_tool("memory", {"action": "detect", "project": str(REPO_ROOT / "examples" / "board")})
    if memory.get("isError"):
        failures.append("memory detect failed on the bundled board example")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print("MCP self-test OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass
    parser = argparse.ArgumentParser(prog="mcp_server.py", description="MCP stdio server for the embedded-dev tool layer.")
    parser.add_argument("--self-test", action="store_true", help="offline smoke test; no stdin loop")
    parser.add_argument("--version", action="version", version=f"embedded-dev MCP server {read_version()}")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"parse error: {exc}"}}), flush=True)
            continue
        response = handle_request(request)
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
