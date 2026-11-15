#!/usr/bin/env python3
"""One-click installer / verifier for the embedded-dev skill.

The repository is laid out as a single skill directory: SKILL.md lives at the
repository root together with references/ and scripts/. That layout is directly
discoverable by DSH, Codex, Claude Code and other Agent Skills standard hosts
when copied to one of their personal skill roots. This installer copies only the
runtime entries (not tests, CI or git history) and verifies the copy.

Exit codes: 0 installed/verified, 1 usage or verification error, 2 refused to
overwrite without --force.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_NAME = "embedded-dev"
RUNTIME_ENTRIES = ["SKILL.md", "README.md", "LICENSE", "VERSION", "references", "scripts", "examples"]
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".pytest_cache", "*.log")

HOSTS = {
    "dsh": lambda: Path.home() / ".dsh" / "skills",
    "codex": lambda: Path.home() / ".codex" / "skills",
    "claude": lambda: Path.home() / ".claude" / "skills",
    "agents": lambda: Path.home() / ".agents" / "skills",
    "project": lambda: Path.cwd() / ".dsh" / "skills",
    "project-claude": lambda: Path.cwd() / ".claude" / "skills",
}


def skill_frontmatter_name(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^name:\s*([A-Za-z0-9._-]+)\s*$", text, re.MULTILINE)
    return match.group(1) if match else None


def verify_install(dest: Path) -> list[str]:
    errors: list[str] = []
    skill_md = dest / "SKILL.md"
    if not skill_md.is_file():
        errors.append("SKILL.md missing")
    elif skill_frontmatter_name(skill_md) != SKILL_NAME:
        errors.append(f"SKILL.md frontmatter name must be {SKILL_NAME!r}")
    refs = dest / "references"
    if not refs.is_dir() or not any(refs.glob("*.md")):
        errors.append("references/*.md missing")
    script = dest / "scripts" / "embedded_dev.py"
    if not script.is_file():
        errors.append("scripts/embedded_dev.py missing")
    if not (dest / "scripts" / "install.py").is_file():
        errors.append("scripts/install.py missing (run the copy again)")
    if not (dest / "examples" / "control" / "proposal.json").is_file():
        errors.append("examples/control/proposal.json missing (docs reference it)")
    if not (dest / "examples" / "board" / "demo.ioc").is_file():
        errors.append("examples/board/demo.ioc missing (docs reference it)")
    return errors


def host_root(host: str) -> Path:
    if host not in HOSTS:
        raise KeyError(host)
    return HOSTS[host]()


def cmd_list(as_json: bool) -> int:
    rows = []
    for host, root_fn in HOSTS.items():
        root = root_fn()
        target = root / SKILL_NAME
        rows.append(
            {
                "host": host,
                "root": str(root),
                "root_exists": root.exists(),
                "installed": target.exists(),
                "target": str(target),
            }
        )
    if as_json:
        print(json.dumps({"repo": str(REPO_ROOT), "version": (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip(), "hosts": rows}, ensure_ascii=False, indent=2))
        return 0
    print(f"embedded-dev source: {REPO_ROOT}")
    print(f"{'host':<16} {'root exists':<12} {'installed':<10} target")
    for row in rows:
        print(f"{row['host']:<16} {str(row['root_exists']):<12} {str(row['installed']):<10} {row['target']}")
    print("\nInstall: python scripts/install.py --target dsh")
    print("Project-scoped: python scripts/install.py --target project")
    return 0


def install(host: str, dest: Path | None, force: bool, dry_run: bool, as_json: bool) -> int:
    target = dest if dest else host_root(host) / SKILL_NAME
    plan = {"host": host, "target": str(target), "entries": RUNTIME_ENTRIES, "dry_run": dry_run}
    if dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2) if as_json else f"would install to {target}")
        return 0
    if target.exists() and not force:
        print(f"refusing to overwrite existing install at {target}; pass --force to replace it", file=sys.stderr)
        return 2
    if target.exists() and force:
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    for entry in RUNTIME_ENTRIES:
        source = REPO_ROOT / entry
        if not source.exists():
            print(f"source entry missing: {source}", file=sys.stderr)
            return 1
        if source.is_dir():
            shutil.copytree(source, target / entry, ignore=IGNORE)
        else:
            shutil.copy2(source, target / entry)
    errors = verify_install(target)
    if errors:
        print("verification failed: " + "; ".join(errors), file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps({"installed": True, "target": str(target), "plan": plan}, ensure_ascii=False, indent=2))
    else:
        print(f"installed embedded-dev { (REPO_ROOT / 'VERSION').read_text(encoding='utf-8').strip() } -> {target}")
        print("start a fresh session so the host rediscovers the skill directory")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install or verify the embedded-dev skill in an agent harness.")
    parser.add_argument("--target", default="dsh", choices=sorted(HOSTS), help="host skill root (default: dsh)")
    parser.add_argument("--dest", help="explicit destination directory (overrides --target)")
    parser.add_argument("--force", action="store_true", help="replace an existing installation")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify-only", action="store_true", help="verify an existing installation and exit")
    parser.add_argument("--list", action="store_true", help="list known hosts and detection status")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.list:
        return cmd_list(args.json)
    target = Path(args.dest) if args.dest else host_root(args.target) / SKILL_NAME
    if args.verify_only:
        errors = verify_install(target)
        if args.json:
            print(json.dumps({"target": str(target), "valid": not errors, "errors": errors}, ensure_ascii=False, indent=2))
        elif errors:
            print("invalid installation: " + "; ".join(errors), file=sys.stderr)
        else:
            print(f"installation OK: {target}")
        return 0 if not errors else 1
    try:
        return install(args.target, Path(args.dest) if args.dest else None, args.force, args.dry_run, args.json)
    except KeyError:
        print(f"unknown host {args.target}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
