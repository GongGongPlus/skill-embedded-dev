# Changelog

All notable changes to this repository are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Agent Skills are consumed by an LLM, so the version number also tells the agent
whether it is looking at the same contract as an earlier session.

## [Unreleased]

### Fixed

- `references/board-pinouts.md` claimed the nRF52840 DK debug pins were
  `P0.13 / P0.14`. Those are **LED1 / LED2**; SWDIO/SWDCLK are dedicated pins
  that are not part of the GPIO port and cannot be routed through
  `nrf-pinctrl`. The DK table is now re-derived from the Zephyr
  `nrf52840dk_nrf52840-pinctrl.dtsi`, which also corrected the pin-sharing notes
  (I2C0/SPI0 on P0.26-P0.27, QSPI on P0.17-P0.23, UART1 TX P1.02 / RX P1.01) and
  separated the Nice!Nano clone, which shares no pin mapping with the DK.
- `cmd_stack` read `sys.stdin` without protection. A test runner that raises
  instead of returning EOF produced a traceback instead of the documented exit
  code 6. It now reports the read failure and returns `EXIT_CONFIG`.
- `scripts/install.py` skipped every `*.log` when copying `examples/`, so an
  installed skill could not run its own candump walkthroughs (`dbc_decode.py`
  and `decode can` both need the fixture). The two capture fixtures are now on
  an explicit allow-list, `verify_install` names them, and a regression installs
  to a temp directory and asserts the fixtures survive while `*.pyc` and
  `__pycache__` do not.
- `THIRD_PARTY_NOTICES.md` was linked from the README's open-source section but
  was missing from `RUNTIME_ENTRIES`, so every installed copy carried a dead
  link. It now ships, and a new gate derives the required root files from the
  README's own local links instead of a second hand-maintained list.

### Changed

- SKILL.md and README.md claimed "14 subcommands / 14 tools" while the CLI and
  the MCP surface had grown to 19. Two new repository gates derive the count
  from `build_parser()` and assert that SKILL.md, README.md, and
  `mcp_server.TOOLS` all agree with it, so the number can no longer drift.
- `references/board-pinouts.md` now carries a per-table source and check-status
  column: only the nRF52840 DK table is `已确认`; the rest are explicitly marked
  `待核对` and must be re-derived from the authority document before wiring.

### Tests

- 361 -> 366 tests. The new ones exist to catch the four defects above rather
  than to raise a number: an unreadable stdin, a drifted subcommand count, an
  MCP surface that disagrees with the CLI, and an installer that drops fixtures.
  Each was falsified by re-introducing the bug and watching the test fail.

## [0.8.0] - 2026-11-17

Five resource- and fact-oriented tools. Each closes a gap between something the
skill already promised in prose and something it could not actually execute.

### Added

- `scripts/map_parse.py` + `map` subcommand: GNU ld `.map` analysis. Per-section
  totals, flash/RAM usage against the `Memory Configuration` table, the largest
  symbols, `--max-flash`/`--max-ram` budget gates (exit 1), and
  `--write-baseline`/`--baseline` growth regression detection. `references/memory-boot.md`
  described three uses for a map file and nothing could read one.
- `scripts/svd_decode.py` + `svd` subcommand: CMSIS-SVD decoding of peripheral
  register dumps into named bitfields, with `enumeratedValues` meanings, explicit
  `reserved` ranges for bits the SVD does not define, `--find` for definitions,
  and exit 4 when an address falls outside every peripheral.
- `scripts/dbc_decode.py` + `dbc` subcommand: Vector DBC parsing and candump
  signal decoding. Intel `@1` and Motorola `@0` bit orders are implemented
  separately, plus factor/offset scaling, `VAL_` enumerations, mux branch
  selection, and exit 4 for unknown IDs or DLC mismatches.
- `scripts/rtt_capture.py` + `rtt` subcommand: SEGGER RTT reads. Offline mode
  parses a control block plus ring-buffer dump (including the `WrOff < RdOff`
  wrap that a naive slice returns empty for); the live path drives JLinkExe with
  a mandatory `--dry-run` preview.
- `scripts/size_gate.py` + `size` subcommand: text/data/bss budgets from an ELF
  or a saved `size -A` transcript, with baseline growth regression detection so
  a build that stays under budget while quietly growing still fails.
- Examples and fixtures: `examples/map/`, `examples/svd/`, `examples/dbc/`,
  `examples/rtt/`, `examples/size/`, each with a walkthrough README and
  hand-checkable numbers.
- Tests: `test_map_parse.py` (33), `test_svd_decode.py` (32), `test_dbc_decode.py` (39),
  `test_rtt_capture.py` (29), `test_size_gate.py` (25).

### Changed

- The MCP surface grew from 14 to 19 tools, and the main CLI from 14 to 19
  subcommands; both forward to the same modules, so there is still one
  implementation per capability.
- New repository gate `test_no_orphan_scripts`: every file in `scripts/` must be
  named in `SKILL.md`, named in `README.md`, referenced by a test, and expose
  `main()`. It immediately caught four older modules (`board_map.py`,
  `experiment.py`, `project_memory.py`, `protocol_decode.py`) that had never been
  registered in `SKILL.md`.

### Fixed

- Intel/Motorola bit extraction: the first implementation accumulated both orders
  with a left shift, which bit-reversed every Intel signal (byte 0 `0x12` decoded
  as `0x48`). Both orders now have hand-computed tests pinning the expected
  values, since the wrong answer here still looks plausible.
- `rtt_capture.py` turns a missing JLink executable into a configuration error
  instead of an uncaught `FileNotFoundError`.

## [0.7.0] - 2026-11-15

### Added

- `embedded_dev.py stack`: FreeRTOS task stack watermark report from `vTaskList` /
  `uxTaskGetSystemState` output. Flags every task below `--min-free` words, honours
  `--word-bytes` for a non-`uint32_t` `StackType_t`, and exits 3 (target fault) instead
  of 0 when a watermark is under the threshold, so a low stack cannot be reported as a
  clean run.
- `embedded_dev.py symbolize`: resolve bare crash addresses from any core (Cortex-M,
  Xtensa, RISC-V) through `addr2line`. `ENV_TOOLS["addr2line"]` now probes
  `arm-none-eabi`, `xtensa-esp32s3-elf`, `xtensa-esp32-elf`, `riscv32-esp-elf` and the
  host tool, in that order.
- `install.py --target dsh,codex` / `--target all` / `--verify-only --target all`:
  install and verify several host skill roots in one command, with per-host results.
- `pyproject.toml` with a ruff configuration (`E4,E7,E9,F,B,I,UP`, `py310` target) and
  a CI lint step, closing the gap between the skill's `-Wall -Werror` rule and its own
  Python tooling.

### Changed

- `install.py` verification is now content-based. `--list` runs `verify_install` on
  every host and reports `valid`, the installed `version`, whether it matches the
  source, and the exact missing entries. A directory that merely exists is no longer
  reported as installed, and a stale copy is called out as INVALID/OUTDATED instead of
  passing silently.
- `--force` replaces only `RUNTIME_ENTRIES`; `.git`, `tests/` and host-owned files
  survive, so force-updating a git clone no longer deletes its history.
- `install.py` `verify_install` also checks `VERSION` and names each missing runtime
  entry instead of a partial script/fixture list.

### Removed

- `scripts/stack_report.py` and `scripts/fault_decode.sh`. Both were named only in the
  README tree, unreachable from `SKILL.md`, and untested; their behaviour now lives in
  `stack` and `symbolize`, which are covered by tests, reachable through the main CLI
  and listed in the MCP tool surface.

### Fixed

- The MCP surface grew from 12 to 14 tools (`stack`, `symbolize`) with the self-test
  expectation updated to match, so the CLI, MCP server and docs agree on one surface.
- Unused imports, `zip()` without `strict=`, a bare `raise` in `project_memory.py` and
  a shadowed loop variable in `mcp_server.py` are resolved; `ruff check` is clean.

## [0.6.0] - 2026-11-15

### Added

- `scripts/experiment.py`: offline control-loop evidence tooling. Telemetry CSV analysis
  (IAE/MAE, time-weighted tail bias, overshoot relative to the initial amplitude,
  rise/settling time, optional saturation fraction), segmentation on session/mode/round/
  setpoint/gain changes, bounded PID update validation that rejects out-of-bounds or
  oversized steps instead of clipping them, a deterministic first-order plant simulator,
  and trial-record validation that refuses `converged=true`.
- `scripts/board_map.py`: controller pin map from KiCad XML netlists (series passives
  traced, capacitors only bridged across signal nets, power/ground pins excluded, DNP
  flagged), plus `.ioc` pin roles and reuse-risk notes. Ambiguous MCU candidates exit 4
  with the candidate list; PDF netlists are refused instead of guessed.
- `scripts/project_memory.py`: project detection (`.ioc`/`.uvprojx`/`.ewp`/CMake/Makefile/
  PlatformIO/ESP-IDF), three memory kinds, evidence levels, a source required for
  `已确认`, 200-entry and 512 KB caps, file locks with `--expected-version` conflicts, and
  tombstones that block silent recreation.
- `embedded_dev.py tune/board/memory`: thin delegation subcommands so the same helpers
  are reachable through the main CLI and the MCP server.
- `scripts/mcp_server.py`: tool surface grows from 9 to 12 tools, with positional
  `action` arguments for `tune`, `board`, and `memory`, and offline self-test coverage.
- `references/control-tuning.md`, `references/board-facts.md`,
  `references/evidence-scope.md`, `references/project-memory.md`.
- `examples/control/` (telemetry, bounded proposal, thermal demo) and `examples/board/`
  (KiCad netlist, `.ioc`) with walkthrough READMEs.
- `tests/test_experiment.py`, `tests/test_board_map.py`, `tests/test_project_memory.py`;
  lint checks for the bundled control/board/memory fixtures and the 12-tool MCP surface.

### Changed

- `install.py` now copies `examples/` and verifies the new fixtures after installation.
- SKILL triage gains control tuning, board-fact verification, evidence-scope, and project
  memory routes; two iron rules cover bounded control experiments and evidence scope.
- Exit-code wording distinguishes ambiguous evidence (exit 4) from configuration errors
  (exit 6).

### Notes

- No source code was copied from llm-pid-tuner or yoma; only public interface and workflow
  ideas were reimplemented under this repository's MIT license. See
  `THIRD_PARTY_NOTICES.md`.

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
