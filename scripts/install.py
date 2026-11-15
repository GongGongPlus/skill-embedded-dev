#!/usr/bin/env python3
"""One-click installer / verifier for the embedded-dev skill.

The repository is laid out as a single skill directory: SKILL.md lives at the
repository root together with references/ and scripts/. That layout is directly
discoverable by DSH, Codex, Claude Code and other Agent Skills standard hosts
when copied to one of their personal skill roots. This installer copies only the
runtime entries (not tests, CI or git history) and verifies the copy.

Verification is content-based, never a directory-exists guess: a target is only
reported as installed when every runtime entry is present and the copied
SKILL.md carries the right frontmatter name. `--list` runs the same check, so a
stale or partial copy shows up as `valid: false` with the concrete missing
entries instead of a misleading green tick.

`--target` accepts one host, a comma list (``dsh,codex``) or ``all`` (every host
whose skill root already exists), so the same skill can be kept in sync for
DSH and Codex in one command. ``--force`` replaces only the runtime entries and
leaves unrelated files (``.git``, ``tests/``, host metadata) untouched.

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
RUNTIME_ENTRIES = [
    "SKILL.md",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "VERSION",
    "pyproject.toml",
    "references",
    "scripts",
    "examples",
]
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".pytest_cache", "*.log")

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_REFUSED = 2
EXIT_INVALID = EXIT_USAGE

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


def read_version(directory: Path) -> str | None:
    try:
        text = (directory / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def verify_install(dest: Path) -> list[str]:
    """Return concrete defects for an installed copy; empty list means valid."""
    errors: list[str] = []
    if not dest.is_dir():
        return [f"target directory missing: {dest}"]
    skill_md = dest / "SKILL.md"
    if not skill_md.is_file():
        errors.append("SKILL.md missing")
    elif skill_frontmatter_name(skill_md) != SKILL_NAME:
        errors.append(f"SKILL.md frontmatter name must be {SKILL_NAME!r}")
    refs = dest / "references"
    if not refs.is_dir() or not any(refs.glob("*.md")):
        errors.append("references/*.md missing")
    for relative in ("scripts/embedded_dev.py", "scripts/install.py"):
        if not (dest / relative).is_file():
            errors.append(f"{relative} missing (run the copy again)")
    for relative in ("examples/control/proposal.json", "examples/board/demo.ioc"):
        if not (dest / relative).is_file():
            errors.append(f"{relative} missing (docs reference it)")
    if not (dest / "CHANGELOG.md").is_file():
        errors.append("CHANGELOG.md missing (README links it)")
    if read_version(dest) is None:
        errors.append("VERSION missing or empty")
    return errors


def host_root(host: str) -> Path:
    if host not in HOSTS:
        raise KeyError(host)
    return HOSTS[host]()


def host_status(host: str) -> dict:
    root = host_root(host)
    target = root / SKILL_NAME
    row = {
        "host": host,
        "root": str(root),
        "root_exists": root.exists(),
        "target": str(target),
        "installed": target.is_dir(),
        "valid": False,
        "version": None,
        "version_matches_source": None,
        "errors": [],
    }
    if not row["installed"]:
        row["errors"] = ["not installed"]
        return row
    row["errors"] = verify_install(target)
    row["valid"] = not row["errors"]
    row["version"] = read_version(target)
    source_version = read_version(REPO_ROOT)
    row["version_matches_source"] = row["version"] == source_version
    return row


def cmd_list(as_json: bool) -> int:
    rows = [host_status(host) for host in HOSTS]
    source_version = read_version(REPO_ROOT)
    currency = [row["host"] for row in rows if row["installed"] and not row["valid"]]
    drifted = [
        row["host"]
        for row in rows
        if row["installed"] and row["valid"] and row["version_matches_source"] is False
    ]
    if as_json:
        print(
            json.dumps(
                {
                    "repo": str(REPO_ROOT),
                    "version": source_version,
                    "hosts": rows,
                    "invalid": currency,
                    "outdated": drifted,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return EXIT_OK
    print(f"embedded-dev source: {REPO_ROOT} (version {source_version})")
    print(f"{'host':<16}{'installed':<11}{'valid':<7}{'version':<9}target")
    for row in rows:
        print(
            f"{row['host']:<16}{str(row['installed']):<11}{str(row['valid']):<7}"
            f"{row['version'] or '-':<9}{row['target']}"
        )
    for row in rows:
        if row["installed"] and not row["valid"]:
            print(f"\n{row['host']} is INVALID: " + "; ".join(row["errors"]))
        elif row["installed"] and row["version_matches_source"] is False:
            print(f"\n{row['host']} is OUTDATED: {row['version']} != {source_version}")
    print("\nInstall one host:      python scripts/install.py --target dsh")
    print("Install both hosts:    python scripts/install.py --target dsh,codex --force")
    print("Verify every host:     python scripts/install.py --verify-only --target all")
    print("Project-scoped:        python scripts/install.py --target project")
    return EXIT_OK


def resolve_targets(spec: str) -> list[str]:
    if spec == "all":
        return [host for host in HOSTS if host_root(host).exists()]
    names = [part.strip() for part in spec.split(",") if part.strip()]
    unknown = [name for name in names if name not in HOSTS]
    if unknown:
        raise KeyError(", ".join(unknown))
    return names


def replace_runtime_entries(target: Path) -> None:
    """Remove only runtime entries so .git, tests/ and host metadata survive."""
    for entry in RUNTIME_ENTRIES:
        existing = target / entry
        if existing.is_dir() and not existing.is_symlink():
            shutil.rmtree(existing)
        elif existing.exists() or existing.is_symlink():
            existing.unlink()


def is_source_checkout(target: Path) -> bool:
    """True when target *is* this repository, so copying onto it would destroy the source."""
    try:
        return target.resolve() == REPO_ROOT.resolve()
    except OSError:
        return False


def install_one(host: str, dest: Path | None, force: bool, dry_run: bool) -> dict:
    target = dest if dest else host_root(host) / SKILL_NAME
    plan = {"host": host, "target": str(target), "entries": RUNTIME_ENTRIES, "dry_run": dry_run}
    result = {"host": host, "target": str(target), "plan": plan, "installed": False, "errors": []}
    if is_source_checkout(target):
        result.update(
            {
                "installed": True,
                "skipped": True,
                "reason": "target is the source repository itself; it already holds this version",
            }
        )
        return result
    if dry_run:
        result["dry_run"] = True
        return result
    if target.exists() and not force:
        result["errors"] = [f"refusing to overwrite existing install at {target}; pass --force"]
        return result
    if target.exists():
        replace_runtime_entries(target)
    target.mkdir(parents=True, exist_ok=True)
    for entry in RUNTIME_ENTRIES:
        source = REPO_ROOT / entry
        if not source.exists():
            result["errors"] = [f"source entry missing: {source}"]
            return result
        if source.is_dir():
            shutil.copytree(source, target / entry, ignore=IGNORE)
        else:
            shutil.copy2(source, target / entry)
    errors = verify_install(target)
    result["errors"] = errors
    result["installed"] = not errors
    return result


def install(host: str, dest: Path | None, force: bool, dry_run: bool, as_json: bool) -> int:
    result = install_one(host, dest, force, dry_run)
    if result.get("dry_run"):
        print(json.dumps(result["plan"], ensure_ascii=False, indent=2) if as_json else f"would install to {result['target']}")
        return EXIT_OK
    if result.get("skipped"):
        message = f"{result['target']} is the source repository; skipped (already current)"
        print(json.dumps(result, ensure_ascii=False, indent=2) if as_json else message)
        return EXIT_OK
    if result["errors"]:
        joined = "; ".join(result["errors"])
        if all("refusing to overwrite" in error for error in result["errors"]):
            print(joined, file=sys.stderr)
            return EXIT_REFUSED
        print(f"verification failed for {result['target']}: {joined}", file=sys.stderr)
        return EXIT_USAGE
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        version = read_version(REPO_ROOT) or "unknown"
        print(f"installed embedded-dev {version} -> {result['target']}")
        print("start a fresh session so the host rediscovers the skill directory")
    return EXIT_OK


def install_many(spec: str, force: bool, dry_run: bool, as_json: bool) -> int:
    hosts = resolve_targets(spec)
    results = [install_one(host, None, force, dry_run) for host in hosts]
    failures = [row for row in results if row["errors"]]
    if as_json:
        print(json.dumps({"installed": not failures, "dry_run": dry_run, "results": results}, ensure_ascii=False, indent=2))
    else:
        for row in results:
            if row["errors"]:
                print(f"{row['host']:<12} FAILED: " + "; ".join(row["errors"]), file=sys.stderr)
            elif row.get("skipped"):
                print(f"{row['host']:<12} skipped: target is the source repository")
            elif row.get("dry_run"):
                print(f"{row['host']:<12} would install to {row['target']}")
            else:
                print(f"{row['host']:<12} installed -> {row['target']}")
    if failures:
        refused = all(
            error.startswith("refusing to overwrite") for row in failures for error in row["errors"]
        )
        return EXIT_REFUSED if refused else EXIT_USAGE
    return EXIT_OK


def verify_targets(spec: str, as_json: bool) -> int:
    rows = [host_status(host) for host in resolve_targets(spec)]
    bad = [row for row in rows if not row["valid"]]
    if as_json:
        print(json.dumps({"valid": not bad, "hosts": rows}, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            state = "OK" if row["valid"] else "INVALID: " + "; ".join(row["errors"])
            version = row["version"] or "-"
            print(f"{row['host']:<16}{version:<9}{state}")
    return EXIT_OK if not bad else EXIT_INVALID


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install or verify the embedded-dev skill in an agent harness.")
    parser.add_argument(
        "--target",
        default="dsh",
        help="host skill root: one name, a comma list (dsh,codex) or 'all' (default: dsh)",
    )
    parser.add_argument("--dest", help="explicit destination directory (overrides --target)")
    parser.add_argument("--force", action="store_true", help="replace the runtime entries of an existing install")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify-only", action="store_true", help="verify existing installations and exit")
    parser.add_argument("--list", action="store_true", help="list known hosts with content verification")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.list:
        return cmd_list(args.json)
    try:
        targets = resolve_targets(args.target)
    except KeyError as exc:
        print(f"unknown host {exc}", file=sys.stderr)
        return EXIT_USAGE
    if args.verify_only:
        if args.dest:
            errors = verify_install(Path(args.dest))
            if args.json:
                print(json.dumps({"target": args.dest, "valid": not errors, "errors": errors}, ensure_ascii=False, indent=2))
            elif errors:
                print("invalid installation: " + "; ".join(errors), file=sys.stderr)
            else:
                print(f"installation OK: {args.dest}")
            return EXIT_OK if not errors else EXIT_INVALID
        return verify_targets(args.target, args.json)
    if args.dest:
        return install(targets[0], Path(args.dest), args.force, args.dry_run, args.json)
    if len(targets) == 1:
        return install(targets[0], None, args.force, args.dry_run, args.json)
    return install_many(args.target, args.force, args.dry_run, args.json)


if __name__ == "__main__":
    sys.exit(main())
