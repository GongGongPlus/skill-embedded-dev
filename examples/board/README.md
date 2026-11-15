# Board-facts examples

Offline fixtures for `embedded_dev.py board`. Both files describe the same
minimal STM32F103 board so the two parsers can be compared: a KiCad XML netlist
(topology, series passives, connectors) and a CubeMX `.ioc` (pin roles and
alternate functions).

## Files

- `demo.kicadxml` — KiCad XML netlist: `U1` STM32F103C8Tx, decoupling caps,
  8 MHz crystal, LED with series resistor, SWD header, two connectors.
- `demo.ioc` — CubeMX project: `STM32F103C8Tx`, family `STM32F1`, LQFP48,
  NVIC/RCC/SYS/USART1; PA9/PA10 USART1, PA13/PA14 SWD, PC13 labeled
  `STATUS_LED`, PD0/PD1 oscillator.

## Commands

```bash
python scripts/embedded_dev.py board controller --netlist examples/board/demo.kicadxml
python scripts/embedded_dev.py board controller --netlist examples/board/demo.kicadxml --main-controller U1
python scripts/embedded_dev.py board from-ioc --file examples/board/demo.ioc
```

`board controller` detects the controller by score and refuses to guess when two
MCU-like candidates are within 15 points: it exits 4 with the candidate list and
asks for `--main-controller`. Power/ground pins are excluded from the signal
table. Series resistors/inductors/ferrites are traced through; the LED pin is
reported as `net: LED_CTRL` with a trace `via R1 → endpoint D1`. Decoupling
capacitors only appear as direct nodes. PDF input is refused (exit 6) instead
of being guessed.

`board from-ioc` prints the MCU, peripherals and per-pin signal/mode/label, and
adds risk notes for pins such as PA13/PA14 (SWD), PC13 (weak drive),
PC14/PC15 (32 kHz crystal) and BOOT0.

Pin maps are design declarations. A mapped pin does not prove the firmware
configures it; verify against firmware code, then against a logic analyzer or
oscilloscope before writing "verified" in a report.
