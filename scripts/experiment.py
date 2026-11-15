#!/usr/bin/env python3
"""Control-loop experiment tools: telemetry analysis, bounded PID updates,
a first-order demo simulator and continuation-record validation.

The design follows one rule: report measurements, never certify a loop.
Every metric this tool prints is derived from the CSV it was given; it does
not claim that a controller converged, is stable in production, or is safe.

Exit codes:
    0  success (analysis produced, proposal accepted, record valid)
    4  data rejected (invalid CSV, rejected update, invalid record)
    6  configuration error (missing file, malformed JSON, bad option)
"""

from __future__ import annotations

import csv
import io
import json
import math
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_DATA = 4
EXIT_CONFIG = 6

SCHEMA_VERSION = "control-experiment.v1"
PROPOSAL_SCHEMA = "bounded-update.v1"
RECORD_SCHEMA = "trial-record.v1"
SIM_SCHEMA = "thermal-demo.v1"

REQUIRED_COLUMNS = ("timestamp_ms", "setpoint", "input")
OPTIONAL_COLUMNS = ("session_id", "mode", "round", "pwm", "p", "i", "d", "p2", "i2", "d2")
GAIN_COLUMNS = ("p", "i", "d", "p2", "i2", "d2")
SEGMENT_KEYS = ("session_id", "mode", "round", "setpoint") + GAIN_COLUMNS

UPDATE_SOURCES = ("measured_hardware", "simulation", "offline_log", "unknown")
ACCEPTANCE = ("accepted", "needs_longer_observation", "rejected", "unknown")
DECISIONS = ("hold", "apply", "rollback", "stop")
RECORD_REQUIRED = (
    "trial",
    "source",
    "controller_form",
    "conditions",
    "applied_parameters",
    "readback_confirmed",
    "metrics",
    "acceptance",
    "decision",
    "reason",
)


class DataError(Exception):
    """Input data is well-formed enough to read but must not be trusted."""


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Telemetry CSV
# ---------------------------------------------------------------------------


def _to_float(row: dict[str, str], key: str, row_number: int) -> float | None:
    raw = (row.get(key) or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise DataError(f"row {row_number}: column '{key}' is not a number: {raw!r}") from exc
    if not math.isfinite(value):
        raise DataError(f"row {row_number}: column '{key}' is not finite: {raw!r}")
    return value


def parse_csv(text: str) -> list[dict[str, Any]]:
    """Parse telemetry CSV. Missing/NaN/duplicate timestamps are data errors."""
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))
    rows = list(reader)
    if not rows:
        raise DataError("CSV is empty")
    header = [cell.strip() for cell in rows[0]]
    if len(set(header)) != len(header):
        raise DataError("CSV header contains duplicate column names")
    missing = [name for name in REQUIRED_COLUMNS if name not in header]
    if missing:
        raise DataError(f"CSV is missing required columns: {', '.join(missing)}")
    index = {name: position for position, name in enumerate(header)}
    records: list[dict[str, Any]] = []
    for offset, row in enumerate(rows[1:], start=2):
        if not row or all(not cell.strip() for cell in row):
            continue
        if len(row) != len(header):
            raise DataError(f"row {offset}: expected {len(header)} columns, got {len(row)}")
        record: dict[str, Any] = {}
        for name in header:
            raw = row[index[name]].strip()
            record[name] = raw if raw else None
        record["_row"] = offset
        records.append(record)
    if not records:
        raise DataError("CSV has a header but no data rows")
    parsed: list[dict[str, Any]] = []
    seen_ts: set[int] = set()
    for record in records:
        timestamp = _to_float(record, "timestamp_ms", record["_row"])
        assert timestamp is not None
        if timestamp != int(timestamp):
            raise DataError(f"row {record['_row']}: timestamp_ms must be an integer number of ms")
        timestamp = int(timestamp)
        if timestamp in seen_ts:
            raise DataError(f"row {record['_row']}: duplicate timestamp_ms {timestamp}")
        seen_ts.add(timestamp)
        item: dict[str, Any] = {"timestamp_ms": timestamp, "_row": record["_row"]}
        for name in header:
            if name == "timestamp_ms":
                continue
            if name in ("session_id", "mode", "round"):
                item[name] = record.get(name)
                continue
            if name not in REQUIRED_COLUMNS and name not in OPTIONAL_COLUMNS:
                continue
            item[name] = _to_float(record, name, record["_row"])
        parsed.append(item)
    parsed.sort(key=lambda item: item["timestamp_ms"])
    return parsed


def _carry_forward(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fill sparse session/mode/round/setpoint/gain cells from earlier rows."""
    carried: dict[str, Any] = {}
    filled: list[dict[str, Any]] = []
    for row in rows:
        current = dict(row)
        for key in SEGMENT_KEYS:
            value = current.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                if key in carried:
                    current[key] = carried[key]
            else:
                carried[key] = value
        if current.get("setpoint") is None:
            raise DataError("the first row must carry a setpoint (sparse carry-forward cannot start empty)")
        filled.append(current)
    return filled


def split_segments(rows: Sequence[dict[str, Any]]) -> tuple[list[list[dict[str, Any]]], list[str]]:
    """Split telemetry into independent observation windows."""
    filled = _carry_forward(rows)
    segments: list[list[dict[str, Any]]] = []
    warnings: list[str] = []
    current: list[dict[str, Any]] = []
    for row in filled:
        if not current:
            current = [row]
            continue
        previous = current[-1]
        reason = None
        if row["timestamp_ms"] < previous["timestamp_ms"]:
            reason = "time moved backwards"
            warnings.append(
                f"row {row.get('_row')}: timestamp moved backwards; starting a new segment"
            )
        else:
            for key in SEGMENT_KEYS:
                if row.get(key) != previous.get(key):
                    reason = f"'{key}' changed"
                    break
        if reason:
            segments.append(current)
            current = [row]
        else:
            current.append(row)
    if current:
        segments.append(current)
    return segments, warnings


def _trapezoid(points: Sequence[tuple[float, float]]) -> float:
    total = 0.0
    for (t0, v0), (t1, v1) in zip(points, points[1:], strict=False):
        total += (t1 - t0) * (v0 + v1) / 2.0
    return total


def compute_metrics(
    segment: Sequence[dict[str, Any]],
    *,
    control_limits: tuple[float, float] | None = None,
    settling_band: float | None = None,
    tail_fraction: float = 0.2,
) -> dict[str, Any]:
    """Compute response metrics for one segment. Undefined values are null."""
    if not 0.0 < tail_fraction < 1.0:
        raise ValueError("tail fraction must be between 0 and 1")
    start_ms = segment[0]["timestamp_ms"]
    times = [(row["timestamp_ms"] - start_ms) / 1000.0 for row in segment]
    duration = times[-1]
    metrics: dict[str, Any] = {
        "samples": len(segment),
        "duration_s": round(duration, 6),
        "setpoint": segment[0]["setpoint"],
    }
    warnings: list[str] = []
    if duration <= 0 or len(segment) < 2:
        metrics.update(
            {
                "mae": None,
                "iae": None,
                "tail_mae": None,
                "tail_bias": None,
                "tail_peak_to_peak": None,
                "overshoot_ratio": None,
                "rise_time_s": None,
                "settling_time_s": None,
                "saturation_fraction": None,
            }
        )
        warnings.append("segment is too short to integrate")
        metrics["warnings"] = warnings
        return metrics

    errors = [row["setpoint"] - row["input"] for row in segment]
    absolute = [(t, abs(e)) for t, e in zip(times, errors, strict=True)]
    iae = _trapezoid(absolute)
    metrics["iae"] = round(iae, 6)
    metrics["mae"] = round(iae / duration, 6)

    tail_start = duration * (1.0 - tail_fraction)
    tail_pairs = []
    for (t0, e0), (t1, e1) in zip(
        zip(times, errors, strict=True), zip(times[1:], errors[1:], strict=True), strict=False
    ):
        lo, hi = max(t0, tail_start), t1
        if hi <= lo:
            continue
        span = t1 - t0
        ratio_lo = (lo - t0) / span if span else 0.0
        ratio_hi = (hi - t0) / span if span else 0.0
        tail_pairs.append((hi - lo, e0 + (e1 - e0) * (ratio_lo + ratio_hi) / 2.0))
    tail_duration = sum(span for span, _ in tail_pairs)
    if tail_duration > 0:
        metrics["tail_bias"] = round(sum(span * e for span, e in tail_pairs) / tail_duration, 6)
        metrics["tail_mae"] = round(
            sum(span * abs(e) for span, e in tail_pairs) / tail_duration, 6
        )
    else:
        metrics["tail_bias"] = None
        metrics["tail_mae"] = None
        warnings.append("segment is shorter than the tail window")

    tail_inputs = [row["input"] for row in segment if (row["timestamp_ms"] - start_ms) / 1000.0 >= tail_start]
    if tail_inputs:
        metrics["tail_peak_to_peak"] = round(max(tail_inputs) - min(tail_inputs), 6)
    else:
        metrics["tail_peak_to_peak"] = None

    target = segment[0]["setpoint"]
    first_input = segment[0]["input"]
    amplitude = target - first_input
    if amplitude == 0:
        metrics["overshoot_ratio"] = None
        metrics["rise_time_s"] = None
        metrics["settling_time_s"] = None
        warnings.append("initial amplitude is zero; overshoot/rise/settling are undefined")
    else:
        if amplitude > 0:
            peak = max(row["input"] for row in segment)
            overshoot = max(0.0, (peak - target) / amplitude)
        else:
            trough = min(row["input"] for row in segment)
            overshoot = max(0.0, (target - trough) / (-amplitude))
        metrics["overshoot_ratio"] = round(overshoot, 6)
        metrics["rise_time_s"] = _rise_time(segment, times, first_input, amplitude)
        metrics["settling_time_s"] = _settling_time(
            segment, times, target, amplitude, settling_band
        )

    if control_limits is not None and any(row.get("pwm") is not None for row in segment):
        low, high = control_limits
        limited = 0.0
        total = 0.0
        for row in segment:
            pwm = row.get("pwm")
            if pwm is None:
                continue
            total += 1.0
            if pwm <= low + 1e-9 or pwm >= high - 1e-9:
                limited += 1.0
        metrics["saturation_fraction"] = round(limited / total, 6) if total else None
    else:
        metrics["saturation_fraction"] = None
        if control_limits is not None:
            warnings.append("control limits given but no pwm column was found")

    metrics["warnings"] = warnings
    return metrics


def _rise_time(
    segment: Sequence[dict[str, Any]], times: Sequence[float], first_input: float, amplitude: float
) -> float | None:
    low = first_input + 0.1 * amplitude
    high = first_input + 0.9 * amplitude
    low_at = None
    for row, t in zip(segment, times, strict=False):
        if low_at is None and (row["input"] >= low if amplitude > 0 else row["input"] <= low):
            low_at = t
        if low_at is not None and (row["input"] >= high if amplitude > 0 else row["input"] <= high):
            return round(t - low_at, 6)
    return None


def _settling_time(
    segment: Sequence[dict[str, Any]],
    times: Sequence[float],
    target: float,
    amplitude: float,
    settling_band: float | None,
) -> float | None:
    band = settling_band if settling_band is not None else 0.02 * abs(amplitude)
    if band <= 0:
        return None
    outside = [index for index, row in enumerate(segment) if abs(row["input"] - target) > band]
    if not outside:
        return 0.0
    last_outside = outside[-1]
    if last_outside == len(segment) - 1:
        return None
    return round(times[last_outside + 1], 6)


def analyze_file(
    path: Path,
    *,
    control_limits: tuple[float, float] | None = None,
    settling_band: float | None = None,
    tail_fraction: float = 0.2,
) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise FileNotFoundError(f"cannot read CSV: {exc}") from exc
    rows = parse_csv(text)
    segments, warnings = split_segments(rows)
    analyzed = []
    for index, segment in enumerate(segments, start=1):
        metrics = compute_metrics(
            segment,
            control_limits=control_limits,
            settling_band=settling_band,
            tail_fraction=tail_fraction,
        )
        analyzed.append(
            {
                "index": index,
                "first_row": segment[0].get("_row"),
                "last_row": segment[-1].get("_row"),
                "first_timestamp_ms": segment[0]["timestamp_ms"],
                "last_timestamp_ms": segment[-1]["timestamp_ms"],
                "metrics": metrics,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "file": str(path),
        "segments": analyzed,
        "warnings": warnings,
        "claims": "metrics only; this file does not prove convergence or safety",
    }


# ---------------------------------------------------------------------------
# Bounded PID update check
# ---------------------------------------------------------------------------


def check_update(document: Any) -> dict[str, Any]:
    """Validate a bounded PID proposal. Rejected updates are never clipped."""
    violations: list[dict[str, Any]] = []
    if not isinstance(document, dict):
        raise DataError("proposal must be a JSON object")
    for key in ("current", "proposed", "bounds"):
        if not isinstance(document.get(key), dict):
            raise DataError(f"proposal is missing an object field '{key}'")
    current: dict[str, Any] = document["current"]
    proposed: dict[str, Any] = document["proposed"]
    bounds: dict[str, Any] = document["bounds"]
    names = sorted(current)
    if names != sorted(proposed) or names != sorted(bounds):
        raise DataError("current, proposed and bounds must list exactly the same gain names")
    if not names:
        raise DataError("proposal lists no gains")
    for name in names:
        entry = bounds[name]
        if not isinstance(entry, dict):
            raise DataError(f"bounds for '{name}' must be an object")
        for key in ("min", "max", "max_step"):
            if key not in entry:
                raise DataError(f"bounds for '{name}' is missing '{key}'")
            if not _finite(entry[key]):
                raise DataError(f"bounds.{name}.{key} must be a finite number")
        low, high, step = entry["min"], entry["max"], entry["max_step"]
        if high < low:
            raise DataError(f"bounds for '{name}' have max < min")
        if step < 0:
            raise DataError(f"bounds for '{name}' have negative max_step")
        for table_name, table in (("current", current), ("proposed", proposed)):
            value = table[name]
            if not _finite(value):
                raise DataError(f"{table_name}.{name} must be a finite JSON number (not bool/string/null)")
        if not low <= current[name] <= high:
            violations.append(
                {
                    "parameter": name,
                    "rule": "current-within-bounds",
                    "message": f"current {current[name]} is outside [{low}, {high}]",
                }
            )
        if not low <= proposed[name] <= high:
            violations.append(
                {
                    "parameter": name,
                    "rule": "proposed-within-bounds",
                    "message": f"proposed {proposed[name]} is outside [{low}, {high}]",
                }
            )
        if abs(proposed[name] - current[name]) > step + 1e-12:
            violations.append(
                {
                    "parameter": name,
                    "rule": "max-step",
                    "message": (
                        f"step {proposed[name] - current[name]:+.6g} exceeds max_step {step}"
                    ),
                }
            )
    changes = {
        name: {
            "from": current[name],
            "to": proposed[name],
            "delta": round(proposed[name] - current[name], 12),
        }
        for name in names
    }
    accepted = not violations
    return {
        "schema_version": PROPOSAL_SCHEMA,
        "accepted": accepted,
        "violations": violations,
        "changes": changes,
        "rejected_reason": None
        if accepted
        else "proposal rejected as written; do not silently clip it into a different experiment",
        "claims": "accepted means the step is bounded; it does not mean the tuning result is good",
    }


# ---------------------------------------------------------------------------
# First-order demo simulator
# ---------------------------------------------------------------------------


def simulate(config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if not isinstance(config, dict):
        raise DataError("simulation config must be a JSON object")
    plant = config.get("plant")
    controller = config.get("controller")
    if not isinstance(plant, dict) or not isinstance(controller, dict):
        raise DataError("config must contain 'plant' and 'controller' objects")
    try:
        ambient = float(plant["ambient"])
        gain = float(plant["gain"])
        tau = float(plant["tau_s"])
        kp = float(controller["kp"])
        ki = float(controller["ki"])
        kd = float(controller["kd"])
        setpoint = float(controller["setpoint"])
        dt = float(controller["sample_time_s"])
        duration = float(controller["duration_s"])
        output_min = float(controller.get("output_min", -1e9))
        output_max = float(controller.get("output_max", 1e9))
        initial_input = float(controller.get("initial_input", ambient))
    except KeyError as exc:
        raise DataError(f"config is missing required key: {exc}") from exc
    except (TypeError, ValueError) as exc:
        raise DataError(f"config value is not numeric: {exc}") from exc
    for label, value in (
        ("tau_s", tau),
        ("sample_time_s", dt),
        ("duration_s", duration),
        ("gain", gain),
        ("ambient", ambient),
        ("kp", kp),
        ("ki", ki),
        ("kd", kd),
        ("setpoint", setpoint),
        ("initial_input", initial_input),
        ("output_min", output_min),
        ("output_max", output_max),
    ):
        if not math.isfinite(value):
            raise DataError(f"config value '{label}' is not finite")
    if tau <= 0:
        raise DataError("tau_s must be positive")
    if dt <= 0:
        raise DataError("sample_time_s must be positive")
    if duration <= 0:
        raise DataError("duration_s must be positive")
    if output_max <= output_min:
        raise DataError("output_max must be greater than output_min")
    steps = duration / dt
    if abs(steps - round(steps)) > 1e-9:
        raise DataError("duration_s / sample_time_s must be an integer number of intervals")
    steps = int(round(steps))
    if steps > 100000:
        raise DataError("simulation would exceed 100000 intervals")

    temperature = initial_input
    integral = 0.0
    previous_measurement = temperature
    lines = ["timestamp_ms,setpoint,input,pwm,p,i,d"]
    buffer: list[dict[str, float]] = []
    for step in range(steps + 1):
        error = setpoint - temperature
        derivative = (temperature - previous_measurement) / dt if step else 0.0
        candidate = kp * error + ki * integral + kd * derivative
        if candidate > output_max:
            if error < 0:
                integral += error * dt
        elif candidate < output_min:
            if error > 0:
                integral += error * dt
        else:
            integral += error * dt
        output = kp * error + ki * integral + kd * derivative
        output = max(output_min, min(output_max, output))
        timestamp_ms = int(round(step * dt * 1000))
        lines.append(
            f"{timestamp_ms},{setpoint:.6g},{temperature:.6g},{output:.6g},"
            f"{kp:.6g},{ki:.6g},{kd:.6g}"
        )
        buffer.append({"temperature": temperature, "output": output})
        previous_measurement = temperature
        temperature += (ambient + gain * output - temperature) * (1.0 - math.exp(-dt / tau))
    rows = parse_csv("\n".join(lines))
    segments, warnings = split_segments(rows)
    metrics = compute_metrics(segments[0])
    payload = {
        "schema_version": SIM_SCHEMA,
        "config": {
            "plant": {"ambient": ambient, "gain": gain, "tau_s": tau},
            "controller": {
                "kp": kp,
                "ki": ki,
                "kd": kd,
                "setpoint": setpoint,
                "sample_time_s": dt,
                "duration_s": duration,
                "output_min": output_min,
                "output_max": output_max,
            },
        },
        "metrics": metrics,
        "warnings": warnings,
        "limits": [
            "first-order linear plant with an exact zero-order-hold integration",
            "no noise, friction, deadband, saturation dynamics or actuator delay",
            "simulation evidence is not hardware evidence",
        ],
        "claims": "simulation only; do not use it as a substitute for on-target verification",
    }
    return "\n".join(lines) + "\n", payload


# ---------------------------------------------------------------------------
# Continuation record
# ---------------------------------------------------------------------------


def validate_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise DataError("record must be a JSON object")
    violations: list[dict[str, Any]] = []
    warnings: list[str] = []
    for key in RECORD_REQUIRED:
        if key not in record or record[key] in (None, "", {}, []):
            violations.append({"field": key, "rule": "required", "message": f"missing '{key}'"})
    source = record.get("source")
    if source is not None and source not in UPDATE_SOURCES:
        violations.append(
            {
                "field": "source",
                "rule": "enum",
                "message": f"source must be one of {', '.join(UPDATE_SOURCES)}",
            }
        )
    acceptance = record.get("acceptance")
    if acceptance is not None and acceptance not in ACCEPTANCE:
        violations.append(
            {"field": "acceptance", "rule": "enum", "message": f"unknown acceptance {acceptance!r}"}
        )
    decision = record.get("decision")
    if decision is not None and decision not in DECISIONS:
        violations.append(
            {"field": "decision", "rule": "enum", "message": f"unknown decision {decision!r}"}
        )
    readback = record.get("readback_confirmed")
    if not isinstance(readback, bool):
        violations.append(
            {"field": "readback_confirmed", "rule": "boolean", "message": "must be true or false"}
        )
    metrics = record.get("metrics")
    if metrics is not None and not isinstance(metrics, dict):
        violations.append({"field": "metrics", "rule": "object", "message": "metrics must be an object"})
    if decision in ("apply", "rollback") and readback is not True:
        violations.append(
            {
                "field": "readback_confirmed",
                "rule": "readback-before-apply",
                "message": f"decision '{decision}' requires a confirmed readback of applied parameters",
            }
        )
    if decision in ("apply", "rollback") and not str(record.get("reason") or "").strip():
        violations.append(
            {"field": "reason", "rule": "decision-needs-reason", "message": f"decision '{decision}' needs a reason"}
        )
    if acceptance == "accepted" and not metrics:
        violations.append(
            {"field": "metrics", "rule": "accepted-needs-metrics", "message": "accepted requires recorded metrics"}
        )
    if source == "simulation" and decision == "apply":
        warnings.append("apply based on simulation only; hardware verification is still required")
    if record.get("converged") is True:
        violations.append(
            {
                "field": "converged",
                "rule": "no-convergence-claims",
                "message": "the assistant reports metrics; it does not certify convergence or safety",
            }
        )
    if record.get("safety_certified") is True:
        warnings.append("'safety_certified' is not a field this tool emits; treat it as unverified")
    return {
        "schema_version": RECORD_SCHEMA,
        "valid": not violations,
        "violations": violations,
        "warnings": warnings,
        "claims": "a valid record stores evidence; it does not assert that the loop is converged or safe",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _emit(payload: dict[str, Any], human: str) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise FileNotFoundError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DataError(f"{path} is not valid JSON: {exc}") from exc


def cmd_analyze(args: Any) -> int:
    limits = None
    if args.control_limits:
        try:
            limits = (float(args.control_limits[0]), float(args.control_limits[1]))
        except (TypeError, ValueError):
            print("error: --control-limits takes two numbers MIN MAX", file=sys.stderr)
            return EXIT_CONFIG
        if limits[1] <= limits[0]:
            print("error: --control-limits MAX must be greater than MIN", file=sys.stderr)
            return EXIT_CONFIG
    try:
        result = analyze_file(
            Path(args.csv),
            control_limits=limits,
            settling_band=args.settling_band,
            tail_fraction=args.tail_fraction,
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except DataError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_DATA
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_check_update(args: Any) -> int:
    try:
        result = check_update(_read_json(Path(args.file)))
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except DataError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_OK if result["accepted"] else EXIT_DATA


def cmd_simulate(args: Any) -> int:
    out = Path(args.out)
    if out.exists() and not args.force:
        print(f"error: refusing to overwrite {out} (pass --force)", file=sys.stderr)
        return EXIT_CONFIG
    try:
        csv_text, payload = simulate(_read_json(Path(args.config)))
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except DataError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        out.write_text(csv_text, encoding="utf-8", newline="")
    except OSError as exc:
        print(f"error: cannot write {out}: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    payload["csv"] = str(out)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_record(args: Any) -> int:
    try:
        result = validate_record(_read_json(Path(args.file)))
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except DataError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_OK if result["valid"] else EXIT_DATA


def build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="experiment.py",
        description=(
            "Control-loop experiment tools: turn telemetry into metrics, bound PID "
            "updates, simulate a first-order example and validate continuation records. "
            "This tool reports evidence; it never certifies convergence or safety."
        ),
    )
    parser.add_argument("--version", action="version", version="embedded-dev experiment 0.6.0")
    subparsers = parser.add_subparsers(dest="action", required=True)

    analyze = subparsers.add_parser(
        "analyze", help="compute response metrics from a telemetry CSV (one result per segment)"
    )
    analyze.add_argument("--csv", required=True, help="telemetry CSV (timestamp_ms,setpoint,input)")
    analyze.add_argument("--control-limits", nargs=2, metavar=("MIN", "MAX"), default=None)
    analyze.add_argument("--settling-band", type=float, default=None)
    analyze.add_argument("--tail-fraction", type=float, default=0.2)
    analyze.add_argument("--json", action="store_true", help="kept for interface consistency")

    check = subparsers.add_parser(
        "check-update", help="validate a bounded PID proposal (bounds, max_step, finite numbers)"
    )
    check.add_argument("--file", required=True, help="proposal JSON")
    check.add_argument("--json", action="store_true")

    sim = subparsers.add_parser(
        "simulate", help="run the bundled first-order thermal demo and write telemetry CSV"
    )
    sim.add_argument("--config", required=True, help="simulation config JSON")
    sim.add_argument("--out", required=True, help="output CSV path (refuses to overwrite)")
    sim.add_argument("--force", action="store_true", help="allow overwriting the output CSV")
    sim.add_argument("--json", action="store_true")

    record = subparsers.add_parser(
        "record", help="validate a compact continuation record before saving it"
    )
    record.add_argument("--file", required=True, help="trial record JSON")
    record.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    handlers = {
        "analyze": cmd_analyze,
        "check-update": cmd_check_update,
        "simulate": cmd_simulate,
        "record": cmd_record,
    }
    try:
        return handlers[args.action](args)
    except KeyboardInterrupt:
        return EXIT_DATA


if __name__ == "__main__":
    sys.exit(main())
