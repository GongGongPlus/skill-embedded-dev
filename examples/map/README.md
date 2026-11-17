# Map file example

`demo.map` is a trimmed GNU ld map file for an STM32F103C8 build. It is small
enough to check by hand and still shows everything the parser has to survive:

- an `Archive member included` preamble and a `Discarded input sections` list
  (neither may inflate the totals)
- a `Memory Configuration` table (FLASH 64K, RAM 20K) that provides the capacity
  figures the percentages are computed against
- output sections written as `name 0xADDR 0xSIZE` at column 0
- contributing object rows indented below each section, one symbol per row
- a symbol whose name sits on its own line and continues on the next row
- linker-script assignments such as `_sdata = .`, which carry no size

## Hand-checkable numbers

| Section | Size |
|---|---|
| `.isr_vector` | 0x130 = 304 |
| `.text` | 0x9a4 = 2468 |
| `.rodata` | 0x64 = 100 |
| `.data` | 0x28 = 40 |
| `.bss` | 0x1d8 = 472 |

- flash = isr_vector + text + rodata + data = **2912 bytes** of 65536 (4.44%)
- RAM = data + bss = **512 bytes** of 20480 (2.50%)

## Try it

```bash
python scripts/map_parse.py --map examples/map/demo.map
python scripts/map_parse.py --map examples/map/demo.map --json
python scripts/map_parse.py --map examples/map/demo.map --top 5
python scripts/map_parse.py --map examples/map/demo.map --max-flash 2K      # exits 1
python scripts/map_parse.py --map examples/map/demo.map --write-baseline baseline.json
python scripts/map_parse.py --map examples/map/demo.map --baseline baseline.json
```

## Evidence boundary

The report only states what the map file states. If a project splits code across
extra regions (`.ccmram`, external QSPI), extend `FLASH_SECTIONS` /
`RAM_SECTIONS` in `scripts/map_parse.py`; the tool will not infer a region it was
never told about, and it exits 6 rather than reporting zeros for a file it cannot
recognise.
