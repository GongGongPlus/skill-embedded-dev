# 协议解码：串口帧、CAN / ISO-TP、UDS、OBD-II、J1939

这一篇解决“抓到了字节，但不知道每个字节是什么意思”的问题。原则只有一条：**协议事实来自 datasheet / 标准，不来自猜测**。猜错一个校验范围或字节序，后面的所有结论都会错，而且很难发现。

相关工具：

- `scripts/protocol_decode.py`：JSON spec 驱动的 UART 解码 + candump 日志解码（ISO-TP 重组、UDS、OBD-II、J1939）。
- `scripts/embedded_dev.py decode ...`：同一能力的统一入口，退出码与其他子命令一致（0 成功、4 带 `--strict` 时发现解码错误、6 spec/配置错误）。
- `examples/protocols/`：可重放的示例（UART spec + 抓包 + candump 日志）。

## UART：先写契约，再解码

`embedded-protocol.v1` 的最小结构：

```json
{
  "schema_version": "embedded-protocol.v1",
  "name": "my-device-v1",
  "transport": {"type": "uart", "baud": 115200},
  "framing": {
    "header": "AA55",
    "length": {"offset": 0, "size": 1, "endian": "big", "includes_checksum": true},
    "checksum": {"algorithm": "crc16_modbus", "over": "all_before_checksum"},
    "byte_order": "little",
    "max_frame": 256
  },
  "fields": [
    {"name": "msg_type", "offset": 0, "size": 1, "type": "uint8", "enum": {"1": "request", "2": "report"}},
    {"name": "temperature", "offset": 2, "size": 2, "type": "int16", "scale": 0.01, "unit": "degC"}
  ]
}
```

字段语义（全部来自 datasheet，缺一项就标 `unknown`，不要默认）：

| 键 | 含义 | 缺省时的行为 |
| --- | --- | --- |
| `framing.header` | 帧头字节串（十六进制） | 必填，空则 spec 校验失败 |
| `framing.length.offset` | 长度字段相对帧头结束的偏移 | 0 |
| `framing.length.size` | 长度字段字节数（1–4） | 必填 |
| `framing.length.endian` | 长度字段字节序 `big`/`little` | `big` |
| `framing.length.includes_checksum` | 长度值是否包含校验字节 | `true` |
| `framing.checksum.algorithm` | `none`/`sum8`/`xor8`/`crc8`/`crc16_modbus`/`crc16_ccitt` | `none` |
| `framing.checksum.over` | `all_before_checksum` 或 `payload` | `all_before_checksum`，校验字节按小端读 |
| `framing.byte_order` | 多字节字段的字节序 | `little` |
| `fields[].offset/size/type` | 字段在 payload 内的偏移、字节数、类型 | 必填 |
| `fields[].scale` | 原始值 × scale | 无 |
| `fields[].value_offset` | 物理值 = 原始值 × scale + value_offset（注意不是字节偏移） | 无 |
| `fields[].enum` | 原始值 → 名字（键是十进制或 `0x` 十六进制） | 未命中会给出 unknown 警告 |
| `fields[].bits` | 位号 → 名字（0 是最低位） | 只按位解析，不猜位域含义 |

解码结果保留每个字段的 `raw_hex`、`value`、`enum`、`bits`，所以即使缩放系数猜错，也不需要重新抓包，只改 spec 再跑一次。

## CAN：candump 日志的三种解码

支持 `candump -L` / `-ta` 风格的行，例如 `(1700000000.000000) can0 7E0#0322F190`（`##` 表示 CAN FD）。

1. **ISO-TP 重组**：单帧（PCI `0x0`）、首帧（`0x1`）、连续帧（`0x2`）按“接口 + CAN ID”重组；流控帧（`0x3`）单独标记。缺首帧、长度不足会作为错误记录，不会静默丢弃。
2. **UDS**：识别服务号（`0x10`/`0x11`/`0x14`/`0x19`/`0x22`/`0x2E`/`0x31`/`0x3E` 等）、DID、子功能、例程号、DTC 状态掩码；否定响应 `0x7F` 会翻译 NRC（如 `0x31 requestOutOfRange`、`0x78 responsePending`）。不在表里的服务会保留 `payload_hex` 并提示“查 ISO 14229”。
3. **OBD-II**：模式 1 常用 PID（转速、车速、水温、节气门、MAF、电压、油量等）直接给物理值与单位；未知 PID 只报原始字节。
4. **J1939**：29 位扩展帧按 PGN 拆优先级、源地址、目标地址，内置 EEC1/CCVS/DM1 三个 PGN 的名字；当前只做单帧解码，BAM/TP 重组不冒充完成。

## 不猜规则（错误比“看起来能跑”更值钱）

- 帧头、长度、校验、字节序：任何一项没有依据就停下来问，不写 heuristic 兜底。
- 校验失败、残帧、无法解析的行：作为数据记录到 `errors`，`--strict` 时退出 4；CI 可以用退出码挡住“截断抓包”。
- 未知枚举/未识别服务：给出 `unknown` 与原始字节，不编造名字。
- 修改协议后先用一段**已知正确**的抓包做回归，再上真实设备。

## 与 HIL 闭环的关系

`embedded_dev.py hil` 负责“编译 → 烧录 → 抓串口 → 判定通过令牌”，`decode` 负责把抓到的字节变成字段级证据。两者输出都是 JSON，可直接附在完成报告里，证据等级标为“已确认”（带原始抓包）而不是“工程推论”。

## 来源与致谢

- 思路参考：serial-protocol-tester-skill（PolyForm Noncommercial，仅借鉴“spec 驱动解码”的产品形态，未复制代码）、Lissio（Apache-2.0，CAN ISO-TP/UDS/J1939 工具形态）、TaoCosm-Dev STM32_AutoDebug_Universal_Kit（MIT，令牌式 HIL 闭环）。具体许可说明见 `THIRD_PARTY_NOTICES.md`。
