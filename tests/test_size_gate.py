"""Tests for scripts/size_gate.py: size -A parsing, budgets and baseline growth."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("size_gate_tests", REPO / "scripts" / "size_gate.py")
sg = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(sg)

DEMO_SIZE = REPO / "examples" / "size" / "demo-size.txt"


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = sg.main(argv)
    return code, out.getvalue(), err.getvalue()


class TestSizeValue(unittest.TestCase):
    def test_suffixes_and_hex(self):
        self.assertEqual(sg.parse_size_value("64K"), 65536)
        self.assertEqual(sg.parse_size_value("1M"), 1048576)
        self.assertEqual(sg.parse_size_value("0x10000"), 65536)
        self.assertEqual(sg.parse_size_value("1024"), 1024)

    def test_garbage_is_a_config_error(self):
        with self.assertRaises(sg.SizeError):
            sg.parse_size_value("huge")

    def test_negative_is_rejected(self):
        with self.assertRaises(sg.SizeError):
            sg.parse_size_value("-5")


class TestParsing(unittest.TestCase):
    def test_demo_transcript_sections(self):
        sections = sg.parse_size_output(DEMO_SIZE.read_text(encoding="utf-8"))
        self.assertEqual(sections[".text"], 2468)
        self.assertEqual(sections[".isr_vector"], 304)
        self.assertEqual(sections[".data"], 40)
        self.assertEqual(sections[".bss"], 472)
        self.assertEqual(sections[".debug_info"], 12345)

    def test_buckets_follow_gnu_size_conventions(self):
        sections = sg.parse_size_output(DEMO_SIZE.read_text(encoding="utf-8"))
        totals = sg.summarise(sections)
        # text = isr_vector 304 + text 2468 + rodata 100 + ARM.exidx 8
        self.assertEqual(totals["text"], 2880)
        self.assertEqual(totals["data"], 40)
        # bss = bss 472 + heap 1024 + stack 2048
        self.assertEqual(totals["bss"], 3544)
        self.assertEqual(totals["ram"], 3584)
        self.assertEqual(totals["flash"], 2920)

    def test_debug_sections_do_not_count_towards_flash(self):
        sections = sg.parse_size_output(DEMO_SIZE.read_text(encoding="utf-8"))
        totals = sg.summarise(sections)
        self.assertNotIn(12345, (totals["text"], totals["data"], totals["bss"]))

    def test_unrelated_text_is_rejected(self):
        with self.assertRaises(sg.SizeError):
            sg.parse_size_output("just some words\nnothing tabular\n")

    def test_rows_without_header_or_total_are_rejected(self):
        with self.assertRaises(sg.SizeError):
            sg.parse_size_output(".text 100 0\n.bss 20 0\n")


class TestBudgets(unittest.TestCase):
    def test_within_budget_exits_zero(self):
        code, out, _ = run_main(["--size-output-file", str(DEMO_SIZE), "--max-text", "8K", "--max-bss", "8K"])
        self.assertEqual(code, sg.EXIT_OK)
        self.assertIn("2880", out)

    def test_text_over_budget_exits_one(self):
        code, _, _ = run_main(["--size-output-file", str(DEMO_SIZE), "--max-text", "2K"])
        self.assertEqual(code, sg.EXIT_BUDGET)

    def test_bss_over_budget_exits_one(self):
        code, _, _ = run_main(["--size-output-file", str(DEMO_SIZE), "--max-bss", "1K"])
        self.assertEqual(code, sg.EXIT_BUDGET)

    def test_data_over_budget_exits_one(self):
        code, _, _ = run_main(["--size-output-file", str(DEMO_SIZE), "--max-data", "16"])
        self.assertEqual(code, sg.EXIT_BUDGET)

    def test_failure_names_the_gate_and_overrun(self):
        code, out, _ = run_main(["--size-output-file", str(DEMO_SIZE), "--max-text", "2K", "--json"])
        payload = json.loads(out)
        self.assertEqual(code, sg.EXIT_BUDGET)
        self.assertEqual(payload["status"], "OVER_BUDGET")
        self.assertEqual(payload["failures"][0]["gate"], "max_text")
        self.assertEqual(payload["failures"][0]["over_by"], 2880 - 2048)

    def test_bad_budget_is_a_config_error(self):
        code, _, err = run_main(["--size-output-file", str(DEMO_SIZE), "--max-text", "lots"])
        self.assertEqual(code, sg.EXIT_CONFIG)
        self.assertIn("--max-text", err)


class TestBaseline(unittest.TestCase):
    def test_write_then_compare_without_growth(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline = Path(tmp) / "base.json"
            code, _, _ = run_main(["--size-output-file", str(DEMO_SIZE), "--write-baseline", str(baseline)])
            self.assertEqual(code, sg.EXIT_OK)
            code, _, _ = run_main(["--size-output-file", str(DEMO_SIZE), "--baseline", str(baseline)])
            self.assertEqual(code, sg.EXIT_OK)

    def test_growth_beyond_limit_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline = Path(tmp) / "base.json"
            run_main(["--size-output-file", str(DEMO_SIZE), "--write-baseline", str(baseline)])
            data = json.loads(baseline.read_text(encoding="utf-8"))
            data["totals"]["text"] = 100
            baseline.write_text(json.dumps(data), encoding="utf-8")
            code, _, _ = run_main(["--size-output-file", str(DEMO_SIZE), "--baseline", str(baseline)])
            self.assertEqual(code, sg.EXIT_BUDGET)

    def test_allowed_growth_absorbs_the_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline = Path(tmp) / "base.json"
            run_main(["--size-output-file", str(DEMO_SIZE), "--write-baseline", str(baseline)])
            data = json.loads(baseline.read_text(encoding="utf-8"))
            data["totals"]["text"] = 2880 - 10
            baseline.write_text(json.dumps(data), encoding="utf-8")
            code, _, _ = run_main(
                ["--size-output-file", str(DEMO_SIZE), "--baseline", str(baseline), "--max-growth", "16"]
            )
            self.assertEqual(code, sg.EXIT_OK)

    def test_missing_baseline_is_a_config_error(self):
        code, _, err = run_main(["--size-output-file", str(DEMO_SIZE), "--baseline", "nope.json"])
        self.assertEqual(code, sg.EXIT_CONFIG)
        self.assertIn("baseline not found", err)

    def test_invalid_baseline_json_is_a_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            code, _, err = run_main(["--size-output-file", str(DEMO_SIZE), "--baseline", str(bad)])
        self.assertEqual(code, sg.EXIT_CONFIG)
        self.assertIn("not valid JSON", err)


class TestBadInput(unittest.TestCase):
    def test_no_selector_exits_six(self):
        code, _, err = run_main([])
        self.assertEqual(code, sg.EXIT_CONFIG)
        self.assertIn("--elf", err)

    def test_missing_transcript_exits_six(self):
        code, _, err = run_main(["--size-output-file", "nope.txt"])
        self.assertEqual(code, sg.EXIT_CONFIG)
        self.assertIn("size output file not found", err)

    def test_missing_elf_exits_six(self):
        code, _, err = run_main(["--elf", "nope.elf"])
        self.assertEqual(code, sg.EXIT_CONFIG)
        self.assertIn("ELF not found", err)


class TestOutput(unittest.TestCase):
    def test_json_fields(self):
        code, out, _ = run_main(["--size-output-file", str(DEMO_SIZE), "--json"])
        self.assertEqual(code, sg.EXIT_OK)
        payload = json.loads(out)
        for key in ("schema_version", "source", "sections", "totals", "gates", "growth", "failures", "status"):
            self.assertIn(key, payload)
        self.assertEqual(payload["schema_version"], "size-report.v1")
        self.assertEqual(payload["totals"]["text"], 2880)

    def test_text_and_json_agree(self):
        _, text_out, _ = run_main(["--size-output-file", str(DEMO_SIZE)])
        _, json_out, _ = run_main(["--size-output-file", str(DEMO_SIZE), "--json"])
        totals = json.loads(json_out)["totals"]
        self.assertIn(str(totals["text"]), text_out)
        self.assertIn(str(totals["ram"]), text_out)

    def test_output_flag_writes_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "size.json"
            run_main(["--size-output-file", str(DEMO_SIZE), "--json", "--output", str(target)])
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["schema_version"], "size-report.v1")


if __name__ == "__main__":
    unittest.main()