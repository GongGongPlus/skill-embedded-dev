# RTT example

Two fixtures that let `rtt_capture.py` be tested without a probe.

## `control-block.bin` (60 bytes)

A `SEGGER_RTT_CB` plus one `SEGGER_RTT_BUFFER_UP`, little-endian, 32-bit:

| Offset | Field | Value |
|---|---|---|
| 0 | `acId[16]` | `"SEGGER RTT\0..."` |
| 16 | `MaxNumUpBuffers` | 1 |
| 20 | `MaxNumDownBuffers` | 0 |
| 24 | `sName[16]` | `"Terminal"` |
| 40 | `pBuffer` | `0x20001000` |
| 44 | `SizeOfBuffer` | 64 |
| 48 | `WrOff` | 20 |
| 52 | `RdOff` | 40 |
| 56 | `Flags` | 0 (`BLOCK_IF_FULL`) |

## `buffer.bin` (64 bytes)

The ring buffer contents. `WrOff (20) < RdOff (40)` means **the writer wrapped**,
so the live region is `buffer[40:64]` followed by `buffer[0:20]` — 44 bytes:

```text
boot ok
RTT_TEST_PASSED
stage 2 done
idle
```

This is the case a naive `buffer[rd:wr]` slice silently gets wrong (it returns an
empty string), which is why it is the primary fixture.

## Try it

```bash
python scripts/rtt_capture.py --control-block-file examples/rtt/control-block.bin \
  --buffer-file examples/rtt/buffer.bin --expect RTT_TEST_PASSED          # exits 0
python scripts/rtt_capture.py --control-block-file examples/rtt/control-block.bin \
  --buffer-file examples/rtt/buffer.bin --expect NEVER_PRINTED            # exits 4
python scripts/rtt_capture.py --control-block-file examples/rtt/control-block.bin \
  --buffer-file examples/rtt/buffer.bin --json
```

## Live capture over J-Link

```bash
python scripts/rtt_capture.py --device STM32F103C8 --control-block-address 0x20000000 --dry-run
```

The dry run prints the exact J-Link commander script (`connect`, `halt`, one
`mem ADDR 4` per word) without touching hardware. The live path performs two
J-Link passes: one for the control block, one for the ring buffer whose address
and size come from that control block.

## Evidence boundary

Only the offline path is verified here. The live path drives `JLinkExe` and needs
a probe, a connected target and a firmware that actually calls `SEGGER_RTT_Init`;
none of that is available in this environment, so a live capture has **not been
validated** and must be confirmed on real hardware before being trusted.

Not supported: down-buffers (host-to-target), `SEGGER_RTT_LOCK/UNLOCK` interlock
during a live read, and multi-buffer iteration beyond `--buffer-index`.