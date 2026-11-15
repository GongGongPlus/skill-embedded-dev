"""Tests for scripts/install.py: copies must be verifiable, overwrites explicit."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("install_script", REPO / "scripts" / "install.py")
installer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(installer)


class TestVerify(unittest.TestCase):
    def test_empty_directory_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            errors = installer.verify_install(Path(tmp))
        self.assertTrue(errors)
        self.assertTrue(any("SKILL.md" in error for error in errors))

    def test_bad_frontmatter_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            (dest / "SKILL.md").write_text("---\nname: something-else\n---\n", encoding="utf-8")
            (dest / "references").mkdir()
            (dest / "references" / "fault-debug.md").write_text("# x", encoding="utf-8")
            (dest / "scripts").mkdir()
            for name in ("embedded_dev.py", "install.py"):
                (dest / "scripts" / name).write_text("# placeholder", encoding="utf-8")
            errors = installer.verify_install(dest)
        self.assertTrue(any("frontmatter" in error for error in errors))


class TestInstall(unittest.TestCase):
    def test_install_to_temp_then_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "embedded-dev"
            with contextlib.redirect_stdout(io.StringIO()):
                code = installer.install("dsh", dest, force=False, dry_run=False, as_json=False)
            self.assertEqual(code, 0)
            self.assertEqual(installer.verify_install(dest), [])
            self.assertTrue((dest / "scripts" / "embedded_dev.py").is_file())
            self.assertTrue((dest / "VERSION").is_file())

    def test_refuses_to_overwrite_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "embedded-dev"
            dest.mkdir()
            (dest / "keep.txt").write_text("existing", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                code = installer.install("dsh", dest, force=False, dry_run=False, as_json=False)
            self.assertEqual(code, 2)
            self.assertTrue((dest / "keep.txt").is_file())

    def test_force_replaces_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "embedded-dev"
            dest.mkdir()
            (dest / "stale.txt").write_text("old", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                code = installer.install("dsh", dest, force=True, dry_run=False, as_json=False)
            self.assertEqual(code, 0)
            self.assertFalse((dest / "stale.txt").exists())
            self.assertEqual(installer.verify_install(dest), [])

    def test_dry_run_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "embedded-dev"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = installer.install("dsh", dest, force=False, dry_run=True, as_json=True)
            self.assertEqual(code, 0)
            self.assertFalse(dest.exists())
            payload = json.loads(out.getvalue())
            self.assertTrue(payload["dry_run"])


class TestCli(unittest.TestCase):
    def test_list_json(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = installer.main(["--list", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertIn("hosts", payload)
        self.assertGreaterEqual(len(payload["hosts"]), 5)

    def test_verify_only_on_missing_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stderr(io.StringIO()):
                code = installer.main(["--dest", str(Path(tmp) / "nope"), "--verify-only"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
