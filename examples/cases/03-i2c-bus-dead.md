# Case: the I2C bus is dead — triage before touching the driver

**Symptom:** `HAL_I2C_Master_Transmit()` returns `HAL_BUSY` forever; the sensor
never responds; a logic analyzer is not available at first.

## Step 1 — is the bus physically alive?

```text
# with the target halted, read the GPIO mode and the I2C status registers
openocd -f interface/cmsis-dap.cfg -f target/stm32f1x.cfg \
  -c "init; halt; mdw 0x40010800 2; mdw 0x40005400 6; exit"
```

- Both SCL/SDA configured as open-drain alternate function? A push-pull SDA pin
  can fight a slave that is holding the line low.
- `I2C_SR1` BUSY bit stuck with no slave: a previous transaction was interrupted
  mid-byte and the bus is wedged.
- External 4.7 kΩ pull-ups measured to 3.3 V? Many breakout boards include
  pull-ups; many bare sensors do not.

## Step 2 — recover the bus, then re-test

Procedure that works without a scope:

1. Reconfigure SCL/SDA as GPIO open-drain.
2. Drive SCL low/high nine times while SDA is released, then generate a STOP
   (SDA low→high while SCL high).
3. Reconfigure back to I2C alternate function and issue an address-only probe.
4. If the probe still fails, power-cycle the sensor — some devices latch a stuck
   bus until power is removed.

This recovery belongs in the driver's error path, not in a one-off test script.

## Step 3 — look at the waveform only after the above

What to capture with a scope/logic analyzer:

| Observation | Meaning |
|---|---|
| SDA never goes high | pull-up missing / slave holding low / short |
| SCL held low by the slave | clock stretching; master must support it |
| Acknowledge bit high at the 9th clock | no device at that address |
| ACK then NACK on the first data byte | register address wrong or device in a different mode |
| Ringing / slow rise > 300 ns | pull-up too weak for the bus capacitance |
| Two masters talking at once | multi-master arbitration bug |

## Step 4 — address and configuration check

- 7-bit address vs 8-bit shifted address is the most common bug; verify by
  scanning the bus (`i2cdetect -y 1` on Linux) instead of trusting the header.
- Pull `SDA` low with a test pin and confirm the line actually reads low; a
  broken trace passes all register checks.

## Root cause seen in this case

The sensor module and a second board shared the bus, both populated with 4.7 kΩ
pull-ups, and a third device held SDA low after a brown-out. Recovery sequence +
power-cycle of the third device fixed it; the driver change alone (increasing
timeouts) only made the failure slower.

## Verification and evidence level

- Bus recovery function unit-tested with a GPIO mock in host tests.
- On hardware: 10 000 transactions with no `HAL_BUSY`, scope capture archived.
- Evidence level: **confirmed** on this board; the timeout value is
  **engineering inference** for other slaves.
