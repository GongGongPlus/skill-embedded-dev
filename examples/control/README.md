# Control-loop examples

Offline fixtures for `embedded_dev.py tune`. They demonstrate the evidence
contract, not a tuning recipe: the tool computes metrics and rejects unbounded
steps, it never certifies convergence or safety.

## Files

- `response.csv` — 2 s step response (21 samples, `timestamp_ms,setpoint,input`
  plus `pwm` and gain columns). The tail settles at 30.02 with setpoint 30.
- `proposal.json` — a bounded PID update: `kp` 2.0 → 2.4 (step 0.4 ≤ 0.5),
  `ki` 0.5 → 0.55 (step 0.05 ≤ 0.1), with explicit min/max/max_step bounds.
- `thermal-demo.json` — first-order plant `dy/dt = (ambient + gain*u - y) / tau`
  with a parallel PID controller. Deterministic and noise-free on purpose.

## Commands

```bash
python scripts/embedded_dev.py tune analyze --csv examples/control/response.csv
python scripts/embedded_dev.py tune check-update --file examples/control/proposal.json
python scripts/embedded_dev.py tune simulate --config examples/control/thermal-demo.json --out build/thermal.csv
```

`tune analyze` reports IAE/MAE, tail bias, overshoot, rise/settling time and
warnings. `check-update` accepts only proposals that are inside `[min,max]` and
within `max_step`; an out-of-bounds proposal is rejected as written, never
silently clipped into a different experiment. `tune simulate` writes the
telemetry CSV so it can be fed back into `tune analyze`.

Expected results for the bundled fixtures:

- `analyze`: `mae` 1.2685, `tail_bias` -0.02, `overshoot_ratio` 0.08,
  `rise_time_s` 0.3, `settling_time_s` 0.9.
- `check-update`: accepted, no violations.
- `simulate`: 401 samples over 40 s for the bundled config.

A pass here means the arithmetic and the bounds held. It does not mean the
loop is stable on real hardware: re-run the experiment on the target and record
`source: measured_hardware` before claiming anything about the physical system.
