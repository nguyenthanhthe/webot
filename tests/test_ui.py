"""Tests of the uiautomator ADB driver helper."""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from wvbot.ui import Node, Ui, UiError, _parse_xml, describe


SAMPLE_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<hierarchy>"
    '<node text="Play" content-desc="" class="android.widget.Button" '
    'clickable="true" bounds="[10,20][110,120]" resource-id="btn_play" />'
    '<node text="" content-desc="Lobby" class="android.view.View" '
    'clickable="false" bounds="[0,0][100,50]" />'
    "</hierarchy>"
)


class UiTest(unittest.TestCase):
    def test_parse_xml_rejects_doctype_entity_and_oversized(self) -> None:
        with self.assertRaises(UiError):
            _parse_xml('<!DOCTYPE foo [<!ENTITY xxe "boom">]><hierarchy/>')
        with self.assertRaises(UiError):
            _parse_xml("x" * (4 * 1024 * 1024 + 1))

    def test_dump_find_and_describe(self) -> None:
        ui = Ui(serial="emulator-5554", adb="adb")
        proc = mock.Mock(returncode=0, stdout=f"garbage\n{SAMPLE_XML}\n", stderr="")
        with mock.patch("subprocess.run", return_value=proc) as run_mock:
            nodes = ui.dump()
        self.assertEqual(len(nodes), 2)
        self.assertEqual(nodes[0].center, (60, 70))
        self.assertEqual(nodes[0].label, "Play")
        self.assertEqual(nodes[1].label, "Lobby")

        _, kwargs = run_mock.call_args
        self.assertEqual(kwargs.get("encoding"), "utf-8")
        self.assertEqual(kwargs.get("errors"), "replace")

        self.assertEqual(len(Ui.find(nodes, text="play", clickable=True)), 1)
        self.assertEqual(len(Ui.find(nodes, desc_contains="lob")), 1)
        self.assertIn("'Play'", describe(nodes))

    def test_run_clears_connected_on_errors_and_reconnects(self) -> None:
        ui = Ui(connect="127.0.0.1:5555")
        ok = mock.Mock(returncode=0, stdout="", stderr="")
        fail = mock.Mock(returncode=1, stdout="", stderr="device offline")

        with mock.patch("subprocess.run", side_effect=[ok, fail, ok, ok]) as run_mock:
            ui.ensure_connected()
            self.assertTrue(ui._connected)
            with self.assertRaises(UiError):
                ui.tap(10, 20)
            self.assertFalse(ui._connected)
            ui.tap(30, 40)
            self.assertTrue(ui._connected)

        commands = [c[0][0] for c in run_mock.call_args_list]
        self.assertEqual(commands[0], ["adb", "connect", "127.0.0.1:5555"])
        self.assertEqual(commands[2], ["adb", "connect", "127.0.0.1:5555"])

    def test_run_clears_connected_on_timeout_or_missing_adb(self) -> None:
        ui = Ui(connect="127.0.0.1:5555")
        ui._connected = True
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="adb", timeout=5),
        ):
            with self.assertRaises(UiError):
                ui._run(["shell", "id"])
        self.assertFalse(ui._connected)

        ui._connected = True
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("no adb")):
            with self.assertRaises(UiError):
                ui._run(["shell", "id"])
        self.assertFalse(ui._connected)

    def test_screen_is_on(self) -> None:
        ui = Ui()
        with mock.patch.object(
            ui, "_run", side_effect=["mWakefulness=Dozing", "mWakefulness=Awake"]
        ):
            self.assertFalse(ui.screen_is_on())
            self.assertTrue(ui.screen_is_on())

        node = Node(text="Go", desc="", cls="Button", clickable=True, bounds=(0, 0, 10, 10))
        with mock.patch.object(ui, "_run") as run_mock:
            ui.tap_node(node)
        run_mock.assert_called_once_with(["shell", "input", "tap", "5", "5"], timeout=15)


if __name__ == "__main__":
    unittest.main()
