# 嵌入式与固件开发 / Embedded and Firmware Development

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Open Source](https://img.shields.io/badge/status-open--source-brightgreen.svg)](#开源状态--open-source-status)
[![Codex Skill](https://img.shields.io/badge/Codex-Skill-black.svg)](SKILL.md)

> 面向 MCU、外设、RTOS、故障定位、Bootloader 与真机验证的嵌入式开发 Skill。
>
> An embedded-development skill for MCUs, peripherals, RTOS work, fault diagnosis, bootloaders, and hardware validation.

## 中文介绍

### 能力范围

- 梳理芯片、板卡、原理图、引脚和工具链证据
- 开发与调试 UART、I2C、SPI、CAN、ADC 和 DMA
- 分析 HardFault、栈水位、内存与链接问题
- 处理 FreeRTOS/Zephyr 并发、优先级和 ISR 约束
- 区分源码检查、编译、烧录与真实硬件行为证据

### 适用场景

单片机固件、驱动开发、RTOS 故障、Bootloader/OTA 和板级联调。

### 设计方式

`SKILL.md` 是唯一入口，先完成场景分诊，再按需读取 `references/` 中的专题资料。这样既保留关键约束，又避免把所有领域知识一次性装入上下文。脚本仅用于可重复、可验证的机械操作。

### 安装

```powershell
git clone https://github.com/GongGongPlus/skill-embedded-dev.git "$env:USERPROFILE\.codex\skills\embedded-dev"
```

安装后重新打开 Codex 会话，让 Skill 目录重新被发现。也可以直接在任务中点名 `$embedded-dev`。

### 使用示例

```text
请使用 $embedded-dev 处理这个任务，并把已验证事实、工程推论和待验证项分开报告。
```

### 证据与边界

未连接并验证真机时，不声称烧录成功、波形正确或机构已运动；eFuse、Option Bytes 等不可逆操作必须再次确认。

本仓库提供工作流与判断框架，不替代官方规则、专业认证、生产环境审批或真实设备验证。执行涉及资金、硬件熔丝、生产部署、外部提交等高风险操作前，必须取得明确授权。

## English

### Scope

- Establish evidence for the chip, board, schematic, pins, and toolchain
- Develop and debug UART, I2C, SPI, CAN, ADC, and DMA
- Analyze HardFaults, stack watermarks, memory, and linker issues
- Handle FreeRTOS/Zephyr concurrency, priorities, and ISR constraints
- Separate source review, build, flash, and physical hardware evidence

### Best fit

MCU firmware, driver development, RTOS failures, bootloader/OTA work, and board bring-up.

### Design

`SKILL.md` is the single entry point. It triages the request first and loads only the relevant files under `references/`. This preserves important constraints without loading the entire knowledge base into context. Scripts are reserved for repeatable, verifiable mechanical work.

### Installation

```bash
git clone https://github.com/GongGongPlus/skill-embedded-dev.git "$HOME/.codex/skills/embedded-dev"
```

Start a fresh Codex session after installation so the skill directory is rediscovered. You can also invoke it explicitly as `$embedded-dev`.

### Example prompt

```text
Use $embedded-dev for this task. Separate verified facts, engineering inferences, and items that still need validation.
```

### Evidence boundary

Do not claim successful flashing, correct waveforms, or physical motion without hardware evidence; irreversible operations such as eFuse or option-byte changes require explicit confirmation.

This repository provides workflows and decision support. It does not replace official rules, professional certification, production approval, or real-device validation. Explicit authorization is required before high-risk actions involving funds, hardware fuses, production deployment, or external submission.

## Repository structure / 仓库结构

```text
skill-embedded-dev/
|-- references/
|   |-- fault-debug.md
|   |-- memory-boot.md
|   |-- peripheral-bringup.md
|   |-- platforms.md
|   |-- rtos-concurrency.md
|-- scripts/
|   |-- fault_decode.sh
|   |-- stack_report.py
|-- SKILL.md
```

Reference topics / 专题资料：

- `references/fault-debug.md`
- `references/memory-boot.md`
- `references/peripheral-bringup.md`
- `references/platforms.md`
- `references/rtos-concurrency.md`

## Author / 作者

- Author and maintainer / 作者与维护者：**中北大学机器人协会续晋全**
- GitHub: [@GongGongPlus](https://github.com/GongGongPlus)
- Collection index / 总索引：[GongGongPlus/agent-skills-index](https://github.com/GongGongPlus/agent-skills-index)

The author statement identifies the maintainer and original integrator of this repository. Referenced third-party projects retain their own authorship and rights.

作者信息表示本仓库的维护者与原创整合者；被引用的第三方项目仍保留其原作者身份与相关权利。

## 开源状态 / Open-source status

- Visibility / 可见性：**Public / 公开**
- Status / 状态：**Open source / 开源**
- License / 许可证：[MIT](LICENSE)
- Warranty / 担保：按“原样”提供，不承诺适用于特定目的 / Provided as-is, without warranty
- Third-party boundary / 第三方边界：见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)

欢迎在遵守许可证与证据边界的前提下使用、修改和分发。Issues 与 Pull Requests 可用于报告可复现问题或提交改进。

Use, modification, and redistribution are welcome under the license and evidence boundaries. Issues and pull requests may be used for reproducible bug reports and improvements.
