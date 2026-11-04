"""REST helpers for the Wolvesville API.

Only the endpoints the bot actually needs are wrapped. The response shapes
come from the official web client (``static/js/main.*.js``):

* ``GET  {game}/api/public/game/custom?language=en`` -> ``{openGames: [...]}``
* ``GET  {game}/api/public/game/custom/settings?gameId=..&locale=en``
* ``GET  {game}/api/public/game/customGames/checkPasswordPresence?gameId=..``
* ``GET  {core}/players/meAndCheckAppVersion``
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any

from .config import CORE_URL, Config
from .http_client import HttpClient

log = logging.getLogger("wvbot.api")


class GameApi:
    def __init__(self, cfg: Config, http: HttpClient, auth_headers: dict[str, str] | None = None):
        self.cfg = cfg
        self.http = http
        self.auth_headers = dict(auth_headers or {})

    # ------------------------------------------------------------------
    def list_custom_games(self, language: str | None = None) -> list[dict[str, Any]]:
        """Return the open custom-game rooms for a language."""
        lang = language or self.cfg.language
        url = f"{self.cfg.game_base_url}/api/public/game/custom?language={lang}"
        data = self.http.request("GET", url, headers=self.auth_headers)
        if isinstance(data, dict):
            games = data.get("openGames") or data.get("games") or []
            return games if isinstance(games, list) else []
        if isinstance(data, list):
            return data
        return []

    # ------------------------------------------------------------------
    @staticmethod
    def _room_name(room: dict[str, Any]) -> str:
        for key in ("name", "gameName", "title", "roomName"):
            value = room.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    @staticmethod
    def _room_id(room: dict[str, Any]) -> Any:
        # The REST list names the field "gameId"; the socket list uses "id".
        for key in ("gameId", "id", "miniGameId", "_id"):
            if key in room:
                return room[key]
        return None

    @staticmethod
    def fold(value: str) -> str:
        """Fold a name to plain ASCII.

        The English rooms are named by players and use all kinds of
        characters - "E  VİLL WİN" with the Turkish dotted I, for example.
        Folding makes the comparison reliable.
        """
        text = str(value or "").replace("İ", "I").replace("ı", "i")
        text = "".join(
            char for char in unicodedata.normalize("NFD", text)
            if not unicodedata.combining(char)
        )
        return text.lower().strip()

    def find_rooms(self, names: list[str]) -> list[dict[str, Any]]:
        """Find open rooms whose name matches one of ``names``.

        Matching ignores case and accents and also accepts a room whose
        name merely contains the target, because operators rename rooms
        freely.
        """
        wanted = [self.fold(n) for n in names if n and n.strip()]
        if not wanted:
            return []
        found: list[dict[str, Any]] = []
        for room in self.list_custom_games():
            if not isinstance(room, dict):
                continue
            name = self.fold(self._room_name(room))
            if not name:
                continue
            if name in wanted or any(w in name for w in wanted):
                found.append(room)
        return found

    # ------------------------------------------------------------------
    def check_password_presence(self, game_id: Any) -> bool:
        url = (
            f"{self.cfg.game_base_url}/api/public/game/customGames/"
            f"checkPasswordPresence?gameId={game_id}"
        )
        data = self.http.request("GET", url, headers=self.auth_headers)
        if isinstance(data, dict):
            for key in ("hasPassword", "isPasswordProtected", "passwordRequired"):
                if key in data:
                    return bool(data[key])
        return False

    def get_game_settings(self, game_id: Any, locale: str = "en") -> Any:
        url = (
            f"{self.cfg.game_base_url}/api/public/game/custom/settings"
            f"?gameId={game_id}&locale={locale}"
        )
        return self.http.request("GET", url, headers=self.auth_headers)

    def get_me(self) -> Any:
        url = f"{CORE_URL}/players/meAndCheckAppVersion"
        return self.http.request("GET", url, headers=self.auth_headers)


def looks_like_room_name(blob: Any, needle: str) -> bool:
    """Fallback room matcher that scans a raw JSON blob for the name."""
    if not isinstance(blob, (dict, list)):
        return False
    text = json_dumps(blob)
    return re.search(re.escape(needle), text, re.IGNORECASE) is not None


def json_dumps(value: Any) -> str:
    import json

    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)
