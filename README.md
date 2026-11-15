# 嵌入式与固件开发 / Embedded and Firmware Development

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.4.0-blue.svg)](CHANGELOG.md)
[![Tests](https://img.shields.io/badge/tests-unittest-4c1.svg)](.github/workflows/tests.yml)
[![Codex Skill](https://img.shields.io/badge/Codex-Skill-black.svg)](SKILL.md)

> 面向 MCU、外设、RTOS、故障定位、Bootloader 与真机验证的嵌入式开发 Skill，
> 自带可执行的 build / flash / monitor / regs / fault 工具层与证据契约。
>
> An embedded-development skill for MCUs, peripherals, RTOS work, fault diagnosis,
> bootloaders, and hardware validation, with an executable build/flash/monitor/
> regs/fault tool layer and an evidence contract.

## 中文介绍

### 能力范围

- 梳理芯片、板卡、原理图、引脚和工具链证据；板卡引脚/strapping 先核对再接线
- 开发与调试 UART、I2C、SPI、CAN、ADC 和 DMA 外设
- 分析 Cortex-M HardFault（CFSR/HFSR/BFAR/MFAR）、栈水位、内存与链接问题
- 处理 FreeRTOS/Zephyr 并发、优先级与 ISR 约束；Zephyr 构建、设备树与 Kconfig
- RISC-V MCU（ESP32-C3/C6、GD32VF103、CH32V003 等）的 trap 现场与根因映射
- 嵌入式 Linux 外设分层排查（device tree、pinctrl、i2c/spi/can/gpio 用户态）
- BLE/Wi-Fi/LoRa/2.4 GHz 的配置、抓包、天线与功耗分诊
- 通过 MISRA 静态检查、编写 host/目标板单元测试、把编译-测试-烧录做成 CI 门禁
- 区分源码检查、编译、烧录与真实硬件证据，报告必须带证据等级

### 可执行工具层

`scripts/embedded_dev.py` 不需要第三方依赖（`monitor` 需要 pyserial），所有命令都可以先
`--dry-run` 检查将要执行的命令行：

```bash
python scripts/embedded_dev.py env --json            # 工具链/探针/串口探测
python scripts/embedded_dev.py build --project . --backend cmake --dry-run
python scripts/embedded_dev.py flash --image build/firmware.elf --backend openocd \
  --openocd-interface interface/cmsis-dap.cfg --openocd-target target/stm32f1x.cfg --dry-run
python scripts/embedded_dev.py monitor --port COM7 --baud 115200 --expect "TEST_PASSED"
python scripts/embedded_dev.py regs --from-text regs.txt --json
python scripts/embedded_dev.py fault --cfsr 0x8200 --bfar 0x20000100
python scripts/embedded_dev.py report --file report.json
```

| 子命令 | 作用 |
|---|---|
| `env` | 检测工具链、构建/烧录后端、串口；`--strict` 缺依赖即失败 |
| `build` | cmake / PlatformIO / ESP-IDF 构建，支持 dry-run |
| `flash` | OpenOCD、pyOCD、J-Link、STM32CubeProgrammer、esptool、nrfjprog、probe-rs、PlatformIO；必须显式给出识别参数，多候选不猜 |
| `monitor` | 串口抓取 + 令牌断言（`--expect`），支持 `--mock-file` 离线测试 |
| `regs` | 解析寄存器 dump 并解码 CFSR/HFSR |
| `fault` | 由 CFSR/HFSR/地址/PC 生成根因提示与下一步 |
| `report` | 校验完成报告契约（证据等级、命令、风险、下一步） |

统一退出码：`0` 正常、`1` 工具错误、`2` 构建失败、`3` 检测到故障、
`4` 设备/测试失败、`5` 超时、`6` 配置错误（缺工具或缺必填参数）。

### 一键安装 / 多平台

```bash
python scripts/install.py --list                 # 查看 DSH/Codex/Claude Code 等 skill 根目录
python scripts/install.py --target dsh            # 安装到 ~/.dsh/skills/embedded-dev
python scripts/install.py --target codex          # ~/.codex/skills/embedded-dev
python scripts/install.py --target claude         # ~/.claude/skills/embedded-dev
python scripts/install.py --target project        # 当前工程 .dsh/skills/embedded-dev
python scripts/install.py --verify-only --target dsh
```

安装会校验 `SKILL.md` frontmatter、references、工具脚本是否齐全；安装后新开一个会话，
让宿主重新发现 skill 目录。

也可以手动 clone 到对应 skill 根目录：

```powershell
git clone https://github.com/GongGongPlus/skill-embedded-dev.git "$env:USERPROFILE\.codex\skills\embedded-dev"
```

### 适用场景

单片机固件、驱动开发、RTOS 故障、Bootloader/OTA、板级联调、无线联调、Zephyr/Linux 集成，
以及需要把构建-烧录-验证固化成可重复流程的工程。

### 设计方式

`SKILL.md` 是唯一入口：先做场景分诊，再按需读取 `references/` 中的专题资料。这样既保留
关键约束，又避免把所有领域知识一次性装入上下文。机械性、可重复、可验证的操作交给
`scripts/`：能用工具执行的，就不只输出一段命令。

### 示例与现场案例

- `examples/stm32-openocd/`：裸机 STM32F103C8 + CMake/GNU Arm + OpenOCD 的最小真机冒烟工程
- `examples/cases/01-hardfault-bfar.md`：BusFault/BFAR 现场定位
- `examples/cases/02-uart-token-hil.md`：串口令牌 HIL 通过/失败门禁
- `examples/cases/03-i2c-bus-dead.md`：I2C 总线死锁的分层排查

### 质量与 CI

```bash
python -m unittest discover -s tests -v     # CLI / 安装器 / 仓库元数据 lint
python -m compileall -q scripts tests
```

GitHub Actions（`.github/workflows/tests.yml`）在 Ubuntu/Windows/macOS × Python 3.10/3.12
上运行单元测试、编译检查、CLI 帮助、dry-run 冒烟、报告契约、安装器校验与文档相对链接检查。

### 证据与边界

未连接并验证真机时，不声称烧录成功、波形正确或机构已运动；eFuse、Option Bytes 等不可逆
操作必须再次确认；所有结论必须带证据等级（已确认 / 工程推论 / 估算 / 待验证 / 数据不足）。

本仓库提供工作流与判断框架，不替代官方规则、专业认证、生产环境审批或真实设备验证。执行
涉及资金、硬件熔丝、生产部署、外部提交等高风险操作前，必须取得明确授权。

## English

### Scope

- Establish evidence for the chip, board, schematic, pins, and toolchain before wiring anything
- Develop and debug UART, I2C, SPI, CAN, ADC, and DMA
- Analyze Cortex-M HardFaults (CFSR/HFSR/BFAR/MFAR), stack watermarks, memory, and linker issues
- Handle FreeRTOS/Zephyr concurrency, priorities, and ISR constraints; Zephyr build/devicetree/Kconfig
- Debug RISC-V MCUs (ESP32-C3/C6, GD32VF103, CH32V003) through `mcause`/`mtval`/`mepc`
- Triage embedded Linux peripherals (device tree, pinctrl, i2c/spi/can/gpio user space)
- Triage BLE, Wi-Fi, LoRa/LoRaWAN, and 2.4 GHz configuration, captures, antenna, and power
- Enforce MISRA static checks, write host/target unit tests, and gate build-test-flash in CI
- Keep source review, build, flash, and physical hardware evidence separate and labeled

### Tool layer

`scripts/embedded_dev.py` provides `env`, `build`, `flash`, `monitor`, `regs`, `fault`, and
`report` subcommands with a stable exit-code contract (`0` ok, `1` tool error, `2` build
failed, `3` fault detected, `4` device/test failed, `5` timeout, `6` configuration error).
Every hardware-writing command is dry-run first and every flash backend requires its
explicit identifying options; ambiguous probes or backends return candidates instead of
guessing.

### Installation

```bash
python scripts/install.py --list
python scripts/install.py --target dsh       # DSH
python scripts/install.py --target codex     # Codex
python scripts/install.py --target claude    # Claude Code
python scripts/install.py --target project   # project-scoped
```

The installer verifies the copied `SKILL.md` frontmatter, references, and tool scripts.
Start a fresh session after installation so the host rediscovers the skill directory.

### Design

`SKILL.md` is the single entry point. It triages the request first and loads only the
relevant files under `references/`, preserving important constraints without loading the
entire knowledge base into context. Repeatable, verifiable mechanical work belongs in
`scripts/`; if a tool can perform it, do not stop at printing a command.

### Examples

`examples/stm32-openocd/` is a minimal bare-metal STM32F103C8 build (CMake, GNU Arm,
OpenOCD). `examples/cases/` contains three field-case walkthroughs: HardFault/BFAR capture,
UART-token HIL gating, and dead I2C bus triage.

### Evidence boundary

Do not claim successful flashing, correct waveforms, or physical motion without hardware
evidence; irreversible operations such as eFuse or option-byte changes require explicit
confirmation. Every conclusion carries an evidence level.

This repository provides workflows and decision support. It does not replace official
rules, professional certification, production approval, or real-device validation.
Explicit authorization is required before high-risk actions involving funds, hardware
fuses, production deployment, or external submission.

## Repository structure / 仓库结构

```text
skill-embedded-dev/
|-- .github/workflows/tests.yml
|-- examples/
|   |-- README.md
|   |-- stm32-openocd/          # 裸机 STM32F103 示例：CMake、startup、linker、openocd.cfg
|   |-- cases/                  # 三个现场案例
|-- references/
|   |-- board-pinouts.md
|   |-- fault-debug.md
|   |-- memory-boot.md
|   |-- peripheral-bringup.md
|   |-- platforms.md
|   |-- power-on-checklist.md
|   |-- quality-gate.md
|   |-- riscv.md
|   |-- rtos-concurrency.md
|   |-- wireless.md
|   |-- zephyr-linux.md
|-- scripts/
|   |-- embedded_dev.py         # 工具层 CLI（build/flash/monitor/regs/fault/report）
|   |-- install.py              # 多平台一键安装/校验
|   |-- report_schema.json      # 完成报告契约
|   |-- fault_decode.sh
|   |-- stack_report.py
|-- tests/                      # CLI、安装器、仓库元数据与文档链接测试
|-- CHANGELOG.md
|-- SKILL.md
|-- VERSION
```

Reference topics / 专题资料：

- [references/fault-debug.md](references/fault-debug.md)
- [references/memory-boot.md](references/memory-boot.md)
- [references/peripheral-bringup.md](references/peripheral-bringup.md)
- [references/platforms.md](references/platforms.md)
- [references/power-on-checklist.md](references/power-on-checklist.md)
- [references/quality-gate.md](references/quality-gate.md)
- [references/rtos-concurrency.md](references/rtos-concurrency.md)
- [references/board-pinouts.md](references/board-pinouts.md)
- [references/riscv.md](references/riscv.md)
- [references/zephyr-linux.md](references/zephyr-linux.md)
- [references/wireless.md](references/wireless.md)

## Changelog / 变更记录

See [CHANGELOG.md](CHANGELOG.md) for the version history. Current version: see
[VERSION](VERSION).

## Author / 作者

- Author and maintainer / 作者与维护者：**中北大学机器人协会续晋全**
- GitHub: [@GongGongPlus](https://github.com/GongGongPlus)
- Collection index / 总索引：[GongGongPlus/agent-skills-index](https://github.com/GongGongPlus/agent-skills-index)

The author statement identifies the maintainer and original integrator of this repository.
Referenced third-party projects retain their own authorship and rights.

作者信息表示本仓库的维护者与原创整合者；被引用的第三方项目仍保留其原作者身份与相关权利。

## 开源状态 / Open-source status

- Visibility / 可见性：**Public / 公开**
- Status / 状态：**Open source / 开源**
- License / 许可证：[MIT](LICENSE)
- Warranty / 担保：按“原样”提供，不承诺适用于特定目的 / Provided as-is, without warranty
- Third-party boundary / 第三方边界：见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)

欢迎在遵守许可证与证据边界的前提下使用、修改和分发。Issues 与 Pull Requests 可用于报告
可复现问题或提交改进。

Use, modification, and redistribution are welcome under the license and evidence
boundaries. Issues and pull requests may be used for reproducible bug reports and
improvements.
