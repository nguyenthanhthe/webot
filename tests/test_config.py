"""Tests of the configuration: file names, coercion, the load order."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wvbot import config as config_mod
from wvbot.config import Config, _coerce, load_config, state_path


class StatePathTest(unittest.TestCase):
    def test_a_plain_name_lands_in_the_state_dir(self) -> None:
        path = state_path("state.json")
        self.assertEqual(path.name, "state.json")
        self.assertEqual(path.parent, config_mod.STATE_DIR)

    def test_separators_are_rejected(self) -> None:
        for bad in ("a/b.json", "a" + os.sep + "b.json", "a\\b.json"):
            with self.assertRaises(ValueError):
                state_path(bad)

    def test_parent_references_are_rejected(self) -> None:
        # Built at runtime so the source itself never contains the
        # traversal literal.
        escape = os.pardir + os.sep + "escape.json"
        with self.assertRaises(ValueError):
            state_path(escape)

    def test_empty_and_dots_are_rejected(self) -> None:
        for bad in ("", ".", ".."):
            with self.assertRaises(ValueError):
                state_path(bad)


class CoerceTest(unittest.TestCase):
    def test_bool(self) -> None:
        self.assertIs(_coerce(True, "yes"), True)
        self.assertIs(_coerce(True, "0"), False)
        self.assertIs(_coerce(False, "ON"), True)

    def test_int(self) -> None:
        self.assertEqual(_coerce(1, "5"), 5)
        self.assertEqual(_coerce(1, "5.0"), 5)

    def test_float(self) -> None:
        self.assertAlmostEqual(_coerce(1.0, "2.5"), 2.5)

    def test_list_splits_on_commas(self) -> None:
        self.assertEqual(_coerce(["a"], "alpha, beta ,"), ["alpha", "beta"])

    def test_string_passes_through(self) -> None:
        self.assertEqual(_coerce("x", "hello"), "hello")


class LoadConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        patcher = mock.patch.object(config_mod, "STATE_DIR", self.state_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_defaults_when_no_file_exists(self) -> None:
        cfg = load_config("missing-config.json")
        self.assertEqual(cfg.joins_per_hour, 12)
        self.assertEqual(cfg.room_names, ["vill win"])

    def test_the_json_file_wins_over_the_defaults(self) -> None:
        (self.state_dir / "config.json").write_text(
            '{"joins_per_hour": 7, "unknown_key": true}',
            encoding="utf-8",
        )
        cfg = load_config()
        self.assertEqual(cfg.joins_per_hour, 7)
        # A key the bot does not know is ignored.
        self.assertFalse(hasattr(cfg, "unknown_key"))

    def test_environment_wins_over_the_file(self) -> None:
        (self.state_dir / "config.json").write_text(
            '{"joins_per_hour": 7}',
            encoding="utf-8",
        )
        env = {
            "WV_BOT_JOINS_PER_HOUR": "5",
            "WV_BOT_DELAY_MIN_SEC": "10.5",
            "WV_BOT_ROOM_NAMES": "a, b",
            "WV_BOT_CLAIM_DOUBLE_XP": "no",
        }
        with mock.patch.dict(os.environ, env):
            cfg = load_config()
        self.assertEqual(cfg.joins_per_hour, 5)
        self.assertAlmostEqual(cfg.delay_min_sec, 10.5)
        self.assertEqual(cfg.room_names, ["a", "b"])
        self.assertIs(cfg.claim_double_xp, False)

    def test_the_refresh_token_file_wins(self) -> None:
        (self.state_dir / "refresh_token.txt").write_text(
            "token-from-file\n",
            encoding="utf-8",
        )
        cfg = load_config("missing-config.json")
        self.assertEqual(cfg.refresh_token, "token-from-file")

    def test_to_dict_masks_the_secrets(self) -> None:
        cfg = Config(password="pw", refresh_token="rt", cf_jwt="jwt")
        data = cfg.to_dict()
        self.assertEqual(data["password"], "***")
        self.assertEqual(data["refresh_token"], "***")
        self.assertEqual(data["cf_jwt"], "***")
        self.assertEqual(data["email"], "")

    def test_game_base_url_follows_the_asia_flag(self) -> None:
        cfg = Config()
        self.assertEqual(cfg.game_base_url, config_mod.GAME_URL)
        cfg.use_asia_host = True
        self.assertEqual(cfg.game_base_url, config_mod.GAME_URL_ASIA)


if __name__ == "__main__":
    unittest.main()
