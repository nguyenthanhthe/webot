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

from . import __version__
from .api import GameApi
from .auth import AuthManager
from .config import Config, state_path
from .dashboard import StatusStore, start_dashboard
from .game import GameState, Policy, parse_payload, xp_to_next_level
from .history import XpHistory
from .http_client import HttpClient
from .rewards import Rewards
from .sio import SocketIOClient
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
        # The actions already taken in the current phase, so the bot
        # acts once and not on every repeated server event.
        self._acted_keys: set[str] = set()
        # The XP counters survive a restart.
        self.history = XpHistory(state_path(cfg.state_file.replace(".json", "_history.json")))
        # When the profile was read the last time.
        self._last_profile_read = 0.0
        # Watches the game for a patch that could break the protocol.
        self.updates = UpdateWatcher(cfg, self.http, self.auth.auth_headers())
        self._last_update_check = 0.0

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

    def _skip_room(self) -> bool:
        """True when this matching room is left alone, as a human would.

        The skipped room does not count as a join for the hourly limit.
        """
        return random.SystemRandom().random() < self.cfg.room_skip_chance

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
        start_dashboard(
            self.store,
            self.cfg.dashboard_host,
            self.cfg.dashboard_port,
            control=self._control,
            reward=self._reward,
        )
        self.log_event("bot", f"bot starting (v{__version__})")
        self.store.update(version=__version__)
        self.history.record_session()
        self.store.update(history=self.history.get())
        try:
            self.auth.ensure_token()
            self.api.auth_headers = self.auth.auth_headers()
            self.log_event("auth", "authenticated")
            self._read_profile()
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            self.store.update(status="error", last_error=str(exc), errors=1)
            self.log_event("error", f"initial auth failed: {exc}")

        while not self.stop_event.is_set():
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
                # The rewards are picked up between two games, when the
                # bot is not holding a room.
                self._collect_rewards()
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
        self._clear_actions()
        self.store.update(status="looking for room", phase="lobby", room=None)

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

        room = random.SystemRandom().choice(rooms)
        room_id = GameApi._room_id(room)
        room_name = GameApi._room_name(room)

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
            # The account XP changed on the server, so the level and the
            # projection have to be read again.
            self._read_profile()
            self.log_event("xp", f"+{xp} xp" + (f" (+{levels} levels)" if levels else ""))
            # The counters survive a restart.
            self.history.record_game(xp)
            self.store.update(history=self.history.get())
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
                levels=self._player_levels(),
            )
            self._night_action(sock)
            self._protector_action(sock)
        elif event in ("game-night-started", "game-werewolves-vote-set"):
            self.state.apply(event, payload)
            # A new night clears the actions of the last one.
            if event == "game-night-started":
                self._clear_actions()
            self._night_action(sock)
            self._protector_action(sock)
        elif event == "game-day-voting-started":
            self.state.apply(event, payload)
            self._clear_actions()
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
            "player-joined-and-equipped-items",
            "players-and-equipped-items",
        ):
            self.state.apply(event, payload)
            if event in ("player-joined-and-equipped-items", "players-and-equipped-items"):
                self.store.update(levels=self._player_levels())
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
        open_ones = [r for r in ladder if not r.get("claimed")]
        if not open_ones:
            return
        kind = str(open_ones[0].get("type") or "reward")
        amount = open_ones[0].get("amount")
        try:
            rewards.claim_daily_reward()
        except Exception as exc:  # noqa: BLE001
            self.log_event("warn", f"the daily reward failed: {exc}")
            return
        self.log_event("reward", f"claimed the daily reward: {kind} x{amount}")

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

        The XP rate comes from the session so far, the time to the next
        level from the XP the server says is missing.
        """
        stats = self.store.get()
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
        # One vote per night.  The server sends the night events again
        # for every vote in the room, and voting over and over looks
        # like a bot.
        if self._acted("werewolf-vote"):
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

    def _acted(self, key: str) -> bool:
        """True when this action was already taken in the current phase.

        The flag is cleared whenever a new phase starts, so the bot acts
        once per night and once per day.
        """
        if key in self._acted_keys:
            return True
        self._acted_keys.add(key)
        return False

    def _clear_actions(self) -> None:
        self._acted_keys.clear()

    def _day_vote(self, sock: SocketIOClient) -> None:
        if self._acted("day-vote"):
            return
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
        # One kill per day.  The server repeats the day vote for every
        # player that votes, so without this the bot would shoot the
        # same victim over and over.
        if self._acted(event):
            return
        data = self.state.day_votes
        target = None
        for value in data.values():
            target = value
        if target is None:
            self._acted_keys.discard(event)
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
        if self._acted(event):
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
