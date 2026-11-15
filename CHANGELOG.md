# Changelog

All notable changes to this repository are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Agent Skills are consumed by an LLM, so the version number also tells the agent
whether it is looking at the same contract as an earlier session.

## [0.5.0] - 2026-11-15

### Added

- `scripts/protocol_decode.py`: decode UART captures from an `embedded-protocol.v1`
  JSON specification (framing header, length field, checksum `none`/`sum8`/`xor8`/
  `crc8`/`crc16_modbus`/`crc16_ccitt`, typed fields, `scale`/`value_offset`, enums and
  bit fields, request/response commands), plus candump log decoding with ISO-TP
  multi-frame reassembly, UDS service/NRC names, OBD-II PIDs, and J1939 PGNs.
  Missing specification details stay `unknown`; a bad spec exits 6, decode errors exit 4
  under `--strict`.
- `scripts/mcp_server.py`: dependency-free stdio MCP server that exposes the existing
  `env`/`build`/`flash`/`monitor`/`regs`/`fault`/`decode`/`report`/`hil` tools over
  JSON-RPC, with `--self-test` for CI.
- `embedded_dev.py decode`: validates a protocol spec or decodes UART/CAN input.
- `embedded_dev.py hil`: build -> flash -> monitor pass token -> `diagnostic_report.json`;
  the report carries a stable failure `signature`, `next_actions`, `ai_repair_prompt`,
  `repeated_failure`, and `source_unchanged`. The same signature on unchanged sources is
  reported as `STALLED` (exit 5) instead of letting the agent edit code blindly.
- `embedded_dev.py fault/regs --elf`: resolve PC/LR through `addr2line` as a DWARF gate;
  a missing tool or ELF is a configuration error, not a guessed symbol.
- `scripts/diagnostic_report_schema.json`: reviewable contract for HIL reports.
- `references/protocols.md`: specification-driven UART framing, checksum and CAN
  ISO-TP/UDS/OBD-II/J1939 decoding rules, including the no-guess boundary.
- `examples/protocols/`: a validated UART sensor spec, a two-frame capture, a candump
  log with UDS and OBD-II traffic, and a README walkthrough.
- `tests/test_protocol_decode.py`, `tests/test_mcp_server.py`, `tests/test_hil.py`,
  plus lint coverage for the new CLI subcommands, diagnostic schema, bundled protocol
  examples, and the MCP self-test.

### Changed

- `SKILL.md` and `README.md` document `decode`, `hil`, the MCP server, and the corrected
  exit-code semantics (`1` build failed, `2` flash failed, `5` stalled).
- CI now runs the MCP self-test, decodes the bundled protocol examples, and dry-runs the
  HIL pipeline on all three operating systems.

### Notes

- Interface ideas were studied from public projects; no source code was copied.
  See `THIRD_PARTY_NOTICES.md` for the exact boundary.

## [0.4.0] - 2026-11-15

### Added

- `scripts/embedded_dev.py`: a dependency-free CLI with a stable exit-code
  contract (`0` ok, `1` tool error, `2` build failed, `3` fault detected,
  `4` device/test failed, `5` timeout, `6` config error) and subcommands:
  - `env` — detect tools, toolchains, flash/debug backends, serial ports; JSON output, optional `--strict`.
  - `build` — `cmake` / `platformio` / `idf.py` invocation, dry-run first.
  - `flash` — OpenOCD, pyOCD, J-Link, STM32CubeProgrammer, esptool, nrfjprog, probe-rs, PlatformIO; every backend requires its explicit identifying options and refuses to guess.
  - `monitor` — serial capture with a token assertion (`--expect`) and a `--mock-file` path for hardware-free tests.
  - `regs` — register dump parsing plus Cortex-M CFSR/HFSR decoding.
  - `fault` — CFSR/HFSR/MFAR/BFAR/PC/LR/PSP/MSP decode with next-step guidance.
  - `report` — validate the completion report contract (evidence levels, command results, risks, next step).
- `scripts/report_schema.json`: reviewable JSON Schema for the completion report.
- `scripts/install.py`: one-click installer/verifier for DSH, Codex, Claude Code, generic Agent Skills roots, and project-scoped roots; dry-run and JSON output.
- `references/board-pinouts.md`: board pinout and strapping-pin tables with a three-source verification rule.
- `references/riscv.md`: RISC-V CSR model, `mcause`/`mtval` mapping, trap capture commands.
- `references/zephyr-linux.md`: Zephyr west/devicetree/Kconfig workflow and embedded-Linux peripheral triage.
- `references/wireless.md`: BLE, Wi-Fi, LoRa/LoRaWAN, and 2.4 GHz triage with evidence rules.
- `examples/stm32-openocd/`: bare-metal STM32F103 C8 CMake project with linker script, startup, and OpenOCD config.
- `examples/cases/`: three field-case walkthroughs (HardFault/BFAR, UART token HIL, dead I2C bus).
- `tests/`: unit tests for the CLI, installer, and repository metadata/lint; `.github/workflows/tests.yml` runs them on Ubuntu/Windows/macOS across Python 3.10 and 3.12.
- `VERSION` and this changelog.

### Changed

- `SKILL.md`: new triage rows for board pinouts, RISC-V, Zephyr/Linux, and wireless; a new "tooling and evidence contract" section that makes the CLI and the no-guess/dry-run rules part of the workflow.
- `README.md`: current capabilities, tool usage, one-click installation for multiple harnesses, and the example project.

### Fixed

- Fault decode now matches the Cortex-M CFSR bit layout (e.g. PRECISERR is bit 9,
  BFARVALID bit 15, DIVBYZERO bit 25) and register dumps printed by OpenOCD with
  `(/32)` unit annotations are parsed correctly.

## [0.3.0] - 2026-09-21

### Added

- `references/power-on-checklist.md` and field lessons from real boards.

## [0.2.0] - 2026-09-01

### Added

- `references/quality-gate.md`: MISRA rules and deviations, unit-test layers,
  CI flash discipline, SEGGER RTT/ITM observation, HIL checklist.

## [0.1.0] - 2026-08-28

### Added

- Initial public release: `SKILL.md`, six-step workflow, fault/peripheral/RTOS/
  boot/platform references, `scripts/fault_decode.sh`, `scripts/stack_report.py`.
