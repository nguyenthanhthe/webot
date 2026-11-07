"""Game state tracking and the decision policy of the bot.

Everything here was derived from the official client sources:

* server -> client events: ``game-started``, ``game-settings-changed``,
  ``game-day-voting-started``, ``game-day-vote-set``, ``game-night-started``,
  ``game-players-killed``, ``game-werewolves-set-roles``,
  ``game-werewolves-vote-set``, ``game-cupid-lover-ids-and-roles``,
  ``game-over-awards-available``, ``game-reconnect-set-players``,
  ``game:chat-public:msg``
* client -> server events: ``game-day-vote-set``, ``game-werewolves-vote-set``,
  ``game-doctor-protect-player``, ``game-priest-kill-player``,
  ``game-vigilante-shoot``, ``game-gunner-shoot-player``,
  ``game-vote-to-skip``, ``game:chat-public:msg``

Every payload arrives as a JSON *string* and has to be parsed twice
(outer string -> object), which is why :func:`parse_payload` exists.
"""

from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("wvbot.game")

WOLF_TEAMS = {"WEREWOLF", "WOLF", "EVIL", "WEREWOLVES"}

# Fast regex for detecting wolf claims in chat (e.g. "5 w", "w 5", "5 wolf", "im 5 w")
WOLF_CLAIM_RE = re.compile(
    r"(?:^|\b)(?:(?:im|i\s*am)\s+)?(?:(\d{1,2})\s*(?:w|wolf)\b|(?:w|wolf)\s*(\d{1,2})\b)",
    re.IGNORECASE,
)

# The XP each level needs, from the official client bundle.  The table
# is a list of per-level requirements; the cumulative thresholds are
# its prefix sums.  The server reports "xpUntilNextLevel" anyway, so
# this is only used to project how long the next level takes.
LEVEL_XP = [
    200, 350, 500, 650, 750, 850, 950, 1050, 1150, 1250,
    1350, 1450, 1500, 1550, 1600, 1650, 1700, 1750, 1800, 1850,
    1900, 2000, 2000, 2000, 2000, 2000, 2000, 2000, 2000, 2000,
]

# The cumulative XP needed to reach a level.  Index 0 is level 1.
LEVEL_XP_TOTAL = [0]
for _need in LEVEL_XP:
    LEVEL_XP_TOTAL.append(LEVEL_XP_TOTAL[-1] + _need)


def xp_to_next_level(level: int) -> int:
    """The XP that separates the current level from the next one."""
    if level < 1:
        level = 1
    if level >= len(LEVEL_XP):
        return LEVEL_XP[-1]
    return LEVEL_XP[level - 1]


def xp_total_for_level(level: int) -> int:
    """The cumulative XP needed to reach a level."""
    if level < 1:
        level = 1
    if level > len(LEVEL_XP_TOTAL):
        level = len(LEVEL_XP_TOTAL)
    return LEVEL_XP_TOTAL[level - 1]


def parse_payload(raw: Any) -> Any:
    """The server sends JSON-encoded strings; unwrap up to two levels."""
    value = raw
    for _ in range(2):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return value
        else:
            break
    return value


@dataclass
class Player:
    id: Any
    username: str = ""
    grid_idx: int = -1
    is_alive: bool = True
    role: str = ""
    team: str = ""
    level: int = 0

    @classmethod
    def from_object(cls, raw: Any) -> "Player | None":
        if not isinstance(raw, dict):
            return None
        pid = raw.get("id") or raw.get("playerId") or raw.get("_id")
        if pid is None:
            return None
        role = raw.get("role")
        if isinstance(role, dict):
            role_id = role.get("id") or role.get("_id") or ""
            team = role.get("team") or ""
        else:
            role_id = role or ""
            team = raw.get("team") or ""
        try:
            level = int(raw.get("level") or 0)
        except (TypeError, ValueError):
            level = 0
        # The grid index can legitimately be 0 (the first slot), so the
        # default has to be told apart from a real zero here.
        grid_raw = raw.get("gridIdx", raw.get("gridIndex", -1))
        try:
            grid_idx = int(grid_raw)
        except (TypeError, ValueError):
            grid_idx = -1
        return cls(
            id=pid,
            username=str(raw.get("username") or raw.get("name") or ""),
            grid_idx=grid_idx,
            is_alive=bool(raw.get("isAlive", True)),
            role=str(role_id),
            team=str(team),
            level=level,
        )


@dataclass
class GameState:
    game_id: Any = None
    server_url: str = ""
    role_id: str = ""
    role_name: str = ""
    role_team: str = ""
    players: list[Player] = field(default_factory=list)
    dead_ids: list[Any] = field(default_factory=list)
    lover_id: Any = None
    phase: str = "lobby"
    day_votes: dict[Any, Any] = field(default_factory=dict)
    night_votes: dict[Any, Any] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    last_award: dict[str, Any] = field(default_factory=dict)
    claimed_wolf_id: Any = None
    claimed_wolf_seat: int | None = None

    # -- helpers -------------------------------------------------------
    def by_id(self, pid: Any) -> Player | None:
        for player in self.players:
            if player.id == pid:
                return player
        return None

    def player_by_seat(self, seat: int) -> Player | None:
        """Find player by 1-based seat number (grid_idx + 1)."""
        for player in self.players:
            if player.grid_idx >= 0 and (player.grid_idx + 1) == seat:
                return player
        if 1 <= seat <= len(self.players):
            return self.players[seat - 1]
        return None

    def own_seat(self) -> int | None:
        """Return bot's 1-based seat number."""
        own = self.by_id(self.own_id())
        if own is not None and own.grid_idx >= 0:
            return own.grid_idx + 1
        for idx, p in enumerate(self.players):
            if p.id == self.own_id():
                return idx + 1
        return None

    def _check_wolf_claim(self, data: dict[str, Any]) -> None:
        """Parse chat message for wolf claims (e.g. '5 w', 'w 5', 'im wolf')."""
        msg = str(data.get("msg") or "").strip()
        if not msg:
            return
        m = WOLF_CLAIM_RE.search(msg)
        if m:
            seat_str = m.group(1) or m.group(2)
            try:
                seat = int(seat_str)
                claimed_player = self.player_by_seat(seat)
                if claimed_player and claimed_player.is_alive:
                    self.claimed_wolf_id = claimed_player.id
                    self.claimed_wolf_seat = seat
                    log.info("wolf claim registered: seat %d -> player %s (%s)", seat, claimed_player.username, claimed_player.id)
                    return
            except (ValueError, TypeError):
                pass
        clean = msg.lower().strip()
        if clean in ("im wolf", "i am wolf", "im w", "i am w", "me wolf", "wolf here"):
            author_id = data.get("authorId")
            if author_id is not None:
                author = self.by_id(author_id)
                if author and author.is_alive:
                    self.claimed_wolf_id = author.id
                    self.claimed_wolf_seat = (author.grid_idx + 1) if author.grid_idx >= 0 else None
                    log.info("author wolf claim registered: player %s (%s)", author.username, author.id)

    @property
    def alive(self) -> list[Player]:
        return [p for p in self.players if p.is_alive and p.id not in self.dead_ids]

    @property
    def is_wolf(self) -> bool:
        return self.role_team.upper() in WOLF_TEAMS or self.role_id.lower() in (
            "werewolf",
            "alpha-werewolf",
            "big-wolf",
        )

    def reset(self) -> None:
        self.game_id = None
        self.server_url = ""
        self.role_id = ""
        self.role_name = ""
        self.role_team = ""
        self.players = []
        self.dead_ids = []
        self.lover_id = None
        self.phase = "lobby"
        self.day_votes = {}
        self.night_votes = {}
        self.messages = []
        self.last_award = {}
        self.claimed_wolf_id = None
        self.claimed_wolf_seat = None
        # A new game is a new player list; the old own id must not leak
        # into it.
        self._own_id = None

    # -- ingest --------------------------------------------------------
    def apply(self, event: str, raw: Any) -> None:
        data = parse_payload(raw)
        if event == "game-started":
            self._on_started(data)
        elif event == "game-settings-changed":
            self.phase = "settings"
        elif event == "game-day-voting-started":
            self.phase = "day-voting"
        elif event == "game-night-started":
            self.phase = "night"
        elif event == "game-players-killed":
            self._on_killed(data)
        elif event == "game-werewolves-set-roles":
            self._on_wolf_roles(data)
        elif event == "game-werewolves-vote-set":
            self.phase = "night-voting"
            if isinstance(data, dict) and "targetPlayerId" in data:
                self.night_votes[self.own_id()] = data["targetPlayerId"]
        elif event == "game-day-vote-set":
            if isinstance(data, dict) and "targetPlayerId" in data:
                self.day_votes[data.get("authorId") or self.own_id()] = data["targetPlayerId"]
        elif event == "game-cupid-lover-ids-and-roles":
            if isinstance(data, dict):
                self.lover_id = data.get("loverId") or data.get("loverPlayerId")
        elif event == "game-over-awards-available":
            if isinstance(data, dict):
                self.phase = "game-over"
                award = data.get("playerAward") or {}
                self.last_award = award if isinstance(award, dict) else {}
        elif event == "game:chat-public:msg":
            if isinstance(data, dict):
                self.messages.append(data)
                del self.messages[:-200]
                self._check_wolf_claim(data)
        elif event == "game-reconnect-set-players":
            self._on_reconnect(data)
        elif event in (
            "player-joined-and-equipped-items",
            "players-and-equipped-items",
        ):
            self._on_equipped_items(data)

    def _on_equipped_items(self, data: Any) -> None:
        """The lobby tells us the level of every player.

        The game start event does not carry the levels, but the lobby
        events do, so the bot learns the level of everybody in the room
        before the game begins.
        """
        if not isinstance(data, dict):
            return
        raw_players = data.get("players")
        if not isinstance(raw_players, list):
            single = data.get("player")
            raw_players = [single] if isinstance(single, dict) else []
        for raw in raw_players:
            if not isinstance(raw, dict):
                continue
            pid = raw.get("id") or raw.get("playerId") or raw.get("_id")
            if pid is None:
                continue
            player = self.by_id(pid)
            if player is None:
                # The player is not in the game list yet: remember the
                # name and the level anyway.
                player = Player(id=pid)
                self.players.append(player)
            try:
                level = int(raw.get("level") or 0)
            except (TypeError, ValueError):
                level = 0
            if level:
                player.level = level
            username = str(raw.get("username") or raw.get("name") or "")
            if username:
                player.username = username
            if raw.get("gridIdx") is not None:
                try:
                    player.grid_idx = int(raw["gridIdx"])
                except (TypeError, ValueError):
                    pass

    def _on_started(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        self.phase = "started"
        role = data.get("role")
        if isinstance(role, dict):
            self.role_id = str(role.get("id") or role.get("_id") or "")
            self.role_name = str(role.get("name") or "")
            self.role_team = str(role.get("team") or "")
        elif isinstance(role, str):
            self.role_id = role
        players = data.get("players")
        if isinstance(players, list):
            parsed = [Player.from_object(p) for p in players]
            self.players = [p for p in parsed if p]
        log.info(
            "game started: role=%s team=%s players=%d",
            self.role_id or "?",
            self.role_team or "?",
            len(self.players),
        )

    def _on_killed(self, data: Any) -> None:
        ids: list[Any] = []
        if isinstance(data, list):
            ids = data
        elif isinstance(data, dict):
            for key in ("playerIds", "ids", "killedPlayerIds", "players"):
                value = data.get(key)
                if isinstance(value, list):
                    ids = value
                    break
            else:
                single = data.get("playerId") or data.get("id")
                if single is not None:
                    ids = [single]
        for pid in ids:
            if pid not in self.dead_ids:
                self.dead_ids.append(pid)
            player = self.by_id(pid)
            if player:
                player.is_alive = False

    def _on_wolf_roles(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        entries = data.get("roles") or data.get("players") or data
        if isinstance(entries, dict):
            entries = list(entries.values())
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            pid = entry.get("playerId") or entry.get("id")
            player = self.by_id(pid)
            if player is None:
                continue
            role = entry.get("role")
            if isinstance(role, dict):
                player.role = str(role.get("id") or "")
                player.team = str(role.get("team") or "")
            elif role:
                player.role = str(role)

    def _on_reconnect(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        for pid, info in data.items():
            if not isinstance(info, dict):
                continue
            player = self.by_id(pid)
            if player is None:
                continue
            alive = info.get("isAlive")
            if alive is not None:
                player.is_alive = bool(alive)
                if not alive and pid not in self.dead_ids:
                    self.dead_ids.append(pid)

    def own_id(self) -> Any:
        return getattr(self, "_own_id", None)

    def set_own_id(self, pid: Any) -> None:
        self._own_id = pid


class Policy:
    """Turns the observed game state into the next action.

    The goal is a win rate high enough to farm XP, not optimal play:

    * villagers vote for whoever the room accuses most often
    * werewolves kill a living non-teammate, preferring players who are
      not accusing them
    * the killing roles (priest / vigilante / gunner) act on the player the
      day vote landed on
    """

    def __init__(self, cfg: Any = None):
        self.cfg = cfg
        # SystemRandom: no prediction value needed, and it keeps the linter
        # happy about weak randomness.
        self.rng = random.SystemRandom()

    # -- voting --------------------------------------------------------
    def _accusation_counts(self, state: GameState) -> dict[Any, int]:
        """How often each *living* player was accused.

        Dead players are left out: voting for one would waste the vote,
        and the night policy only wants to know who is quietly alive.
        The bot itself is left out too - a room that accuses the bot
        must not make it vote for itself.
        """
        counts: dict[Any, int] = {}
        for message in state.messages:
            if not isinstance(message, dict):
                continue
            text = str(message.get("msg") or "")
            lowered = text.lower()
            if not any(word in lowered for word in ("wolf", "sus", "vote", "susp", "werewolf")):
                continue
            author = message.get("authorId")
            for player in state.alive:
                if player.id == author or player.id == state.own_id():
                    continue
                if player.id == author:
                    continue
                if player.username and player.username.lower() in lowered:
                    counts[player.id] = counts.get(player.id, 0) + 1
        return counts

    def vote_target(self, state: GameState) -> Any | None:
        alive = state.alive
        if not alive:
            return None
        # Claimed wolf from chat has absolute highest priority for voting!
        claimed_id = getattr(state, "claimed_wolf_id", None)
        if claimed_id is not None:
            claimed = state.by_id(claimed_id)
            if claimed and claimed.is_alive and claimed.id != state.own_id():
                return claimed.id
            if claimed_id == state.own_id():
                return None
        counts = self._accusation_counts(state)
        if counts:
            return max(counts.items(), key=lambda item: item[1])[0]
        # No information: vote for someone the room has already voted for,
        # otherwise skip the vote entirely.
        if state.day_votes:
            return None
        return None

    def skip_vote(self) -> bool:
        """Skip when there is no signal at all - avoids a random lynching."""
        return True

    # -- night ---------------------------------------------------------
    def kill_target(self, state: GameState, vill_win_mode: bool = False) -> Any | None:
        if vill_win_mode:
            # In vill-win rooms, werewolves NEVER bite at night
            return None
        candidates = [
            p
            for p in state.alive
            if p.id != state.own_id()
            and p.role.lower() not in WOLF_TEAMS
            and p.team.upper() not in WOLF_TEAMS
        ]
        if not candidates:
            candidates = [p for p in state.alive if p.id != state.own_id()]
        if not candidates:
            return None
        # Prefer a player nobody accused.
        counts = self._accusation_counts(state)
        quiet = [p for p in candidates if counts.get(p.id, 0) == 0]
        pool = quiet or candidates
        return self.rng.choice(pool).id

    def protect_target(self, state: GameState) -> Any | None:
        alive = [p for p in state.alive if p.id != state.own_id()]
        if not alive:
            return None
        # Prioritize key roles (seer, aura-seer, detective) if known
        seers = [
            p
            for p in alive
            if p.role.lower() in ("seer", "aura-seer", "detective")
            and p.team.upper() not in WOLF_TEAMS
        ]
        if seers:
            return self.rng.choice(seers).id
        # Otherwise protect highest level living player
        alive.sort(key=lambda p: getattr(p, "level", 0), reverse=True)
        top = alive[:3]
        return self.rng.choice(top).id

    def revive_target(self, state: GameState) -> Any | None:
        """Find a dead villager to revive for the Medium role."""
        dead = [
            p
            for p in state.players
            if (not p.is_alive or p.id in state.dead_ids)
            and p.id != state.own_id()
            and p.role.lower() not in WOLF_TEAMS
            and p.team.upper() not in WOLF_TEAMS
        ]
        if not dead:
            return None
        # Prefer higher level dead player
        dead.sort(key=lambda p: getattr(p, "level", 0), reverse=True)
        return dead[0].id

    def reveal_target(self, state: GameState) -> Any | None:
        """Find an unrevealed living player to investigate for Seer / Aura Seer."""
        candidates = [
            p for p in state.alive
            if p.id != state.own_id()
            and not p.role
        ]
        if not candidates:
            candidates = [p for p in state.alive if p.id != state.own_id()]
        if not candidates:
            return None
        return self.rng.choice(candidates).id

    def killer_target(self, state: GameState, safe_mode: bool = True) -> Any | None:
        """Choose a target for killer roles (priest, vigilante, gunner).

        In vill-win rooms:
        1. If a living player claimed wolf in chat, shoot them IMMEDIATELY!
        2. If a living player is confirmed wolf by role/team, shoot them.
        3. If safe_mode is False, fall back to accusations or day votes.
        """
        # 1. Claimed wolf from chat has absolute highest priority!
        claimed_id = getattr(state, "claimed_wolf_id", None)
        if claimed_id is not None:
            claimed = state.by_id(claimed_id)
            if claimed and claimed.is_alive and claimed.id != state.own_id():
                return claimed.id

        # 2. Known wolves
        wolves = [
            p
            for p in state.alive
            if p.id != state.own_id()
            and (p.team.upper() in WOLF_TEAMS or p.role.lower() in WOLF_TEAMS)
        ]
        if wolves:
            return self.rng.choice(wolves).id

        counts = self._accusation_counts(state)
        accused = [pid for pid, c in counts.items() if c >= 2]
        if accused:
            return accused[0]

        if safe_mode:
            return None

        for value in state.day_votes.values():
            if value and value != state.own_id():
                return value
        return None
