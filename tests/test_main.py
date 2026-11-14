"""Tests of the entry point: mode routing, dynamic STATE_DIR, signals, RosterError."""

from __future__ import annotations

import io
import json
import logging
import os
import signal
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from wvbot import __main__ as entry
from wvbot import config as config_mod


class ThreadExcepthookTest(unittest.TestCase):
    def test_a_dead_thread_is_logged_not_raised(self) -> None:
        records = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record)
        log = logging.getLogger("wvbot.thread")
        log.addHandler(handler)
        try:
            entry._thread_excepthook(
                types.SimpleNamespace(
                    thread=types.SimpleNamespace(name="sio-reader"),
                    exc_type=RuntimeError,
                    exc_value=RuntimeError("boom"),
                    exc_traceback=None,
                )
            )
        finally:
            log.removeHandler(handler)
        self.assertTrue(any("sio-reader" in r.getMessage() for r in records))

    def test_an_anonymous_thread_is_handled(self) -> None:
        entry._thread_excepthook(
            types.SimpleNamespace(
                thread=None,
                exc_type=OSError,
                exc_value=OSError("x"),
                exc_traceback=None,
            )
        )


class MainEntrypointTest(unittest.TestCase):
    def test_env_truthy_treats_false_zero_empty_as_falsy(self) -> None:
        for val in ("", "0", "false", "FALSE", "no", "off", None):
            self.assertFalse(entry._env_truthy(val), msg=f"expected falsy for {val!r}")
        for val in ("1", "true", "YES", "on"):
            self.assertTrue(entry._env_truthy(val), msg=f"expected truthy for {val!r}")

    def test_wv_bot_farm_zero_runs_protocol_mode_and_resolves_state_dir_dynamically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bot = MagicMock()
            with (
                patch.object(config_mod, "STATE_DIR", root),
                patch.dict(os.environ, {"WV_BOT_FARM": "0"}, clear=False),
                patch("wvbot.__main__.Bot", return_value=fake_bot) as mock_bot_cls,
            ):
                rc = entry.main([])
            self.assertEqual(rc, 0)
            mock_bot_cls.assert_called_once()
            self.assertEqual(mock_bot_cls.call_args.kwargs["account_dir"], root)
            fake_bot.run.assert_called_once()

    def test_mode_protocol_overrides_accounts_json_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "accounts.json").write_text(
                json.dumps({"accounts": [{"name": "a"}]}),
                encoding="utf-8",
            )
            fake_bot = MagicMock()
            with (
                patch.object(config_mod, "STATE_DIR", root),
                patch.dict(os.environ, {"WV_BOT_FARM": "1"}, clear=False),
                patch("wvbot.__main__.Bot", return_value=fake_bot) as mock_bot_cls,
            ):
                rc = entry.main(["--mode", "protocol"])
            self.assertEqual(rc, 0)
            mock_bot_cls.assert_called_once()
            fake_bot.run.assert_called_once()

    def test_mode_farm_runs_farm_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_farm = MagicMock()
            fake_farm.stop_event.is_set.return_value = True
            with (
                patch.object(config_mod, "STATE_DIR", root),
                patch("wvbot.farm.Farm", return_value=fake_farm) as mock_farm_cls,
            ):
                rc = entry.main(["--mode", "farm"])
            self.assertEqual(rc, 0)
            mock_farm_cls.assert_called_once()
            fake_farm.start_dashboard.assert_called_once()
            fake_farm.start.assert_called_once()
            fake_farm.stop.assert_called()

    def test_roster_error_prints_to_stderr_and_returns_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "accounts.json").write_text("{broken json", encoding="utf-8")
            stderr = io.StringIO()
            with (
                patch.object(config_mod, "STATE_DIR", root),
                patch("sys.stderr", stderr),
            ):
                rc = entry.main(["--mode", "farm"])
            self.assertEqual(rc, 1)
            self.assertIn("roster", stderr.getvalue().lower())

    def test_signal_handlers_call_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_farm = MagicMock()
            fake_farm.stop_event.is_set.return_value = True
            captured: dict[int, object] = {}

            def fake_signal(sig, handler):
                captured[sig] = handler

            with (
                patch.object(config_mod, "STATE_DIR", root),
                patch("wvbot.farm.Farm", return_value=fake_farm),
                patch("wvbot.__main__.signal.signal", side_effect=fake_signal),
            ):
                entry.main(["--mode", "farm"])
            self.assertIn(signal.SIGINT, captured)
            fake_farm.stop.reset_mock()
            captured[signal.SIGINT](signal.SIGINT, None)
            fake_farm.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
