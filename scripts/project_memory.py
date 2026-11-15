#!/usr/bin/env python3
"""Project archive and evidence-graded memory for embedded work.

Two files, two purposes:

* ``.embedded-dev/project.json`` records how the project was detected. It is
  a suggestion to be confirmed, safe to commit, and never a substitute for the
  schematic or the datasheet.
* ``.embedded-dev/knowledge/state.json`` stores durable notes with an evidence
  level and a source. It is local by default because it can contain excerpts
  of logs and debug sessions.

Memory is not a checkpoint: it does not replace the conversation, the code,
the hardware run or the user's acceptance. It records what was learned, with
enough provenance that a later session can decide whether to trust it.

Exit codes:
    0  success
    4  rejected write / conflict / not found
    6  configuration error (not a project, locked state, malformed files)
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import time
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Any

EXIT_OK = 0
EXIT_DATA = 4
EXIT_CONFIG = 6

DIR_NAME = ".embedded-dev"
PROJECT_FILE = "project.json"
KNOWLEDGE_DIR = "knowledge"
STATE_FILE = "state.json"
PROJECT_SCHEMA = "project-archive.v1"
STATE_SCHEMA = "project-memory.v1"

MAX_ENTRIES = 200
MAX_STATE_BYTES = 512 * 1024
MAX_TITLE = 120
MAX_CONTENT = 20000
MAX_TAGS = 10
MAX_SCAN_DEPTH = 2
MAX_SCAN_ENTRIES = 400
LOCK_STALE_SECONDS = 10.0
LOCK_TIMEOUT_SECONDS = 3.0

EVIDENCE_LEVELS = ["已确认", "工程推论", "估算", "待验证", "数据不足"]
EVIDENCE_ALIASES = {
    "verified": "已确认",
    "confirmed": "已确认",
    "inferred": "工程推论",
    "inference": "工程推论",
    "estimated": "估算",
    "estimate": "估算",
    "assumption": "待验证",
    "unverified": "待验证",
    "insufficient": "数据不足",
    "missing": "数据不足",
}
SKIP_DIRS = {
    ".git",
    ".svn",
    ".hg",
    ".embedded-dev",
    "node_modules",
    "build",
    "dist",
    "out",
    "target",
    ".venv",
    "venv",
    "__pycache__",
    "third_party",
    "drivers",
    "middlewares",
}
MARKER_SUFFIXES = {".ioc", ".uvprojx", ".ewp"}
MARKER_NAMES = {
    "cmakelists.txt": "cmake",
    "makefile": "make",
    "platformio.ini": "platformio",
    "sdkconfig": "esp-idf",
    "idf_component.yml": "esp-idf",
}
CHIP_PATTERN = re.compile(
    r"(STM32[A-Z0-9]+|GD32[A-Z0-9]+|CH32[A-Z0-9]+|ESP32[A-Z0-9\-]*|RP2040|RP2350|"
    r"NRF5\d+|SAMD\d+|ATMEGA[A-Z0-9]*|ATTINY[A-Z0-9]*|MSP430[A-Z0-9]+)",
    re.IGNORECASE,
)


class MemoryError_(Exception):
    """Write rejected for a reason the caller must fix (exit 4)."""


class ConfigError(Exception):
    """Environment/arguments unusable (exit 6)."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _paths(root: Path) -> tuple[Path, Path, Path]:
    base = root / DIR_NAME
    return base, base / PROJECT_FILE, base / KNOWLEDGE_DIR / STATE_FILE


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _scan(root: Path) -> tuple[list[dict[str, str]], set[str]]:
    markers: list[dict[str, str]] = []
    chips: set[str] = set()
    seen = 0
    for current, directories, files in os.walk(root, followlinks=False):
        depth = len(PurePath(current).relative_to(root).parts)
        if depth >= MAX_SCAN_DEPTH:
            directories[:] = []
        directories[:] = [name for name in sorted(directories) if name.lower() not in SKIP_DIRS]
        for name in sorted(files):
            seen += 1
            if seen > MAX_SCAN_ENTRIES:
                return markers, chips
            suffix = Path(name).suffix.lower()
            lowered = name.lower()
            kind = None
            if suffix in MARKER_SUFFIXES:
                kind = "cubemx" if suffix == ".ioc" else ("keil" if suffix == ".uvprojx" else "iar")
            elif lowered in MARKER_NAMES:
                kind = MARKER_NAMES[lowered]
            if kind is None:
                continue
            path = Path(current) / name
            markers.append({"kind": kind, "path": str(path.relative_to(root))})
            if suffix == ".ioc":
                try:
                    chips.update(_ioc_chips(path.read_text(encoding="utf-8-sig", errors="replace")))
                except OSError:
                    pass
            elif suffix == ".uvprojx":
                try:
                    text = path.read_text(encoding="utf-8-sig", errors="replace")
                    chips.update(match.group(1).strip() for match in re.finditer(r"<Device>([^<]+)</Device>", text))
                except OSError:
                    pass
    return markers, chips


def _ioc_chips(text: str) -> set[str]:
    """Return the single most specific MCU name recorded by CubeMX."""
    for key in ("Mcu.UserName", "Mcu.Name", "Mcu.PartNumber"):
        match = re.search(rf"^{key}=(.+)$", text, re.MULTILINE)
        if match and match.group(1).strip():
            return {match.group(1).strip()}
    return set()


def detect_project(root: Path) -> dict[str, Any]:
    if not root.exists() or not root.is_dir():
        raise ConfigError(f"project path is not a directory: {root}")
    markers, chips = _scan(root)
    build_systems = sorted({marker["kind"] for marker in markers} - {"cubemx", "keil", "iar"})
    if not build_systems:
        build_systems = sorted({marker["kind"] for marker in markers})
    ordered_chips = sorted(chips)
    ambiguous = len(ordered_chips) > 1
    if ordered_chips and not ambiguous:
        confidence = "high"
    elif ordered_chips and ambiguous:
        confidence = "medium"
    else:
        confidence = "low"
    warnings: list[str] = []
    if ambiguous:
        warnings.append(
            "multiple chip candidates were found; confirm the target before generating anything"
        )
    if not ordered_chips:
        warnings.append("no chip was identified from .ioc/.uvprojx metadata; confirm the MCU manually")
    if not markers:
        warnings.append("no build-system or CubeMX marker was found in the scanned folders")
    return {
        "schema_version": PROJECT_SCHEMA,
        "root": str(root),
        "markers": markers,
        "build_systems": build_systems,
        "chips": ordered_chips,
        "ambiguous": ambiguous,
        "confidence": confidence,
        "warnings": warnings,
        "limits": [
            f"scan covers the project root and up to {MAX_SCAN_DEPTH} folder levels, max {MAX_SCAN_ENTRIES} entries",
            "detection is a suggestion, not a verified fact; confirm the chip/build before relying on it",
            "a successful build is not a flash and is not hardware acceptance",
        ],
    }


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA,
        "version": 0,
        "next_id": 1,
        "entries": [],
        "tombstone_titles": [],
    }


def _validate_state(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise ConfigError("state.json must contain an object")
    if state.get("schema_version") != STATE_SCHEMA:
        raise ConfigError("state.json has an unexpected schema_version")
    entries = state.get("entries")
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        raise ConfigError(f"state.json entries must be a list of at most {MAX_ENTRIES} items")
    if not isinstance(state.get("version"), int) or state["version"] < 0:
        raise ConfigError("state.json version must be a non-negative integer")
    if not isinstance(state.get("next_id"), int) or state["next_id"] < 1:
        raise ConfigError("state.json next_id must be a positive integer")
    if not isinstance(state.get("tombstone_titles"), list):
        raise ConfigError("state.json tombstone_titles must be a list")
    return state


def load_state(root: Path) -> dict[str, Any]:
    _, _, state_path = _paths(root)
    if not state_path.exists():
        return _empty_state()
    return _validate_state(_read_json(state_path))


def _save_state(root: Path, state: dict[str, Any]) -> None:
    _, _, state_path = _paths(root)
    text = json.dumps(state, ensure_ascii=False, indent=2)
    if len(text.encode("utf-8")) > MAX_STATE_BYTES:
        raise MemoryError_(
            f"state would exceed {MAX_STATE_BYTES} bytes; forget or shorten entries before saving"
        )
    _write_json(state_path, state)


@contextlib.contextmanager
def _state_lock(root: Path) -> Iterator[None]:
    _, _, state_path = _paths(root)
    lock_path = state_path.parent / "state.lock"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(descriptor)
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = LOCK_STALE_SECONDS
            if age > LOCK_STALE_SECONDS:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() > deadline:
                raise ConfigError(
                    "memory state is locked by another session; wait a moment and retry"
                ) from None
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


def _require_project(root: Path) -> dict[str, Any]:
    _, project_path, _ = _paths(root)
    if not project_path.exists():
        raise ConfigError(
            f"{root} has no {DIR_NAME}/{PROJECT_FILE}; run 'memory init --project <dir>' first"
        )
    return _read_json(project_path)


# ---------------------------------------------------------------------------
# Entry helpers
# ---------------------------------------------------------------------------


def normalize_evidence_level(value: str | None) -> str:
    if value is None or not str(value).strip():
        return "待验证"
    raw = str(value).strip()
    if raw in EVIDENCE_LEVELS:
        return raw
    lowered = raw.lower()
    if lowered in EVIDENCE_ALIASES:
        return EVIDENCE_ALIASES[lowered]
    raise MemoryError_(
        "evidence level must be one of " + " / ".join(EVIDENCE_LEVELS) + f" (got {raw!r})"
    )


def _normalize_tags(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        items = [item.strip() for item in raw.split(",")]
    elif isinstance(raw, list):
        items = [str(item).strip() for item in raw]
    else:
        raise MemoryError_("tags must be a comma-separated string or a list")
    tags = []
    for item in items:
        if item and item not in tags:
            tags.append(item)
    if len(tags) > MAX_TAGS:
        raise MemoryError_(f"at most {MAX_TAGS} tags are allowed")
    return tags


def _entry_from_args(args: Any, state: dict[str, Any]) -> dict[str, Any]:
    title = (args.title or "").strip()
    content = (args.content or "").strip()
    if not title:
        raise MemoryError_("--title must not be empty")
    if len(title) > MAX_TITLE:
        raise MemoryError_(f"--title must be at most {MAX_TITLE} characters")
    if not content:
        raise MemoryError_("--content must not be empty")
    if len(content) > MAX_CONTENT:
        raise MemoryError_(f"--content must be at most {MAX_CONTENT} characters")
    level = normalize_evidence_level(args.evidence_level)
    source = (args.source or "").strip() or None
    if level == "已确认" and not source:
        raise MemoryError_(
            "'已确认' requires --source (datasheet section, register, logic-analyzer capture, log line)"
        )
    if title in state["tombstone_titles"] and not args.allow_recreate:
        raise MemoryError_(
            f"'{title}' was forgotten earlier; use a different title or pass --allow-recreate"
        )
    if len(state["entries"]) >= MAX_ENTRIES:
        raise MemoryError_(f"memory holds the maximum of {MAX_ENTRIES} entries; forget one first")
    stamp = _now()
    entry = {
        "id": state["next_id"],
        "title": title,
        "content": content,
        "scope": (args.scope or "project").strip() or "project",
        "evidence_level": level,
        "source": source,
        "tags": _normalize_tags(args.tags),
        "created_at": stamp,
        "updated_at": stamp,
    }
    if title in state["tombstone_titles"]:
        state["tombstone_titles"].remove(title)
    state["next_id"] += 1
    state["version"] += 1
    state["entries"].append(entry)
    return entry


def _find_entry(state: dict[str, Any], entry_id: int | None, title: str | None) -> dict[str, Any]:
    if entry_id is not None:
        for entry in state["entries"]:
            if entry.get("id") == entry_id:
                return entry
        raise MemoryError_(f"no memory entry with id {entry_id}")
    if title:
        for entry in state["entries"]:
            if entry.get("title") == title:
                return entry
        raise MemoryError_(f"no memory entry titled {title!r}")
    raise MemoryError_("pass --id or --title")


def _check_expected_version(state: dict[str, Any], expected: int | None) -> None:
    if expected is not None and state["version"] != expected:
        raise MemoryError_(
            f"version conflict: state is at version {state['version']}, caller expected {expected}; "
            "re-read before writing"
        )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_detect(args: Any) -> int:
    payload = detect_project(Path(args.project).resolve())
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_init(args: Any) -> int:
    root = Path(args.project).resolve()
    detection = detect_project(root)
    base, project_path, _ = _paths(root)
    if project_path.exists() and not args.force:
        raise MemoryError_(f"{project_path} already exists; pass --force to overwrite the detection record")
    base.mkdir(parents=True, exist_ok=True)
    (base / KNOWLEDGE_DIR).mkdir(parents=True, exist_ok=True)
    project = {
        "schema_version": PROJECT_SCHEMA,
        "project": {
            "name": root.name,
            "root": str(root),
            "detected": {
                "markers": detection["markers"],
                "build_systems": detection["build_systems"],
                "chips": detection["chips"],
                "ambiguous": detection["ambiguous"],
                "confidence": detection["confidence"],
            },
            "confirmed": {},
        },
        "created_at": _now(),
        "updated_at": _now(),
        "notes": [
            "detection is a suggestion; confirm chip/build in 'confirmed' before relying on it",
            "this file is safe to commit; knowledge/state.json is local by default",
        ],
    }
    _write_json(project_path, project)
    with _state_lock(root):
        state = load_state(root)
        if not (base / KNOWLEDGE_DIR / STATE_FILE).exists():
            _save_state(root, state)
    payload = {
        "schema_version": "project-memory-init.v1",
        "initialized": True,
        "project": str(project_path),
        "detection": detection,
        "memory": {"entries": len(load_state(root)["entries"]), "version": load_state(root)["version"]},
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else
          f"initialized {project_path}\nchips: {', '.join(detection['chips']) or 'unknown'} "
          f"(confidence {detection['confidence']})")
    return EXIT_OK


def cmd_show(args: Any) -> int:
    root = Path(args.project).resolve()
    project = _require_project(root)
    state = load_state(root)
    recent = sorted(state["entries"], key=lambda entry: entry.get("updated_at", ""), reverse=True)[: args.recent]
    payload = {
        "schema_version": "project-memory-show.v1",
        "project": project,
        "memory": {
            "version": state["version"],
            "entries": len(state["entries"]),
            "tombstones": len(state["tombstone_titles"]),
        },
        "recent": recent,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK
    detected = project.get("project", {}).get("detected", {})
    print(f"project: {project.get('project', {}).get('name')} ({root})")
    print(f"chips: {', '.join(detected.get('chips', [])) or 'unknown'} (confidence {detected.get('confidence')})")
    print(f"build: {', '.join(detected.get('build_systems', [])) or 'unknown'}")
    print(f"memory: {len(state['entries'])} entries, version {state['version']}")
    for entry in recent:
        print(f"  #{entry['id']} [{entry['evidence_level']}] {entry['title']}")
    return EXIT_OK


def _search_entries(entries: list[dict[str, Any]], query: str, limit: int) -> list[dict[str, Any]]:
    needle = query.casefold()
    scored = []
    for entry in entries:
        title = str(entry.get("title", ""))
        haystack = " ".join(
            [
                title,
                str(entry.get("content", "")),
                str(entry.get("scope", "")),
                " ".join(entry.get("tags", [])),
            ]
        ).casefold()
        if needle not in haystack:
            continue
        score = 0
        if needle in title.casefold():
            score += 10
        if any(needle in tag.casefold() for tag in entry.get("tags", [])):
            score += 5
        if needle in str(entry.get("scope", "")).casefold():
            score += 3
        scored.append((score, entry.get("updated_at", ""), entry))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [entry for _, _, entry in scored[:limit]]


def cmd_search(args: Any) -> int:
    root = Path(args.project).resolve()
    _require_project(root)
    state = load_state(root)
    matches = _search_entries(state["entries"], args.query, args.limit)
    payload = {
        "schema_version": "project-memory-search.v1",
        "query": args.query,
        "count": len(matches),
        "matches": matches,
        "limits": ["substring search only; there is no semantic index and no external service"],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"{len(matches)} match(es) for {args.query!r}")
        for entry in matches:
            print(f"  #{entry['id']} [{entry['evidence_level']}] {entry['title']}")
    return EXIT_OK


def cmd_list(args: Any) -> int:
    root = Path(args.project).resolve()
    _require_project(root)
    state = load_state(root)
    entries = sorted(state["entries"], key=lambda entry: entry.get("updated_at", ""), reverse=True)
    if not args.all:
        entries = entries[: args.recent]
    payload = {
        "schema_version": "project-memory-list.v1",
        "version": state["version"],
        "total": len(state["entries"]),
        "count": len(entries),
        "entries": entries,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for entry in entries:
            print(f"#{entry['id']} [{entry['evidence_level']}] {entry['title']} ({entry['updated_at']})")
        if not entries:
            print("no entries")
    return EXIT_OK


def cmd_remember(args: Any) -> int:
    root = Path(args.project).resolve()
    _require_project(root)
    with _state_lock(root):
        state = load_state(root)
        _check_expected_version(state, args.expected_version)
        entry = _entry_from_args(args, state)
        _save_state(root, state)
    payload = {
        "schema_version": "project-memory-write.v1",
        "saved": True,
        "entry": entry,
        "version": state["version"],
        "count": len(state["entries"]),
        "limits": [
            "memory records evidence, not acceptance; hardware verification is still required",
            "the local state file stays out of git by default; do not store secrets",
        ],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"saved #{entry['id']} [{entry['evidence_level']}] {entry['title']} (version {state['version']})")
    return EXIT_OK


def cmd_forget(args: Any) -> int:
    root = Path(args.project).resolve()
    _require_project(root)
    with _state_lock(root):
        state = load_state(root)
        _check_expected_version(state, args.expected_version)
        entry = _find_entry(state, args.id, args.title)
        state["entries"].remove(entry)
        if entry["title"] not in state["tombstone_titles"]:
            state["tombstone_titles"].append(entry["title"])
        state["version"] += 1
        _save_state(root, state)
    payload = {
        "schema_version": "project-memory-forget.v1",
        "forgotten": {"id": entry["id"], "title": entry["title"]},
        "version": state["version"],
        "remaining": len(state["entries"]),
        "limits": [
            "the title is tombstoned to prevent silent re-creation; pass --allow-recreate to override",
        ],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"forgot #{entry['id']} {entry['title']} (version {state['version']})")
    return EXIT_OK


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="project_memory.py",
        description=(
            "Project archive and evidence-graded memory: detect the toolchain/chip, record "
            "notes with provenance, search them and forget them. Memory is local evidence, "
            "not a hardware acceptance record."
        ),
    )
    parser.add_argument("--version", action="version", version="embedded-dev project_memory 0.6.0")
    subparsers = parser.add_subparsers(dest="action", required=True)

    detect = subparsers.add_parser("detect", help="scan a project for chip/build markers (read-only)")
    detect.add_argument("--project", required=True)
    detect.add_argument("--json", action="store_true")

    init = subparsers.add_parser("init", help="write .embedded-dev/project.json and the memory state")
    init.add_argument("--project", required=True)
    init.add_argument("--force", action="store_true", help="overwrite an existing project.json")
    init.add_argument("--json", action="store_true")

    show = subparsers.add_parser("show", help="show the project archive and recent memories")
    show.add_argument("--project", required=True)
    show.add_argument("--recent", type=int, default=6)
    show.add_argument("--json", action="store_true")

    remember = subparsers.add_parser("remember", help="save one memory entry with evidence level and source")
    remember.add_argument("--project", required=True)
    remember.add_argument("--title", required=True)
    remember.add_argument("--content", required=True)
    remember.add_argument("--scope", default="project")
    remember.add_argument("--evidence-level", default=None, help=" | ".join(EVIDENCE_LEVELS))
    remember.add_argument("--source", default=None)
    remember.add_argument("--tags", default=None, help="comma-separated")
    remember.add_argument("--expected-version", type=int, default=None)
    remember.add_argument("--allow-recreate", action="store_true")
    remember.add_argument("--json", action="store_true")

    search = subparsers.add_parser("search", help="substring search across title/content/scope/tags")
    search.add_argument("--project", required=True)
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--json", action="store_true")

    listing = subparsers.add_parser("list", help="list memories, most recently updated first")
    listing.add_argument("--project", required=True)
    listing.add_argument("--recent", type=int, default=6)
    listing.add_argument("--all", action="store_true")
    listing.add_argument("--json", action="store_true")

    forget = subparsers.add_parser("forget", help="forget one entry by id or exact title")
    forget.add_argument("--project", required=True)
    forget.add_argument("--id", type=int, default=None)
    forget.add_argument("--title", default=None)
    forget.add_argument("--expected-version", type=int, default=None)
    forget.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    handlers = {
        "detect": cmd_detect,
        "init": cmd_init,
        "show": cmd_show,
        "remember": cmd_remember,
        "search": cmd_search,
        "list": cmd_list,
        "forget": cmd_forget,
    }
    try:
        return handlers[args.action](args)
    except MemoryError_ as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_DATA
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        return EXIT_DATA


if __name__ == "__main__":
    sys.exit(main())
