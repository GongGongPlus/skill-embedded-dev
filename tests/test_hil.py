"""Offline tests for the hil subcommand: exit-code contract, diagnostic report, stalled detection."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, REPO / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ed = load_module("embedded_dev_hil_tests", "scripts/embedded_dev.py")

FAKE_OK = [{"command": "fake", "exit_code": 0, "stdout": "", "stderr": "", "skipped": False}]


class HilTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.image = self.tmp / "firmware.elf"
        self.image.write_bytes(b"\x7fELF")
        self.mock = self.tmp / "serial.txt"
        self.report = self.tmp / "diagnostic_report.json"

    def argv(self, *extra: str, mock_text: str | None = "BOOT OK\nTEST_PASS_TOKEN\n") -> list[str]:
        if mock_text is not None:
            self.mock.write_text(mock_text, encoding="utf-8")
        base = [
            "hil",
            "--project",
            str(self.project),
            "--image",
            str(self.image),
            "--expect",
            "TEST_PASS_TOKEN",
            "--build-backend",
            "cmake",
            "--flash-backend",
            "openocd",
            "--openocd-interface",
            "interface/cmsis-dap.cfg",
            "--openocd-target",
            "target/stm32f1x.cfg",
            "--mock-file",
            str(self.mock),
            "--report-out",
            str(self.report),
        ]
        return base + list(extra)

    def run_hil(self, *extra: str, mock_text: str | None = "BOOT OK\nTEST_PASS_TOKEN\n", run_commands=None):
        argv = self.argv(*extra, mock_text=mock_text)
        patcher = mock.patch.object(ed, "run_commands", run_commands or (lambda *a, **k: list(FAKE_OK)))
        with patcher, redirect_stdout(__import__("io").StringIO()):
            return ed.main(argv)

    def read_report(self) -> dict:
        return json.loads(self.report.read_text(encoding="utf-8"))


class TestHilPlan(HilTestBase):
    def test_dry_run_prints_plan_and_exits_zero(self):
        argv = self.argv("--dry-run", mock_text=None)
        buffer = __import__("io").StringIO()
        with redirect_stdout(buffer):
            exit_code = ed.main(argv)
        self.assertEqual(exit_code, ed.EXIT_OK)
        self.assertIn("hil dry-run plan", buffer.getvalue())
        self.assertIn("openocd", buffer.getvalue())

    def test_missing_image_is_a_config_error(self):
        buffer = __import__("io").StringIO()
        with redirect_stdout(buffer):
            exit_code = ed.main(["hil", "--project", str(self.project), "--build-backend", "cmake", "--flash-backend", "openocd", "--openocd-interface", "i", "--openocd-target", "t"])
        self.assertEqual(exit_code, ed.EXIT_CONFIG)

    def test_real_run_without_expect_is_a_config_error(self):
        argv = [
            "hil",
            "--project",
            str(self.project),
            "--image",
            str(self.image),
            "--build-backend",
            "cmake",
            "--flash-backend",
            "openocd",
            "--openocd-interface",
            "interface/cmsis-dap.cfg",
            "--openocd-target",
            "target/stm32f1x.cfg",
        ]
        with redirect_stdout(__import__("io").StringIO()):
            exit_code = ed.main(argv)
        self.assertEqual(exit_code, ed.EXIT_CONFIG)


class TestHilOutcomes(HilTestBase):
    def test_token_means_test_passed(self):
        exit_code = self.run_hil()
        self.assertEqual(exit_code, ed.EXIT_OK)
        report = self.read_report()
        self.assertEqual(report["status"], "TEST_PASSED")
        for key in ed.DIAGNOSTIC_REQUIRED:
            self.assertIn(key, report)
        self.assertTrue(report["ai_repair_prompt"])
        self.assertIn("git", report["source_context"])

    def test_missing_token_means_test_failed(self):
        exit_code = self.run_hil(mock_text="BOOT OK\nno token here\n")
        self.assertEqual(exit_code, ed.EXIT_DEVICE)
        self.assertEqual(self.read_report()["status"], "TEST_FAILED")

    def test_cfsr_fault_wins_over_the_token(self):
        exit_code = self.run_hil("--cfsr", "0x02000000", mock_text="HardFault handler\nTEST_PASS_TOKEN\n")
        self.assertEqual(exit_code, ed.EXIT_FAULT)
        report = self.read_report()
        self.assertEqual(report["status"], "HARD_FAULT")
        names = [bit["name"] for bit in report["fault_diagnostics"]["cfsr_bits"]]
        self.assertIn("DIVBYZERO", names)

    def test_same_signature_and_clean_source_is_stalled(self):
        stable = {"clean": True, "head": "abc1234"}
        with mock.patch.object(ed, "git_source_state", lambda project: dict(stable)):
            first = self.run_hil(mock_text="BOOT OK\nno token\n")
            self.assertEqual(first, ed.EXIT_DEVICE)
            second = self.run_hil("--previous", str(self.report), mock_text="BOOT OK\nno token\n")
        self.assertEqual(second, ed.EXIT_STALLED)
        report = self.read_report()
        self.assertEqual(report["status"], "STALLED")
        self.assertTrue(report["repeated_failure"])
        self.assertTrue(report["source_unchanged"])

    def test_changed_source_is_not_stalled(self):
        self.run_hil(mock_text="BOOT OK\nno token\n")
        moving = {"clean": True, "head": "def5678"}
        with mock.patch.object(ed, "git_source_state", lambda project: dict(moving)):
            second = self.run_hil("--previous", str(self.report), mock_text="BOOT OK\nno token\n")
        self.assertEqual(second, ed.EXIT_DEVICE)
        self.assertEqual(self.read_report()["status"], "TEST_FAILED")


if __name__ == "__main__":
    unittest.main()
