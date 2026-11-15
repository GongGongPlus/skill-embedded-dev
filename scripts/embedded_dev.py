#!/usr/bin/env python3
"""embedded-dev tool layer: build / flash / monitor / regs / fault decode.

Design rules (see SKILL.md "Tool layer and evidence contract"):
- Never guess a probe, toolchain, target or address. If a choice is ambiguous,
  list the candidates and exit CONFIG_ERROR (6) so the operator can decide.
- Every side-effecting command has --dry-run. Dry-run prints the exact command
  line(s) and performs no side effect.
- Success is an exit code, not a line in a log. Exit 0 for build/flash means
  only that build and flash succeeded; it does not prove the target behaves
  correctly. Prove behaviour with `monitor --expect <token>` on the real board.

Exit-code contract:
  0 PASS / success
  1 BUILD_FAILED
  2 FLASH_FAILED
  3 TARGET_FAULT (decoded target fault, or crash signature found)
  4 DEVICE / TIMEOUT (probe, port or expected token unavailable)
  5 STALLED (same failure signature repeated; stop and ask the operator)
  6 CONFIG_ERROR (missing tool, ambiguous backend, missing required option)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_BUILD = 1
EXIT_FLASH = 2
EXIT_FAULT = 3
EXIT_DEVICE = 4
EXIT_STALLED = 5
EXIT_CONFIG = 6

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
VERSION_FALLBACK = "0.4.0"


def read_version() -> str:
    """Read VERSION from the repository root, falling back to the constant."""
    version_file = REPO_ROOT / "VERSION"
    try:
        text = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return VERSION_FALLBACK
    return text or VERSION_FALLBACK


# ---------------------------------------------------------------------------
# Fault decoding (Cortex-M SCB; addresses from the ARMv7-M architecture manual)
# ---------------------------------------------------------------------------

CFSR_BITS = [
    ("MMFSR", 0, "IACCVIOL", "Instruction access violation (MPU or XN region)."),
    ("MMFSR", 1, "DACCVIOL", "Data access violation (MPU or XN region)."),
    ("MMFSR", 3, "MUNSTKERR", "MemManage fault during exception return (unstacking)."),
    ("MMFSR", 4, "MSTKERR", "MemManage fault during exception entry (stacking)."),
    ("MMFSR", 5, "MLSPERR", "MemManage fault during lazy FPU state preservation."),
    ("MMFSR", 7, "MMARVALID", "MMFAR holds the faulting data address."),
    ("BFSR", 8, "IBUSERR", "Instruction bus error (bad fetch address)."),
    ("BFSR", 9, "PRECISERR", "Precise data bus error (BFAR valid when BFARVALID set)."),
    ("BFSR", 10, "IMPRECISERR", "Imprecise data bus error (write buffer; may be a late report)."),
    ("BFSR", 11, "UNSTKERR", "BusFault during exception return (unstacking)."),
    ("BFSR", 12, "STKERR", "BusFault during exception entry (stacking)."),
    ("BFSR", 13, "LSPERR", "BusFault during lazy FPU state preservation."),
    ("BFSR", 15, "BFARVALID", "BFAR holds the faulting data address."),
    ("UFSR", 16, "UNDEFINSTR", "Undefined instruction."),
    ("UFSR", 17, "INVSTATE", "Invalid state: EPSR.T bit is clear on an ARMv7-M target."),
    ("UFSR", 18, "INVPC", "Invalid PC load on exception return."),
    ("UFSR", 19, "NOCP", "Attempt to use an FPU or coprocessor that is not enabled."),
    ("UFSR", 24, "UNALIGNED", "Unaligned access when UNALIGN_TRP is enabled."),
    ("UFSR", 25, "DIVBYZERO", "Integer divide by zero when DIV_0_TRP is enabled."),
]

HFSR_BITS = [
    (1, "VECTTBL", "Fault during vector table read (bad VTOR / corrupt vector)."),
    (30, "FORCED", "Escalated from a configurable fault; inspect CFSR first."),
    (31, "DEBUGEVT", "Debug event fault."),
]

FAULT_HINTS = {
    "MSTKERR": "Stack overflow or corrupt stack pointer at exception entry: check PSP/MSP and stack watermark before blaming the handler.",
    "STKERR": "Stack overflow or corrupt stack pointer at exception entry (BusFault on stacking): inspect SP and stack size.",
    "UNSTKERR": "Corrupt stack frame at exception return: a task or ISR overwrote the stack.",
    "MUNSTKERR": "Corrupt stack frame at exception return: check the task stack and PSP.",
    "PRECISERR": "Read/write to an invalid address: take BFAR (when BFARVALID) and map it against the memory map and DMA buffers.",
    "IMPRECISERR": "Late write-buffer report: the real store may be far from the handler. Check DMA descriptors, buffer overruns and MPU/XN.",
    "IBUSERR": "Instruction fetch from an invalid address: corrupted function pointer or a jump through a NULL/stale pointer.",
    "UNDEFINSTR": "Undefined instruction: wrong library/binary, corrupted flash, or a jump into data.",
    "INVSTATE": "Thumb bit not set: calling a data pointer as a function or a bad vector entry.",
    "NOCP": "FPU/CP access without enabling it (CPACR/FPU init) or soft-float/hard-float ABI mismatch.",
    "UNALIGNED": "Unaligned access with UNALIGN_TRP enabled: fix the buffer/struct layout or use memcpy.",
    "DIVBYZERO": "Integer division by zero with DIV_0_TRP enabled: guard the divisor.",
}


def parse_hex(value: str | int | None) -> int | None:
    """Parse 0x-prefixed or bare hexadecimal, tolerating underscores and whitespace."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().replace("_", "")
    if not text:
        return None
    if text.lower().startswith("0x"):
        text = text[2:]
    return int(text, 16)


def decode_fault(
    cfsr: int = 0,
    hfsr: int = 0,
    mmfar: int | None = None,
    bfar: int | None = None,
    pc: int | None = None,
    lr: int | None = None,
    psp: int | None = None,
    msp: int | None = None,
) -> dict:
    """Decode Cortex-M fault registers into bits, hints and next steps."""
    hits = []
    for register, bit, name, meaning in CFSR_BITS:
        if cfsr & (1 << bit):
            hits.append(
                {
                    "register": register,
                    "bit": bit,
                    "name": name,
                    "meaning": meaning,
                    "hint": FAULT_HINTS.get(name, ""),
                }
            )
    hfsr_hits = []
    for bit, name, meaning in HFSR_BITS:
        if hfsr & (1 << bit):
            hfsr_hits.append({"register": "HFSR", "bit": bit, "name": name, "meaning": meaning})

    result = {
        "cfsr": cfsr,
        "hfsr": hfsr,
        "cfsr_bits": hits,
        "hfsr_bits": hfsr_hits,
        "mmfar": mmfar,
        "bfar": bfar,
        "pc": pc,
        "lr": lr,
        "psp": psp,
        "msp": msp,
        "next_steps": [],
    }

    def add_step(text: str) -> None:
        if text not in result["next_steps"]:
            result["next_steps"].append(text)

    if not cfsr and not hfsr:
        add_step("No fault bits supplied; capture CFSR/HFSR before resetting the target.")
    if any(hit["name"] == "MMARVALID" for hit in hits) and mmfar is not None:
        add_step(f"Map MMFAR {mmfar:#010x} against the memory map, MPU regions and stack bound.")
    if any(hit["name"] == "BFARVALID" for hit in hits) and bfar is not None:
        add_step(f"Map BFAR {bfar:#010x} against the memory map and DMA/descriptor buffers.")
    if any(hit["name"] in {"MSTKERR", "STKERR", "UNSTKERR", "MUNSTKERR"} for hit in hits):
        add_step("Check MSP/PSP and the FreeRTOS stack watermark; a bad stack frame is the common cause.")
    if any(hit["name"] == "IMPRECISERR" for hit in hits):
        add_step("For imprecise BusFaults, disable the write buffer or bisect DMA/store paths before trusting the PC.")
    if any(hit["name"] in {"UNDEFINSTR", "INVSTATE", "INVPC"} for hit in hits):
        add_step("Inspect the corrupted PC/LR pair and any indirect call or vector table entry.")
    if any(hit["name"] == "NOCP" for hit in hits):
        add_step("Enable CPACR for the FPU before first FP use and check the compile-time float ABI.")
    if hfsr & (1 << 30):
        add_step("HFSR.FORCED is set: fix the configurable fault reported in CFSR first.")
    if pc is not None:
        add_step(f"Resolve the crash site offline: arm-none-eabi-addr2line -f -C -e firmware.elf {pc:#010x}")
    if not result["next_steps"]:
        add_step("Capture a register dump and the stack before resetting; do not clear evidence.")
    return result


def format_fault(result: dict) -> str:
    lines = [
        f"CFSR=0x{result['cfsr']:08x} HFSR=0x{result['hfsr']:08x}",
    ]
    if result["bfar"] is not None:
        lines.append(f"BFAR=0x{result['bfar']:08x}")
    if result["mmfar"] is not None:
        lines.append(f"MMFAR=0x{result['mmfar']:08x}")
    for key in ("pc", "lr", "psp", "msp"):
        if result[key] is not None:
            lines.append(f"{key.upper()}=0x{result[key]:08x}")
    if not result["cfsr_bits"] and not result["hfsr_bits"]:
        lines.append("no fault bits set")
    for hit in result["cfsr_bits"]:
        lines.append(f"{hit['register']}.{hit['name']} (bit {hit['bit']}): {hit['meaning']}")
    for hit in result["hfsr_bits"]:
        lines.append(f"HFSR.{hit['name']} (bit {hit['bit']}): {hit['meaning']}")
    lines.append("")
    lines.append("Hints (software interpretation; confirm with the reference manual):")
    for hit in result["cfsr_bits"]:
        if hit["hint"]:
            lines.append(f"- {hit['hint']}")
    for step in result["next_steps"]:
        lines.append(f"* {step}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool discovery and backend selection
# ---------------------------------------------------------------------------

BUILD_BACKENDS = {
    "cmake": ["cmake"],
    "platformio": ["platformio", "pio"],
    "idf": ["idf.py"],
}

FLASH_BACKENDS = {
    "openocd": ["openocd"],
    "pyocd": ["pyocd"],
    "jlink": ["JLinkExe", "JLink"],
    "stm32cubeprogrammer": ["STM32_Programmer_CLI"],
    "esptool": ["esptool.py", "esptool"],
    "nrfjprog": ["nrfjprog"],
    "probe-rs": ["probe-rs", "probe_rs"],
    "platformio": ["platformio", "pio"],
}

ENV_TOOLS = {
    "cmake": ["cmake"],
    "platformio": ["platformio", "pio"],
    "idf.py": ["idf.py"],
    "openocd": ["openocd"],
    "pyocd": ["pyocd"],
    "jlink": ["JLinkExe", "JLink"],
    "stm32cubeprogrammer": ["STM32_Programmer_CLI"],
    "esptool": ["esptool.py", "esptool"],
    "nrfjprog": ["nrfjprog"],
    "probe-rs": ["probe-rs", "probe_rs"],
    "arm-none-eabi-gcc": ["arm-none-eabi-gcc"],
    "arm-none-eabi-gdb": ["arm-none-eabi-gdb", "gdb-multiarch"],
    "addr2line": ["arm-none-eabi-addr2line", "addr2line"],
}


def find_tool(names: list[str]) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def detect_tools() -> dict[str, str | None]:
    return {name: find_tool(names) for name, names in ENV_TOOLS.items()}


def available_backends(backend_table: dict[str, list[str]]) -> dict[str, str | None]:
    return {backend: find_tool(names) for backend, names in backend_table.items()}


def select_backend(requested: str, table: dict[str, list[str]], allow_auto: bool = True) -> tuple[str | None, list[str]]:
    """Return (backend, candidates_list) or (None, candidates).

    Never guesses when more than one backend is available: the caller must
    choose explicitly, matching the skill's no-guessing rule.
    """
    available = [name for name, names in table.items() if find_tool(names)]
    if requested != "auto":
        if requested not in table:
            return None, available
        if requested not in available:
            return None, available
        return requested, available
    if not allow_auto:
        return None, available
    if len(available) == 1:
        return available[0], available
    return None, available


# ---------------------------------------------------------------------------
# Command construction (pure functions, covered by tests)
# ---------------------------------------------------------------------------

def build_commands(
    backend: str,
    project: str,
    build_dir: str = "build",
    target: str | None = None,
    platformio_env: str | None = None,
) -> list[list[str]]:
    if backend == "cmake":
        configure = ["cmake", "-S", project, "-B", build_dir, "-DCMAKE_BUILD_TYPE=Debug"]
        compile_cmd = ["cmake", "--build", build_dir]
        if target:
            compile_cmd.extend(["--target", target])
        compile_cmd.extend(["--parallel"])
        return [configure, compile_cmd]
    if backend == "platformio":
        cmd = ["platformio", "run", "-d", project]
        if platformio_env:
            cmd.extend(["-e", platformio_env])
        return [cmd]
    if backend == "idf":
        return [["idf.py", "-C", project, "build"]]
    raise ValueError(f"unsupported build backend: {backend}")


def flash_commands(
    backend: str,
    image: str,
    *,
    chip: str | None = None,
    target: str | None = None,
    port: str | None = None,
    baud: int = 115200,
    probe: str | None = None,
    openocd_interface: str | None = None,
    openocd_target: str | None = None,
    jlink_device: str | None = None,
    snr: str | None = None,
    project: str | None = None,
) -> tuple[list[list[str]], dict[str, str]]:
    """Return (commands, extra_files). extra_files maps relative name -> content."""
    image_posix = Path(image).as_posix()
    extras: dict[str, str] = {}
    if backend == "openocd":
        if not openocd_interface or not openocd_target:
            raise ValueError("openocd needs --openocd-interface and --openocd-target (no guessing)")
        cmd = [
            "openocd",
            "-f",
            openocd_interface,
            "-f",
            openocd_target,
            "-c",
            f"program {image_posix} verify reset exit",
        ]
        return [cmd], extras
    if backend == "pyocd":
        if not target:
            raise ValueError("pyocd needs --target (MCU name, e.g. stm32f103c8)")
        return [["pyocd", "flash", "--target", target, image_posix]], extras
    if backend == "jlink":
        if not jlink_device:
            raise ValueError("jlink needs --jlink-device (J-Link device name)")
        script = "\n".join(
            [
                "si SWD",
                "speed 4000",
                "connect",
                f"loadfile {image_posix}",
                "r",
                "g",
                "qc",
                "",
            ]
        )
        cmd = [
            "JLinkExe",
            "-device",
            jlink_device,
            "-if",
            "SWD",
            "-speed",
            "4000",
            "-autoconnect",
            "1",
            "-CommanderScript",
            "embedded_dev_flash.jlink",
        ]
        extras["embedded_dev_flash.jlink"] = script
        return [cmd], extras
    if backend == "stm32cubeprogrammer":
        return [["STM32_Programmer_CLI", "-c", "port=SWD", "-w", image_posix, "-v", "-rst"]], extras
    if backend == "esptool":
        if not chip or not port:
            raise ValueError("esptool needs --chip and --port (no guessing)")
        return [
            [
                "esptool.py",
                "--chip",
                chip,
                "--port",
                port,
                "--baud",
                str(baud),
                "write_flash",
                "0x0",
                image_posix,
            ]
        ], extras
    if backend == "nrfjprog":
        cmd = ["nrfjprog", "--program", image_posix, "--chiperase", "--verify", "--reset"]
        if snr:
            cmd[1:1] = ["--snr", snr]
        return [cmd], extras
    if backend == "probe-rs":
        if not chip:
            raise ValueError("probe-rs needs --chip (target name)")
        cmd = ["probe-rs", "download", image_posix, "--chip", chip]
        if probe:
            cmd.extend(["--probe", probe])
        return [cmd], extras
    if backend == "platformio":
        if not project:
            raise ValueError("platformio upload needs --project")
        cmd = ["platformio", "run", "-d", project, "-t", "upload"]
        if port:
            cmd.extend(["--upload-port", port])
        return [cmd], extras
    raise ValueError(f"unsupported flash backend: {backend}")


def commands_to_text(commands: list[list[str]]) -> str:
    return "\n".join(" ".join(shlex.quote(part) for part in cmd) for cmd in commands)


def run_commands(commands: list[list[str]], dry_run: bool = False, timeout: float | None = None) -> list[dict]:
    """Run commands sequentially; stop at the first non-zero exit code."""
    results: list[dict] = []
    for cmd in commands:
        if dry_run:
            results.append({"command": cmd, "exit_code": None, "dry_run": True})
            continue
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            results.append(
                {
                    "command": cmd,
                    "exit_code": proc.returncode,
                    "stdout": proc.stdout[-4000:],
                    "stderr": proc.stderr[-4000:],
                }
            )
            if proc.returncode != 0:
                break
        except FileNotFoundError as exc:
            results.append({"command": cmd, "exit_code": EXIT_CONFIG, "error": str(exc)})
            break
        except subprocess.TimeoutExpired:
            results.append({"command": cmd, "exit_code": EXIT_DEVICE, "error": "timeout"})
            break
    return results


# ---------------------------------------------------------------------------
# Register text parsing and serial monitor helpers
# ---------------------------------------------------------------------------

REGISTER_PATTERNS = {
    "pc": re.compile(r"\bpc\b[^0-9a-fA-F]*(0x[0-9a-fA-F]+|\b[0-9a-fA-F]{8}\b)"),
    "lr": re.compile(r"\blr\b[^0-9a-fA-F]*(0x[0-9a-fA-F]+|\b[0-9a-fA-F]{8}\b)"),
    "psp": re.compile(r"\bpsp\b[^0-9a-fA-F]*(0x[0-9a-fA-F]+|\b[0-9a-fA-F]{8}\b)"),
    "msp": re.compile(r"\bmsp\b[^0-9a-fA-F]*(0x[0-9a-fA-F]+|\b[0-9a-fA-F]{8}\b)"),
    "cfsr": re.compile(r"cfsr[^0-9a-fA-F]*(0x[0-9a-fA-F]+|\b[0-9a-fA-F]{8}\b)", re.IGNORECASE),
    "hfsr": re.compile(r"hfsr[^0-9a-fA-F]*(0x[0-9a-fA-F]+|\b[0-9a-fA-F]{8}\b)", re.IGNORECASE),
    "bfar": re.compile(r"bfar[^0-9a-fA-F]*(0x[0-9a-fA-F]+|\b[0-9a-fA-F]{8}\b)", re.IGNORECASE),
    "mmfar": re.compile(r"mmfar[^0-9a-fA-F]*(0x[0-9a-fA-F]+|\b[0-9a-fA-F]{8}\b)", re.IGNORECASE),
}


def parse_registers(text: str) -> dict[str, int]:
    found: dict[str, int] = {}
    # OpenOCD prints units such as "pc (/32): 0x..."; drop them so the value follows the name.
    normalized = re.sub(r"\(\s*/\s*\d+\s*\)", " ", text)
    for name, pattern in REGISTER_PATTERNS.items():
        match = pattern.search(normalized)
        if match:
            found[name] = int(match.group(1), 16)
    return found


def scan_token(data: bytes, token: str | None) -> bool:
    if token is None:
        return True
    return token.encode("utf-8", errors="replace") in data


def serial_ports() -> list[dict]:
    try:
        from serial.tools import list_ports  # type: ignore
    except Exception:
        return []
    ports = []
    for info in list_ports.comports():
        ports.append({"device": info.device, "description": info.description, "hwid": info.hwid})
    return ports


# ---------------------------------------------------------------------------
# Completion report contract
# ---------------------------------------------------------------------------

REPORT_REQUIRED = ["schema_version", "summary", "changes", "evidence", "commands", "risks", "next_step"]
REPORT_EVIDENCE_LEVELS = {
    "已确认",
    "工程推论",
    "估算",
    "待验证",
    "数据不足",
    "confirmed",
    "inferred",
    "estimate",
    "pending",
    "insufficient",
}


def validate_report(data: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["report must be a JSON object"]
    for key in REPORT_REQUIRED:
        if key not in data:
            errors.append(f"missing required field: {key}")
    if not isinstance(data.get("summary"), str) or not data.get("summary", "").strip():
        errors.append("summary must be a non-empty string")
    if not isinstance(data.get("changes"), list) or not data.get("changes"):
        errors.append("changes must be a non-empty list")
    if not isinstance(data.get("commands"), list) or not data.get("commands"):
        errors.append("commands must be a non-empty list")
    if not isinstance(data.get("risks"), list):
        errors.append("risks must be a list (may be empty)")
    if not isinstance(data.get("next_step"), str) or not data.get("next_step", "").strip():
        errors.append("next_step must be a non-empty string")
    evidence = data.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        errors.append("evidence must be a non-empty list")
    else:
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                errors.append(f"evidence[{index}] must be an object")
                continue
            if not isinstance(item.get("claim"), str) or not item.get("claim", "").strip():
                errors.append(f"evidence[{index}].claim must be a non-empty string")
            level = item.get("level")
            if level not in REPORT_EVIDENCE_LEVELS:
                errors.append(f"evidence[{index}].level must be one of the documented evidence levels")
    commands = data.get("commands")
    if isinstance(commands, list):
        for index, item in enumerate(commands):
            if not isinstance(item, dict):
                errors.append(f"commands[{index}] must be an object")
            elif not isinstance(item.get("command"), str) or not item.get("command", "").strip():
                errors.append(f"commands[{index}].command must be a non-empty string")
    return errors


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def emit(data: object, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_env(args: argparse.Namespace) -> int:
    pyserial = False
    pyserial_version = None
    try:
        import serial  # type: ignore

        pyserial = True
        pyserial_version = getattr(serial, "__version__", "unknown")
    except Exception:
        pass
    tools = detect_tools()
    data = {
        "version": read_version(),
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "platform": sys.platform,
        "pyserial": {"available": pyserial, "version": pyserial_version},
        "tools": tools,
        "serial_ports": serial_ports(),
        "build_backends": available_backends(BUILD_BACKENDS),
        "flash_backends": available_backends(FLASH_BACKENDS),
    }
    if args.json:
        emit(data, True)
    else:
        print(f"embedded-dev tool layer {data['version']} (Python {data['python']})")
        for name, path in tools.items():
            if path:
                print(f"  [ok] {name}: {path}")
        missing = [name for name, path in tools.items() if not path]
        if missing:
            print("  [missing] " + ", ".join(missing))
        print("  build backends: " + (", ".join(sorted(k for k, v in data["build_backends"].items() if v)) or "none"))
        print("  flash backends: " + (", ".join(sorted(k for k, v in data["flash_backends"].items() if v)) or "none"))
        print("  serial ports: " + (", ".join(p["device"] for p in data["serial_ports"]) or "none"))
    if args.strict:
        problems = []
        if not any(data["build_backends"].values()):
            problems.append("no build backend found")
        if not any(data["flash_backends"].values()):
            problems.append("no flash backend found")
        if not pyserial:
            problems.append("pyserial is not installed (monitor unavailable)")
        if problems:
            print("strict check failed: " + "; ".join(problems), file=sys.stderr)
            return EXIT_CONFIG
    return EXIT_OK


def cmd_build(args: argparse.Namespace) -> int:
    backend, candidates = select_backend(args.backend, BUILD_BACKENDS)
    if backend is None:
        if args.backend != "auto":
            print(f"requested build backend '{args.backend}' is not available; detected={candidates}", file=sys.stderr)
        else:
            message = "no usable build backend" if not candidates else "ambiguous build backend; choose one explicitly"
            print(f"{message}: available={candidates}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        commands = build_commands(backend, args.project, args.build_dir, args.target, args.platformio_env)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG
    if args.dry_run:
        print(commands_to_text(commands))
        return EXIT_OK
    results = run_commands(commands, timeout=args.timeout)
    if args.json:
        emit({"backend": backend, "results": results}, True)
    else:
        for result in results:
            print(f"$ {' '.join(result['command'])} -> exit {result['exit_code']}")
            if result.get("stdout"):
                print(result["stdout"].rstrip())
            if result.get("stderr"):
                print(result["stderr"].rstrip(), file=sys.stderr)
    return EXIT_OK if results and all(r.get("exit_code") == 0 for r in results) else EXIT_BUILD


def cmd_flash(args: argparse.Namespace) -> int:
    backend, candidates = select_backend(args.backend, FLASH_BACKENDS)
    if backend is None:
        if args.backend != "auto":
            print(f"requested flash backend '{args.backend}' is not available; detected={candidates}", file=sys.stderr)
        else:
            message = "no usable flash backend" if not candidates else "ambiguous flash backend; choose one explicitly"
            print(f"{message}: available={candidates}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        commands, extras = flash_commands(
            backend,
            args.image,
            chip=args.chip,
            target=args.target,
            port=args.port,
            baud=args.baud,
            probe=args.probe,
            openocd_interface=args.openocd_interface,
            openocd_target=args.openocd_target,
            jlink_device=args.jlink_device,
            snr=args.snr,
            project=args.project,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG
    if args.dry_run:
        print(commands_to_text(commands))
        for name, content in extras.items():
            print(f"# file {name}:\n{content}")
        return EXIT_OK
    for name, content in extras.items():
        Path(name).write_text(content, encoding="utf-8")
    results = run_commands(commands, timeout=args.timeout)
    if args.json:
        emit({"backend": backend, "results": results}, True)
    else:
        for result in results:
            print(f"$ {' '.join(result['command'])} -> exit {result['exit_code']}")
            for stream in ("stdout", "stderr"):
                if result.get(stream):
                    print(result[stream].rstrip())
    return EXIT_OK if results and all(r.get("exit_code") == 0 for r in results) else EXIT_FLASH


def cmd_monitor(args: argparse.Namespace) -> int:
    if args.mock_file:
        data = Path(args.mock_file).read_bytes()
        found = scan_token(data, args.expect)
        result = {
            "mock_file": args.mock_file,
            "bytes": len(data),
            "expect": args.expect,
            "token_found": found,
            "text": data.decode("utf-8", errors="replace")[-4000:],
        }
        if args.json:
            emit(result, True)
        else:
            print(f"read {len(data)} bytes from mock file; token {args.expect!r} found={found}")
        return EXIT_OK if found else EXIT_DEVICE
    try:
        import serial  # type: ignore
    except Exception:
        print("pyserial is required for monitor; install it with: pip install pyserial", file=sys.stderr)
        return EXIT_CONFIG
    if args.dry_run:
        print(f"would open {args.port} at {args.baud} baud and wait {args.timeout}s for {args.expect!r}")
        return EXIT_OK
    try:
        port = serial.Serial(args.port, args.baud, timeout=0.2)
    except Exception as exc:
        print(f"cannot open {args.port}: {exc}", file=sys.stderr)
        return EXIT_DEVICE
    deadline = __import__("time").monotonic() + args.timeout
    chunks = []
    try:
        if args.send:
            port.write(args.send.encode("utf-8") + b"\r\n")
        while __import__("time").monotonic() < deadline:
            chunk = port.read(256)
            if chunk:
                chunks.append(chunk)
                if scan_token(b"".join(chunks), args.expect):
                    break
    finally:
        port.close()
    data = b"".join(chunks)
    found = scan_token(data, args.expect)
    result = {"port": args.port, "bytes": len(data), "expect": args.expect, "token_found": found, "text": data.decode("utf-8", errors="replace")[-4000:]}
    if args.json:
        emit(result, True)
    else:
        print(result["text"])
        print(f"token {args.expect!r} found={found}")
    return EXIT_OK if found else EXIT_DEVICE


def cmd_regs(args: argparse.Namespace) -> int:
    if args.values:
        values = {}
        try:
            for pair in args.values.split(","):
                name, value = pair.split("=", 1)
                values[name.strip().lower()] = parse_hex(value)
        except ValueError:
            print("--values expects comma-separated name=0xHEX pairs", file=sys.stderr)
            return EXIT_CONFIG
    elif args.from_text:
        values = parse_registers(Path(args.from_text).read_text(encoding="utf-8", errors="replace"))
    elif args.dry_run:
        print("regs dry-run: provide one of --backend (with probe), --values or --from-text")
        print("openocd template: openocd -f <interface> -f <target> -c \"init; halt; reg pc; reg lr; reg sp; reg psp; reg msp; mdw 0xE000ED28 3; mdw 0xE000ED34 1; shutdown\"")
        return EXIT_OK
    else:
        print("regs needs --values, --from-text or --dry-run in this offline build", file=sys.stderr)
        return EXIT_CONFIG
    result = decode_fault(
        cfsr=values.get("cfsr", 0),
        hfsr=values.get("hfsr", 0),
        mmfar=values.get("mmfar"),
        bfar=values.get("bfar"),
        pc=values.get("pc"),
        lr=values.get("lr"),
        psp=values.get("psp"),
        msp=values.get("msp"),
    )
    if args.json:
        emit(result, True)
    else:
        print(format_fault(result))
    return EXIT_OK


def cmd_fault(args: argparse.Namespace) -> int:
    result = decode_fault(
        cfsr=parse_hex(args.cfsr) or 0,
        hfsr=parse_hex(args.hfsr) or 0,
        mmfar=parse_hex(args.mmfar),
        bfar=parse_hex(args.bfar),
        pc=parse_hex(args.pc),
        lr=parse_hex(args.lr),
        psp=parse_hex(args.psp),
        msp=parse_hex(args.msp),
    )
    if args.json:
        emit(result, True)
    else:
        print(format_fault(result))
    return EXIT_FAULT if (result["cfsr_bits"] or result["hfsr_bits"]) else EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    if args.emit_example:
        example = {
            "schema_version": 1,
            "summary": "What was changed and on which board/commit.",
            "changes": ["files, versions and generated artefacts"],
            "evidence": [
                {"claim": "build succeeds", "level": "已确认", "how": "cmake --build exit 0"},
                {"claim": "behaviour is correct on target", "level": "待验证", "how": "needs board token"},
            ],
            "commands": [{"command": "python scripts/embedded_dev.py build --project .", "result": "exit 0"}],
            "risks": ["board not connected: flash/monitor not executed"],
            "next_step": "flash and run monitor --expect <token> on the target board",
        }
        emit(example, True)
        return EXIT_OK
    if args.stdin:
        raw = sys.stdin.read()
    else:
        raw = Path(args.file).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"invalid JSON: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    errors = validate_report(data)
    if args.json:
        emit({"valid": not errors, "errors": errors}, True)
    else:
        if errors:
            for error in errors:
                print(f"FAIL: {error}", file=sys.stderr)
        else:
            print("report contract OK")
    return EXIT_OK if not errors else EXIT_CONFIG


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="embedded_dev.py",
        description="embedded-dev tool layer: build, flash, monitor, registers and fault decoding with an exit-code contract.",
    )
    parser.add_argument("--version", action="version", version=f"embedded-dev tool layer {read_version()}")
    sub = parser.add_subparsers(dest="command", required=True)

    env = sub.add_parser("env", help="detect toolchains, probes, serial ports and bundles")
    env.add_argument("--json", action="store_true")
    env.add_argument("--strict", action="store_true", help="exit 6 unless a build backend, flash backend and pyserial exist")
    env.set_defaults(func=cmd_env)

    build = sub.add_parser("build", help="configure and build a firmware project")
    build.add_argument("--project", required=True)
    build.add_argument("--backend", default="auto", choices=["auto", *BUILD_BACKENDS])
    build.add_argument("--build-dir", default="build")
    build.add_argument("--target")
    build.add_argument("--platformio-env", dest="platformio_env")
    build.add_argument("--dry-run", action="store_true")
    build.add_argument("--json", action="store_true")
    build.add_argument("--timeout", type=float, default=None)
    build.set_defaults(func=cmd_build)

    flash = sub.add_parser("flash", help="flash a firmware image with an explicit backend")
    flash.add_argument("--image", required=True)
    flash.add_argument("--backend", default="auto", choices=["auto", *FLASH_BACKENDS])
    flash.add_argument("--chip", help="esptool/probe-rs target chip")
    flash.add_argument("--target", help="pyOCD target name")
    flash.add_argument("--port")
    flash.add_argument("--baud", type=int, default=115200)
    flash.add_argument("--probe")
    flash.add_argument("--openocd-interface")
    flash.add_argument("--openocd-target", dest="openocd_target")
    flash.add_argument("--jlink-device", dest="jlink_device")
    flash.add_argument("--snr")
    flash.add_argument("--project")
    flash.add_argument("--dry-run", action="store_true")
    flash.add_argument("--json", action="store_true")
    flash.add_argument("--timeout", type=float, default=None)
    flash.set_defaults(func=cmd_flash)

    monitor = sub.add_parser("monitor", help="read a serial port until the expected token appears")
    monitor.add_argument("--port")
    monitor.add_argument("--baud", type=int, default=115200)
    monitor.add_argument("--expect", help="pass token emitted by the firmware")
    monitor.add_argument("--send", help="line sent once after opening the port")
    monitor.add_argument("--timeout", type=float, default=10.0)
    monitor.add_argument("--mock-file", dest="mock_file", help="offline: scan a captured byte file instead of a real port")
    monitor.add_argument("--dry-run", action="store_true")
    monitor.add_argument("--json", action="store_true")
    monitor.set_defaults(func=cmd_monitor)

    regs = sub.add_parser("regs", help="decode a register dump (offline) or print the read plan")
    regs.add_argument("--values", help="comma-separated name=0xHEX pairs, e.g. pc=0x08000100,cfsr=0x02000000")
    regs.add_argument("--from-text", dest="from_text", help="file containing a captured openocd/gdb register dump")
    regs.add_argument("--dry-run", action="store_true")
    regs.add_argument("--json", action="store_true")
    regs.set_defaults(func=cmd_regs)

    fault = sub.add_parser("fault", help="decode Cortex-M fault registers")
    fault.add_argument("--cfsr", default="0x0")
    fault.add_argument("--hfsr", default="0x0")
    fault.add_argument("--mmfar")
    fault.add_argument("--bfar")
    fault.add_argument("--pc")
    fault.add_argument("--lr")
    fault.add_argument("--psp")
    fault.add_argument("--msp")
    fault.add_argument("--json", action="store_true")
    fault.set_defaults(func=cmd_fault)

    report = sub.add_parser("report", help="validate a completion report against the evidence schema")
    report.add_argument("--file")
    report.add_argument("--stdin", action="store_true")
    report.add_argument("--emit-example", dest="emit_example", action="store_true")
    report.add_argument("--json", action="store_true")
    report.set_defaults(func=cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Keep JSON/Chinese output valid UTF-8 even when stdout is a Windows pipe
    # (the console codepage would otherwise break CI log capture and subprocess tests).
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_DEVICE


if __name__ == "__main__":
    sys.exit(main())
