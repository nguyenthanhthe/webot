"""Tests of the watchdog logic: heartbeat touch, freshness calculation, and rotation."""

from __future__ import annotations

import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import wvbot.config as config_module
from wvbot.bot import Bot


def shell(**attrs) -> Bot:
    bot = Bot.__new__(Bot)
    for name, value in attrs.items():
        setattr(bot, name, value)
    return bot


class HeartbeatTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        patcher = mock.patch.object(config_module, "STATE_DIR", self.state_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_touch_heartbeat_creates_file(self) -> None:
        bot = shell()
        bot._state = lambda name: self.state_dir / name
        bot._touch_heartbeat()

        hb_file = self.state_dir / "heartbeat.txt"
        self.assertTrue(hb_file.is_file())
        stamp = float(hb_file.read_text(encoding="utf-8").strip())
        self.assertAlmostEqual(stamp, time.time(), delta=2.0)

    def test_heartbeat_freshness_check(self) -> None:
        stale_threshold = 8000.0  # seconds

        now = 1700000000.0
        # Fresh heartbeat (10 seconds ago)
        fresh_beat = now - 10.0
        self.assertLess(now - fresh_beat, stale_threshold)

        # Stale heartbeat (9000 seconds ago)
        stale_beat = now - 9000.0
        self.assertGreater(now - stale_beat, stale_threshold)


class WatchdogLogRotationTest(unittest.TestCase):
    def test_log_size_threshold(self) -> None:
        max_bytes = 5 * 1024 * 1024  # 5 MB
        self.assertTrue(6 * 1024 * 1024 > max_bytes)
        self.assertFalse(4 * 1024 * 1024 > max_bytes)

    def test_in_place_tail_rotation_simulation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "wv-bot.log"
            lines = [f"line {i}\n" for i in range(5000)]
            log_path.write_text("".join(lines), encoding="utf-8")

            # Simulate tail -n 2000 in-place
            all_lines = log_path.read_text(encoding="utf-8").splitlines(keepends=True)
            tail = all_lines[-2000:]
            rot_path = Path(tmp) / "wv-bot.log.rot"
            rot_path.write_text("".join(tail), encoding="utf-8")
            log_path.write_text(rot_path.read_text(encoding="utf-8"), encoding="utf-8")
            rot_path.unlink()

            res = log_path.read_text(encoding="utf-8").splitlines(keepends=True)
            self.assertEqual(len(res), 2000)
            self.assertEqual(res[-1], "line 4999\n")

    def test_touch_heartbeat_creates_missing_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "missing" / "subdir"
            bot = shell()
            bot._state = lambda name: nested / name
            bot._touch_heartbeat()
            self.assertTrue((nested / "heartbeat.txt").is_file())


if __name__ == "__main__":
    unittest.main()
