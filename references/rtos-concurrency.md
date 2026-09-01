# RTOS 并发、栈与状态机

## 栈溢出（随机死机第一大根因）

三重检测叠加（栈溢出导致的 HardFault 归因见 fault-debug.md 根因清单第 1 条）：
1. 每个关键任务周期调用 `uxTaskGetStackHighWaterMark(NULL)`，剩余 <512 字节告警（单位是字，注意换算）
2. 启动后打印全部任务水位：`uxTaskGetSystemState` / 遍历任务句柄
3. `configCHECK_FOR_STACK_OVERFLOW=2`（canary 填充检查）+ 钩子函数 `vApplicationStackOverflowHook` 记录任务名；ESP32 用 `CONFIG_FREERTOS_CHECK_STACKOVERFLOW_CANARY=y`

ISR/MSP 侧：查 handler 内大局部数组、中断嵌套深度、`configMINIMAL_STACK_SIZE` 是否够 printf 浮点格式化。

## 环形缓冲 / 队列

- **先定所有权**：单生产者/单消费者？ISR→task？DMA→task？不同所有权不同保护方案
- 经典 off-by-one：容量 N−1 区分满/空——"总丢 1 字节"的经典根因
- ISR 生产者 + 任务消费者必须用临界区/原子索引，且 ISR 内只入队/发通知，不做业务
- **满时行为必须显式**：丢弃最旧（overwrite）或拒绝新数据（drop），都要加溢出计数与高水位统计；静默覆盖是事故源
- 队列里存指针时，防止指向栈对象（出作用域即悬垂）
- burst 写跨回绕边界必须专门测试

## 优先级与同步

- 随机崩溃先查栈大小与内存腐蚀，再怀疑调度
- 优先级反转：共享资源用互斥量（继承优先级）而非二值信号量；持有时间要短
- ISR 内禁止：阻塞 API、`vTaskDelay`、非 FromISR 版本 API、printf
- 中断优先级与 `configMAX_SYSCALL_INTERRUPT_PRIORITY` 的关系必须核对：高于它的 ISR 里调用任何 FromISR API 都是未定义行为
- 共享数据的最小保护单元要明确（关中断/临界区/互斥量），混用会漏保护

## 软定时器

- tick 单位勿混用（ms/tick/秒）；`pdMS_TO_TICKS` 统一换算
- 周期定时器从"计划时刻"重启而非"回调完成时刻"，否则执行耗时累积漂移
- 回调在 timer 服务任务上下文执行——不得阻塞；耗时工作转发给工作队列/任务
- tick 计数回绕用短周期模拟测试

## 状态机

- 显式定义**非法事件策略**：忽略/记录/assert/恢复，四选一写清楚，不许未定义
- timeout 与 success 事件竞争是卡死根因：取消定时器与处理事件需在同一临界区内决策
- ISR 事件只入队，由任务侧驱动状态转移
- 状态名反映产品行为（如 CHARGING），不反映实现步骤
- 加转移日志：旧状态/事件/新状态/错误转移，线上问题靠它归因

## Zephyr 对应要点

- 线程用 `K_THREAD_STACK_DEFINE` + `k_thread_create`；`CONFIG_THREAD_STACK_INFO=y` 后用 `k_thread_stack_space_get` 测水位
- ISR 用 `K_KERNEL_STACK`，任务用 `K_THREAD_STACK`，勿混
- work queue 替代"ISR 里干活"：`k_work_submit`；`k_work_poll` 做事件驱动
- `CONFIG_ASSERT=y` + `__ASSERT` 打开开发期检查；k_oops/k_panic 区分可恢复与不可恢复
- 仿真先行：native_sim 跑逻辑测试，再上真板（native_sim ≠ 真机证据，中断/时序行为不同）

## 栈水位与资源判据（全网对标补强）

- 栈空间 MUST 至少留 3× `uxTaskGetStackHighWaterMark()` 实测余量，且余量不得低于 64 words；ISR 栈按最坏嵌套深度 + 128 words 规划。
- 验证链闭环：cppcheck + 栈高水位 + 逻辑分析仪测 ISR 延迟；可用 `configASSERT(highwater > 32)` 这类编译期/运行期断言落地。
