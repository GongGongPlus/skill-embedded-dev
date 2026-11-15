# Examples

These are runnable reference projects and field-case walkthroughs. They are not
part of the installed skill runtime (`scripts/install.py` copies only
`SKILL.md`, `README.md`, `LICENSE`, `VERSION`, `references/`, `scripts/`), so they
can stay as teaching material for the repository.

| Path | What it demonstrates |
|---|---|
| `stm32-openocd/` | Bare-metal STM32F103C8 build with CMake + GNU Arm toolchain, OpenOCD flash, LED smoke test, register read-back |
| `protocols/` | `serial_protocol.v1` UART specification, a capture, and a candump log decoded as ISO-TP/UDS/OBD-II/J1939 |
| `cases/01-hardfault-bfar.md` | BusFault capture and root cause from CFSR/BFAR, with evidence levels |
| `cases/02-uart-token-hil.md` | Hardware-in-the-loop pass/fail using a UART token and exit codes |
| `cases/03-i2c-bus-dead.md` | Dead I2C bus triage: pull-ups, clock stretching, bus recovery |

## The STM32 example in one screen

```bash
# 0) prerequisites: arm-none-eabi-gcc, cmake, OpenOCD with a CMSIS-DAP or ST-Link probe
python scripts/embedded_dev.py env --json          # what is actually installed

# 1) build (the CLI calls the same cmake you would type)
python scripts/embedded_dev.py build --project examples/stm32-openocd \
  --backend cmake --build-dir build --dry-run      # review, then drop --dry-run

# 2) flash (explicit interface/target; no guessing when several probes exist)
python scripts/embedded_dev.py flash --image build/firmware.elf \
  --backend openocd --openocd-interface interface/cmsis-dap.cfg \
  --openocd-target target/stm32f1x.cfg --dry-run
```

If no probe or toolchain is present, every command exits with code 6
(`EXIT_CONFIG`) instead of inventing a command line. That is deliberate: a
guessed OpenOCD invocation can brick a board or erase the wrong flash bank.

## Case study format

Each case is written as: symptom → what was captured → analysis → root cause →
fix → verification → evidence level. Copy this format into your own completion
reports; it is the same contract `scripts/embedded_dev.py report` validates.
