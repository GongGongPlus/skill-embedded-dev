# SVD example

`demo.svd` is a small CMSIS-SVD fragment for two peripherals on an STM32F103-style
device. It is deliberately limited but exercises every construct the decoder has
to handle:

| Construct | Where |
|---|---|
| `bitOffset` / `bitWidth` fields | `RCC.CR`, `RCC.APB2ENR` |
| `lsb` / `msb` fields | covered by unit tests (`test_lsb_msb_form_is_also_accepted`) |
| `enumeratedValues` | `RCC.CR.HSION`, `RCC.CR.PLLSRC`, `RCC.APB2ENR.IOPAEN`, `GPIOA.CRL.MODE0` |
| `derivedFrom` inheritance | `GPIOA.CRH` derives from `GPIOA.CRL` |
| Undefined gaps between fields | `RCC.CR` bits 2, 15:8, 17, 23:22, 31:25 |

`demo-dump.txt` holds a captured dump in the format `--dump` accepts
(`ADDRESS=VALUE` or `ADDRESS VALUE`, hex). It intentionally includes two rows the
SVD cannot explain, so the ambiguity path is exercised:

- `0x40021004` — inside RCC's window but no register at that offset
- `0x50000000` — outside every peripheral in this fragment

Both are reported as `UNKNOWN` with a reason, and the command exits 4. That is
the point: a register the SVD does not describe must never look like a decoded one.

## Try it

```bash
python scripts/svd_decode.py --svd examples/svd/demo.svd --dump examples/svd/demo-dump.txt
python scripts/svd_decode.py --svd examples/svd/demo.svd --dump examples/svd/demo-dump.txt --json
python scripts/svd_decode.py --svd examples/svd/demo.svd --values "0x40021018=0x00004005"
python scripts/svd_decode.py --svd examples/svd/demo.svd --find PLL
python scripts/svd_decode.py --svd examples/svd/demo.svd --find APB2ENR --json
```

Expected decode of `RCC.CR = 0x011D0003`:

```text
[ 0:0 ] HSION  = 0x1 -> ON
[ 1:1 ] HSIRDY = 0x1
[16:16] PLLSRC = 0x1 -> HSE
[21:18] PLLMUL = 0x7
[24:24] PLLON  = 0x1
```

## Evidence boundary

The SVD is the only authority for what a bit means. Device-specific behaviour
(reserved-bit read/write semantics, errata, write-1-to-clear) is not in the SVD
and is not inferred here; confirm it against the reference manual before acting
on a decoded value.

Supported: `bitOffset`/`bitWidth`, `lsb`/`msb`, `addressOffset`-based registers,
`enumeratedValues`, same-file `derivedFrom`.

Not supported (reported, never guessed): `dim`/`dimIncrement` array expansion,
`cluster` elements, cross-file `derivedFrom`, and per-peripheral address blocks
declared with `addressBlock` beyond the fixed 0x1000 window used for lookup.
