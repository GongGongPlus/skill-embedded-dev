"""Tests for scripts/dbc_decode.py: DBC parsing and Intel/Motorola signal decoding.

The bit-order tests are hand-computable on purpose. Getting Intel and Motorola
mixed up produces plausible-looking numbers, so each case below states the
expected value that can be verified by hand from the byte layout.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("dbc_decode_tests", REPO / "scripts" / "dbc_decode.py")
dbc = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(dbc)

DEMO_DBC = REPO / "examples" / "dbc" / "demo.dbc"
DEMO_LOG = REPO / "examples" / "dbc" / "demo-candump.log"


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = dbc.main(argv)
    return code, out.getvalue(), err.getvalue()


class TestBitOrder(unittest.TestCase):
    """data = 12 34 AB CD.

    Intel positions count up from the LSB; Motorola positions start at the MSB
    and walk down, jumping to the next byte's bit 7 when a byte's bit 0 is passed.
    """

    DATA = bytes([0x12, 0x34, 0xAB, 0xCD])

    def test_intel_positions_are_linear(self):
        self.assertEqual(dbc.intel_positions(0, 8), [0, 1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(dbc.intel_positions(8, 4), [8, 9, 10, 11])

    def test_motorola_positions_walk_down_then_jump(self):
        self.assertEqual(dbc.motorola_positions(7, 8), [7, 6, 5, 4, 3, 2, 1, 0])
        self.assertEqual(dbc.motorola_positions(15, 8), [15, 14, 13, 12, 11, 10, 9, 8])
        self.assertEqual(
            dbc.motorola_positions(7, 16),
            [7, 6, 5, 4, 3, 2, 1, 0, 15, 14, 13, 12, 11, 10, 9, 8],
        )
        # Starting mid-byte continues into the next byte rather than stopping.
        self.assertEqual(dbc.motorola_positions(3, 8), [3, 2, 1, 0, 15, 14, 13, 12])

    def test_intel_byte_zero_is_0x12(self):
        signal = {"start": 0, "length": 8, "order": "intel"}
        self.assertEqual(dbc.extract_raw(self.DATA, signal), 0x12)

    def test_intel_byte_one_is_0x34(self):
        signal = {"start": 8, "length": 8, "order": "intel"}
        self.assertEqual(dbc.extract_raw(self.DATA, signal), 0x34)

    def test_intel_16bit_is_little_endian(self):
        """Low byte at start=0, high byte at start=8 -> 0x3412, not 0x1234."""
        signal = {"start": 0, "length": 16, "order": "intel"}
        self.assertEqual(dbc.extract_raw(self.DATA, signal), 0x3412)

    def test_motorola_byte_zero_is_0x12(self):
        """start=7 means bit 7, the MSB of byte 0."""
        signal = {"start": 7, "length": 8, "order": "motorola"}
        self.assertEqual(dbc.extract_raw(self.DATA, signal), 0x12)

    def test_motorola_byte_one_is_0x34(self):
        signal = {"start": 15, "length": 8, "order": "motorola"}
        self.assertEqual(dbc.extract_raw(self.DATA, signal), 0x34)

    def test_motorola_16bit_is_big_endian(self):
        signal = {"start": 7, "length": 16, "order": "motorola"}
        self.assertEqual(dbc.extract_raw(self.DATA, signal), 0x1234)

    def test_motorola_crossing_a_byte_boundary(self):
        """start=3 spans the low nibble of byte 0 and the high nibble of byte 1."""
        signal = {"start": 3, "length": 8, "order": "motorola"}
        self.assertEqual(dbc.extract_raw(self.DATA, signal), 0x23)

    def test_signed_conversion(self):
        self.assertEqual(dbc.apply_signed(0xFF, 8), -1)
        self.assertEqual(dbc.apply_signed(0x7F, 8), 127)
        self.assertEqual(dbc.apply_signed(0x00, 8), 0)


class TestDbcParsing(unittest.TestCase):
    def setUp(self):
        self.model = dbc.parse_dbc(DEMO_DBC.read_text(encoding="utf-8"))

    def test_messages_are_keyed_by_id(self):
        self.assertEqual(self.model["message_count"], 3)
        self.assertIn(416, self.model["messages"])
        self.assertEqual(self.model["messages"][416]["name"], "ENGINE_STATUS")
        self.assertEqual(self.model["messages"][416]["dlc"], 8)

    def test_signal_attributes(self):
        engine_speed = self.model["messages"][416]["signals"]["EngineSpeed"]
        self.assertEqual(engine_speed["start"], 0)
        self.assertEqual(engine_speed["length"], 16)
        self.assertEqual(engine_speed["order"], "intel")
        self.assertEqual(engine_speed["factor"], 0.25)
        self.assertEqual(engine_speed["unit"], "rpm")

    def test_motorola_signals_are_flagged(self):
        gear = self.model["messages"][512]["signals"]["GearRatio"]
        self.assertEqual(gear["order"], "motorola")

    def test_val_blocks_attach_to_signals(self):
        door_state = self.model["messages"][256]["signals"]["DoorState"]
        self.assertEqual(door_state["enumerated"][1], "front_left_open")
        self.assertEqual(door_state["enumerated"][2], "front_right_open")

    def test_multiplexer_markers(self):
        signals = self.model["messages"][256]["signals"]
        self.assertEqual(signals["DoorState"]["mux_kind"], "selector")
        self.assertIsNone(signals["DoorState"]["mux_value"])
        self.assertEqual(signals["DoorFL"]["mux_kind"], "multiplexed")
        self.assertEqual(signals["DoorFL"]["mux_value"], 0)
        self.assertEqual(signals["WindowPos"]["mux_value"], 1)

    def test_other_sections_are_ignored(self):
        """BO_TX_BU_, CM_ and friends must not break the parser."""
        self.assertIn("ENGINE_STATUS", [msg["name"] for msg in self.model["messages"].values()])


class TestDecoding(unittest.TestCase):
    def setUp(self):
        self.model = dbc.parse_dbc(DEMO_DBC.read_text(encoding="utf-8"))

    def test_engineering_values_from_known_bytes(self):
        """E0 2E | 82 | 80 -> rpm 3000, temp 90, throttle 50.196%."""
        data = bytes.fromhex("E02E828000000000")
        result = dbc.decode_message(self.model["messages"][416], data)
        signals = {signal["name"]: signal for signal in result["signals"]}
        self.assertAlmostEqual(signals["EngineSpeed"]["physical"], 3000.0)
        self.assertAlmostEqual(signals["EngineTemp"]["physical"], 90.0)
        self.assertAlmostEqual(signals["ThrottlePos"]["physical"], 50.196, places=3)

    def test_motorola_values_from_known_bytes(self):
        """0B B8 | 1F 40 -> gear ratio 3.000, oil pressure 800.0 bar."""
        data = bytes.fromhex("0BB81F4000000000")
        result = dbc.decode_message(self.model["messages"][512], data)
        signals = {signal["name"]: signal for signal in result["signals"]}
        self.assertAlmostEqual(signals["GearRatio"]["physical"], 3.0)
        self.assertAlmostEqual(signals["OilPressure"]["physical"], 800.0)

    def test_enumerated_meaning_is_attached(self):
        data = bytes.fromhex("0200002A00000000")
        result = dbc.decode_message(self.model["messages"][256], data)
        signals = {signal["name"]: signal for signal in result["signals"]}
        self.assertEqual(signals["DoorState"]["meaning"], "front_right_open")

    def test_mux_selects_the_matching_branch(self):
        """DoorState == 1 selects m1, so WindowPos decodes and the m0 signals skip."""
        data = bytes.fromhex("01FF002A00000000")
        result = dbc.decode_message(self.model["messages"][256], data)
        signals = {signal["name"]: signal for signal in result["signals"]}
        self.assertEqual(signals["DoorState"]["raw"], 1)
        self.assertEqual(signals["WindowPos"]["status"], "decoded")
        self.assertEqual(signals["DoorFL"]["status"], "skipped")
        self.assertIn("does not select m0", signals["DoorFL"]["reason"])

    def test_mux_value_zero_selects_the_m0_branch(self):
        data = bytes.fromhex("00FF002A00000000")
        result = dbc.decode_message(self.model["messages"][256], data)
        signals = {signal["name"]: signal for signal in result["signals"]}
        self.assertEqual(signals["DoorFL"]["status"], "decoded")
        self.assertEqual(signals["DoorFR"]["status"], "decoded")
        self.assertEqual(signals["WindowPos"]["status"], "skipped")

    def test_unselected_branch_is_skipped_not_guessed(self):
        data = bytes.fromhex("0200002A00000000")
        result = dbc.decode_message(self.model["messages"][256], data)
        signals = {signal["name"]: signal for signal in result["signals"]}
        self.assertEqual(signals["DoorFL"]["status"], "skipped")
        self.assertNotIn("physical", signals["DoorFL"])

    def test_dlc_mismatch_is_flagged(self):
        result = dbc.decode_message(self.model["messages"][416], bytes.fromhex("E02E82"))
        self.assertEqual(result["status"], "dlc_mismatch")

    def test_short_frame_reads_missing_bytes_as_zero(self):
        """A short frame must not raise; it decodes and reports the mismatch."""
        result = dbc.decode_message(self.model["messages"][416], bytes.fromhex("E02E"))
        signals = {signal["name"]: signal for signal in result["signals"]}
        self.assertEqual(signals["EngineTemp"]["raw"], 0)


class TestFrames(unittest.TestCase):
    def test_candump_parsing(self):
        frames = dbc.parse_candump(
            "# comment\n(1700000000.0) can0 1A0#E02E828000000000\n(1700000001.0) can0 7E0#0322F190\n"
        )
        self.assertEqual(frames[0][0], 0x1A0)
        self.assertEqual(frames[0][1], bytes.fromhex("E02E828000000000"))
        self.assertEqual(len(frames), 2)

    def test_inline_hex_parsing(self):
        frames = dbc.parse_inline_hex("1A0#1122,200#AABB")
        self.assertEqual(frames, [(0x1A0, bytes.fromhex("1122")), (0x200, bytes.fromhex("AABB"))])

    def test_inline_hex_rejects_garbage(self):
        with self.assertRaises(dbc.DbcError):
            dbc.parse_inline_hex("not-a-frame")


class TestCli(unittest.TestCase):
    def test_demo_log_decodes_with_one_unknown_id(self):
        code, out, _ = run_main(["--dbc", str(DEMO_DBC), "--log", str(DEMO_LOG)])
        self.assertEqual(code, dbc.EXIT_AMBIGUOUS)
        self.assertIn("UNKNOWN ID", out)
        self.assertIn("3000.000 rpm", out)

    def test_known_frames_only_exit_zero(self):
        code, _, _ = run_main(["--dbc", str(DEMO_DBC), "--hex", "1A0#E02E828000000000"])
        self.assertEqual(code, dbc.EXIT_OK)

    def test_json_fields(self):
        code, out, _ = run_main(["--dbc", str(DEMO_DBC), "--log", str(DEMO_LOG), "--json"])
        self.assertEqual(code, dbc.EXIT_AMBIGUOUS)
        payload = json.loads(out)
        for key in ("schema_version", "dbc", "frame_count", "ambiguous_count", "frames"):
            self.assertIn(key, payload)
        self.assertEqual(payload["schema_version"], "can-signals.v1")
        self.assertEqual(payload["ambiguous_count"], 1)

    def test_filter_id(self):
        code, out, _ = run_main(["--dbc", str(DEMO_DBC), "--log", str(DEMO_LOG), "--filter-id", "1A0"])
        self.assertEqual(code, dbc.EXIT_OK)
        self.assertIn("ENGINE_STATUS", out)
        self.assertNotIn("GEARBOX", out)

    def test_filter_id_without_matches_exits_six(self):
        code, _, err = run_main(["--dbc", str(DEMO_DBC), "--log", str(DEMO_LOG), "--filter-id", "7FF"])
        self.assertEqual(code, dbc.EXIT_CONFIG)
        self.assertIn("no frames with ID", err)

    def test_output_flag_writes_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.json"
            run_main(["--dbc", str(DEMO_DBC), "--hex", "1A0#E02E828000000000", "--output", str(target)])
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["schema_version"], "can-signals.v1")


class TestBadInput(unittest.TestCase):
    def test_missing_dbc_exits_six(self):
        code, _, err = run_main(["--dbc", "nope.dbc", "--hex", "1A0#00"])
        self.assertEqual(code, dbc.EXIT_CONFIG)
        self.assertIn("DBC not found", err)

    def test_missing_log_exits_six(self):
        code, _, err = run_main(["--dbc", str(DEMO_DBC), "--log", "nope.log"])
        self.assertEqual(code, dbc.EXIT_CONFIG)
        self.assertIn("candump log not found", err)

    def test_file_without_bo_entries_exits_six(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.dbc"
            bad.write_text("VERSION \"\"\n\nBU_: ECU\n", encoding="utf-8")
            code, _, err = run_main(["--dbc", str(bad), "--hex", "1A0#00"])
        self.assertEqual(code, dbc.EXIT_CONFIG)
        self.assertIn("no BO_ message definitions", err)

    def test_unparsable_sg_line_exits_six(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.dbc"
            bad.write_text("BO_ 100 M: 8 ECU\n SG_ broken line\n", encoding="utf-8")
            code, _, err = run_main(["--dbc", str(bad), "--hex", "100#00"])
        self.assertEqual(code, dbc.EXIT_CONFIG)
        self.assertIn("unparsable SG_ line", err)

    def test_no_input_selector_exits_six(self):
        code, _, err = run_main(["--dbc", str(DEMO_DBC)])
        self.assertEqual(code, dbc.EXIT_CONFIG)
        self.assertIn("--log or --hex", err)

    def test_log_without_frames_exits_six(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty.log"
            empty.write_text("# nothing\n", encoding="utf-8")
            code, _, err = run_main(["--dbc", str(DEMO_DBC), "--log", str(empty)])
        self.assertEqual(code, dbc.EXIT_CONFIG)
        self.assertIn("no CAN frames", err)


if __name__ == "__main__":
    unittest.main()