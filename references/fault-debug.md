# HardFault / 死机 / 复位排查

## 第一步：保全现场

- **绝不立即复位**。fault 寄存器、栈帧、RAM 内容是唯一证据。
- 先问：目标现在是否已被复位？复位后只能做静态分析，判断力大幅下降。
- 确认调试器/分析所用的 ELF、MAP 与板上烧录的镜像**完全一致**（同一次构建产物），否则 backtrace 全部不可信。
- 读 reset reason 寄存器（RCC->RSR / esp_reset_reason 等）：看门狗复位、掉电、软件复位、异常复位要区分——**长初始化未喂狗导致的复位经常被误判为 HardFault**。
- 修复时找"最早的腐蚀点"（如越界写坏堆元数据的地方），不是"崩溃的那一行"。

## Cortex-M fault 寄存器 → 根因映射

SCB 基址 0xE000ED00。CFSR(0xED28) = UFSR[31:16] + BFSR[15:8] + MMFSR[7:0]；HFSR(0xED2C)；MMFAR(0xED34) / BFAR(0xED38) 存故障地址。注意 M0/M0+ 没有 CFSR/BFAR，解码不能混用 M3/M4 的方法。

| CFSR/HFSR 典型值 | 含义 | 下一步 |
|---|---|---|
| HFSR bit30 FORCED=1 | 可配置 fault 升级为 HardFault | 必须拆解 CFSR |
| BFSR PRECISERR (0x8200) | 精确总线错误，**BFAR 有效** | 读 BFAR 取故障地址 |
| BFSR IMPRECISERR (0x0400) | 不精确总线错误，BFAR 无效 | 嫌疑：DMA 写已释放/越界缓冲、写缓冲、cache 不一致；用 watchpoint/二分定位 |
| UFSR INVSTATE (0x020000) | 跳到 Thumb 位为 0 的地址 | 函数指针损坏 / 返回地址被栈溢出覆盖 |
| UFSR UNDEFINSTR (0x010000) | 执行未定义指令 | 跳到数据区 / PC 指向已释放内存 |
| UFSR UNALIGNED (0x01000000) | 非对齐访问（需 CCR.UNALIGN_TRP） | `packed` 结构体指针强转；改用 memcpy |
| MMFSR IACCVIOL / DACCVIOL | MPU 权限 / XN 区访问 | 检查 MPU 配置与内存区归属 |
| BFSR STKERR (0x0800) / UNSTKERR (0x1000) | 异常入栈/出栈时总线错误 | **栈溢出**或栈指针损坏；查 ISR 栈与嵌套深度 |
| HFSR VECTTBL | 取向量时出错 | 向量表地址/VTOR 错误 |

BFAR 地址判读（0x8200 时最快路径）：
- SRAM 区（0x2000_0000 附近）→ 空指针 / use-after-free / 越界写坏堆
- 外设区（0x4000_0000）→ **外设未开时钟就访问寄存器**（初始化顺序错误，高频根因）
- Flash 区 → 对齐或写只读区
- 0x00000000 → 空函数指针 / 未初始化回调

## Handler 写法与栈帧解析

Handler 必须 naked，内联汇编区分 MSP/PSP（RTOS 任务栈在 PSP），把 R0 当栈帧指针传给 C 函数：

```c
__attribute__((naked)) void HardFault_Handler(void) {
    __asm volatile(
        "tst lr, #4       \n"
        "ite eq           \n"
        "mrseq r0, msp    \n"
        "mrsne r0, psp    \n"
        "b hardfault_c    \n");
}
void hardfault_c(uint32_t *frame) {
    /* frame[0..3]=R0-R3, [4]=R12, [5]=LR, [6]=PC(出错指令), [7]=xPSR */
    volatile uint32_t pc = frame[6]; /* 保存后输出，勿复位 */
}
```

PC → 源码三法（按速度排序）：
1. `addr2line -e firmware.elf <pc> -f`（加 `-p -C` 更可读）
2. MAP 文件 Global Symbols：找"起始地址 ≤ PC 的最大函数"，偏移 = PC − 函数起始
3. 调试器反汇编窗口跳到 PC

ESP32：Guru Meditation 后 `xtensa-esp32s3-elf-addr2line -pf -C -e build/app.elf <PC>`；LoadProhibited=空指针，StoreProhibited=越界/野指针，InstrLoadProhibited=函数指针/栈溢出，IntegerDivideByZero=除零。

## 常见根因清单（按频率排）

1. 栈溢出（含 ISR 内大局部数组、中断嵌套叠加）
2. 空指针 / 野指针 / use-after-free
3. 外设时钟未使能就访问外设
4. 函数指针未初始化或被腐蚀（回调注册表、跳转表）
5. DMA 越界写 / 写到已释放内存 / cache 不一致（M7、带 PSRAM 系统）
6. 向量表 / 链接脚本 origin 与 bootloader 偏移不匹配
7. 命中 weak 默认中断 handler（中断使能了但没写对应 ISR → 反复进中断"假死"）
8. FPU 编译开启但未初始化 / 未使能 CPACR
9. NMI 引脚被复用外设拉异常（例：MISO 复用 NMI，外设损坏拉高 → 复位后立即卡 NMI；可在 NMI handler 里禁用 NMI 逃出）
10. Bootloader 跳转代码缺陷（C 函数指针 + 栈上局部变量 + `__set_MSP`）→ 随机"见鬼"故障，见 memory-boot.md

横切判据：
- 只在运行一段时间后复现 → 资源泄漏 / 计数器回绕 / 碎片化
- 只在特定操作序列后复现 → 状态残留 / 未初始化路径
- 与温度/电压相关 → 硬件问题（晶振、供电、时序余量）

## 证明而非猜测

用 watchpoint、内存 guard 值、栈水印（填充 0xA5 后查被写深度）、最小化复现代码来**证明**根因。修复后用原复现序列回归验证。

## 缓冲区溢出高危函数映射表（ima 实战文章「缓冲区溢出」蒸馏，已确认）

| 高危函数 | 风险 | 替代 |
|---|---|---|
| `gets()` | 最危险，无边界检查 | `fgets(buf, size, stdin)` |
| `strcpy()` | 溢出 | `strncpy()` |
| `strcat()` | 溢出 | `strncat()` |
| `sprintf()` | 溢出 | `snprintf()` 或精度说明符 |
| `scanf()/sscanf()` | 溢出 | 精度说明符或自解析 |
| `vsprintf()` | 溢出 | `vsnprintf()` |
| 负索引 | `pos` 为 int 可负，越界写 | 索引类型改 `size_t` 并校验 `< size` |
| 无终止符 | `char array[]={'0'...'8'}` 缺 `'\0'` | 显式加 `'\0'` 或用初始化列表含结尾 |

判据：凡固件中出现上述函数且无边界校验 → 直接判"缓冲区溢出嫌疑"，即使当前没崩。

## 平台崩溃→根因映射（全网深挖，来源 embedded-debug）

- **ESP32**：`LoadProhibited` + EXCVADDR≈0x00000000 → NULL 指针解引用；`StoreProhibited` → 写只读/野指针；`InstrFetchProhibited` → 跳转到非法地址；brownout → 供电不足；cache error → flash 映射问题。
- **AVR**：栈溢出是"静默损坏"（无异常），只能靠高位标记/编译器护栏检测。
- 崩溃分析五步：解析 panic/guru meditation/HardFault/watchdog reset → 用 ELF 解码裸地址栈 trace → 解读寄存器 dump → 关联源码定位故障路径 → 给出带代码的修复建议 + 预防策略（栈大小/看门狗/空指针检查），只给解码结果不给预防 = 不合格。
