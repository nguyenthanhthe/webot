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
import random
import threading
import time
import uuid
from typing import Any

from .api import GameApi
from .auth import AuthManager
from .config import Config, state_path
from .dashboard import StatusStore, start_dashboard
from .game import GameState, Policy, parse_payload
from .http_client import HttpClient
from .sio import SocketIOClient

log = logging.getLogger("wvbot.bot")

# Roles that act on the player the day vote landed on.
KILLER_ROLES = {
    "priest": "game-priest-kill-player",
    "vigilante": "game-vigilante-shoot",
    "gunner": "game-gunner-shoot-player",
}


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = StatusStore()
        self.http = HttpClient(
            timeout=cfg.request_timeout_sec,
            max_retries=cfg.max_retries,
            token_provider=self._refresh_cf_jwt,
        )
        self.auth = AuthManager(cfg, self.http)
        self.api = GameApi(cfg, self.http, self.auth.auth_headers())
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

    # ------------------------------------------------------------------
    def _load_device_id(self) -> str:
        path = state_path("device_id.txt")
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = str(uuid.uuid4())
        path.write_text(value, encoding="utf-8")
        return value

    def _refresh_cf_jwt(self) -> None:
        self.auth.refresh_cf_jwt()
        self.api.auth_headers = self.auth.auth_headers()

    def log_event(self, kind: str, message: str, **extra: Any) -> None:
        log.info("%s", message)
        self.store.add_event(kind, message, **extra)

    # ------------------------------------------------------------------
    # pacing
    # ------------------------------------------------------------------
    def _next_delay(self) -> float:
        """How long to wait before the next game.

        A flat delay is a fingerprint in itself, so the wait is drawn
        from a range.  When the bot has joined a lot of games in the last
        hour it waits longer on top of that.
        """
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

    # ------------------------------------------------------------------
    def run(self) -> None:
        start_dashboard(self.store, self.cfg.dashboard_host, self.cfg.dashboard_port)
        self.log_event("bot", "bot starting")
        try:
            self.auth.ensure_token()
            self.api.auth_headers = self.auth.auth_headers()
            self.log_event("auth", "authenticated")
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            self.store.update(status="error", last_error=str(exc), errors=1)
            self.log_event("error", f"initial auth failed: {exc}")

        while not self.stop_event.is_set():
            # The server bans players that join too often.  Waiting it out
            # is the only way back in.
            wait = self._banned_until - time.time()
            if wait > 0:
                self.log_event("warn", f"banned for another {int(wait)}s")
                self.store.update(status="banned")
                self.stop_event.wait(min(wait + 5, 7200))
                if self.stop_event.is_set():
                    break
                self._banned_until = 0.0
            self._banned.clear()

            try:
                played = self.session()
                self._failures = 0
                self._backoff = self.cfg.reconnect_delay_sec
                # Always pause between games: joining back to back is
                # what gets the account banned.
                delay = self._next_delay()
                self.log_event("lobby", f"next game in {int(delay)}s")
                self.stop_event.wait(delay)
                if not played:
                    continue
            except Exception as exc:  # noqa: BLE001 - the supervisor must not die
                self._failures += 1
                self.store.update(errors=self._failures, last_error=str(exc))
                self.log_event("error", f"session failed: {exc}")
                self.stop_event.wait(self._backoff)
                self._backoff = min(self._backoff * 2, self.cfg.reconnect_delay_max_sec)

        self.log_event("bot", "bot stopped")

    # ------------------------------------------------------------------
    def session(self) -> bool:
        """Join one room and play until the game ends or we get kicked."""
        self.state.reset()
        self.store.update(status="looking for room", phase="lobby", room=None)

        rooms = self.api.find_rooms(self.cfg.room_names)
        if not rooms:
            self.log_event(
                "lobby",
                f"no room named {self.cfg.room_names} in language={self.cfg.language}",
            )
            self.store.update(status="waiting for room")
            return False

        room = random.SystemRandom().choice(rooms)
        room_id = GameApi._room_id(room)
        room_name = GameApi._room_name(room)
        self.log_event("lobby", f"joining room '{room_name}' (id={room_id})")
        self.store.update(status="joining", room=room_name)
        self._note_join()

        token = self.auth.ensure_token()
        self.api.auth_headers = self.auth.auth_headers()

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

    def _play_game(self, token: str, game_id: Any, server_url: str) -> bool:
        # The official client puts the firebase token in the handshake
        # auth and the game id in the query.
        sock = SocketIOClient(
            server_url,
            query={
                "gameId": game_id,
                "gameMode": self.cfg.game_mode,
                "reconnect": "true",
                "ids": 1,
                "apiV": 1,
                "deviceId": self.device_id,
            },
            auth=self._socket_auth(token),
            headers={"Origin": "https://app.wolvesville.com"},
        )

        finished = threading.Event()

        def handle(event: str, *args: Any) -> None:
            self._on_game_event(sock, event, args)

        sock.on_any(handle)
        sock.on("disconnect", lambda *a: finished.set())
        sock.on("game-over-awards-available", lambda *a: finished.set())

        try:
            sock.start()
        except Exception as exc:  # noqa: BLE001
            self.log_event("error", f"game socket failed: {exc}")
            return False

        self.store.update(status="playing")
        # Stop as soon as the server bans us: touching it again would
        # only make the ban longer.
        while not finished.wait(timeout=1.0):
            if self._banned.is_set():
                break
        sock.close()

        award = self.state.last_award
        if award:
            xp = int(award.get("awardedTotalXp") or 0)
            levels = int(award.get("awardedLevels") or 0)
            stats = self.store.get()
            self.store.update(
                xp_total=int(stats.get("xp_total") or 0) + xp,
                xp_last=xp,
                games_played=int(stats.get("games_played") or 0) + 1,
            )
            self.log_event("xp", f"+{xp} xp" + (f" (+{levels} levels)" if levels else ""))
            if self.cfg.claim_double_xp and award.get("canClaimDoubleXp"):
                self._claim_double_xp(sock)

        self.store.update(status="reconnecting")
        self.store.update(reconnects=int(self.store.get().get("reconnects") or 0) + 1)
        return True

    def _claim_double_xp(self, sock: SocketIOClient) -> None:
        try:
            sock.emit("videoAdClaimReward", {"adId": "DOUBLE_XP", "type": "XP_DOUBLER"})
            sock.emit("game-over-double-xp")
            self.log_event("xp", "claimed double XP")
        except Exception as exc:  # noqa: BLE001
            self.log_event("warn", f"double XP claim failed: {exc}")

    # ------------------------------------------------------------------
    def _on_game_event(self, sock: SocketIOClient, event: str, args: tuple) -> None:
        payload = args[0] if args else None
        if event == "game-started":
            self.state.apply(event, payload)
            self.store.update(
                role=self.state.role_name or self.state.role_id,
                players=len(self.state.players),
                phase=self.state.phase,
            )
            self._night_action(sock)
        elif event in ("game-night-started", "game-werewolves-vote-set"):
            self.state.apply(event, payload)
            self._night_action(sock)
        elif event == "game-day-voting-started":
            self.state.apply(event, payload)
            self._day_vote(sock)
        elif event == "game-day-vote-set":
            self.state.apply(event, payload)
            self._killer_action(sock)
        elif event in (
            "game-settings-changed",
            "game-players-killed",
            "game-werewolves-set-roles",
            "game-cupid-lover-ids-and-roles",
            "game-reconnect-set-players",
            "game:chat-public:msg",
        ):
            self.state.apply(event, payload)
        elif event == "game-over-awards-available":
            self.state.apply(event, payload)
            self.store.update(phase="game-over")
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

    # -- actions -------------------------------------------------------
    # The official client sends every game payload as a JSON *string*;
    # only the ad reward takes a plain object.
    def _hesitate(self, low: float = 3.0, high: float = 12.0) -> None:
        """Wait a random moment before acting.

        Voting the instant the voting opens is a tell; a human takes a
        few seconds to read the chat first.
        """
        delay = random.SystemRandom().uniform(low, high)
        self.stop_event.wait(delay)

    def _night_action(self, sock: SocketIOClient) -> None:
        if not self.state.is_wolf:
            return
        target = self.policy.kill_target(self.state)
        if target is None:
            return
        self._hesitate()
        try:
            sock.emit("game-werewolves-vote-set", json.dumps({"targetPlayerId": target}))
            self.log_event("action", f"werewolf vote -> {target}")
        except Exception as exc:  # noqa: BLE001
            self.log_event("warn", f"werewolf vote failed: {exc}")

    def _day_vote(self, sock: SocketIOClient) -> None:
        target = self.policy.vote_target(self.state)
        self._hesitate()
        try:
            if target is None:
                sock.emit("game-vote-to-skip", json.dumps({}))
                self.log_event("action", "day vote: skip")
            else:
                sock.emit("game-day-vote-set", json.dumps({"targetPlayerId": target}))
                self.log_event("action", f"day vote -> {target}")
        except Exception as exc:  # noqa: BLE001
            self.log_event("warn", f"day vote failed: {exc}")

    def _killer_action(self, sock: SocketIOClient) -> None:
        event = KILLER_ROLES.get(self.state.role_id.lower())
        if not event:
            return
        data = self.state.day_votes
        target = None
        for value in data.values():
            target = value
        if target is None:
            return
        try:
            sock.emit(event, json.dumps({"targetPlayerId": target}))
            self.log_event("action", f"{event} -> {target}")
        except Exception as exc:  # noqa: BLE001
            self.log_event("warn", f"{event} failed: {exc}")

    # ------------------------------------------------------------------
    def chat(self, sock: SocketIOClient, message: str) -> None:
        if not self.cfg.chat_enabled or not message:
            return
        try:
            sock.emit("game:chat-public:msg", {"msg": message})
        except Exception:  # noqa: BLE001
            pass


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
