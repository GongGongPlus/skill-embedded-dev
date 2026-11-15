# Case: UART token HIL — turn a serial log into a pass/fail gate

**Goal:** every candidate firmware build is accepted or rejected by the device
itself, not by reading a log with your eyes. This is the smallest useful
hardware-in-the-loop loop and it maps directly to `scripts/embedded_dev.py
monitor --expect` exit codes.

## Contract

1. Flash the candidate image.
2. Open the serial port at a fixed baud rate.
3. Firmware prints `TEST_PASSED` only after its self-test completes.
4. Host waits N seconds for the token; missing token = fail; host exits 4.

## Host side

```bash
python scripts/embedded_dev.py flash --image build/firmware.bin --backend openocd \
  --openocd-interface interface/cmsis-dap.cfg --openocd-target target/stm32f1x.cfg

python scripts/embedded_dev.py monitor --port COM7 --baud 115200 \
  --expect "TEST_PASSED" --timeout 5
echo "monitor exit=$?"     # 0 = token seen, 4 = timeout/missing token
```

The monitor writes the capture to a file when `--capture` is supplied (check
`--help` for the current flag set) and never interprets the log — a token match
is the only success signal.

## Firmware side (order matters)

```c
/* after clock, GPIO, and peripheral init */
self_test_result_t r = run_self_test();
if (r.ok) {
    uart_write_str("\r\nTEST_PASSED\r\n");
} else {
    uart_write_str("\r\nTEST_FAILED: ");
    uart_write_str(r.reason);
    uart_write_str("\r\n");
}
```

Traps found in the field:

- Printing the token *before* the UART is fully initialized truncates it; the
  host then sees a partial match and fails.
- Printing on a `printf` retarget that buffers means the token arrives after the
  host timeout.
- On ESP32/AVR, the bootloader also writes to UART0: open the port *after* reset
  and tolerate boot noise; `--expect` searches the whole capture.
- Do not reboot the device while the host is listening unless the contract says
  so; a reboot can produce a stale token from the old image.

## CI shape

```yaml
- name: HIL smoke (self-hosted runner with the board attached)
  run: |
    python scripts/embedded_dev.py flash --image build/firmware.bin --backend openocd ...
    python scripts/embedded_dev.py monitor --port "$SERIAL_PORT" --baud 115200 --expect TEST_PASSED --timeout 5
```

Rules: the runner must own the board exclusively; check probe serial numbers when
more than one is connected; never run HIL and a human debug session on the same
board; record the probe id and firmware hash in the report.

## Verification and evidence level

- Success run: token seen within 2.3 s, three consecutive runs.
- Negative test: flash a build with the self-test disabled; monitor exits 4.
- Evidence level: **confirmed** for the tested image and board, **engineering
  inference** for other boards until repeated there.
