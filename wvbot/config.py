"""Configuration for the Wolvesville custom-game farming bot (v0.0.1).

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
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

AUTH_URL = "https://auth.api-wolvesville.com"
CORE_URL = "https://core.api-wolvesville.com"
GAME_URL = "https://game.api-wolvesville.com"
GAME_URL_ASIA = "https://game-asia.api-wolvesville.com"

# Turnstile site keys used by the official clients.
# 0x4AAAAAAATLZS5RyqlMGxsL = managed (invisible) widget
# 0x4AAAAAAATLZulAZ6iRPZsU = forced visible widget
TURNSTILE_SITEKEY_MANAGED = "0x4AAAAAAATLZS5RyqlMGxsL"
TURNSTILE_SITEKEY_VISIBLE = "0x4AAAAAAATLZulAZ6iRPZsU"

STATE_DIR = Path.home() / ".wv-bot"

# On Android the home directory is fixed: `Path.home()` can point somewhere
# else when the process is started through `run-as`.
ANDROID_HOME = Path("/data/data/com.termux/files/home")
if ANDROID_HOME.is_dir():
    STATE_DIR = ANDROID_HOME / ".wv-bot"

# The durable credential, stored on its own (mode 600).
REFRESH_TOKEN_FILE = "refresh_token.txt"


def state_path(name: str) -> Path:
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
    return STATE_DIR / name


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
    language: str = "en"
    game_mode: str = "custom"
    # Asia routing: the English lobby lives on game.api-wolvesville.com,
    # but game-asia.api-wolvesville.com is a valid alternative host.
    use_asia_host: bool = False

    # --- behaviour -----------------------------------------------------
    claim_double_xp: bool = True
    chat_enabled: bool = False
    chat_messages: list[str] = field(default_factory=list)
    # Seconds to wait before re-joining after being kicked / disconnected.
    # The server bans players that join too often, so this is deliberately
    # generous rather than quick.
    reconnect_delay_sec: float = 60.0
    reconnect_delay_max_sec: float = 300.0
    # A flat delay is a fingerprint in itself, so the wait is drawn from
    # a range.  This is what RXZBot calls "human behaviour".
    delay_min_sec: float = 45.0
    delay_max_sec: float = 150.0
    # Seconds of "play then leave" before recycling into a fresh room.
    idle_restart_sec: float = 900.0
    # How many games may be joined per hour before the bot backs off on
    # its own.  The server bans accounts that join too often.
    joins_per_hour: int = 12
    request_timeout_sec: float = 20.0
    max_retries: int = 3

    # --- dashboard -----------------------------------------------------
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8787

    # --- runtime -------------------------------------------------------
    log_level: str = "INFO"
    state_file: str = "state.json"

    # ------------------------------------------------------------------
    @property
    def game_base_url(self) -> str:
        return GAME_URL_ASIA if self.use_asia_host else GAME_URL

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in ("password", "refresh_token", "cf_jwt"):
            if data.get(key):
                data[key] = "***"
        return data


def _coerce(current: Any, raw: str) -> Any:
    if isinstance(current, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current, int) and not isinstance(current, bool):
        return int(float(raw))
    if isinstance(current, float):
        return float(raw)
    if isinstance(current, list):
        return [p.strip() for p in raw.split(",") if p.strip()]
    return raw


def load_config(config_file: str | None = None) -> Config:
    cfg = Config()
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    name = config_file or os.environ.get("WV_BOT_CONFIG", "config.json")
    cfg_path = state_path(name)
    if cfg_path.is_file():
        with open(cfg_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for key, value in data.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)

    for key in list(asdict(cfg).keys()):
        env_key = "WV_BOT_" + key.upper()
        if env_key in os.environ:
            setattr(cfg, key, _coerce(getattr(cfg, key), os.environ[env_key]))

    # A cf_jwt file wins over the inline value when it exists: the external
    # Turnstile helper refreshes it in place.
    try:
        jwt_file = state_path(cfg.cf_jwt_file)
    except ValueError:
        jwt_file = None
    if jwt_file is not None and jwt_file.is_file():
        token = jwt_file.read_text(encoding="utf-8").strip()
        if token:
            cfg.cf_jwt = token

    # The refresh token lives in its own file (mode 600) so it never ends
    # up in a config file that gets copied around.
    try:
        refresh_file = state_path(REFRESH_TOKEN_FILE)
    except ValueError:
        refresh_file = None
    if refresh_file is not None and refresh_file.is_file():
        token = refresh_file.read_text(encoding="utf-8").strip()
        if token:
            cfg.refresh_token = token

    return cfg
