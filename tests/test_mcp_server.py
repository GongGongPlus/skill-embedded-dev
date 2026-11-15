"""Offline tests for mcp_server.py: tool surface, JSON-RPC handling and self-test."""

from __future__ import annotations

import importlib.util
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, REPO / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


mcp = load_module("mcp_server_tests", "scripts/mcp_server.py")


class TestToolSurface(unittest.TestCase):
    def test_expected_tools_are_exposed(self):
        names = {tool["name"] for tool in mcp.tools_list()}
        self.assertEqual(
            names,
            {
                "env",
                "build",
                "flash",
                "monitor",
                "regs",
                "symbolize",
                "stack",
                "fault",
                "decode",
                "report",
                "hil",
                "tune",
                "board",
                "memory",
            },
        )

    def test_every_tool_has_an_input_schema(self):
        for tool in mcp.tools_list():
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertIsInstance(tool["description"], str)
            self.assertTrue(tool["description"])

    def test_build_argv_adds_json_flag_and_required_args(self):
        argv = mcp.build_argv("flash", {"image": "fw.elf", "backend": "openocd"})
        self.assertEqual(argv[0], "flash")
        self.assertIn("--image", argv)
        self.assertIn("fw.elf", argv)
        self.assertIn("--json", argv)

    def test_missing_required_argument_is_rejected(self):
        with self.assertRaises(ValueError):
            mcp.build_argv("build", {})

    def test_action_tools_put_the_action_right_after_the_command(self):
        argv = mcp.build_argv("tune", {"action": "analyze", "csv": "telemetry.csv"})
        self.assertEqual(argv[:3], ["tune", "analyze", "--csv"])
        self.assertIn("--json", argv)
        memory = mcp.build_argv("memory", {"action": "search", "project": "proj", "query": "uart"})
        self.assertEqual(memory[0:2], ["memory", "search"])

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(ValueError):
            mcp.build_argv("tune", {"action": "autotune"})

    def test_action_is_required(self):
        with self.assertRaises(ValueError):
            mcp.build_argv("board", {"netlist": "demo.kicadxml"})


class TestJsonRpc(unittest.TestCase):
    def test_initialize(self):
        response = mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(response["id"], 1)
        self.assertIn("serverInfo", response["result"])
        self.assertEqual(response["result"]["protocolVersion"], mcp.PROTOCOL_VERSION)

    def test_ping(self):
        response = mcp.handle_request({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        self.assertEqual(response["result"], {})

    def test_tools_list(self):
        response = mcp.handle_request({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertIn("decode", names)
        self.assertIn("hil", names)

    def test_unknown_method_returns_error(self):
        response = mcp.handle_request({"jsonrpc": "2.0", "id": 4, "method": "nope"})
        self.assertEqual(response["error"]["code"], -32601)

    def test_notification_has_no_response(self):
        self.assertIsNone(mcp.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_env_tool_round_trip(self):
        response = mcp.handle_request(
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "env", "arguments": {}}}
        )
        self.assertFalse(response["result"]["isError"])
        text = response["result"]["content"][0]["text"]
        payload = json.loads(text)
        self.assertIn("build_backends", payload)

    def test_bad_tool_is_an_error(self):
        response = mcp.handle_request(
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "nope", "arguments": {}}}
        )
        self.assertEqual(response["error"]["code"], -32602)

    def test_fault_diagnosis_exit_3_is_not_an_error(self):
        result = mcp.call_tool("fault", {"cfsr": "0x02000000"})
        self.assertFalse(result["isError"])
        self.assertIn("DIVBYZERO", result["content"][0]["text"])

    def test_stack_below_threshold_is_a_diagnosis_not_a_failure(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tasklist = Path(tmp) / "tasklist.txt"
            tasklist.write_text(
                "Task State Prio Stack Num\nmain B 2 64 4\nsensor B 3 900 5\n", encoding="utf-8"
            )
            result = mcp.call_tool("stack", {"tasklist": str(tasklist), "min_free": "128"})
        self.assertFalse(result["isError"], result["content"][0]["text"])
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["below_threshold"], ["main"])

    def test_symbolize_requires_the_elf_argument(self):
        with self.assertRaises(ValueError):
            mcp.build_argv("symbolize", {"addr": "0x08000100"})


class TestSelfTest(unittest.TestCase):
    def test_self_test_passes(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = mcp.self_test()
        self.assertEqual(exit_code, 0, buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
