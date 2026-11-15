---
name: embedded-dev
description: "嵌入式/固件开发全流程专家：单片机 MCU 固件、STM32/ESP32/nRF/裸机、FreeRTOS/Zephyr、外设驱动（UART/I2C/SPI/CAN/ADC/DMA）、HardFault 死机复位排查、RTOS 并发与栈、bootloader/OTA、内存与链接优化、MISRA 静态检查、单元测试与 CI 烧录、真机烧录验证、控制环/PID 调参与有界更新校验、网表/原理图推板级引脚映射、跨会话工程记忆与交接。当用户做嵌入式开发、写驱动、调外设、排查死机、调控制环/PID、核对板级引脚或网表、要过 MISRA/写测试/搭自动化烧录、或提到 MCU/固件/开发板/单片机/示波器/逻辑分析仪/烧录/网表/工程记忆时使用。Use when developing MCU firmware, drivers, debugging HardFault, RTOS issues, tuning control loops/PID, verifying board-level pin maps or netlists, MISRA compliance, unit testing, CI flashing, project handoff memory, or flashing real hardware."
agent_created: true
---

# 嵌入式开发（embedded-dev）

单入口 skill。按需加载 `references/` 下的专题文件，不要一次性全部读取。

## 工作流（六步，用户明确从中间开始时可跳过前面步骤）

1. **上下文**：查看工程文件、芯片型号（勿按板卡品牌猜型号，从 probe/丝印/数据手册确认）、SDK/工具链版本、板卡版本、原理图或引脚表、日志。缺关键信息时先问，最多问 3 个会影响技术决策的问题；问不到就标注"未知"，禁止编造寄存器值、时钟频率、引脚号。
2. **契约**：写下硬件身份、供电/复位/时钟、引脚与总线归属、时序、内存预算、并发模型、验收标准。接口先冻结再写实现。
3. **实现**：应用层 / 驱动(HAL)层 / 平台层分离；优先用厂商 SDK 现成 API 与工程既有风格；生成代码（CubeMX .ioc、Zephyr DT/Kconfig）是配置证据，改配置重生成后 diff 检查，不手改生成产物。
4. **构建**：干净构建 + 零警告（`-Wall -Werror`）+ 静态检查（cppcheck 等；MISRA/单测/CI 门禁见 quality-gate.md）。编译通过 ≠ 运行正确。
5. **真机验证**：`build → flash → monitor → test`，亲眼看串口输出/测量数据才能声称完成。无硬件时明确说"未做真机验证"并给出下一个可执行实验，不得宣称完成。
6. **记录**：按"完成报告"格式收尾（见下）。

## 工具层与证据契约（可执行优先，不只给命令）

`scripts/embedded_dev.py` 把构建、烧录、串口、寄存器读回、故障解码、崩溃地址符号化、RTOS 栈水位、协议解析、HIL 验证、控制环分析、板级引脚映射和工程记忆固化为 14 个可执行子命令，统一退出码：`0` 成功、`1` 构建失败、`2` 烧录失败、`3` 检测到硬件故障、`4` 测试/串口/超时失败或证据歧义、`5` 停滞（同一失败签名且源码未变）、`6` 配置错误（缺工具、缺必填参数或多个候选不猜）。

- 先 `env --json` 查清真实工具链、探针与串口，再决定路径；环境不明不猜。
- 所有写硬件的命令先 `--dry-run`，确认命令行后再执行。
- 烧录后端必须显式给出识别参数（`--openocd-interface`+`--openocd-target`、`--chip`+`--port`、`--target`、`--jlink-device` 等）；存在多探针/多后端歧义时脚本返回候选列表并退出 6，不自动选。
- 真机验证顺序 `build → flash → monitor --expect <token>`；未捕获令牌即失败（退出 4）。`hil` 把这条链路一次跑完，并写 `diagnostic_report.json`（含失败签名、next_actions、ai_repair_prompt、repeated_failure、source_unchanged）；同一失败重复出现且源码未变时报 `STALLED`（退出 5）。
- 故障先 `fault` / `regs` 解码 CFSR/HFSR/BFAR 与栈帧；带 `--elf` 时用 addr2line 把 PC/LR 解析到函数/行号（DWARF 门禁，缺工具即报配置错误），再谈复位。完成报告用 `report --file` 校验证据分级与命令记录，不合格即拒绝。
- 只有裸地址（Xtensa/RISC-V 崩溃日志、只有 PC 没有寄存器）时用 `symbolize --elf --addr`，一个命令解析任意核；工具链按 ARM → Xtensa → RISC-V → 宿主顺序自动探测。
- RTOS 栈水位用 `stack`：喂入 `vTaskList`/`uxTaskGetSystemState` 输出，按阈值标出低水位任务并退出 3；单位默认 StackType_t=4 字节，`--word-bytes` 可改。水位不足是硬件故障证据，不是"看起来还行"。
- 串口/CAN 帧解析用 `decode`：先 `decode validate --spec` 验证 `embedded-protocol.v1` 规范，再解 UART 抓包或 candump 日志（ISO-TP 重组、UDS/NRC、OBD-II PID、J1939 PGN）；规范没写的位段保持 unknown，不猜。
- 需要把工具暴露给其他 agent 运行时（Claude Code、Codex、DSH 等）时，用 `scripts/mcp_server.py` 的 stdio MCP 服务，不自造第二套命令。
- 控制环调参用 `tune`：先 `tune analyze` 从遥测 CSV 算 IAE/MAE、尾偏差、超调、上升/稳定时间，再 `tune check-update` 校验有界更新（越界或超步长直接拒绝，不裁剪成另一个实验），`tune simulate` 可离线产生遥测供回归；只报告指标，不输出"已收敛/已安全"。
- 板级事实用 `board`：`board controller --netlist` 从 KiCad XML 网表推主控与每脚连接（串联 R/L/ferrite 穿透、电容仅在两信号网间桥接、电源/地排除、DNP 标注），`board from-ioc` 读 CubeMX 引脚角色与复用风险；多主控歧义退出 4 不猜，PDF 网表拒绝而不是猜。
- 工程记忆用 `memory`：`detect` 识别工程与芯片、`init` 建 `.embedded-dev/` 档案、`remember/search/show/list/forget` 存取带证据等级的条目；`已确认` 必须给 `--source`，版本冲突退出 4，`forget` 留墓碑防止同名重建冒充旧结论。
- 多平台安装：`python scripts/install.py --list` 逐个 host 做内容校验并显示版本漂移，`--target dsh,codex` 一条命令同步 DSH 与 Codex（`--target all` 覆盖所有已存在的 skill 根），`--verify-only --target all` 检查每一份副本。

## 铁律

- **单变量原则**：一次只改一个配置，否则无法归因。
- **证据分级**：结论只能标为 已确认（文件/工具输出/测量支持）/ 工程推论（给出推理链+证伪方法）/ 估算 / 待验证 / 数据不足。禁止无边界地说"没问题""稳定""可用"——说"某条件某板上实测通过"。
- **不可逆操作先 dry-run**：eFuse/OTP、secure-boot 密钥、flash 加密、RDP/option bytes、量产校准、mass erase——只做计划，等用户明确授权且恢复路径已确认。密钥/密码不进源码和日志。
- **烧录前先明确**：已知好镜像、boot 模式、恢复路径、停止条件。恢复失败立即停止并保留全部日志。
- **不猜寄存器**：先读数据手册/reference manual 和 errata，再动寄存器。
- **故障现场优先保全**：死机先 halt 读 fault 寄存器和栈帧，再谈复位；复位即销毁唯一证据。
- **HardFault handler 必须 naked** 并区分 MSP/PSP（见 fault-debug.md）；bootloader 跳转最后几步必须纯汇编函数（见 memory-boot.md）。
- **高于 `configMAX_SYSCALL_INTERRUPT_PRIORITY` 的 ISR 禁调任何 FromISR API**（未定义行为）。
- **控制环不做无界实验**：参数变更先声明 bounds 与 max_step，越界即拒绝而不是裁剪后继续；未在目标上复测前，不宣称已收敛、稳定或安全。
- **结论不越证据范围**：配置、软件自报、推导量、直接观测四类证据不可混写；"编译通过/已烧录/串口有输出"各自只证明自己那一层（见 evidence-scope.md）。
- **"已安装"必须是内容校验**：目录存在、README 写着能跑、宿主自报都不是证据；只有 `install.py --verify-only` 逐项核对运行时条目通过才算已安装。

## 固件编码 MUST / NOT

MUST：
- 硬件寄存器、ISR 与任务共享变量用 `volatile`；ISR 共享数据用临界区/原子操作保护
- ISR 保持极短：读硬件、置标志/发队列、退出；耗时工作丢给任务
- 有看门狗；确认长初始化路径会喂狗（否则复位会被误判为死机）
- 缓冲区边界检查、指针判空、DMA 缓冲对齐；静态分配优先，动态分配必须有界
- 记录资源占用（flash/RAM/栈水位/电流）

NOT：
- ISR 里阻塞、延时、printf、动态分配、直接改状态机状态
- 无界 `memcpy`/`sprintf`；用 `snprintf`
- 忽略 errata；硬编码 magic 值（用带名字的宏并注明来源手册页）
- 无 FPU 的核上随意用浮点（软浮点开销大且不可重入陷阱）
- 满队列静默覆盖数据（必须显式 drop 策略 + 溢出计数）

## 症状分诊（按需加载对应参考文件）

| 工程症状 | 用户常这么说 | 加载（内容摘要） |
|---|---|---|
| HardFault/死机/莫名复位/卡在启动 | "板子跑一会儿就死" "复位了不知道为啥" | [fault-debug.md](references/fault-debug.md) — CFSR 位→根因映射、naked 栈帧、根因清单 |
| 外设不工作：串口乱码、I2C/SPI 无响应、ADC 异常、DMA 丢数据 | "串口全是乱码" "传感器读不到" | [peripheral-bringup.md](references/peripheral-bringup.md) — bring-up 六步、各外设坑、症状速查 |
| RTOS 任务崩溃、随机死机、优先级/队列问题 | "越跑越慢/越跑越死" "偶尔死机" | [rtos-concurrency.md](references/rtos-concurrency.md) — 栈溢出三重检测、环形缓冲、优先级 |
| 启动失败、向量表、链接/内存优化、bootloader/OTA | "烧完就没反应/变砖" "升级完开不了机" | [memory-boot.md](references/memory-boot.md) — 向量表征验、map 文件、跳转漏洞、OTA 矩阵 |
| 烧录/交付前确认"上电即跑"；任务创建失败死锁；功能静默不执行 | "能跑吗/上电直接能用？" "跑着跑着全卡住" "自动功能没反应" | [power-on-checklist.md](references/power-on-checklist.md) — 七维自检、堆余量铁律、重复符号、数据源有效位 |
| STM32/ESP32/nRF/嵌入式 Linux 平台特定坑 | "Wi-Fi 老掉线" "HAL 时基不对" | [platforms.md](references/platforms.md) — CubeMX/ESP-IDF/nRF/Zephyr 注意事项 |
| 要过 MISRA/甲方静态检查、补单元测试、把编译-测试-烧录做成 CI | "要有测试/要过静态检查" "能不能自动烧录" | [quality-gate.md](references/quality-gate.md) — MISRA 高频规则与偏离模板、Unity/Ceedling 单测分层、自托管 runner 烧录与 HIL 门禁 |
| 引脚接错/板卡不确定/上电异常/电平或启动模式问题 | "这个脚能接吗" "换了块板就不行" | [board-pinouts.md](references/board-pinouts.md) — 常见板卡引脚表、strapping/仅输入/flash 占用、三源核对规则 |
| RISC-V 芯片（ESP32-C3/C6、GD32VF103、CH32V003 等）死机/异常 | "RISC-V 上跑飞了" | [riscv.md](references/riscv.md) — CSR 模型、mcause/mtval 映射、trap 现场抓取 |
| Zephyr 构建/设备树/Kconfig，或嵌入式 Linux 外设不工作 | "west 烧不进去" "i2cdetect 看不到" | [zephyr-linux.md](references/zephyr-linux.md) — west/DT/Kconfig 工作流、Linux 外设分层排查 |
| BLE/Wi-Fi/LoRa/2.4G 连不上、掉线、距离短、功耗高 | "蓝牙搜不到" "Wi-Fi 老掉线" | [wireless.md](references/wireless.md) — 无线五步分诊、配参与抓包、天线与共存 |
| 串口/CAN 帧解析不了、CRC 对不上、UDS/OBD-II 读不出 | "这帧怎么解" "诊断仪读不到" | [protocols.md](references/protocols.md) — serial_protocol.v1 规范、切帧与校验、ISO-TP/UDS/OBD-II/J1939 |
| 控制环/PID 调不准：超调、振荡、稳态误差、响应慢 | "参数怎么调" "一直振荡" | [control-tuning.md](references/control-tuning.md) — 遥测契约、证据化指标、有界更新与拒绝不裁剪、停止/回滚条件 |
| 板级接线/引脚不确定，要核对网表、原理图或 .ioc | "这个脚接哪" "网表里怎么连" | [board-facts.md](references/board-facts.md) — 网表/`ioc` 解析、主控评分与歧义、串联件穿透、电源地排除、结论边界 |
| 结论是否说大了、证据够不够、要不要继续试 | "能说修好了吗" "还要再试吗" | [evidence-scope.md](references/evidence-scope.md) — 四类证据、结论与最小证据对照、越界模式、HIL 报告与停止条件 |
| 换会话/换人接手，需要上次的工程事实与调试结论 | "上次查到哪了" "交接一下" | [project-memory.md](references/project-memory.md) — 工程识别、三类记忆、证据等级与 source、版本冲突与墓碑 |

外部查询边界：厂商手册细节（寄存器位段、errata）不内置；需要时以官方 datasheet / reference manual / errata 为唯一依据，工具输出与第三方资料只能作为线索，不得作为参数来源。

## 完成报告格式

收尾必须包含：改动的文件与版本；路由与依据；执行的命令与测试结果；每个关键结论的证据等级；跳过的检查及原因；遗留风险；一个具体的下一步。

可运行示例与现场案例见 [examples/stm32-openocd/README.md](examples/stm32-openocd/README.md)、[examples/protocols/README.md](examples/protocols/README.md)、[examples/control/README.md](examples/control/README.md)、[examples/board/README.md](examples/board/README.md) 与 [examples/cases/01-hardfault-bfar.md](examples/cases/01-hardfault-bfar.md)。