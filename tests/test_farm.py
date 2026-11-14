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

    def test_save_and_remove_account_from_roster(self) -> None:
        from wvbot.farm import remove_account_from_roster, save_account_to_roster
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_account_to_roster(root, "sub2", refresh_token="tok456", overrides={"language": "en"})
            roster = load_roster(root)
            self.assertEqual(len(roster), 1)
            self.assertEqual(roster[0]["name"], "sub2")
            self.assertEqual(roster[0]["language"], "en")
            token_file = root / "accounts" / "sub2" / "refresh_token.txt"
            self.assertTrue(token_file.is_file())
            self.assertEqual(token_file.read_text(encoding="utf-8").strip(), "tok456")

            # Remove account
            self.assertTrue(remove_account_from_roster(root, "sub2"))
            self.assertFalse(remove_account_from_roster(root, "sub2"))
            self.assertEqual(load_roster(root), [{"name": "main"}])

    def test_farm_dynamic_add_and_remove_account(self) -> None:
        built: list[FakeBot] = []

        def factory(acc: dict) -> FakeBot:
            bot = FakeBot(acc)
            built.append(bot)
            return bot

        farm = Farm([{"name": "bot1"}], factory, max_bots=3)
        farm.start()
        try:
            time.sleep(0.2)
            self.assertEqual(len(farm._slots), 1)
            self.assertEqual(len(built), 1)

            # Hot-add bot2
            self.assertTrue(farm.add_account({"name": "bot2"}))
            time.sleep(0.2)
            self.assertEqual(len(farm._slots), 2)
            self.assertEqual(len(built), 2)
            self.assertEqual(built[1].name, "bot2")

            # Duplicate add is rejected
            self.assertFalse(farm.add_account({"name": "bot2"}))

            # Remove bot2
            self.assertTrue(farm.remove_account("bot2"))
            self.assertEqual(len(farm._slots), 1)
            self.assertTrue(built[1].stop_event.is_set())

            # Remove nonexistent
            self.assertFalse(farm.remove_account("bot2"))
        finally:
            farm.stop()

    def test_accounts_api_and_dashboard(self) -> None:
        import urllib.request
        from wvbot.dashboard import get_accounts_summary, start_dashboard
        from wvbot.farm import save_account_to_roster

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_account_to_roster(root, "alpha", "tok_alpha")
            farm = Farm([{"name": "alpha"}], FakeBot, farm_root=root)
            server = start_dashboard(host="127.0.0.1", port=0, farm=farm)
            port = server.server_address[1]
            try:
                # 1. get_accounts_summary
                summary = get_accounts_summary(root, farm)
                self.assertEqual(len(summary), 1)
                self.assertEqual(summary[0]["name"], "alpha")
                self.assertTrue(summary[0]["ready"])
                self.assertTrue(summary[0]["has_token"])
                self.assertNotIn("refresh_token", summary[0])

                # 2. GET /api/accounts
                req = urllib.request.Request(f"http://127.0.0.1:{port}/api/accounts")
                with urllib.request.urlopen(req) as resp:
                    self.assertEqual(resp.status, 200)
                    data = json.loads(resp.read().decode())
                    self.assertEqual(data["count"], 1)
                    self.assertEqual(data["accounts"][0]["name"], "alpha")

                # 3. POST /api/accounts (add beta)
                add_data = json.dumps({"name": "beta", "refresh_token": "tok_beta"}).encode()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/accounts",
                    data=add_data,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req) as resp:
                    self.assertEqual(resp.status, 200)
                    res_body = json.loads(resp.read().decode())
                    self.assertTrue(res_body["ok"])
                    self.assertEqual(res_body["name"], "beta")

                # Verify beta was added to farm slots
                self.assertTrue(any(s.name == "beta" for s in farm._slots))

                # 4. DELETE /api/accounts/beta
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/accounts/beta",
                    method="DELETE",
                )
                with urllib.request.urlopen(req) as resp:
                    self.assertEqual(resp.status, 200)
                    res_body = json.loads(resp.read().decode())
                    self.assertTrue(res_body["ok"])
                    self.assertTrue(res_body["removed"])

                # Verify beta removed from farm slots
                self.assertFalse(any(s.name == "beta" for s in farm._slots))

                # 5. OPTIONS /api/accounts
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/accounts",
                    method="OPTIONS",
                )
                with urllib.request.urlopen(req) as resp:
                    self.assertEqual(resp.status, 204)
                    self.assertIn("DELETE", resp.headers.get("Access-Control-Allow-Methods", ""))
            finally:
                server.shutdown()
                server.server_close()


class FarmSecurityAndResilienceTest(unittest.TestCase):
    def test_account_name_traversal_rejected_while_clean_name_compatible(self):
        from wvbot.farm import (
            Farm,
            RosterError,
            clean_name,
            default_bot_factory,
            load_roster,
            remove_account_from_roster,
            save_account_to_roster,
        )

        self.assertEqual(clean_name("../../etc"), "etc")

        bad_names = ["../evil", "..", ".", "a/b", "a\\b", "bad\x00name", ""]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for bad in bad_names:
                with self.subTest(bad=bad):
                    with self.assertRaises((RosterError, ValueError)):
                        save_account_to_roster(root, bad, refresh_token="tok")
                    with self.assertRaises((RosterError, ValueError)):
                        default_bot_factory({"name": bad}, farm_root=root)

            farm = Farm([], bot_factory=lambda acc: None, farm_root=root)
            for bad in bad_names:
                self.assertFalse(farm.add_account({"name": bad}))
                self.assertFalse(farm.remove_account(bad))
                self.assertFalse(remove_account_from_roster(root, bad))

            (root / "accounts.json").write_text(
                json.dumps({"accounts": [{"name": "../escape"}]}),
                encoding="utf-8",
            )
            with self.assertRaises((RosterError, ValueError)):
                load_roster(root)

    def test_corrupt_or_non_list_roster_not_wiped_by_save(self):
        from wvbot.farm import RosterError, save_account_to_roster

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roster_path = root / "accounts.json"

            corrupt_text = "{not valid json"
            roster_path.write_text(corrupt_text, encoding="utf-8")
            with self.assertRaises(RosterError):
                save_account_to_roster(root, "alice", refresh_token="tok")
            self.assertEqual(roster_path.read_text(encoding="utf-8"), corrupt_text)

            non_list_text = json.dumps({"accounts": {"alice": {}}})
            roster_path.write_text(non_list_text, encoding="utf-8")
            with self.assertRaises(RosterError):
                save_account_to_roster(root, "alice", refresh_token="tok")
            self.assertEqual(roster_path.read_text(encoding="utf-8"), non_list_text)

    def test_default_bot_factory_preserves_rotated_token_inherits_safe_defaults_and_blocks_method_override(self):
        from wvbot.farm import default_bot_factory

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Root config has both safe shared defaults and sensitive fields
            (root / "config.json").write_text(
                json.dumps({
                    "use_asia_host": True,
                    "daily_restart_hour": 5,
                    "game_mode": "quick",
                    "refresh_token": "root-secret-token",
                    "email": "root@example.com",
                    "password": "root-secret-password",
                }),
                encoding="utf-8",
            )

            acc_dir = root / "accounts" / "alice"
            acc_dir.mkdir(parents=True)
            (acc_dir / "refresh_token.txt").write_text("rotated-on-disk-token\n", encoding="utf-8")

            bot = default_bot_factory(
                {
                    "name": "alice",
                    "refresh_token": "stale-roster-token",
                    "game_base_url": "https://evil.example",
                    "to_dict": "not-a-method",
                    "__class__": "hijack",
                    "daily_restart_hour": 2,
                },
                farm_root=root,
            )

            # Rotated disk token wins over stale inline token
            self.assertEqual((acc_dir / "refresh_token.txt").read_text(encoding="utf-8").strip(), "rotated-on-disk-token")
            self.assertEqual(bot.cfg.refresh_token, "rotated-on-disk-token")
            # Inherits non-secret root defaults unless overridden by account
            self.assertTrue(bot.cfg.use_asia_host)
            self.assertEqual(bot.cfg.game_mode, "quick")
            self.assertEqual(bot.cfg.daily_restart_hour, 2)
            # Root secrets are never inherited by per-account bot
            self.assertEqual(bot.cfg.email, "")
            self.assertEqual(bot.cfg.password, "")
            # Config methods and @property were NOT overwritten via setattr
            self.assertTrue(callable(bot.cfg.to_dict))
            self.assertNotEqual(bot.cfg.game_base_url, "https://evil.example")

    def test_default_bot_factory_seeds_missing_or_empty_token_file(self):
        from wvbot.farm import default_bot_factory

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            acc_dir = root / "accounts" / "bob"
            acc_dir.mkdir(parents=True)
            (acc_dir / "refresh_token.txt").write_text("   \n", encoding="utf-8")

            bot = default_bot_factory(
                {"name": "bob", "refresh_token": "fresh-roster-token"},
                farm_root=root,
            )
            self.assertEqual((acc_dir / "refresh_token.txt").read_text(encoding="utf-8"), "fresh-roster-token")
            self.assertEqual(bot.cfg.refresh_token, "fresh-roster-token")

    def test_unexpected_clean_exit_counts_as_crash_backoff_and_healthy_reset(self):
        from wvbot.farm import Farm

        class CleanExitBot:
            def __init__(self, account):
                self.stop_event = threading.Event()
                self.pause_event = threading.Event()
                self.hub = None
                self.store = type("S", (), {"get": lambda _s: {"status": "playing"}})()
                self.runs = 0

            def run(self):
                self.runs += 1
                # Exits unexpectedly without raising

        created: list[CleanExitBot] = []

        def strict_one_arg_factory(account):
            bot = CleanExitBot(account)
            created.append(bot)
            return bot

        farm = Farm(
            [{"name": "cleanexit"}],
            bot_factory=strict_one_arg_factory,
            farm_root=Path(tempfile.gettempdir()),
        )
        farm.crash_backoff_base = 0.02
        farm.crash_backoff_max = 0.05
        farm.start()
        try:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                snap = farm.snapshot()["bots"][0]
                if snap["status"] == "failed":
                    break
                time.sleep(0.02)
            snap = farm.snapshot()["bots"][0]
            self.assertEqual(snap["status"], "failed")
            self.assertEqual(snap["crashes"], 3)
            self.assertEqual(len(created), 3)
            self.assertEqual(sum(b.runs for b in created), 3)
            # Hub attached without changing 1-arg bot_factory contract
            self.assertIs(created[0].hub, farm.hub)
        finally:
            farm.stop()

        # Sustained healthy uptime resets crash counter
        class HealthyThenExitBot:
            def __init__(self, account):
                self.stop_event = threading.Event()
                self.pause_event = threading.Event()
                self.runs = 0

            def run(self):
                self.runs += 1
                if self.runs == 1:
                    raise RuntimeError("transient boot hiccup")
                if self.runs == 2:
                    time.sleep(0.08)
                    raise RuntimeError("later hiccup after healthy uptime")
                self.stop_event.wait(2.0)

        healthy_bot = HealthyThenExitBot({"name": "heal"})
        farm2 = Farm([{"name": "heal"}], bot_factory=lambda acc: healthy_bot, farm_root=Path(tempfile.gettempdir()))
        farm2.crash_backoff_base = 0.01
        farm2.crash_backoff_max = 0.02
        farm2.healthy_uptime_reset_sec = 0.04
        farm2.start()
        try:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if healthy_bot.runs >= 3:
                    break
                time.sleep(0.02)
            self.assertEqual(healthy_bot.runs, 3)
            snap = farm2.snapshot()["bots"][0]
            self.assertNotEqual(snap["status"], "failed")
            # First crash reset during healthy run 2, so after run 2's crash count was 1 (not 2)
            self.assertLessEqual(snap["crashes"], 1)
        finally:
            farm2.stop()

    def test_slot_status_updates_reward_snapshot_resilience_and_stop_closes_dashboard(self):
        from wvbot.farm import Farm

        class RichFakeBot:
            def __init__(self, account):
                self.stop_event = threading.Event()
                self.pause_event = threading.Event()
                self.started = threading.Event()
                self.exited = threading.Event()
                self.rewards: list[str] = []
                self.fail_store = False
                self.store = self

            def get(self):
                if self.fail_store:
                    raise RuntimeError("store read error")
                return {"status": "in_game", "level": 42}

            def _reward(self, kind):
                self.rewards.append(kind)
                return True

            def run(self):
                self.started.set()
                self.stop_event.wait(5.0)
                self.exited.set()

        bot_instance = RichFakeBot({"name": "alpha"})
        with tempfile.TemporaryDirectory() as tmp:
            farm = Farm(
                [{"name": "alpha"}],
                bot_factory=lambda acc: bot_instance,
                farm_root=Path(tmp),
            )
            srv = farm.start_dashboard(host="127.0.0.1", port=0)
            port = srv.server_address[1]
            farm.start()
            try:
                self.assertTrue(bot_instance.started.wait(2.0))
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    with farm._lock:
                        slot_status = farm._slots[0].status
                    if slot_status == "in_game":
                        break
                    time.sleep(0.02)
                self.assertEqual(farm.snapshot()["bots"][0]["status"], "in_game")

                self.assertTrue(farm.reward("lootbox", bot_name="alpha"))
                self.assertTrue(farm.reward("wheel"))
                self.assertEqual(bot_instance.rewards, ["lootbox", "wheel"])

                bot_instance.fail_store = True
                snap = farm.snapshot()["bots"][0]
                self.assertEqual(snap["name"], "alpha")
                self.assertIn(snap["status"], ("running", "in_game"))
            finally:
                farm.stop()

            self.assertTrue(bot_instance.exited.wait(2.0))
            import urllib.error
            import urllib.request
            with self.assertRaises((urllib.error.URLError, OSError)):
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.0):
                    pass

    def test_concurrent_add_remove_and_removed_slot_not_restarted(self):
        from wvbot.farm import Farm

        spawned: dict[str, int] = {}

        class ShortLivedBot:
            def __init__(self, account):
                self.name = account["name"]
                self.stop_event = threading.Event()
                self.pause_event = threading.Event()
                spawned[self.name] = spawned.get(self.name, 0) + 1

            def run(self):
                self.stop_event.wait(0.2)

        with tempfile.TemporaryDirectory() as tmp:
            farm = Farm(
                [{"name": "stay"}],
                bot_factory=lambda acc: ShortLivedBot(acc),
                max_bots=10,
                farm_root=Path(tmp),
            )
            farm.start()
            try:
                errors: list[Exception] = []

                def worker(idx: int):
                    try:
                        name = f"bot{idx}"
                        farm.add_account({"name": name})
                        time.sleep(0.02)
                        farm.snapshot()
                        farm.remove_account(name)
                    except Exception as exc:
                        errors.append(exc)

                threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=3.0)
                self.assertEqual(errors, [])

                names = [s["name"] for s in farm.snapshot()["bots"]]
                self.assertEqual(names, ["stay"])
            finally:
                farm.stop()


if __name__ == "__main__":
    unittest.main()

