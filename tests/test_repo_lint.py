"""Repository lint: metadata, docs, cross-links, installer and CLI surface.

These tests do not touch hardware. They exist so the repository cannot drift:
version numbers, changelog entries, README badges, reference links, and the
report contract stay consistent across releases.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def load_module(name: str, relative: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, REPO / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ed = load_module("embedded_dev_lint", "scripts/embedded_dev.py")

LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


class TestMetadata(unittest.TestCase):
    def test_version_file_is_semver(self):
        version = (REPO / "VERSION").read_text(encoding="utf-8").strip()
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        self.assertEqual(ed.read_version(), version)

    def test_changelog_mentions_current_version(self):
        version = (REPO / "VERSION").read_text(encoding="utf-8").strip()
        changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## [{version}]", changelog)
        self.assertIn("Keep a Changelog", changelog)

    def test_readme_has_version_badge(self):
        version = (REPO / "VERSION").read_text(encoding="utf-8").strip()
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"version-{version}", readme)

    def test_license_is_mit(self):
        license_text = (REPO / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("MIT License", license_text)

    def test_third_party_notice_exists(self):
        self.assertTrue((REPO / "THIRD_PARTY_NOTICES.md").is_file())


class TestSkillEntrypoint(unittest.TestCase):
    def test_frontmatter(self):
        text = (REPO / "SKILL.md").read_text(encoding="utf-8")
        frontmatter = text.split("---", 2)[1]
        self.assertIn("name: embedded-dev", frontmatter)
        self.assertRegex(frontmatter, r"description:\s*\S")
        self.assertLessEqual(len(frontmatter), 4096, "frontmatter too large for a skill description")

    def test_every_reference_is_linked_from_skill(self):
        skill_text = (REPO / "SKILL.md").read_text(encoding="utf-8")
        references = sorted((REPO / "references").glob("*.md"))
        self.assertGreaterEqual(len(references), 16)
        for path in references:
            self.assertIn(f"references/{path.name}", skill_text, f"{path.name} is not reachable from SKILL.md")

    def test_reference_files_have_substance(self):
        for path in sorted((REPO / "references").glob("*.md")):
            body = path.read_text(encoding="utf-8")
            self.assertGreater(len(body), 1200, f"{path.name} is suspiciously short")
            self.assertTrue(body.startswith("# "), f"{path.name} must start with a level-1 heading")
            self.assertNotIn("TODO", body, f"{path.name} contains a TODO placeholder")

    def test_skill_links_to_cli_and_installer(self):
        skill_text = (REPO / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("scripts/embedded_dev.py", skill_text)
        self.assertIn("scripts/install.py", skill_text)

    def test_no_orphan_scripts(self):
        """Every script must be reachable from SKILL.md, tested, and documented.

        0.7.0 removed two scripts that were only named in the README tree; this
        check exists so a script can never quietly become unreachable again.
        """
        skill_text = (REPO / "SKILL.md").read_text(encoding="utf-8")
        readme_text = (REPO / "README.md").read_text(encoding="utf-8")
        test_text = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted((REPO / "tests").glob("test_*.py"))
        )
        scripts = sorted((REPO / "scripts").glob("*.py"))
        self.assertTrue(scripts)
        for script in scripts:
            name = script.name
            with self.subTest(script=name):
                self.assertIn(name, skill_text, f"{name} is not mentioned in SKILL.md")
                self.assertIn(name, readme_text, f"{name} is not mentioned in README.md")
                self.assertIn(name, test_text, f"{name} has no test that references it")

    def test_every_script_has_a_main(self):
        for script in sorted((REPO / "scripts").glob("*.py")):
            body = script.read_text(encoding="utf-8")
            with self.subTest(script=script.name):
                self.assertRegex(body, r"(?m)^def main\(", f"{script.name} must expose main(argv) -> int")
                self.assertIn("if __name__ == \"__main__\"", body, f"{script.name} needs a __main__ guard")


class TestMarkdownLinks(unittest.TestCase):
    def _check(self, document: Path):
        text = document.read_text(encoding="utf-8")
        for target in LINK_RE.findall(text):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            path_part = target.split("#", 1)[0].strip()
            if not path_part:
                continue
            resolved = (document.parent / path_part).resolve()
            self.assertTrue(resolved.exists(), f"{document.name}: broken relative link {target}")

    def test_readme_links(self):
        self._check(REPO / "README.md")

    def test_skill_links(self):
        self._check(REPO / "SKILL.md")

    def test_reference_links(self):
        for path in sorted((REPO / "references").glob("*.md")):
            self._check(path)


class TestContract(unittest.TestCase):
    def test_report_schema_required_keys_match_validator(self):
        schema = json.loads((REPO / "scripts" / "report_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["required"], ed.REPORT_REQUIRED)

    def test_diagnostic_schema_matches_hil_contract(self):
        schema = json.loads((REPO / "scripts" / "diagnostic_report_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["required"], ed.DIAGNOSTIC_REQUIRED)
        self.assertEqual(set(schema["properties"]["status"]["enum"]), set(ed.DIAGNOSTIC_STATUSES))

    def test_emitted_example_satisfies_validator(self):
        out = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "embedded_dev.py"), "report", "--emit-example", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(ed.validate_report(json.loads(out.stdout)), [])

    def test_evidence_levels_include_chinese_and_english(self):
        for level in ("已确认", "工程推论", "估算", "待验证", "数据不足", "confirmed", "inferred"):
            self.assertIn(level, ed.REPORT_EVIDENCE_LEVELS)


class TestInstallerAndCli(unittest.TestCase):
    def test_installer_lists_hosts(self):
        out = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "install.py"), "--list", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        payload = json.loads(out.stdout)
        hosts = {row["host"] for row in payload["hosts"]}
        self.assertTrue({"dsh", "codex", "claude", "project"}.issubset(hosts))

    def test_installer_plan_carries_examples(self):
        out = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "install.py"),
                "--dry-run",
                "--dest",
                str(REPO / "build" / "install-plan"),
                "--json",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("examples", json.loads(out.stdout)["entries"])

    def test_cli_help_works(self):
        out = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "embedded_dev.py"), "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(out.returncode, 0)
        for subcommand in (
            "env",
            "build",
            "flash",
            "monitor",
            "regs",
            "fault",
            "decode",
            "hil",
            "report",
            "tune",
            "board",
            "memory",
        ):
            self.assertIn(subcommand, out.stdout)

    def test_bundled_protocol_examples_decode(self):
        example = REPO / "examples" / "protocols" / "uart-env-sensor.json"
        capture = REPO / "examples" / "protocols" / "capture-uart.hex"
        validate = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "embedded_dev.py"), "decode", "validate", "--spec", str(example), "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(validate.returncode, 0, validate.stderr)
        decoded = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "embedded_dev.py"),
                "decode",
                "uart",
                "--spec",
                str(example),
                "--file",
                str(capture),
                "--json",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(decoded.returncode, 0, decoded.stderr)
        payload = json.loads(decoded.stdout)
        self.assertEqual(len(payload["frames"]), 2)
        self.assertEqual(payload["summary"]["checksum_failures"], 0)

    def test_bundled_control_examples(self):
        analyze = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "embedded_dev.py"),
                "tune",
                "analyze",
                "--csv",
                str(REPO / "examples" / "control" / "response.csv"),
                "--json",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(analyze.returncode, 0, analyze.stderr)
        self.assertEqual(json.loads(analyze.stdout)["schema_version"], "control-experiment.v1")
        update = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "embedded_dev.py"),
                "tune",
                "check-update",
                "--file",
                str(REPO / "examples" / "control" / "proposal.json"),
                "--json",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(update.returncode, 0, update.stderr)
        self.assertTrue(json.loads(update.stdout)["accepted"])

    def test_bundled_board_examples(self):
        from_ioc = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "embedded_dev.py"),
                "board",
                "from-ioc",
                "--file",
                str(REPO / "examples" / "board" / "demo.ioc"),
                "--json",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(from_ioc.returncode, 0, from_ioc.stderr)
        payload = json.loads(from_ioc.stdout)
        self.assertEqual(payload["schema_version"], "board-ioc-map.v1")
        self.assertEqual(payload["mcu"]["name"], "STM32F103C8Tx")
        controller = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "embedded_dev.py"),
                "board",
                "controller",
                "--netlist",
                str(REPO / "examples" / "board" / "demo.kicadxml"),
                "--json",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(controller.returncode, 0, controller.stderr)
        self.assertEqual(json.loads(controller.stdout)["controller"]["ref"], "U1")

    def test_bundled_memory_detection(self):
        out = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "embedded_dev.py"),
                "memory",
                "detect",
                "--project",
                str(REPO / "examples" / "board"),
                "--json",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        detection = json.loads(out.stdout)
        self.assertEqual(detection["chips"], ["STM32F103C8Tx"])
        self.assertFalse(detection["ambiguous"])

    def test_mcp_server_self_test(self):
        out = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "mcp_server.py"), "--self-test"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(out.returncode, 0, out.stderr or out.stdout)

    def test_compileall(self):
        out = subprocess.run(
            [sys.executable, "-m", "compileall", "-q", "scripts", "tests"],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == "__main__":
    unittest.main()
