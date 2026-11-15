"""Offline tests for board_map.py: netlist parsing, controller detection and pin tracing.

No hardware and no schematic tooling are involved: the fixtures are plain KiCad
XML and CubeMX .ioc files. Ambiguity must fail loudly, never guess.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples" / "board"


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, REPO / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


bm = load_module("board_map_tests", "scripts/board_map.py")


class TestNetlistParsing(unittest.TestCase):
    def setUp(self):
        self.text = (EXAMPLES / "demo.kicadxml").read_text(encoding="utf-8")
        self.board = bm.parse_kicad_netlist(self.text)

    def test_components_are_parsed(self):
        components = self.board["components"]
        self.assertEqual(components["U1"]["value"], "STM32F103C8Tx")
        self.assertEqual(components["R1"]["value"], "330")
        self.assertTrue(components["U1"]["mounted"])

    def test_net_members_are_parsed(self):
        nets = self.board["nets"]
        self.assertIn("UART_TX", nets)
        refs = {node["ref"] for node in nets["UART_TX"]["nodes"]}
        self.assertIn("U1", refs)
        self.assertIn("J1", refs)

    def test_non_xml_input_is_rejected(self):
        with self.assertRaises(bm.ConfigError):
            bm.parse_kicad_netlist("not xml at all")


class TestClassifyAndMount(unittest.TestCase):
    def test_reference_designators(self):
        self.assertEqual(bm.classify_ref("U1"), "ic")
        self.assertEqual(bm.classify_ref("R1"), "passive")
        self.assertEqual(bm.classify_ref("C1"), "passive")
        self.assertEqual(bm.classify_ref("Y1"), "crystal")
        self.assertEqual(bm.classify_ref("J1"), "connector")
        self.assertEqual(bm.classify_ref("D1"), "diode")

    def test_unmounted_detection(self):
        self.assertTrue(bm.is_unmounted({"dnp": "DNP"}, "0"))
        self.assertTrue(bm.is_unmounted({}, "N.C."))
        self.assertFalse(bm.is_unmounted({}, "330"))


class TestControllerMap(unittest.TestCase):
    def setUp(self):
        self.text = (EXAMPLES / "demo.kicadxml").read_text(encoding="utf-8")
        self.map_data, self.ambiguous = bm.build_controller_map(self.text, "demo.kicadxml")
        self.pins = self.map_data["pins"]

    def test_controller_is_detected_with_high_confidence(self):
        self.assertEqual(self.map_data["controller"]["ref"], "U1")
        self.assertEqual(self.map_data["controller"]["confidence"], "high")
        self.assertFalse(self.ambiguous)

    def test_power_and_ground_pins_are_excluded(self):
        nets = {pin["net"] for pin in self.pins}
        self.assertNotIn("GND", nets)
        self.assertNotIn("+3V3", nets)
        self.assertNotIn("VDD", nets)

    def test_uart_pins_keep_direct_connector_nodes(self):
        uart = {pin["pin_name"]: pin for pin in self.pins if pin["net"].startswith("UART")}
        self.assertIn("PA9", uart)
        direct_refs = {node["ref"] for node in uart["PA9"]["direct_nodes"]}
        self.assertIn("J1", direct_refs)

    def test_series_resistor_is_traced_to_led_endpoint(self):
        led = [pin for pin in self.pins if pin["net"] == "LED_CTRL"][0]
        self.assertTrue(led["traced"])
        endpoint = led["traced"][0]["endpoint"]
        self.assertEqual(endpoint["ref"], "D1")
        self.assertEqual(led["traced"][0]["via"][0]["ref"], "R1")

    def test_traced_endpoint_never_reuses_the_via_part_itself(self):
        for pin in self.pins:
            for route in pin["traced"]:
                endpoint = route.get("endpoint")
                if not endpoint:
                    continue
                via_refs = {item["ref"] for item in route["via"]}
                self.assertNotIn(endpoint["ref"], via_refs)

    def test_forced_controller_override(self):
        forced, ambiguous = bm.build_controller_map(self.text, "demo.kicadxml", "U1")
        self.assertEqual(forced["controller"]["ref"], "U1")
        self.assertFalse(ambiguous)

    def test_forced_non_ic_controller_is_rejected(self):
        with self.assertRaises(bm.ConfigError):
            bm.build_controller_map(self.text, "demo.kicadxml", "J1")

    def test_unknown_forced_controller_is_rejected(self):
        with self.assertRaises(bm.ConfigError):
            bm.build_controller_map(self.text, "demo.kicadxml", "U9")


class TestIoc(unittest.TestCase):
    def setUp(self):
        self.text = (EXAMPLES / "demo.ioc").read_text(encoding="utf-8")
        self.ioc = bm.parse_ioc(self.text)

    def test_mcu_and_peripherals_are_parsed(self):
        self.assertEqual(self.ioc["mcu"]["name"], "STM32F103C8Tx")
        self.assertEqual(self.ioc["mcu"]["family"], "STM32F1")
        self.assertIn("USART1", self.ioc["mcu"]["peripherals"])

    def test_pins_keep_signal_and_label(self):
        pins = {pin["pin"]: pin for pin in self.ioc["pins"]}
        self.assertEqual(pins["PA9"]["signal"], "USART1_TX")
        self.assertEqual(pins["PC13"]["label"], "STATUS_LED")

    def test_pdf_input_is_refused_not_guessed(self):
        code = bm.main(["controller", "--netlist", "schematic.pdf"])
        self.assertEqual(code, bm.EXIT_CONFIG)


if __name__ == "__main__":
    unittest.main()
