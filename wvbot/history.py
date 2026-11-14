"""The XP history of the account.

The bot restarts - after a crash, a reboot or a manual restart - and
the dashboard would start counting from zero every time.  The counters
live in a small file in the state directory instead, so the numbers
keep growing.

The file holds the totals of the current day and of all time.  A new
day starts at midnight UTC, which is when the game resets its own
daily counters.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("wvbot.history")

STATE_FILE = "xp_history.json"


def _day_key(now: float | None = None) -> str:
    """The UTC day the given moment belongs to."""
    moment = time.gmtime(now if now is not None else time.time())
    return time.strftime("%Y-%m-%d", moment)


class XpHistory:
    """The XP the account collected, kept across restarts."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.data: dict[str, Any] = self._load()

    # ------------------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        empty: dict[str, Any] = {
            "day": _day_key(),
            "today_xp": 0,
            "today_games": 0,
            "total_xp": 0,
            "total_games": 0,
            "sessions": 0,
        }
        try:
            if not self.path.is_file():
                return empty
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.debug("could not read the xp history: %s", exc)
            return empty
        if not isinstance(raw, dict):
            return empty
        data = dict(empty)
        for key in empty:
            if key in raw:
                data[key] = raw[key]
        # A new day starts the daily counters from zero.
        if data.get("day") != _day_key():
            data["day"] = _day_key()
            data["today_xp"] = 0
            data["today_games"] = 0
        return data

    def _rollover_locked(self) -> bool:
        today = _day_key()
        if self.data.get("day") != today:
            self.data["day"] = today
            self.data["today_xp"] = 0
            self.data["today_games"] = 0
            return True
        return False

    def _save_locked(self) -> None:
        tmp_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_name(
                f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            tmp_path.write_text(
                json.dumps(self.data, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp_path, self.path)
            tmp_path = None
        except OSError as exc:
            log.debug("could not write the xp history: %s", exc)
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    # ------------------------------------------------------------------
    def record_game(self, xp: int, games: int = 1) -> None:
        """Add the XP of a finished game."""
        with self._lock:
            self._rollover_locked()
            self.data["today_xp"] = int(self.data.get("today_xp") or 0) + int(xp)
            self.data["today_games"] = int(self.data.get("today_games") or 0) + games
            self.data["total_xp"] = int(self.data.get("total_xp") or 0) + int(xp)
            self.data["total_games"] = int(self.data.get("total_games") or 0) + games
            self._save_locked()

    def record_session(self) -> None:
        """Count the start of a new bot session."""
        with self._lock:
            self._rollover_locked()
            self.data["sessions"] = int(self.data.get("sessions") or 0) + 1
            self._save_locked()

    def get(self) -> dict[str, Any]:
        with self._lock:
            if self._rollover_locked():
                self._save_locked()
            return dict(self.data)
