# Size gate example

`demo-size.txt` is a saved `arm-none-eabi-size -A` transcript for an STM32F103C8
build. Keeping the transcript in the repository is deliberate: it lets the size
gate run in CI on a machine with no ARM toolchain installed.

## Hand-checkable numbers

| Bucket | Sections | Bytes |
|---|---|---|
| text | `.isr_vector` 304 + `.text` 2468 + `.rodata` 100 + `.ARM.exidx` 8 | **2880** |
| data | `.data` 40 | **40** |
| bss | `.bss` 472 + `.heap` 1024 + `.stack` 2048 | **3544** |
| ram | data + bss | **3584** |
| flash | text + data | **2920** |

The `.debug_*`, `.symtab`, `.strtab`, `.shstrtab` and `.comment` rows are present
in the transcript on purpose: they must **not** count towards flash, which is the
easiest way to produce a bogus "image too big" failure.

## Try it

```bash
python scripts/size_gate.py --size-output-file examples/size/demo-size.txt
python scripts/size_gate.py --size-output-file examples/size/demo-size.txt --max-text 8K --max-bss 8K   # 0
python scripts/size_gate.py --size-output-file examples/size/demo-size.txt --max-text 2K                # 1
python scripts/size_gate.py --size-output-file examples/size/demo-size.txt --json
python scripts/size_gate.py --size-output-file examples/size/demo-size.txt --write-baseline base.json
python scripts/size_gate.py --size-output-file examples/size/demo-size.txt --baseline base.json
```

## Live measurement

```bash
python scripts/size_gate.py --elf build/firmware.elf --max-text 64K --max-data 8K --max-bss 16K
```

The tool uses `arm-none-eabi-size -A` if it is on `PATH`, then plain `size`, and
exits 6 rather than guessing if neither exists.

## Why a baseline on top of a budget

A budget catches "too big". It does not catch "+2 KB since last week" while still
under the limit. `--write-baseline` records the current totals; a later run with
`--baseline` fails when any bucket grows by more than `--max-growth` bytes
(default 0, i.e. any growth fails). Commit the baseline alongside the firmware so
a regression is attributable to a specific change.

## Evidence boundary

`size -A` reports section sizes as the linker laid them out. It does not prove the
image boots, and it does not include data placed outside the measured ELF (boot
metadata, OTA headers, a second slot). Confirm those separately against the map
file and the flashing procedure.