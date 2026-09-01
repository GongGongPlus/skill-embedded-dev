# 平台特定注意事项

## STM32（CubeMX / HAL / LL）

- **.ioc 是配置证据**：进版本库；CubeMX 重生成后 diff 检查；用户代码放在 `USER CODE BEGIN/END` 区间内，否则重生成即丢失
- 时钟树先在 CubeMX 里配好并核验：HSE 就绪与频率、PLL 分频/倍频、总线时钟上限（APB1/APB2 各有上限）、电压域与主频关系
- HAL 阻塞型 API（`HAL_Delay`、轮询收发）不要进 ISR 与 RTOS 高优先级任务；HAL 时基（默认 SysTick）与 RTOS 时基冲突：RTOS 下把 HAL 时基挪到其他定时器（如 TIM6/TIM7）
- 中断优先级分组（NVIC_PriorityGroup）全局只设一次；FreeRTOS 下按 `configLIBRARY_LOWEST_INTERRUPT_PRIORITY`/`configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY` 核对
- M7 部分（F7/H7）：cache 开启时 DMA 一致性必须处理（`SCB_CleanDCache/InvalidateDCache` 或 MPU 非 cache 区）；H7 的 DTCM 与 DMA2/DMA1 可达性差异（部分 DMA 访问不到 DTCM，缓冲区要放 AXI SRAM）
- 命名核对：中断函数名必须与启动文件向量表完全一致（拼错 = 落到 weak 默认 handler，反复进中断）

## ESP32（ESP-IDF）

- 格式化字符串：`uint32_t` 用 `PRIu32`/`%lu`、`size_t` 用 `%zu`——`-Werror` 下高频编译错误
- 崩溃解码：backtrace 地址逐个 `xtensa-esp32s3-elf-addr2line -pf -C -e build/app.elf <addr>`
- 内存泄漏：周期对比 `esp_get_free_heap_size`；`CONFIG_HEAP_TRACING` + `heap_trace_start(HEAP_TRACE_LEAKS)` 精确定位；`heap_caps_get_largest_free_block` 判断碎片化（总余量够但最大块不够 = 分配失败的隐形根因）
- Wi-Fi 断连/吞吐差先查**供电**（发射瞬间峰值电流 >500mA，电源带不动就反复掉线），再查协议栈；`CONFIG_ESP32_PHY_MAX_WIFI_TX_POWER`、天线匹配
- NVS 存凭据与配置，不硬编码；NVS 写入次数有限（勿高频写）
- 深度睡眠唤醒源与 `esp_sleep_enable_xx_wakeup` 配置核对；进睡前的状态保持（RTC 内存变量 `RTC_DATA_ATTR`）
- 任务栈用 `uxTaskGetStackHighWaterMark` 核验，`xTaskCreatePinnedToCore` 注意核心亲和性（Wi-Fi/BT 协议栈占用核心 0）
- CMake 组件化：`idf_component_register` 的 REQUIRES/PRIV_REQUIRES 漏写导致头文件"找不到"或隐式依赖

## nRF Connect SDK / Zephyr

- west eworkspace 版本锁定（west.yml revision）；NCS 版本 + SDK 版本 + 工具链版本三者绑定
- Kconfig/DT 是配置层：改行为先找 Kconfig 选项与 overlay，不改 vendor 源码；`prj.conf` 增量、`boards/*.overlay` 按板覆盖
- BLE 调试：连接参数（interval/latency/timeout）与对端协商结果、MTU 协商、`CONFIG_BT_*` 缓冲池大小（丢包先查缓冲）
- DFU：MCUboot 槽布局（Partition Manager）+ 签名密钥配置；SMP/MCUmgr 传输验证

## 嵌入式 Linux / BSP

- 设备树不匹配的典型症状：probe 失败 / `dmesg` 里 deferred probe——依赖的 provider 未就绪；`/proc/device-tree` 验证运行时实际生效的 DT
- 驱动开发：platform_driver + probe/remove 生命周期；I2C/SPI 客户端用 regmap 简化寄存器访问；中断 `request_irq` 与 DT `interrupts` 属性对应
- 用户态验证顺序：`dmesg` → sysfs/设备节点 → 应用层；内核日志等级 `echo 8 > /proc/sys/kernel/printk`
- 构建系统（Yocto/Buildroot）版本与层锁定；不要在生成目录里手改产物

## 编译器与优化（通用）

- **-O0 不是护身符**：优化等级 0 仍可能做危险优化；"加了 volatile 就没事"要实测验证
- 优化敏感的 bug（开优化就崩、关优化就好）：先降优化等级定位源码问题，但根因多半是缺 volatile、未定义行为或时序假设，修根因而不是锁死 -O0
- MDK"缩圈定位法"：按文件/按函数粒度单独降优化，缩小嫌疑范围
- weak 符号默认实现（如默认中断 handler）在激进优化下行为可能出人意料——确认链接的是你写的版本
- **Semihosting 忘记移除**：release 固件跑到 printf 时无调试器挂载 → 直接卡死（BKPT 指令）；发布前全局检查半主机调用
- 断言（assert）：开发期全开、交付版可关闭；断言失败要保留现场信息（文件/行/条件）而非静默复位

## 调试探针（通用）

- OpenOCD：`-c "adapter speed 1000"` 降速排连接问题；`reset halt` 后 halt 状态读寄存器
- J-Link RTT 是无干扰日志通道（比 UART 快且不打断实时性）；SWO/ITM 备选（需引脚与配置）
- 断点/单步会改变时序——时序相关 bug 用 RTT 日志 + 逻辑分析仪，不用断点
- 复位/挂起后 RAM 内容通常保留：死机后先 halt 再 dump 栈，不要 reset

## 平台级判据（全网对标补强，来源 jeffallan/claude-skills·embedded + embedded-debug）

- **崩溃分诊**：HardFault 首查 CFSR 按位定第一嫌疑——IBUSERR=取指/总线、PRECISERR=精确数据总线错、IMPRECISERR=延迟写回错（查 BFARVALID/MMFARVALID）、STKERR/MSTKERR=入栈/出栈错、UNSTKERR=异常返回栈错；位图全 0 仍 HardFault 时 MUST 再查 SHCSR 的 MemManage/BusFault/UsageFault 使能位。
- **errata 铁律**：选型/写驱动前 MUST 查阅该型号 errata sheet，将与本驱动相关的已知勘误以注释写入代码；不得假定 datasheet 完全正确。
- **DMA/cache 一致性**：启用 DCache 的 Cortex-M7 上，DMA 写内存后 MUST clean+invalidate 或改用非缓存内存；DMA 读内存前 MUST invalidate；同一 buffer 禁止在 DCache 与 DMA 间轮换访问。
- **提交门槛**：固件提交 MUST 通过 `-Wall -Werror` + cppcheck（`--enable=warning,portability`）零警告，否则禁止声称"验证通过"。

## FreeRTOS 模板参数（全网深挖，来源 jeffallan·embedded-systems）

- 任务栈 256 words、优先级 2、周期 `pdMS_TO_TICKS(10)` + `vTaskDelayUntil`（无漂移）；NVIC 优先级 6。
- 输出模板固定五段：硬件初始化 → 驱动(HAL/中断) → 应用 → 资源占用汇总（flash/RAM/功耗估算）→ 时序与优化决策说明。
