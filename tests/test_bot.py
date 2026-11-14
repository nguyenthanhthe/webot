"""Tests of the bot helpers: ban timestamps, pacing, the phase guard.

The helpers are tested on a bare ``Bot`` shell - no sockets, no HTTP, no
state directory.  Only the attributes a helper touches are attached.
"""

from __future__ import annotations

import os
import sys
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
from wvbot.bot import (
    INVESTIGATOR_ROLES,
    KILLER_ROLES,
    PROTECTOR_ROLES,
    REVIVER_ROLES,
    Bot,
    _parse_ban_until,
)
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

    def test_ban_sleep_does_not_clear_while_in_future(self) -> None:
        future = time.time() + 100000
        bot = shell(store=StatusStore(), stop_event=threading.Event())
        bot._banned_until = future
        self.assertTrue(time.time() < bot._banned_until)


class RoleTablesTest(unittest.TestCase):
    def test_killer_events_are_unique(self) -> None:
        events = list(KILLER_ROLES.values())
        self.assertEqual(len(events), len(set(events)))

    def test_protector_events_are_unique(self) -> None:
        events = list(PROTECTOR_ROLES.values())
        self.assertEqual(len(events), len(set(events)))

    def test_reviver_events_are_unique(self) -> None:
        events = list(REVIVER_ROLES.values())
        self.assertEqual(len(events), len(set(events)))

    def test_investigator_events_are_unique(self) -> None:
        events = list(INVESTIGATOR_ROLES.values())
        self.assertEqual(len(events), len(set(events)))

    def test_no_role_cross_contamination(self) -> None:
        self.assertEqual(set(KILLER_ROLES) & set(PROTECTOR_ROLES), set())
        self.assertEqual(set(KILLER_ROLES) & set(REVIVER_ROLES), set())
        self.assertEqual(set(PROTECTOR_ROLES) & set(REVIVER_ROLES), set())


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
    def _bot(self, prefer_small: bool = True) -> Bot:
        return shell(store=StatusStore(), cfg=types.SimpleNamespace(prefer_small_rooms=prefer_small))

    def test_empty_list_gives_none(self) -> None:
        self.assertIsNone(self._bot()._pick_room([]))

    def test_a_single_room_is_taken(self) -> None:
        room = {"gameId": 1}
        self.assertEqual(self._bot()._pick_room([room]), room)

    def test_rooms_without_the_xp_flag_are_left_alone(self) -> None:
        good = {"gameId": 1, "regularXp": True}
        poor = {"gameId": 2, "regularXp": False}
        pick = self._bot()._pick_room
        for _ in range(40):
            self.assertEqual(pick([good, poor]), good)

    def test_small_rooms_are_preferred(self) -> None:
        # The fast-rotating eight-seat rooms pay more per hour than a
        # full sixteen.
        small = {"gameId": 1, "regularXp": True, "playerCount": 8}
        small2 = {"gameId": 2, "regularXp": True, "playerCount": 9}
        full = {"gameId": 3, "regularXp": True, "playerCount": 16}
        pick = self._bot()._pick_room
        for _ in range(40):
            self.assertIn(pick([small, small2, full]), (small, small2))

    def test_the_small_preference_can_be_switched_off(self) -> None:
        small = {"gameId": 1, "regularXp": True, "playerCount": 8}
        full = {"gameId": 2, "regularXp": True, "playerCount": 16}
        pick = self._bot(prefer_small=False)._pick_room
        seen = {pick([small, full])["gameId"] for _ in range(60)}
        self.assertEqual(seen, {1, 2})

    def test_nearly_empty_rooms_are_not_called_small(self) -> None:
        # A room of two is still filling up, not fast.
        filling = {"gameId": 1, "regularXp": True, "playerCount": 2}
        medium = {"gameId": 2, "regularXp": True, "playerCount": 9}
        pick = self._bot()._pick_room
        for _ in range(40):
            self.assertIn(pick([filling, medium]), (filling, medium))

    def test_without_xp_data_the_choice_is_free(self) -> None:
        a = {"gameId": 1}
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


class ChatMethodTest(unittest.TestCase):
    def test_chat_emits_json_string(self) -> None:
        import json

        bot = shell(cfg=types.SimpleNamespace(chat_enabled=True))
        emitted = []
        sock = types.SimpleNamespace(emit=lambda evt, payload: emitted.append((evt, payload)))
        bot.chat(sock, "hello room")
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0][0], "game:chat-public:msg")
        self.assertIsInstance(emitted[0][1], str)
        self.assertEqual(json.loads(emitted[0][1]), {"msg": "hello room"})

    def test_chat_disabled_does_not_emit(self) -> None:
        bot = shell(cfg=types.SimpleNamespace(chat_enabled=False))
        emitted = []
        sock = types.SimpleNamespace(emit=lambda evt, payload: emitted.append((evt, payload)))
        bot.chat(sock, "hello")
        self.assertEqual(emitted, [])

    def test_empty_message_does_not_emit(self) -> None:
        bot = shell(cfg=types.SimpleNamespace(chat_enabled=True))
        emitted = []
        sock = types.SimpleNamespace(emit=lambda evt, payload: emitted.append((evt, payload)))
        bot.chat(sock, "")
        self.assertEqual(emitted, [])


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
        state.apply("game-day-voting-started", {})
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


class JoinBudgetTest(unittest.TestCase):
    def _bot(self, joins: int, per_hour: int) -> Bot:
        now = time.time()
        return shell(
            cfg=types.SimpleNamespace(joins_per_hour=per_hour),
            _join_times=[now - 60 * m for m in range(1, joins + 1)],
            store=StatusStore(),
        )

    def test_budget_is_open_below_the_cap(self) -> None:
        bot = self._bot(joins=5, per_hour=12)
        bot.log_event = lambda *a, **k: None
        self.assertTrue(bot._join_budget_left())

    def test_budget_is_closed_at_the_cap(self) -> None:
        bot = self._bot(joins=12, per_hour=12)
        self.assertFalse(bot._join_budget_left())

    def test_old_joins_do_not_close_the_budget(self) -> None:
        now = time.time()
        bot = shell(
            cfg=types.SimpleNamespace(joins_per_hour=12),
            _join_times=[now - 7200] * 20,
            store=StatusStore(),
        )
        self.assertTrue(bot._join_budget_left())


class AliveCountTest(unittest.TestCase):
    def test_the_alive_count_shrinks_with_the_kills(self) -> None:
        from wvbot.game import GameState

        state = GameState()
        state.apply(
            "game-started",
            {"players": [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]},
        )
        bot = shell(state=state, store=StatusStore())
        bot._update_alive()
        self.assertEqual(bot.store.get().get("alive"), 4)
        state.apply("game-players-killed", [2, 3])
        bot._update_alive()
        self.assertEqual(bot.store.get().get("alive"), 2)


class ProjectionTest(unittest.TestCase):
    def test_the_rate_comes_from_the_day_history(self) -> None:
        bot = shell(store=StatusStore())
        # Five hours of the UTC day are gone, 100 xp farmed.
        now = time.time()
        bot.store.update(history={"today_xp": 100, "today_games": 3})
        with mock.patch.object(bot_module.time, "time", return_value=(5 * 3600)):
            projection = bot._projection()
        self.assertEqual(projection["xp_per_hour"], 20)

    def test_an_empty_day_falls_back_to_the_session(self) -> None:
        bot = shell(store=StatusStore())
        bot.store.update(xp_total=100, history={"today_xp": 0})
        bot.store.update(started_at=time.time() - 3600)
        projection = bot._projection()
        self.assertEqual(projection["xp_per_hour"], 100)
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


class FakeGameSocket:
    def __init__(self, start_fails: bool = False):
        self.handlers: dict[str, list] = {}
        self.any_handlers: list = []
        self.emitted: list = []
        self.closed: bool = False
        self.connected: bool = True
        self.start_fails: bool = start_fails

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def on_any(self, handler):
        self.any_handlers.append(handler)

    def start(self):
        if self.start_fails:
            raise RuntimeError("network down")
        self.connected = True

    def emit(self, event, *args):
        self.emitted.append((event, args))

    def close(self):
        self.closed = True
        self.connected = False

    def simulate_event(self, event, *args):
        for h in list(self.any_handlers):
            h(event, *args)
        for h in list(self.handlers.get(event, [])):
            h(*args)


class FastReconnectTest(unittest.TestCase):
    def _create_bot(self, **cfg_overrides) -> tuple[Bot, list[FakeGameSocket]]:
        cfg_vals = {
            "fast_reconnect_enabled": True,
            "fast_reconnect_max_attempts": 2,
            "fast_reconnect_delay_sec": 0.01,
            "socket_join_delay_sec": 0.01,
            "silence_timeout_sec": 300.0,
            "claim_double_xp": False,
        }
        cfg_vals.update(cfg_overrides)
        cfg = types.SimpleNamespace(**cfg_vals)
        from wvbot.game import GameState

        bot = shell(
            cfg=cfg,
            store=StatusStore(),
            stop_event=threading.Event(),
            _restart=threading.Event(),
            _banned=threading.Event(),
            _session_over=threading.Event(),
            state=GameState(),
            auth=types.SimpleNamespace(ensure_token=lambda: "tok123"),
            device_id="dev-test",
            history=types.SimpleNamespace(record_game=lambda xp: None, get=lambda: {}),
        )
        bot.log_event = lambda *a, **k: None
        bot._read_profile = lambda: None

        sockets: list[FakeGameSocket] = []

        def create_sock(token, url):
            s = FakeGameSocket()
            sockets.append(s)
            return s

        bot._create_game_socket = create_sock
        return bot, sockets

    def test_unexpected_disconnect_triggers_fast_reconnect(self) -> None:
        bot, sockets = self._create_bot()
        result = []

        thread = threading.Thread(
            target=lambda: result.append(bot._play_game("tok", "room-1", "http://server"))
        )
        thread.start()

        # Wait for first socket setup
        for _ in range(50):
            if sockets and sockets[0].emitted:
                break
            time.sleep(0.02)

        self.assertEqual(len(sockets), 1)
        self.assertEqual(sockets[0].emitted[0][0], "mg:join-open-game")

        # Simulate network drop
        sockets[0].simulate_event("disconnect")

        # Wait for fast reconnect to create second socket and rejoin
        for _ in range(50):
            if len(sockets) >= 2 and sockets[1].emitted:
                break
            time.sleep(0.02)

        self.assertEqual(len(sockets), 2)
        self.assertEqual(sockets[1].emitted[0][0], "mg:join-open-game")
        self.assertEqual(sockets[1].emitted[0][1][0], {"miniGameId": "room-1"})
        self.assertGreaterEqual(int(bot.store.get().get("reconnects") or 0), 1)

        # Complete game with awards on reconnected socket
        sockets[1].simulate_event("game-over-awards-available", {"playerAward": {"awardedTotalXp": 50}})
        thread.join(timeout=3.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [True])

    def test_fast_reconnect_disabled_exits(self) -> None:
        bot, sockets = self._create_bot(fast_reconnect_enabled=False)
        result = []

        thread = threading.Thread(
            target=lambda: result.append(bot._play_game("tok", "room-1", "http://server"))
        )
        thread.start()

        for _ in range(50):
            if sockets and sockets[0].emitted:
                break
            time.sleep(0.02)

        sockets[0].simulate_event("disconnect")
        thread.join(timeout=3.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(sockets), 1)
        self.assertEqual(result, [True])

    def test_fast_reconnect_exhausted_gives_up(self) -> None:
        bot, sockets = self._create_bot(fast_reconnect_max_attempts=1)
        failing_sockets = []

        def failing_create_sock(token, url):
            if not sockets:
                s = FakeGameSocket()
                sockets.append(s)
                return s
            s = FakeGameSocket(start_fails=True)
            failing_sockets.append(s)
            return s

        bot._create_game_socket = failing_create_sock
        result = []

        thread = threading.Thread(
            target=lambda: result.append(bot._play_game("tok", "room-1", "http://server"))
        )
        thread.start()

        for _ in range(50):
            if sockets and sockets[0].emitted:
                break
            time.sleep(0.02)

        # Trigger disconnect; the rejoin socket will fail to start
        sockets[0].simulate_event("disconnect")
        thread.join(timeout=3.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(failing_sockets), 1)
        self.assertEqual(result, [True])

    def test_stay_in_room_keeps_socket_open_after_game_over(self) -> None:
        bot, sockets = self._create_bot(stay_in_room=True)
        result = []

        thread = threading.Thread(
            target=lambda: result.append(bot._play_game("tok", "room-1", "http://server"))
        )
        thread.start()

        for _ in range(50):
            if sockets and sockets[0].emitted:
                break
            time.sleep(0.02)

        # Game over with awards
        sockets[0].simulate_event("game-over-awards-available", {"playerAward": {"awardedTotalXp": 80}})
        time.sleep(0.1)

        # Thread should still be running and staying in room (NOT exited)
        self.assertTrue(thread.is_alive())
        self.assertEqual(bot.store.get().get("status"), "staying in room")
        self.assertEqual(int(bot.store.get().get("xp_last") or 0), 80)

        # Verify mg:exit-game was NOT emitted yet
        emitted_events = [e[0] for e in sockets[0].emitted]
        self.assertNotIn("mg:exit-game", emitted_events)

        # Stopping the bot causes clean exit
        bot.stop_event.set()
        thread.join(timeout=3.0)
        self.assertFalse(thread.is_alive())
        final_emitted = [e[0] for e in sockets[0].emitted]
        self.assertIn("mg:exit-game", final_emitted)


class RoleActionHandlersTest(unittest.TestCase):
    def test_reviver_action_queues_medium_event(self) -> None:
        state = types.SimpleNamespace(
            role_id="medium",
            players=[],
            dead_ids=[],
            own_id=lambda: "bot-id",
        )
        policy = types.SimpleNamespace(
            revive_target=mock.MagicMock(return_value="dead-player-1"),
        )
        queued = []
        bot = shell(
            state=state,
            policy=policy,
            _acted_keys=set(),
            _queue_action=lambda name, low, high, act: queued.append((name, act)),
            log_event=mock.MagicMock(),
        )
        fake_sock = mock.MagicMock()
        bot._reviver_action(fake_sock)
        self.assertEqual(len(queued), 1)
        name, act = queued[0]
        self.assertEqual(name, "game-medium-revive-player")
        act()
        fake_sock.emit.assert_called_once_with(
            "game-medium-revive-player",
            '{"targetPlayerId": "dead-player-1"}',
        )

    def test_investigator_action_queues_seer_event(self) -> None:
        state = types.SimpleNamespace(
            role_id="seer",
            players=[],
            dead_ids=[],
            alive=[],
            own_id=lambda: "bot-id",
        )
        policy = types.SimpleNamespace(
            reveal_target=mock.MagicMock(return_value="sus-player-2"),
        )
        queued = []
        bot = shell(
            state=state,
            policy=policy,
            _acted_keys=set(),
            _queue_action=lambda name, low, high, act: queued.append((name, act)),
            log_event=mock.MagicMock(),
        )
        fake_sock = mock.MagicMock()
        bot._investigator_action(fake_sock)
        self.assertEqual(len(queued), 1)
        name, act = queued[0]
        self.assertEqual(name, "game-seer-reveal-player")
        act()
        fake_sock.emit.assert_called_once_with(
            "game-seer-reveal-player",
            '{"targetPlayerId": "sus-player-2"}',
        )

    def test_safe_killer_suppresses_blind_shot_in_vill_win(self) -> None:
        state = types.SimpleNamespace(
            role_id="gunner",
            players=[],
            alive=[],
            day_votes={},
            messages=[],
            own_id=lambda: "bot-id",
        )
        cfg = types.SimpleNamespace(room_names="🏆 VILL WIN 🏆")
        from wvbot.game import Policy
        policy = Policy(cfg)
        queued = []
        bot = shell(
            state=state,
            cfg=cfg,
            policy=policy,
            _acted_keys=set(),
            _queue_action=lambda name, low, high, act: queued.append((name, act)),
            log_event=mock.MagicMock(),
        )
        fake_sock = mock.MagicMock()
        bot._killer_action(fake_sock)
        # In vill-win, no confirmed wolves and no accusations -> suppresses shot
        self.assertEqual(len(queued), 0)


class TurboVillWinFarmTest(unittest.TestCase):
    def test_turbo_mode_delay_is_minimal(self) -> None:
        cfg = types.SimpleNamespace(
            turbo_mode=True,
            turbo_delay_min_sec=1.0,
            turbo_delay_max_sec=2.5,
            delay_min_sec=45.0,
            delay_max_sec=150.0,
            joins_per_hour=12,
        )
        bot = shell(cfg=cfg, _join_times=[], log_event=mock.MagicMock())
        for _ in range(20):
            d = bot._next_delay()
            self.assertGreaterEqual(d, 1.0)
            self.assertLessEqual(d, 2.5)

    def test_turbo_mode_skip_room_is_disabled(self) -> None:
        cfg = types.SimpleNamespace(
            turbo_mode=True,
            room_skip_chance=0.8,
        )
        bot = shell(cfg=cfg)
        for _ in range(10):
            self.assertFalse(bot._skip_room())

    def test_turbo_mode_unlimited_join_budget(self) -> None:
        cfg = types.SimpleNamespace(
            turbo_mode=True,
            joins_per_hour=1,
        )
        bot = shell(cfg=cfg, _join_times=[time.time()] * 100)
        self.assertTrue(bot._join_budget_left())

    def test_pick_room_strictly_prioritizes_8p_rooms(self) -> None:
        cfg = types.SimpleNamespace(
            prefer_small_rooms=True,
            prefer_8p_rooms=True,
        )
        bot = shell(cfg=cfg)
        rooms = [
            {"gameId": "room-16p", "name": "Vill Win 16P (E)", "regularXp": True, "playerCount": 14, "maxPlayerCount": 16},
            {"gameId": "room-8p", "name": "8P VILL WIN FAST", "regularXp": True, "playerCount": 6, "maxPlayerCount": 8},
        ]
        picked = bot._pick_room(rooms)
        self.assertIsNotNone(picked)
        self.assertEqual(picked["gameId"], "room-8p")

    def test_pick_room_prioritizes_tf_duration(self) -> None:
        cfg = types.SimpleNamespace(
            prefer_small_rooms=True,
            prefer_8p_rooms=True,
        )
        bot = shell(cfg=cfg)
        rooms = [
            {"gameId": "room-normal", "name": "Vill Win Normal", "dayDiscussionDurationInMs": 60000, "playerCount": 10},
            {"gameId": "room-tf", "name": "Vill Win Fast", "dayDiscussionDurationInMs": 15000, "playerCount": 5},
        ]
        picked = bot._pick_room(rooms)
        self.assertIsNotNone(picked)
        self.assertEqual(picked["gameId"], "room-tf")

    def test_strict_tf_only_rejects_non_tf_rooms(self) -> None:
        cfg = types.SimpleNamespace(
            prefer_small_rooms=True,
            prefer_8p_rooms=True,
            strict_tf_only=True,
        )
        bot = shell(cfg=cfg)
        slow_rooms = [
            {"gameId": "room-slow", "name": "VIll WIN EEE", "dayDiscussionDurationInMs": 60000, "playerCount": 14},
            {"gameId": "room-normal", "name": "Vill Win Casual", "dayDiscussionDurationInMs": 60000, "playerCount": 10},
        ]
        picked = bot._pick_room(slow_rooms)
        self.assertIsNone(picked)
    def test_wolf_self_claim_emits_seat_number(self) -> None:
        state = types.SimpleNamespace(
            is_wolf=True,
            own_seat=lambda: 5,
            own_id=lambda: "bot-wolf-id",
            claimed_wolf_id=None,
            claimed_wolf_seat=None,
        )
        cfg = types.SimpleNamespace(
            vill_win_mode=True,
            wolf_self_claim=True,
            turbo_mode=True,
        )
        queued = []
        chats = []
        bot = shell(
            state=state,
            cfg=cfg,
            _acted_keys=set(),
            _queue_action=lambda name, low, high, act: queued.append((name, act)),
            chat=lambda sock, msg: chats.append(msg),
            log_event=mock.MagicMock(),
        )
        fake_sock = mock.MagicMock()
        bot._wolf_self_claim(fake_sock)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][0], "wolf claim")
        queued[0][1]()
        self.assertIn("5 w", chats)
        self.assertEqual(state.claimed_wolf_id, "bot-wolf-id")
        self.assertEqual(state.claimed_wolf_seat, 5)

    def test_killer_action_instant_kill_on_claimed_wolf(self) -> None:
        from wvbot.game import Policy
        state = types.SimpleNamespace(
            role_id="gunner",
            players=[
                types.SimpleNamespace(id="bot-gunner", grid_idx=0, is_alive=True, role="gunner", team="VILLAGER"),
                types.SimpleNamespace(id="wolf-target", grid_idx=4, is_alive=True, role="", team=""),
            ],
            alive=[
                types.SimpleNamespace(id="bot-gunner", grid_idx=0, is_alive=True, role="gunner", team="VILLAGER"),
                types.SimpleNamespace(id="wolf-target", grid_idx=4, is_alive=True, role="", team=""),
            ],
            by_id=lambda pid: types.SimpleNamespace(id="wolf-target", grid_idx=4, is_alive=True, role="", team="") if pid == "wolf-target" else None,
            own_id=lambda: "bot-gunner",
            claimed_wolf_id="wolf-target",
            claimed_wolf_seat=5,
            day_votes={},
            messages=[],
        )
        cfg = types.SimpleNamespace(
            room_names=["vill win"],
            vill_win_mode=True,
            instant_kill=True,
        )
        policy = Policy(cfg)
        queued = []
        bot = shell(
            state=state,
            cfg=cfg,
            policy=policy,
            _acted_keys=set(),
            _queue_action=lambda name, low, high, act: queued.append((name, low, high, act)),
            log_event=mock.MagicMock(),
        )
        fake_sock = mock.MagicMock()
        bot._killer_action(fake_sock, instant=True)
        self.assertEqual(len(queued), 1)
        name, low, high, act = queued[0]
        self.assertEqual(name, "game-gunner-shoot-player")
        self.assertLessEqual(low, 0.3)
        act()
        fake_sock.emit.assert_called_once_with(
            "game-gunner-shoot-player",
            '{"targetPlayerId": "wolf-target"}',
        )

    def test_wolf_self_claim_emits_m_in_say_m_rooms(self) -> None:
        state = types.SimpleNamespace(
            is_wolf=True,
            own_seat=lambda: 3,
            own_id=lambda: "bot-wolf-id",
            claimed_wolf_id=None,
            claimed_wolf_seat=None,
        )
        cfg = types.SimpleNamespace(
            vill_win_mode=True,
            wolf_self_claim=True,
            turbo_mode=True,
        )
        fake_store = types.SimpleNamespace(
            get=lambda: {"room": "WW Say M (E)"}
        )
        queued = []
        chats = []
        bot = shell(
            state=state,
            cfg=cfg,
            store=fake_store,
            _acted_keys=set(),
            _queue_action=lambda name, low, high, act: queued.append((name, act)),
            chat=lambda sock, msg: chats.append(msg),
            log_event=mock.MagicMock(),
        )
        fake_sock = mock.MagicMock()
        bot._wolf_self_claim(fake_sock)
        self.assertEqual(len(queued), 1)
        queued[0][1]()
        self.assertIn("m", chats)

    def test_strict_8p_only_rejects_16p_rooms(self) -> None:
        cfg = types.SimpleNamespace(
            strict_8p_only=True,
            prefer_8p_rooms=True,
            prefer_small_rooms=True,
        )
        bot = shell(cfg=cfg)
        rooms_16p = [
            {"name": "vill win 16p", "regularXp": True, "roles": ["r"] * 16, "playerCount": 10},
        ]
        picked = bot._pick_room(rooms_16p)
        self.assertIsNone(picked)

        rooms_with_8p = [
            {"name": "vill win 16p", "regularXp": True, "roles": ["r"] * 16, "playerCount": 10},
            {"name": "WW Say M", "regularXp": True, "roles": ["r"] * 8, "playerCount": 6},
        ]
        picked_8p = bot._pick_room(rooms_with_8p)
        self.assertIsNotNone(picked_8p)
        self.assertEqual(picked_8p["name"], "WW Say M")

    def test_stay_in_room_keeps_socket_open_and_resets_on_next_game(self) -> None:
        cfg_vals = {
            "fast_reconnect_enabled": False,
            "socket_join_delay_sec": 0.0,
            "silence_timeout_sec": 300.0,
            "claim_double_xp": False,
            "stay_in_room": True,
            "stay_in_room_timeout_sec": 0.3,
        }
        cfg = types.SimpleNamespace(**cfg_vals)
        from wvbot.game import GameState

        store = StatusStore()
        bot = shell(
            cfg=cfg,
            store=store,
            stop_event=threading.Event(),
            _restart=threading.Event(),
            _banned=threading.Event(),
            _session_over=threading.Event(),
            state=GameState(),
            auth=types.SimpleNamespace(ensure_token=lambda: "tok123"),
            device_id="dev-test",
            history=types.SimpleNamespace(record_game=lambda xp: None, get=lambda: {}),
        )
        bot.log_event = lambda *a, **k: None
        bot._read_profile = lambda: None
        bot._update_alive = lambda: None
        bot._maybe_chat = lambda *a: None
        bot._night_action = lambda *a: None
        bot._protector_action = lambda *a: None
        bot._reviver_action = lambda *a: None
        bot._investigator_action = lambda *a: None

        sockets = []
        def create_sock(token, url):
            s = FakeGameSocket()
            sockets.append(s)
            return s
        bot._create_game_socket = create_sock

        results = []
        thread = threading.Thread(
            target=lambda: results.append(bot._play_game("tok", "room-1", "http://server"))
        )
        thread.start()

        # Wait for socket join
        for _ in range(50):
            if sockets and sockets[0].emitted:
                break
            time.sleep(0.01)
        self.assertEqual(len(sockets), 1)

        # First game ends with awards
        sockets[0].simulate_event("game-over-awards-available", {"playerAward": {"awardedTotalXp": 120}})
        time.sleep(0.05)

        # Bot must stay in room: no mg:exit-game emitted, store updated
        emitted_events = [e[0] for e in sockets[0].emitted]
        self.assertNotIn("mg:exit-game", emitted_events)
        self.assertEqual(store.get().get("status"), "staying in room")
        self.assertEqual(store.get().get("xp_last"), 120)

        # Host starts second game on the SAME socket
        sockets[0].simulate_event("game-started", {"role": {"id": "villager"}, "players": []})
        time.sleep(0.05)
        self.assertEqual(store.get().get("status"), "playing")
        self.assertEqual(len(sockets), 1)  # No new socket created!

        # Second game ends
        sockets[0].simulate_event("game-over-awards-available", {"playerAward": {"awardedTotalXp": 80}})
        time.sleep(0.05)
        self.assertEqual(store.get().get("xp_last"), 80)
        self.assertEqual(store.get().get("games_played"), 2)

        # Wait for stay_in_room_timeout_sec (0.3s) to expire + exit sleep
        thread.join(timeout=3.0)
        self.assertFalse(thread.is_alive())
        # Now mg:exit-game should have been emitted
        emitted_events = [e[0] for e in sockets[0].emitted]
        self.assertIn("mg:exit-game", emitted_events)

    def test_stay_in_room_disabled_exits_immediately_on_game_over(self) -> None:
        cfg_vals = {
            "fast_reconnect_enabled": False,
            "socket_join_delay_sec": 0.0,
            "silence_timeout_sec": 300.0,
            "claim_double_xp": False,
            "stay_in_room": False,
        }
        cfg = types.SimpleNamespace(**cfg_vals)
        from wvbot.game import GameState

        store = StatusStore()
        bot = shell(
            cfg=cfg,
            store=store,
            stop_event=threading.Event(),
            _restart=threading.Event(),
            _banned=threading.Event(),
            _session_over=threading.Event(),
            state=GameState(),
            auth=types.SimpleNamespace(ensure_token=lambda: "tok123"),
            device_id="dev-test",
            history=types.SimpleNamespace(record_game=lambda xp: None, get=lambda: {}),
        )
        bot.log_event = lambda *a, **k: None
        bot._read_profile = lambda: None

        sockets = []
        def create_sock(token, url):
            s = FakeGameSocket()
            sockets.append(s)
            return s
        bot._create_game_socket = create_sock

        results = []
        thread = threading.Thread(
            target=lambda: results.append(bot._play_game("tok", "room-1", "http://server"))
        )
        thread.start()

        for _ in range(50):
            if sockets and sockets[0].emitted:
                break
            time.sleep(0.01)

        sockets[0].simulate_event("game-over-awards-available", {"playerAward": {"awardedTotalXp": 50}})
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        emitted_events = [e[0] for e in sockets[0].emitted]
        self.assertIn("mg:exit-game", emitted_events)


class BotInitAndLifecycleTest(unittest.TestCase):
    def test_init_accepts_hub_creates_state_dir_and_non_json_history(self) -> None:
        from wvbot.stream import StreamHub

        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "nested" / "state"
            cfg = config_module.Config(state_file="custom_state")
            hub = StreamHub()
            with mock.patch.object(config_module, "STATE_DIR", nested):
                with mock.patch.object(bot_module, "UpdateWatcher") as mock_updates:
                    bot = Bot(cfg, start_web_dashboard=False, hub=hub)

            self.assertIs(bot.hub, hub)
            self.assertTrue((nested / "device_id.txt").is_file())
            self.assertEqual(bot.history.path.name, "custom_state_history.json")
            self.assertEqual(mock_updates.call_count, 1)
            self.assertTrue(callable(mock_updates.call_args[0][2]))
            self.assertEqual(mock_updates.call_args[0][2], bot.auth.auth_headers)

    def test_run_shuts_down_dashboard_in_finally_and_skips_rewards_on_stop(self) -> None:
        server = mock.MagicMock()
        bot = shell(
            cfg=types.SimpleNamespace(
                dashboard_host="127.0.0.1",
                dashboard_port=0,
                reconnect_delay_sec=1.0,
                reconnect_delay_max_sec=5.0,
                bio_break_enabled=False,
                turbo_mode=True,
            ),
            start_web_dashboard=True,
            store=StatusStore(),
            hub=None,
            history=types.SimpleNamespace(record_session=lambda: None, get=lambda: {}),
            auth=types.SimpleNamespace(ensure_token=lambda force=False: "tok", auth_headers=lambda: {}),
            api=types.SimpleNamespace(auth_headers=lambda: {}),
            stop_event=threading.Event(),
            _banned_until=0.0,
            _banned=threading.Event(),
            _paused=threading.Event(),
            _consecutive_games=0,
            _backoff=1.0,
            _failures=0,
        )
        bot.log_event = lambda *a, **k: None
        bot._read_profile = lambda: None
        bot._touch_heartbeat = lambda: None
        bot._refresh_profile = lambda: None
        bot._check_updates = lambda: None
        bot._nightly_restart_due = lambda: False
        bot._next_delay = lambda: 0.0
        reward_calls = []
        bot._collect_rewards = lambda: reward_calls.append(True)

        def stop_in_session() -> bool:
            bot.stop_event.set()
            return True

        bot.session = stop_in_session

        with mock.patch.object(bot_module, "start_dashboard", return_value=server):
            bot.run()

        server.shutdown.assert_called_once_with()
        server.server_close.assert_called_once_with()
        self.assertEqual(reward_calls, [])


class StayInRoomRaceAndDoubleXpTest(unittest.TestCase):
    def test_stay_in_room_does_not_clobber_fast_rematch(self) -> None:
        from wvbot.game import GameState

        cfg = types.SimpleNamespace(
            fast_reconnect_enabled=False,
            socket_join_delay_sec=0.0,
            silence_timeout_sec=300.0,
            claim_double_xp=False,
            stay_in_room=True,
            stay_in_room_timeout_sec=0.25,
            stay_in_room_max_game_duration_sec=150.0,
            reflex_chat_enabled=False,
            vill_win_mode=False,
            wolf_self_claim=False,
            instant_kill=False,
        )
        store = StatusStore()
        bot = shell(
            cfg=cfg,
            store=store,
            stop_event=threading.Event(),
            _restart=threading.Event(),
            _banned=threading.Event(),
            _session_over=threading.Event(),
            _acted_keys=set(),
            state=GameState(),
            auth=types.SimpleNamespace(ensure_token=lambda: "tok"),
            device_id="dev",
            history=types.SimpleNamespace(record_game=lambda xp: None, get=lambda: {}),
        )
        bot.log_event = lambda *a, **k: None
        bot._update_alive = lambda: None
        bot._maybe_chat = lambda *a: None
        bot._night_action = lambda *a: None
        bot._killer_action = lambda *a, **k: None
        bot._protector_action = lambda *a: None
        bot._reviver_action = lambda *a: None
        bot._investigator_action = lambda *a: None

        sockets: list[FakeGameSocket] = []
        bot._create_game_socket = lambda token, url: (sockets.append(FakeGameSocket()) or sockets[-1])

        read_count = [0]
        saw_game2_started = threading.Event()

        def racy_read_profile() -> None:
            read_count[0] += 1
            if read_count[0] == 1:
                sockets[0].simulate_event(
                    "game-started",
                    {"role": {"id": "villager"}, "players": [{"id": 1}]},
                )
                bot._acted_keys.add("rematch-action")
                saw_game2_started.set()

        bot._read_profile = racy_read_profile

        thread = threading.Thread(
            target=lambda: bot._play_game("tok", "room-1", "http://server"),
            daemon=True,
        )
        thread.start()

        for _ in range(50):
            if sockets and sockets[0].emitted:
                break
            time.sleep(0.01)

        sockets[0].simulate_event(
            "game-over-awards-available",
            {"playerAward": {"awardedTotalXp": 90}},
        )
        self.assertTrue(saw_game2_started.wait(timeout=2.0))
        time.sleep(0.05)

        # Game #2's state must survive _process_game_award finishing Game #1.
        self.assertEqual(bot.state.phase, "started")
        self.assertEqual(store.get().get("status"), "playing")
        self.assertIn("rematch-action", bot._acted_keys)

        # Finish Game #2 cleanly and verify stay_in_room timeout exits the room.
        sockets[0].simulate_event(
            "game-over-awards-available",
            {"playerAward": {"awardedTotalXp": 40}},
        )
        thread.join(timeout=3.0)
        self.assertFalse(thread.is_alive())

    def test_double_xp_claimed_once_and_clean_exit_keeps_reconnects_zero(self) -> None:
        from wvbot.game import GameState

        cfg = types.SimpleNamespace(
            fast_reconnect_enabled=False,
            socket_join_delay_sec=0.0,
            silence_timeout_sec=300.0,
            claim_double_xp=True,
            stay_in_room=False,
            stay_in_room_max_game_duration_sec=150.0,
            reflex_chat_enabled=False,
        )
        store = StatusStore()
        bot = shell(
            cfg=cfg,
            store=store,
            stop_event=threading.Event(),
            _restart=threading.Event(),
            _banned=threading.Event(),
            _session_over=threading.Event(),
            state=GameState(),
            auth=types.SimpleNamespace(ensure_token=lambda: "tok"),
            device_id="dev",
            history=types.SimpleNamespace(record_game=lambda xp: None, get=lambda: {}),
        )
        bot.log_event = lambda *a, **k: None
        bot._read_profile = lambda: None
        sockets: list[FakeGameSocket] = []
        bot._create_game_socket = lambda token, url: (sockets.append(FakeGameSocket()) or sockets[-1])

        thread = threading.Thread(target=lambda: bot._play_game("tok", "room-1", "http://server"))
        thread.start()

        for _ in range(50):
            if sockets and sockets[0].emitted:
                break
            time.sleep(0.01)

        sockets[0].simulate_event(
            "game-over-awards-available",
            {"playerAward": {"awardedTotalXp": 50, "canClaimDoubleXp": True}},
        )
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())

        double_xp_emits = [e for e in sockets[0].emitted if e[0] == "game-over-double-xp"]
        self.assertEqual(len(double_xp_emits), 1)
        self.assertEqual(int(store.get().get("reconnects") or 0), 0)


class MultiAttemptFastReconnectAndPasswordTest(unittest.TestCase):
    def test_multi_attempt_fast_reconnect_succeeds_after_initial_setup_failure(self) -> None:
        from wvbot.game import GameState

        cfg = types.SimpleNamespace(
            fast_reconnect_enabled=True,
            fast_reconnect_max_attempts=3,
            fast_reconnect_delay_sec=0.01,
            socket_join_delay_sec=0.0,
            silence_timeout_sec=300.0,
            claim_double_xp=False,
            room_password="pw123",
        )
        store = StatusStore()
        bot = shell(
            cfg=cfg,
            store=store,
            stop_event=threading.Event(),
            _restart=threading.Event(),
            _banned=threading.Event(),
            _session_over=threading.Event(),
            state=GameState(),
            auth=types.SimpleNamespace(ensure_token=lambda: "tok"),
            device_id="dev",
            history=types.SimpleNamespace(record_game=lambda xp: None, get=lambda: {}),
        )
        bot.log_event = lambda *a, **k: None
        bot._read_profile = lambda: None

        created: list[FakeGameSocket] = []

        def create_sock(token, url):
            # Socket 0 connects, Socket 1 fails start(), Socket 2 succeeds immediately.
            s = FakeGameSocket(start_fails=(len(created) == 1))
            created.append(s)
            return s

        bot._create_game_socket = create_sock
        results = []
        thread = threading.Thread(
            target=lambda: results.append(bot._play_game("tok", "room-9", "http://server"))
        )
        thread.start()

        for _ in range(50):
            if created and created[0].emitted:
                break
            time.sleep(0.01)

        self.assertEqual(created[0].emitted[0], ("mg:join-open-game", ({"miniGameId": "room-9", "password": "pw123"},)))
        created[0].simulate_event("disconnect")

        for _ in range(100):
            if len(created) >= 3 and created[2].emitted:
                break
            time.sleep(0.01)

        self.assertEqual(len(created), 3)
        self.assertTrue(created[1].closed)
        self.assertEqual(int(store.get().get("reconnects") or 0), 2)

        created[2].simulate_event("game-over-awards-available", {"playerAward": {"awardedTotalXp": 30}})
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [True])


class ImmediateVoteSafetyAndPhaseTest(unittest.TestCase):
    def _bot(self, phase: str) -> Bot:
        from wvbot.game import GameState, Policy

        state = GameState()
        state.apply(
            "game-started",
            {"players": [{"id": 1, "gridIdx": 0}, {"id": 2, "gridIdx": 1}]},
        )
        state.set_own_id(1)
        state.phase = phase
        state.messages.append({"authorId": 1, "msg": "vote 2"})
        return shell(
            cfg=types.SimpleNamespace(turbo_mode=True),
            state=state,
            policy=Policy(),
            store=StatusStore(),
            _acted_keys=set(),
            stop_event=threading.Event(),
            log_event=mock.MagicMock(),
            _queue_action=lambda name, low, high, act: act(),
        )

    def test_immediate_vote_catches_socket_emit_error(self) -> None:
        bot = self._bot("day-voting")
        sock = mock.MagicMock()
        sock.emit.side_effect = OSError("socket closed")
        bot._day_vote(sock, immediate=True)
        bot.log_event.assert_called()

    def test_out_of_phase_blocks_immediate_and_queued_votes(self) -> None:
        for bad_phase in ("started", "night", "lobby"):
            bot = self._bot(bad_phase)
            sock = mock.MagicMock()
            bot._day_vote(sock, immediate=True)
            bot._day_vote(sock, immediate=False)
            sock.emit.assert_not_called()


class ReexecAndFarmModeTest(unittest.TestCase):
    def test_reexec_disabled_in_farm_mode_variants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config_module, "STATE_DIR", Path(tmp)):
                # Variant 1: injected hub
                b1 = shell(store=StatusStore(), _hub_injected=True, log_event=mock.MagicMock())
                # Variant 2: start_web_dashboard=False (farm child bot)
                b2 = shell(store=StatusStore(), start_web_dashboard=False, log_event=mock.MagicMock())
                # Variant 3: WV_BOT_FARM env var
                b3 = shell(store=StatusStore(), log_event=mock.MagicMock())

                with mock.patch.object(bot_module.os, "execv") as execv_mock:
                    b1._reexec()
                    b2._reexec()
                    with mock.patch.dict(os.environ, {"WV_BOT_FARM": "1"}):
                        b3._reexec()
                    execv_mock.assert_not_called()

    def test_reexec_argv_supports_module_invocation(self) -> None:
        with mock.patch.object(sys, "orig_argv", [sys.executable, "-m", "wvbot", "--fast"], create=True):
            self.assertEqual(Bot._reexec_argv(), [sys.executable, "-m", "wvbot", "--fast"])

        with mock.patch.object(sys, "orig_argv", [], create=True):
            with mock.patch.object(sys, "argv", ["C:/repo/wvbot/__main__.py", "--flag"]):
                self.assertEqual(Bot._reexec_argv(), [sys.executable, "-m", "wvbot", "--flag"])


class WolfSelfClaimDay1AndDeadGuardTest(unittest.TestCase):
    def test_wolf_self_claim_fires_once_on_day1_and_resets_next_night(self) -> None:
        from wvbot.game import GameState, Policy

        state = GameState()
        state.set_own_id("w1")
        cfg = types.SimpleNamespace(
            vill_win_mode=True,
            wolf_self_claim=True,
            turbo_mode=True,
            instant_kill=False,
            reflex_chat_enabled=False,
            chat_enabled=True,
        )
        bot = shell(
            cfg=cfg,
            state=state,
            policy=Policy(cfg),
            store=StatusStore(),
            _acted_keys=set(),
            stop_event=threading.Event(),
            _queue_action=lambda name, low, high, act: act(),
        )
        bot.store.update(room="WW Say M (E)")
        bot.log_event = lambda *a, **k: None
        bot._find_own_id = lambda: state.set_own_id("w1")
        bot._update_alive = lambda: None
        bot._player_levels = lambda: []
        bot._maybe_chat = lambda *a: None
        bot._night_action = lambda *a: None
        bot._killer_action = lambda *a, **k: None
        bot._protector_action = lambda *a: None
        bot._reviver_action = lambda *a: None
        bot._investigator_action = lambda *a: None
        sock = FakeGameSocket()

        bot._on_game_event(
            sock,
            "game-started",
            ({"role": {"id": "werewolf"}, "players": [{"id": "w1", "gridIdx": 0}, {"id": "v1", "gridIdx": 1}]},),
        )
        bot._on_game_event(sock, "game-day-started", ())
        bot._on_game_event(sock, "game-day-voting-started", ())
        self.assertEqual(len([e for e in sock.emitted if e[0] == "game:chat-public:msg"]), 1)

        bot._on_game_event(sock, "game-night-started", ())
        bot._on_game_event(sock, "game-day-started", ())
        self.assertEqual(len([e for e in sock.emitted if e[0] == "game:chat-public:msg"]), 2)

    def test_dead_bot_blocks_all_active_role_actions(self) -> None:
        from wvbot.game import GameState, Policy

        for role_id in ("werewolf", "gunner", "doctor", "medium", "seer"):
            state = GameState()
            state.apply(
                "game-started",
                {
                    "role": {"id": role_id},
                    "players": [
                        {"id": 1, "gridIdx": 0, "isAlive": False},
                        {"id": 2, "gridIdx": 1, "isAlive": True, "role": "werewolf"},
                        {"id": 3, "gridIdx": 2, "isAlive": False, "role": "villager"},
                    ],
                },
            )
            state.set_own_id(1)
            state.dead_ids = [1, 3]
            state.phase = "day-voting"
            bot = shell(
                cfg=types.SimpleNamespace(
                    vill_win_mode=True,
                    wolf_self_claim=True,
                    turbo_mode=True,
                    instant_kill=True,
                    chat_enabled=True,
                ),
                state=state,
                policy=Policy(),
                store=StatusStore(),
                _acted_keys=set(),
                stop_event=threading.Event(),
                _queue_action=lambda name, low, high, act: act(),
            )
            bot.log_event = lambda *a, **k: None
            sock = FakeGameSocket()
            bot._night_action(sock)
            bot._wolf_self_claim(sock)
            bot._day_vote(sock, immediate=True)
            bot._killer_action(sock, instant=True)
            bot._protector_action(sock)
            bot._reviver_action(sock)
            bot._investigator_action(sock)
            self.assertEqual(sock.emitted, [], msg=f"dead {role_id} emitted {sock.emitted}")


class PickRoomAndSessionFilterTest(unittest.TestCase):
    def test_pick_room_folds_unicode_safe_casts_and_filters_full(self) -> None:
        cfg = types.SimpleNamespace(prefer_small_rooms=True, max_room_size=10)
        bot = shell(store=StatusStore(), cfg=cfg)
        rooms = [
            {"gameId": "full-max", "name": "TF Full", "playerCount": 8, "maxPlayerCount": 8},
            {"gameId": "full-cfg", "name": "TF Cap", "playerCount": 10},
            {"gameId": "good-tf", "name": "E VİLL WİN TF", "playerCount": "bad", "roles": ["r"] * 8},
            {"gameId": "slow", "name": "Normal Room", "playerCount": 5, "regularXp": True},
        ]
        self.assertEqual(bot._pick_room(rooms)["gameId"], "good-tf")

    def test_session_updates_status_when_pick_room_returns_none(self) -> None:
        from wvbot.game import GameState

        store = StatusStore()
        bot = shell(
            cfg=types.SimpleNamespace(room_names=["vill"], language="en", strict_8p_only=True),
            state=GameState(),
            store=store,
            _acted_keys=set(),
            auth=types.SimpleNamespace(ensure_token=lambda: "tok", auth_headers=lambda: {}),
            api=types.SimpleNamespace(
                find_rooms=lambda names: [{"gameId": "r1", "roles": ["r"] * 16, "playerCount": 10}],
                auth_headers=lambda: {},
            ),
            log_event=mock.MagicMock(),
        )
        self.assertFalse(bot.session())
        self.assertEqual(store.get().get("status"), "waiting for room")


class RewardsGatingAndSafetyTest(unittest.TestCase):
    def test_collect_rewards_gates_loot_boxes_on_open_loot_boxes_flag(self) -> None:
        fake_rewards = mock.MagicMock()
        fake_rewards.daily_rewards.return_value = []
        bot = shell(
            cfg=types.SimpleNamespace(
                claim_daily_rewards=True,
                spin_golden_wheel=False,
                open_loot_boxes=False,
            ),
            http=mock.MagicMock(),
            auth=types.SimpleNamespace(auth_headers=lambda: {}),
            log_event=mock.MagicMock(),
        )
        with mock.patch.object(bot_module, "Rewards", return_value=fake_rewards):
            bot._collect_rewards()
        fake_rewards.open_loot_boxes.assert_not_called()

        bot.cfg.open_loot_boxes = True
        fake_rewards.loot_box_count.return_value = 2
        with mock.patch.object(bot_module, "Rewards", return_value=fake_rewards):
            bot._collect_rewards()
        fake_rewards.open_loot_boxes.assert_called_once_with()

    def test_claim_daily_writes_utc_marker_only_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config_module, "STATE_DIR", Path(tmp)):
                bot = shell(store=StatusStore(), log_event=mock.MagicMock())
                fake_rewards = mock.MagicMock()
                fake_rewards.daily_rewards.return_value = [{"type": "GOLD", "amount": 25}]
                fake_rewards.claim_daily_reward.side_effect = [RuntimeError("503"), {"ok": True}]

                # Transient error must leave daily_claim.txt absent so next attempt retries.
                bot._claim_daily(fake_rewards)
                marker = Path(tmp) / "daily_claim.txt"
                self.assertFalse(marker.exists())

                bot._claim_daily(fake_rewards)
                self.assertEqual(fake_rewards.claim_daily_reward.call_count, 2)
                expected_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                self.assertEqual(marker.read_text(encoding="utf-8").strip(), f"{expected_day}|GOLD")

    def test_spin_wheel_requires_valid_cost_in_roses(self) -> None:
        bot = shell(cfg=types.SimpleNamespace(wheel_max_roses=30), log_event=mock.MagicMock())
        for bad_cond in ({}, {"costInRoses": None}, {"costInRoses": "nan"}):
            fake_rewards = mock.MagicMock()
            fake_rewards.golden_wheel_conditions.return_value = bad_cond
            bot._spin_wheel(fake_rewards)
            fake_rewards.spin_golden_wheel.assert_not_called()


if __name__ == "__main__":
    unittest.main()
