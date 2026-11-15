# 控制环调参与证据闭环

适用场景：PID/反馈控制器的参数调试、串级/前馈环的验证、调参记录的可追溯保存。核心原则：**把调参变成可归因实验，而不是连续试错**。助手只报告指标与证据，绝不宣称收敛或安全。

## 一、先理解控制器，再动参数

1. 读控制器实现与配置：并行式还是标准式、微分是对误差还是对量测、采样周期、输出限幅、积分抗饱和策略、死区与滤波。
2. 确认时间基准：`timestamp_ms` 的单位与真实采样率。至少用 10 个以上周期验证名义采样频率与实际一致（yoma 的 `adc-actual-rate` 用例就是因为没核对实际采样率而误判）。
3. 明确验收上限：稳态误差、超调、调节时间、执行器占用的可接受范围，以及本次实验预算（试次上限）。用户没给就询问；不得臆造通用超调目标。
4. 明确环的性质：离线日志、可复位仿真、真实物理环。三者证据强度不同，报告必须写明来源。

## 二、遥测数据契约

CSV 必需列：`timestamp_ms,setpoint,input`（时间戳毫秒，可为整数或小数）。可选列：`session_id,mode,round,pwm,p,i,d,p2,i2,d2`。允许 UTF-8 BOM 与 CRLF。

必须报错而不是静默丢弃：缺必需列、重复表头、无法解析的行、NaN/Inf、重复时间戳。行内未知列忽略。

分段规则：`session_id`、`mode`、`round`、`setpoint`、已知增益列发生变化，或时间戳倒退，都切分为独立段；每段独立计算指标。分段是数据事实，不是评价。

## 三、指标定义（固定口径，禁止自创）

- `iae` / `mae`：误差绝对值对观测时间做梯形积分 / 采样点均值。
- `tail_fraction`：默认最后 20% 时长；`tail_bias` 是按时间加权的带符号平均误差，判断长期偏离方向；`tail_mae` 是尾段绝对误差。
- `tail_peak_to_peak`：尾段峰峰值；只是噪声线索，不能单独证明振荡。
- `overshoot_ratio`：相对该段初始幅度的超出比例，负目标、下降目标同样处理；起始点位于响应中段时只能称"窗口相对超调"。
- 零初始幅度时 `overshoot/rise/settling` 必须为 null，不得编造。
- `rise_time_s`：10%→90% 的首次采样交叉。
- `settling_time_s`：进入误差带（默认初始幅度 2%，可用 `--settling-band` 指定输出单位）后所有采样都在带内；最后一个采样在带外或只在此前采样在带内，则置 null。不插值。
- `saturation_fraction`：仅在显式给出 `--control-limits MIN MAX` 且有 pwm 列时计算，否则 null。

**窗口末端在带内不等于长期稳定**：需要多窗口或重复实验才能支持稳定性结论。

## 四、有界变更（bounded update）

提案 JSON 结构：`{"current":{"kp":..,"ki":..},"proposed":{...},"bounds":{"kp":{"min":..,"max":..,"max_step":..}}}`。

规则（`embedded_dev.py tune check-update`）：

- `current`、`proposed`、`bounds` 必须列完全相同的参数名；不允许隐式补默认值。
- 所有值必须是有限 JSON 数字；拒绝 bool、字符串、null、NaN/Inf。
- `current` 与 `proposed` 都必须在 `[min, max]` 内。
- 每个参数变化量不得超过 `max_step`。
- **被拒绝的提案绝不静默裁剪成另一个实验**：报 violating 规则并保留原提案，由人决定重写还是放弃。
- 允许负增益（如果声明的界限允许）；`max_step` 必须来自真实控制器/测试计划，不是助手自拟。

## 五、实验记录（trial record）

每个试次保存紧凑 JSON，全量原始 CSV 留在磁盘，只把指标与变更送回上下文。字段：

- `trial`、`source`（measured_hardware | simulation | offline_log | unknown）
- `data_file`、`controller_form`、`conditions`（输入样式、负载、环境、限幅）
- `applied_parameters`、`readback_confirmed`（写入后回读成功才为 true）
- `metrics`、`acceptance`（accepted | needs_longer_observation | rejected | unknown）
- `decision`（hold | apply | rollback | stop）、`reason`
- `best_verified_trial`、`next_proposal`

禁止字段：`converged=true` 之类的安全/收敛宣称。仿真数据 apply 必须产生"仍需真机验证"警告。

停止条件：达到验收、预算耗尽、证据不可得、连续无改善、用户叫停、违反物理限。回滚同样需要回读与观察，不能只看写入命令返回 0。

## 六、安全护栏

- 不做无授权的极限增益/继电测试；测试台必须可复位/可断电。
- 写入失败必须报告回读值而非请求值；参数读回不一致时停止，不顺延。
- 真实硬件上的每个试次保留完整参数、条件与原始日志，保证可复现与可回滚。
- 助手不认证"安全"。最终接受与上线由人决定。
