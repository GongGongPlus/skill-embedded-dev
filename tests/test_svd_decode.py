"""Tests for scripts/svd_decode.py: CMSIS-SVD parsing, dump decoding, reserved gaps."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("svd_decode_tests", REPO / "scripts" / "svd_decode.py")
svd = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(svd)

DEMO_SVD = REPO / "examples" / "svd" / "demo.svd"
DEMO_DUMP = REPO / "examples" / "svd" / "demo-dump.txt"


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = svd.main(argv)
    return code, out.getvalue(), err.getvalue()


class TestSvdParsing(unittest.TestCase):
    def setUp(self):
        self.model = svd.parse_svd(DEMO_SVD)

    def test_device_name(self):
        self.assertEqual(self.model["device"], "DEMO_STM32F103")

    def test_peripheral_addresses(self):
        rcc = self.model["peripherals"]["RCC"]
        self.assertEqual(rcc["base_address"], 0x40021000)
        self.assertEqual(self.model["peripherals"]["GPIOA"]["base_address"], 0x40010800)

    def test_register_offsets_and_sizes(self):
        registers = self.model["peripherals"]["RCC"]["registers"]
        self.assertEqual(registers["CR"]["address_offset"], 0x00)
        self.assertEqual(registers["APB2ENR"]["address_offset"], 0x18)
        self.assertEqual(registers["CR"]["size"], 32)
        self.assertEqual(registers["CR"]["reset_value"], 0x83)

    def test_fields_use_bitoffset_and_bitwidth(self):
        fields = {field["name"]: field for field in self.model["peripherals"]["RCC"]["registers"]["CR"]["fields"]}
        self.assertEqual(fields["HSION"]["lsb"], 0)
        self.assertEqual(fields["HSION"]["msb"], 0)
        self.assertEqual(fields["HSITRIM"]["lsb"], 3)
        self.assertEqual(fields["HSITRIM"]["msb"], 7)
        self.assertEqual(fields["PLLMUL"]["width"], 4)

    def test_lsb_msb_form_is_also_accepted(self):
        """Some SVDs use <lsb>/<msb> instead of bitOffset/bitWidth."""
        xml = """<?xml version="1.0"?>
        <device><name>LB</name><peripherals><peripheral>
          <name>P</name><baseAddress>0x1000</baseAddress><registers><register>
            <name>R</name><addressOffset>0</addressOffset><size>8</size><fields>
              <field><name>F</name><lsb>2</lsb><msb>5</msb></field>
            </fields>
          </register></registers>
        </peripheral></peripherals></device>"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lb.svd"
            path.write_text(xml, encoding="utf-8")
            model = svd.parse_svd(path)
        field = model["peripherals"]["P"]["registers"]["R"]["fields"][0]
        self.assertEqual(field["lsb"], 2)
        self.assertEqual(field["msb"], 5)
        self.assertEqual(field["width"], 4)

    def test_enumerated_values_are_parsed(self):
        fields = {field["name"]: field for field in self.model["peripherals"]["RCC"]["registers"]["CR"]["fields"]}
        self.assertEqual(fields["HSION"]["enumerated"], {0: "OFF", 1: "ON"})
        self.assertEqual(fields["PLLSRC"]["enumerated"], {0: "HSI_DIV2", 1: "HSE"})

    def test_derived_from_copies_fields(self):
        registers = self.model["peripherals"]["GPIOA"]["registers"]
        self.assertEqual(registers["CRH"]["derived_from"], "CRL")
        crl_fields = [field["name"] for field in registers["CRL"]["fields"]]
        crh_fields = [field["name"] for field in registers["CRH"]["fields"]]
        self.assertEqual(crl_fields, crh_fields)
        self.assertEqual(registers["CRH"]["size"], registers["CRL"]["size"])


class TestValueDecoding(unittest.TestCase):
    def setUp(self):
        self.model = svd.parse_svd(DEMO_SVD)

    def test_field_values_and_enumerated_meanings(self):
        cr = self.model["peripherals"]["RCC"]["registers"]["CR"]
        decoded = svd.decode_value(cr, 0x011D0003)
        fields = {field["name"]: field for field in decoded["fields"]}
        self.assertEqual(fields["HSION"]["value"], 1)
        self.assertEqual(fields["HSION"]["meaning"], "ON")
        self.assertEqual(fields["PLLSRC"]["value"], 1)
        self.assertEqual(fields["PLLSRC"]["meaning"], "HSE")
        self.assertEqual(fields["PLLMUL"]["value"], 0x7)

    def test_undefined_bits_are_reported_as_reserved(self):
        cr = self.model["peripherals"]["RCC"]["registers"]["CR"]
        decoded = svd.decode_value(cr, 0xFFFFFFFF)
        self.assertTrue(decoded["has_undefined_bits"])
        self.assertTrue(decoded["reserved"])
        ranges = {(row["lsb"], row["msb"]) for row in decoded["reserved"]}
        self.assertIn((2, 2), ranges)  # the gap between HSIRDY and HSITRIM
        self.assertIn((8, 15), ranges)

    def test_fully_defined_register_has_no_reserved(self):
        decoded = svd.decode_value(
            {
                "size": 8,
                "fields": [{"name": "ALL", "lsb": 0, "msb": 7, "width": 8, "access": None,
                            "description": None, "enumerated": {}}],
            },
            0xAB,
        )
        self.assertFalse(decoded["has_undefined_bits"])
        self.assertEqual(decoded["reserved"], [])
        self.assertEqual(decoded["fields"][0]["value"], 0xAB)

    def test_register_size_masks_the_value(self):
        decoded = svd.decode_value({"size": 8, "fields": []}, 0x1234)
        self.assertEqual(decoded["raw"], 0x34)


class TestDumpDecoding(unittest.TestCase):
    def setUp(self):
        self.model = svd.parse_svd(DEMO_SVD)

    def test_parses_both_separators(self):
        rows = svd.parse_dump("0x40021000=0x1\n0x40021018 0x2\nnoise\n")
        self.assertEqual(rows, [(0x40021000, 0x1), (0x40021018, 0x2)])

    def test_known_register_is_decoded(self):
        row = svd.decode_dump_line(self.model, 0x40021018, 0x00004005)
        self.assertEqual(row["status"], "decoded")
        self.assertEqual(row["peripheral"], "RCC")
        self.assertEqual(row["register"], "APB2ENR")
        fields = {field["name"]: field for field in row["decoded"]["fields"]}
        self.assertEqual(fields["IOPAEN"]["meaning"], "ENABLED")

    def test_address_inside_peripheral_but_wrong_offset_is_unknown(self):
        row = svd.decode_dump_line(self.model, 0x40021004, 0x0)
        self.assertEqual(row["status"], "unknown")
        self.assertIn("offset", row["reason"])

    def test_address_outside_every_peripheral_is_unknown(self):
        row = svd.decode_dump_line(self.model, 0x50000000, 0xDEADBEEF)
        self.assertEqual(row["status"], "unknown")
        self.assertIn("outside every peripheral", row["reason"])


class TestCli(unittest.TestCase):
    def test_dump_with_unknown_rows_exits_four(self):
        code, out, _ = run_main(["--svd", str(DEMO_SVD), "--dump", str(DEMO_DUMP)])
        self.assertEqual(code, svd.EXIT_AMBIGUOUS)
        self.assertIn("UNKNOWN", out)
        self.assertIn("RCC.CR", out)

    def test_all_known_rows_exit_zero(self):
        code, out, _ = run_main(["--svd", str(DEMO_SVD), "--values", "0x40021000=0x011D0003"])
        self.assertEqual(code, svd.EXIT_OK)
        self.assertIn("PLLMUL", out)

    def test_values_accepts_several_entries(self):
        code, out, _ = run_main(
            ["--svd", str(DEMO_SVD), "--values", "0x40021000=0x1,0x40021018=0x4", "--json"]
        )
        self.assertEqual(code, svd.EXIT_OK)
        payload = json.loads(out)
        self.assertEqual(payload["decoded_count"], 2)

    def test_malformed_values_entry_exits_six(self):
        code, _, err = run_main(["--svd", str(DEMO_SVD), "--values", "junk"])
        self.assertEqual(code, svd.EXIT_CONFIG)
        self.assertIn("cannot parse --values", err)

    def test_find_register(self):
        code, out, _ = run_main(["--svd", str(DEMO_SVD), "--find", "APB2ENR", "--json"])
        self.assertEqual(code, svd.EXIT_OK)
        payload = json.loads(out)
        self.assertGreaterEqual(payload["hit_count"], 1)
        self.assertEqual(payload["hits"][0]["register"], "APB2ENR")

    def test_find_field(self):
        code, out, _ = run_main(["--svd", str(DEMO_SVD), "--find", "pllmul", "--json"])
        self.assertEqual(code, svd.EXIT_OK)
        hits = json.loads(out)["hits"]
        self.assertTrue(any(hit.get("field") == "PLLMUL" for hit in hits))

    def test_find_without_matches_is_ambiguous_not_ok(self):
        code, _, _ = run_main(["--svd", str(DEMO_SVD), "--find", "nosuchthing"])
        self.assertEqual(code, svd.EXIT_AMBIGUOUS)

    def test_json_fields(self):
        code, out, _ = run_main(["--svd", str(DEMO_SVD), "--dump", str(DEMO_DUMP), "--json"])
        self.assertEqual(code, svd.EXIT_AMBIGUOUS)
        payload = json.loads(out)
        for key in ("schema_version", "svd", "device", "results", "decoded_count", "unknown_count"):
            self.assertIn(key, payload)
        self.assertEqual(payload["schema_version"], "svd-decode.v1")
        decoded = [row for row in payload["results"] if row["status"] == "decoded"][0]
        self.assertIn("decoded", decoded)
        self.assertIn("fields", decoded["decoded"])
        self.assertIn("reserved", decoded["decoded"])

    def test_output_flag_writes_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.json"
            code, _, _ = run_main(["--svd", str(DEMO_SVD), "--values", "0x40021000=0x1", "--output", str(target)])
            self.assertEqual(code, svd.EXIT_OK)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["schema_version"], "svd-decode.v1")


class TestBadInput(unittest.TestCase):
    def test_missing_svd_exits_six(self):
        code, _, err = run_main(["--svd", "nope.svd", "--values", "0x0=0x1"])
        self.assertEqual(code, svd.EXIT_CONFIG)
        self.assertIn("SVD not found", err)

    def test_invalid_xml_exits_six(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.svd"
            bad.write_text("<device><name>unclosed", encoding="utf-8")
            code, _, err = run_main(["--svd", str(bad), "--values", "0x0=0x1"])
        self.assertEqual(code, svd.EXIT_CONFIG)
        self.assertIn("not valid XML", err)

    def test_wrong_root_element_exits_six(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "root.svd"
            bad.write_text('<?xml version="1.0"?><peripherals/>', encoding="utf-8")
            code, _, err = run_main(["--svd", str(bad), "--values", "0x0=0x1"])
        self.assertEqual(code, svd.EXIT_CONFIG)
        self.assertIn("expected <device>", err)

    def test_no_peripherals_exits_six(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "empty.svd"
            bad.write_text('<?xml version="1.0"?><device><name>E</name><peripherals/></device>', encoding="utf-8")
            code, _, err = run_main(["--svd", str(bad), "--values", "0x0=0x1"])
        self.assertEqual(code, svd.EXIT_CONFIG)
        self.assertIn("no usable peripheral", err)

    def test_dump_file_without_rows_exits_six(self):
        with tempfile.TemporaryDirectory() as tmp:
            dump = Path(tmp) / "empty.txt"
            dump.write_text("nothing here\n", encoding="utf-8")
            code, _, err = run_main(["--svd", str(DEMO_SVD), "--dump", str(dump)])
        self.assertEqual(code, svd.EXIT_CONFIG)
        self.assertIn("no ADDRESS VALUE rows", err)

    def test_missing_dump_file_exits_six(self):
        code, _, err = run_main(["--svd", str(DEMO_SVD), "--dump", "nope.txt"])
        self.assertEqual(code, svd.EXIT_CONFIG)
        self.assertIn("dump file not found", err)

    def test_no_input_selector_exits_six(self):
        code, _, err = run_main(["--svd", str(DEMO_SVD)])
        self.assertEqual(code, svd.EXIT_CONFIG)
        self.assertIn("--dump, --values or --find", err)

    def test_unknown_peripheral_filter_exits_six(self):
        code, _, err = run_main(["--svd", str(DEMO_SVD), "--values", "0x40021000=0x1", "--peripheral", "NOPE"])
        self.assertEqual(code, svd.EXIT_CONFIG)
        self.assertIn("not in the SVD", err)


if __name__ == "__main__":
    unittest.main()