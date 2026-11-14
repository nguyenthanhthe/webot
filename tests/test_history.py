"""Tests of the XP history: counters, the day rollover, a broken file."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from wvbot.history import XpHistory, _day_key


class HistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "xp_history.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_empty_history(self) -> None:
        history = XpHistory(self.path)
        self.assertEqual(history.get()["today_xp"], 0)
        self.assertEqual(history.get()["total_games"], 0)

    def test_record_game_accumulates(self) -> None:
        history = XpHistory(self.path)
        history.record_game(15)
        history.record_game(80)
        data = history.get()
        self.assertEqual(data["today_xp"], 95)
        self.assertEqual(data["total_xp"], 95)
        self.assertEqual(data["today_games"], 2)
        self.assertEqual(data["total_games"], 2)

    def test_counters_survive_a_restart(self) -> None:
        history = XpHistory(self.path)
        history.record_game(80)
        again = XpHistory(self.path)
        self.assertEqual(again.get()["total_xp"], 80)

    def test_a_new_day_zeroes_the_daily_counters(self) -> None:
        history = XpHistory(self.path)
        history.record_game(80)
        yesterday = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["day"] = yesterday
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        again = XpHistory(self.path)
        data = again.get()
        self.assertEqual(data["day"], _day_key())
        self.assertEqual(data["today_xp"], 0)
        self.assertEqual(data["today_games"], 0)
        # The all-time counters keep growing.
        self.assertEqual(data["total_xp"], 80)

    def test_record_game_rolls_the_day_over(self) -> None:
        history = XpHistory(self.path)
        history.record_game(80)
        history.data["day"] = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))
        history.record_game(10)
        data = history.get()
        self.assertEqual(data["today_xp"], 10)
        self.assertEqual(data["total_xp"], 90)

    def test_a_broken_file_starts_empty(self) -> None:
        self.path.write_text("not json at all", encoding="utf-8")
        history = XpHistory(self.path)
        self.assertEqual(history.get()["total_xp"], 0)

    def test_a_non_object_file_starts_empty(self) -> None:
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        history = XpHistory(self.path)
        self.assertEqual(history.get()["total_xp"], 0)

    def test_record_session_counts_sessions(self) -> None:
        history = XpHistory(self.path)
        history.record_session()
        history.record_session()
        self.assertEqual(history.get()["sessions"], 2)

    def test_day_key_has_the_iso_shape(self) -> None:
        self.assertRegex(_day_key(0), r"\d{4}-\d{2}-\d{2}")
        self.assertEqual(_day_key(0), "1970-01-01")

    def test_get_rolls_day_over_at_utc_midnight(self) -> None:
        with mock.patch("wvbot.history._day_key", return_value="2026-11-13"):
            history = XpHistory(self.path)
            history.record_game(120, games=2)
            self.assertEqual(history.get()["today_xp"], 120)

        with mock.patch("wvbot.history._day_key", return_value="2026-11-14"):
            data = history.get()
        self.assertEqual(data["day"], "2026-11-14")
        self.assertEqual(data["today_xp"], 0)
        self.assertEqual(data["today_games"], 0)
        self.assertEqual(data["total_xp"], 120)
        self.assertEqual(data["total_games"], 2)
        persisted = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["day"], "2026-11-14")

    def test_concurrent_record_game_keeps_valid_json_and_exact_totals(self) -> None:
        history = XpHistory(self.path)
        errors: list[Exception] = []

        def worker() -> None:
            try:
                for _ in range(10):
                    history.record_game(5)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        data = history.get()
        self.assertEqual(data["total_xp"], 200)
        self.assertEqual(data["total_games"], 40)
        persisted = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["total_xp"], 200)


if __name__ == "__main__":
    unittest.main()
