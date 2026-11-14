"""Configuration for the Wolvesville custom-game farming bot.

Config resolution order (later wins):
    1. built-in defaults
    2. JSON file inside the state directory (default ``config.json``)
    3. environment variables (prefix ``WV_BOT_``)

All state lives in a single fixed directory (``~/.wv-bot``). File names are
plain names, never paths, so no config value can point the bot outside it.
"""

from __future__ import annotations

import json
import os
import types
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints

AUTH_URL = "https://auth.api-wolvesville.com"
CORE_URL = "https://core.api-wolvesville.com"
GAME_URL = "https://game.api-wolvesville.com"
GAME_URL_ASIA = "https://game-asia.api-wolvesville.com"

# Turnstile site keys used by the official clients.
# 0x4AAAAAAATLZS5RyqlMGxsL = managed (invisible) widget
# 0x4AAAAAAATLZulAZ6iRPZsU = forced visible widget
TURNSTILE_SITEKEY_MANAGED = "0x4AAAAAAATLZS5RyqlMGxsL"
TURNSTILE_SITEKEY_VISIBLE = "0x4AAAAAAATLZulAZ6iRPZsU"

ANDROID_HOME = Path("/data/data/com.termux/files/home")


def _env_state_dir() -> Path | None:
    for env_key in ("WV_BOT_STATE_DIR", "STATE_DIR"):
        raw = os.environ.get(env_key, "").strip()
        if raw:
            return Path(raw)
    return None


def _platform_state_dir() -> Path:
    # On Android the home directory is fixed: `Path.home()` can point somewhere
    # else when the process is started through `run-as`.
    if ANDROID_HOME.is_dir():
        return ANDROID_HOME / ".wv-bot"
    return Path.home() / ".wv-bot"


_DEFAULT_STATE_DIR = _env_state_dir() or _platform_state_dir()
STATE_DIR = _DEFAULT_STATE_DIR

# The durable credential, stored on its own (mode 600).
REFRESH_TOKEN_FILE = "refresh_token.txt"


def get_state_dir() -> Path:
    """Return the active state directory.

    Priority: explicitly overridden ``STATE_DIR`` (preserving test
    monkeypatching), then ``WV_BOT_STATE_DIR`` / ``STATE_DIR`` env vars,
    then the platform default.
    """
    if STATE_DIR != _DEFAULT_STATE_DIR:
        return STATE_DIR
    env_dir = _env_state_dir()
    if env_dir is not None:
        return env_dir
    return _platform_state_dir()


def state_path(name: str, base: Path | None = None) -> Path:
    """Return a path inside the state directory for a plain file name.

    ``name`` must be a bare file name: separators and parent references are
    rejected, so the result can never leave the state directory.
    """
    if (
        not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or os.sep in name
        or Path(name).name != name
    ):
        raise ValueError(f"not a valid file name: {name!r}")
    root = base if base is not None else get_state_dir()
    return root / name


@dataclass
class Config:
    # --- account -------------------------------------------------------
    email: str = ""
    password: str = ""
    # A Firebase refreshToken lets us mint new idTokens forever without
    # re-running Turnstile. Preferred over email+password at runtime.
    refresh_token: str = ""
    # Cloudflare attestation JWT ("Cf-JWT"). Written by an external helper.
    cf_jwt: str = ""
    cf_jwt_file: str = "cf_jwt.txt"

    # --- what to farm --------------------------------------------------
    # Room names to join in the custom-game list. "vill win" is the English
    # region room the operator reported as continuously populated.
    room_names: list[str] = field(default_factory=lambda: ["vill win"])
    # Rooms with a password are skipped by default: the server refuses
    # the join without it.
    allow_password_rooms: bool = False
    # The password of the rooms, when they all share the same one.
    room_password: str = ""
    language: str = "en"
    game_mode: str = "custom"
    # Asia routing: the English lobby lives on game.api-wolvesville.com,
    # but game-asia.api-wolvesville.com is a valid alternative host.
    use_asia_host: bool = False

    # --- turbo & vill-win xp farm mode ---------------------------------
    # Turbo mode enables high-speed custom room farming (>= 3,000 XP/h target):
    # aggressive cycling (1-2.5s delay), no bio-break pauses, 0% room skip,
    # no hourly join limit throttling, prioritization of 8-player rooms.
    turbo_mode: bool = True
    turbo_delay_min_sec: float = 1.0
    turbo_delay_max_sec: float = 2.5
    vill_win_mode: bool = True  # Wolf self-claim & no bite at night, killer instant reaction
    wolf_self_claim: bool = True  # Claim seat number as wolf on Day 1 (e.g. "5 w")
    instant_kill: bool = True  # Killer roles shoot claimed wolf immediately (<0.5s)
    prefer_8p_rooms: bool = True  # Prioritize rooms near the 8-player start threshold (7-10 players)
    strict_8p_only: bool = False  # Relaxed: Wolvesville rooms have 16 role slots but start at 8 players
    strict_tf_only: bool = True  # Strict fast farm: only join Time Flies or fast rooms, never fall back to slow casual rooms
    max_room_size: int = 16  # Standard Wolvesville custom lobby size
    stay_in_room: bool = True  # Stay in the room for continuous fast rematches with the same group
    stay_in_room_timeout_sec: float = 60.0  # Max idle seconds in room lobby before looking for a new room
    stay_in_room_max_game_duration_sec: float = 150.0  # Ditch rooms whose matches drag on > 2.5 minutes

    # --- behaviour -----------------------------------------------------
    claim_double_xp: bool = True
    chat_enabled: bool = False
    chat_messages: list[str] = field(default_factory=list)
    # Instant deterministic regex reflex chat (0ms AI-free reflex)
    reflex_chat_enabled: bool = True
    reflex_chat_cooldown_sec: float = 12.0
    # Bio-break fatigue simulator (prevent 24/7 robotic anomaly detection)
    bio_break_enabled: bool = True
    bio_break_interval_games: int = 12
    bio_break_min_sec: float = 120.0
    bio_break_max_sec: float = 240.0
    # Fast reconnect on unexpected network disconnect during an active game
    fast_reconnect_enabled: bool = True
    fast_reconnect_max_attempts: int = 3
    fast_reconnect_delay_sec: float = 3.0
    socket_join_delay_sec: float = 1.0
    # Seconds to wait before re-joining after being kicked / disconnected.
    # The server bans players that join too often, so this is deliberately
    # generous rather than quick.
    reconnect_delay_sec: float = 60.0
    reconnect_delay_max_sec: float = 300.0
    # A flat delay is a fingerprint in itself, so the wait is drawn from
    # a range.  This is what RXZBot calls "human behaviour".
    delay_min_sec: float = 45.0
    delay_max_sec: float = 150.0
    # A game that sends no event for this long is treated as hung and
    # restarted.  A normal game sends a chat or vote event every minute.
    silence_timeout_sec: float = 300.0
    # The process re-execs itself at this local hour (between two games)
    # to keep a 24/7 run fresh.  None disables it.
    daily_restart_hour: int | None = 4
    # How many games may be joined per hour before the bot backs off on
    # its own.  The server bans accounts that join too often.
    joins_per_hour: int = 12
    # A human does not take every room.  This is the chance that a
    # matching room is left alone this round and the bot looks again
    # after the normal wait.
    room_skip_chance: float = 0.15
    # Small vill-win rooms rotate their games faster, which pays more
    # per hour than waiting for a full sixteen.  When true the room
    # pick prefers the smallest matches.
    prefer_small_rooms: bool = True
    request_timeout_sec: float = 20.0
    max_retries: int = 3
    # How often the account profile is read.  The profile carries the
    # name, the level and the ban, so it is worth a look now and then.
    profile_refresh_sec: float = 120.0
    # How often the game is checked for a patch.
    update_check_sec: float = 1800.0

    # --- rewards -------------------------------------------------------
    # The daily reward is free, so it is claimed by default.  The
    # golden wheel costs roses, so it stays off until asked for.
    claim_daily_rewards: bool = True
    # A loot box holds gold, XP or a role card, so it is opened.
    open_loot_boxes: bool = True
    spin_golden_wheel: bool = False
    # How many roses the wheel may cost before the bot refuses to spin.
    wheel_max_roses: int = 100

    # --- dashboard -----------------------------------------------------
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8787
    dashboard_token: str = ""

    # --- runtime -------------------------------------------------------
    log_level: str = "INFO"
    state_file: str = "state.json"

    # ------------------------------------------------------------------
    @property
    def game_base_url(self) -> str:
        return GAME_URL_ASIA if self.use_asia_host else GAME_URL

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in ("password", "refresh_token", "cf_jwt", "dashboard_token"):
            if data.get(key):
                data[key] = "***"
        return data


CONFIG_FIELDS: frozenset[str] = frozenset(f.name for f in fields(Config))
_CONFIG_FIELD_TYPES: dict[str, Any] = get_type_hints(Config)


def apply_overrides(cfg: Config, mapping: Mapping[str, Any]) -> Config:
    """Apply ``mapping`` values restricted to declared ``Config`` fields."""
    for key, value in mapping.items():
        if key in CONFIG_FIELDS:
            setattr(cfg, key, value)
    return cfg


def _unwrap_type(field_type: Any) -> tuple[Any, bool]:
    """Return ``(base_type, allows_none)`` for a dataclass field annotation."""
    origin = get_origin(field_type)
    if origin in (Union, types.UnionType):
        args = get_args(field_type)
        allows_none = type(None) in args
        non_none = [arg for arg in args if arg is not type(None)]
        inner = non_none[0] if len(non_none) == 1 else field_type
        return (get_origin(inner) or inner), allows_none
    return (origin or field_type), False


def _coerce(current: Any, raw: str, field_type: Any = None) -> Any:
    target_type: Any = None
    allows_none = False
    if field_type is not None:
        target_type, allows_none = _unwrap_type(field_type)
        if allows_none and raw.strip().lower() in ("", "none", "null"):
            return None
    if target_type is None and current is not None:
        target_type = type(current)

    if target_type is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if target_type is int:
        if not raw.strip():
            return None if allows_none else current
        return int(float(raw))
    if target_type is float:
        if not raw.strip():
            return None if allows_none else current
        return float(raw)
    if target_type is list:
        return [p.strip() for p in raw.split(",") if p.strip()]
    return raw


def load_config(config_file: str | None = None, base: Path | None = None) -> Config:
    cfg = Config()
    root = base if base is not None else get_state_dir()
    root.mkdir(parents=True, exist_ok=True)

    name = config_file or os.environ.get("WV_BOT_CONFIG", "config.json")
    cfg_path = state_path(name, base=root)
    if cfg_path.is_file():
        with open(cfg_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, Mapping):
            apply_overrides(cfg, data)

    if "WV_DASHBOARD_TOKEN" in os.environ:
        cfg.dashboard_token = os.environ["WV_DASHBOARD_TOKEN"]

    for key in CONFIG_FIELDS:
        env_key = "WV_BOT_" + key.upper()
        if env_key in os.environ:
            setattr(
                cfg,
                key,
                _coerce(getattr(cfg, key), os.environ[env_key], _CONFIG_FIELD_TYPES.get(key)),
            )

    # A cf_jwt file wins over the inline value when it exists: the external
    # Turnstile helper refreshes it in place.
    try:
        jwt_file = state_path(cfg.cf_jwt_file, base=root)
    except ValueError:
        jwt_file = None
    if jwt_file is not None and jwt_file.is_file():
        token = jwt_file.read_text(encoding="utf-8").strip()
        if token:
            cfg.cf_jwt = token

    # The refresh token lives in its own file (mode 600) so it never ends
    # up in a config file that gets copied around.
    try:
        refresh_file = state_path(REFRESH_TOKEN_FILE, base=root)
    except ValueError:
        refresh_file = None
    if refresh_file is not None and refresh_file.is_file():
        token = refresh_file.read_text(encoding="utf-8").strip()
        if token:
            cfg.refresh_token = token

    return cfg
