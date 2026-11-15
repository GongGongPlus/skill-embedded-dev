"""Offline tests for project_memory.py: archive detection, evidence levels and lifecycle.

All lifecycle tests run in a temporary directory. Nothing here claims hardware
acceptance: the tool records provenance, it does not verify it.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples" / "board"
SCRIPT = REPO / "scripts" / "project_memory.py"


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, REPO / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


pm = load_module("project_memory_tests", "scripts/project_memory.py")


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )


class TestDetection(unittest.TestCase):
    def test_bundled_board_example(self):
        detection = pm.detect_project(EXAMPLES)
        self.assertEqual(detection["schema_version"], "project-archive.v1")
        self.assertEqual(detection["chips"], ["STM32F103C8Tx"])
        self.assertFalse(detection["ambiguous"])
        kinds = {marker["kind"] for marker in detection["markers"]}
        self.assertIn("cubemx", kinds)

    def test_empty_directory_has_no_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            detection = pm.detect_project(Path(tmp))
            self.assertEqual(detection["markers"], [])
            self.assertEqual(detection["chips"], [])
            self.assertFalse(detection["ambiguous"])

    def test_detection_is_a_suggestion_not_a_fact(self):
        detection = pm.detect_project(EXAMPLES)
        limits = " ".join(detection["limits"])
        self.assertIn("suggestion", limits)
        self.assertIn("not a flash", limits)


class TestEvidenceLevels(unittest.TestCase):
    def test_chinese_levels(self):
        self.assertEqual(pm.normalize_evidence_level("已确认"), "已确认")
        self.assertEqual(pm.normalize_evidence_level("待验证"), "待验证")

    def test_english_aliases(self):
        self.assertEqual(pm.normalize_evidence_level("verified"), "已确认")
        self.assertEqual(pm.normalize_evidence_level("inferred"), "工程推论")

    def test_unknown_level_is_rejected(self):
        with self.assertRaises(pm.MemoryError_):
            pm.normalize_evidence_level("definitely-true")


class TestLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mem-test-")
        shutil.copyfile(EXAMPLES / "demo.ioc", Path(self.tmp) / "demo.ioc")
        self.project = self.tmp

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_init_creates_archive_and_state(self):
        result = run_cli("init", "--project", self.project, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["initialized"])
        self.assertTrue((Path(self.project) / ".embedded-dev" / "project.json").is_file())
        self.assertTrue((Path(self.project) / ".embedded-dev" / "knowledge" / "state.json").is_file())

    def test_confirmed_memory_requires_a_source(self):
        self.assertEqual(run_cli("init", "--project", self.project).returncode, 0)
        result = run_cli(
            "remember",
            "--project",
            self.project,
            "--title",
            "uart pins",
            "--content",
            "PA9=TX, PA10=RX",
            "--evidence-level",
            "已确认",
        )
        self.assertEqual(result.returncode, 4)
        self.assertIn("requires --source", result.stderr)

    def test_remember_search_list_forget(self):
        self.assertEqual(run_cli("init", "--project", self.project).returncode, 0)
        remembered = run_cli(
            "remember",
            "--project",
            self.project,
            "--title",
            "uart pins",
            "--content",
            "PA9=TX, PA10=RX",
            "--evidence-level",
            "已确认",
            "--source",
            "demo.ioc",
            "--tags",
            "uart,pins",
            "--json",
        )
        self.assertEqual(remembered.returncode, 0, remembered.stderr)
        entry = json.loads(remembered.stdout)["entry"]
        self.assertEqual(entry["evidence_level"], "已确认")

        found = run_cli("search", "--project", self.project, "--query", "UART", "--json")
        self.assertEqual(found.returncode, 0)
        self.assertEqual(json.loads(found.stdout)["count"], 1)

        listed = run_cli("list", "--project", self.project, "--json")
        self.assertEqual(json.loads(listed.stdout)["total"], 1)

        forgotten = run_cli("forget", "--project", self.project, "--title", "uart pins", "--json")
        self.assertEqual(forgotten.returncode, 0, forgotten.stderr)
        after = run_cli("list", "--project", self.project, "--json")
        self.assertEqual(json.loads(after.stdout)["total"], 0)

    def test_tombstone_blocks_recreating_a_forgotten_title(self):
        self.assertEqual(run_cli("init", "--project", self.project).returncode, 0)
        self.assertEqual(
            run_cli(
                "remember",
                "--project",
                self.project,
                "--title",
                "old fact",
                "--content",
                "x",
                "--evidence-level",
                "待验证",
            ).returncode,
            0,
        )
        self.assertEqual(run_cli("forget", "--project", self.project, "--title", "old fact").returncode, 0)
        blocked = run_cli(
            "remember",
            "--project",
            self.project,
            "--title",
            "old fact",
            "--content",
            "y",
            "--evidence-level",
            "待验证",
        )
        self.assertEqual(blocked.returncode, 4)
        recreated = run_cli(
            "remember",
            "--project",
            self.project,
            "--title",
            "old fact",
            "--content",
            "y",
            "--evidence-level",
            "待验证",
            "--allow-recreate",
        )
        self.assertEqual(recreated.returncode, 0, recreated.stderr)

    def test_expected_version_conflict_is_refused(self):
        self.assertEqual(run_cli("init", "--project", self.project).returncode, 0)
        stale = run_cli(
            "remember",
            "--project",
            self.project,
            "--title",
            "stale write",
            "--content",
            "x",
            "--evidence-level",
            "待验证",
            "--expected-version",
            "99",
        )
        self.assertEqual(stale.returncode, 4)

    def test_missing_archive_fails_loudly(self):
        result = run_cli("list", "--project", self.project)
        self.assertEqual(result.returncode, 6)
        self.assertIn("run 'memory init", result.stderr)


if __name__ == "__main__":
    unittest.main()
