# protocols 示例

这些文件演示“先用 JSON 契约描述协议，再解码抓包”，以及“把 CAN 日志直接翻译成 UDS / OBD-II / J1939 记录”。

## 文件

- `uart-env-sensor.json`：一个示例 UART 协议 spec（`embedded-protocol.v1`）。帧格式 `AA 55 | len | payload | crc16_modbus`，长度包含 2 字节校验，校验覆盖“校验字段之前的全部字节”。
- `capture-uart.hex`：与上面的 spec 对应的抓包（两帧：一帧传感器上报，一帧心跳）。
- `candump.log`：candump `-L` 风格的 CAN 日志（UDS 请求、UDS 否定响应、OBD-II 转速）。

## 命令

```bash
# 1) 只校验 spec 结构（不猜任何字段）
python scripts/embedded_dev.py decode validate --spec examples/protocols/uart-env-sensor.json

# 2) 解码 UART 抓包；--strict 会在校验和/帧错误出现时退出 4
python scripts/embedded_dev.py decode uart --spec examples/protocols/uart-env-sensor.json \
  --file examples/protocols/capture-uart.hex --strict

# 3) 解码 CAN 日志（ISO-TP 重组 + UDS/OBD-II/J1939）
python scripts/embedded_dev.py decode can --spec examples/protocols/uart-env-sensor.json \
  --log examples/protocols/candump.log
```

预期结果：

- UART：2 帧、0 个校验失败；第一帧 `msg_type=report`、`seq=7`、`temperature=23.45 degC`、`humidity=54.3 %RH`、`status_flags` 含 `sensor_ok` 与 `low_battery`。
- CAN：3 个 ISO-TP 单帧消息；其中 `0x7E0` 是 UDS `ReadDataByIdentifier` DID `0xF190`，`0x7E8` 是 NRC `requestOutOfRange`，`0x7DF` 是 OBD-II 转速 `1726 rpm`。

## 换成真实项目时

1. 从 datasheet 抄下帧头、长度字段、字节序、校验算法与覆盖范围，写进 spec；不确定就留空并标 `unknown`，**不要猜**。
2. 先 `decode validate`，再喂一段已知正确的抓包核对字段值。
3. 字段名、单位、缩放系数写进 spec，让证据可复现；`unknown` 字节会原样保留在 `unknown_payload_hex`。
4. 把 spec 和抓包一起提交，代码评审时可以直接重放。
