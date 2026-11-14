"""Unit tests for browser fallback modules (webbot, firefox, marionette, cdp, netfix)."""

from __future__ import annotations

import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from wvbot import cdp, firefox, netfix, webbot
from wvbot.config import AUTH_URL, Config
from wvbot.marionette import MAX_PACKET_BYTES, Marionette, MarionetteError
from wvbot.stream import StreamHub
from wvbot.ws import WebSocketError


class NetfixTest(unittest.TestCase):
    def test_ensure_dns_does_not_mutate_global_socket_timeout(self):
        prev_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(17.5)
        try:
            with patch.object(netfix, "_resolve_works", return_value=True):
                result = netfix.ensure_dns()
            self.assertTrue(result)
            self.assertEqual(socket.getdefaulttimeout(), 17.5)
        finally:
            socket.setdefaulttimeout(prev_timeout)

    def test_skips_termux_resolv_write_outside_termux(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_resolv = Path(tmp) / "missing_etc" / "resolv.conf"
            with patch.object(netfix, "RESOLV", fake_resolv):
                netfix._write_resolv("1.1.1.1")
                self.assertFalse(fake_resolv.exists())

    def test_current_nameserver_guards_bare_nameserver_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolv = Path(tmp) / "resolv.conf"
            resolv.write_text("# comment\nnameserver\nnameserver 8.8.8.8\n", encoding="utf-8")
            with patch.object(netfix, "RESOLV", resolv):
                self.assertEqual(netfix._current_nameserver(), "8.8.8.8")

            resolv.write_text("nameserver\n", encoding="utf-8")
            with patch.object(netfix, "RESOLV", resolv):
                self.assertEqual(netfix._current_nameserver(), "")


class FirefoxTest(unittest.TestCase):
    def test_find_firefox_prefers_binary_and_detects_missing(self):
        with (
            patch.object(firefox, "FIREFOX", Path("/nonexistent/termux/firefox")),
            patch("wvbot.firefox.shutil.which", side_effect=lambda name: "/usr/bin/firefox" if name == "firefox" else None),
        ):
            self.assertEqual(firefox.find_firefox(), "/usr/bin/firefox")

        with (
            patch.object(firefox, "FIREFOX", Path("/nonexistent/termux/firefox")),
            patch("wvbot.firefox.shutil.which", return_value=None),
            patch("wvbot.firefox._FALLBACK_FIREFOX_PATHS", ()),
        ):
            with self.assertRaises(FileNotFoundError):
                firefox.find_firefox()

    def test_configure_marionette_port_writes_user_js(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile"
            firefox._configure_marionette_port(profile, 3133)
            user_js = (profile / "user.js").read_text(encoding="utf-8")
            self.assertIn("marionette.port", user_js)
            self.assertIn("3133", user_js)

            # Re-running updates the port without duplicating stale lines
            firefox._configure_marionette_port(profile, 3244)
            user_js2 = (profile / "user.js").read_text(encoding="utf-8")
            self.assertIn("3244", user_js2)
            self.assertNotIn("3133", user_js2)

    def test_wait_for_marionette_aborts_when_process_exits_early(self):
        proc = MagicMock()
        proc.poll.return_value = 1
        with patch("wvbot.firefox.port_open", return_value=False):
            self.assertFalse(firefox.wait_for_marionette(port=2828, timeout=5.0, process=proc))

    def test_stop_calls_wait_even_after_kill(self):
        proc = MagicMock()
        proc.pid = 12345
        firefox.stop(proc)
        proc.terminate.assert_called_once()
        proc.wait.assert_called_once()


class MarionetteTest(unittest.TestCase):
    def test_connect_retries_catch_marionette_error_and_reset_socket(self):
        m = Marionette(port=2828, timeout=1.0)
        socks_created: list[MagicMock] = []

        def fake_create_connection(*args, **kwargs):
            s = MagicMock()
            socks_created.append(s)
            return s

        calls = 0

        def fake_read_packet():
            nonlocal calls
            calls += 1
            if calls == 1:
                m._buffer = b"corrupt"
                raise MarionetteError("bad handshake")
            return {"marionetteProtocol": 3}

        with (
            patch("wvbot.marionette.socket.create_connection", side_effect=fake_create_connection),
            patch.object(m, "_read_packet", side_effect=fake_read_packet),
            patch("wvbot.marionette.time.sleep"),
        ):
            m.connect(retries=3)

        self.assertEqual(calls, 2)
        socks_created[0].close.assert_called_once()
        self.assertEqual(m._buffer, b"")

    def test_read_packet_rejects_oversized_packet_and_prefix(self):
        m = Marionette()
        m._sock = MagicMock()
        oversized = f"{MAX_PACKET_BYTES + 1}:{{}}".encode("ascii")
        m._buffer = oversized
        with self.assertRaises(MarionetteError):
            m._read_packet()

        m._buffer = b"123456789012:x"
        with self.assertRaises(MarionetteError):
            m._read_packet()


class CdpTest(unittest.TestCase):
    def test_validate_port_rejects_bool(self):
        with self.assertRaises(cdp.CdpError):
            cdp._validate_port(True)
        with self.assertRaises(cdp.CdpError):
            cdp._validate_port(False)
        self.assertEqual(cdp._validate_port(9222), 9222)

    def test_launch_omits_empty_user_data_dir(self):
        chrome = cdp.Chrome(port=9222, user_data_dir="", headless=True)
        with (
            patch("wvbot.cdp.find_browser", return_value="/usr/bin/chromium"),
            patch("wvbot.cdp.subprocess.Popen") as mock_popen,
        ):
            chrome.launch()
        cmd = mock_popen.call_args[0][0]
        self.assertFalse(any(arg.startswith("--user-data-dir=") for arg in cmd))
        self.assertIn("--headless=new", cmd)

    def test_send_honors_timeout_and_wait_for_catches_websocket_error(self):
        chrome = cdp.Chrome(port=9222)
        mock_ws = MagicMock()
        mock_sock = MagicMock()
        mock_sock.gettimeout.return_value = 30.0
        mock_ws._sock = mock_sock
        mock_ws.recv_text.return_value = json.dumps({"id": 1, "result": {"ok": True}})
        chrome._ws = mock_ws

        res = chrome.send("Runtime.evaluate", {"expression": "1"}, timeout=4.5)
        self.assertEqual(res, {"ok": True})
        timeout_calls = [c.args[0] for c in mock_sock.settimeout.call_args_list]
        self.assertTrue(any(isinstance(t, (int, float)) and 0 < t <= 4.5 for t in timeout_calls))
        self.assertEqual(timeout_calls[-1], 30.0)

        with (
            patch.object(chrome, "evaluate", side_effect=WebSocketError("closed")),
            patch("wvbot.cdp.time.sleep"),
        ):
            self.assertFalse(chrome.wait_for("true", timeout=0.05))

    def test_http_raises_on_http_error_status_and_close_waits_after_kill(self):
        chrome = cdp.Chrome(port=9222)
        mock_conn = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_resp.reason = "Server Error"
        mock_resp.read.return_value = b"boom"
        mock_conn.getresponse.return_value = mock_resp

        with patch("wvbot.cdp.http.client.HTTPConnection", return_value=mock_conn):
            with self.assertRaises(cdp.CdpError):
                chrome._http("GET", "/json")

        proc = MagicMock()
        proc.wait.side_effect = [cdp.subprocess.TimeoutExpired(cmd="chrome", timeout=5), 0]
        chrome.process = proc
        chrome.close()
        proc.kill.assert_called_once()
        self.assertEqual(proc.wait.call_count, 2)


class WebBotTest(unittest.TestCase):
    def test_config_js_uses_auth_url_and_asia_host(self):
        cfg_global = Config(use_asia_host=False)
        wb_global = webbot.WebBot(cfg_global, hub=StreamHub())
        js_global = wb_global._config_js()
        self.assertIn(AUTH_URL, js_global)
        self.assertNotIn("wolesville", js_global)
        self.assertIn("wolvesville.com", js_global)

        cfg_asia = Config(use_asia_host=True)
        wb_asia = webbot.WebBot(cfg_asia)
        js_asia = wb_asia._config_js()
        self.assertIn("game-asia.api-wolvesville.com", js_asia)

    def test_payload_path_finds_web_subdir(self):
        wb = webbot.WebBot(Config())
        p = wb._payload_path()
        self.assertEqual(p.name, webbot.PAYLOAD_FILE)
        self.assertTrue(p.exists())
        self.assertFalse(hasattr(webbot.WebBot, "_solve_turnstile"))


if __name__ == "__main__":
    unittest.main()
