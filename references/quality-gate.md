# 质量门禁：MISRA、单元测试与 CI 烧录

**第一件事**：先写"什么算过"的门禁清单（见文末表格），再挑工具。三个议题一次只动一个（单变量原则），否则回归无法归因。

## 一、MISRA C：静态检查

### 选版本（先确认合同/认证要求，不要自选）

| 背景 | 该用的版本 |
|---|---|
| 通用固件 / 甲方没指定 | MISRA C:2012（后续有 MISRA C:2023 等合订/更新版本，规则号基本沿用；版本以官方当期发布与合同指定为准） |
| 汽车 ISO 26262 | MISRA C:2012/2023；C++ 用 MISRA C++:2023 或 AUTOSAR C++14 |
| 工业 IEC 61508 / 机械 ISO 13849 | 常引用 MISRA C:2012，以认证方书面要求为准 |
| 航空 DO-178C | 通常要求 MISRA C + 结构覆盖（MC/DC）；工具需资格确认（DO-330） |

**判据**：版本、Mandatory/Required/Advisory 的处理方式、是否允许偏离——这三件事必须在开工前写进项目约定；中途换版本 = 全部基线作废。

### 三类规则的处理方式

| 类别 | 数量级 | 处理 |
|---|---|---|
| Mandatory | 少数 | 一条都不许违反，无偏离通道 |
| Required | 主体 | 默认必须遵守；确需违反要写正式偏离（deviation） |
| Advisory | 少量 | 建议遵守；记录趋势即可，不作阻断门禁 |

### 固件高频命中规则（按踩坑频率排）

| 规则 | 内容 | 典型踩法 | 处理 |
|---|---|---|---|
| Dir 4.1 | 运行时故障要最小化 | 不检查 HAL 返回值；任务创建失败静默继续 | 可能失败的调用必须有显式错误路径（交叉见 power-on-checklist.md 堆余量铁律） |
| Rule 1.3 | 不得出现未定义行为 | 有符号整型溢出、移位越界、解引用空指针 | 用无符号/饱和运算；UB 是"编译器可以任意优化"的地雷，开 -O2 才暴露 |
| Rule 2.1 | 不得有不可达代码（Mandatory） | 调试残留 `if(0)`、永不执行的分支、return 之后的代码 | 删除，不要注释保留；死循环用 `for(;;)`，调试开关走配置项 |
| Rule 5.1 / 5.2 / 5.3 | 外部标识符可区分 / 同作用域标识符可区分 / 内层标识符不得遮蔽外层 | 全局 `state` 与局部 `state` 并存 | 加前缀（`g_`/`s_`/模块名）；内层变量改名，别靠遮蔽 |
| Rule 6.1 | 位域只能用适当类型（有/无符号 int） | `uint8_t flag:1`、枚举位域 | 位域固定 `unsigned int`；寄存器映射用显式掩码 |
| Rule 7.1 | 不得使用八进制常量 | `mode = 077` 误以为是十进制 | 一律 `0x` 或十进制 |
| Rule 8.2 | 函数必须是带参数名的原型形式 | K&R 风格、隐式声明 | 头文件写全参数类型；开 `-Wmissing-prototypes` |
| Rule 8.4 | 定义外部对象/函数前必须可见兼容声明 | .c 里定义函数但没 include 自己的头 | 定义前先 include 对应头文件 |
| Rule 8.7 | 只在一个编译单元用的函数/对象不应有外部链接 | 工具函数全 `extern` | 加 `static` |
| Rule 8.13 | 指针应尽量指向 const 限定类型 | 只读缓冲用非 const 指针 | 入参能加 const 就加 |
| Rule 9.1 | 自动存储期对象未赋值不得读 | `uint8_t buf[64]; send(buf, 64);` | 初始化或显式 memset |
| Rule 10.3 | 赋值不得缩窄或改变本质类型类别 | 隐式 `int→uint8_t` 截断、有符号/无符号混算 | 显式强转 + 范围检查（强转本身可能踩 10.x，需一并核对） |
| Rule 11.3 | 不得在指向不同类型对象的指针间强转 | `(uint8_t*)&frame` 按字节访问结构体 | 用 `memcpy` 序列化（union 另受 19.x 约束） |
| Rule 11.4 | 指针与整数之间不应互相转换 | 寄存器地址宏、`(void*)0x40000000` | 嵌入式**必然偏离项**，写成标准偏离模板 |
| Rule 11.9 | 只能用标准形式表示空指针常量 | 自定义 `#define NULL 0`；函数指针与 0 比较 | 用 `<stddef.h>` 的 NULL |
| Rule 12.1 | 运算符优先级应显式 | `a & b == c`、位移与加法混用 | 加括号，别靠记忆 |
| Rule 13.5 | `&&`/`\|\|` 右操作数不得有持久副作用 | `if (ok && i++)` | 副作用单独一行 |
| Rule 14.3 | 控制表达式不得恒定 | 发布代码残留 `while(1)`/`if(0)` | 死循环用 `for(;;)`；调试开关走配置项 |
| Rule 14.4 | 控制表达式必须是本质布尔类型 | `if (ptr)`、`if (count)` | 写 `if (ptr != NULL)`、`if (count != 0u)` |
| Rule 15.5 | 函数应只有一个出口（末尾） | 多 return、失败提早返回 | Advisory；保留早返回需记录为接受项 |
| Rule 15.7 | if...else if 结构必须以 else 收尾 | 漏 else 导致状态残留 | 补 else（哪怕 `else { }` 加注释理由） |
| Rule 16.4 | 每个 switch 必须有 default | 状态机漏分支 | default 走显式错误/安全态，禁止空 default |
| Rule 17.1 | 不得使用 `<stdarg.h>` | 自定义变参 log/printf | 固定参数宏或封装日志；驱动层偏离 |
| Rule 17.2 | 不得直接或间接递归 | 树/链表遍历写成递归 | 改迭代 + 显式栈 |
| Rule 17.4 | 非 void 函数所有出口都要显式 return | 末尾漏 return，返回值不确定 | `-Wreturn-type` 兜底 |
| Rule 17.7 | 有返回值函数的返回值必须被使用 | `HAL_Init();` 不检查、`fread` 忽略返回值 | 显式检查；确实丢弃时 `(void)` + 注释说明 |
| Rule 18.1 | 指针算术结果必须落在同一数组内 | `buf + len` 越界算地址 | 用 `size_t` 索引 + 边界检查 |
| Rule 21.3 | 不得使用 `<stdlib.h>` 的内存分配/释放 | 运行期 malloc/free 碎片化 | 静态池/有界分配器；必须动态分配时走偏离 |
| Rule 21.6 | 不得使用标准库 I/O 函数 | 发布固件里 `printf` | 重定向到 UART 的 `_write` 封装，仅调试构建启用 |
| Rule 21.7 | 不得使用 atof/atoi/atol/atoll | `atoi(buf)` 无错误检测 | `strtol` + `endptr` + 范围校验 |
| Rule 21.10 | 不得使用标准库时间/日期函数 | `time()` 取系统时间 | 用 RTC 驱动或单调 tick |

> 未使用变量/命名参数属 MISRA 的 unused 规则族：先让编译器 `-Wunused-variable` / `-Wunused-parameter` 兜底，再核对工具报告；其规则号与 Required/Advisory 归属随 MISRA 版本调整，以合同指定版本原文为准。

**权威性声明**：规则号以 MISRA 官方文档及勘误表（Technical Corrigendum）为准；不同工具与版本对同一 id 的解释可能有差异，工具报告必须人工复核，不得直接当合规结论。

### 嵌入式必然偏离的几类（提前写好模板，避免逐个吵）

- **寄存器访问**：11.3/11.4 加上 19.x（union）——由 HAL/CMSIS 头文件承担，偏离范围 = 厂商头文件目录 + 版本号
- **厂商 HAL/SDK 源码**：通常大规模不合规——整套目录豁免，偏离里写清"版本 + 不修改"原则
- **链接脚本/启动文件/汇编**：不在 C 规则范围内，单独说明
- **调试构建**：`printf`/断言仅 release 关闭——按构建类型分别设基线，不要用一套门禁卡两种构建

偏离文档必须写全五要素：**规则号 + 位置（文件:行/目录） + 理由 + 替代风险控制措施 + 批准人与复核期限**。缺任何一项视为无效偏离，审核会被打回。

### 工具选择

| 工具 | 定位 | 注意 |
|---|---|---|
| cppcheck + misra.py 插件 | 免费起步、CI 快速反馈 | `--addon=misra.json` 启用（需 Python）；规则覆盖不全，只适合增量门禁 |
| clang-tidy | 免费，cert/clang-analyzer 检查强 | 不是 MISRA 全量实现，不能当合规证据 |
| IAR / Keil / Arm Compiler 内建检查 | 工具链自带，零额外成本 | 规则子集，报告格式各异 |
| PC-lint Plus / Helix QAC | 商业，MISRA 覆盖最全 | 需先配规则集与偏离库，初次调参成本高 |
| Polyspace / Coverity / CodeSonar | 商业，深数据流分析 | 适合大型代码库；误报治理要专人 |
| LDRA / VectorCAST / Tessy | 认证向（测试 + 需求追溯） | 汽车/航空项目常用，价格与流程都重 |

**工具资格**：ISO 26262 看 TCL（工具置信度等级），DO-178C 看 DO-330 工具鉴定——认证项目不能拿免费工具的输出当证据。

### 落地顺序（老项目怎么不崩）

1. 只对**新增/改动代码**开 Required 门禁（0 违规），存量代码生成基线文件冻结
2. 存量按模块逐步清零：先 Mandatory → 再高风险 Required（内存/指针/未初始化）
3. 厂商代码与自研代码分开跑（不同规则集/不同 `-W` 选项），否则零警告门禁立刻被 SDK 淹没
4. 每次清理单独提交并跑单测回归，禁止"一次性全仓格式化 + 改代码"
5. 度量只看三件事：Mandatory 违规数、新增 Required 违规数、剩余偏离数

## 二、单元测试

### 前提：可测性改造（不做这步，测试写不下去）

- 寄存器访问收进 HAL 接口（函数指针表 / driver struct），业务层不直接读写寄存器
- 时钟、延时、随机数、复位原因全部可注入（mock tick），测试里别出现 `HAL_Delay`
- 静态状态可复位：成对提供 `init/deinit`，`setUp/tearDown` 必须回到初始态
- 中断不真触发：测试里直接调 ISR 函数 + 手动置标志，或用事件注入

### 分层（金字塔，别倒过来）

| 层 | 在哪跑 | 测什么 | 依赖 |
|---|---|---|---|
| 纯逻辑 | host | 协议编解码、状态机、滤波、控制律、定点缩放 | 无硬件 |
| 驱动 | host | 初始化序列、寄存器写序、错误重试 | fake HAL（记录调用、注入返回值） |
| 板级集成 | 目标板 | 时钟/中断/DMA/外设真实行为 | 真硬件 |
| HIL | 目标板 + 台架 | 端到端功能、掉电恢复、通信对端 | 可编程电源/信号源/上位机 |

**判据**：host 全绿 ≠ 目标板可用。字长、对齐、优化等级（-O2 暴露 UB）、浮点、中断时序都可能只在目标板翻车——关键模块必须在目标上复跑一遍。

### 框架选型

| 框架 | 适合 | 备注 |
|---|---|---|
| Unity + CMock + Ceedling | 纯 C 新项目、要自动生成 mock | Ruby/Rake 驱动；`ceedling test:all`，gcov 插件出覆盖率 |
| CppUTest | C/C++ 混合、要内存泄漏检测 | 轻量，mock 手写 |
| GoogleTest + fff | 已有 C++ 工程/上位机工具 | fff 做 C 函数 fake，不侵入源码 |
| cmocka | 小项目、快速上手 | 功能少但够用 |
| VectorCAST / Tessy / LDRA | 认证项目（ISO 26262 / DO-178C） | 需求追溯 + 覆盖率 + 工具鉴定一条龙 |

### 固件必测清单（比"提高覆盖率"有用）

- 环形缓冲：空/满/回绕/单生产者单消费者并发；溢出计数策略
- 状态机：非法迁移被拒、超时回退、复位后初始态、断电恢复
- 协议解析：截断帧、长度字段超界、坏 CRC、粘包/半包、最大帧
- 数值：除零、溢出饱和、定点缩放、边界极值（0/最大值/负数）
- 错误路径：I2C NACK、超时、重试上限、看门狗未喂、malloc 失败
- 时间：32 位 ms 计数回绕（约 49.7 天）、tick 丢失

### 覆盖率：能报但别迷信

- 类型：语句 / 分支 / 条件 / **MC/DC**（DO-178C Level A 要求；较新 GCC/Clang 才开始支持插桩，旧工具链需商业工具）
- gcc：`--coverage` + lcov/gcovr；clang：`llvm-cov`
- 覆盖率 ≠ 质量：100% 行覆盖照样漏并发与时序；禁止为凑数写无断言测试
- 门禁建议：**改动代码分支覆盖 ≥ 80%**，整体只做趋势，不卡绝对值

### 在目标板上跑

- Unity on-target：`unity_config.h` 里把输出接到 UART，板上跑同一套测试
- 或 pytest + pyserial：上位机发命令、板子回结果，逐条断言（本质是 HIL 冒烟）
- 断言失败要能拿到现场：打印测试名 + 期望/实际值，不要只打 `FAIL`

## 三、CI 烧录与 HIL

### 流水线四段

```
build（多板型 matrix）→ 静态检查（cppcheck/clang-tidy，与 build 并行）
                      → host 单元测试 + 覆盖率
                      → flash & HIL（自托管 runner + 真实探针）
```

### 十条纪律（每条都对应一次真实翻车）

1. **产物同一性**：测试的 elf = 烧录的 elf = 归档的 elf。用 artifact 传递并记录 sha256，禁止"烧录前重新编译一次"
2. **烧录必须 verify**：读回校验通过才算成功；日志里的 "Flashing succeeded" 不是证据
3. **探针独占**：两个 job 抢一个 ST-Link 会随机失败——一板一 runner，或至少用 `concurrency` 组串行化
4. **冷启动验证**：调试器复位 ≠ 上电复位（掉电时序、boot 引脚、外部器件上电慢、看门狗）。要有可编程电源/USB relay 真断电重上电；做不到就明确标注"非冷启动"
5. **日志采集**：pyserial 抓启动日志，按关键行正则断言 + 全局超时，别用固定 `sleep 5` 等结果
6. **失败留证据**：串口日志、fault 寄存器 dump、elf/map、台架截图全部上传，不允许"重跑一次就好了"掩盖
7. **重试上限 1 次**：连续两次同点失败判回归；偶发失败单独统计为"硬件抖动率"
8. **不可逆操作不进 CI**：option bytes、eFuse、量产校准、密钥烧写一律人工 + 双人确认
9. **密钥不进日志**：签名/证书走 CI secret，命令回显要脱敏
10. **失败要阻断**：门禁 job 失败必须红，不要 `continue-on-error` 放行

### 烧录命令对照（先在本地手动跑通，再进 CI）

| 探针/平台 | 命令 |
|---|---|
| OpenOCD（ST-Link/DAPLink） | `openocd -f interface/stlink.cfg -f target/stm32f4x.cfg -c "program build/fw.elf verify reset exit"` |
| pyOCD | `pyocd flash -t stm32f407vg build/fw.hex`（该命令本身不做读回校验） |
| J-Link | `JLinkExe -device STM32F407VG -if SWD -speed 4000 -autoconnect 1 -CommanderScript flash.jlink`（Windows 下可执行文件为 `JLink.exe`，参数相同） |
| STM32CubeProgrammer CLI | `STM32_Programmer_CLI -c port=SWD -w build/fw.hex -v -rst` |
| ESP32 | `esptool.py --chip esp32s3 -p COM5 -b 921600 write_flash 0x0 build/fw.bin` |
| ESP-IDF | `idf.py -p COM5 flash`（monitor 单独跑，CI 里用 pyserial 采集） |
| nRF | `nrfjprog --program build/fw.hex --chiperase --verify --reset`，或 `west flash` |
| PlatformIO | `pio run -t upload --upload-port COM5` |

**判据**：先在本地把这行命令跑通并看到 verify 通过，再原样搬进 CI——在 CI 里调命令是归因最困难的方式。

注：OpenOCD 旧版本（< 0.11）接口文件名为 `interface/stlink-v2.cfg`；target 文件按芯片系列（`stm32f4x.cfg` / `stm32g4x.cfg` / `stm32h7x.cfg` 等）。Windows runner 需预装探针驱动，且 USB 断连后建议重启 runner 服务或复位探针。

pyOCD 的 `flash` 命令不做读回校验：要满足"烧录必须 verify"，需另跑 `pyocd commander -c "compare fw.hex 0x0"` 或应用层 CRC 校验，别把"刷写完成"当读回通过。esptool 的 `write_flash 0x0` 只适用于合并镜像（merged bin）；单 app.bin 通常写 0x10000（ESP32-S3：bootloader 0x0、分区表 0x8000），地址以分区表为准。

### GitHub Actions 骨架（自托管 runner 烧录）

```yaml
name: firmware-ci
on: [push, pull_request]

jobs:
  build-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: sudo apt-get update && sudo apt-get install -y gcc-arm-none-eabi cmake ruby python3 cppcheck
      - run: gem install ceedling
      # 以下三行按工程实际工具链替换：CMake 构建 + Ceedling 单测只是示例
      - run: cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j
      - run: cppcheck --addon=misra.json --error-exitcode=1 src/
      - run: ceedling test:all
      - uses: actions/upload-artifact@v4
        with:
          name: fw
          path: |
            build/*.elf
            build/*.map
            build/*.hex

  flash-hil:
    needs: build-test
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'   # 只有主仓主分支才碰真台架
    runs-on: [self-hosted, board-stm32f4]   # 一板一 runner，探针与驱动预装
    concurrency:
      group: hw-bench                       # 单台台架串行，避免两个 job 抢探针
    steps:
      - uses: actions/checkout@v4              # HIL 脚本在仓库里，必须 checkout
      - uses: actions/download-artifact@v4
        with: { name: fw, path: fw }
      - run: openocd -f interface/stlink.cfg -f target/stm32f4x.cfg -c "program fw/fw.elf verify reset exit"
      - run: python hil/smoke.py --port COM5 --timeout 15
      - if: always()
        uses: actions/upload-artifact@v4
        with: { name: hil-log, path: hil/logs/ }
```

（`misra.json` 内容：`{"script": "misra.py"}`；跨平台构建产物记得记录 sha256 并归档。）

安全提示：公开仓库不要把 `pull_request` 直接派给自托管 runner——fork PR 不会进入自托管 runner（永久排队），且等于让外部代码在台架机上执行。骨架里用 `if:` 只允许主仓 main 的 push 碰真台架；也可用 `workflow_dispatch` + environment 审批。

### HIL 冒烟脚本骨架（pyserial 断言启动日志）

```python
import argparse, os, serial, time, sys

EXPECT = ["BOOT OK", "SENSOR OK", "HEARTBEAT"]

ap = argparse.ArgumentParser()
ap.add_argument("--port", default="COM5")
ap.add_argument("--timeout", type=float, default=15)
args = ap.parse_args()

os.makedirs("hil/logs", exist_ok=True)          # 落盘后 artifact 才有东西可传
log = open("hil/logs/uart.log", "w", encoding="utf-8")

port = serial.Serial(args.port, 115200, timeout=1)
deadline = time.time() + args.timeout
seen = set()
while time.time() < deadline and len(seen) < len(EXPECT):
    line = port.readline().decode("utf-8", "replace")
    log.write(line); log.flush()
    for i, key in enumerate(EXPECT):
        if key in line:
            seen.add(i)
            print(f"[{time.strftime('%H:%M:%S')}] {key}: {line.strip()}")

missing = [EXPECT[i] for i in range(len(EXPECT)) if i not in seen]
log.close()
if missing:
    print(f"HIL FAIL missing: {missing}")
    sys.exit(1)
print("HIL PASS")
```

真实项目里复位要由 relay/电源控制，日志每行落盘到 `hil/logs/` 供 artifact 上传。

### 门禁清单（写进 README，别只放脑子里）

| # | 门禁 | 判定 | 阻断级别 |
|---|---|---|---|
| 1 | 构建 | 干净构建、`-Wall -Werror` 零警告 | 阻断 |
| 2 | 静态 | Mandatory 违规 0、新增 Required 违规 0 | 阻断 |
| 3 | 单元测试 | host 全过；改动代码分支覆盖 ≥ 80% | 阻断 |
| 4 | 烧录 | verify 读回通过，产物 sha256 与测试产物一致 | 阻断 |
| 5 | HIL | 冒烟关键日志全部命中，无超时 | 阻断 |
| 6 | 证据归档 | elf/map/hex、覆盖率报告、静态报告、串口日志 | 警告 |
| 7 | 趋势 | Advisory 违规数、HIL 抖动率 | 仅记录 |

### 证据等级怎么报（接 SKILL.md 铁律）

- host 单测通过 = 已确认（**该层逻辑**，不代表目标板）
- 静态门禁通过 = 已确认（**该工具该配置下**）
- CI 烧录 verify 通过 = 已确认（**该镜像在该板**）
- HIL 冒烟通过 = 已确认（**该用例该台架**）
- 没跑 HIL / 没接真机 = 只能写"未做真机验证"，禁止写"已验证/稳定"

## 四、常见反模式（见到立刻叫停）

- 为了过检查**关规则/加抑制**，而不是改代码或写偏离
- 测试里 `sleep(500)` 等硬件、断言只写 `TEST_ASSERT_TRUE(1)`
- fake HAL 直接返回真实外设数据——测的是硬件不是逻辑
- 覆盖率刷到 100% 但全是平凡路径
- CI 里"重新编译→烧录"，导致测试的产物与烧录的产物不是同一个
- 只看工具绿灯，不看偏离清单与覆盖率
- 冷启动从未验证，全靠调试器复位

---

**与其它参考的衔接**：HardFault 现场保全见 [fault-debug.md](fault-debug.md)；上电自检七维见 [power-on-checklist.md](power-on-checklist.md)；bootloader/OTA 镜像与签名见 [memory-boot.md](memory-boot.md)；RTOS 并发与栈水位见 [rtos-concurrency.md](rtos-concurrency.md)。
