# RISC-V MCU debugging

Purpose: RISC-V bare-metal and RTOS targets fail differently from Cortex-M. This
file gives the CSR vocabulary, the exception-to-root-cause mapping, and the
commands to capture evidence. Treat every mapping as an engineering inference
until the real `mcause`/`mtval` values from the board confirm it.

## Register model (machine mode)

| CSR | Meaning | Debug value |
|---|---|---|
| `mstatus` | MIE/MPIE/MPP stack | Was an interrupt enabled when the trap happened? |
| `mtvec` | trap vector base; low bits = mode (0 direct, 1 vectored) | Wrong mode explains "all exceptions go to handler 0" |
| `mepc` | PC of the trapping instruction | Crash location; subtract 4 for compressed/compressed-pair ambiguity |
| `mcause` | exception code (MSB set = interrupt) | Root-cause class, see table |
| `mtval` | bad address, bad instruction, or 0 | The single most useful value on load/store faults |
| `mie` / `mip` | interrupt enables/pending | Lost interrupt vs lost handler |
| `mscratch` | often holds the trap stack pointer | Context switch bugs |
| `pmpcfg*`/`pmpaddr*` | physical memory protection | A load fault at a valid address often means PMP denies it |

Interrupt cause = `mcause` with the MSB set; the code is `mcause & 0x7fffffff`.
Common external interrupts: machine timer (7), machine external (11).

## Exception code map

| `mcause` | Name | Usual root cause | First probe |
|---|---|---|---|
| 0 | instruction address misaligned | jump through a corrupted function pointer / compressed alignment | `mepc`, call stack |
| 1 | instruction access fault | PC outside mapped flash, PMP deny, erased flash | `mepc` vs linker map |
| 2 | illegal instruction | wrong `-march` (e.g. code uses `M` extension on RV32EC), corrupted PC, uninitialized function pointer | `mtval` (instruction bits), disassemble around `mepc` |
| 3 | breakpoint | `ebreak` left in, debugger breakpoint, `__builtin_trap()` | who executed it |
| 4 | load address misaligned | unaligned struct/pointer cast, packed struct misuse, DMA buffer offset | `mtval`, surrounding code |
| 5 | load access fault | null/dangling pointer, address outside RAM/flash, PMP, peripheral clock gated | `mtval` (address) |
| 6 | store/AMO address misaligned | same as 4 for writes | `mtval` |
| 7 | store/AMO access fault | writing to flash/ROM, null pointer, PMP, read-only region | `mtval` |
| 8 | environment call from U | user-mode `ecall` without handler | mode config |
| 9 | environment call from S | | |
| 11 | environment call from M | `ecall` in bare-metal code intended as breakpoint | |
| 12/13/15 | page faults | only with MMU (Linux-class cores) | |

For load/store faults, the sequence is: read `mtval` → map the address against
the linker map / peripheral map → decide "valid address but not enabled" vs
"invalid address" → only then read source. A fault address inside a valid RAM
window almost always means a bad pointer or PMP, not a hardware defect.

## Stack and call-stack capture

On trap, save a frame before calling any C function: at minimum `mepc`,
`mstatus`, `mcause`, `mtval`, `ra`, `sp`, `gp`, `tp`, `t0–t6`, `s0–s11`, `a0–a7`.
Print them in a fixed order so two crashes are comparable. For task crashes under
FreeRTOS, also print the current TCB pointer and the task name (read via the
port's `pxCurrentTCB`).

`objdump -d` the firmware and check whether `mepc` lands on an instruction
boundary and inside a known function. If `mepc` is in `.text` but 2 bytes off,
suspect a corrupted return address or a vector-table entry with the wrong mode
bits.

## Common RISC-V MCU families

| Family | Core | Debug transport | Notes |
|---|---|---|---|
| ESP32-C3 | RV32IMC @160 MHz | built-in USB Serial/JTAG, or JTAG | `mcause` visible via panic handler / OpenOCD |
| ESP32-C6/H2/P4 | RV32 | USB/JTAG | panic output includes MEPC/MTVAL |
| GD32VF103 | RV32IMAC (Nuclei) | JTAG/SWD-like | needs Nuclei OpenOCD config, not a stock `riscv` target |
| CH32V003 | RV32EC @48 MHz | single-wire debug (SDI) | no `M` extension: multiplication/division in software |
| SiFive FE310 (HiFive1) | RV32IMAC | JTAG (FT2232) | memory-mapped CLINT/PLIC layout |
| RP2350 (Hazard3) | RV32IMAC | SWD | dual-arch part; confirm the core really booted RISC-V |
| BL808 | RV32 + others | JTAG | multi-core; check which hart trapped |

## Toolchain and build evidence

```bash
# xPack / Zephyr SDK toolchains (pick the one your project already uses)
riscv-none-elf-gcc -march=rv32imc -mabi=ilp32 -c main.c -o main.o
riscv-none-elf-objdump -d -S build/firmware.elf | sed -n '/<fault_handler>:/,+20p'
riscv-none-elf-readelf -l build/firmware.elf      # loadable segments vs addresses
```

Match `-march`/`-mabi` to the core. A mismatch usually shows up as illegal
instruction (2) on the first multiply/divide/atomic. Verify the vector table
entries point to real functions (`readelf -x .text` or `objdump -s -j .text`).

## OpenOCD / GDB capture

```bash
# generic RISC-V, config depends on the probe and the SoC
openocd -f interface/ftdi/olimex-arm-usb-tiny-h.cfg -f target/riscv.cfg \
        -c "init; halt; reg mcause; reg mtval; reg mepc; reg mstatus; resume; exit"
```

ESP32-C3 example (IDF ships the config):

```bash
openocd -f board/esp32c3-builtin.cfg -c "init; halt; reg mcause; reg mtval; reg mepc; exit"
python scripts/embedded_dev.py regs --from-text regs.txt --json   # decode saved dump
```

`probe-rs` and `pyocd` also speak RISC-V on supported targets; list what is
installed with `python scripts/embedded_dev.py env --json` and refuse to guess a
backend when several are present.

## Failure signatures seen in the field

- Illegal instruction right after enabling an FPU/DSP extension: the core lacks
  it or the toolchain emitted instructions gated by a config CSR.
- Load access fault with `mtval = 0x00000000`: null pointer dereference.
- Store access fault with `mtval` in flash: trying to write calibration data into
  `.text` because the linker script put it in ROM.
- Interrupt storm with `mcause` MSB never observed: `mstatus.MIE` was cleared and
  never restored (nested disable), or `mtvec` still points at the reset stub.
- Crash on first `printf`: newlib `malloc` before the heap region is initialized;
  check `_sbrk` and the heap/stack boundary in the linker script.
- Works under debugger, fails standalone: vector table not relocated, or the
  debugger is hiding a fault by halting on `ebreak` before the trap handler runs.

## Evidence rules

Report `mcause`, `mtval`, `mepc` as **confirmed** only when read from the chip;
a disassembly-based guess is **engineering inference**. If the core family's
trap CSRs are not documented in the datasheet you have, mark the conclusion
**insufficient data** and request the reference manual section — do not invent
bit meanings.
