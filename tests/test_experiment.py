"""Offline tests for experiment.py: telemetry analysis, bounded updates and trial records.

The tool reports evidence only; these tests assert the arithmetic, the rejection
rules and the "never certify convergence" contract, not tuning quality.
"""

from __future__ import annotations

import importlib.util
import json
import math
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples" / "control"


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, REPO / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


exp = load_module("experiment_tests", "scripts/experiment.py")

CSV_HEADER = "timestamp_ms,setpoint,input\n"


def make_csv(body: str, header: str = CSV_HEADER) -> str:
    return header + body


class TestParseCsv(unittest.TestCase):
    def test_parses_bundled_example(self):
        rows = exp.parse_csv((EXAMPLES / "response.csv").read_text(encoding="utf-8"))
        self.assertEqual(len(rows), 21)
        self.assertEqual(rows[0]["timestamp_ms"], 0.0)
        self.assertEqual(rows[-1]["timestamp_ms"], 2000.0)
        self.assertEqual(rows[-1]["setpoint"], 30.0)

    def test_accepts_bom_and_crlf(self):
        text = "\ufeff" + "timestamp_ms,setpoint,input\r\n0,10,0\r\n100,10,5\r\n"
        rows = exp.parse_csv(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["input"], 5.0)

    def test_missing_required_column_is_an_error(self):
        with self.assertRaises(exp.DataError):
            exp.parse_csv(make_csv("0,10\n100,10\n", header="timestamp_ms,setpoint\n"))

    def test_unparsable_value_is_an_error(self):
        with self.assertRaises(exp.DataError):
            exp.parse_csv(make_csv("0,10,abc\n"))

    def test_nan_is_an_error(self):
        with self.assertRaises(exp.DataError):
            exp.parse_csv(make_csv("0,10,nan\n"))

    def test_duplicate_timestamp_is_an_error(self):
        with self.assertRaises(exp.DataError):
            exp.parse_csv(make_csv("0,10,0\n0,10,1\n"))

    def test_rows_are_sorted_by_timestamp(self):
        rows = exp.parse_csv(make_csv("100,10,0\n50,10,1\n"))
        self.assertEqual([row["timestamp_ms"] for row in rows], [50.0, 100.0])


class TestSegmentsAndMetrics(unittest.TestCase):
    def test_setpoint_change_splits_segments(self):
        rows = exp.parse_csv(make_csv("0,10,0\n100,10,1\n200,20,2\n300,20,3\n"))
        segments, warnings = exp.split_segments(rows)
        self.assertEqual(len(segments), 2)
        self.assertEqual(warnings, [])

    def test_tracking_error_is_zero_for_perfect_following(self):
        rows = exp.parse_csv(make_csv("0,10,10\n100,10,10\n200,10,10\n"))
        metrics = exp.compute_metrics(rows)
        self.assertEqual(metrics["samples"], 3)
        self.assertAlmostEqual(metrics["mae"], 0.0)
        self.assertAlmostEqual(metrics["iae"], 0.0)
        self.assertAlmostEqual(metrics["tail_bias"], 0.0)
        self.assertIsNone(metrics["overshoot_ratio"])

    def test_mae_and_iae_are_trapezoidal(self):
        # errors are 2,2,0,0: IAE trapezoids to 0.3, MAE is the per-sample mean 1.0
        rows = exp.parse_csv(make_csv("0,10,8\n100,10,8\n200,10,10\n300,10,10\n"))
        metrics = exp.compute_metrics(rows)
        self.assertAlmostEqual(metrics["iae"], 0.3)
        self.assertAlmostEqual(metrics["mae"], 1.0)
        self.assertAlmostEqual(metrics["duration_s"], 0.3)

    def test_overshoot_is_relative_to_initial_amplitude(self):
        rows = exp.parse_csv(make_csv("0,100,0\n100,100,60\n200,100,120\n300,100,100\n"))
        metrics = exp.compute_metrics(rows)
        self.assertAlmostEqual(metrics["overshoot_ratio"], 0.2)
        self.assertGreater(metrics["rise_time_s"], 0.0)

    def test_saturation_is_null_without_declared_limits(self):
        rows = exp.parse_csv((EXAMPLES / "response.csv").read_text(encoding="utf-8"))
        self.assertIsNone(exp.compute_metrics(rows)["saturation_fraction"])
        bounded = exp.compute_metrics(rows, control_limits=(0.0, 1.0))
        self.assertIsNotNone(bounded["saturation_fraction"])

    def test_analysis_of_bundled_example(self):
        payload = exp.analyze_file(EXAMPLES / "response.csv")
        self.assertEqual(payload["schema_version"], "control-experiment.v1")
        self.assertEqual(len(payload["segments"]), 1)
        metrics = payload["segments"][0]["metrics"]
        self.assertAlmostEqual(metrics["mae"], 1.2685)
        self.assertAlmostEqual(metrics["tail_bias"], -0.02)
        self.assertIn("does not prove convergence", payload["claims"])


class TestCheckUpdate(unittest.TestCase):
    def setUp(self):
        self.proposal = json.loads((EXAMPLES / "proposal.json").read_text(encoding="utf-8"))

    def test_bundled_proposal_is_accepted(self):
        result = exp.check_update(self.proposal)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["violations"], [])
        self.assertIsNone(result["rejected_reason"])

    def test_step_larger_than_max_step_is_rejected_not_clipped(self):
        document = {
            "current": {"kp": 2.0},
            "proposed": {"kp": 9.0},
            "bounds": {"kp": {"min": 0.5, "max": 10.0, "max_step": 0.5}},
        }
        result = exp.check_update(document)
        self.assertFalse(result["accepted"])
        rules = {violation["rule"] for violation in result["violations"]}
        self.assertIn("max-step", rules)
        self.assertIn("do not silently clip", result["rejected_reason"])

    def test_proposed_value_outside_bounds_is_rejected(self):
        document = {
            "current": {"ki": 0.5},
            "proposed": {"ki": 0.9},
            "bounds": {"ki": {"min": 0.0, "max": 0.8, "max_step": 0.5}},
        }
        result = exp.check_update(document)
        self.assertFalse(result["accepted"])
        self.assertIn("proposed-within-bounds", {v["rule"] for v in result["violations"]})

    def test_current_outside_bounds_is_rejected(self):
        document = {
            "current": {"kp": 99.0},
            "proposed": {"kp": 99.1},
            "bounds": {"kp": {"min": 0.0, "max": 10.0, "max_step": 1.0}},
        }
        result = exp.check_update(document)
        self.assertFalse(result["accepted"])
        self.assertIn("current-within-bounds", {v["rule"] for v in result["violations"]})

    def test_non_finite_value_is_rejected(self):
        document = {
            "current": {"kp": 2.0},
            "proposed": {"kp": math.inf},
            "bounds": {"kp": {"min": 0.0, "max": 10.0, "max_step": 1.0}},
        }
        with self.assertRaises(exp.DataError):
            exp.check_update(document)

    def test_boolean_value_is_rejected(self):
        document = {
            "current": {"kp": 2.0},
            "proposed": {"kp": True},
            "bounds": {"kp": {"min": 0.0, "max": 10.0, "max_step": 1.0}},
        }
        with self.assertRaises(exp.DataError):
            exp.check_update(document)

    def test_mismatched_parameter_sets_are_rejected(self):
        document = {
            "current": {"kp": 2.0},
            "proposed": {"kp": 2.4, "ki": 0.55},
            "bounds": {"kp": {"min": 0.0, "max": 10.0, "max_step": 1.0}},
        }
        with self.assertRaises(exp.DataError):
            exp.check_update(document)


class TestSimulate(unittest.TestCase):
    def test_simulation_produces_telemetry_and_metrics(self):
        config = json.loads((EXAMPLES / "thermal-demo.json").read_text(encoding="utf-8"))
        text, payload = exp.simulate(config)
        lines = text.strip().splitlines()
        self.assertTrue(lines[0].startswith("timestamp_ms,setpoint,input"))
        self.assertEqual(len(lines), 1 + payload["metrics"]["samples"])
        self.assertEqual(payload["schema_version"], "thermal-demo.v1")
        self.assertGreater(payload["metrics"]["samples"], 100)

    def test_deterministic_output(self):
        config = json.loads((EXAMPLES / "thermal-demo.json").read_text(encoding="utf-8"))
        first, _ = exp.simulate(config)
        second, _ = exp.simulate(config)
        self.assertEqual(first, second)

    def test_non_integer_intervals_are_rejected(self):
        config = {
            "plant": {"ambient": 20.0, "gain": 10.0, "tau_s": 5.0},
            "controller": {
                "kp": 1.0,
                "ki": 0.0,
                "kd": 0.0,
                "setpoint": 30.0,
                "sample_time_s": 0.1,
                "duration_s": 2.05,
                "output_min": -100.0,
                "output_max": 100.0,
            },
        }
        with self.assertRaises(exp.DataError):
            exp.simulate(config)


class TestValidateRecord(unittest.TestCase):
    def valid_record(self):
        return {
            "schema_version": "trial-record.v1",
            "trial": 1,
            "source": "measured_hardware",
            "data_file": "telemetry.csv",
            "controller_form": "parallel-pid",
            "conditions": {"mode": "heating"},
            "applied_parameters": {"kp": 2.4},
            "readback_confirmed": True,
            "metrics": {"mae": 1.2},
            "acceptance": "accepted",
            "decision": "apply",
            "reason": "bounded step, metrics recorded",
            "best_verified_trial": 0,
            "next_proposal": "hold",
        }

    def test_valid_record(self):
        result = exp.validate_record(self.valid_record())
        self.assertTrue(result["valid"])
        self.assertEqual(result["violations"], [])

    def test_converged_claim_is_a_violation(self):
        record = self.valid_record()
        record["converged"] = True
        result = exp.validate_record(record)
        self.assertFalse(result["valid"])
        self.assertIn("no-convergence-claims", {v["rule"] for v in result["violations"]})

    def test_missing_required_field(self):
        record = self.valid_record()
        del record["readback_confirmed"]
        result = exp.validate_record(record)
        self.assertFalse(result["valid"])

    def test_apply_without_reason_is_rejected(self):
        record = self.valid_record()
        record["decision"] = "apply"
        record["reason"] = ""
        result = exp.validate_record(record)
        self.assertFalse(result["valid"])

    def test_simulation_only_apply_is_a_warning(self):
        record = self.valid_record()
        record["source"] = "simulation"
        result = exp.validate_record(record)
        self.assertTrue(result["valid"])
        self.assertTrue(any("hardware verification" in warning for warning in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
