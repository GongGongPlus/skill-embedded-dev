"""Tests for scripts/rtt_capture.py: RTT control block parsing and ring-buffer reads."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import struct
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("rtt_capture_tests", REPO / "scripts" / "rtt_capture.py")
rtt = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(rtt)

EXAMPLES = REPO / "examples" / "rtt"


def build_control_block(name: str = "Terminal", size: int = 64, wr: int = 0, rd: int = 0, flags: int = 0) -> bytes:
    cb = struct.pack("<16sii", b"SEGGER RTT", 1, 0)
    cb += struct.pack("<16sIIIII", name.encode(), 0x20001000, size, wr, rd, flags)
    return cb


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = rtt.main(argv)
    return code, out.getvalue(), err.getvalue()


class TestControlBlock(unittest.TestCase):
    def test_parses_identifier_and_buffer(self):
        parsed = rtt.parse_control_block(build_control_block())
        self.assertTrue(parsed["identifier"].startswith("SEGGER RTT"))
        self.assertEqual(parsed["max_up_buffers"], 1)
        self.assertEqual(len(parsed["buffers"]), 1)
        buffer = parsed["buffers"][0]
        self.assertEqual(buffer["name"], "Terminal")
        self.assertEqual(buffer["size"], 64)
        self.assertEqual(buffer["buffer_address"], 0x20001000)

    def test_wrong_magic_is_rejected(self):
        bad = struct.pack("<16sii", b"NOT RTT AT ALL", 1, 0) + struct.pack("<16sIIIII", b"T", 0, 64, 0, 0, 0)
        with self.assertRaises(rtt.RttError) as ctx:
            rtt.parse_control_block(bad)
        self.assertIn("SEGGER RTT", str(ctx.exception))

    def test_too_short_is_rejected(self):
        with self.assertRaises(rtt.RttError):
            rtt.parse_control_block(b"SEGGER RTT")

    def test_implausible_buffer_count_is_rejected(self):
        bad = struct.pack("<16sii", b"SEGGER RTT", 9, 0)
        with self.assertRaises(rtt.RttError) as ctx:
            rtt.parse_control_block(bad)
        self.assertIn("MaxNumUpBuffers", str(ctx.exception))

    def test_flags_are_described(self):
        self.assertIn("BLOCK_IF_FULL", rtt.describe_flags(0))
        self.assertIn("NO_BLOCK_SKIP", rtt.describe_flags(1 << 1))
        self.assertIn("NO_BLOCK_TRIM", rtt.describe_flags(1 << 2))


class TestRingBuffer(unittest.TestCase):
    def test_empty_when_wr_equals_rd(self):
        buffer = {"name": "b", "size": 8, "wr_off": 4, "rd_off": 4}
        self.assertEqual(rtt.read_buffer(buffer, bytes(8)), b"")

    def test_linear_case(self):
        buffer = {"name": "b", "size": 8, "wr_off": 5, "rd_off": 2}
        data = b"01234567"
        self.assertEqual(rtt.read_buffer(buffer, data), b"234")

    def test_wrapped_case(self):
        """wr < rd means the writer wrapped: read the tail then the head."""
        buffer = {"name": "b", "size": 8, "wr_off": 3, "rd_off": 5}
        data = b"01234567"  # tail "567" then head "012"
        self.assertEqual(rtt.read_buffer(buffer, data), b"567012")

    def test_offsets_beyond_size_are_modulo(self):
        buffer = {"name": "b", "size": 8, "wr_off": 11, "rd_off": 8}
        self.assertEqual(rtt.read_buffer(buffer, b"01234567"), b"012")

    def test_zero_sized_buffer_is_rejected(self):
        with self.assertRaises(rtt.RttError):
            rtt.read_buffer({"name": "b", "size": 0, "wr_off": 0, "rd_off": 0}, b"")

    def test_short_dump_is_rejected(self):
        with self.assertRaises(rtt.RttError) as ctx:
            rtt.read_buffer({"name": "b", "size": 64, "wr_off": 4, "rd_off": 0}, bytes(8))
        self.assertIn("must cover the whole ring buffer", str(ctx.exception))


class TestExtractText(unittest.TestCase):
    def test_example_fixture_round_trip(self):
        control = (EXAMPLES / "control-block.bin").read_bytes()
        buffer = (EXAMPLES / "buffer.bin").read_bytes()
        result = rtt.extract_text(control, {0: buffer}, 0)
        self.assertEqual(result["buffer"]["wr_off"], 20)
        self.assertEqual(result["buffer"]["rd_off"], 40)
        self.assertEqual(result["bytes"], 44)
        self.assertIn("RTT_TEST_PASSED", result["text"])
        self.assertTrue(result["text"].startswith("boot ok"))

    def test_requesting_a_missing_buffer_index_is_rejected(self):
        control = build_control_block()
        with self.assertRaises(rtt.RttError):
            rtt.extract_text(control, {0: bytes(64)}, 5)

    def test_missing_buffer_dump_is_rejected(self):
        with self.assertRaises(rtt.RttError) as ctx:
            rtt.extract_text(build_control_block(), {}, 0)
        self.assertIn("no buffer dump", str(ctx.exception))


class TestJlinkHelpers(unittest.TestCase):
    def test_script_lists_device_and_memory_reads(self):
        script = rtt.build_jlink_script("STM32F103C8", "SWD", 0x20000000, 3)
        self.assertIn("device STM32F103C8", script)
        self.assertIn("connect", script)
        self.assertIn("mem 0x20000000 4", script)
        self.assertIn("mem 0x20000008 4", script)
        self.assertEqual(script[-1], "exit")

    def test_mem_output_is_reassembled_in_order(self):
        """JLink prints 32-bit words; the payload is their little-endian bytes.

        0x52544720 -> 20 47 54 52 -> " GTR"
        0x4E414D45 -> 45 4D 41 4E -> "EMAN"
        """
        text = "20000000 = 52544720\n20000004 = 4E414D45\n"
        self.assertEqual(rtt.parse_mem_output(text), b" GTR" + b"EMAN")

    def test_mem_rows_are_sorted_by_address(self):
        reversed_text = "20000004 = 4E414D45\n20000000 = 52544720\n"
        self.assertEqual(rtt.parse_mem_output(reversed_text), b" GTR" + b"EMAN")

    def test_mem_output_without_rows_is_rejected(self):
        with self.assertRaises(rtt.RttError):
            rtt.parse_mem_output("no memory here\n")


class TestCli(unittest.TestCase):
    def test_offline_capture_with_matching_token_exits_zero(self):
        code, out, _ = run_main(
            [
                "--control-block-file", str(EXAMPLES / "control-block.bin"),
                "--buffer-file", str(EXAMPLES / "buffer.bin"),
                "--expect", "RTT_TEST_PASSED",
            ]
        )
        self.assertEqual(code, rtt.EXIT_OK)
        self.assertIn("found=True", out)

    def test_missing_token_exits_four(self):
        code, _, _ = run_main(
            [
                "--control-block-file", str(EXAMPLES / "control-block.bin"),
                "--buffer-file", str(EXAMPLES / "buffer.bin"),
                "--expect", "NEVER_PRINTED",
            ]
        )
        self.assertEqual(code, rtt.EXIT_AMBIGUOUS)

    def test_no_token_declared_exits_zero(self):
        code, _, _ = run_main(
            [
                "--control-block-file", str(EXAMPLES / "control-block.bin"),
                "--buffer-file", str(EXAMPLES / "buffer.bin"),
            ]
        )
        self.assertEqual(code, rtt.EXIT_OK)

    def test_json_fields(self):
        code, out, _ = run_main(
            [
                "--control-block-file", str(EXAMPLES / "control-block.bin"),
                "--buffer-file", str(EXAMPLES / "buffer.bin"),
                "--expect", "RTT_TEST_PASSED",
                "--json",
            ]
        )
        self.assertEqual(code, rtt.EXIT_OK)
        payload = json.loads(out)
        for key in ("schema_version", "control_block", "buffer", "bytes", "text", "token_found"):
            self.assertIn(key, payload)
        self.assertEqual(payload["schema_version"], "rtt-capture.v1")

    def test_dry_run_prints_script_and_exits_zero(self):
        code, out, _ = run_main(
            ["--device", "STM32F103C8", "--control-block-address", "0x20000000", "--dry-run"]
        )
        self.assertEqual(code, rtt.EXIT_OK)
        self.assertIn("would run:", out)
        self.assertIn("mem 0x20000000 4", out)

    def test_missing_jlink_is_a_config_error(self):
        code, _, err = run_main(
            [
                "--device", "STM32F103C8",
                "--control-block-address", "0x20000000",
                "--jlink", "definitely-not-jlink",
            ]
        )
        self.assertEqual(code, rtt.EXIT_CONFIG)

    def test_control_block_without_buffer_is_a_config_error(self):
        code, _, err = run_main(["--control-block-file", str(EXAMPLES / "control-block.bin")])
        self.assertEqual(code, rtt.EXIT_CONFIG)
        self.assertIn("--buffer-file", err)

    def test_missing_inputs_are_config_errors(self):
        code, _, err = run_main(["--control-block-file", "nope.bin", "--buffer-file", "nope2.bin"])
        self.assertEqual(code, rtt.EXIT_CONFIG)
        self.assertIn("control block file not found", err)

    def test_live_without_device_is_a_config_error(self):
        code, _, err = run_main(["--control-block-address", "0x20000000"])
        self.assertEqual(code, rtt.EXIT_CONFIG)
        self.assertIn("--device", err)

    def test_live_without_address_is_a_config_error(self):
        code, _, err = run_main(["--device", "STM32F103C8"])
        self.assertEqual(code, rtt.EXIT_CONFIG)
        self.assertIn("--control-block-address", err)

    def test_output_flag_writes_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "rtt.json"
            run_main(
                [
                    "--control-block-file", str(EXAMPLES / "control-block.bin"),
                    "--buffer-file", str(EXAMPLES / "buffer.bin"),
                    "--output", str(target),
                ]
            )
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["schema_version"], "rtt-capture.v1")


if __name__ == "__main__":
    unittest.main()