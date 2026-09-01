---
name: embedded-dev
description: "嵌入式/固件开发全流程专家：单片机 MCU 固件、STM32/ESP32/nRF/裸机、FreeRTOS/Zephyr、外设驱动（UART/I2C/SPI/CAN/ADC/DMA）、HardFault 死机复位排查、RTOS 并发与栈、bootloader/OTA、内存与链接优化、真机烧录验证。当用户做嵌入式开发、写驱动、调外设、排查死机、或提到 MCU/固件/开发板/单片机/示波器/逻辑分析仪/烧录时使用。Use when developing MCU firmware, drivers, debugging HardFault, RTOS issues, or flashing real hardware."
metadata:
  agent_created: true
---

# 嵌入式开发（embedded-dev）

单入口 skill。按需加载 `references/` 下的专题文件，不要一次性全部读取。

## 工作流（六步，用户明确从中间开始时可跳过前面步骤）

1. **上下文**：查看工程文件、芯片型号（勿按板卡品牌猜型号，从 probe/丝印/数据手册确认）、SDK/工具链版本、板卡版本、原理图或引脚表、日志。缺关键信息时先问，最多问 3 个会影响技术决策的问题；问不到就标注"未知"，禁止编造寄存器值、时钟频率、引脚号。
2. **契约**：写下硬件身份、供电/复位/时钟、引脚与总线归属、时序、内存预算、并发模型、验收标准。接口先冻结再写实现。
3. **实现**：应用层 / 驱动(HAL)层 / 平台层分离；优先用厂商 SDK 现成 API 与工程既有风格；生成代码（CubeMX .ioc、Zephyr DT/Kconfig）是配置证据，改配置重生成后 diff 检查，不手改生成产物。
4. **构建**：干净构建 + 零警告（`-Wall -Werror`）+ 静态检查（cppcheck 等）。编译通过 ≠ 运行正确。
5. **真机验证**：`build → flash → monitor → test`，亲眼看串口输出/测量数据才能声称完成。无硬件时明确说"未做真机验证"并给出下一个可执行实验，不得宣称完成。
6. **记录**：按"完成报告"格式收尾（见下）。

## 铁律

- **单变量原则**：一次只改一个配置，否则无法归因。
- **证据分级**：结论只能标为 已确认（文件/工具输出/测量支持）/ 工程推论（给出推理链+证伪方法）/ 估算 / 待验证 / 数据不足。禁止无边界地说"没问题""稳定""可用"——说"某条件某板上实测通过"。
- **不可逆操作先 dry-run**：eFuse/OTP、secure-boot 密钥、flash 加密、RDP/option bytes、量产校准、mass erase——只做计划，等用户明确授权且恢复路径已确认。密钥/密码不进源码和日志。
- **烧录前先明确**：已知好镜像、boot 模式、恢复路径、停止条件。恢复失败立即停止并保留全部日志。
- **不猜寄存器**：先读数据手册/reference manual 和 errata，再动寄存器。
- **故障现场优先保全**：死机先 halt 读 fault 寄存器和栈帧，再谈复位；复位即销毁唯一证据。
- **HardFault handler 必须 naked** 并区分 MSP/PSP（见 fault-debug.md）；bootloader 跳转最后几步必须纯汇编函数（见 memory-boot.md）。
- **高于 `configMAX_SYSCALL_INTERRUPT_PRIORITY` 的 ISR 禁调任何 FromISR API**（未定义行为）。

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
| STM32/ESP32/nRF/嵌入式 Linux 平台特定坑 | "Wi-Fi 老掉线" "HAL 时基不对" | [platforms.md](references/platforms.md) — CubeMX/ESP-IDF/nRF/Zephyr 注意事项 |

外部按需查询：厂商手册细节（寄存器位段、errata）不内置，可定向检索 ima 知识库「STM32开发资料」「裸机思维」「嵌入式软件开发」，仅取实战结论，手册参数仍以官方 datasheet 为准。

## 完成报告格式

收尾必须包含：改动的文件与版本；路由与依据；执行的命令与测试结果；每个关键结论的证据等级；跳过的检查及原因；遗留风险；一个具体的下一步。

