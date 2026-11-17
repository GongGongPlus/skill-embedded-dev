#!/usr/bin/env python3
"""GNU ld map-file analysis: section totals, flash/RAM budget and top symbols.

Why this exists: ``MUST`` in SKILL.md requires recording flash/RAM usage, and
``references/memory-boot.md`` says a map file has three uses (locate a symbol,
decide whether a fault address is inside a valid region, find the big spenders).
This module turns that prose into a command with an exit-code contract, so a
budget overrun fails CI instead of being noticed after a board is bricked.

Real GNU ld layout this parser expects::

    Memory Configuration

    Name             Origin             Length             Attributes
    FLASH            0x08000000         0x00010000         xr
    RAM              0x20000000         0x00005000         xrw

    Linker script and memory map

    .text           0x08000100          0x2a0
     *(.text*)
     .text.main     0x08000100          0x40    build/main.o

The output-section header carries the address and size; the indented object lines
below it carry one symbol each and feed the "top spenders" list.

Only what the map file states is reported. A map file this parser cannot
recognise is a configuration error (exit 6), never a zeroed report.

Exit codes: 0 within budget, 1 over budget or growth regression, 6 unusable
input (missing/unrecognised map, bad budget value).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_BUDGET = 1
EXIT_CONFIG = 6

# Flash-resident: vector table, code, constants, and the load image of .data.
FLASH_SECTIONS = (".isr_vector", ".text", ".rodata", ".data", ".init", ".fini", ".ARM.exidx", ".ARM.extab")
# RAM-resident at run time: initialised data (copied from flash) and zeroed data.
RAM_SECTIONS = (".data", ".bss", ".noinit", ".heap", ".stack", "COMMON", ".stack_dummy")

MAP_MARKERS = (
    "Linker script and memory map",
    "Memory Configuration",
    "Archive member included",
    "Discarded input sections",
)

# "FLASH            0x08000000         0x00010000         xr"
MEMORY_ROW_RE = re.compile(
    r"^(?P<name>\S+)\s+0x(?P<origin>[0-9a-fA-F]+)\s+0x(?P<length>[0-9a-fA-F]+)(?P<rest>.*)$"
)
# ".text           0x08000100          0x2a0"  (output section header, column 0)
SECTION_HEADER_RE = re.compile(
    r"^(?P<section>[.][\w.$]+)\s+0x(?P<address>[0-9a-fA-F]+)\s+0x(?P<size>[0-9a-fA-F]+)(?P<rest>.*)$"
)
# " .text.main     0x08000100          0x40    build/main.o"  (contributing object)
OBJECT_ROW_RE = re.compile(
    r"^\s+(?P<section>[.][\w.$]+)\s+0x(?P<address>[0-9a-fA-F]+)\s+0x(?P<size>[0-9a-fA-F]+)\s+(?P<source>\S+)\s*$"
)
# "                0x080002a0                _sidata = LOADADDR (.data)"
ASSIGNMENT_ROW_RE = re.compile(r"^\s{2,}0x(?P<address>[0-9a-fA-F]+)\s+(?P<expr>\S.*)$")
# " . = ALIGN (0x4)"
LOCATION_ROW_RE = re.compile(r"^\s+\.\s*=")


class MapError(Exception):
    """The map file cannot be interpreted; report it instead of guessing."""


def parse_size_value(value: str | int, *, field: str = "size") -> int:
    """Parse ``65536``, ``64K``, ``1M`` or ``0x10000`` into a byte count."""
    if isinstance(value, int):
        return value
    raw = str(value).strip().replace("_", "")
    if not raw:
        raise MapError(f"empty {field} value")
    multiplier = 1
    suffix = raw[-1:].upper()
    if suffix in {"K", "M"}:
        multiplier = 1024 if suffix == "K" else 1024 * 1024
        raw = raw[:-1].strip()
    try:
        base = 16 if raw.lower().startswith("0x") else 10
        number = int(raw, base)
    except ValueError as exc:
        raise MapError(f"cannot parse {field} value {value!r}") from exc
    if number < 0:
        raise MapError(f"{field} must not be negative: {value!r}")
    return number * multiplier


def is_gnu_map(text: str) -> bool:
    return any(marker in text for marker in MAP_MARKERS)


def parse_memory_configuration(lines: list[str]) -> dict[str, dict[str, int]]:
    """Read the ``Memory Configuration`` table into name -> {origin, length}."""
    regions: dict[str, dict[str, int]] = {}
    in_table = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Memory Configuration"):
            in_table = True
            continue
        if not in_table:
            continue
        if stripped.startswith("Linker script and memory map"):
            break
        if not stripped or stripped.startswith("Name"):
            continue
        match = MEMORY_ROW_RE.match(stripped)
        if not match:
            continue
        name = match.group("name")
        if name.startswith("*"):  # *default* placeholder region
            continue
        regions[name] = {
            "origin": int(match.group("origin"), 16),
            "length": int(match.group("length"), 16),
        }
    return regions


def parse_sections(lines: list[str]) -> dict[str, dict[str, Any]]:
    """Aggregate per output section: address, size, and contributing symbols.

    Only lines inside the ``Linker script and memory map`` block are considered,
    so the ``Discarded input sections`` list cannot inflate the totals.
    """
    sections: dict[str, dict[str, Any]] = {}
    in_map = False
    current: str | None = None
    for raw in lines:
        line = raw.rstrip("\n")
        if line.strip().startswith("Linker script and memory map"):
            in_map = True
            continue
        if not in_map:
            continue
        if not line.strip():
            continue
        header = SECTION_HEADER_RE.match(line)
        if header:
            current = header.group("section")
            entry = sections.setdefault(current, {"address": None, "size": 0, "symbols": []})
            entry["address"] = int(header.group("address"), 16)
            entry["size"] = max(entry["size"], int(header.group("size"), 16))
            continue
        if current is None or LOCATION_ROW_RE.match(line):
            continue
        symbol = OBJECT_ROW_RE.match(line)
        if symbol:
            size = int(symbol.group("size"), 16)
            if size:
                sections[current]["symbols"].append(
                    {
                        "name": symbol.group("section"),
                        "size": size,
                        "address": int(symbol.group("address"), 16),
                    }
                )
            continue
        # Remaining rows are linker-script assignments ("_sdata = .") or the
        # continuation of a symbol name written on its own line. They carry no
        # size, so they are not "top spenders" and are skipped rather than listed.
    return sections


def _matches(section: str, wanted: tuple[str, ...]) -> bool:
    for candidate in wanted:
        if section == candidate or section.startswith(candidate + "."):
            return True
    return False


def _usage(used: int, capacity: int) -> dict[str, Any]:
    if capacity <= 0:
        return {"used": used, "capacity": None, "free": None, "percent": None}
    return {
        "used": used,
        "capacity": capacity,
        "free": capacity - used,
        "percent": round(used * 100.0 / capacity, 2),
    }


def summarise(sections: dict[str, dict[str, Any]], regions: dict[str, dict[str, int]]) -> dict[str, Any]:
    flash_used = sum(entry["size"] for name, entry in sections.items() if _matches(name, FLASH_SECTIONS))
    ram_used = sum(entry["size"] for name, entry in sections.items() if _matches(name, RAM_SECTIONS))

    flash_capacity = sum(
        region["length"] for name, region in regions.items() if name.upper().startswith(("FLASH", "ROM"))
    )
    ram_capacity = sum(region["length"] for name, region in regions.items() if name.upper().startswith("RAM"))

    symbols = [
        {"section": name, **symbol} for name, entry in sections.items() for symbol in entry["symbols"]
    ]
    symbols.sort(key=lambda item: (-item["size"], item["name"]))

    section_rows = [
        {"name": name, "address": entry["address"], "size": entry["size"]}
        for name, entry in sorted(sections.items())
    ]

    return {
        "flash": _usage(flash_used, flash_capacity),
        "ram": _usage(ram_used, ram_capacity),
        "regions": regions,
        "sections": section_rows,
        "symbols": symbols,
    }


def build_payload(
    path: Path,
    *,
    top: int = 15,
    max_flash: int | None = None,
    max_ram: int | None = None,
    baseline: dict[str, Any] | None = None,
    max_growth: int = 0,
) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise MapError(f"cannot read map file {path}: {exc}") from exc
    if not text.strip():
        raise MapError(f"map file {path} is empty")
    if not is_gnu_map(text):
        raise MapError(
            f"{path} does not look like a GNU ld map file "
            "(no 'Linker script and memory map' / 'Memory Configuration'); refusing to guess"
        )

    lines = text.splitlines()
    regions = parse_memory_configuration(lines)
    sections = parse_sections(lines)
    if not sections:
        raise MapError(f"{path} has no recognisable output sections; refusing to invent sizes")

    summary = summarise(sections, regions)
    failures: list[dict[str, Any]] = []

    for label, used, limit in (
        ("flash", summary["flash"]["used"], max_flash),
        ("ram", summary["ram"]["used"], max_ram),
    ):
        if limit is not None and used > limit:
            failures.append({"gate": f"max_{label}", "used": used, "limit": limit, "over_by": used - limit})

    growth_report = None
    if baseline is not None:
        growth_report = []
        for key in ("flash", "ram"):
            before = int((baseline.get(key) or {}).get("used", 0))
            now = summary[key]["used"]
            delta = now - before
            growth_report.append({"metric": key, "baseline": before, "current": now, "delta": delta})
            if delta > max_growth:
                failures.append(
                    {"gate": f"growth_{key}", "delta": delta, "limit": max_growth, "over_by": delta - max_growth}
                )

    return {
        "schema_version": "map-report.v1",
        "source": {"path": str(path), "format": "gnu-ld-map"},
        **summary,
        "symbols": summary["symbols"][:top],
        "symbol_count": len(summary["symbols"]),
        "baseline": growth_report,
        "gates": {"max_flash": max_flash, "max_ram": max_ram, "max_growth": max_growth},
        "failures": failures,
        "status": "OVER_BUDGET" if failures else "WITHIN_BUDGET",
    }


def format_report(payload: dict[str, Any]) -> str:
    lines = [f"map: {payload['source']['path']}"]
    for label in ("flash", "ram"):
        row = payload[label]
        if row["capacity"] is None:
            lines.append(f"  {label:<6}{row['used']:>9} bytes  (no {label.upper()} region in map file)")
        else:
            lines.append(
                f"  {label:<6}{row['used']:>9} / {row['capacity']:<9} bytes"
                f"  {row['percent']:>6.2f}%  free {row['free']}"
            )
    if payload["failures"]:
        lines.append("")
        for failure in payload["failures"]:
            lines.append(f"  FAIL {failure['gate']}: over by {failure['over_by']} bytes")
    if payload["baseline"]:
        lines.append("")
        for row in payload["baseline"]:
            lines.append(f"  {row['metric']:<6} baseline {row['baseline']} -> {row['current']} ({row['delta']:+d})")
    if payload["symbols"]:
        lines.append("")
        lines.append(f"  top {len(payload['symbols'])} of {payload['symbol_count']} symbols by size:")
        for symbol in payload["symbols"]:
            lines.append(f"    {symbol['size']:>7}  {symbol['section']:<16}{symbol['name']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass

    parser = argparse.ArgumentParser(
        prog="map_parse.py",
        description=(
            "Report flash/RAM usage from a GNU ld map file, with optional budget and growth "
            "gates. Unrecognised input is a configuration error, never a zero report."
        ),
    )
    parser.add_argument("--map", dest="map_path", required=True, help="GNU ld .map file")
    parser.add_argument("--top", type=int, default=15, help="how many largest symbols to list (default 15)")
    parser.add_argument("--max-flash", help="flash budget, e.g. 64K / 1M / 65536 / 0x10000")
    parser.add_argument("--max-ram", help="RAM budget, e.g. 20K / 65536 / 0x10000")
    parser.add_argument("--baseline", help="baseline JSON written earlier by --write-baseline")
    parser.add_argument("--write-baseline", help="write current usage as a baseline JSON")
    parser.add_argument("--max-growth", default="0", help="allowed byte growth vs baseline (default 0)")
    parser.add_argument("--output", help="also write the JSON report to this path")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a text summary")
    parser.add_argument("--version", action="version", version="embedded-dev map_parse 0.8.0")
    args = parser.parse_args(argv)

    try:
        max_flash = parse_size_value(args.max_flash, field="--max-flash") if args.max_flash else None
        max_ram = parse_size_value(args.max_ram, field="--max-ram") if args.max_ram else None
        max_growth = parse_size_value(args.max_growth, field="--max-growth")
        baseline = None
        if args.baseline:
            baseline_path = Path(args.baseline)
            if not baseline_path.is_file():
                raise MapError(f"baseline not found: {baseline_path}")
            try:
                baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise MapError(f"baseline {baseline_path} is not valid JSON: {exc}") from exc
        payload = build_payload(
            Path(args.map_path),
            top=args.top,
            max_flash=max_flash,
            max_ram=max_ram,
            baseline=baseline,
            max_growth=max_growth,
        )
    except MapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.write_baseline:
        try:
            Path(args.write_baseline).write_text(rendered + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot write {args.write_baseline}: {exc}", file=sys.stderr)
            return EXIT_CONFIG
        print(f"baseline written: {args.write_baseline}")
    if args.output:
        try:
            Path(args.output).write_text(rendered + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot write {args.output}: {exc}", file=sys.stderr)
            return EXIT_CONFIG

    print(rendered if args.json else format_report(payload))
    return EXIT_BUDGET if payload["failures"] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())