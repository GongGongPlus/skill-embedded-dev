"""Offline tests for protocol_decode.py: specs, checksums, UART and CAN decoding.

No hardware and no network are required. The CAN fixture is a real candump log
kept in examples/protocols so the same bytes can be replayed by hand.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples" / "protocols"


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, REPO / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


pd = load_module("protocol_decode_tests", "scripts/protocol_decode.py")


class TestChecksums(unittest.TestCase):
    def test_crc16_modbus_reference_vector(self):
        self.assertEqual(pd.compute_checksum(b"123456789", "crc16_modbus"), 0x4B37)

    def test_crc16_ccitt_reference_vector(self):
        self.assertEqual(pd.compute_checksum(b"123456789", "crc16_ccitt"), 0x29B1)

    def test_crc8_reference_vector(self):
        self.assertEqual(pd.compute_checksum(b"123456789", "crc8"), 0xF4)

    def test_simple_checksums(self):
        self.assertEqual(pd.compute_checksum(b"\x01\x02\x03", "sum8"), 6)
        self.assertEqual(pd.compute_checksum(b"\x01\x02\x03", "xor8"), 0)
        self.assertEqual(pd.compute_checksum(b"abc", "none"), 0)

    def test_unknown_checksum_is_config_error(self):
        with self.assertRaises(pd.ProtocolError):
            pd.compute_checksum(b"x", "sha256")


class TestSpecValidation(unittest.TestCase):
    def _spec(self) -> dict:
        return json.loads((EXAMPLES / "uart-env-sensor.json").read_text(encoding="utf-8"))

    def test_example_spec_is_valid(self):
        self.assertEqual(pd.validate_spec(self._spec()), [])

    def test_missing_header_is_reported(self):
        spec = self._spec()
        spec["framing"]["header"] = ""
        errors = pd.validate_spec(spec)
        self.assertTrue(any("header" in error for error in errors), errors)

    def test_bad_checksum_algorithm_is_reported(self):
        spec = self._spec()
        spec["framing"]["checksum"]["algorithm"] = "sha256"
        errors = pd.validate_spec(spec)
        self.assertTrue(any("checksum" in error for error in errors), errors)

    def test_duplicate_field_names_are_reported(self):
        spec = self._spec()
        spec["fields"][1]["name"] = spec["fields"][0]["name"]
        errors = pd.validate_spec(spec)
        self.assertTrue(any("duplicated" in error for error in errors), errors)

    def test_non_numeric_value_offset_is_reported(self):
        spec = self._spec()
        spec["fields"][2]["value_offset"] = "2"
        errors = pd.validate_spec(spec)
        self.assertTrue(any("value_offset" in error for error in errors), errors)

    def test_unknown_checksum_in_load_path_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            with self.assertRaises(pd.ProtocolError):
                pd.load_spec(broken)


class TestFieldDecoding(unittest.TestCase):
    def test_scale_and_value_offset_are_separate_from_byte_offset(self):
        payload = bytes([0x10, 0x27])  # 0x2710 = 10000 little-endian
        fields = [{"name": "value", "offset": 0, "size": 2, "type": "uint16", "scale": 0.1, "value_offset": -40}]
        decoded, details, warnings = pd.decode_fields(payload, fields, "little")
        self.assertAlmostEqual(decoded["value"], 960.0)
        self.assertEqual(details[0]["raw_hex"], "1027")
        self.assertEqual(warnings, [])

    def test_enum_and_bits_are_decoded_without_guessing(self):
        payload = bytes([0x02, 0x81])
        fields = [
            {"name": "kind", "offset": 0, "size": 1, "type": "uint8", "enum": {"1": "request", "2": "report"}},
            {"name": "flags", "offset": 1, "size": 1, "type": "uint8", "bits": {"0": "ok", "7": "low_battery"}},
        ]
        decoded, details, warnings = pd.decode_fields(payload, fields, "little")
        self.assertEqual(decoded["kind"], "report")
        self.assertEqual(details[1]["bits"], ["ok", "low_battery"])
        self.assertEqual(warnings, [])

    def test_unknown_enum_value_warns_instead_of_inventing_a_label(self):
        payload = bytes([9])
        fields = [{"name": "kind", "offset": 0, "size": 1, "type": "uint8", "enum": {"1": "request"}}]
        decoded, details, warnings = pd.decode_fields(payload, fields, "little")
        self.assertEqual(decoded["kind"], 9)
        self.assertTrue(any("not in the enum" in warning for warning in warnings), warnings)


class TestUartDecoding(unittest.TestCase):
    def setUp(self):
        self.spec = pd.load_spec(EXAMPLES / "uart-env-sensor.json")
        self.capture = pd._read_input(None, str(EXAMPLES / "capture-uart.hex"), "hex")

    def test_capture_decodes_two_clean_frames(self):
        result = pd.decode_uart_stream(self.spec, self.capture)
        self.assertEqual(result["summary"]["frames"], 2)
        self.assertEqual(result["summary"]["checksum_failures"], 0)
        first = result["frames"][0]
        self.assertEqual(first["fields"]["msg_type"], "report")
        self.assertEqual(first["fields"]["seq"], 7)
        self.assertAlmostEqual(first["fields"]["temperature"], 23.45)
        self.assertAlmostEqual(first["fields"]["humidity"], 54.3)

    def test_corrupted_byte_breaks_the_checksum(self):
        corrupted = bytearray(self.capture)
        corrupted[5] ^= 0xFF
        result = pd.decode_uart_stream(self.spec, bytes(corrupted))
        self.assertGreaterEqual(result["summary"]["checksum_failures"], 1)
        self.assertGreaterEqual(result["summary"]["frames_with_errors"], 1)

    def test_truncated_stream_is_reported_not_guessed(self):
        result = pd.decode_uart_stream(self.spec, self.capture[:14])
        self.assertTrue(result["errors"])
        self.assertEqual(result["summary"]["frames"], 1)

    def test_comment_lines_are_ignored(self):
        text = "# comment\nAA5509020729091F0281D977\n"
        data = pd._read_input(text, None, "hex")
        self.assertEqual(data, bytes.fromhex("AA5509020729091F0281D977"))


class TestCanDecoding(unittest.TestCase):
    def test_candump_log_decodes_uds_and_obd2(self):
        text = (EXAMPLES / "candump.log").read_text(encoding="utf-8")
        result = pd.decode_can_log(text)
        self.assertEqual(result["summary"]["can_frames"], 3)
        self.assertEqual(result["summary"]["uds"], 2)
        self.assertEqual(result["summary"]["obd2"], 1)
        self.assertEqual(result["errors"], [])
        obd = [message for message in result["messages"] if (message["decoded"] or {}).get("protocol") == "obd2"][0]
        self.assertAlmostEqual(obd["decoded"]["value"], 1726.0)
        self.assertEqual(obd["decoded"]["unit"], "rpm")

    def test_negative_response_translates_nrc(self):
        result = pd.decode_can_log("(1.0) can0 7E8#037F2231\n")
        decoded = result["messages"][0]["decoded"]
        self.assertEqual(decoded["protocol"], "uds")
        self.assertEqual(decoded["nrc"], "0x31")
        self.assertEqual(decoded["nrc_name"], "requestOutOfRange")

    def test_isotp_multi_frame_is_reassembled(self):
        log = "\n".join(
            [
                "(1.0) can0 7E0#100F62F190",  # first frame: 15 payload bytes
                "(1.1) can0 7E0#21AABBCCDDEEFF",  # consecutive frame
                "(1.2) can0 7E0#22001122334455",  # consecutive frame (15 bytes total)
            ]
        )
        result = pd.decode_can_log(log)
        message = result["messages"][0]
        self.assertEqual(message["status"], "complete")
        self.assertEqual(message["length"], 15)
        self.assertEqual(len(bytes.fromhex(message["payload_hex"])), 15)

    def test_extended_j1939_frame_is_recognised(self):
        frame = pd.parse_candump_line("(1.0) can0 0CF00400#FF7DFFFFFFFFFFFF")
        self.assertIsNotNone(frame)
        self.assertTrue(frame["extended"])
        self.assertEqual(frame["id"], 0x0CF00400)

    def test_unparseable_line_is_an_error(self):
        result = pd.decode_can_log("this is not a candump line\n")
        self.assertTrue(result["errors"])


class TestCli(unittest.TestCase):
    def _run(self, *args: str):
        return subprocess.run(
            [sys.executable, str(REPO / "scripts" / "protocol_decode.py"), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def test_validate_ok(self):
        out = self._run("validate", "--spec", str(EXAMPLES / "uart-env-sensor.json"), "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(json.loads(out.stdout)["valid"])

    def test_validate_bad_spec_exits_6(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps({"schema_version": "wrong"}), encoding="utf-8")
            out = self._run("validate", "--spec", str(bad))
            self.assertEqual(out.returncode, 6, out.stdout + out.stderr)

    def test_uart_strict_with_corrupted_frame_exits_4(self):
        out = self._run(
            "uart",
            "--spec", str(EXAMPLES / "uart-env-sensor.json"),
            "--hex", "AA5509020729091F0281FFFF",
            "--strict",
        )
        self.assertEqual(out.returncode, 4, out.stdout + out.stderr)

    def test_can_log_decodes(self):
        out = self._run("can", "--log", str(EXAMPLES / "candump.log"), "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["summary"]["obd2"], 1)


if __name__ == "__main__":
    unittest.main()
