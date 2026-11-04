"""Probe the game API from the phone (Termux).

Run inside Termux:
    python3 probe_phone.py

The refresh token is read from the state directory
(``~/.wv-bot/refresh_token.txt``), which the installer writes.

It checks:
  1. can the phone mint an idToken from the refreshToken?
  2. can the phone list the open custom games?
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from wvbot.config import state_path  # noqa: E402
from wvbot.http_client import HttpClient  # noqa: E402

TOKEN_FILE = "refresh_token.txt"


def read_refresh_token() -> str:
    path = state_path(TOKEN_FILE)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def main() -> int:
    refresh = read_refresh_token()
    if not refresh:
        print("no refresh token found - run deploy.sh first")
        return 1
    client = HttpClient()

    print("== 1. mint idToken ==")
    try:
        data = client.request(
            "POST",
            "https://auth.api-wolvesville.com/players/createIdToken",
            body={"refreshToken": refresh},
        )
        token = (data or {}).get("idToken", "")
        print(f"ok: idToken length {len(token)}")
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {exc}")
        return 1

    print("\n== 2. list custom games ==")
    try:
        data = client.request(
            "GET",
            "https://game.api-wolvesville.com/api/public/game/custom?language=en",
            headers={"Authorization": f"Bearer {token}"},
        )
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {exc}")
        return 1

    games = (data or {}).get("openGames", []) or []
    print(f"open games: {len(games)}")
    for game in games:
        print(
            f"  id={game.get('gameId')} name={game.get('name')!r} "
            f"players={game.get('playerCount')} password={game.get('hasPassword')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
