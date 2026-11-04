"""List the open rooms with the password flag and the matching result.

Run inside Termux:
    python3 probe_rooms.py
"""

from __future__ import annotations

import json
import sys

sys.path.insert(0, ".")

from wvbot.api import GameApi  # noqa: E402
from wvbot.auth import AuthManager  # noqa: E402
from wvbot.config import load_config  # noqa: E402
from wvbot.http_client import HttpClient  # noqa: E402


def main() -> int:
    cfg = load_config()
    http = HttpClient()
    auth = AuthManager(cfg, http)
    token = auth.ensure_token()
    api = GameApi(cfg, http, auth.auth_headers())

    rooms = api.list_custom_games()
    print(f"{len(rooms)} open room(s):")
    for room in rooms:
        name = api._room_name(room)
        print(
            f"  {name!r:45} id={api._room_id(room)} "
            f"players={room.get('playerCount')} "
            f"password={room.get('hasPassword')} "
            f"regularXp={room.get('regularXp')}"
        )

    matched = api.find_rooms(cfg.room_names)
    print(f"\nmatched by find_rooms({cfg.room_names}):")
    for room in matched:
        print(f"  {api._room_name(room)!r} password={room.get('hasPassword')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
