"""Unit tests for scripts/embedded_dev.py (offline; no board, no toolchain needed)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("embedded_dev", REPO / "scripts" / "embedded_dev.py")
ed = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(ed)


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ed.main(argv)
    return code, out.getvalue(), err.getvalue()


class TestHexAndFault(unittest.TestCase):
    def test_parse_hex(self):
        self.assertEqual(ed.parse_hex("0x02000000"), 0x02000000)
        self.assertEqual(ed.parse_hex("02000000"), 0x02000000)
        self.assertEqual(ed.parse_hex("0x1_000"), 0x1000)
        self.assertEqual(ed.parse_hex(32), 32)
        self.assertIsNone(ed.parse_hex(None))

    def test_decode_precise_bus_fault(self):
        result = ed.decode_fault(cfsr=0x0000_8200, bfar=0x2000_0100)
        names = [hit["name"] for hit in result["cfsr_bits"]]
        self.assertIn("PRECISERR", names)
        self.assertIn("BFARVALID", names)
        self.assertEqual(result["bfar"], 0x2000_0100)
        self.assertTrue(any("BFAR" in step for step in result["next_steps"]))

    def test_decode_stack_faults(self):
        for bit_name in ("MSTKERR", "STKERR", "UNSTKERR"):
            hit = next(item for item in ed.CFSR_BITS if item[2] == bit_name)
            result = ed.decode_fault(cfsr=1 << hit[1])
            self.assertTrue(any("stack" in step.lower() for step in result["next_steps"]), bit_name)

    def test_decode_imprecise_and_forced(self):
        result = ed.decode_fault(cfsr=1 << 10, hfsr=1 << 30)
        self.assertIn("IMPRECISERR", [hit["name"] for hit in result["cfsr_bits"]])
        self.assertIn("FORCED", [hit["name"] for hit in result["hfsr_bits"]])
        self.assertTrue(any("write buffer" in step.lower() for step in result["next_steps"]))

    def test_decode_empty_is_not_silent(self):
        result = ed.decode_fault()
        self.assertEqual(result["cfsr_bits"], [])
        self.assertTrue(result["next_steps"])

    def test_format_fault_contains_register_names(self):
        text = ed.format_fault(ed.decode_fault(cfsr=1 << 1))
        self.assertIn("CFSR=0x00000002", text)
        self.assertIn("DACCVIOL", text)

    def test_fault_cli_returns_target_fault_code(self):
        code, out, _ = run_main(["fault", "--cfsr", "0x02000000", "--bfar", "0x20000000"])
        self.assertEqual(code, ed.EXIT_FAULT)
        self.assertIn("BFAR", out)

    def test_fault_cli_zero_returns_ok(self):
        code, _, _ = run_main(["fault", "--cfsr", "0x0"])
        self.assertEqual(code, ed.EXIT_OK)


class TestBackends(unittest.TestCase):
    def test_select_backend_explicit_missing(self):
        from unittest import mock

        with mock.patch.object(ed, "find_tool", return_value=None):
            backend, candidates = ed.select_backend("cmake", ed.BUILD_BACKENDS)
        self.assertIsNone(backend)
        self.assertEqual(candidates, [])

    def test_select_backend_auto_ambiguous_does_not_guess(self):
        from unittest import mock

        with mock.patch.object(ed, "find_tool", side_effect=lambda names: "/usr/bin/x" if any(n in ("cmake", "platformio") for n in names) else None):
            backend, candidates = ed.select_backend("auto", ed.BUILD_BACKENDS)
        self.assertIsNone(backend)
        self.assertEqual(sorted(candidates), ["cmake", "platformio"])

    def test_select_backend_auto_single(self):
        from unittest import mock

        with mock.patch.object(ed, "find_tool", side_effect=lambda names: "/usr/bin/cmake" if "cmake" in names else None):
            backend, _ = ed.select_backend("auto", ed.BUILD_BACKENDS)
        self.assertEqual(backend, "cmake")

    def test_build_commands(self):
        cmake = ed.build_commands("cmake", "app", "build", "blink")
        self.assertEqual(cmake[0][:4], ["cmake", "-S", "app", "-B"])
        self.assertIn("--target", cmake[1])
        pio = ed.build_commands("platformio", "app", platformio_env="bluepill")
        self.assertEqual(pio[0], ["platformio", "run", "-d", "app", "-e", "bluepill"])
        idf = ed.build_commands("idf", "app")
        self.assertEqual(idf[0], ["idf.py", "-C", "app", "build"])

    def test_flash_openocd_requires_config(self):
        with self.assertRaises(ValueError):
            ed.flash_commands("openocd", "fw.elf")

    def test_flash_openocd_command(self):
        commands, _ = ed.flash_commands(
            "openocd",
            "build/fw.elf",
            openocd_interface="interface/cmsis-dap.cfg",
            openocd_target="target/stm32f1x.cfg",
        )
        joined = " ".join(commands[0])
        self.assertIn("program build/fw.elf verify reset exit", joined)

    def test_flash_backends_build_expected_commands(self):
        cases = [
            ({"backend": "pyocd", "target": "stm32f103c8"}, ["pyocd", "flash", "--target", "stm32f103c8"]),
            ({"backend": "esptool", "chip": "esp32c3", "port": "COM7"}, ["esptool.py", "--chip", "esp32c3", "--port", "COM7"]),
            ({"backend": "nrfjprog"}, ["nrfjprog", "--program"]),
            ({"backend": "probe-rs", "chip": "nrf52840"}, ["probe-rs", "download"]),
            ({"backend": "stm32cubeprogrammer"}, ["STM32_Programmer_CLI", "-c", "port=SWD"]),
        ]
        for kwargs, expected_prefix in cases:
            backend = kwargs.pop("backend")
            commands, _ = ed.flash_commands(backend, "fw.bin", **kwargs)
            self.assertEqual(commands[0][: len(expected_prefix)], expected_prefix, backend)

    def test_flash_jlink_generates_script(self):
        commands, extras = ed.flash_commands("jlink", "build/fw.hex", jlink_device="STM32F103C8")
        self.assertIn("embedded_dev_flash.jlink", extras)
        self.assertIn("loadfile build/fw.hex", extras["embedded_dev_flash.jlink"])
        self.assertIn("-CommanderScript", commands[0])

    def test_commands_to_text_quotes_spaces(self):
        text = ed.commands_to_text([["openocd", "-c", "program /a b/fw.elf exit"]])
        self.assertIn("'program /a b/fw.elf exit'", text)

    def test_run_commands_dry_run_does_not_execute(self):
        results = ed.run_commands([["definitely-not-a-real-binary-xyz"]], dry_run=True)
        self.assertEqual(results[0]["exit_code"], None)
        self.assertTrue(results[0]["dry_run"])

    def test_run_commands_short_circuits_on_failure(self):
        commands = [
            [sys.executable, "-c", "import sys; sys.exit(3)"],
            [sys.executable, "-c", "print('should not run')"],
        ]
        results = ed.run_commands(commands)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["exit_code"], 3)


class TestRegisters(unittest.TestCase):
    def test_parse_register_dump(self):
        text = "pc (/32): 0x08001234\nlr (/32): 0x0800abcc\npsp (/32): 0x20007ff0\ncfsr: 0x02000000\nbfar: 0x20000100\n"
        found = ed.parse_registers(text)
        self.assertEqual(found["pc"], 0x08001234)
        self.assertEqual(found["cfsr"], 0x02000000)
        self.assertEqual(found["bfar"], 0x20000100)

    def test_regs_values_cli(self):
        code, out, _ = run_main(["regs", "--values", "pc=0x08001234,cfsr=0x00008200,bfar=0x20000100", "--json"])
        self.assertEqual(code, ed.EXIT_OK)
        payload = json.loads(out)
        self.assertEqual(payload["pc"], 0x08001234)
        self.assertEqual([hit["name"] for hit in payload["cfsr_bits"]], ["PRECISERR", "BFARVALID"])

    def test_regs_from_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            dump = Path(tmp) / "regs.txt"
            dump.write_text("pc = 0x08000000\ncfsr = 0x00000001\n", encoding="utf-8")
            code, out, _ = run_main(["regs", "--from-text", str(dump), "--json"])
        self.assertEqual(code, ed.EXIT_OK)
        self.assertEqual(json.loads(out)["cfsr_bits"][0]["name"], "IACCVIOL")


class TestMonitorAndReport(unittest.TestCase):
    def test_monitor_mock_token_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp) / "uart.bin"
            capture.write_bytes(b"boot\r\nBLINK OK\r\nTEST_PASSED\r\n")
            code, out, _ = run_main(["monitor", "--mock-file", str(capture), "--expect", "TEST_PASSED", "--json"])
        self.assertEqual(code, ed.EXIT_OK)
        self.assertTrue(json.loads(out)["token_found"])

    def test_monitor_mock_token_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp) / "uart.bin"
            capture.write_bytes(b"boot\r\n")
            code, _, err = run_main(["monitor", "--mock-file", str(capture), "--expect", "TEST_PASSED"])
        self.assertEqual(code, ed.EXIT_DEVICE)

    def test_report_valid_and_invalid(self):
        valid = {
            "schema_version": 1,
            "summary": "demo",
            "changes": ["main.c"],
            "evidence": [{"claim": "build ok", "level": "已确认"}],
            "commands": [{"command": "build", "result": "exit 0"}],
            "risks": [],
            "next_step": "flash on board",
        }
        self.assertEqual(ed.validate_report(valid), [])
        self.assertNotEqual(ed.validate_report({"summary": "x"}), [])

    def test_report_cli_validates_file(self):
        valid = {
            "schema_version": 1,
            "summary": "demo",
            "changes": ["main.c"],
            "evidence": [{"claim": "build ok", "level": "confirmed"}],
            "commands": [{"command": "build"}],
            "risks": [],
            "next_step": "flash",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            path.write_text(json.dumps(valid), encoding="utf-8")
            code, out, _ = run_main(["report", "--file", str(path)])
        self.assertEqual(code, ed.EXIT_OK)
        self.assertIn("OK", out)

    def test_report_emit_example(self):
        code, out, _ = run_main(["report", "--emit-example", "--json"])
        self.assertEqual(code, ed.EXIT_OK)
        self.assertEqual(ed.validate_report(json.loads(out)), [])


class TestCliSurface(unittest.TestCase):
    def test_env_json(self):
        code, out, _ = run_main(["env", "--json"])
        self.assertEqual(code, ed.EXIT_OK)
        payload = json.loads(out)
        for key in ("version", "python", "tools", "build_backends", "flash_backends", "serial_ports"):
            self.assertIn(key, payload)

    def test_build_dry_run(self):
        code, out, _ = run_main(["build", "--project", ".", "--backend", "cmake", "--dry-run"])
        self.assertEqual(code, ed.EXIT_OK)
        self.assertIn("cmake -S . -B build", out)

    def test_flash_dry_run(self):
        code, out, _ = run_main(
            [
                "flash",
                "--image",
                "fw.elf",
                "--backend",
                "openocd",
                "--openocd-interface",
                "interface/cmsis-dap.cfg",
                "--openocd-target",
                "target/stm32f1x.cfg",
                "--dry-run",
            ]
        )
        self.assertEqual(code, ed.EXIT_OK)
        self.assertIn("program fw.elf verify reset exit", out)

    def test_flash_missing_required_options_is_config_error(self):
        code, _, _ = run_main(["flash", "--image", "fw.elf", "--backend", "esptool", "--dry-run"])
        self.assertEqual(code, ed.EXIT_CONFIG)

    def test_unknown_subcommand_exits_nonzero(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                ed.main(["nope"])


TASKLIST = """
Task          State  Prio  Stack  Num
IDLE          R      0     120    3
main          B      2     64     4
sensor        B      3     900    5
--------------
"""


class TestStackReport(unittest.TestCase):
    def test_parse_tasklist_reads_free_words_and_state(self):
        tasks = ed.parse_tasklist(TASKLIST)
        self.assertEqual([task["name"] for task in tasks], ["IDLE", "main", "sensor"])
        self.assertEqual(tasks[1]["free_words"], 64)
        self.assertEqual(tasks[1]["state"], "B")

    def test_parse_tasklist_skips_banners_and_short_lines(self):
        text = "Task State Prio Stack Num\n-------\nnoise\n" + TASKLIST
        tasks = ed.parse_tasklist(text)
        self.assertEqual(len(tasks), 3)

    def test_parse_tasklist_returns_empty_for_unrelated_text(self):
        self.assertEqual(ed.parse_tasklist("boot ok\nheap 4096\n"), [])

    def test_stack_flags_tasks_below_threshold(self):
        code, out, _ = run_main(["stack", "--min-free", "128", "--json"])
        self.assertNotEqual(code, ed.EXIT_OK, "stdin is empty, this must not claim success")

    def test_stack_unreadable_stdin_is_config_error_not_traceback(self):
        """A test runner (or a closed pipe) raises on read; that must be a clean exit."""

        class UnreadableStdin:
            def read(self, *args, **kwargs):
                raise OSError("pytest: reading from stdin while output is captured!")

        original = sys.stdin
        sys.stdin = UnreadableStdin()
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = ed.main(["stack", "--min-free", "128", "--json"])
        finally:
            sys.stdin = original
        self.assertEqual(code, ed.EXIT_CONFIG)

    def test_stack_json_marks_below_threshold_tasks(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasklist.txt"
            path.write_text(TASKLIST, encoding="utf-8")
            code, out, _ = run_main(["stack", "--tasklist", str(path), "--min-free", "128", "--json"])
        payload = json.loads(out)
        self.assertEqual(code, ed.EXIT_FAULT)
        self.assertEqual(payload["below_threshold"], ["IDLE", "main"])
        self.assertEqual(payload["worst"]["name"], "main")
        self.assertEqual(payload["worst"]["free_bytes"], 256)

    def test_stack_passes_when_every_task_is_above_threshold(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasklist.txt"
            path.write_text(TASKLIST, encoding="utf-8")
            code, out, _ = run_main(["stack", "--tasklist", str(path), "--min-free", "16", "--json"])
        self.assertEqual(code, ed.EXIT_OK)
        self.assertEqual(json.loads(out)["below_threshold"], [])

    def test_stack_word_bytes_is_respected(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasklist.txt"
            path.write_text(TASKLIST, encoding="utf-8")
            _, out, _ = run_main(["stack", "--tasklist", str(path), "--min-free", "16", "--word-bytes", "8", "--json"])
        payload = json.loads(out)
        self.assertEqual(payload["tasks"][2]["free_bytes"], 900 * 8)

    def test_stack_missing_file_is_config_error(self):
        code, _, err = run_main(["stack", "--tasklist", "does-not-exist.txt"])
        self.assertEqual(code, ed.EXIT_CONFIG)
        self.assertIn("cannot read", err)


class TestSymbolize(unittest.TestCase):
    def test_symbolize_requires_an_elf(self):
        code, _, err = run_main(["symbolize", "--addr", "0x08000100"])
        self.assertEqual(code, ed.EXIT_CONFIG)
        self.assertIn("--elf", err)

    def test_symbolize_requires_an_address(self):
        code, _, err = run_main(["symbolize", "--elf", __file__])
        self.assertEqual(code, ed.EXIT_CONFIG)
        self.assertIn("at least one address", err)

    def test_symbolize_rejects_unparsable_address(self):
        code, _, err = run_main(["symbolize", "--elf", __file__, "--addr", "zzz"])
        self.assertEqual(code, ed.EXIT_CONFIG)
        self.assertIn("cannot parse address", err)

    def test_symbolize_missing_elf_file_is_config_error(self):
        code, _, err = run_main(["symbolize", "--elf", "nope.elf", "--addr", "0x08000100"])
        self.assertEqual(code, ed.EXIT_CONFIG)
        self.assertIn("not found", err)

    def test_addr2line_candidates_cover_arm_xtensa_and_riscv(self):
        candidates = ed.ENV_TOOLS["addr2line"]
        self.assertTrue(any("arm-none-eabi" in name for name in candidates))
        self.assertTrue(any("xtensa" in name for name in candidates))
        self.assertTrue(any("riscv" in name for name in candidates))


if __name__ == "__main__":
    unittest.main()
