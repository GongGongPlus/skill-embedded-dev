# Board pinouts and strapping pins

Purpose: stop guessing which GPIO a board exposes. A pinout is a fact about a
specific board revision; the official schematic and the MCU datasheet are the
only authorities. The tables below are the *default* wiring of common boards and
must be re-checked against the silkscreen and schematic for the board in hand.

Evidence level of everything in this file: **engineering inference from public
schematics** until verified with a multimeter or a register read-back on the
actual board. Say so in the completion report.

## Rules before wiring anything

1. Identify the exact board revision (silk text, USB connector, flash size).
2. Check every pin against: MCU datasheet pinout table, board schematic, and the
   bootloader/strapping documentation. Three sources, then wire.
3. Mark pins that are **boot strapping**, **flash/PSRAM**, **input-only**,
   **5V-tolerant**, or **shared with a debug peripheral**. These categories
   cause more field failures than wrong code.
4. When a board is unknown, stop and ask for the schematic. Do not interpolate
   from a similar-looking board.
5. Verify with a register read before blaming firmware:

```bash
python scripts/embedded_dev.py regs --values "moder=0x00000000" --json   # placeholder dump
# on target: read GPIOx->MODER / IDR through OpenOCD or GDB
openocd -f interface/cmsis-dap.cfg -f target/stm32f1x.cfg -c "init; mdw 0x40010800 2; exit"
```

## STM32F103C8T6 "Blue Pill" / "Black Pill"

| Signal | Pin | Note |
|---|---|---|
| SWDIO / SWCLK | PA13 / PA14 | never disable in code; test point pads on board |
| User LED | PC13 | active-low, sourcing only ~3 mA, not for load |
| USART1 TX/RX | PA9 / PA10 | usually the 4-pin header next to USB |
| USART2 TX/RX | PA2 / PA3 | PA3 also TIM2_CH4; check board |
| USART3 TX/RX | PB10 / PB11 | |
| I2C1 SCL/SDA | PB6 / PB7 | 4.7 kΩ pull-ups usually present on modules, not on board |
| SPI1 NSS/SCK/MISO/MOSI | PA4 / PA5 / PA6 / PA7 | PA4 also ADC; NSS software-controlled |
| USB FS DM/DP | PA11 / PA12 | needs 1.5 kΩ pull-up on DP on some clones |
| HSE crystal | PD0 / PD1 | 8 MHz on most boards; check for 12 MHz variants |
| BOOT0 | separate pin/header | 0 = flash boot, 1 = system memory bootloader |
| VBAT | dedicated pin | leave open if RTC not used; do not tie to 3.3 V without diode |

Clone-specific traps: some boards omit the USB pull-up, some wire the LED to
PC13 through a resistor that makes it dim, some have the 32.768 kHz crystal
missing. Check `RCC->BDCR` before assuming the LSE exists.

## STM32F401/F411 "Black Pill"

| Signal | Pin | Note |
|---|---|---|
| SWDIO / SWCLK | PA13 / PA14 | |
| User LED | PC13 | active-low |
| USART1 TX/RX | PA9 / PA10 | |
| USART2 TX/RX | PA2 / PA3 | PA3 can also be SWO if configured |
| USB FS DM/DP | PA11 / PA12 | |
| I2C1 SCL/SDA | PB6 / PB7 | |
| SPI1 SCK/MISO/MOSI | PA5 / PA6 / PA7 | |
| BOOT0 | header | 0 = flash |

## ESP32-DevKitC (ESP32-WROOM-32)

| Signal | Pin | Note |
|---|---|---|
| UART0 TX0 / RX0 | GPIO1 / GPIO3 | boot log; do not use for application UART |
| Strapping: BOOT | GPIO0 | low at reset enters download mode |
| Strapping: MTDI (flash voltage) | GPIO12 | must not be pulled high at boot on 3.3 V flash modules |
| Strapping: MTDO | GPIO15 | boot log control; floating/low at boot |
| Strapping: GPIO2, GPIO5 | GPIO2 / GPIO5 | must be low or floating at boot (GPIO2 also on-board LED) |
| SPI flash (internal) | GPIO6–GPIO11 | **never use**; connected to integrated SPI flash |
| Input-only | GPIO34–GPIO39 | no output, no internal pull-up/pull-down |
| I2C default (Arduino) | GPIO21 SDA / GPIO22 SCL | external pull-ups required |
| VSPI | SCK GPIO18 / MISO GPIO19 / MOSI GPIO23 / CS GPIO5 | CS pin may need changing |
| DAC | GPIO25 / GPIO26 | only DAC-capable pins |
| Touch | GPIO4/12/13/14/15/27/32/33 | ADC2 touch conflicts with Wi-Fi |

Rules: GPIO6–11 are reserved for the in-package flash. GPIO34–39 have no
output drivers. ADC2 pins cannot be read while Wi-Fi is active (use ADC1:
GPIO32–39). Any pin used as a strapping pin must be checked at reset time, not
just in firmware.

## ESP32-C3-DevKitM-1

| Signal | Pin | Note |
|---|---|---|
| UART0 RX/TX | GPIO20 / GPIO21 | boot log |
| Strapping: BOOT | GPIO9 | low at reset enters download mode |
| Strapping: GPIO2 / GPIO8 | GPIO2 / GPIO8 | must be high at boot; GPIO8 also on-board RGB LED |
| SPI flash (internal) | GPIO11–GPIO17 | **never use** |
| USB Serial/JTAG D-/D+ | GPIO18 / GPIO19 | native USB, no external bridge needed |
| ADC1 | GPIO0–GPIO4 | |
| I2C | any GPIO (matrix) | e.g. GPIO8 SDA / GPIO9 SCL on DevKitM |

## Raspberry Pi Pico (RP2040)

| Signal | Pin | Note |
|---|---|---|
| UART0 TX/RX | GP0 / GP1 | also default debug UART |
| I2C0 / I2C1 | GP4/GP5 / GP2/GP3 | external pull-ups |
| SPI0 | GP16 RX / GP17 CS / GP18 SCK / GP19 TX | |
| On-board LED | GP25 | |
| ADC | GP26/GP27/GP28 | 12-bit, 3.3 V reference |
| BOOTSEL | button to QSPI CS | hold at reset for USB mass-storage bootloader |
| SWD | SWCLK/SWDIO/GND test pads | not on header on Pico |

## nRF52840 DK / Nice!Nano

| Signal | Pin | Note |
|---|---|---|
| SWDIO / SWDCLK | P0.13 / P0.14 on DK | |
| UART TX/RX | P0.06 / P0.08 on DK (default) | check board definition in Zephyr |
| NFC | P0.09 / P0.10 | must be configured as NFC or GPIO deliberately |
| USB D-/D+ | dedicated | |
| QSPI flash | dedicated pins | do not repurpose on boards with external flash |

## AVR ATmega328P "Nano"

| Signal | Pin | Note |
|---|---|---|
| UART RX/TX | D0 / D1 | D0 is also used by the bootloader on reset |
| External INT | D2 / D3 | |
| PWM | D3 / D5 / D6 / D9 / D10 / D11 | timers 0/1/2 |
| I2C SDA/SCL | A4 / A5 | external pull-ups |
| SPI | D10 CS / D11 MOSI / D12 MISO / D13 SCK | LED on D13 |
| ADC | A0–A7 | 10-bit, AVcc reference |
| Crystal | 16 MHz on most Nano clones | `F_CPU` must match |

## Record template (paste into the completion report or a board note)

```text
board: <name> rev <silk> mcu: <part> package: <LQFP48...>
source: schematic <file/URL> + datasheet section <n>, checked <date> by <who>
peripheral | signal | mcu pin | board pin | mux/config | note
USART1     | TX     | PA9     | J1-3     | AF push-pull | 115200 8N1
```

## When the pinout is the bug

Symptom patterns: peripheral silent but register init looks correct; works on one
board revision, fails on another; works until an unrelated feature is enabled.
Check in this order: (1) strapping pin state at reset, (2) pin used by internal
flash/PSRAM, (3) input-only pin driven as output, (4) alternate function not
enabled, (5) 5 V logic into a non-tolerant pin, (6) solder bridge or clone
routing difference. Only after these, suspect the driver.
