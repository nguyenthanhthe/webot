"""Tests of the bot helpers: ban timestamps, pacing, the phase guard.

The helpers are tested on a bare ``Bot`` shell - no sockets, no HTTP, no
state directory.  Only the attributes a helper touches are attached.
"""

from __future__ import annotations

import tempfile
import threading
import time
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import wvbot.bot as bot_module
import wvbot.config as config_module
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


class RoomSkipTest(unittest.TestCase):
    def _bot(self, chance: float) -> Bot:
        return shell(
            cfg=types.SimpleNamespace(room_skip_chance=chance),
            store=StatusStore(),
        )

    def test_a_chance_of_zero_never_skips(self) -> None:
        bot = self._bot(0.0)
        for _ in range(50):
            self.assertFalse(bot._skip_room())

    def test_a_chance_of_one_always_skips(self) -> None:
        bot = self._bot(1.0)
        for _ in range(50):
            self.assertTrue(bot._skip_room())

    def test_the_default_chance_stays_rare(self) -> None:
        from wvbot.config import Config

        self.assertLess(Config().room_skip_chance, 0.5)


class PickRoomTest(unittest.TestCase):
    def _bot(self) -> Bot:
        return shell(store=StatusStore())

    def test_empty_list_gives_none(self) -> None:
        self.assertIsNone(self._bot()._pick_room([]))

    def test_a_single_room_is_taken(self) -> None:
        room = {"gameId": 1}
        self.assertEqual(self._bot()._pick_room([room]), room)

    def test_low_xp_rooms_are_left_alone(self) -> None:
        good = {"gameId": 1, "regularXp": 20}
        other_good = {"gameId": 2, "regularXp": 18}
        poor = {"gameId": 3, "regularXp": 5}
        pick = self._bot()._pick_room
        for _ in range(40):
            self.assertIn(pick([good, other_good, poor]), (good, other_good))

    def test_without_xp_data_the_choice_is_free(self) -> None:
        a = {"gameId": 1}
        b = {"gameId": 2}
        self.assertIn(self._bot()._pick_room([a, b]), (a, b))

    def test_a_broken_xp_value_counts_as_zero(self) -> None:
        a = {"gameId": 1, "regularXp": "many"}
        b = {"gameId": 2}
        self.assertIn(self._bot()._pick_room([a, b]), (a, b))


class NightlyRestartTest(unittest.TestCase):
    def _bot(self, cfg_hour, restart_day="") -> Bot:
        return shell(
            cfg=types.SimpleNamespace(daily_restart_hour=cfg_hour),
            _last_restart_day=restart_day,
            store=StatusStore(),
        )

    def test_due_at_the_configured_hour(self) -> None:
        bot = self._bot(4)
        with mock.patch.object(bot_module.time, "localtime") as lt:
            lt.return_value = types.SimpleNamespace(tm_hour=4)
            with mock.patch.object(bot_module.time, "strftime", return_value="2026-11-05"):
                self.assertTrue(bot._nightly_restart_due())

    def test_not_due_at_another_hour(self) -> None:
        bot = self._bot(4)
        with mock.patch.object(bot_module.time, "localtime") as lt:
            lt.return_value = types.SimpleNamespace(tm_hour=17)
            self.assertFalse(bot._nightly_restart_due())

    def test_disabled_when_none(self) -> None:
        self.assertFalse(self._bot(None)._nightly_restart_due())

    def test_only_once_per_day(self) -> None:
        bot = self._bot(4, restart_day="2026-11-05")
        with mock.patch.object(bot_module.time, "localtime") as lt:
            lt.return_value = types.SimpleNamespace(tm_hour=4)
            with mock.patch.object(bot_module.time, "strftime", return_value="2026-11-05"):
                self.assertFalse(bot._nightly_restart_due())

    def test_the_marker_survives_an_exec(self) -> None:
        # os.execv wipes the memory: the day has to live in the state
        # dir, or the bot restarts in a tight loop (seen live at 4 AM).
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config_module, "STATE_DIR", Path(tmp)):
                bot = Bot.__new__(Bot)
                bot._last_restart_day = bot._load_restart_day()
                self.assertEqual(bot._last_restart_day, "")
                bot._save_restart_day("2026-11-05")
                again = Bot.__new__(Bot)
                again._last_restart_day = again._load_restart_day()
                self.assertEqual(again._last_restart_day, "2026-11-05")


class MaybeChatTest(unittest.TestCase):
    def _bot(self, enabled: bool) -> Bot:
        return shell(
            cfg=types.SimpleNamespace(
                chat_enabled=enabled,
                chat_messages=["hi"],
            ),
            store=StatusStore(),
            stop_event=threading.Event(),
        )

    def test_off_by_default_nothing_is_sent(self) -> None:
        from wvbot.config import Config

        self.assertFalse(Config().chat_enabled)
        bot = self._bot(False)
        self.assertIsNone(bot._maybe_chat(None))  # type: ignore[arg-type]

    def test_with_messages_it_sends_sometimes(self) -> None:
        bot = self._bot(True)
        # The decision branch itself: a bot with messages and the flag
        # on can decide to speak; the roll decides.  The speak runs in
        # the queue worker, so the test waits for it.
        sent = []
        with mock.patch.object(bot_module.random.SystemRandom, "random", return_value=0.0):
            with mock.patch.object(bot, "chat", lambda sock, msg: sent.append(msg)):
                with mock.patch.object(bot, "_hesitate", lambda *a, **k: None):
                    bot._maybe_chat(None)  # type: ignore[arg-type]
                    for _ in range(40):
                        if sent:
                            break
                        time.sleep(0.05)
        self.assertEqual(sent, ["hi"])

    def test_a_bad_roll_stays_silent(self) -> None:
        bot = self._bot(True)
        sent = []
        with mock.patch.object(bot_module.random.SystemRandom, "random", return_value=0.9):
            with mock.patch.object(bot, "chat", lambda sock, msg: sent.append(msg)):
                bot._maybe_chat(None)  # type: ignore[arg-type]
                time.sleep(0.3)
        self.assertEqual(sent, [])


class DailyClaimMarkerTest(unittest.TestCase):
    def _bot(self) -> Bot:
        return shell(store=StatusStore())

    def test_the_marker_roundtrips_through_the_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config_module, "STATE_DIR", Path(tmp)):
                bot = self._bot()
                self.assertFalse(bot._daily_claim_attempted("TALISMAN"))
                bot._note_daily_claim_attempt("TALISMAN")
                again = self._bot()
                self.assertTrue(again._daily_claim_attempted("TALISMAN"))
                # A different rung on the same day is a different mark.
                self.assertFalse(again._daily_claim_attempted("GOLD"))

    def test_a_claimed_rung_is_not_posted_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config_module, "STATE_DIR", Path(tmp)):
                bot = self._bot()
                calls = []

                class FakeRewards:
                    def daily_rewards(self):
                        return [{"type": "TALISMAN", "amount": 1}]

                    def claim_daily_reward(self):
                        calls.append(True)
                        return {}

                bot._claim_daily(FakeRewards())
                bot._claim_daily(FakeRewards())
                self.assertEqual(len(calls), 1)


class RoomProbeTest(unittest.TestCase):
    def test_probe_uses_the_asia_host_when_asked(self) -> None:
        urls = []

        class FakeHttp:
            def request(self, method, url, headers=None, **kwargs):
                urls.append(url)
                return {"openGames": []}

        bot = shell(
            cfg=types.SimpleNamespace(game_base_url=config_module.GAME_URL),
            http=FakeHttp(),
            auth=types.SimpleNamespace(auth_headers=lambda: {}),
        )
        bot._room_probe("en")
        bot._room_probe("en", "asia")
        self.assertTrue(urls[0].startswith(config_module.GAME_URL))
        self.assertTrue(urls[1].startswith(config_module.GAME_URL_ASIA))
        self.assertTrue(urls[0].endswith("language=en"))

    def test_a_dirty_language_is_sanitised(self) -> None:
        urls = []

        class FakeHttp:
            def request(self, method, url, headers=None, **kwargs):
                urls.append(url)
                return {"openGames": []}

        bot = shell(
            cfg=types.SimpleNamespace(game_base_url=config_module.GAME_URL),
            http=FakeHttp(),
            auth=types.SimpleNamespace(auth_headers=lambda: {}),
        )
        bot._room_probe("en; DROP TABLE players")
        self.assertTrue(urls[0].endswith("language=enDROPTA"))


class QueuedActionTest(unittest.TestCase):
    """The human pause must run off the reader thread: a blocked
    reader stops answering the engine pings and the server drops the
    socket."""

    def _bot(self, hesitate) -> Bot:
        from wvbot.game import GameState, Policy

        state = GameState()
        state.apply("game-started", {"players": [{"id": 1}, {"id": 2}]})
        return shell(
            cfg=types.SimpleNamespace(),
            state=state,
            policy=Policy(),
            store=StatusStore(),
            _acted_keys=set(),
            stop_event=threading.Event(),
            _hesitate=hesitate,
        )

    def test_the_day_vote_returns_at_once_and_emits_later(self) -> None:
        emitted = []
        done = threading.Event()

        class FakeSock:
            def emit(self, *args):
                emitted.append(args)
                done.set()

        def instant(low, high):
            done.set()

        bot = self._bot(instant)
        started = time.monotonic()
        bot._day_vote(FakeSock())
        elapsed = time.monotonic() - started
        # The handler itself must not wait out the pause.
        self.assertLess(elapsed, 0.5)
        self.assertTrue(done.wait(2.0), "the queued action never ran")
        self.assertIn(("game-vote-to-skip",), emitted)

    def test_a_stop_cancels_the_queued_action(self) -> None:
        emitted = []

        class FakeSock:
            def emit(self, *args):
                emitted.append(args)

        bot = self._bot(lambda low, high: None)
        bot.stop_event.set()
        bot._day_vote(FakeSock())
        time.sleep(0.3)
        self.assertEqual(emitted, [])

    def test_a_failing_emit_is_logged_not_raised(self) -> None:
        class FakeSock:
            def emit(self, *args):
                raise OSError("socket gone")

        bot = self._bot(lambda low, high: None)
        bot._day_vote(FakeSock())  # must not raise out of the worker
        time.sleep(0.3)
        kinds = [e["kind"] for e in bot.store.events()]
        self.assertIn("warn", kinds)


class HeartbeatTest(unittest.TestCase):
    def test_the_heartbeat_is_written_to_the_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config_module, "STATE_DIR", Path(tmp)):
                bot = shell(store=StatusStore())
                bot._touch_heartbeat()
                value = (Path(tmp) / "heartbeat.txt").read_text(encoding="utf-8")
                self.assertGreater(float(value), 0.0)


class ReexecGuardTest(unittest.TestCase):
    def test_a_failing_exec_does_not_kill_the_bot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config_module, "STATE_DIR", Path(tmp)):
                bot = shell(store=StatusStore(), log_event=lambda *a, **k: None)
                with mock.patch.object(
                    bot_module.os, "execv", side_effect=OSError("no interpreter")
                ):
                    # Before the fix this raised out of the run loop and
                    # took the whole farm down.
                    bot._reexec()
                self.assertEqual(bot.store.get().get("status"), "running")


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


class DailyRewardTest(unittest.TestCase):
    """The daily ladder holds the whole month, so only a reward whose
    date has arrived may be picked up."""

    def setUp(self) -> None:
        # The claim writes its once-a-day marker into the state dir;
        # every test here runs against a throwaway one.
        self._tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(config_module, "STATE_DIR", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_a_reward_of_a_future_day_is_not_due(self) -> None:
        now = datetime(2026, 11, 5, tzinfo=timezone.utc)
        future = {"type": "GOLD", "amount": 25, "canBeClaimedDate": "2026-11-20T00:00:00.000Z"}
        self.assertFalse(Bot._reward_is_due(future, now))

    def test_a_reward_of_today_is_due(self) -> None:
        now = datetime(2026, 11, 5, tzinfo=timezone.utc)
        today = {"type": "GOLD", "amount": 10, "canBeClaimedDate": "2026-11-05T00:00:00.000Z"}
        self.assertTrue(Bot._reward_is_due(today, now))

    def test_a_reward_without_a_date_is_due(self) -> None:
        now = datetime(2026, 11, 5, tzinfo=timezone.utc)
        self.assertTrue(Bot._reward_is_due({"type": "GOLD", "amount": 5}, now))

    def test_a_broken_date_is_due(self) -> None:
        now = datetime(2026, 11, 5, tzinfo=timezone.utc)
        self.assertTrue(Bot._reward_is_due({"canBeClaimedDate": "not a date"}, now))

    def test_a_reward_in_the_past_is_due(self) -> None:
        now = datetime(2026, 11, 5, tzinfo=timezone.utc)
        past = {"canBeClaimedDate": "2026-11-01T00:00:00.000Z"}
        self.assertTrue(Bot._reward_is_due(past, now))

    def test_only_a_due_reward_is_claimed(self) -> None:
        bot = shell(store=StatusStore())
        calls = []

        class FakeRewards:
            def daily_rewards(self):
                return [
                    {"type": "GOLD", "amount": 10, "claimed": True},
                    {
                        "type": "GOLD",
                        "amount": 25,
                        "claimed": False,
                        "canBeClaimedDate": "2026-12-01T00:00:00.000Z",
                    },
                ]

            def claim_daily_reward(self):
                calls.append(True)
                return {"ok": True}

        bot._claim_daily(FakeRewards())
        # The only open reward is of a future day, so nothing is claimed.
        self.assertEqual(calls, [])

    def test_a_due_reward_is_claimed(self) -> None:
        bot = shell(store=StatusStore())
        calls = []

        class FakeRewards:
            def daily_rewards(self):
                return [
                    {
                        "type": "GOLD",
                        "amount": 25,
                        "claimed": False,
                        "canBeClaimedDate": "2020-01-01T00:00:00.000Z",
                    }
                ]

            def claim_daily_reward(self):
                calls.append(True)
                return {"ok": True}

        bot._claim_daily(FakeRewards())
        self.assertEqual(len(calls), 1)


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
