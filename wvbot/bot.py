"""The bot itself: find a room, join it, play it, reconnect when kicked.

Run loop (this is the "auto reconnect" requirement):

    while not stopped:
        try:
            session()      # join one room and play until it ends / we are kicked
        except Exception:
            log the failure and back off
        sleep(backoff)    # backoff grows with consecutive failures

``session()`` returns for any of these reasons, all of which lead straight
back to joining a room again:

* the game ended (``game-over-awards-available``)
* the server closed the socket (room closed, kicked, or network drop)
* the room list had no matching room (wait and retry)
"""

from __future__ import annotations

import json
import logging
import os
import random
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from . import __version__
from .api import GameApi
from .auth import AuthManager
from .config import GAME_URL_ASIA, Config, state_path
from .dashboard import StatusStore, start_dashboard
from .game import GameState, Policy, parse_payload, xp_to_next_level
from .history import XpHistory
from .http_client import HttpClient
from .reflex import ReflexEngine
from .rewards import Rewards
from .sio import SocketIOClient
from .stream import StreamHub
from .updates import UpdateWatcher

log = logging.getLogger("wvbot.bot")

# Roles that act on the player the day vote landed on.
# The roles that kill somebody and the event they use.  The list comes
# from the events the official client emits; a role that is missing here
# simply does not act.
KILLER_ROLES = {
    "priest": "game-priest-kill-player",
    "vigilante": "game-vigilante-shoot",
    "gunner": "game-gunner-shoot-player",
    "marksman": "game-marksman-shoot",
    "witch": "game-witch-kill-player",
    "warden": "game-warden-kill",
    "jailer": "game-jailer-kill-player",
    "serial-killer": "game-serial-killer-vote",
}

# The roles that protect somebody.
PROTECTOR_ROLES = {
    "doctor": "game-doctor-protect-player",
    "bodyguard": "game-bodyguard-protect-player",
}

# The roles that revive a dead player.
REVIVER_ROLES = {
    "medium": "game-medium-revive-player",
}

# The roles that reveal or investigate somebody.
INVESTIGATOR_ROLES = {
    "seer": "game-seer-reveal-player",
    "aura-seer": "game-aura-seer-reveal-player",
    "detective": "game-detective-examine-player",
}


class Bot:
    def __init__(
        self,
        cfg: Config,
        account_dir: Path | None = None,
        start_web_dashboard: bool = True,
    ):
        self.cfg = cfg
        self.account_dir = account_dir
        self.start_web_dashboard = start_web_dashboard
        self.store = StatusStore()
        self.hub = StreamHub()
        self.http = HttpClient(
            timeout=cfg.request_timeout_sec,
            max_retries=cfg.max_retries,
            token_provider=self._refresh_cf_jwt,
        )
        self.auth = AuthManager(cfg, self.http, account_dir=account_dir)
        self.api = GameApi(cfg, self.http, self.auth.auth_headers)
        self.state = GameState()
        self.policy = Policy(cfg)
        self.device_id = self._load_device_id()
        self.stop_event = threading.Event()
        self._backoff = cfg.reconnect_delay_sec
        self._failures = 0
        # Set when the server bans us; the run loop waits it out.
        self._banned_until = 0.0
        self._banned = threading.Event()
        # Timestamps of the recent joins, used to slow down on our own.
        self._join_times: list[float] = []
        # The player id of the logged-in account, learned from the profile.
        self._own_player_id: Any = None
        # Set by the dashboard: pause the bot, or restart the session.
        self._paused = threading.Event()
        self._restart = threading.Event()
        # Set when the server ends the game session with an error.
        self._session_over = threading.Event()
        # The actions already taken in the current phase, so the bot
        # acts once and not on every repeated server event.
        self._acted_keys: set[str] = set()
        # The XP counters survive a restart.
        self.history = XpHistory(self._state(cfg.state_file.replace(".json", "_history.json")))
        # When the profile was read the last time.
        self._last_profile_read = 0.0
        # Watches the game for a patch that could break the protocol.
        self.updates = UpdateWatcher(cfg, self.http, self.auth.auth_headers())
        self._last_update_check = 0.0
        # The day the process re-executed itself the last time.  It
        # lives in the state dir: os.execv wipes the memory, and a
        # marker only in RAM would restart the bot in a tight loop.
        self._last_restart_day = self._load_restart_day()
        # Deterministic instant reflex engine & anti-bot check evasion
        self.reflex = ReflexEngine(cooldown_sec=getattr(cfg, "reflex_chat_cooldown_sec", 12.0))
        self._consecutive_games = 0

    def _state(self, name: str) -> Path:
        base = getattr(self, "account_dir", None)
        return state_path(name, base=base)

    # ------------------------------------------------------------------
    def _load_device_id(self) -> str:
        path = self._state("device_id.txt")
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = str(uuid.uuid4())
        path.write_text(value, encoding="utf-8")
        return value

    def _refresh_cf_jwt(self) -> None:
        self.auth.refresh_cf_jwt()
        self.api.auth_headers = self.auth.auth_headers

    def _room_probe(self, language: str = "en", host: str = "") -> Any:
        """The raw room list of a region, served through the dashboard.

        Only the bot holds a valid attestation, so the laptop asks the
        bot instead of the game server.  Read-only; nothing joins.
        """
        clean = "".join(c for c in str(language) if c.isalpha())[:8] or "en"
        base = GAME_URL_ASIA if host == "asia" else self.cfg.game_base_url
        url = f"{base}/api/public/game/custom?language={clean}"
        data = self.http.request("GET", url, headers=self.auth.auth_headers())
        if isinstance(data, dict):
            games = data.get("openGames") or data.get("games") or []
            return games if isinstance(games, list) else []
        return data if isinstance(data, list) else []

    def log_event(self, kind: str, message: str, **extra: Any) -> None:
        log.info("%s", message)
        self.store.add_event(kind, message, **extra)
        if hasattr(self, "hub") and self.hub is not None:
            try:
                self.hub.publish("event", {"kind": kind, "message": message, "ts": time.time(), **extra})
            except Exception:
                pass

    # ------------------------------------------------------------------
    # pacing
    # ------------------------------------------------------------------
    def _next_delay(self) -> float:
        """How long to wait before the next game.

        In turbo mode, the bot cycles rooms with minimal latency (1-2.5s)
        to maximize games and XP per hour (>= 3,000 XP/h target).
        """
        if getattr(self.cfg, "turbo_mode", False):
            low = max(0.2, getattr(self.cfg, "turbo_delay_min_sec", 1.0))
            high = max(low, getattr(self.cfg, "turbo_delay_max_sec", 2.5))
            return round(random.SystemRandom().uniform(low, high), 2)

        low = max(0.0, self.cfg.delay_min_sec)
        high = max(low, self.cfg.delay_max_sec)
        delay = random.SystemRandom().uniform(low, high)
        if self._joins_in_hour() >= self.cfg.joins_per_hour:
            delay += 300
            self.log_event(
                "lobby",
                f"{self._joins_in_hour()} joins in the last hour - waiting longer",
            )
        return delay

    def _joins_in_hour(self) -> int:
        """How many games were joined in the last hour."""
        cutoff = time.time() - 3600
        self._join_times = [stamp for stamp in self._join_times if stamp > cutoff]
        return len(self._join_times)

    def _note_join(self) -> None:
        self._join_times.append(time.time())
        self._joins_in_hour()

    def _skip_room(self) -> bool:
        """True when this matching room is left alone, as a human would.

        In turbo mode, skipping is disabled (0% skip) for maximum XP throughput.
        """
        if getattr(self.cfg, "turbo_mode", False):
            return False
        return random.SystemRandom().random() < self.cfg.room_skip_chance

    def _join_budget_left(self) -> bool:
        """False when the hourly join cap is used up this round.

        In turbo mode, custom room joins are unconstrained.
        """
        if getattr(self.cfg, "turbo_mode", False):
            return True
        left = self._joins_in_hour() < self.cfg.joins_per_hour
        if not left:
            self.log_event(
                "lobby",
                f"{self._joins_in_hour()} joins in the last hour - "
                "cap reached, waiting this round out",
            )
        return left

    # ------------------------------------------------------------------
    def _nightly_restart_due(self) -> bool:
        """True once a day at the configured local hour."""
        hour = self.cfg.daily_restart_hour
        if hour is None:
            return False
        if time.localtime().tm_hour != hour:
            return False
        today = time.strftime("%Y-%m-%d")
        return self._last_restart_day != today

    def _touch_heartbeat(self) -> None:
        """Prove the loop is alive, for the outside watchdog.

        The JobScheduler watchdog can only see the lock directory; a
        bot that hangs while holding it would sit there forever.  The
        heartbeat file is the difference between hung and paused.
        """
        try:
            self._state("heartbeat.txt").write_text(f"{time.time()}\n", encoding="utf-8")
        except OSError as exc:
            log.warning("could not write the heartbeat: %s", exc)

    def _load_restart_day(self) -> str:
        try:
            path = self._state("restart_day.txt")
            if path.is_file():
                return path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        return ""

    def _save_restart_day(self, day: str) -> None:
        try:
            self._state("restart_day.txt").write_text(day + "\n", encoding="utf-8")
        except OSError as exc:
            log.warning("could not record the restart day: %s", exc)

    def _reexec(self) -> None:
        """Replace the process with a fresh one, same command line.

        ``os.execv`` keeps the pid and the lock file valid, so the
        service runner and the watchdog never notice.  The restart day
        is recorded in the state dir BEFORE the exec - the fresh
        process reads it and does not restart again.  If the exec
        itself fails, the bot carries on: dying here would take the
        farm down until the watchdog notices.
        """
        today = time.strftime("%Y-%m-%d")
        self._save_restart_day(today)
        self._last_restart_day = today
        self.log_event("bot", "daily restart - replacing the process")
        self.store.update(status="restarting")
        try:
            os.execv(sys.executable, [sys.executable, *sys.argv])
        except OSError as exc:
            self.log_event("warn", f"the daily restart failed, carrying on: {exc}")
            self.store.update(status="running")

    def _pick_room(self, rooms: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Choose a room, preferring the fast-rotating 8-player ones.

        Vill-win rooms pay full XP regardless of size, but an 8-player
        room turns around in 30-60 seconds compared to 4-5 minutes for
        a 16-player room. Prioritizing 8-player rooms is essential to
        reaching >= 3,000 XP/hour.
        """
        if not rooms:
            return None

        def is_8p_room(room: dict[str, Any]) -> bool:
            name = GameApi._room_name(room).lower()
            max_p = 0
            for key in ("maxPlayerCount", "maxPlayers", "playerLimit"):
                if key in room:
                    try:
                        max_p = int(room[key] or 0)
                        break
                    except (TypeError, ValueError):
                        pass
            roles = room.get("roles")
            roles_count = len(roles) if isinstance(roles, list) else 0
            if 0 < max_p <= 8:
                return True
            if 0 < roles_count <= 8:
                return True
            if any(k in name for k in ("8p", "8 p", "8-p", " 8 ", "8player", "vw8", "vw 8", "say m", "ww say", "m only")):
                return True
            return False

        def is_tf_room(room: dict[str, Any]) -> bool:
            """Time Flies (TF) rooms shorten day/night timers drastically for rapid XP farming."""
            name = GameApi._room_name(room).lower()
            if any(k in name for k in (" tf", "-tf", "(tf)", "tf ", "time flies", "timeflies", "e tf", "|tf")):
                return True
            day_ms = room.get("dayDiscussionDurationInMs")
            if isinstance(day_ms, (int, float)) and 0 < day_ms <= 30000:
                return True
            return False

        # Prioritize instant fast-farm rooms: 'WW Say M' or 'TF' (Time Flies)
        fast_rooms = [
            r for r in rooms
            if any(k in GameApi._room_name(r).lower() for k in ("say m", "ww say", "m only"))
            or is_tf_room(r)
        ]
        if fast_rooms:
            # Sort by player count descending to join rooms closest to the start threshold (8-16 players)
            fast_rooms.sort(key=lambda r: int(r.get("playerCount") or 0), reverse=True)
            return fast_rooms[0]

        # In fast-farm mode, avoid falling back to slow casual rooms where matches drag on 10-15m
        # and human players roleplay or report bots for not playing standard games.
        if getattr(self.cfg, "strict_tf_only", False):
            return None

        if getattr(self.cfg, "strict_8p_only", False):
            rooms = [r for r in rooms if is_8p_room(r)]
            if not rooms:
                return None

        if len(rooms) == 1:
            return rooms[0]

        def xp(room: dict[str, Any]) -> int:
            value = room.get("regularXp")
            if isinstance(value, bool):
                return 1 if value else 0
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        best = max(xp(room) for room in rooms)
        if best <= 0:
            top = list(rooms)
        else:
            top = [room for room in rooms if xp(room) >= best * 0.8]

        def size(room: dict[str, Any]) -> int:
            try:
                return int(room.get("playerCount") or 0)
            except (TypeError, ValueError):
                return 99

        if getattr(self.cfg, "prefer_8p_rooms", True):
            eight_p = [r for r in top if is_8p_room(r)]
            if eight_p:
                top = eight_p

        if not self.cfg.prefer_small_rooms:
            return random.SystemRandom().choice(top)

        # In Wolvesville, 7-10 players means the room has hit the 8-player start threshold
        ready_to_start = [room for room in top if 7 <= size(room) <= 11]
        if ready_to_start:
            return random.SystemRandom().choice(ready_to_start)

        # In 8p rooms, 4-7 players means the room is about to start
        viable = [room for room in top if size(room) >= 4] or top
        smallest = min(size(room) for room in viable)
        small = [room for room in viable if size(room) <= smallest + 3]
        return random.SystemRandom().choice(small)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # remote control
    # ------------------------------------------------------------------
    def _control(self, action: str) -> bool:
        """Handle a control action from the dashboard."""
        action = action.strip().lower()
        if action == "pause":
            self._paused.set()
            self.log_event("bot", "paused from the dashboard")
            self.store.update(status="paused")
            return True
        if action == "resume":
            self._paused.clear()
            self.log_event("bot", "resumed from the dashboard")
            self.store.update(status="running")
            return True
        if action == "restart":
            self.log_event("bot", "restart requested from the dashboard")
            self._restart.set()
            return True
        return False

    def _reward(self, action: str) -> bool:
        """Handle a reward action from the dashboard."""
        action = action.strip().lower()
        if action not in ("daily", "wheel", "lootbox"):
            return False
        try:
            rewards = Rewards(self.cfg, self.http, self.auth.auth_headers())
            if action == "daily":
                self._claim_daily(rewards)
            elif action == "lootbox":
                self._open_loot_boxes(rewards)
            else:
                self._spin_wheel(rewards)
        except Exception as exc:  # noqa: BLE001
            self.log_event("warn", f"the reward request failed: {exc}")
            return False
        return True

    def run(self) -> None:
        if self.start_web_dashboard:
            start_dashboard(
                self.store,
                self.cfg.dashboard_host,
                self.cfg.dashboard_port,
                control=self._control,
                reward=self._reward,
                rooms=self._room_probe,
                hub=self.hub,
            )
        self.log_event("bot", f"bot starting (v{__version__})")
        self.store.update(version=__version__)
        self.history.record_session()
        self.store.update(history=self.history.get())
        try:
            self.auth.ensure_token()
            self.api.auth_headers = self.auth.auth_headers
            self.log_event("auth", "authenticated")
            self._read_profile()
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            self.store.update(status="error", last_error=str(exc), errors=1)
            self.log_event("error", f"initial auth failed: {exc}")

        while not self.stop_event.is_set():
            # The outside watchdog reads this file to tell a hung bot
            # from a working one.
            self._touch_heartbeat()
            # The profile is read from time to time, also while the bot
            # waits for a room: the account can be renamed and a new
            # ban can appear at any moment.
            self._refresh_profile()
            self._check_updates()

            # The server bans players that join too often.  Waiting it out
            # is the only way back in.
            wait = self._banned_until - time.time()
            if wait > 0:
                self.log_event("warn", f"banned for another {int(wait)}s")
                self.store.update(
                    status="banned",
                    banned_until=time.strftime(
                        "%H:%M:%S UTC", time.gmtime(self._banned_until)
                    ),
                )
                self.stop_event.wait(min(wait + 5, 7200))
                if self.stop_event.is_set():
                    break
                if time.time() < self._banned_until:
                    continue
                self._banned_until = 0.0
                self.store.update(banned_until=None)
            self._banned.clear()

            # A pause from the dashboard stops everything but keeps the
            # bot alive and reachable.
            if self._paused.is_set():
                self.store.update(status="paused")
                self.stop_event.wait(2.0)
                continue

            # A long 24/7 run stays fresh when the process replaces
            # itself once a day, between two games.
            if self._nightly_restart_due():
                self._reexec()

            try:
                played = self.session()
                self._failures = 0
                self._backoff = self.cfg.reconnect_delay_sec
                # Human bio-break / fatigue simulator: take a natural break
                # every N consecutive games to break robotic 24/7 signatures (bypassed in turbo_mode)
                if played and getattr(self.cfg, "bio_break_enabled", True) and not getattr(self.cfg, "turbo_mode", False):
                    self._consecutive_games += 1
                    target_games = getattr(self.cfg, "bio_break_interval_games", 12)
                    self.store.update(
                        consecutive_games=self._consecutive_games,
                        games_to_bio_break=max(0, target_games - self._consecutive_games),
                    )
                    if self._consecutive_games >= target_games:
                        self._consecutive_games = 0
                        break_sec = random.SystemRandom().uniform(
                            getattr(self.cfg, "bio_break_min_sec", 120.0),
                            getattr(self.cfg, "bio_break_max_sec", 240.0),
                        )
                        self.log_event("bio-break", f"taking a human bio-break ({int(break_sec)}s) after consecutive games")
                        self.store.update(status=f"bio-break ({int(break_sec)}s)", games_to_bio_break=target_games)
                        self.stop_event.wait(break_sec)
                        self.store.update(status="running")
                # Always pause between games: joining back to back is
                # what gets the account banned.
                delay = self._next_delay()
                self.log_event("lobby", f"next game in {int(delay)}s")
                self.stop_event.wait(delay)
                # The rewards are picked up between two games, when the
                # bot is not holding a room.
                self._collect_rewards()
                if not played:
                    continue
            except Exception as exc:  # noqa: BLE001 - the supervisor must not die
                self._failures += 1
                self.store.update(errors=self._failures, last_error=str(exc))
                self.log_event("error", f"session failed: {exc}")
                if "401" in str(exc):
                    try:
                        self.log_event("auth", "HTTP 401 received - forcing credential refresh")
                        self.auth.ensure_token(force=True)
                        self.api.auth_headers = self.auth.auth_headers
                    except Exception as auth_exc:  # noqa: BLE001
                        log.warning("forced token refresh failed: %s", auth_exc)
                self.stop_event.wait(self._backoff)
                self._backoff = min(self._backoff * 2, self.cfg.reconnect_delay_max_sec)

        self.log_event("bot", "bot stopped")

    # ------------------------------------------------------------------
    def session(self) -> bool:
        """Join one room and play until the game ends or we get kicked."""
        self.state.reset()
        self._clear_actions()
        self.store.update(status="looking for room", phase="lobby", room=None)
        try:
            self.auth.ensure_token()
            self.api.auth_headers = self.auth.auth_headers
        except Exception as exc:  # noqa: BLE001
            log.warning("could not ensure auth token before session: %s", exc)

        rooms = self.api.find_rooms(self.cfg.room_names)
        if not rooms:
            matching = self.api.last_matching
            locked = self.api.last_locked
            if matching and locked == matching:
                reason = (
                    f"the {matching} matching room(s) all need a password - "
                    "set allow_password_rooms and room_password in the config"
                )
            elif matching:
                reason = f"{matching} room(s) match, {locked} of them need a password"
            else:
                reason = f"no room named {self.cfg.room_names} in language={self.cfg.language}"
            self.log_event("lobby", reason)
            self.store.update(status="waiting for room")
            return False

        room = self._pick_room(rooms)
        if room is None:
            return False
        room_id = GameApi._room_id(room)
        room_name = GameApi._room_name(room)

        # The hourly cap is checked before the join: a longer wait
        # afterwards would still put one join too many in the window.
        if not self._join_budget_left():
            self.store.update(status="waiting for room")
            return False

        # A human does not take every room: this round is occasionally
        # left alone entirely.  The skipped room does not count as a
        # join for the hourly limit.
        if self._skip_room():
            self.log_event("lobby", f"leaving room '{room_name}' alone this round")
            self.store.update(status="waiting for room")
            return False

        self.log_event("lobby", f"joining room '{room_name}' (id={room_id})")
        self.store.update(status="joining", room=room_name)
        self._note_join()

        token = self.auth.ensure_token()
        self.api.auth_headers = self.auth.auth_headers

        # A custom game is entered directly: the game socket carries the
        # game id and the mode, there is no lobby step in between.
        self.state.game_id = room_id
        self.state.server_url = self.cfg.game_base_url
        self.log_event("game", f"game {room_id} on {self.cfg.game_base_url}")
        self.store.update(status="in game", game_id=str(room_id))

        return self._play_game(token, room_id, self.cfg.game_base_url)

    # ------------------------------------------------------------------
    def _socket_auth(self, token: str) -> dict[str, str]:
        auth = {"firebaseToken": token}
        if self.auth.cf_jwt:
            auth["Cf-JWT"] = self.auth.cf_jwt
        return auth

    @staticmethod
    def _nonce() -> int:
        """A random number between 42 and 99, like the official client."""
        return random.SystemRandom().randint(42, 99)

    def _create_game_socket(self, token: str, server_url: str) -> SocketIOClient:
        return SocketIOClient(
            server_url,
            query={
                "firebaseToken": token,
                "ids": 1,
                "apiV": 1,
                "b": self._nonce(),
                "deviceId": self.device_id,
            },
            auth=self._socket_auth(token),
            headers={"Origin": "https://app.wolvesville.com"},
        )

    def _play_game(self, token: str, game_id: Any, server_url: str) -> bool:
        self._session_over.clear()
        finished = threading.Event()
        disconnected = threading.Event()
        game_over = threading.Event()
        session_connected_at = time.time()
        last_event = [time.time()]
        last_heartbeat = [time.time()]
        last_game_over_time = [0.0]
        game_started_at = [0.0]
        active_sock = [None]

        def _process_game_award(current_sock: Any) -> None:
            award = self.state.last_award
            if not award:
                return
            xp = int(award.get("awardedTotalXp") or 0)
            levels = int(award.get("awardedLevels") or 0)
            stats = self.store.get()
            self.store.update(
                xp_total=int(stats.get("xp_total") or 0) + xp,
                xp_last=xp,
                games_played=int(stats.get("games_played") or 0) + 1,
            )
            self._read_profile()
            self.log_event("xp", f"+{xp} xp" + (f" (+{levels} levels)" if levels else ""))
            self.history.record_game(xp)
            self.store.update(history=self.history.get())
            if self.cfg.claim_double_xp and award.get("canClaimDoubleXp") and current_sock:
                self._claim_double_xp(current_sock)
            self.state.last_award = {}

        def handle(event: str, *args: Any) -> None:
            last_event[0] = time.time()
            if event == "game-started":
                last_game_over_time[0] = 0.0
                game_started_at[0] = time.time()
                self.store.update(status="playing")
            if time.time() - last_heartbeat[0] > 30.0:
                self._touch_heartbeat()
                last_heartbeat[0] = time.time()
            if active_sock[0]:
                self._on_game_event(active_sock[0], event, args)

        def on_disconnect() -> None:
            if not game_over.is_set():
                disconnected.set()
            finished.set()

        def on_awards(*args: Any) -> None:
            game_over.set()
            finished.set()

        def setup_socket(s: Any) -> bool:
            s.on_any(handle)
            s.on("disconnect", on_disconnect)
            s.on("game-over-awards-available", on_awards)
            try:
                s.start()
            except Exception as exc:  # noqa: BLE001
                self.log_event("error", f"game socket failed: {exc}")
                return False
            join_delay = getattr(self.cfg, "socket_join_delay_sec", 1.0)
            if join_delay > 0:
                time.sleep(join_delay)
            try:
                s.emit("mg:join-open-game", {"miniGameId": game_id})
            except Exception as exc:  # noqa: BLE001
                self.log_event("error", f"the join request failed: {exc}")
                s.close()
                return False
            return True

        sock = self._create_game_socket(token, server_url)
        active_sock[0] = sock
        if not setup_socket(sock):
            return False

        self.store.update(status="playing")
        reconnect_attempts = 0
        max_reconnects = (
            getattr(self.cfg, "fast_reconnect_max_attempts", 3)
            if getattr(self.cfg, "fast_reconnect_enabled", True)
            else 0
        )
        reconnect_delay = getattr(self.cfg, "fast_reconnect_delay_sec", 3.0)

        while not self.stop_event.is_set():
            if finished.wait(timeout=1.0):
                if game_over.is_set():
                    _process_game_award(active_sock[0])
                    start_time = game_started_at[0] if game_started_at[0] > 0 else session_connected_at
                    duration = max(0.0, time.time() - start_time)
                    stay_max = getattr(self.cfg, "stay_in_room_max_game_duration_sec", 150.0)
                    if getattr(self.cfg, "stay_in_room", False) and duration <= stay_max:
                        game_over.clear()
                        finished.clear()
                        self._clear_actions()
                        self.state.phase = "lobby"
                        self.store.update(status="staying in room", phase="lobby")
                        room_label = (
                            (self.store.get().get("room") or game_id)
                            if hasattr(self, "store")
                            else game_id
                        )
                        dur_text = f" trong {int(duration)}s (nhanh)" if duration > 0 else ""
                        self.log_event("lobby", f"cắm phòng '{room_label}': ván xong{dur_text}, giữ nguyên kết nối chờ ván tiếp...")
                        last_event[0] = time.time()
                        last_game_over_time[0] = time.time()
                        game_started_at[0] = 0.0
                        continue
                    else:
                        room_label = (
                            (self.store.get().get("room") or game_id)
                            if hasattr(self, "store")
                            else game_id
                        )
                        if duration > stay_max:
                            self.log_event("lobby", f"phòng '{room_label}' chậm (ván kéo dài {int(duration)}s > {int(stay_max)}s) - rời phòng tìm phòng TF/fast farm khác")
                        break

                if (
                    disconnected.is_set()
                    and not self._session_over.is_set()
                    and not self._banned.is_set()
                ):
                    disconnected.clear()
                    if (
                        reconnect_attempts < max_reconnects
                        and not self.stop_event.is_set()
                        and not self._restart.is_set()
                    ):
                        reconnect_attempts += 1
                        self.log_event(
                            "warn",
                            f"socket disconnected mid-game - fast rejoin attempt "
                            f"{reconnect_attempts}/{max_reconnects} to room {game_id}",
                        )
                        self.store.update(status="reconnecting")
                        self.store.update(
                            reconnects=int(self.store.get().get("reconnects") or 0) + 1
                        )
                        try:
                            if active_sock[0]:
                                active_sock[0].close()
                        except Exception:
                            pass

                        self.stop_event.wait(reconnect_delay)
                        if self.stop_event.is_set():
                            break

                        finished.clear()
                        try:
                            fresh_token = self.auth.ensure_token()
                        except Exception as auth_exc:  # noqa: BLE001
                            log.warning("token refresh during reconnect failed: %s", auth_exc)
                            fresh_token = token

                        new_sock = self._create_game_socket(fresh_token, server_url)
                        active_sock[0] = new_sock
                        if setup_socket(new_sock):
                            last_event[0] = time.time()
                            self.store.update(status="playing")
                            self.log_event("game", f"fast rejoin connected for room {game_id}")
                            continue
                        else:
                            self.log_event(
                                "warn",
                                f"fast rejoin setup failed on attempt {reconnect_attempts}",
                            )
                            if reconnect_attempts >= max_reconnects:
                                break
                            continue
                    else:
                        self.log_event("warn", "fast reconnect exhausted or disabled - leaving room")
                        break

            if self.stop_event.is_set() or self._restart.is_set():
                self._restart.clear()
                self.log_event("bot", "stop or restart request - leaving the room properly")
                break
            if self._banned.is_set():
                break
            if self._session_over.is_set():
                break
            if time.time() - last_heartbeat[0] > 30.0:
                self._touch_heartbeat()
                last_heartbeat[0] = time.time()
            if last_game_over_time[0] > 0.0:
                stay_timeout = getattr(self.cfg, "stay_in_room_timeout_sec", 60.0)
                if time.time() - last_game_over_time[0] > stay_timeout:
                    room_label = (
                        (self.store.get().get("room") or game_id)
                        if hasattr(self, "store")
                        else game_id
                    )
                    self.log_event("lobby", f"phòng '{room_label}' không bắt đầu ván mới sau {int(stay_timeout)}s - rời phòng tìm phòng khác")
                    break
            if time.time() - last_event[0] > self.cfg.silence_timeout_sec:
                self.log_event("warn", "no server event for a long time - restarting the game")
                break

        _process_game_award(active_sock[0])

        # Leaving the room has to be announced.  A socket that simply
        # disappears counts as "fleeing the village" and the server bans
        # the account for it.
        try:
            if active_sock[0]:
                active_sock[0].emit("mg:exit-game", {"miniGameId": game_id})
                self.log_event("game", "left the room")
        except Exception as exc:  # noqa: BLE001
            self.log_event("warn", f"the leave request failed: {exc}")
        time.sleep(1.0)
        if active_sock[0]:
            active_sock[0].close()

        self.store.update(status="reconnecting")
        self.store.update(reconnects=int(self.store.get().get("reconnects") or 0) + 1)
        return True

    def _claim_double_xp(self, sock: SocketIOClient) -> None:
        """Claim the doubler when the server offers it.

        The offer carries a cooldown of its own - most games answer
        with canClaimDoubleXp false and nothing is sent.  Whether the
        claim really doubled the award shows in the next profile read.
        """
        try:
            sock.emit("videoAdClaimReward", {"adId": "DOUBLE_XP", "type": "XP_DOUBLER"})
            sock.emit("game-over-double-xp")
            self.log_event("xp", "double XP claimed - the profile read confirms it")
        except Exception as exc:  # noqa: BLE001
            self.log_event("warn", f"double XP claim failed: {exc}")

    # ------------------------------------------------------------------
    def _on_game_event(self, sock: SocketIOClient, event: str, args: tuple) -> None:
        payload = args[0] if args else None
        if event == "game-started":
            self.state.apply(event, payload)
            self._find_own_id()
            self.store.update(
                role=self.state.role_name or self.state.role_id,
                players=len(self.state.players),
                phase=self.state.phase,
                levels=self._player_levels(),
            )
            self._update_alive()
            self._maybe_chat(sock)
            self._night_action(sock)
            self._killer_action(sock)
            self._protector_action(sock)
            self._reviver_action(sock)
            self._investigator_action(sock)
        elif event in ("game-night-started", "game-werewolves-vote-set"):
            self.state.apply(event, payload)
            self.store.update(phase=self.state.phase)
            # A new night clears the actions of the last one.
            if event == "game-night-started":
                self._clear_actions()
            self._night_action(sock)
            self._killer_action(sock)
            self._protector_action(sock)
            self._reviver_action(sock)
            self._investigator_action(sock)
        elif event in ("game-day-voting-started", "game-day-started"):
            self.state.apply(event, payload)
            self.store.update(phase=self.state.phase)
            self._clear_actions()
            self._wolf_self_claim(sock)
            if event == "game-day-voting-started":
                self._day_vote(sock)
            self._killer_action(sock)
        elif event == "game-day-vote-set":
            self.state.apply(event, payload)
            if self.state.phase == "day-voting" and not self.state.is_wolf:
                self._day_vote(sock, immediate=True)
            self._killer_action(sock)
        elif event in (
            "game-settings-changed",
            "game-players-killed",
            "game-werewolves-set-roles",
            "game-cupid-lover-ids-and-roles",
            "game-reconnect-set-players",
            "game:chat-public:msg",
            "player-joined-and-equipped-items",
            "players-and-equipped-items",
        ):
            self.state.apply(event, payload)
            if event == "game:chat-public:msg":
                if self.state.claimed_wolf_id:
                    if getattr(self.cfg, "instant_kill", True):
                        self._killer_action(sock, instant=True)
                    if self.state.phase == "day-voting" and not self.state.is_wolf:
                        self._day_vote(sock, immediate=True)
                if getattr(self.cfg, "reflex_chat_enabled", True):
                    self._handle_chat_reflex(sock, parse_payload(payload))
            if event in ("player-joined-and-equipped-items", "players-and-equipped-items"):
                self.store.update(levels=self._player_levels())
            if event in ("game-players-killed", "game-reconnect-set-players"):
                self._update_alive()
                if event == "game-reconnect-set-players":
                    self.log_event("game", "reconnected - player state synchronized")
        elif event == "game-over-awards-available":
            # The raw payload goes into the log once per game: it is the
            # ground truth a future feature (win counting) needs, and it
            # costs nothing to write it down.
            try:
                self.log_event(
                    "game",
                    "game-over payload: " + json.dumps(parse_payload(payload))[:1600],
                )
            except Exception:  # noqa: BLE001 - logging must not break play
                pass
            self.state.apply(event, payload)
            self.store.update(phase="game-over")
            award = self.state.last_award
            if self.cfg.claim_double_xp and award.get("canClaimDoubleXp"):
                self._claim_double_xp(sock)
            if getattr(self.cfg, "reflex_chat_enabled", True):
                self._handle_game_over_reflex(sock)
        elif event == "error-user-banned":
            # The server bans players that join too often or leave a
            # lobby during the countdown.  The bot has to wait it out.
            data = parse_payload(payload)
            reason = ""
            until = ""
            if isinstance(data, dict):
                reason = str(data.get("banReasonMsg") or data.get("banReason") or "")
                until = str(data.get("bannedUntilTime") or "")
            self._banned_until = _parse_ban_until(until)
            self.log_event(
                "warn",
                f"banned by the server: {reason or 'unknown reason'} "
                f"until {until or 'unknown'}".strip(),
            )
            self.store.update(status="banned", last_error=reason or "banned")
            # Do not touch the server again until the ban is over.
            self._banned.set()
        elif event in ("game-player-left", "game-over-player-left"):
            self.log_event("game", f"{event}")
        elif event.startswith("error-"):
            # The server refused something: the game was closed, we were
            # kicked, or the room vanished.  Either way the session is
            # over and a new room has to be found.  The session is ended
            # right away - waiting for the silence timeout would keep a
            # dead game open for minutes.
            self.log_event("warn", f"server error: {event} {str(payload)[:120]}")
            self.store.update(last_error=event)
            self._session_over.set()

    # -- actions -------------------------------------------------------
    def _collect_rewards(self) -> None:
        """Pick up the rewards between two games.

        The daily reward is free, so it is claimed.  The golden wheel
        costs roses and only spins when the config says so.
        """
        if not self.cfg.claim_daily_rewards and not self.cfg.spin_golden_wheel:
            return
        try:
            rewards = Rewards(self.cfg, self.http, self.auth.auth_headers())
            if self.cfg.claim_daily_rewards:
                self._claim_daily(rewards)
            if self.cfg.spin_golden_wheel:
                self._spin_wheel(rewards)
            self._open_loot_boxes(rewards)
        except Exception as exc:  # noqa: BLE001 - a reward must not stop the bot
            self.log_event("warn", f"the rewards failed: {exc}")

    def _open_loot_boxes(self, rewards: Rewards) -> None:
        """Open the loot boxes the game left behind.

        A loot box holds gold, XP or a role card, so it is worth
        opening as soon as it shows up in the inventory.
        """
        try:
            count = rewards.loot_box_count()
        except Exception as exc:  # noqa: BLE001
            log.debug("could not count the loot boxes: %s", exc)
            return
        if count <= 0:
            return
        try:
            items = rewards.open_loot_boxes()
        except Exception as exc:  # noqa: BLE001
            self.log_event("warn", f"the loot boxes did not open: {exc}")
            return
        self.log_event("reward", f"opened {count} loot box(es): {json.dumps(items)[:200]}")

    def _claim_daily(self, rewards: Rewards) -> None:
        ladder = rewards.daily_rewards()
        if not ladder:
            return
        # Only a reward whose date has arrived can be picked up.  The
        # ladder holds the whole month, so most of its entries are
        # still in the future.  The server refuses those, and the
        # claim would be repeated on every round.
        now = datetime.now(timezone.utc)
        due = [
            reward
            for reward in ladder
            if not reward.get("claimed") and self._reward_is_due(reward, now)
        ]
        if not due:
            return
        kind = str(due[0].get("type") or "reward")
        amount = due[0].get("amount")
        # One attempt per day per rung: a rung the server keeps
        # refusing must not be posted again on every round.
        if self._daily_claim_attempted(kind):
            return
        self._note_daily_claim_attempt(kind)
        try:
            result = rewards.claim_daily_reward()
        except Exception as exc:  # noqa: BLE001
            self.log_event("warn", f"the daily reward failed: {exc}")
            return
        # The answer decides what gets logged: a claim the server
        # refused must not read as a success.
        detail = json.dumps(result)[:200] if result else "server answered nothing"
        self.log_event("reward", f"daily reward {kind} x{amount} -> {detail}")

    def _daily_claim_attempted(self, kind: str) -> bool:
        """True when this rung was already tried today."""
        return self._load_claim_marker() == f"{_day_key_now()}|{kind}"

    def _note_daily_claim_attempt(self, kind: str) -> None:
        self._save_claim_marker(f"{_day_key_now()}|{kind}")

    def _load_claim_marker(self) -> str:
        try:
            return self._state("daily_claim.txt").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _save_claim_marker(self, value: str) -> None:
        try:
            self._state("daily_claim.txt").write_text(value + "\n", encoding="utf-8")
        except OSError as exc:
            log.warning("could not record the daily claim attempt: %s", exc)

    @staticmethod
    def _reward_is_due(reward: dict[str, Any], now: datetime) -> bool:
        """Is the date of this reward reached yet?

        An entry without a date is due right away.  A date the server
        sent in a shape that cannot be read is treated as due as well,
        so a broken answer never blocks the reward.
        """
        raw = reward.get("canBeClaimedDate")
        if not raw:
            return True
        try:
            moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return True
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment <= now

    def _spin_wheel(self, rewards: Rewards) -> None:
        conditions = rewards.golden_wheel_conditions()
        cost = int(conditions.get("costInRoses") or 0)
        if cost > self.cfg.wheel_max_roses:
            self.log_event(
                "reward",
                f"the wheel costs {cost} roses - over the limit, not spinning",
            )
            return
        state = rewards.golden_wheel_state()
        if not state.get("wheelSupported"):
            return
        try:
            won = rewards.spin_golden_wheel()
        except Exception as exc:  # noqa: BLE001
            self.log_event("warn", f"the wheel did not spin: {exc}")
            return
        if won:
            self.log_event("reward", f"the wheel gave: {json.dumps(won)[:200]}")

    def _projection(self) -> dict[str, Any]:
        """How fast the account levels up.

        The rate prefers the persistent day history: it survives the
        restarts and reflects the whole day, not just this process.  A
        day with no games yet falls back to the session counter.  The
        time to the next level comes from the XP the server says is
        missing.
        """
        stats = self.store.get()
        history = stats.get("history") or {}
        today_xp = int(history.get("today_xp") or 0)
        if today_xp > 0:
            # The day counters roll over at UTC midnight; the epoch is
            # aligned with it, so the elapsed part of the day is a
            # simple modulo.
            now = time.time()
            hours = max(0.25, (now % 86400.0) / 3600.0)
            rate = today_xp / hours
        else:
            uptime = max(1.0, float(stats.get("uptime_sec") or 1))
            farmed = int(stats.get("xp_total") or 0)
            rate = farmed / (uptime / 3600.0)

        level = stats.get("level")
        remaining = stats.get("xp_until_next")
        if remaining is None:
            remaining = xp_to_next_level(int(level or 1))
        try:
            remaining = int(remaining)
        except (TypeError, ValueError):
            remaining = 0

        out: dict[str, Any] = {"xp_per_hour": round(rate)}
        if remaining > 0 and rate > 0:
            out["hours_to_next_level"] = round(remaining / rate, 1)
        elif remaining > 0:
            out["hours_to_next_level"] = None
        return out

    def _update_alive(self) -> None:
        """How many players are still standing, for the dashboard."""
        self.store.update(alive=len(self.state.alive))

    def _player_levels(self) -> list[dict[str, Any]]:
        """The level of every player in the room, for the dashboard."""
        rows: list[dict[str, Any]] = []
        for player in self.state.players:
            rows.append(
                {
                    "name": player.username,
                    "level": player.level,
                    "alive": player.is_alive and player.id not in self.state.dead_ids,
                    "own": player.id == self.state.own_id(),
                }
            )
        rows.sort(key=lambda row: -int(row.get("level") or 0))
        return rows

    def _find_own_id(self) -> None:
        """Work out which player in the list is the bot.

        The player id comes from the profile; if that is not available,
        the player that carries the role the server handed us is us.
        """
        if self.state.own_id() is not None:
            return
        own = getattr(self, "_own_player_id", None)
        if not own:
            try:
                api = getattr(self, "api", None)
                if api and hasattr(api, "get_own_player_id"):
                    own = api.get_own_player_id(getattr(self, "device_id", ""))
                    self._own_player_id = own
            except Exception as exc:  # noqa: BLE001 - advisory only
                log.debug("could not read the player profile: %s", exc)
                own = None
        if own:
            self.state.set_own_id(own)
            return
        # Fall back: the player whose role equals the one the server
        # gave us is the bot.
        role = (self.state.role_id or "").lower()
        if not role:
            return
        for player in self.state.players:
            if (player.role or "").lower() == role:
                self.state.set_own_id(player.id)
                log.info("own player id: %s", player.id)
                return

    def _check_updates(self) -> None:
        """Notice a game patch.

        The reference bot promises to keep working after a patch.  The
        protocol here is re-implemented in this repository, so a patch
        means the official client changed and the notes have to be
        read again.  The bot only reports it.
        """
        now = time.time()
        if now - self._last_update_check < self.cfg.update_check_sec:
            return
        self._last_update_check = now
        try:
            result = self.updates.check()
        except Exception as exc:  # noqa: BLE001 - advisory only
            log.debug("the update check failed: %s", exc)
            return
        self.store.update(update=result)
        if result.get("build_changed"):
            self.log_event(
                "warn",
                "the game was patched: build "
                f"{result.get('previous_build')} -> {result.get('build_number')}",
            )
        if result.get("bundle_changed"):
            self.log_event(
                "warn",
                "the client bundle changed: "
                f"{result.get('previous_bundle')} -> {result.get('bundle_hash')}",
            )

    def _refresh_profile(self) -> None:
        """Read the profile now and then.

        The profile carries the name, the level and the ban, and it is
        the only way to notice a rename while the bot is waiting for a
        room.  It is not read on every loop, that would be a request
        every second.
        """
        now = time.time()
        if now - self._last_profile_read < self.cfg.profile_refresh_sec:
            return
        self._last_profile_read = now
        self._read_profile()

    def _read_profile(self) -> None:
        """Read the account profile.

        The profile carries the level, the total xp and - importantly -
        the time until the ban ends, so the bot knows when it may play
        again without having to try.
        """
        try:
            profile = self.api.get_profile()
        except Exception as exc:  # noqa: BLE001 - advisory only
            log.debug("could not read the profile: %s", exc)
            return
        if not profile:
            return
        stats = self.store.get()
        username = profile.get("username")
        # The account can be renamed at any time.  The bot follows the
        # new name instead of playing under the old one.
        previous = stats.get("username")
        if previous and username and previous != username:
            self.log_event("bot", f"the account was renamed: {previous} -> {username}")
        self.store.update(
            username=username,
            level=profile.get("level"),
            account_xp=profile.get("xpTotal"),
            games_finished=profile.get("finishedGamesCount"),
            xp_until_next=profile.get("xpUntilNextLevel"),
            projection=self._projection(),
        )
        banned_until = _parse_ban_until(str(profile.get("bannedUntilTime") or ""))
        if banned_until > time.time():
            self._banned_until = banned_until
            self.log_event(
                "warn",
                "the account is banned until "
                + time.strftime("%H:%M:%S UTC", time.gmtime(banned_until)),
            )
        log.info(
            "profile: %s level %s, %s xp",
            profile.get("username"),
            profile.get("level"),
            profile.get("xpTotal"),
        )

    # The official client sends every game payload as a JSON *string*;
    # only the ad reward takes a plain object.
    def _maybe_chat(self, sock: SocketIOClient) -> None:
        """A greeting now and then, like a human joining a room.

        Off unless the config asks for it: an unsolicited message can
        annoy a room, and a kick costs the same as a flee.
        """
        if not self.cfg.chat_enabled or not self.cfg.chat_messages:
            return
        if random.SystemRandom().random() > 0.3:
            return
        message = random.SystemRandom().choice(list(self.cfg.chat_messages))

        def speak() -> None:
            self.chat(sock, message)
            self.log_event("chat", f"said: {message}")

        self._queue_action("chat", 5.0, 25.0, speak)

    def _hesitate(self, low: float = 3.0, high: float = 12.0) -> None:
        """Wait a random moment before acting.

        Voting the instant the voting opens is a tell; a human takes a
        few seconds to read the chat first.
        """
        delay = random.SystemRandom().uniform(low, high)
        self.stop_event.wait(delay)

    def _queue_action(self, name: str, low: float, high: float, act: Any) -> None:
        """Run an action after a human pause - off the reader thread.

        Every game event arrives on the reader thread; a pause taken
        there would stop the answers to the engine pings and the
        server would drop the socket mid-game.  The pause therefore
        runs in its own short-lived worker.
        """
        threading.Thread(
            target=self._delayed_action,
            args=(name, low, high, act),
            daemon=True,
            name="wvbot-action",
        ).start()

    def _delayed_action(self, name: str, low: float, high: float, act: Any) -> None:
        self._hesitate(low, high)
        if self.stop_event.is_set():
            return
        try:
            act()
        except Exception as exc:  # noqa: BLE001 - an action must not kill anything
            self.log_event("warn", f"{name} failed: {exc}")

    def _night_action(self, sock: SocketIOClient) -> None:
        if not self.state.is_wolf:
            return
        if self._acted("werewolf-vote"):
            return
        is_vill_win = getattr(self.cfg, "vill_win_mode", True)
        target = self.policy.kill_target(self.state, vill_win_mode=is_vill_win)
        if target is None:
            return

        def vote() -> None:
            sock.emit("game-werewolves-vote-set", json.dumps({"targetPlayerId": target}))
            self.log_event("action", f"werewolf vote -> {target}")

        self._queue_action("werewolf vote", 3.0, 12.0, vote)

    def _wolf_self_claim(self, sock: SocketIOClient) -> None:
        """In vill-win rooms, if the bot is wolf, immediately claim seat number in chat."""
        if not getattr(self.cfg, "vill_win_mode", True) or not getattr(self.cfg, "wolf_self_claim", True):
            return
        if not self.state.is_wolf:
            return
        if hasattr(self.state, "by_id"):
            own = self.state.by_id(self.state.own_id())
            if own and not getattr(own, "is_alive", True):
                return
        if self._acted("wolf-self-claim"):
            return
        seat = self.state.own_seat()
        if seat is None:
            return
        self.state.claimed_wolf_id = self.state.own_id()
        self.state.claimed_wolf_seat = seat
        store = getattr(self, "store", None)
        room_name = (store.get().get("room") or "").lower() if store else ""
        if any(k in room_name for k in ("say m", "ww say", "m only")):
            claim_msg = "m"
        else:
            claim_msg = f"{seat} w"

        def speak() -> None:
            self.chat(sock, claim_msg)
            self.log_event("chat", f"vill-win wolf claim: {claim_msg}")

        delay = 0.3 if getattr(self.cfg, "turbo_mode", False) else 1.0
        self._queue_action("wolf claim", delay, delay + 0.5, speak)

    def _acted(self, key: str) -> bool:
        """True when this action was already taken in the current phase.

        The flag is cleared whenever a new phase starts, so the bot acts
        once per night and once per day.
        """
        if not hasattr(self, "_acted_keys"):
            self._acted_keys = set()
        if key in self._acted_keys:
            return True
        self._acted_keys.add(key)
        return False

    def _clear_actions(self) -> None:
        if hasattr(self, "_acted_keys"):
            self._acted_keys.clear()

    def _day_vote(self, sock: SocketIOClient, immediate: bool = False) -> None:
        if hasattr(self.state, "by_id"):
            own = self.state.by_id(self.state.own_id())
            if own and not getattr(own, "is_alive", True):
                return

        def do_vote() -> None:
            # Re-evaluate vote target right before emitting
            target = self.policy.vote_target(self.state)
            current_vote = self.state.day_votes.get(self.state.own_id())
            if target is not None:
                if current_vote != target:
                    sock.emit("game-day-vote-set", json.dumps({"targetPlayerId": target}))
                    self.state.day_votes[self.state.own_id()] = target
                    self.log_event("action", f"day vote -> {target}")
            elif not immediate and current_vote is None:
                # Only vote skip as a fallback if no target and no vote yet
                sock.emit("game-vote-to-skip")
                self.log_event("action", "day vote: skip")

        if immediate:
            do_vote()
            return

        if self._acted("day-vote"):
            return

        target = self.policy.vote_target(self.state)
        if target is not None:
            delay = 0.3 if getattr(self.cfg, "turbo_mode", False) else 1.5
            self._queue_action("day vote", delay, delay + 0.5, do_vote)
        else:
            delay = 4.0 if getattr(self.cfg, "turbo_mode", False) else 10.0
            self._queue_action("day vote skip fallback", delay, delay + 2.0, do_vote)

    def _killer_action(self, sock: SocketIOClient, instant: bool = False) -> None:
        event = KILLER_ROLES.get(self.state.role_id.lower())
        if not event or self._acted(event):
            return
        # One kill per day. In vill-win rooms, protect villagers by using safe mode
        room_names = getattr(self.cfg, "room_names", [])
        is_vill_win = getattr(self.cfg, "vill_win_mode", True)
        if not is_vill_win and room_names:
            if isinstance(room_names, str):
                is_vill_win = "vill" in room_names.lower() or "vw" in room_names.lower()
            else:
                is_vill_win = any("vill" in str(n).lower() or "vw" in str(n).lower() for n in room_names)
        target = self.policy.killer_target(self.state, safe_mode=is_vill_win)
        if target is None:
            self._acted_keys.discard(event)
            return

        def kill() -> None:
            sock.emit(event, json.dumps({"targetPlayerId": target}))
            self.log_event("action", f"{event} -> {target}")

        if instant or (getattr(self.cfg, "instant_kill", True) and getattr(self.state, "claimed_wolf_id", None) == target):
            # Instant reaction (0.2 - 0.5s) to end the game immediately on Day 1!
            self._queue_action(event, 0.2, 0.5, kill)
        else:
            delay = 0.8 if getattr(self.cfg, "turbo_mode", False) else 3.0
            self._queue_action(event, delay, delay + 2.0, kill)

    def _protector_action(self, sock: SocketIOClient) -> None:
        """The doctor and the bodyguard pick somebody to protect."""
        event = PROTECTOR_ROLES.get(self.state.role_id.lower())
        if not event or self._acted(event):
            return
        target = self.policy.protect_target(self.state)
        if target is None:
            return

        def protect() -> None:
            sock.emit(event, json.dumps({"targetPlayerId": target}))
            self.log_event("action", f"{event} -> {target}")

        self._queue_action(event, 3.0, 12.0, protect)

    def _reviver_action(self, sock: SocketIOClient) -> None:
        """The medium revives a fallen villager."""
        event = REVIVER_ROLES.get(self.state.role_id.lower())
        if not event or self._acted(event):
            return
        target = self.policy.revive_target(self.state)
        if target is None:
            return

        def revive() -> None:
            sock.emit(event, json.dumps({"targetPlayerId": target}))
            self.log_event("action", f"{event} -> {target}")

        self._queue_action(event, 3.0, 10.0, revive)

    def _investigator_action(self, sock: SocketIOClient) -> None:
        """The seer, aura seer, or detective reveals a player."""
        event = INVESTIGATOR_ROLES.get(self.state.role_id.lower())
        if not event or self._acted(event):
            return
        target = self.policy.reveal_target(self.state)
        if target is None:
            return

        def reveal() -> None:
            sock.emit(event, json.dumps({"targetPlayerId": target}))
            self.log_event("action", f"{event} -> {target}")

        self._queue_action(event, 2.5, 8.0, reveal)

    def _handle_chat_reflex(self, sock: SocketIOClient, data: Any) -> None:
        if not hasattr(self, "reflex") or not isinstance(data, dict):
            return
        msg = data.get("msg") or ""
        author_id = data.get("authorId")
        username = self.store.get().get("username") or ""
        reply = self.reflex.decide_chat_response(
            msg=msg,
            author_id=author_id,
            state=self.state,
            own_username=username,
        )
        if not reply:
            return
        delay = self.reflex.calculate_typing_delay(reply)
        self.reflex.note_reply(reply)

        def speak() -> None:
            self.chat(sock, reply)
            self.log_event("chat", f"reflex said: {reply}")
            try:
                stats = self.store.get()
                self.store.update(reflex_count=int(stats.get("reflex_count") or 0) + 1)
            except Exception:  # noqa: BLE001
                pass

        self._queue_action("reflex chat", delay * 0.8, delay * 1.2, speak)

    def _handle_game_over_reflex(self, sock: SocketIOClient) -> None:
        if not hasattr(self, "reflex"):
            return
        gg = self.reflex.decide_game_over_response()
        if not gg:
            return
        self.reflex.note_reply(gg)

        def say_gg() -> None:
            self.chat(sock, gg)
            self.log_event("chat", f"reflex said: {gg}")
            try:
                stats = self.store.get()
                self.store.update(reflex_count=int(stats.get("reflex_count") or 0) + 1)
            except Exception:  # noqa: BLE001
                pass

        self._queue_action("reflex gg", 1.5, 3.5, say_gg)

    # ------------------------------------------------------------------
    def chat(self, sock: SocketIOClient, message: str) -> None:
        allowed = self.cfg.chat_enabled or getattr(self.cfg, "reflex_chat_enabled", False)
        if not allowed or not message:
            return
        try:
            sock.emit("game:chat-public:msg", json.dumps({"msg": message}))
        except Exception:  # noqa: BLE001
            pass


def _day_key_now() -> str:
    """The local day, used for the once-a-day claim marker."""
    return time.strftime("%Y-%m-%d")


def _parse_ban_until(value: str) -> float:
    """Turn the server's ban timestamp into a local epoch value.

    The server sends an ISO timestamp in UTC.  A value we cannot read
    means "no known end", and the caller then uses its own backoff.
    """
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        from datetime import datetime, timezone

        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.timestamp()
    except Exception:  # noqa: BLE001 - advisory only
        return 0.0


def _extract_game_info(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    info: dict[str, Any] = {}
    for key in ("gameId", "id"):
        if key in data and data[key] is not None:
            info["gameId"] = data[key]
            break
    for key in ("serverUrl", "server", "url"):
        if key in data and isinstance(data[key], str):
            info["serverUrl"] = data[key]
            break

    values = list(data.values())
    if "gameId" not in info and values:
        info["gameId"] = values[0]
    if "serverUrl" not in info and len(values) > 1:
        candidate = values[1]
        if isinstance(candidate, str) and candidate.startswith("http"):
            info["serverUrl"] = candidate
    return info
