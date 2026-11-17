#!/usr/bin/env python3
""".text/.data/.bss budget gate with optional baseline regression detection.

`record flash/RAM usage` is a MUST in SKILL.md, and a size regression is usually
noticed only after the image stops fitting. This module makes the check a command
with an exit code, so it can gate CI.

Two input paths:
- live: run ``arm-none-eabi-size -A <elf>`` (falls back to ``size``), so the
  number is measured from the artefact actually built;
- offline: ``--size-output-file`` parses a saved ``size -A`` transcript, which is
  what the test suite uses and what a CI box without a toolchain can consume.

Exit codes: 0 within budget, 1 over budget or growth regression, 6 configuration
error (no size tool and no transcript, unparsable input).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_BUDGET = 1
EXIT_CONFIG = 6

SIZE_TOOLS = ("arm-none-eabi-size", "size")
# Sections that make up the three classic buckets reported by GNU size.
TEXT_SECTIONS = (".isr_vector", ".text", ".rodata", ".ARM.exidx", ".ARM.extab", ".init", ".fini")
DATA_SECTIONS = (".data", ".data*", ".tdata")
BSS_SECTIONS = (".bss", ".bss*", ".tbss", ".noinit", ".heap", ".stack")


class SizeError(Exception):
    """The size input cannot be used; report instead of guessing."""


def parse_size_value(value: str | int, *, field: str = "size") -> int:
    if isinstance(value, int):
        return value
    raw = str(value).strip().replace("_", "")
    if not raw:
        raise SizeError(f"empty {field} value")
    multiplier = 1
    suffix = raw[-1:].upper()
    if suffix in {"K", "M"}:
        multiplier = 1024 if suffix == "K" else 1024 * 1024
        raw = raw[:-1].strip()
    try:
        number = int(raw, 16 if raw.lower().startswith("0x") else 10)
    except ValueError as exc:
        raise SizeError(f"cannot parse {field} value {value!r}") from exc
    if number < 0:
        raise SizeError(f"{field} must not be negative")
    return number * multiplier


def parse_size_output(text: str) -> dict[str, int]:
    """Parse ``size -A`` output: one ``SECTION  SIZE  ADDR`` row per section."""
    sections: dict[str, int] = {}
    saw_header = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("section") and "size" in stripped:
            saw_header = True
            continue
        parts = stripped.split()
        if len(parts) < 2:
            continue
        name = parts[0]
        if not (name.startswith(".") or name == "Total"):
            continue
        try:
            size = int(parts[1], 0)
        except ValueError:
            continue
        sections[name] = size
    if not sections:
        raise SizeError(
            "no section rows found; expected `size -A` output "
            "(section  size  addr per line)"
        )
    if not saw_header and "Total" not in sections:
        # Rows parsed but nothing looks like a size table: refuse rather than half-report.
        raise SizeError("input has section-like rows but no size table header or Total row")
    return sections


def summarise(sections: dict[str, int]) -> dict[str, int]:
    """Bucket sections into text/data/bss the way GNU size reports them."""
    text = 0
    data = 0
    bss = 0
    for name, size in sections.items():
        if name == "Total":
            continue
        if name in TEXT_SECTIONS:
            text += size
        elif name in DATA_SECTIONS:
            data += size
        elif name in BSS_SECTIONS:
            bss += size
    return {"text": text, "data": data, "bss": bss, "ram": data + bss, "flash": text + data}


def read_size_output(args: argparse.Namespace) -> tuple[dict[str, int], str]:
    if args.size_output_file:
        path = Path(args.size_output_file)
        if not path.is_file():
            raise SizeError(f"size output file not found: {path}")
        return parse_size_output(path.read_text(encoding="utf-8", errors="replace")), str(path)

    if not args.elf:
        raise SizeError("give --elf (live measurement) or --size-output-file (offline transcript)")
    elf = Path(args.elf)
    if not elf.is_file():
        raise SizeError(f"ELF not found: {elf}")

    tool = None
    for candidate in SIZE_TOOLS:
        found = shutil.which(candidate)
        if found:
            tool = found
            break
    if tool is None:
        raise SizeError(
            "no size tool found (looked for arm-none-eabi-size, size); "
            "install the GNU Arm toolchain or pass --size-output-file"
        )
    proc = subprocess.run(
        [tool, "-A", str(elf)], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if proc.returncode != 0:
        raise SizeError(f"{Path(tool).name} failed (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-300:]}")
    return parse_size_output(proc.stdout or ""), str(elf)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass

    parser = argparse.ArgumentParser(
        prog="size_gate.py",
        description=(
            "Check text/data/bss budgets from an ELF or a saved `size -A` transcript, and "
            "detect growth against a recorded baseline."
        ),
    )
    parser.add_argument("--elf", help="ELF to measure with arm-none-eabi-size / size")
    parser.add_argument("--size-output-file", dest="size_output_file", help="offline: saved `size -A` text")
    parser.add_argument("--max-text", help="text budget, e.g. 64K / 65536 / 0x10000")
    parser.add_argument("--max-data", help="data budget")
    parser.add_argument("--max-bss", help="bss budget")
    parser.add_argument("--baseline", help="baseline JSON written by --write-baseline")
    parser.add_argument("--write-baseline", help="write current sizes as a baseline JSON")
    parser.add_argument("--max-growth", default="0", help="allowed bytes of growth per bucket (default 0)")
    parser.add_argument("--output", help="also write the JSON result to this path")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a text summary")
    parser.add_argument("--version", action="version", version="embedded-dev size_gate 0.8.0")
    args = parser.parse_args(argv)

    try:
        sections, source = read_size_output(args)
        totals = summarise(sections)
        max_text = parse_size_value(args.max_text, field="--max-text") if args.max_text else None
        max_data = parse_size_value(args.max_data, field="--max-data") if args.max_data else None
        max_bss = parse_size_value(args.max_bss, field="--max-bss") if args.max_bss else None
        max_growth = parse_size_value(args.max_growth, field="--max-growth")

        baseline = None
        if args.baseline:
            path = Path(args.baseline)
            if not path.is_file():
                raise SizeError(f"baseline not found: {path}")
            try:
                baseline = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise SizeError(f"baseline {path} is not valid JSON: {exc}") from exc
    except SizeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    failures: list[dict[str, Any]] = []
    for label, limit in (("text", max_text), ("data", max_data), ("bss", max_bss)):
        if limit is not None and totals[label] > limit:
            failures.append({"gate": f"max_{label}", "used": totals[label], "limit": limit, "over_by": totals[label] - limit})

    growth = None
    if baseline is not None:
        growth = []
        recorded = baseline.get("totals") or baseline
        for label in ("text", "data", "bss"):
            before = int(recorded.get(label, 0))
            delta = totals[label] - before
            growth.append({"bucket": label, "baseline": before, "current": totals[label], "delta": delta})
            if delta > max_growth:
                failures.append({"gate": f"growth_{label}", "delta": delta, "limit": max_growth, "over_by": delta - max_growth})

    payload = {
        "schema_version": "size-report.v1",
        "source": source,
        "sections": dict(sorted(sections.items())),
        "totals": totals,
        "gates": {"max_text": max_text, "max_data": max_data, "max_bss": max_bss, "max_growth": max_growth},
        "growth": growth,
        "failures": failures,
        "status": "OVER_BUDGET" if failures else "WITHIN_BUDGET",
    }
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

    if args.json:
        print(rendered)
    else:
        print(f"source: {source}")
        for label in ("text", "data", "bss"):
            limit = payload["gates"][f"max_{label}"]
            limit_text = f" / {limit}" if limit is not None else ""
            print(f"  {label:<5}{totals[label]:>8}{limit_text} bytes")
        print(f"  {'ram':<5}{totals['ram']:>8} bytes (data + bss)")
        for row in growth or []:
            print(f"  {row['bucket']:<5} baseline {row['baseline']} -> {row['current']} ({row['delta']:+d})")
        for failure in failures:
            print(f"  FAIL {failure['gate']}: over by {failure['over_by']} bytes")
    return EXIT_BUDGET if failures else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())