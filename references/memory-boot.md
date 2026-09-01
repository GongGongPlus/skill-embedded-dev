# 启动、内存、Bootloader 与 OTA

## 启动失败排查（复位后无任何输出，或输出即乱码）

Cortex-M 向量表约定：word0 = 初始 SP（必须指向合法 RAM 顶部），word1 = Reset_Handler 地址（bit0=1，Thumb 位）。依次核验：
1. 用调试器连上（连不上→降 SWD 速度 100k–1MHz、connect-under-reset），读 SP/PC：SP 指向合法 RAM？PC 在 Reset_Handler？
2. `.isr_vector` 段地址与链接脚本 FLASH origin 一致
3. bootloader 跳 app：app 链接地址与实际烧录偏移一致；跳转前重定位 **VTOR**、关中断、（M7）清 cache/关 FPU 相关
4. 时钟初始化是否早于任何依赖该时钟的外设访问
5. C 库运行时：`.data` 拷贝与 `.bss` 清零是否在 main 前完成（启动文件）
6. 喂狗与看门狗默认开启的芯片（如某些 KEA/CH 系列）——长初始化未喂狗 = 复位循环

## 链接与内存

MAP 文件三用途：
- **Global Symbols**：PC → 函数定位（HardFault 分析）
- **Memory Map**：判定故障地址是否落在合法区（区外 = 野指针实锤）
- **Cross References / 分区统计**：找出吃 flash/RAM 的大户

优化流程（必须前后对比）：
1. 改前存一份 map，记录 flash/RAM 用量与最大栈深度
2. 手段：`-Os`、`-ffunction-sections -fdata-sections` + `--gc-sections`、newlib-nano、移除未用 printf 浮点支持、查 LTO 可行性、把常量表放 flash（`const` + 确认未落 RAM）
3. 改后再存 map，量化对比；溢出断言（ASSERT(SIZEOF(.stack)...)）写进链接脚本
4. 记录结论到工程文档，防反复

栈堆配置：startup 文件或链接脚本定主栈；RTOS 任务栈各任务自定；核验最坏调用路径（printf 浮点、加密库、TLS 握手是大户）。

## Bootloader → App 跳转：经典漏洞（必须检查）

网上流传最广的跳转写法**存在系统性缺陷**——C 函数指针跳转 + 栈上局部变量 + `__set_MSP()`：

```c
void JumpToApp(uint32_t addr) {
    uint32_t StackAddr = *(__IO uint32_t*)addr;      /* 栈上局部变量 */
    uint32_t ResetVector = *(__IO uint32_t*)(addr+4); /* 栈上局部变量 */
    __set_MSP(StackAddr);          /* 编译器不知道你换了栈！ */
    ((pFunction)ResetVector)();    /* 此后任何栈访问都是刻舟求剑 */
}
```

原理：编译器假定你从不修改栈顶指针（AAPCS 约定）。`__set_MSP()` 在它眼里只是内联汇编，因此它可以把 `MSR MSP` 重排到局部变量读写中间，把 `ResetVector` 写到 **新栈的 MSP+4 处**——而 MSP 通常指向 SRAM 末尾，MSP+4/+8 直接越界（BusFault 或地址绕回）。函数未标 `__NO_RETURN` 时，退出时的栈帧释放也发生在新栈上。

后果特征：故障**随机、玄学、复现条件看脸**——不同编译器/版本/优化等级结果不同，"量产了没出事"只是侥幸。

修复（按可靠度排序）：
1. **跳转最后几步用纯汇编函数**（整个函数纯汇编，不是 C 里嵌内联汇编）——正解
2. 关键局部变量 `register`/`static` 化——只缓解，编译器仍可能用栈过渡
3. 函数标 `__NO_RETURN`

**诊断启发式**：bootloader 类产品"偶尔见鬼"、现场随机死机、办公室无法复现 → **先查跳转代码**，命中率极高。

## Bootloader / OTA 设计要点

设计要点：
- 镜像校验：CRC 至少要有；签名（ECDSA/Ed25519）用于安全性要求场景；先验签再跳转
- A/B 双槽 + 确认机制（test/confirmed 状态），新固件自证可用后才置 confirmed
- 掉电安全：任何时刻断电必须能回滚到已知好镜像；升级状态机落盘（NVS/flash 标志位）
- 防降级（anti-rollback）：版本单调计数，权衡是否真需要
- bootloader 自身极少更新——预留功能（多种传输方式、日志），升级失败要有恢复入口（按键/串口命令）

验证矩阵（缺一项就是未完成）：
- 正常升级 → 确认 → 运行
- 升级中断电（各阶段）→ 回滚成功
- 坏镜像/错签名 → 拒绝且原固件完好
- 连续 3 次升级回归无异常

密钥红线：私钥只存在于安全位置（HSM/签名机），不进代码仓库、不进固件、不进日志；日志里只记 key id 与机制。

## 复位与恢复路径（发布前必答）

- 每种失败模式（升级断电、坏参数、外设异常）是否都能回到已知好状态？
- 复位原因是否被记录（reset reason + 关键状态快照入备份 RAM/flash），现场问题可回溯？
