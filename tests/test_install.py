"""Tests for scripts/install.py: copies must be verifiable, overwrites explicit."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
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
    def test_verify_reports_missing_runtime_entries_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            (dest / "SKILL.md").write_text("---\nname: embedded-dev\n---\n", encoding="utf-8")
            (dest / "references").mkdir()
            (dest / "references" / "fault-debug.md").write_text("# x", encoding="utf-8")
            (dest / "scripts").mkdir()
            (dest / "scripts" / "embedded_dev.py").write_text("# placeholder", encoding="utf-8")
            errors = installer.verify_install(dest)
        joined = " ".join(errors)
        self.assertIn("scripts/install.py", joined)
        self.assertIn("VERSION", joined)

    def test_install_to_temp_then_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "embedded-dev"
            with contextlib.redirect_stdout(io.StringIO()):
                code = installer.install("dsh", dest, force=False, dry_run=False, as_json=False)
            self.assertEqual(code, 0)
            self.assertEqual(installer.verify_install(dest), [])
            self.assertTrue((dest / "scripts" / "embedded_dev.py").is_file())
            self.assertTrue((dest / "VERSION").is_file())

    def test_install_ships_the_bundled_capture_fixtures(self):
        """A blanket "*.log" skip used to ship a skill whose own examples could not run.

        The candump fixtures under examples/ are walkthrough inputs, not build
        noise, so they must survive the copy and be named by verify_install.
        """
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "embedded-dev"
            with contextlib.redirect_stdout(io.StringIO()):
                installer.install("dsh", dest, force=False, dry_run=False, as_json=False)
            for relative in installer.CAPTURE_FIXTURES:
                with self.subTest(fixture=relative):
                    self.assertTrue((dest / relative).is_file(), f"{relative} was dropped by the installer")
            # .pyc and cache dirs must still be excluded.
            self.assertEqual(list(dest.rglob("*.pyc")), [])
            self.assertEqual([p for p in dest.rglob("__pycache__")], [])

    def test_verify_flags_a_missing_capture_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "embedded-dev"
            with contextlib.redirect_stdout(io.StringIO()):
                installer.install("dsh", dest, force=False, dry_run=False, as_json=False)
            (dest / installer.CAPTURE_FIXTURES[0]).unlink()
            errors = installer.verify_install(dest)
        self.assertTrue(any(installer.CAPTURE_FIXTURES[0] in error for error in errors), errors)

    def test_every_root_file_the_readme_links_is_shipped(self):
        """README links THIRD_PARTY_NOTICES.md; the installer did not ship it.

        A dead link in an installed skill is the same class of defect as a stale
        subcommand count: the copy claims something it cannot back up. Derive the
        requirement from the README links instead of maintaining a second list.
        """
        import re

        text = (REPO / "README.md").read_text(encoding="utf-8")
        linked_root_files = set()
        for target in re.findall(r"\]\(([^)]+)\)", text):
            path_part = target.split("#", 1)[0].strip()
            if not path_part or path_part.startswith(("http://", "https://", "mailto:")):
                continue
            # Only repo-root entries are the installer's responsibility; files
            # inside a directory entry (references/, scripts/, examples/) ride along.
            if "/" in path_part or "\\" in path_part:
                continue
            linked_root_files.add(path_part)

        self.assertTrue(linked_root_files, "README has no local root links to check")
        for name in sorted(linked_root_files):
            with self.subTest(file=name):
                self.assertTrue((REPO / name).exists(), f"README links {name}, which does not exist")
                self.assertIn(name, installer.RUNTIME_ENTRIES, f"README links {name} but the installer never copies it")

    def test_refuses_to_overwrite_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "embedded-dev"
            dest.mkdir()
            (dest / "keep.txt").write_text("existing", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                code = installer.install("dsh", dest, force=False, dry_run=False, as_json=False)
            self.assertEqual(code, 2)
            self.assertTrue((dest / "keep.txt").is_file())

    def test_force_replaces_runtime_entries_and_keeps_foreign_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "embedded-dev"
            dest.mkdir()
            (dest / "foreign.txt").write_text("keep me", encoding="utf-8")
            (dest / "references").mkdir()
            (dest / "references" / "stale.md").write_text("# stale", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                code = installer.install("dsh", dest, force=True, dry_run=False, as_json=False)
            self.assertEqual(code, 0)
            self.assertTrue((dest / "foreign.txt").is_file(), "unrelated files must survive --force")
            self.assertFalse((dest / "references" / "stale.md").exists(), "stale runtime entries must be replaced")
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

    def test_list_reports_verification_state_not_just_existence(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            installer.main(["--list", "--json"])
        payload = json.loads(out.getvalue())
        for row in payload["hosts"]:
            self.assertIn("valid", row)
            self.assertIn("version", row)
            self.assertIn("version_matches_source", row)
            if row["installed"]:
                self.assertEqual(row["valid"], not row["errors"], row)
            else:
                self.assertFalse(row["valid"], row)

    def test_verify_only_on_missing_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stderr(io.StringIO()):
                code = installer.main(["--dest", str(Path(tmp) / "nope"), "--verify-only"])
        self.assertEqual(code, 1)

    def test_verify_targets_treats_absent_hosts_as_not_installed(self):
        """`--verify-only --target all` must not fail just because a host is unused."""
        with tempfile.TemporaryDirectory() as tmp:
            dsh_root = Path(tmp) / "dsh-skills"
            dsh_root.mkdir()
            codex_root = Path(tmp) / "codex-skills"
            codex_root.mkdir()  # root exists, but no embedded-dev inside it
            with contextlib.redirect_stdout(io.StringIO()):
                installer.install("dsh", dsh_root / "embedded-dev", force=False, dry_run=False, as_json=False)
            original = installer.HOSTS
            installer.HOSTS = {
                "dsh": (lambda p=dsh_root: p),
                "codex": (lambda c=codex_root: c),
            }
            try:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = installer.verify_targets("all", as_json=True)
            finally:
                installer.HOSTS = original
        payload = json.loads(out.getvalue())
        self.assertEqual(code, installer.EXIT_OK, "an uninstalled host is not a broken install")
        self.assertEqual(payload["not_installed"], ["codex"])
        self.assertTrue(payload["valid"])


class TestMultiHost(unittest.TestCase):
    def test_resolve_targets_accepts_a_comma_list(self):
        self.assertEqual(installer.resolve_targets("dsh,codex"), ["dsh", "codex"])
        self.assertEqual(installer.resolve_targets(" dsh , codex "), ["dsh", "codex"])

    def test_resolve_targets_rejects_unknown_host(self):
        with self.assertRaises(KeyError):
            installer.resolve_targets("dsh,nope")

    def test_resolve_targets_all_only_lists_existing_roots(self):
        hosts = installer.resolve_targets("all")
        self.assertTrue(hosts)
        for host in hosts:
            self.assertTrue(installer.host_root(host).exists(), host)

    def test_install_many_reaches_every_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = {}
            for host in ("dsh", "codex"):
                root = Path(tmp) / host
                root.mkdir()
                roots[host] = root
            original = installer.HOSTS
            installer.HOSTS = {host: (lambda r=root: r) for host, root in roots.items()}
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    code = installer.install_many("dsh,codex", force=False, dry_run=False, as_json=False)
            finally:
                installer.HOSTS = original
            self.assertEqual(code, 0)
            for host in ("dsh", "codex"):
                target = roots[host] / installer.SKILL_NAME
                self.assertEqual(installer.verify_install(target), [], host)

    def test_same_skill_name_and_entries_for_every_host(self):
        for host in installer.HOSTS:
            self.assertEqual((installer.host_root(host) / installer.SKILL_NAME).name, "embedded-dev")
        self.assertIn("SKILL.md", installer.RUNTIME_ENTRIES)
        self.assertIn("references", installer.RUNTIME_ENTRIES)
        self.assertIn("scripts", installer.RUNTIME_ENTRIES)

    def test_installing_onto_the_source_checkout_is_refused(self):
        with contextlib.redirect_stdout(io.StringIO()):
            code = installer.install("dsh", installer.REPO_ROOT, force=True, dry_run=False, as_json=False)
        self.assertEqual(code, 0)
        self.assertTrue(installer.REPO_ROOT.joinpath("SKILL.md").is_file(), "source must survive")
        self.assertTrue(installer.REPO_ROOT.joinpath("scripts", "embedded_dev.py").is_file())

    def test_source_checkout_is_reported_as_skipped(self):
        result = installer.install_one("dsh", installer.REPO_ROOT, force=True, dry_run=False)
        self.assertTrue(result.get("skipped"))
        self.assertFalse(result["errors"])


class TestNonAsciiPath(unittest.TestCase):
    """A skill root may live under a non-ASCII directory (a Chinese Desktop folder).

    Regression guard: `install.py` used to print JSON in the console codepage, so
    a caller decoding stdout as UTF-8 crashed with UnicodeDecodeError and
    subprocess reported `stdout=None`. The tool must emit UTF-8 regardless of the
    path characters or the Windows console codepage.
    """

    def test_list_json_is_utf8_decodable_from_a_non_ascii_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "嵌入式-skills"
            root.mkdir()
            original = installer.HOSTS
            installer.HOSTS = {"dsh": (lambda r=root: r)}
            try:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = installer.main(["--list", "--json"])
            finally:
                installer.HOSTS = original
        self.assertEqual(code, installer.EXIT_OK)
        payload = json.loads(out.getvalue())
        self.assertIn("嵌入式-skills", payload["hosts"][0]["root"])

    def test_subprocess_captures_stdout_under_a_non_ascii_cwd(self):
        """The exact failure mode: an outer process reading our stdout as UTF-8."""
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "嵌入式项目"
            workdir.mkdir()
            for entry in ("scripts", "SKILL.md", "VERSION"):
                source = installer.REPO_ROOT / entry
                target = workdir / entry
                if source.is_dir():
                    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
                else:
                    shutil.copy2(source, target)
            completed = subprocess.run(
                [sys.executable, "scripts/install.py", "--list", "--json"],
                cwd=workdir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertIsNotNone(completed.stdout, f"stdout was lost: {completed.stderr[:300]}")
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["version"], (installer.REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip())


if __name__ == "__main__":
    unittest.main()
