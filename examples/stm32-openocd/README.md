# STM32F103C8 bare-metal example

Minimal, dependency-free firmware for a "Blue Pill" STM32F103C8T6 used as the
CLI's runnable smoke test: no HAL, no CMSIS, no vendor headers, just registers.
PC13 blinks, SysTick provides a 1 ms time base, and the image is small enough to
flash in seconds.

```text
stm32-openocd/
|-- CMakeLists.txt           # cross build, post-build .hex/.bin + size report
|-- cmake/arm-none-eabi.cmake
|-- linker.ld                # 64K flash / 20K RAM, stack+heap reserve
|-- startup_stm32f103c8.s    # vector table, .data copy, .bss zero, Default_Handler trap
|-- src/main.c               # RCC/GPIOC/SysTick register writes, blink loop
|-- openocd.cfg              # CMSIS-DAP + SWD + target/stm32f1x.cfg
```

## Build

```bash
python scripts/embedded_dev.py env --json                     # is the toolchain present?
python scripts/embedded_dev.py build --project examples/stm32-openocd \
  --backend cmake --build-dir examples/stm32-openocd/build --dry-run
# drop --dry-run to execute; or use cmake directly:
cmake -S examples/stm32-openocd -B examples/stm32-openocd/build \
  -DCMAKE_TOOLCHAIN_FILE=examples/stm32-openocd/cmake/arm-none-eabi.cmake
cmake --build examples/stm32-openocd/build
```

Expected evidence: `arm-none-eabi-size` prints a text/data/bss table that fits
`64K` flash and `20K` RAM, and `firmware.hex` / `firmware.bin` appear in the
build directory.

## Flash

```bash
python scripts/embedded_dev.py flash --image examples/stm32-openocd/build/firmware.elf \
  --backend openocd --openocd-interface interface/cmsis-dap.cfg \
  --openocd-target target/stm32f1x.cfg --dry-run
# for ST-Link use interface/stlink.cfg; for another probe, name its interface explicitly
```

The CLI refuses to guess when several probes or backends are plausible: it lists
candidates and exits `6`. That is intentional — a wrong OpenOCD invocation can
erase the wrong flash bank or disable SWD.

## Observe

- PC13 LED should toggle about every 0.5 s (active low).
- With OpenOCD attached, read back the GPIO state:

```bash
openocd -f examples/stm32-openocd/openocd.cfg \
  -c "init; halt; mdw 0x4001100C 1; exit"     # GPIOC_ODR
```

- Capture the boot banner if you add UART output:

```bash
python scripts/embedded_dev.py monitor --port COM7 --baud 115200 --expect "BOOT_OK"
```

## Fault drill (optional)

Deliberately write to an invalid address inside `main` and rebuild; the default
handler traps in a `b .` loop. Then capture the evidence *before* resetting:

```bash
openocd -f examples/stm32-openocd/openocd.cfg \
  -c "init; halt; reg pc; reg lr; reg xpsr; mdw 0xE000ED28 4; exit" | tee regs.txt
python scripts/embedded_dev.py regs --from-text regs.txt --json
python scripts/embedded_dev.py fault --cfsr 0x00008200 --bfar 0x20000100
```

See `examples/cases/01-hardfault-bfar.md` for a full walkthrough of turning
`BFAR`/`CFSR` into a root cause.

## Notes and limits

- Tested target: STM32F103C8T6 with CMSIS-DAP probe. ST-Link works by swapping
  the interface config; other targets need their own `target/*.cfg`.
- `-Wall -Wextra -Werror=implicit-function-declaration` is on; the example is
  warnings-clean with GNU Arm 13.x.
- This is a teaching/smoke-test image, not a production board support package:
  clock tree, watchdog, and option bytes are intentionally left to the reader.
