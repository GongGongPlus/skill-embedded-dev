# Wireless: BLE, Wi-Fi, LoRa, 2.4 GHz

Purpose: wireless bugs are usually configuration, timing, or RF — not business
logic. This file gives a triage order and the concrete evidence to capture before
changing code. Every claim about range or throughput is an estimate until measured
on the target hardware; label it accordingly.

## Universal triage order

1. **Radio initialized and calibrated?** Read the chip's status/error register or
   the driver's init return. A radio that never started cannot be debugged from
   the application layer.
2. **Antenna and matching.** Wrong antenna, missing matching network, or a
   ground-plane keep-out violation explains 80% of "short range" complaints.
   Scope the RF path only if you have a spectrum analyzer; otherwise compare with
   a known-good board of the same model.
3. **Regulatory/channel config.** Region, channel, TX power, and duty cycle are
   legal constraints. An illegal channel that works in the lab is still a bug.
4. **Protocol frame on the air.** Capture with a sniffer (BLE: nRF Sniffer +
   Wireshark; Wi-Fi: monitor mode; LoRa: SDR or a second node) before reading
   stacks.
5. **Power and timing.** Connection interval, advertising interval, sleep mode,
   and clock accuracy (ppm) cause intermittent failures that look random.

## BLE

| Concept | Practical check |
|---|---|
| Advertising | `adv` payload ≤ 31 bytes legacy; extended advertising needs controller support. Name UUID 128-bit is 16 bytes plus overhead. |
| Connection parameters | interval / latency / supervision timeout must satisfy `timeout > (1+latency) * interval * 1.25 ms`; a bad tuple can be rejected or drop connections. |
| ATT MTU | default 23 bytes; request larger MTU explicitly; long writes need prepared-write or L2CAP CoC. |
| Notifications | the client must write the CCCD before notifications are sent; check CCCD state, not just the notify call's return. |
| Bonding | bonds stored in NVM; erasing flash erases keys and devices silently stop reconnecting. |
| UUID byte order | 128-bit UUIDs are stored little-endian in the packet; copy-paste bugs here are common. |

Tools: nRF Connect (phone), `bluetoothctl` (Linux), ESP32 NimBLE examples, nRF
Sniffer + Wireshark for over-the-air truth. Capture: connection interval,
MTU, RSSI, and the exact GATT handle that failed.

Signature failures: advertising name truncated (payload overflow), notification
call succeeds but nothing arrives (no CCCD), reconnect storms (stale bond),
throughput far below estimate (MTU not negotiated, or one notification per
connection event).

## Wi-Fi (ESP32-class)

| Symptom | First evidence |
|---|---|
| Cannot connect | `esp_wifi` error code, AP channel, auth mode, RSSI; scan result list vs configured SSID |
| Connects then drops | reason code from the disconnect event, power-save mode, AP idle timeout |
| DHCP fails | static IP test, `ip` config; DHCP timeout is not a radio problem |
| Poor throughput | channel width, coexistence with BLE, TCP window, power save |
| Works on one AP, not another | 802.11 mode (b/g/n), PMF/WPA3, hidden SSID |

Rules: log the disconnect *reason code* every time; keep STA retry bounded with
backoff; never busy-loop scanning. For provisioning, separate the credential
path from the data path so a wrong password cannot brick the device.

## LoRa / LoRaWAN

| Parameter | Meaning | Trap |
|---|---|---|
| SF (7–12) | spreading factor | higher SF = longer range, much longer airtime, duty-cycle pressure |
| BW (125/250/500 kHz) | bandwidth | both ends must match exactly |
| CR (4/5–4/8) | coding rate | |
| Frequency | region plan (EU868, US915, AS923…) | sub-band and channel mask must match the network |
| OTAA/ABP | join method | OTAA keys DevEUI/AppEUI/AppKey; ABP has no join, session keys fixed |
| ADR | adaptive data rate | enable only for static nodes |
| Duty cycle | 1% typical in EU | exceeding it is both illegal and self-interfering |

Evidence to capture: join request/accept, uplink counter, downlink window timing
(RX1/RX2), RSSI/SNR from the network server, airtime calculated vs duty cycle.
A join failure with correct keys is usually frequency plan or antenna.

## 2.4 GHz proprietary

- **nRF24L01+**: address width and pipe config must match both ends; payload
  width is fixed per pipe; ACK payload requires `W_TX_PAYLOAD_NOACK` vs
  `W_TX_PAYLOAD` discipline; check `STATUS` after every operation. Watch for
  clone modules with missing decoupling — they fail only at higher TX power.
- **ESP-NOW**: peer MAC must be added before send; channel must match the Wi-Fi
  channel; encrypted peers need the LMK set.
- **Zigbee/Thread/Matter**: commissioning and network keys dominate; capture with
  a sniffer before reading application code; check network steering and
  permit-join window.

## Antenna, EMC, and coexistence

- 2.4 GHz keep-out: no copper, battery, or metal within the antenna keep-out
  area; follow the module's reference design, not "close enough".
- USB 3.x and switching regulators raise the 2.4 GHz noise floor: move the
  antenna, add shielding, or change the regulator's switching frequency.
- Matching network components are board-specific; do not swap values.
- TX power vs regulatory limit: verify with the module's certified setting; a
  higher setting is not automatically better (PA compression, current spikes).
- Coexistence: Wi-Fi + BLE on one chip share the radio; time-slicing causes
  latency spikes. Measure before blaming the stack.

## Power and timing

Battery devices: measure average current, not just sleep current. Advertising
interval, Wi-Fi DTIM, LoRa RX windows, and BLE connection latency dominate.
Use a current profile (Nordic PPK2, Joulescope, or a shunt + scope) as the
evidence; "should be microamps" is an estimate, not a measurement.

## Evidence rules

- RSSI/range numbers are **estimates** unless measured with a calibrated setup.
- A sniffer capture is **confirmed** evidence for what went over the air.
- A datasheet's "up to X dBm" is not a measurement of your board.
- If the radio's registers cannot be read, mark the failure **insufficient data**
  and stop guessing at the protocol layer.

## Quick commands

```bash
# BLE on Linux host
bluetoothctl scan on
bluetoothctl connect <MAC>

# ESP32 serial log capture; add --expect to assert a token
python scripts/embedded_dev.py monitor --port COM7 --baud 115200 --expect "WIFI_CONNECTED"

# chip identity / flash read for evidence
esptool.py --port COM7 flash_id
nrfjprog --ids
```
