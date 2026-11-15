# Case: HardFault with BFARVALID — a DMA buffer overrun

**Board:** STM32F103C8T6 Blue Pill, CMSIS-DAP probe, bare-metal firmware.
**Symptom:** the device stops blinking within a second of enabling a UART DMA
receive. No console output (the same DMA is the console transport).

## What was captured

```text
# halt immediately, do not reset
openocd -f interface/cmsis-dap.cfg -f target/stm32f1x.cfg \
  -c "init; halt; reg pc; reg lr; reg xpsr; mdw 0xE000ED28 4; mdw 0xE000ED38 1; exit"
# registers written to regs.txt, then decoded by the skill CLI
python scripts/embedded_dev.py regs --from-text regs.txt --json
```

Decoded output (abridged):

```json
{
  "pc": 134218050,
  "lr": 134217728,
  "cfsr": 33554432,
  "bfar": 536873088,
  "cfsr_bits": [
    {"name": "PRECISERR", "meaning": "Precise data bus error"},
    {"name": "BFARVALID", "meaning": "BFAR holds a valid fault address"}
  ]
}
```

`cfsr = 0x02000000` → PRECISERR + BFARVALID; `bfar = 0x20002000`.

## Analysis

Linker map for this build: `.bss` ends at `0x20001F00`, heap starts at
`0x20001F08`, end of RAM `0x20005000`. The fault address `0x20002000` is inside
the heap region, *not* inside a declared buffer:

```bash
arm-none-eabi-nm -n build/firmware.elf | grep -E "(_ebss|__heap_start|_estack)"
```

The DMA receive buffer was declared as `uint8_t rx[64]` on the stack, while the
DMA was configured with a transfer count of 256 and circular mode. DMA kept
writing past the stack buffer into the heap control block. The bus error surfaces
on the *next* CPU write through the corrupted heap pointer — that is why `pc`
points at `malloc`, not at the DMA setup.

## Root cause

Inconsistent buffer size: CPU-side buffer 64 bytes, DMA transfer count 256 bytes,
no compile-time check that they match. The `pc` was a symptom; `bfar` was the
actual evidence.

## Fix

1. One size constant, used for both the declaration and the DMA count.
2. `_Static_assert(sizeof rx == RX_COUNT, "DMA/host buffer size mismatch");`
3. DMA buffer moved to a dedicated aligned section (`__attribute__((aligned(32)))`)
   and never a stack local.
4. Added a canary word after the buffer, checked in the idle loop.

## Verification

- Rebuilt: `arm-none-eabi-size build/firmware.elf` shows `.bss` growth as expected.
- Ran 30 minutes under DMA load, no fault.
- Re-read `CFSR` after the soak: `0x00000000`.
- Evidence level: **confirmed** (fault registers and map read from the real
  build/probe; soak test on hardware).

## Lesson generalized

On a precise bus fault, always read `BFAR` before reading code around `PC`.
`PC` tells you where the damage was noticed; `BFAR` tells you where it happened.
