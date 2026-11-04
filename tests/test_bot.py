"""Tests of the bot helpers: ban timestamps, pacing, the phase guard.

The helpers are tested on a bare ``Bot`` shell - no sockets, no HTTP, no
state directory.  Only the attributes a helper touches are attached.
"""

from __future__ import annotations

import time
import types
import unittest
from datetime import datetime, timezone

from wvbot.bot import KILLER_ROLES, PROTECTOR_ROLES, Bot, _extract_game_info, _parse_ban_until
from wvbot.dashboard import StatusStore


def shell(**attrs) -> Bot:
    """A Bot without __init__: just the attributes a helper needs."""
    bot = Bot.__new__(Bot)
    for name, value in attrs.items():
        setattr(bot, name, value)
    return bot


class ParseBanUntilTest(unittest.TestCase):
    def test_iso_utc_zulu(self) -> None:
        expected = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(_parse_ban_until("2026-11-04T12:00:00Z"), expected)

    def test_iso_with_explicit_offset(self) -> None:
        expected = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(_parse_ban_until("2026-11-04T12:00:00+00:00"), expected)

    def test_empty_is_no_ban(self) -> None:
        self.assertEqual(_parse_ban_until(""), 0.0)
        self.assertEqual(_parse_ban_until(None), 0.0)

    def test_garbage_is_no_ban(self) -> None:
        self.assertEqual(_parse_ban_until("yesterday maybe"), 0.0)


class ExtractGameInfoTest(unittest.TestCase):
    def test_named_fields(self) -> None:
        info = _extract_game_info({"gameId": "g1", "serverUrl": "https://s.example"})
        self.assertEqual(info["gameId"], "g1")
        self.assertEqual(info["serverUrl"], "https://s.example")

    def test_id_alias(self) -> None:
        self.assertEqual(_extract_game_info({"id": 7})["gameId"], 7)

    def test_empty_dict_gives_empty_info(self) -> None:
        self.assertEqual(_extract_game_info({}), {})

    def test_non_dict_gives_empty_info(self) -> None:
        self.assertEqual(_extract_game_info(["nope"]), {})
        self.assertEqual(_extract_game_info(None), {})


class RoleTablesTest(unittest.TestCase):
    def test_killer_events_are_unique(self) -> None:
        events = list(KILLER_ROLES.values())
        self.assertEqual(len(events), len(set(events)))

    def test_protector_events_are_unique(self) -> None:
        events = list(PROTECTOR_ROLES.values())
        self.assertEqual(len(events), len(set(events)))

    def test_no_role_is_both_killer_and_protector(self) -> None:
        overlap = set(KILLER_ROLES) & set(PROTECTOR_ROLES)
        self.assertEqual(overlap, set())


class PhaseGuardTest(unittest.TestCase):
    """The bot must act once per phase, the server repeats every event."""

    def test_an_action_runs_once(self) -> None:
        bot = shell(_acted_keys=set())
        self.assertFalse(bot._acted("day-vote"))
        self.assertTrue(bot._acted("day-vote"))
        self.assertTrue(bot._acted("day-vote"))

    def test_actions_are_independent(self) -> None:
        bot = shell(_acted_keys=set())
        self.assertFalse(bot._acted("day-vote"))
        self.assertFalse(bot._acted("werewolf-vote"))

    def test_a_new_phase_clears_the_guard(self) -> None:
        bot = shell(_acted_keys=set())
        self.assertFalse(bot._acted("day-vote"))
        self.assertTrue(bot._acted("day-vote"))
        bot._clear_actions()
        self.assertFalse(bot._acted("day-vote"))

    def test_a_failing_target_releases_the_guard(self) -> None:
        # The killer action puts its key back when there was no target,
        # so the next vote event can still trigger the kill.
        bot = shell(_acted_keys={"game-priest-kill-player"})
        bot._acted_keys.discard("game-priest-kill-player")
        self.assertFalse(bot._acted("game-priest-kill-player"))


class PacingTest(unittest.TestCase):
    CFG = types.SimpleNamespace(
        delay_min_sec=45.0,
        delay_max_sec=150.0,
        joins_per_hour=12,
    )

    def test_delay_stays_inside_the_range(self) -> None:
        bot = shell(cfg=self.CFG, _join_times=[])
        bot.log_event = lambda *a, **k: None
        for _ in range(50):
            delay = bot._next_delay()
            self.assertGreaterEqual(delay, 45.0)
            self.assertLessEqual(delay, 150.0)

    def test_over_the_join_limit_waits_longer(self) -> None:
        now = time.time()
        bot = shell(
            cfg=self.CFG,
            _join_times=[now - 60 * m for m in (1, 3, 5, 8, 11, 14, 20, 30, 40, 50, 55, 58)],
        )
        bot.log_event = lambda *a, **k: None
        delay = bot._next_delay()
        self.assertGreaterEqual(delay, 345.0)
        self.assertLessEqual(delay, 450.0)

    def test_old_joins_fall_out_of_the_window(self) -> None:
        now = time.time()
        bot = shell(_join_times=[now - 7200, now - 3700, now - 600, now - 10])
        self.assertEqual(bot._joins_in_hour(), 2)
        # The stale stamps are dropped from the list as a side effect.
        self.assertEqual(len(bot._join_times), 2)

    def test_note_join_appends(self) -> None:
        bot = shell(_join_times=[])
        bot._note_join()
        bot._note_join()
        self.assertEqual(bot._joins_in_hour(), 2)


class ProjectionTest(unittest.TestCase):
    def test_rate_and_hours_to_next_level(self) -> None:
        bot = shell(store=StatusStore())
        bot.store.update(
            xp_total=100,
            level=3,
            xp_until_next=500,
            started_at=time.time() - 3600,
        )
        projection = bot._projection()
        self.assertEqual(projection["xp_per_hour"], 100)
        self.assertEqual(projection["hours_to_next_level"], 5.0)

    def test_zero_rate_gives_no_projection(self) -> None:
        bot = shell(store=StatusStore())
        bot.store.update(xp_total=0, level=2, xp_until_next=350)
        projection = bot._projection()
        self.assertEqual(projection["xp_per_hour"], 0)
        self.assertIsNone(projection["hours_to_next_level"])

    def test_missing_until_next_falls_back_to_the_table(self) -> None:
        bot = shell(store=StatusStore())
        bot.store.update(xp_total=200, level=None, xp_until_next=None)
        bot.store.update(started_at=time.time() - 3600)
        projection = bot._projection()
        # Level falls back to 1, whose requirement is 200 xp.
        self.assertEqual(projection["hours_to_next_level"], 1.0)


class PlayerLevelsTest(unittest.TestCase):
    def test_rows_carry_name_level_alive_own(self) -> None:
        from wvbot.game import GameState

        state = GameState()
        state.apply(
            "game-started",
            {
                "players": [
                    {"id": 1, "username": "me", "level": 2},
                    {"id": 2, "username": "bob", "level": 20},
                ]
            },
        )
        state.set_own_id(1)
        state.apply("game-players-killed", [2])
        bot = shell(state=state)
        rows = bot._player_levels()
        self.assertEqual(rows[0], {"name": "bob", "level": 20, "alive": False, "own": False})
        self.assertEqual(rows[1], {"name": "me", "level": 2, "alive": True, "own": True})


if __name__ == "__main__":
    unittest.main()
