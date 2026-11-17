#!/usr/bin/env python3
"""SEGGER RTT reading: turn the RTT control block and ring buffers into text.

`monitor` needs a UART. When the debug probe is already attached, RTT gives the
same visibility over the SWD/JTAG link with no extra wiring — which is exactly
the situation where a board is wedged and the UART is the thing being debugged.

The offline path (a captured control block plus buffer dump) is implemented as
pure functions so it can be tested without a probe. The live path drives
JLinkExe and is only proven by a dry run here; it is reported as unverified
rather than pretended to work.

Exit codes: 0 token captured (or nothing to fail), 4 token not captured /
ambiguous evidence, 6 configuration error (missing inputs or JLink).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_AMBIGUOUS = 4
EXIT_CONFIG = 6

RTT_ID = b"SEGGER RTT"
# SEGGER_RTT_CB layout (32-bit): acId[16], MaxNumUpBuffers (i32), MaxNumDownBuffers (i32)
CB_HEADER = struct.Struct("<16sii")
# SEGGER_RTT_BUFFER_UP: sName[16], pBuffer(u32), SizeOfBuffer(u32), WrOff(u32), RdOff(u32), Flags(u32)
BUFFER_UP = struct.Struct("<16sIIIII")
RTT_FLAG_NO_BLOCK_SKIP = 1 << 1
RTT_FLAG_NO_BLOCK_TRIM = 1 << 2
MEM_LINE_RE = re.compile(r"^(?P<address>[0-9A-Fa-f]{8})\s*=\s*(?P<words>(?:[0-9A-Fa-f]{8}\s*)+)$")


class RttError(Exception):
    """The inputs cannot be used; report instead of guessing."""


def _cstring(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


def parse_control_block(data: bytes) -> dict[str, Any]:
    """Decode a SEGGER_RTT_CB dump into its buffers."""
    if len(data) < CB_HEADER.size:
        raise RttError(f"control block is {len(data)} bytes, need at least {CB_HEADER.size}")
    identifier, max_up, max_down = CB_HEADER.unpack_from(data, 0)
    if not identifier.startswith(RTT_ID):
        raise RttError(
            f"control block magic is {identifier[:16]!r}, expected it to start with 'SEGGER RTT'; "
            "this is not an RTT control block"
        )
    if max_up < 0 or max_up > 3:
        raise RttError(f"MaxNumUpBuffers is {max_up}; RTT allows 0..3")
    offset = CB_HEADER.size
    buffers = []
    for index in range(max_up):
        if offset + BUFFER_UP.size > len(data):
            raise RttError(f"control block ends before up-buffer {index}")
        name, pointer, size, wr_off, rd_off, flags = BUFFER_UP.unpack_from(data, offset)
        buffers.append(
            {
                "index": index,
                "name": _cstring(name),
                "buffer_address": pointer,
                "size": size,
                "wr_off": wr_off,
                "rd_off": rd_off,
                "flags": flags,
                "mode": describe_flags(flags),
            }
        )
        offset += BUFFER_UP.size
    return {
        "identifier": _cstring(identifier),
        "max_up_buffers": max_up,
        "max_down_buffers": max_down,
        "buffers": buffers,
    }


def describe_flags(flags: int) -> str:
    if flags & RTT_FLAG_NO_BLOCK_SKIP:
        return "NO_BLOCK_SKIP (writer skips bytes when the reader falls behind)"
    if flags & RTT_FLAG_NO_BLOCK_TRIM:
        return "NO_BLOCK_TRIM (writer discards oldest data to make room)"
    return "BLOCK_IF_FULL (writer blocks when the buffer is full)"


def read_buffer(buffer: dict[str, Any], data: bytes) -> bytes:
    """Return the bytes written since RdOff, handling the ring wrap.

    WrOff == RdOff means empty. WrOff > RdOff is the linear case. WrOff < RdOff
    means the writer wrapped, so the live data is [RdOff:size] + [0:WrOff].
    """
    size = buffer["size"]
    if size <= 0:
        raise RttError(f"buffer {buffer['name']!r} has SizeOfBuffer={size}")
    if len(data) < size:
        raise RttError(
            f"buffer dump is {len(data)} bytes but the control block declares {size}; "
            "the dump must cover the whole ring buffer"
        )
    wr_off = buffer["wr_off"] % size
    rd_off = buffer["rd_off"] % size
    if wr_off == rd_off:
        return b""
    if wr_off > rd_off:
        return data[rd_off:wr_off]
    return data[rd_off:size] + data[:wr_off]


def extract_text(control_block: bytes, buffers: dict[int, bytes], index: int = 0) -> dict[str, Any]:
    parsed = parse_control_block(control_block)
    if index >= len(parsed["buffers"]):
        raise RttError(f"up-buffer {index} requested but the control block declares {len(parsed['buffers'])}")
    buffer = parsed["buffers"][index]
    if index not in buffers:
        raise RttError(f"no buffer dump supplied for up-buffer {index}")
    payload = read_buffer(buffer, buffers[index])
    return {
        "control_block": parsed,
        "buffer": buffer,
        "bytes": len(payload),
        "text": payload.decode("utf-8", errors="replace"),
        "hex": payload.hex(),
    }


def build_jlink_script(device: str, interface: str, control_block: int, num_words: int) -> list[str]:
    script = [
        "si SWD",
        f"device {device}",
        "speed 4000",
        "connect",
        "halt",
    ]
    for word in range(num_words):
        script.append(f"mem 0x{control_block + word * 4:08X} 4")
    script.append("exit")
    _ = interface  # kept for a future multi-interface backend; SWD is the current default
    return script


def parse_mem_output(text: str) -> bytes:
    """Turn JLinkExe 'mem ADDR 4' output lines into a byte string."""
    chunks: list[tuple[int, bytes]] = []
    for line in text.splitlines():
        match = MEM_LINE_RE.match(line.strip())
        if not match:
            continue
        address = int(match.group("address"), 16)
        words = match.group("words").split()
        payload = b"".join(struct.pack("<I", int(word, 16)) for word in words)
        chunks.append((address, payload))
    if not chunks:
        raise RttError("no memory rows found in the JLink output; cannot reconstruct the control block")
    chunks.sort(key=lambda item: item[0])
    return b"".join(payload for _, payload in chunks)


def capture_via_jlink(
    jlink: str, script: list[str], address: int, words: int, buffer_index: int
) -> dict[str, Any]:
    """Run JLinkExe once for the control block, then again for the ring buffer.

    Two passes because the control block states where the buffer lives and how
    large it is; guessing that size would defeat the point of reading it.
    """
    if str(Path(jlink).parent) not in os.environ.get("PATH", ""):
        pass  # JLinkExe finds its own DLLs by its install path, not ours.

    def run_once(commands: list[str]) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "rtt.jlink"
            script_path.write_text("\n".join(commands) + "\n", encoding="utf-8")
            try:
                proc = subprocess.run(
                    [jlink, "-CommanderScript", str(script_path)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                )
            except FileNotFoundError as exc:
                raise RttError(f"JLink executable not found: {jlink}") from exc
            except subprocess.TimeoutExpired as exc:
                raise RttError(f"JLinkExe timed out after 60s: {exc}") from exc
        if proc.returncode != 0:
            raise RttError(f"JLinkExe failed (exit {proc.returncode}): {(proc.stdout or proc.stderr)[-400:]}")
        return proc.stdout or ""

    control_bytes = parse_mem_output(run_once(script))
    parsed = parse_control_block(control_bytes)
    if buffer_index >= len(parsed["buffers"]):
        raise RttError(f"up-buffer {buffer_index} not present; the device declares {len(parsed['buffers'])}")
    buffer = parsed["buffers"][buffer_index]
    size = buffer["size"]
    if size <= 0:
        raise RttError(f"up-buffer {buffer_index} has SizeOfBuffer={size}")

    device_commands = [line for line in script if not line.startswith("mem ")]
    read_words = (size + 3) // 4
    for word in range(read_words):
        device_commands.insert(
            len(device_commands) - 1, f"mem 0x{buffer['buffer_address'] + word * 4:08X} 4"
        )
    buffer_bytes = parse_mem_output(run_once(device_commands))[:size]
    _ = words
    payload = read_buffer(buffer, buffer_bytes)
    return {
        "control_block": parsed,
        "buffer": buffer,
        "bytes": len(payload),
        "text": payload.decode("utf-8", errors="replace"),
        "hex": payload.hex(),
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass

    parser = argparse.ArgumentParser(
        prog="rtt_capture.py",
        description=(
            "Read a SEGGER RTT channel from a captured control block (offline, testable) or "
            "over J-Link. A token that is not captured is an ambiguity (exit 4), never a pass."
        ),
    )
    parser.add_argument("--control-block-file", help="offline: dump of SEGGER_RTT_CB")
    parser.add_argument("--buffer-file", help="offline: dump of up-buffer 0")
    parser.add_argument("--buffer-index", type=int, default=0, help="up-buffer to read (default 0)")
    parser.add_argument("--jlink", help="path to JLinkExe for the live path")
    parser.add_argument("--device", help="J-Link device name, e.g. STM32F103C8")
    parser.add_argument("--interface", default="SWD", help="debug interface (default SWD)")
    parser.add_argument("--control-block-address", help="live: RTT control block address, e.g. 0x20000000")
    parser.add_argument("--control-block-words", type=int, default=96, help="live: words to read (default 96)")
    parser.add_argument("--expect", help="token that must appear in the captured text")
    parser.add_argument("--dry-run", action="store_true", help="print the JLink script and exit")
    parser.add_argument("--output", help="also write the JSON result to this path")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a text summary")
    parser.add_argument("--version", action="version", version="embedded-dev rtt_capture 0.8.0")
    args = parser.parse_args(argv)

    try:
        if args.control_block_file:
            if not args.buffer_file:
                raise RttError("--control-block-file also needs --buffer-file")
            control = Path(args.control_block_file)
            buffer_path = Path(args.buffer_file)
            if not control.is_file():
                raise RttError(f"control block file not found: {control}")
            if not buffer_path.is_file():
                raise RttError(f"buffer file not found: {buffer_path}")
            result = extract_text(
                control.read_bytes(), {args.buffer_index: buffer_path.read_bytes()}, args.buffer_index
            )
            result["source"] = {"kind": "offline", "control_block": str(control), "buffer": str(buffer_path)}
        else:
            if not args.device:
                raise RttError("live capture needs --device (and --control-block-address)")
            if not args.control_block_address:
                raise RttError("live capture needs --control-block-address")
            address = int(args.control_block_address, 16)
            script = build_jlink_script(args.device, args.interface, address, args.control_block_words)
            jlink = args.jlink or shutil.which("JLinkExe") or shutil.which("JLink")
            if args.dry_run:
                print("would run: " + (jlink or "<JLinkExe not found>"))
                for line in script:
                    print(f"  {line}")
                return EXIT_OK
            if not jlink:
                raise RttError("JLinkExe not found; pass --jlink or put it on PATH")
            result = capture_via_jlink(
                jlink, script, address, args.control_block_words, args.buffer_index
            )
            result["source"] = {"kind": "jlink", "jlink": jlink, "device": args.device, "address": f"0x{address:08x}"}

        if args.expect:
            found = args.expect in result["text"]
            result["expect"] = args.expect
            result["token_found"] = found
        else:
            result["token_found"] = None
            found = True
    except RttError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    payload = {
        "schema_version": "rtt-capture.v1",
        **result,
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    if args.json:
        print(rendered)
    else:
        buffer = result["buffer"]
        print(
            f"buffer {buffer['index']} {buffer['name']!r} "
            f"{buffer['size']} bytes, wr={buffer['wr_off']} rd={buffer['rd_off']} [{buffer['mode']}]"
        )
        print(f"{result['bytes']} bytes captured")
        print(result["text"], end="" if result["text"].endswith("\n") else "\n")
        if args.expect:
            print(f"token {args.expect!r} found={found}")
    return EXIT_OK if found else EXIT_AMBIGUOUS


if __name__ == "__main__":
    sys.exit(main())