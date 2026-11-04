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
from .game import GameState, Policy
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
            try:
                played = self.session()
                self._failures = 0
                self._backoff = self.cfg.reconnect_delay_sec
                if not played:
                    self.stop_event.wait(self.cfg.reconnect_delay_sec)
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

        token = self.auth.ensure_token()
        self.api.auth_headers = self.auth.auth_headers()

        lobby = SocketIOClient(
            self.cfg.game_base_url,
            query={"ids": 1, "apiV": 1},
            auth=self._socket_auth(token),
            headers={"Origin": "https://app.wolvesville.com"},
        )
        game_info: dict[str, Any] = {}
        joined = threading.Event()

        def on_games_open(data: Any) -> None:
            log.debug("mg:games-open %s", str(data)[:200])

        def on_running_game(data: Any) -> None:
            info = _extract_game_info(data)
            if info:
                game_info.update(info)
                joined.set()

        def on_game_joined(data: Any) -> None:
            info = _extract_game_info(data)
            if info:
                game_info.update(info)
                joined.set()

        lobby.on("mg:games-open", on_games_open)
        lobby.on("mg:running-game", on_running_game)
        lobby.on("game-joined", on_game_joined)
        lobby.on("mg:game-running", on_running_game)

        try:
            lobby.start()
            lobby.emit("mg:join-open-game", {"miniGameId": room_id})

            if not joined.wait(timeout=30.0):
                self.log_event("lobby", "no game-joined within 30s; will retry")
                return False

            game_id = game_info.get("gameId")
            server_url = game_info.get("serverUrl") or self.cfg.game_base_url
            self.state.game_id = game_id
            self.state.server_url = server_url
            self.log_event("game", f"game {game_id} on {server_url}")
            self.store.update(status="in game", game_id=str(game_id))

            return self._play_game(token, game_id, server_url)
        finally:
            lobby.close()

    # ------------------------------------------------------------------
    def _socket_auth(self, token: str) -> dict[str, str]:
        auth = {"firebaseToken": token}
        if self.auth.cf_jwt:
            auth["Cf-JWT"] = self.auth.cf_jwt
        return auth

    def _play_game(self, token: str, game_id: Any, server_url: str) -> bool:
        sock = SocketIOClient(
            server_url,
            query={
                "firebaseToken": token,
                "gameId": game_id,
                "reconnect": "true",
                "gameMode": self.cfg.game_mode,
                "randomAvatarSlot": "false",
                "dodgeBlockedPlayers": "false",
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
        finished.wait()
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
        elif event in ("game-player-left", "game-over-player-left"):
            self.log_event("game", f"{event}")

    # -- actions -------------------------------------------------------
    def _night_action(self, sock: SocketIOClient) -> None:
        if not self.state.is_wolf:
            return
        target = self.policy.kill_target(self.state)
        if target is None:
            return
        try:
            sock.emit("game-werewolves-vote-set", {"targetPlayerId": target})
            self.log_event("action", f"werewolf vote -> {target}")
        except Exception as exc:  # noqa: BLE001
            self.log_event("warn", f"werewolf vote failed: {exc}")

    def _day_vote(self, sock: SocketIOClient) -> None:
        target = self.policy.vote_target(self.state)
        try:
            if target is None:
                sock.emit("game-vote-to-skip")
                self.log_event("action", "day vote: skip")
            else:
                sock.emit("game-day-vote-set", {"targetPlayerId": target})
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
            sock.emit(event, {"targetPlayerId": target})
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


def _extract_game_info(data: Any) -> dict[str, Any]:
    """``game-joined`` carries the game id and the game server URL.

    The official client reads them positionally from the object's values;
    we accept both the named and the positional form.
    """
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
