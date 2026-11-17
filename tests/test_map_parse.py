"""Tests for scripts/map_parse.py: GNU ld map parsing, budget gates, growth gate."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("map_parse_tests", REPO / "scripts" / "map_parse.py")
mp = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(mp)

DEMO_MAP = REPO / "examples" / "map" / "demo.map"


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mp.main(argv)
    return code, out.getvalue(), err.getvalue()


class TestSizeValue(unittest.TestCase):
    def test_plain_bytes(self):
        self.assertEqual(mp.parse_size_value("65536"), 65536)

    def test_kilo_and_mega_suffix(self):
        self.assertEqual(mp.parse_size_value("64K"), 64 * 1024)
        self.assertEqual(mp.parse_size_value("1M"), 1024 * 1024)
        self.assertEqual(mp.parse_size_value("20k"), 20 * 1024)

    def test_hexadecimal(self):
        self.assertEqual(mp.parse_size_value("0x10000"), 65536)

    def test_underscores_are_tolerated(self):
        self.assertEqual(mp.parse_size_value("1_000"), 1000)

    def test_garbage_is_a_config_error(self):
        with self.assertRaises(mp.MapError):
            mp.parse_size_value("sixty-four")

    def test_empty_is_a_config_error(self):
        with self.assertRaises(mp.MapError):
            mp.parse_size_value("  ")

    def test_negative_is_rejected(self):
        with self.assertRaises(mp.MapError):
            mp.parse_size_value("-1")


class TestParsing(unittest.TestCase):
    def test_demo_map_is_recognised(self):
        text = DEMO_MAP.read_text(encoding="utf-8")
        self.assertTrue(mp.is_gnu_map(text))

    def test_memory_regions(self):
        lines = DEMO_MAP.read_text(encoding="utf-8").splitlines()
        regions = mp.parse_memory_configuration(lines)
        self.assertEqual(regions["FLASH"]["origin"], 0x08000000)
        self.assertEqual(regions["FLASH"]["length"], 0x00010000)
        self.assertEqual(regions["RAM"]["length"], 0x00005000)
        self.assertNotIn("*default*", regions)

    def test_sections_are_aggregated(self):
        lines = DEMO_MAP.read_text(encoding="utf-8").splitlines()
        sections = mp.parse_sections(lines)
        self.assertEqual(sections[".isr_vector"]["size"], 0x130)
        self.assertEqual(sections[".text"]["size"], 0x9A4)
        self.assertEqual(sections[".rodata"]["size"], 0x64)
        self.assertEqual(sections[".data"]["size"], 0x28)
        self.assertEqual(sections[".bss"]["size"], 0x1D8)

    def test_discarded_sections_do_not_inflate_totals(self):
        """The 'Discarded input sections' list appears before the real map."""
        lines = DEMO_MAP.read_text(encoding="utf-8").splitlines()
        sections = mp.parse_sections(lines)
        self.assertNotIn("*fill*", sections)
        self.assertEqual(sections[".text"]["size"], 0x9A4)

    def test_flash_and_ram_totals_match_hand_calculation(self):
        # flash = isr_vector 0x130 + text 0x9a4 + rodata 0x64 + data 0x28
        self.assertEqual(0x130 + 0x9A4 + 0x64 + 0x28, 0xB60)  # 2912 bytes
        # ram = data 0x28 + bss 0x1d8
        self.assertEqual(0x28 + 0x1D8, 0x200)  # 512 bytes
        payload = mp.build_payload(DEMO_MAP)
        self.assertEqual(payload["flash"]["used"], 2912)
        self.assertEqual(payload["ram"]["used"], 512)

    def test_capacity_and_percent_come_from_memory_configuration(self):
        payload = mp.build_payload(DEMO_MAP)
        self.assertEqual(payload["flash"]["capacity"], 65536)
        self.assertEqual(payload["ram"]["capacity"], 20480)
        self.assertEqual(payload["flash"]["free"], 65536 - 2912)
        self.assertAlmostEqual(payload["flash"]["percent"], 4.44, places=2)

    def test_symbols_are_sorted_descending_and_sized(self):
        payload = mp.build_payload(DEMO_MAP, top=3)
        sizes = [symbol["size"] for symbol in payload["symbols"]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertEqual(payload["symbols"][0]["name"], ".text.printf")
        self.assertEqual(payload["symbols"][0]["size"], 960)

    def test_linker_assignments_are_not_listed_as_symbols(self):
        payload = mp.build_payload(DEMO_MAP, top=50)
        names = [symbol["name"] for symbol in payload["symbols"]]
        for assignment in ("_sdata = .", "_ebss = .", "_sivector = ."):
            self.assertNotIn(assignment, names)
        for name in names:
            self.assertNotIn("build/", name, "a continuation row leaked into the symbol list")

    def test_symbol_count_is_higher_than_the_truncated_list(self):
        payload = mp.build_payload(DEMO_MAP, top=2)
        self.assertEqual(len(payload["symbols"]), 2)
        self.assertGreater(payload["symbol_count"], 2)


class TestGates(unittest.TestCase):
    def test_within_budget_exits_zero(self):
        code, out, _ = run_main(["--map", str(DEMO_MAP), "--max-flash", "64K", "--max-ram", "20K"])
        self.assertEqual(code, mp.EXIT_OK)
        self.assertIn("WITHIN_BUDGET", json.dumps(mp.build_payload(DEMO_MAP, max_flash=65536)))

    def test_flash_over_budget_exits_one(self):
        code, _, _ = run_main(["--map", str(DEMO_MAP), "--max-flash", "2K"])
        self.assertEqual(code, mp.EXIT_BUDGET)

    def test_ram_over_budget_exits_one(self):
        code, _, _ = run_main(["--map", str(DEMO_MAP), "--max-ram", "256"])
        self.assertEqual(code, mp.EXIT_BUDGET)

    def test_failure_names_the_gate_and_the_overrun(self):
        payload = mp.build_payload(DEMO_MAP, max_flash=2048)
        self.assertEqual(payload["status"], "OVER_BUDGET")
        failure = payload["failures"][0]
        self.assertEqual(failure["gate"], "max_flash")
        self.assertEqual(failure["over_by"], 2912 - 2048)

    def test_bad_budget_value_exits_six(self):
        code, _, err = run_main(["--map", str(DEMO_MAP), "--max-flash", "lots"])
        self.assertEqual(code, mp.EXIT_CONFIG)
        self.assertIn("--max-flash", err)


class TestBaseline(unittest.TestCase):
    def test_write_baseline_then_compare_without_growth(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline = Path(tmp) / "baseline.json"
            code, _, _ = run_main(["--map", str(DEMO_MAP), "--write-baseline", str(baseline)])
            self.assertEqual(code, mp.EXIT_OK)
            self.assertTrue(baseline.is_file())
            code, _, _ = run_main(["--map", str(DEMO_MAP), "--baseline", str(baseline)])
            self.assertEqual(code, mp.EXIT_OK, "an unchanged build must not trip the growth gate")

    def test_growth_beyond_limit_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline = Path(tmp) / "baseline.json"
            run_main(["--map", str(DEMO_MAP), "--write-baseline", str(baseline)])
            data = json.loads(baseline.read_text(encoding="utf-8"))
            data["flash"]["used"] = 100  # pretend the baseline was much smaller
            baseline.write_text(json.dumps(data), encoding="utf-8")
            code, _, _ = run_main(["--map", str(DEMO_MAP), "--baseline", str(baseline)])
            self.assertEqual(code, mp.EXIT_BUDGET)

    def test_allowed_growth_can_absorb_the_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline = Path(tmp) / "baseline.json"
            run_main(["--map", str(DEMO_MAP), "--write-baseline", str(baseline)])
            data = json.loads(baseline.read_text(encoding="utf-8"))
            data["flash"]["used"] = 2912 - 10
            baseline.write_text(json.dumps(data), encoding="utf-8")
            code, _, _ = run_main(["--map", str(DEMO_MAP), "--baseline", str(baseline), "--max-growth", "16"])
            self.assertEqual(code, mp.EXIT_OK)

    def test_missing_baseline_is_a_config_error(self):
        code, _, err = run_main(["--map", str(DEMO_MAP), "--baseline", "nope.json"])
        self.assertEqual(code, mp.EXIT_CONFIG)
        self.assertIn("baseline", err)


class TestBadInput(unittest.TestCase):
    def test_missing_map_exits_six(self):
        code, _, err = run_main(["--map", "nope.map"])
        self.assertEqual(code, mp.EXIT_CONFIG)
        self.assertIn("cannot read", err)

    def test_csv_is_not_accepted_as_a_map(self):
        code, _, err = run_main(["--map", str(REPO / "examples" / "control" / "response.csv")])
        self.assertEqual(code, mp.EXIT_CONFIG)
        self.assertIn("does not look like a GNU ld map file", err)

    def test_empty_file_exits_six(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty.map"
            empty.write_text("", encoding="utf-8")
            code, _, err = run_main(["--map", str(empty)])
        self.assertEqual(code, mp.EXIT_CONFIG)
        self.assertIn("empty", err)

    def test_map_without_sections_exits_six(self):
        with tempfile.TemporaryDirectory() as tmp:
            partial = Path(tmp) / "partial.map"
            partial.write_text(
                "Memory Configuration\n\nName Origin Length\nFLASH 0x08000000 0x10000\n\n"
                "Linker script and memory map\n\n",
                encoding="utf-8",
            )
            code, _, err = run_main(["--map", str(partial)])
        self.assertEqual(code, mp.EXIT_CONFIG)
        self.assertIn("no recognisable output sections", err)


class TestOutput(unittest.TestCase):
    def test_json_carries_the_documented_fields(self):
        code, out, _ = run_main(["--map", str(DEMO_MAP), "--json"])
        self.assertEqual(code, mp.EXIT_OK)
        payload = json.loads(out)
        for key in (
            "schema_version",
            "source",
            "flash",
            "ram",
            "regions",
            "sections",
            "symbols",
            "symbol_count",
            "gates",
            "failures",
            "status",
        ):
            self.assertIn(key, payload)
        self.assertEqual(payload["schema_version"], "map-report.v1")
        self.assertEqual(payload["source"]["format"], "gnu-ld-map")

    def test_json_keys_are_sorted(self):
        _, out, _ = run_main(["--map", str(DEMO_MAP), "--json"])
        top_level = list(json.loads(out).keys())
        self.assertEqual(top_level, sorted(top_level))

    def test_text_and_json_agree_on_totals(self):
        _, text_out, _ = run_main(["--map", str(DEMO_MAP)])
        _, json_out, _ = run_main(["--map", str(DEMO_MAP), "--json"])
        payload = json.loads(json_out)
        self.assertIn(str(payload["flash"]["used"]), text_out)
        self.assertIn(str(payload["ram"]["used"]), text_out)

    def test_output_flag_writes_the_same_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "report.json"
            code, out, _ = run_main(["--map", str(DEMO_MAP), "--json", "--output", str(target)])
            self.assertEqual(code, mp.EXIT_OK)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), json.loads(out))


if __name__ == "__main__":
    unittest.main()