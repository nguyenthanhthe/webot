"""The farm: several accounts, one service, one supervisor.

Phase 1 of the enterprise plan (docs/ENTERPRISE-PLAN.md).  The Farm
owns a roster of accounts and runs one bot thread per account; a bot
that dies is restarted with the same backoff the single bot uses, and
one that keeps dying is parked as "failed" instead of burning joins.

The roster lives in ``accounts.json`` in the farm root:

    {"accounts": [{"name": "main", "refresh_token": "..."}]}

An absent roster falls back to the classic single-account setup, so
the existing deployment keeps working untouched.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("wvbot.farm")

ROSTER_FILE = "accounts.json"
DEFAULT_MAX_BOTS = 4
# A bot that died this often in a row is parked instead of restarted.
MAX_CONSECUTIVE_CRASHES = 3


def clean_name(value: str) -> str:
    """A roster name is a directory name and a URL path segment."""
    kept = "".join(c if (c.isalnum() or c in "-_") else "" for c in str(value))
    return kept[:24]


class RosterError(ValueError):
    pass


def load_roster(farm_root: Path) -> list[dict[str, Any]]:
    """Read the roster, or fall back to the single classic account.

    Every account carries a clean unique name and whatever credentials
    it needs; unknown fields are kept for the bot factory.
    """
    path = farm_root / ROSTER_FILE
    if not path.is_file():
        return [{"name": "main"}]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RosterError(f"the roster is unreadable: {exc}") from exc
    accounts = raw.get("accounts") if isinstance(raw, dict) else None
    if not isinstance(accounts, list) or not accounts:
        raise RosterError("the roster holds no accounts")
    seen: set[str] = set()
    roster: list[dict[str, Any]] = []
    for entry in accounts:
        if not isinstance(entry, dict):
            raise RosterError("an account entry is not an object")
        name = clean_name(str(entry.get("name") or ""))
        if not name:
            raise RosterError("an account has no usable name")
        if name in seen:
            raise RosterError(f"duplicate account name: {name}")
        seen.add(name)
        account = dict(entry)
        account["name"] = name
        roster.append(account)
    return roster


class _Slot:
    """One supervised bot: its thread, its state, its crash count."""

    def __init__(self, account: dict[str, Any]):
        self.account = account
        self.name = str(account.get("name") or "?")
        self.bot: Any = None
        self.thread: threading.Thread | None = None
        self.status = "idle"
        self.crashes = 0
        self.started_at = 0.0


class Farm:
    """Runs one bot thread per account and keeps them alive.

    ``bot_factory(account) -> bot`` builds the real bot (the tests pass
    a fake).  A bot must offer ``run()``, ``stop_event`` and ``store``;
    the farm never touches anything else, so the contract stays small.
    """

    def __init__(
        self,
        roster: list[dict[str, Any]],
        bot_factory: Callable[[dict[str, Any]], Any],
        max_bots: int = DEFAULT_MAX_BOTS,
    ):
        self.bot_factory = bot_factory
        self.max_bots = max(1, int(max_bots))
        self.stop_event = threading.Event()
        self._slots: list[_Slot] = []
        for account in roster[: self.max_bots]:
            self._slots.append(_Slot(account))
        self._supervisor: threading.Thread | None = None

    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """What the dashboard shows: every bot at a glance.

        The bot's own status (playing, hunting, banned) is the signal;
        the farm's lifecycle status (crashed, failed) wins when a bot
        is no longer running at all.
        """
        bots = []
        for slot in self._slots:
            data: dict[str, Any] = {"name": slot.name, "status": slot.status, "crashes": slot.crashes}
            if slot.bot is not None:
                try:
                    data.update(slot.bot.store.get())
                except Exception:  # noqa: BLE001 - a dead store must not hide the bot
                    pass
            if slot.status.startswith("crashed") or slot.status in ("failed", "stopped"):
                data["status"] = slot.status
            bots.append(data)
        return {
            "bots": bots,
            "playing": sum(1 for b in bots if b.get("status") == "playing"),
            "total_xp": sum(int(b.get("xp_total") or 0) for b in bots),
            "games": sum(int(b.get("games_played") or 0) for b in bots),
            "errors": sum(int(b.get("errors") or 0) for b in bots),
        }

    def stop(self) -> None:
        self.stop_event.set()
        for slot in self._slots:
            if slot.bot is not None:
                slot.bot.stop_event.set()

    # ------------------------------------------------------------------
    def start(self) -> None:
        for slot in self._slots:
            self._launch(slot)
        self._supervisor = threading.Thread(
            target=self._supervise, name="farm-supervisor", daemon=True
        )
        self._supervisor.start()

    def _launch(self, slot: _Slot) -> None:
        try:
            slot.bot = self.bot_factory(slot.account)
        except Exception as exc:  # noqa: BLE001 - a bad account is a finding
            slot.status = "failed"
            log.warning("bot %s could not be built: %s", slot.name, exc)
            return
        slot.bot.stop_event.clear()
        slot.thread = threading.Thread(
            target=self._run_bot, args=(slot,), daemon=True, name=f"bot-{slot.name}"
        )
        slot.status = "starting"
        slot.started_at = time.time()
        slot.thread.start()

    def _run_bot(self, slot: _Slot) -> None:
        assert slot.bot is not None
        try:
            slot.bot.run()
            slot.status = "stopped"
        except Exception as exc:  # noqa: BLE001 - the farm survives anything
            slot.crashes += 1
            log.exception("bot %s crashed: %s", slot.name, exc)
            slot.status = f"crashed: {exc}"
            slot.bot = None

    def _supervise(self) -> None:
        """Restart dead bots; park the ones that keep dying.

        A bot whose thread ended while the farm is running has died -
        whether it walked out cleanly or blew up makes no difference
        to the farm; only the crash counter tells them apart.
        """
        while not self.stop_event.is_set():
            for slot in self._slots:
                if slot.status == "failed":
                    continue
                if slot.thread is not None and slot.thread.is_alive():
                    if slot.bot is not None and slot.status != "starting":
                        slot.status = "running"
                    continue
                if slot.crashes >= MAX_CONSECUTIVE_CRASHES:
                    log.warning("bot %s is parked after %d crashes", slot.name, slot.crashes)
                    slot.status = "failed"
                    continue
                log.warning("bot %s is gone - restarting it (crashes: %d)", slot.name, slot.crashes)
                self._launch(slot)
            self.stop_event.wait(0.5)
