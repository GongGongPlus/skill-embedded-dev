# Zephyr RTOS and embedded Linux

Purpose: two very different integration paths that are easy to confuse. Zephyr
is a build system plus devicetree plus Kconfig on a microcontroller; embedded
Linux is a kernel plus user space on an SoC. Neither is "just another HAL".

## Zephyr: minimum viable workflow

```bash
west init -m https://github.com/zephyrproject-rtos/zephyr --mr v4.1.0 my-ws
cd my-ws && west update
west boards | grep -i <board-name>            # exact board identifier
west build -b <board> samples/basic/blinky    # build
west flash                                    # runner selected from board def
west debug                                    # gdb via the same runner
west build -t run                             # run on native_sim / qemu
```

Rules that save days:

1. The board identifier is a fact. Read it from `west boards`, the board
   documentation, or `zephyr/boards/<vendor>/<board>/`. Never guess.
2. Pin assignments live in **devicetree**, not in C. If a pin does not work, the
   overlay or the board `.dts` is wrong before the driver is wrong.
3. `west flash` chooses a runner (openocd, pyocd, jlink, nrfjprog, espressif,
   dfu). Print it with `west flash --context` / verbose build log; if two probes
   are attached, the runner may pick the wrong one.
4. Kconfig symbols are facts too: `west build -t menuconfig`, or grep the
   generated `.config`. A driver compiled out by default explains "the API
   returns -ENODEV".
5. Console/log routing is devicetree + Kconfig: `CONFIG_LOG`, `zephyr,console`,
   `zephyr,shell-uart`. A silent board often has no console node enabled.

### Devicetree overlay example

```dts
/ {
    aliases {
        my-uart = &usart2;
    };
};

&usart2 {
    status = "okay";
    current-speed = <115200>;
    pinctrl-0 = <&usart2_tx_pa2 &usart2_rx_pa3>;
    pinctrl-names = "default";
};
```

After changing overlays, rebuild and inspect the resolved tree:

```bash
west build -t devicetree_target -- -d build   # or open build/zephyr/zephyr.dts
grep -n "usart2" build/zephyr/zephyr.dts
```

### Flashing with an explicit backend

When the board definition's runner is not what you want, be explicit rather than
letting auto-selection guess (same no-guess rule as the CLI):

```bash
python scripts/embedded_dev.py flash --image build/zephyr/zephyr.elf \
  --backend openocd --openocd-interface interface/cmsis-dap.cfg \
  --openocd-target target/stm32f4x.cfg
```

## Embedded Linux: what to check when "the peripheral is dead"

| Layer | Command | What it proves |
|---|---|---|
| Kernel saw the controller | `dmesg | grep -i i2c` | driver bound or deferred |
| Device tree | `ls /proc/device-tree/`, `dtc -I fs /proc/device-tree` | node exists with expected compatible |
| Pin mux | `cat /sys/kernel/debug/pinctrl/*/pinmux-pins` | pin claimed by the right function |
| GPIO | `gpiodetect`, `gpioinfo`, `gpioget`, `gpioset` | line usable without a custom driver |
| I2C | `i2cdetect -y 1`, `i2cget -y 1 0x48 0x00 w` | device ACKs; then read a known register |
| SPI | `spidev_test -D /dev/spidev0.0 -v` | loopback/MOSI-MISO wiring and mode |
| UART | `stty -F /dev/ttyUSB0 115200 raw -echo`, `cat` | baud/framing/no-flow-control |
| CAN | `ip link set can0 up type can bitrate 500000`, `candump can0` | bus termination and bitrate |
| Clock/power | `cat /sys/kernel/debug/clk/clk_summary` | controller clock actually enabled |

Permission and udev problems present as "works as root, not as service". Fix with
a udev rule and group membership, not by running everything as root.

## Cross-build and verify loop

1. Build in a container pinned to the toolchain version (reproducible evidence).
2. Run unit tests on host, then `native_sim` or QEMU for logic that does not need
   real pins; mark the result as **engineering inference** for target behaviour.
3. Flash and run on hardware: only then is the result **confirmed**.
4. For HIL, drive the UART and assert a token (see `examples/`); keep the
   recovery path (button, SWD, DFU) documented before any irreversible step.

## Zephyr test/build integration for CI

```bash
west build -b native_sim tests/my_test -t run       # host-runnable logic
twister -p native_sim -T tests                      # Zephyr's own test runner
west build -b <real-board> app && west flash --context
```

`twister` is the only reliable way to run many Zephyr tests without hardware; use
it in CI and keep real-board runs as a separate, manually approved job. Never
let CI flash a board that is also connected to someone's bench session.

## Failure signatures

- `west flash` says success but old firmware runs: runner flashed a different
  image path, or the board needs a reset after flashing.
- Devicetree overlay silently ignored: overlay filename must match
  `<board>.overlay` or be passed with `-DEXTRA_DTC_OVERLAY_FILE`; check the build
  log for "overlay" lines.
- Linux `i2cdetect` shows the address but reads fail: clock stretching or missing
  pull-ups, not a driver bug; scope SCL/SDA before touching code.
- Works on devkit, fails on custom board: pin mux/devicetree for the new board,
  not the vendor BSP.
