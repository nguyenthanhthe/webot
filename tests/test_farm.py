"""Tests of the farm: the roster, the supervision, the snapshot.

Every bot here is a fake - the farm contract is small (run, stop_event,
store) and that is exactly what the tests pin down.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from wvbot.dashboard import StatusStore
from wvbot.farm import Farm, RosterError, clean_name, load_roster


class FakeBot:
    """A bot that runs its loop until stopped, or dies on cue."""

    def __init__(self, account: dict, die_after: float | None = None, crash: bool = False):
        self.account = account
        self.name = account.get("name")
        self.store = StatusStore()
        self.stop_event = threading.Event()
        self._die_after = die_after
        self._crash = crash
        self.ran = 0

    def run(self) -> None:
        self.ran += 1
        self.store.update(status="playing", xp_total=10)
        while not self.stop_event.wait(0.05):
            if self._die_after is not None and time.monotonic() > self._die_after:
                if self._crash:
                    raise RuntimeError("boom")
                return  # a clean stop looks like a crash to the farm
        self.store.update(status="stopped")


class RosterTest(unittest.TestCase):
    def test_no_roster_file_falls_back_to_the_classic_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_roster(Path(tmp)), [{"name": "main"}])

    def test_the_roster_is_read_and_names_are_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "accounts.json").write_text(
                json.dumps(
                    {
                        "accounts": [
                            {"name": "Main Account!", "refresh_token": "r1"},
                            {"name": "alt-2", "refresh_token": "r2"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            roster = load_roster(root)
            self.assertEqual([a["name"] for a in roster], ["MainAccount", "alt-2"])

    def test_duplicate_names_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "accounts.json").write_text(
                json.dumps({"accounts": [{"name": "a"}, {"name": "a"}]}),
                encoding="utf-8",
            )
            with self.assertRaises(RosterError):
                load_roster(root)

    def test_a_broken_roster_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "accounts.json").write_text("{not json", encoding="utf-8")
            with self.assertRaises(RosterError):
                load_roster(root)

    def test_names_become_safe_directory_names(self) -> None:
        self.assertEqual(clean_name("../../etc"), "etc")
        self.assertEqual(clean_name("a/b c"), "abc")
        self.assertEqual(clean_name(""), "")


class FarmTest(unittest.TestCase):
    def test_the_farm_runs_one_thread_per_account(self) -> None:
        made: list[str] = []
        farm = Farm([{"name": "a"}, {"name": "b"}], lambda acc: made.append(acc["name"]) or FakeBot(acc))
        farm.start()
        try:
            time.sleep(0.3)
            self.assertEqual(sorted(made), ["a", "b"])
        finally:
            farm.stop()

    def test_a_dead_bot_is_restarted(self) -> None:
        built: list[FakeBot] = []

        def factory(acc: dict) -> FakeBot:
            bot = FakeBot(acc, die_after=time.monotonic() + 0.1)
            built.append(bot)
            return bot

        farm = Farm([{"name": "a"}], factory)
        farm.start()
        try:
            time.sleep(1.5)
            # The first bot died on cue; the supervisor launched another.
            self.assertGreaterEqual(built[0].ran, 1)
            self.assertGreaterEqual(len(built), 2)
        finally:
            farm.stop()

    def test_a_failing_bot_is_parked_after_three_crashes(self) -> None:
        crashes = {"n": 0}

        def factory(acc: dict) -> FakeBot:
            crashes["n"] += 1
            return FakeBot(acc, die_after=time.monotonic() + 0.05, crash=True)

        farm = Farm([{"name": "a"}], factory)
        farm.start()
        try:
            deadline = time.time() + 8
            while time.time() < deadline:
                if farm.snapshot()["bots"][0].get("status") == "failed":
                    break
                time.sleep(0.1)
            self.assertEqual(farm.snapshot()["bots"][0]["status"], "failed")
            self.assertGreaterEqual(farm.snapshot()["bots"][0]["crashes"], 3)
            built = crashes["n"]
            time.sleep(0.6)
            # Parked means parked: no further restart attempts.
            self.assertEqual(crashes["n"], built)
        finally:
            farm.stop()

    def test_the_max_bots_cap_holds(self) -> None:
        farm = Farm([{"name": str(i)} for i in range(9)], FakeBot, max_bots=3)
        self.assertEqual(len(farm.snapshot()["bots"]), 3)

    def test_the_snapshot_aggregates_the_farm(self) -> None:
        farm = Farm([{"name": "a"}, {"name": "b"}], FakeBot)
        farm.start()
        try:
            time.sleep(0.3)
            data = farm.snapshot()
            self.assertEqual(data["total_xp"], 20)
            self.assertEqual(data["games"], 0)
            self.assertEqual(len(data["bots"]), 2)
        finally:
            farm.stop()

    def test_stop_reaches_every_bot(self) -> None:
        built: list[FakeBot] = []

        def factory(acc: dict) -> FakeBot:
            bot = FakeBot(acc)
            built.append(bot)
            return bot

        farm = Farm([{"name": "a"}, {"name": "b"}], factory)
        farm.start()
        time.sleep(0.2)
        farm.stop()
        time.sleep(0.3)
        self.assertTrue(all(b.stop_event.is_set() for b in built))

    def test_farm_controls_propagate_to_bots(self) -> None:
        bots = {}

        class ControllableBot(FakeBot):
            def __init__(self, acc):
                super().__init__(acc)
                self.paused = False

            def _control(self, action: str) -> bool:
                if action == "pause":
                    self.paused = True
                    return True
                return False

        farm = Farm([{"name": "a"}, {"name": "b"}], lambda acc: bots.setdefault(acc["name"], ControllableBot(acc)))
        farm.start()
        try:
            time.sleep(0.2)
            self.assertTrue(farm.control("pause", bot_name="a"))
            self.assertTrue(bots["a"].paused)
            self.assertFalse(bots["b"].paused)
        finally:
            farm.stop()

    def test_default_bot_factory(self) -> None:
        from wvbot.farm import default_bot_factory
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "accounts" / "sub1").mkdir(parents=True, exist_ok=True)
            (root / "accounts" / "sub1" / "config.json").write_text(json.dumps({"language": "vi"}), encoding="utf-8")
            bot = default_bot_factory({"name": "sub1"}, farm_root=root)
            self.assertEqual(bot.account_dir, root / "accounts" / "sub1")
            self.assertEqual(bot.cfg.language, "vi")
            self.assertFalse(bot.start_web_dashboard)



if __name__ == "__main__":
    unittest.main()
