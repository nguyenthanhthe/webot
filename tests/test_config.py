"""Tests of the configuration: file names, coercion, the load order."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wvbot import config as config_mod
from wvbot.config import (
    CONFIG_FIELDS,
    Config,
    _coerce,
    apply_overrides,
    get_state_dir,
    load_config,
    state_path,
)


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

    def test_state_path_with_custom_base(self) -> None:
        custom_base = Path("/custom/state/dir")
        path = state_path("state.json", base=custom_base)
        self.assertEqual(path, custom_base / "state.json")

    def test_state_dir_env_and_monkeypatch_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_env:
            env_dir = Path(tmp_env) / "env-state"
            with mock.patch.dict(os.environ, {"WV_BOT_STATE_DIR": str(env_dir)}, clear=False):
                self.assertEqual(get_state_dir(), env_dir)
                self.assertEqual(state_path("state.json"), env_dir / "state.json")

                override_dir = Path(tmp_env) / "monkey-state"
                with mock.patch.object(config_mod, "STATE_DIR", override_dir):
                    self.assertEqual(get_state_dir(), override_dir)
                    self.assertEqual(state_path("state.json"), override_dir / "state.json")

        with tempfile.TemporaryDirectory() as tmp_env:
            state_dir_env = Path(tmp_env) / "docker-data"
            clean_env = {k: v for k, v in os.environ.items() if k != "WV_BOT_STATE_DIR"}
            clean_env["STATE_DIR"] = str(state_dir_env)
            with mock.patch.dict(os.environ, clean_env, clear=True):
                self.assertEqual(get_state_dir(), state_dir_env)


class CoerceTest(unittest.TestCase):
    def test_bool(self) -> None:
        self.assertIs(_coerce(True, "yes"), True)
        self.assertIs(_coerce(True, "0"), False)
        self.assertIs(_coerce(False, "ON"), True)

    def test_int(self) -> None:
        self.assertEqual(_coerce(1, "5"), 5)
        self.assertEqual(_coerce(1, "5.0"), 5)
        self.assertEqual(_coerce(3, ""), 3)

    def test_optional_int(self) -> None:
        self.assertIsNone(_coerce(4, "", int | None))
        self.assertIsNone(_coerce(4, "none", int | None))
        self.assertIsNone(_coerce(4, "NULL", int | None))
        self.assertEqual(_coerce(None, "6", int | None), 6)

    def test_float(self) -> None:
        self.assertAlmostEqual(_coerce(1.0, "2.5"), 2.5)
        self.assertAlmostEqual(_coerce(1.5, ""), 1.5)

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
        self.assertTrue(cfg.strict_8p_only)
        self.assertEqual(cfg.max_room_size, 8)
        self.assertEqual(cfg.dashboard_host, "127.0.0.1")
        self.assertEqual(cfg.dashboard_token, "")
        self.assertTrue(cfg.fast_reconnect_enabled)
        self.assertEqual(cfg.fast_reconnect_max_attempts, 3)
        self.assertEqual(cfg.fast_reconnect_delay_sec, 3.0)
        self.assertEqual(cfg.socket_join_delay_sec, 1.0)

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
        cfg = Config(password="pw", refresh_token="rt", cf_jwt="jwt", dashboard_token="dash")
        data = cfg.to_dict()
        self.assertEqual(data["password"], "***")
        self.assertEqual(data["refresh_token"], "***")
        self.assertEqual(data["cf_jwt"], "***")
        self.assertEqual(data["dashboard_token"], "***")
        self.assertEqual(data["email"], "")

    def test_apply_overrides_ignores_methods_and_properties(self) -> None:
        cfg = Config()
        apply_overrides(
            cfg,
            {
                "joins_per_hour": 3,
                "to_dict": "pwned",
                "game_base_url": "https://evil.invalid",
                "unknown": 123,
            },
        )
        self.assertEqual(cfg.joins_per_hour, 3)
        self.assertTrue(callable(cfg.to_dict))
        self.assertEqual(cfg.game_base_url, config_mod.GAME_URL)
        self.assertNotIn("to_dict", CONFIG_FIELDS)
        self.assertNotIn("game_base_url", CONFIG_FIELDS)

    def test_optional_int_and_dashboard_token_env_overrides(self) -> None:
        (self.state_dir / "config.json").write_text(
            '{"daily_restart_hour": null, "to_dict": "bad"}',
            encoding="utf-8",
        )
        with mock.patch.dict(
            os.environ,
            {"WV_BOT_DAILY_RESTART_HOUR": "7", "WV_DASHBOARD_TOKEN": "fallback-tok"},
            clear=False,
        ):
            cfg = load_config()
        self.assertEqual(cfg.daily_restart_hour, 7)
        self.assertEqual(cfg.dashboard_token, "fallback-tok")
        self.assertTrue(callable(cfg.to_dict))

        with mock.patch.dict(
            os.environ,
            {
                "WV_BOT_DAILY_RESTART_HOUR": "none",
                "WV_DASHBOARD_TOKEN": "fallback-tok",
                "WV_BOT_DASHBOARD_TOKEN": "primary-tok",
            },
            clear=False,
        ):
            cfg2 = load_config()
        self.assertIsNone(cfg2.daily_restart_hour)
        self.assertEqual(cfg2.dashboard_token, "primary-tok")

    def test_game_base_url_follows_the_asia_flag(self) -> None:
        cfg = Config()
        self.assertEqual(cfg.game_base_url, config_mod.GAME_URL)
        cfg.use_asia_host = True
        self.assertEqual(cfg.game_base_url, config_mod.GAME_URL_ASIA)

    def test_load_config_with_custom_base(self) -> None:
        import json
        acc_dir = self.state_dir / "accounts" / "bot1"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "config.json").write_text(json.dumps({"language": "vi"}), encoding="utf-8")
        (acc_dir / "refresh_token.txt").write_text("custom-token\n", encoding="utf-8")
        cfg = load_config(base=acc_dir)
        self.assertEqual(cfg.language, "vi")
        self.assertEqual(cfg.refresh_token, "custom-token")



if __name__ == "__main__":
    unittest.main()
