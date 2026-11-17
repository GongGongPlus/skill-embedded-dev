#!/usr/bin/env python3
"""embedded-dev tool layer: build / flash / monitor / regs / symbolize / stack /
fault / decode / hil / tune / board / memory.

Design rules (see SKILL.md "Tool layer and evidence contract"):
- Never guess a probe, toolchain, target, address, controller, pin or control
  parameter. If a choice is ambiguous, list the candidates and exit
  CONFIG_ERROR (6) or DEVICE (4) so the operator can decide.
- Every side-effecting command has --dry-run. Dry-run prints the exact command
  line(s) and performs no side effect.
- Success is an exit code, not a line in a log. Exit 0 for build/flash means
  only that build and flash succeeded; it does not prove the target behaves
  correctly. Prove behaviour with `monitor --expect <token>` or `hil` on the
  real board.

Exit-code contract:
  0 PASS / success
  1 BUILD_FAILED
  2 FLASH_FAILED
  3 TARGET_FAULT (decoded target fault, or crash signature found)
  4 DEVICE / TIMEOUT (probe, port, expected token or ambiguous evidence)
  5 STALLED (same failure signature repeated; stop and ask the operator)
  6 CONFIG_ERROR (missing tool, ambiguous backend, missing required option)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
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
VERSION_FALLBACK = "0.8.0"


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
    "addr2line": [
        "arm-none-eabi-addr2line",
        "xtensa-esp32s3-elf-addr2line",
        "xtensa-esp32-elf-addr2line",
        "riscv32-esp-elf-addr2line",
        "addr2line",
    ],
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


# ---------------------------------------------------------------------------
# ELF symbol resolution (DWARF gate)
# ---------------------------------------------------------------------------

def resolve_addresses(elf: str | None, addresses: dict[str, int | None]) -> tuple[dict, str | None]:
    """Resolve addresses with addr2line. Missing tool/ELF is a config error, never a guess."""
    if not elf:
        return {}, None
    if not Path(elf).is_file():
        return {}, f"ELF file not found: {elf}"
    tool = find_tool(ENV_TOOLS["addr2line"])
    if tool is None:
        return {}, "addr2line not found (DWARF gate): install arm-none-eabi-binutils (or binutils for host ELFs)"
    keys = [key for key, value in addresses.items() if value is not None]
    if not keys:
        return {}, None
    command = [tool, "-f", "-C", "-e", str(elf), *[f"0x{addresses[key]:08x}" for key in keys]]
    proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    lines = [line.strip() for line in proc.stdout.splitlines()]
    symbols: dict = {}
    for index, key in enumerate(keys):
        function = lines[2 * index] if 2 * index < len(lines) else "??"
        location = lines[2 * index + 1] if 2 * index + 1 < len(lines) else "??:0"
        symbols[key] = {"address": f"0x{addresses[key]:08x}", "function": function, "location": location}
    if proc.returncode != 0 and not symbols:
        return {}, (proc.stderr or "addr2line failed").strip()
    return symbols, None


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
# Hardware-in-the-loop diagnostic report (build -> flash -> monitor -> diagnose)
# ---------------------------------------------------------------------------

DIAGNOSTIC_REQUIRED = [
    "schema_version",
    "timestamp",
    "iteration",
    "project",
    "status",
    "summary",
    "signature",
    "next_actions",
    "ai_repair_prompt",
    "repeated_failure",
    "source_unchanged",
]

DIAGNOSTIC_STATUSES = [
    "TEST_PASSED",
    "BUILD_FAILED",
    "FLASH_FAILED",
    "HARD_FAULT",
    "ASSERTION_FAILED",
    "TEST_FAILED",
    "TIMEOUT",
    "SERIAL_UNAVAILABLE",
    "STALLED",
]

COMPILER_ERROR_RE = re.compile(
    r"(?P<file>[^\s:][^:\n]*?):(?P<line>\d+)(?::\d+)?:\s*(?P<severity>error|fatal error|warning):\s*(?P<message>.+)"
)
FAULT_MARKERS = ("hardfault", "hard fault", "cfsr", "fault handler", "*** fault", "assert failed", "assertion failed")


def parse_compiler_errors(text: str, limit: int = 20) -> list[dict]:
    errors: list[dict] = []
    for match in COMPILER_ERROR_RE.finditer(text or ""):
        errors.append(
            {
                "file": match.group("file"),
                "line": int(match.group("line")),
                "severity": match.group("severity"),
                "message": match.group("message").strip(),
            }
        )
        if len(errors) >= limit:
            break
    return errors


def git_source_state(project: str | Path) -> dict:
    """Report repository/dirty state so a failure can be tied to a source revision."""
    state: dict = {"git": False, "clean": None, "porcelain": [], "head": None}
    if shutil.which("git") is None:
        state["note"] = "git not installed"
        return state
    try:
        inside = subprocess.run(["git", "-C", str(project), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True, timeout=5)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            state["note"] = "project is not a git work tree"
            return state
        state["git"] = True
        head = subprocess.run(["git", "-C", str(project), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5)
        state["head"] = head.stdout.strip() or None
        status = subprocess.run(["git", "-C", str(project), "status", "--porcelain"], capture_output=True, text=True, timeout=10)
        changed = [line for line in status.stdout.splitlines() if line.strip()]
        state["porcelain"] = changed
        state["clean"] = not changed
    except (OSError, subprocess.SubprocessError) as exc:
        state["note"] = f"git probe failed: {exc}"
    return state


def create_restore_point(project: str | Path) -> str | None:
    """Record a stash commit for a dirty tree without touching the work tree (git 2.13+)."""
    if not git_source_state(project)["git"]:
        return None
    try:
        proc = subprocess.run(["git", "-C", str(project), "stash", "create"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() or None


def failure_signature(status: str, fault: dict | None, serial_text: str) -> str:
    names = []
    if fault:
        names = sorted(hit["name"] for hit in fault.get("cfsr_bits", []) + fault.get("hfsr_bits", []))
    tail = " ".join((serial_text or "").split()).lower()[-1500:]
    payload = "|".join([status, ",".join(names), tail])
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:16]


def build_repair_prompt(report: dict) -> str:
    lines = [
        f"Target status: {report['status']}.",
        f"Project: {report['project']}  iteration: {report['iteration']}  signature: {report['signature']}.",
    ]
    fault = report.get("fault_diagnostics")
    if fault:
        bits = [hit["name"] for hit in fault.get("cfsr_bits", []) + fault.get("hfsr_bits", [])]
        lines.append("Fault bits: " + (", ".join(bits) if bits else "none decoded") + ".")
    if report.get("compiler_errors"):
        first = report["compiler_errors"][0]
        lines.append(f"First compiler error: {first['file']}:{first['line']}: {first['message']}")
    if report.get("repeated_failure"):
        lines.append("This is the same failure signature as the previous run: do not repeat the same patch.")
    if report.get("source_unchanged"):
        lines.append("The source tree is unchanged since the last attempt: change the hypothesis or collect new evidence first.")
    lines.append("Next actions:")
    for action in report.get("next_actions", []):
        lines.append(f"- {action}")
    lines.append("Rules: one variable at a time; verify on real hardware with a token; label every claim with an evidence level.")
    return "\n".join(lines)


def write_diagnostic_report(report: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


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
    deadline = time.monotonic() + args.timeout
    chunks = []
    try:
        if args.send:
            port.write(args.send.encode("utf-8") + b"\r\n")
        while time.monotonic() < deadline:
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


STACK_WARNING = "below threshold"
STACK_WORDS_HINT = "StackType_t (usually uint32_t = 4 bytes)"


def parse_tasklist(text: str) -> list[dict]:
    """Parse FreeRTOS vTaskList / uxTaskGetSystemState text output.

    vTaskList columns: name state priority free_stack_words task_number. The
    free-stack column is read as the second-to-last integer so both the plain
    vTaskList dump and a Task-Status table with a trailing index work. Anything
    that does not look like a task row is skipped rather than guessed at.
    """
    tasks: list[dict] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or set(stripped) <= set("-= "):
            continue
        parts = stripped.split()
        if len(parts) < 4:
            continue
        numbers = [part for part in parts[1:] if part.isdigit()]
        if len(numbers) < 2:
            continue
        name = parts[0]
        state = parts[1] if not parts[1].isdigit() else None
        tasks.append(
            {
                "name": name,
                "state": state,
                "free_words": int(numbers[-2]),
                "free_bytes": int(numbers[-2]) * 4,
            }
        )
    return tasks


def cmd_stack(args: argparse.Namespace) -> int:
    if args.tasklist:
        source = Path(args.tasklist)
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"cannot read {source}: {exc}", file=sys.stderr)
            return EXIT_CONFIG
        origin = str(source)
    else:
        text = sys.stdin.read()
        origin = "stdin"
    tasks = parse_tasklist(text)
    if not tasks:
        print(
            "no task rows parsed: expected vTaskList/uxTaskGetSystemState output "
            "(name state priority free_stack_words [index])",
            file=sys.stderr,
        )
        return EXIT_DEVICE
    word_bytes = args.word_bytes
    threshold_words = args.min_free
    for task in tasks:
        task["free_bytes"] = task["free_words"] * word_bytes
        task["status"] = "WARN" if task["free_words"] < threshold_words else "OK"
    worst = min(tasks, key=lambda task: task["free_words"])
    result = {
        "source": origin,
        "word_bytes": word_bytes,
        "min_free_words": threshold_words,
        "min_free_bytes": threshold_words * word_bytes,
        "task_count": len(tasks),
        "below_threshold": [task["name"] for task in tasks if task["status"] == "WARN"],
        "worst": worst,
        "tasks": tasks,
    }
    if args.json:
        emit(result, True)
    else:
        print(f"{'task':<20}{'free(words)':>13}{'free(bytes)':>13}  state")
        for task in tasks:
            print(
                f"{task['name']:<20}{task['free_words']:>13}{task['free_bytes']:>13}  "
                f"{task['status']} ({task['state'] or '?'})"
            )
        print(
            f"\nlowest watermark: {worst['name']} at {worst['free_bytes']} bytes "
            f"(threshold {threshold_words * word_bytes} bytes, unit {STACK_WORDS_HINT})"
        )
        if result["below_threshold"]:
            print(f"{STACK_WARNING}: {', '.join(result['below_threshold'])}")
    return EXIT_FAULT if result["below_threshold"] else EXIT_OK


def resolve_raw_addresses(elf: str | None, addresses: list[int]) -> tuple[dict, str | None]:
    """Resolve bare addresses (any core) with addr2line, for crash dumps without registers."""
    if not elf:
        return {}, "an --elf image is required to resolve addresses"
    if not Path(elf).is_file():
        return {}, f"ELF file not found: {elf}"
    tool = find_tool(ENV_TOOLS["addr2line"])
    if tool is None:
        return {}, "addr2line not found (DWARF gate): install arm-none-eabi-binutils or the vendor toolchain"
    if not addresses:
        return {}, None
    command = [tool, "-f", "-C", "-e", str(elf), *[f"0x{address:08x}" for address in addresses]]
    proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    lines = [line.strip() for line in proc.stdout.splitlines()]
    resolved = []
    for index, address in enumerate(addresses):
        function = lines[2 * index] if 2 * index < len(lines) else "??"
        location = lines[2 * index + 1] if 2 * index + 1 < len(lines) else "??:0"
        resolved.append({"address": f"0x{address:08x}", "function": function, "location": location})
    if proc.returncode != 0 and not resolved:
        return {}, (proc.stderr or "addr2line failed").strip()
    return {"tool": tool, "symbols": resolved}, None


def cmd_symbolize(args: argparse.Namespace) -> int:
    if not args.elf:
        print("symbolize requires --elf <image>: without DWARF there is nothing to resolve", file=sys.stderr)
        return EXIT_CONFIG
    addresses: list[int] = []
    for raw in args.addr or []:
        try:
            value = parse_hex(raw)
        except ValueError:
            print(f"cannot parse address: {raw}", file=sys.stderr)
            return EXIT_CONFIG
        if value is None:
            print(f"cannot parse address: {raw}", file=sys.stderr)
            return EXIT_CONFIG
        addresses.append(value)
    if not addresses:
        print("symbolize needs at least one address, e.g. --addr 0x08001234", file=sys.stderr)
        return EXIT_CONFIG
    resolved, error = resolve_raw_addresses(args.elf, addresses)
    if error:
        print(error, file=sys.stderr)
        return EXIT_CONFIG
    if args.json:
        emit(resolved, True)
    else:
        print(f"tool: {resolved['tool']}")
        print(f"image: {args.elf}")
        print("confirm this ELF is the same build that is on the board, or the mapping is meaningless")
        for symbol in resolved["symbols"]:
            print(f"{symbol['address']}  {symbol['function']}  {symbol['location']}")
    return EXIT_OK


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
    symbols, symbol_error = resolve_addresses(getattr(args, "elf", None), {"pc": values.get("pc"), "lr": values.get("lr")})
    if symbol_error:
        print(symbol_error, file=sys.stderr)
        return EXIT_CONFIG
    if symbols:
        result["symbols"] = symbols
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
    symbols, symbol_error = resolve_addresses(getattr(args, "elf", None), {"pc": result["pc"], "lr": result["lr"]})
    if symbol_error:
        print(symbol_error, file=sys.stderr)
        return EXIT_CONFIG
    if symbols:
        result["symbols"] = symbols
        for key, info in symbols.items():
            result["next_steps"].append(f"Resolved {key.upper()} with DWARF: {info['function']} at {info['location']}.")
    if args.json:
        emit(result, True)
    else:
        print(format_fault(result))
        for key, info in symbols.items():
            print(f"{key.upper()} -> {info['function']} {info['location']}")
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


def _print_decode_summary(result: dict) -> None:
    summary = result.get("summary") or {}
    if result.get("kind") == "uart" or result.get("transport") == "uart":
        print(
            "uart frames={frames} checksum_failures={checksum_failures} frames_with_errors={frames_with_errors} decode_errors={decode_errors}".format(
                frames=summary.get("frames", 0),
                checksum_failures=summary.get("checksum_failures", 0),
                frames_with_errors=summary.get("frames_with_errors", 0),
                decode_errors=summary.get("decode_errors", 0),
            )
        )
        for frame in result.get("frames", [])[:10]:
            body = ", ".join(f"{name}={value}" for name, value in (frame.get("fields") or {}).items())
            print(f"  offset={frame.get('offset')} {frame.get('direction') or frame.get('command') or ''} len={frame.get('length')} checksum_ok={(frame.get('checksum') or {}).get('ok')} {body}")
    else:
        print(
            "can frames={can_frames} isotp_messages={isotp_messages} uds={uds} obd2={obd2} j1939={j1939} parse_errors={parse_errors}".format(
                can_frames=summary.get("can_frames", 0),
                isotp_messages=summary.get("isotp_messages", 0),
                uds=summary.get("uds", 0),
                obd2=summary.get("obd2", 0),
                j1939=summary.get("j1939", 0),
                parse_errors=summary.get("parse_errors", 0),
            )
        )
        for message in result.get("messages", [])[:20]:
            decoded = message.get("decoded") or {}
            print(f"  {message.get('id_hex')} -> {decoded.get('protocol') or 'raw/unknown'}: {decoded}")
    for error in result.get("errors", [])[:10]:
        print(f"  decode error: {error}")


def cmd_decode(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        import protocol_decode as proto  # noqa: PLC0415 - sibling script, stdlib-only import
    except Exception as exc:  # pragma: no cover - only if the file is missing
        print(f"protocol_decode.py is unavailable: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        spec = proto.load_spec(args.spec)
    except (OSError, ValueError) as exc:
        print(f"cannot load protocol spec: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    spec_errors = proto.validate_spec(spec)
    if args.kind == "validate":
        emit({"valid": not spec_errors, "errors": spec_errors}, args.json)
        return EXIT_OK if not spec_errors else EXIT_CONFIG
    if spec_errors:
        emit({"valid": False, "errors": spec_errors}, args.json)
        return EXIT_CONFIG
    try:
        if args.kind == "uart":
            data = proto._read_input(args.hex, args.file, args.format)
            result = proto.decode_uart_stream(spec, data)
            has_errors = bool(result["summary"]["decode_errors"] or result["summary"]["checksum_failures"])
        else:
            if args.log:
                text = Path(args.log).read_text(encoding="utf-8", errors="replace")
            elif args.file:
                text = Path(args.file).read_text(encoding="utf-8", errors="replace")
            elif args.hex:
                text = args.hex
            else:
                raise ValueError("decode can needs --log, --file or --hex candump lines")
            result = proto.decode_can_log(text)
            has_errors = bool(result["errors"])
    except (OSError, ValueError) as exc:
        print(f"decode failed: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if args.json:
        emit(result, True)
    else:
        _print_decode_summary(result)
    return EXIT_DEVICE if (args.strict and has_errors) else EXIT_OK


HIL_STATUS_EXIT = {
    "TEST_PASSED": EXIT_OK,
    "BUILD_FAILED": EXIT_BUILD,
    "FLASH_FAILED": EXIT_FLASH,
    "HARD_FAULT": EXIT_FAULT,
    "ASSERTION_FAILED": EXIT_FAULT,
    "TEST_FAILED": EXIT_DEVICE,
    "TIMEOUT": EXIT_DEVICE,
    "SERIAL_UNAVAILABLE": EXIT_DEVICE,
    "STALLED": EXIT_STALLED,
}

HIL_NEXT_ACTIONS = {
    "BUILD_FAILED": ["Fix the first compiler error in compiler_errors; rerun with --iteration <n+1> after the edit."],
    "FLASH_FAILED": ["Check board power and probe wiring, then confirm the image matches the chip before retrying."],
    "HARD_FAULT": ["Decode CFSR/HFSR with the fault subcommand, resolve PC/LR with --elf, then fix the first root cause."],
    "ASSERTION_FAILED": ["Inspect the assertion message and its source context; verify the invariant instead of weakening the test."],
    "TEST_FAILED": ["Compare the captured bytes with the expected token; check the observation point (UART wiring, baud, flow control)."],
    "TIMEOUT": ["Confirm the firmware runs (heartbeat/LED), then check the token is emitted after the correct step; widen --timeout only as a last resort."],
    "SERIAL_UNAVAILABLE": ["Close other serial monitors, verify port/baud, or pass --mock-file to validate the pipeline without hardware."],
    "STALLED": ["Same signature and unchanged source: change the hypothesis or collect new evidence before editing code again."],
    "TEST_PASSED": ["Attach the token and raw log to the completion report; raise the evidence level to 已确认."],
}


def _hil_capture(args: argparse.Namespace) -> tuple[bytes, str]:
    if args.mock_file:
        return Path(args.mock_file).read_bytes(), "mock_file"
    try:
        import serial  # type: ignore  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("pyserial is required for hil; install it with: pip install pyserial") from exc
    try:
        port = serial.Serial(args.port, args.baud, timeout=0.2)
    except Exception as exc:
        raise RuntimeError(f"cannot open {args.port}: {exc}") from exc
    chunks: list[bytes] = []
    deadline = time.monotonic() + args.timeout
    try:
        while time.monotonic() < deadline:
            chunk = port.read(256)
            if chunk:
                chunks.append(chunk)
                if scan_token(b"".join(chunks), args.expect):
                    break
    finally:
        port.close()
    return b"".join(chunks), "serial"


def cmd_hil(args: argparse.Namespace) -> int:
    project = Path(args.project)
    if not project.is_dir():
        print(f"project not found: {project}", file=sys.stderr)
        return EXIT_CONFIG
    if not args.image:
        print("hil requires --image <elf-or-bin>: the flash step must know what to write", file=sys.stderr)
        return EXIT_CONFIG
    if not args.dry_run and not args.expect:
        print("hil requires --expect <token>: a hardware test without an observable pass token is a guess", file=sys.stderr)
        return EXIT_CONFIG
    build_backend, flash_backend = args.build_backend, args.flash_backend
    if args.dry_run:
        # A plan is allowed without installed tools; a real run is not.
        if build_backend == "auto":
            build_backend, _ = select_backend("auto", BUILD_BACKENDS)
        if flash_backend == "auto":
            flash_backend, _ = select_backend("auto", FLASH_BACKENDS)
        if build_backend is None or flash_backend is None:
            print("hil dry-run needs --build-backend/--flash-backend when no tools are detected", file=sys.stderr)
            return EXIT_CONFIG
    else:
        build_backend, build_candidates = select_backend(args.build_backend, BUILD_BACKENDS)
        if build_backend is None:
            print(f"no unambiguous build backend (requested={args.build_backend}, available={build_candidates}); pass --build-backend", file=sys.stderr)
            return EXIT_CONFIG
        flash_backend, flash_candidates = select_backend(args.flash_backend, FLASH_BACKENDS)
        if flash_backend is None:
            print(f"no unambiguous flash backend (requested={args.flash_backend}, available={flash_candidates}); pass --flash-backend", file=sys.stderr)
            return EXIT_CONFIG
    try:
        build_steps = build_commands(build_backend, str(project), args.build_dir, args.target, args.platformio_env)
        flash_steps, flash_extras = flash_commands(
            flash_backend,
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
            project=str(project),
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG
    if args.dry_run:
        print("hil dry-run plan")
        print("build:")
        print(commands_to_text(build_steps))
        print("flash:")
        print(commands_to_text(flash_steps))
        return EXIT_OK

    iteration = args.iteration
    report_out = args.report_out
    git_state = git_source_state(project)
    restore_point = create_restore_point(project) if args.restore_point else None
    previous: dict | None = None
    if args.previous:
        try:
            previous = json.loads(Path(args.previous).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"cannot read --previous report: {exc}", file=sys.stderr)
            return EXIT_CONFIG

    def finish(status: str, compiler_errors: list[dict] | None = None, serial_text: str = "", fault: dict | None = None) -> int:
        exit_code = HIL_STATUS_EXIT.get(status, EXIT_DEVICE)
        if status in ("HARD_FAULT",) and not fault:
            fault = decode_fault(cfsr=parse_hex(args.cfsr) or 0, hfsr=parse_hex(args.hfsr) or 0)
        signature = failure_signature(status, fault, serial_text)
        repeated = bool(previous and previous.get("signature") == signature)
        source_unchanged = False
        if repeated:
            prev_context = (previous or {}).get("source_context") or {}
            prev_git = prev_context.get("git") or prev_context
            source_unchanged = bool(git_state.get("clean")) and prev_git.get("head") == git_state.get("head")
            if source_unchanged:
                status = "STALLED"
                exit_code = EXIT_STALLED
        report = {
            "schema_version": 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "iteration": iteration,
            "project": str(project.resolve()),
            "status": status,
            "summary": f"hil iteration {iteration}: {status}",
            "signature": signature,
            "compiler_errors": compiler_errors or [],
            "fault_diagnostics": fault,
            "serial_log_tail": serial_text[-2000:],
            "next_actions": HIL_NEXT_ACTIONS.get(status, []),
            "repeated_failure": repeated,
            "source_unchanged": source_unchanged,
            "source_context": {"git": git_state, "restore_point": restore_point},
        }
        report["ai_repair_prompt"] = build_repair_prompt(report)
        write_diagnostic_report(report, report_out)
        if args.json:
            emit(report, True)
        else:
            print(f"{status} (iteration {iteration}, signature {signature})")
            print(f"diagnostic report: {report_out}")
            for action in report["next_actions"]:
                print(f"  next: {action}")
        return exit_code

    build_results = run_commands(build_steps)
    if not build_results or any(result.get("exit_code") != 0 for result in build_results):
        output = "\n".join((result.get("stdout") or "") + "\n" + (result.get("stderr") or "") for result in build_results)
        return finish("BUILD_FAILED", compiler_errors=parse_compiler_errors(output), serial_text="")

    for name, content in flash_extras.items():
        Path(name).write_text(content, encoding="utf-8")
    flash_results = run_commands(flash_steps)
    if not flash_results or any(result.get("exit_code") != 0 for result in flash_results):
        output = "\n".join((result.get("stdout") or "") + "\n" + (result.get("stderr") or "") for result in flash_results)
        return finish("FLASH_FAILED", compiler_errors=parse_compiler_errors(output), serial_text="")

    fault = decode_fault(cfsr=parse_hex(args.cfsr) or 0, hfsr=parse_hex(args.hfsr) or 0, bfar=parse_hex(args.bfar), pc=parse_hex(args.pc), lr=parse_hex(args.lr))
    try:
        data, mode = _hil_capture(args)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return finish("SERIAL_UNAVAILABLE", fault=fault if fault["cfsr_bits"] or fault["hfsr_bits"] else None)
    if not data:
        return finish("SERIAL_UNAVAILABLE" if mode == "serial" else "TIMEOUT", fault=fault if fault["cfsr_bits"] or fault["hfsr_bits"] else None)
    serial_text = data.decode("utf-8", errors="replace")
    lowered = serial_text.lower()
    decoded_fault = fault if (fault["cfsr_bits"] or fault["hfsr_bits"]) else None
    if decoded_fault:
        return finish("HARD_FAULT", serial_text=serial_text, fault=decoded_fault)
    if any(marker in lowered for marker in ("assert failed", "assertion failed", "assert_failed")):
        return finish("ASSERTION_FAILED", serial_text=serial_text)
    if any(marker in lowered for marker in FAULT_MARKERS):
        return finish("HARD_FAULT", serial_text=serial_text)
    if scan_token(data, args.expect):
        return finish("TEST_PASSED", serial_text=serial_text)
    return finish("TEST_FAILED", serial_text=serial_text)


def _run_helper(script_name: str, argv: list[str]) -> int:
    """Delegate to a sibling tool module, keeping one implementation of each surface."""
    import importlib

    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    try:
        module = importlib.import_module(script_name)
    except ImportError as exc:
        print(f"error: helper module {script_name}.py is missing: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        return int(module.main(list(argv)))
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else EXIT_CONFIG


def cmd_tune(args: argparse.Namespace) -> int:
    if not args.helper_args:
        print(
            "usage: embedded_dev.py tune <analyze|check-update|simulate|record> [options]\n"
            "run 'embedded_dev.py tune analyze --help' for details",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    return _run_helper("experiment", args.helper_args)


def cmd_board(args: argparse.Namespace) -> int:
    if not args.helper_args:
        print(
            "usage: embedded_dev.py board <controller|from-ioc> [options]\n"
            "run 'embedded_dev.py board controller --help' for details",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    return _run_helper("board_map", args.helper_args)


def cmd_memory(args: argparse.Namespace) -> int:
    if not args.helper_args:
        print(
            "usage: embedded_dev.py memory <detect|init|show|remember|search|list|forget> [options]\n"
            "run 'embedded_dev.py memory detect --help' for details",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    return _run_helper("project_memory", args.helper_args)


def _forward(script_name: str, flag_pairs: list[tuple[str, object]], as_json: bool) -> int:
    """Rebuild a sibling module's argv from parsed options and delegate.

    The main CLI keeps its own option spelling; this adapter is the single place
    that knows how a subcommand option maps onto the helper's flag, so the two
    surfaces cannot drift into different semantics.
    """
    argv: list[str] = []
    for flag, value in flag_pairs:
        if value is None or value is False:
            continue
        if value is True:
            argv.append(flag)
        else:
            argv.extend([flag, str(value)])
    if as_json:
        argv.append("--json")
    return _run_helper(script_name, argv)


def cmd_map(args: argparse.Namespace) -> int:
    return _forward(
        "map_parse",
        [
            ("--map", args.map_path),
            ("--top", args.top),
            ("--max-flash", args.max_flash),
            ("--max-ram", args.max_ram),
            ("--baseline", args.baseline),
            ("--write-baseline", args.write_baseline),
            ("--max-growth", args.max_growth if args.max_growth != "0" else None),
            ("--output", args.output),
        ],
        args.json,
    )


def cmd_svd(args: argparse.Namespace) -> int:
    return _forward(
        "svd_decode",
        [
            ("--svd", args.svd),
            ("--dump", args.dump),
            ("--values", args.values),
            ("--peripheral", args.peripheral),
            ("--find", args.find),
            ("--output", args.output),
        ],
        args.json,
    )


def cmd_dbc(args: argparse.Namespace) -> int:
    return _forward(
        "dbc_decode",
        [
            ("--dbc", args.dbc),
            ("--log", args.log),
            ("--hex", args.hex_data),
            ("--filter-id", args.filter_id),
            ("--output", args.output),
        ],
        args.json,
    )


def cmd_rtt(args: argparse.Namespace) -> int:
    return _forward(
        "rtt_capture",
        [
            ("--control-block-file", args.control_block_file),
            ("--buffer-file", args.buffer_file),
            ("--buffer-index", args.buffer_index if args.buffer_index != 0 else None),
            ("--jlink", args.jlink),
            ("--device", args.device),
            ("--control-block-address", args.control_block_address),
            ("--expect", args.expect),
            ("--dry-run", args.dry_run),
            ("--output", args.output),
        ],
        args.json,
    )


def cmd_size(args: argparse.Namespace) -> int:
    return _forward(
        "size_gate",
        [
            ("--elf", args.elf),
            ("--size-output-file", args.size_output_file),
            ("--max-text", args.max_text),
            ("--max-data", args.max_data),
            ("--max-bss", args.max_bss),
            ("--baseline", args.baseline),
            ("--write-baseline", args.write_baseline),
            ("--max-growth", args.max_growth if args.max_growth != "0" else None),
            ("--output", args.output),
        ],
        args.json,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="embedded_dev.py",
        description=(
            "embedded-dev tool layer: build, flash, monitor, registers, fault and protocol "
            "decoding, crash-address symbolization, RTOS stack watermarks, HIL diagnosis, "
            "control-loop evidence, board pin maps, project memory and report validation "
            "with an exit-code contract."
        ),
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
    regs.add_argument("--elf", help="ELF with DWARF data: resolve pc/lr symbols with addr2line (DWARF gate)")
    regs.add_argument("--dry-run", action="store_true")
    regs.add_argument("--json", action="store_true")
    regs.set_defaults(func=cmd_regs)

    symbolize = sub.add_parser(
        "symbolize",
        help="resolve crash addresses (any core: Cortex-M, Xtensa, RISC-V) to function:line with addr2line",
    )
    symbolize.add_argument("--elf", help="ELF with DWARF data from the exact build on the board")
    symbolize.add_argument("--addr", action="append", help="address to resolve, repeatable (hex)")
    symbolize.add_argument("--json", action="store_true")
    symbolize.set_defaults(func=cmd_symbolize)

    stack = sub.add_parser(
        "stack",
        help="report FreeRTOS task stack watermarks from vTaskList / uxTaskGetSystemState output",
    )
    stack.add_argument("--tasklist", help="file with vTaskList output; omit to read stdin")
    stack.add_argument("--min-free", dest="min_free", type=int, default=512, help="warning threshold in words (default 512)")
    stack.add_argument("--word-bytes", dest="word_bytes", type=int, default=4, help="bytes per stack word (default 4)")
    stack.add_argument("--json", action="store_true")
    stack.set_defaults(func=cmd_stack)

    fault = sub.add_parser("fault", help="decode Cortex-M fault registers")
    fault.add_argument("--cfsr", default="0x0")
    fault.add_argument("--hfsr", default="0x0")
    fault.add_argument("--mmfar")
    fault.add_argument("--bfar")
    fault.add_argument("--pc")
    fault.add_argument("--lr")
    fault.add_argument("--psp")
    fault.add_argument("--msp")
    fault.add_argument("--elf", help="ELF with DWARF data: resolve pc/lr symbols with addr2line (DWARF gate)")
    fault.add_argument("--json", action="store_true")
    fault.set_defaults(func=cmd_fault)

    decode = sub.add_parser("decode", help="decode UART frames or CAN ISO-TP/UDS/OBD-II/J1939 from a JSON protocol spec")
    decode.add_argument("kind", choices=["validate", "uart", "can"])
    decode.add_argument("--spec", required=True)
    decode.add_argument("--hex", help="inline hex bytes, or candump lines when kind=can")
    decode.add_argument("--file", help="captured stream file (hex text, raw bytes or candump log)")
    decode.add_argument("--log", help="candump log file (kind=can)")
    decode.add_argument("--format", default="hex", choices=["hex", "bin"])
    decode.add_argument("--strict", action="store_true", help="exit 4 when the stream contains decode errors")
    decode.add_argument("--json", action="store_true")
    decode.set_defaults(func=cmd_decode)

    hil = sub.add_parser("hil", help="build -> flash -> monitor a hardware pass token and write a diagnostic report")
    hil.add_argument("--project", required=True)
    hil.add_argument("--image", help="ELF/bin image to flash (required for a real run)")
    hil.add_argument("--expect", help="pass token the firmware must print (required for a real run)")
    hil.add_argument("--port")
    hil.add_argument("--baud", type=int, default=115200)
    hil.add_argument("--timeout", type=float, default=10.0)
    hil.add_argument("--build-backend", dest="build_backend", default="auto", choices=["auto", *BUILD_BACKENDS])
    hil.add_argument("--build-dir", default="build")
    hil.add_argument("--flash-backend", dest="flash_backend", default="auto", choices=["auto", *FLASH_BACKENDS])
    hil.add_argument("--chip")
    hil.add_argument("--target")
    hil.add_argument("--probe")
    hil.add_argument("--openocd-interface")
    hil.add_argument("--openocd-target", dest="openocd_target")
    hil.add_argument("--jlink-device", dest="jlink_device")
    hil.add_argument("--snr")
    hil.add_argument("--platformio-env", dest="platformio_env")
    hil.add_argument("--mock-file", dest="mock_file", help="offline: scan a captured byte file instead of a real port")
    hil.add_argument("--report-out", dest="report_out", default="diagnostic_report.json")
    hil.add_argument("--previous", help="previous diagnostic_report.json: detect repeated failures")
    hil.add_argument("--iteration", type=int, default=1)
    hil.add_argument("--restore-point", dest="restore_point", action="store_true", help="record a git stash commit before the attempt")
    hil.add_argument("--cfsr")
    hil.add_argument("--hfsr")
    hil.add_argument("--bfar")
    hil.add_argument("--pc")
    hil.add_argument("--lr")
    hil.add_argument("--dry-run", action="store_true")
    hil.add_argument("--json", action="store_true")
    hil.set_defaults(func=cmd_hil)

    report = sub.add_parser("report", help="validate a completion report against the evidence schema")
    report.add_argument("--file")
    report.add_argument("--stdin", action="store_true")
    report.add_argument("--emit-example", dest="emit_example", action="store_true")
    report.add_argument("--json", action="store_true")
    report.set_defaults(func=cmd_report)

    tune = sub.add_parser(
        "tune", help="control-loop evidence: analyze telemetry, check bounded updates, simulate, validate records"
    )
    tune.add_argument("helper_args", nargs=argparse.REMAINDER, help="arguments for experiment.py")
    tune.set_defaults(func=cmd_tune)

    board = sub.add_parser(
        "board", help="hardware facts: controller pin map from a KiCad XML netlist, or a CubeMX .ioc pinmap"
    )
    board.add_argument("helper_args", nargs=argparse.REMAINDER, help="arguments for board_map.py")
    board.set_defaults(func=cmd_board)

    memory = sub.add_parser(
        "memory", help="project archive and evidence-graded memory (detect/init/show/remember/search/list/forget)"
    )
    memory.add_argument("helper_args", nargs=argparse.REMAINDER, help="arguments for project_memory.py")
    memory.set_defaults(func=cmd_memory)

    map_parser = sub.add_parser("map", help="flash/RAM usage from a GNU ld map file, with budget and growth gates")
    map_parser.add_argument("--map", dest="map_path", required=True, help="GNU ld .map file")
    map_parser.add_argument("--top", type=int, default=15, help="how many largest symbols to list (default 15)")
    map_parser.add_argument("--max-flash", help="flash budget: 64K / 1M / 65536 / 0x10000")
    map_parser.add_argument("--max-ram", help="RAM budget")
    map_parser.add_argument("--baseline", help="baseline JSON from an earlier --write-baseline")
    map_parser.add_argument("--write-baseline", help="write current usage as a baseline JSON")
    map_parser.add_argument("--max-growth", default="0", help="allowed byte growth vs baseline (default 0)")
    map_parser.add_argument("--output", help="also write the JSON report here")
    map_parser.add_argument("--json", action="store_true")
    map_parser.set_defaults(func=cmd_map)

    svd_parser = sub.add_parser("svd", help="decode peripheral registers into named bitfields with a CMSIS-SVD file")
    svd_parser.add_argument("--svd", required=True, help="CMSIS-SVD XML file")
    svd_parser.add_argument("--dump", help="file of ADDRESS VALUE rows")
    svd_parser.add_argument("--values", help="inline rows, e.g. '0x40021000=0x03020001'")
    svd_parser.add_argument("--peripheral", help="restrict decoding to this peripheral")
    svd_parser.add_argument("--find", help="search register and field names")
    svd_parser.add_argument("--output", help="also write the JSON result here")
    svd_parser.add_argument("--json", action="store_true")
    svd_parser.set_defaults(func=cmd_svd)

    dbc_parser = sub.add_parser("dbc", help="decode candump frames into physical signals using a Vector DBC file")
    dbc_parser.add_argument("--dbc", required=True, help="Vector DBC file")
    dbc_parser.add_argument("--log", help="candump log file")
    dbc_parser.add_argument("--hex", dest="hex_data", help="inline frames, e.g. '7E0#0322F190'")
    dbc_parser.add_argument("--filter-id", help="only decode this CAN ID (hex)")
    dbc_parser.add_argument("--output", help="also write the JSON result here")
    dbc_parser.add_argument("--json", action="store_true")
    dbc_parser.set_defaults(func=cmd_dbc)

    rtt_parser = sub.add_parser("rtt", help="capture a SEGGER RTT channel from a dump or over J-Link")
    rtt_parser.add_argument("--control-block-file", help="offline: dump of SEGGER_RTT_CB")
    rtt_parser.add_argument("--buffer-file", help="offline: dump of the ring buffer")
    rtt_parser.add_argument("--buffer-index", type=int, default=0, help="up-buffer to read (default 0)")
    rtt_parser.add_argument("--jlink", help="path to JLinkExe for the live path")
    rtt_parser.add_argument("--device", help="J-Link device name")
    rtt_parser.add_argument("--control-block-address", help="live: RTT control block address")
    rtt_parser.add_argument("--expect", help="token that must appear in the captured text")
    rtt_parser.add_argument("--dry-run", action="store_true", help="print the JLink script and exit")
    rtt_parser.add_argument("--output", help="also write the JSON result here")
    rtt_parser.add_argument("--json", action="store_true")
    rtt_parser.set_defaults(func=cmd_rtt)

    size_parser = sub.add_parser("size", help="text/data/bss budget gate with baseline growth detection")
    size_parser.add_argument("--elf", help="ELF to measure with arm-none-eabi-size / size")
    size_parser.add_argument("--size-output-file", help="offline: saved `size -A` transcript")
    size_parser.add_argument("--max-text", help="text budget")
    size_parser.add_argument("--max-data", help="data budget")
    size_parser.add_argument("--max-bss", help="bss budget")
    size_parser.add_argument("--baseline", help="baseline JSON from an earlier --write-baseline")
    size_parser.add_argument("--write-baseline", help="write current sizes as a baseline JSON")
    size_parser.add_argument("--max-growth", default="0", help="allowed byte growth per bucket (default 0)")
    size_parser.add_argument("--output", help="also write the JSON result here")
    size_parser.add_argument("--json", action="store_true")
    size_parser.set_defaults(func=cmd_size)

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
