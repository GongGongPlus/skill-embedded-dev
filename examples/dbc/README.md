# DBC example

`demo.dbc` is a small Vector DBC file with the constructs a decoder has to get
right, and `demo-candump.log` is traffic that matches it.

| Construct | Where |
|---|---|
| Intel (little-endian) signals | `ENGINE_STATUS`: `EngineSpeed`, `EngineTemp`, `ThrottlePos` |
| Motorola (big-endian) signals | `GEARBOX`: `GearRatio` `7|16@0`, `OilPressure` `23|16@0` |
| Multiplexed message | `BODY_STATUS`: `DoorState` is `M`, `DoorFL`/`DoorFR` are `m0`, `WindowPos` is `m1` |
| `VAL_` enumerations | `EngineTemp`, `DoorState`, `DoorFL`, `DoorFR` |
| Sections that must be ignored | `BO_TX_BU_`, `CM_` |

## The bit-order rule that matters

Intel and Motorola do not share numbering, and mixing them up yields plausible
but wrong numbers.

- **Intel `@1`**: the start bit is the **LSB**; positions increase linearly, so
  `0|16@1` on bytes `E0 2E` gives `0x2EE0` = 12000 (little-endian).
- **Motorola `@0`**: the start bit is the **MSB**; positions walk down within a
  byte and jump to the next byte's bit 7 when a byte's bit 0 is passed, so
  `7|16@0` on bytes `0B B8` gives `0x0BB8` = 3000 (big-endian).

Hand-checkable cases fixed by the test suite (`data = 12 34 AB CD`):

| Signal | Expected | Why |
|---|---|---|
| `0|8@1` (Intel) | `0x12` | byte 0, LSB first |
| `0|16@1` (Intel) | `0x3412` | low byte first |
| `7|8@0` (Motorola) | `0x12` | start = bit 7 = MSB of byte 0 |
| `7|16@0` (Motorola) | `0x1234` | high byte first |
| `3|8@0` (Motorola) | `0x23` | crosses the byte boundary mid-byte |

## Try it

```bash
python scripts/dbc_decode.py --dbc examples/dbc/demo.dbc --log examples/dbc/demo-candump.log
python scripts/dbc_decode.py --dbc examples/dbc/demo.dbc --log examples/dbc/demo-candump.log --json
python scripts/dbc_decode.py --dbc examples/dbc/demo.dbc --hex "1A0#E02E828000000000"
python scripts/dbc_decode.py --dbc examples/dbc/demo.dbc --log examples/dbc/demo-candump.log --filter-id 200
```

Expected output for the demo log:

```text
0x1a0 ENGINE_STATUS [E02E828000000000] dlc=8
    EngineSpeed            3000.000 rpm   (raw 12000, intel)
    EngineTemp               90.000 degC  (raw 130, intel)
    ThrottlePos              50.196 %     (raw 128, intel)
0x200 GEARBOX [0BB81F4000000000] dlc=8
    GearRatio                 3.000 ratio (raw 3000, motorola)
    OilPressure             800.000 bar   (raw 8000, motorola)
0x7e0 0322F190  UNKNOWN ID: CAN ID is not defined in the DBC
```

The unknown ID is why that run exits 4: a frame the DBC cannot explain is an
ambiguity, not a silence.

## Evidence boundary

A DBC records what the network author *declared*, not what a specific ECU
actually sends. A decoded value is only as trustworthy as the DBC revision that
matches the firmware on the bus — record the DBC version alongside any capture.

Not supported (reported, never guessed): `SIG_VALTYPE_` IEEE float/double
signals, `BA_` attribute-based scaling overrides, and J1939 multi-packet
reassembly. For diagnostic traffic (ISO-TP/UDS/OBD-II/J1939) use
`embedded_dev.py decode can` instead, which already handles it.
