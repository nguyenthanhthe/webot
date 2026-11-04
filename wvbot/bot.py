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
        # The player id of the logged-in account, learned from the profile.
        self._own_player_id: Any = None
        # Set by the dashboard: pause the bot, or restart the session.
        self._paused = threading.Event()
        self._restart = threading.Event()
        # Set when the server ends the game session with an error.
        self._session_over = threading.Event()

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

    def run(self) -> None:
        start_dashboard(
            self.store,
            self.cfg.dashboard_host,
            self.cfg.dashboard_port,
            control=self._control,
        )
        self.log_event("bot", "bot starting")
        try:
            self.auth.ensure_token()
            self.api.auth_headers = self.auth.auth_headers()
            self.log_event("auth", "authenticated")
            self._read_profile()
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

            # A pause from the dashboard stops everything but keeps the
            # bot alive and reachable.
            if self._paused.is_set():
                self.store.update(status="paused")
                self.stop_event.wait(2.0)
                continue

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

    @staticmethod
    def _nonce() -> int:
        """A random number between 42 and 99, like the official client."""
        return random.SystemRandom().randint(42, 99)

    def _play_game(self, token: str, game_id: Any, server_url: str) -> bool:
        self._session_over.clear()
        # A custom game is played on the LOBBY socket.  The client
        # emits "mg:join-open-game" with the room id and the server
        # sends the whole game (game-started, the votes, the awards) on
        # that same socket.  A separate game socket with the game id in
        # the query always answers "error-game-connection-lost".
        sock = SocketIOClient(
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

        finished = threading.Event()
        last_event = [time.time()]

        def handle(event: str, *args: Any) -> None:
            last_event[0] = time.time()
            self._on_game_event(sock, event, args)

        sock.on_any(handle)
        sock.on("disconnect", lambda *a: finished.set())
        sock.on("game-over-awards-available", lambda *a: finished.set())

        try:
            sock.start()
        except Exception as exc:  # noqa: BLE001
            self.log_event("error", f"game socket failed: {exc}")
            return False

        # The join itself: the room id goes to the lobby socket.  The
        # official client sends a plain object here.
        time.sleep(2.0)
        try:
            sock.emit("mg:join-open-game", {"miniGameId": game_id})
        except Exception as exc:  # noqa: BLE001
            self.log_event("error", f"the join request failed: {exc}")
            sock.close()
            return False

        self.store.update(status="playing")
        # Stop as soon as the server bans us: touching it again would
        # only make the ban longer.
        while not finished.wait(timeout=1.0):
            if self._banned.is_set():
                break
            if self._restart.is_set():
                self._restart.clear()
                self.log_event("bot", "restarting the game on request")
                break
            if self._session_over.is_set():
                break
            # A socket that stays open but says nothing is a hang: the
            # game is over on the server and we would sit here for hours.
            if time.time() - last_event[0] > self.cfg.silence_timeout_sec:
                self.log_event("warn", "no server event for a long time - restarting the game")
                break

        # Leaving the room has to be announced.  A socket that simply
        # disappears counts as "fleeing the village" and the server bans
        # the account for it.
        try:
            sock.emit("mg:exit-game", {"miniGameId": game_id})
            self.log_event("game", "left the room")
        except Exception as exc:  # noqa: BLE001
            self.log_event("warn", f"the leave request failed: {exc}")
        time.sleep(1.0)
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
            self._find_own_id()
            self.store.update(
                role=self.state.role_name or self.state.role_id,
                players=len(self.state.players),
                phase=self.state.phase,
            )
            self._night_action(sock)
            self._protector_action(sock)
        elif event in ("game-night-started", "game-werewolves-vote-set"):
            self.state.apply(event, payload)
            self._night_action(sock)
            self._protector_action(sock)
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
    def _find_own_id(self) -> None:
        """Work out which player in the list is the bot.

        The player id comes from the profile; if that is not available,
        the player that carries the role the server handed us is us.
        """
        if self.state.own_id() is not None:
            return
        if not self._own_player_id:
            try:
                self._own_player_id = self.api.get_own_player_id(self.device_id)
            except Exception as exc:  # noqa: BLE001 - advisory only
                log.debug("could not read the player profile: %s", exc)
                self._own_player_id = None
        own = self._own_player_id
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
        self.store.update(
            username=profile.get("username"),
            level=profile.get("level"),
            account_xp=profile.get("xpTotal"),
            games_finished=profile.get("finishedGamesCount"),
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
                # The official client sends this one without a payload.
                sock.emit("game-vote-to-skip")
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

    def _protector_action(self, sock: SocketIOClient) -> None:
        """The doctor and the bodyguard pick somebody to protect."""
        event = PROTECTOR_ROLES.get(self.state.role_id.lower())
        if not event:
            return
        target = self.policy.protect_target(self.state)
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
