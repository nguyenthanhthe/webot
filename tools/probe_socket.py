"""Probe the socket.io lobby from the phone (Termux).

Run inside Termux:
    python3 probe_socket.py

It checks:
  1. can the phone open the lobby socket (Engine.IO over websocket)?
  2. what does the server answer - open games, or a Cloudflare error?
  3. can the bot join a room and reach the game socket?
"""

from __future__ import annotations

import json
import sys
import time
import unicodedata

sys.path.insert(0, ".")

from wvbot.config import load_config, state_path  # noqa: E402
from wvbot.http_client import HttpClient  # noqa: E402
from wvbot.sio import SocketIOClient  # noqa: E402

GAME_HOST = "https://game.api-wolvesville.com"


def fold(value: str) -> str:
    """Fold a room name to plain ASCII, so "VİLL WİN" matches "vill win"."""
    text = str(value or "").replace("İ", "I").replace("ı", "i")
    text = "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c))
    return text.lower().strip()



def main() -> int:
    cfg = load_config()
    client = HttpClient()
    print("client.allowed_hosts:", sorted(client.allowed_hosts))
    print("client module:", type(client).__module__, type(client).__module__ and sys.modules[type(client).__module__].__file__)
    try:
        data = client.request(
            "POST",
            "https://auth.api-wolvesville.com/players/createIdToken",
            body={"refreshToken": cfg.refresh_token},
        )
    except Exception as exc:  # noqa: BLE001
        print("request failed:", exc)
        print("allowed_hosts now:", sorted(client.allowed_hosts))
        print("module file:", sys.modules[type(client).__module__].__file__)
        raise

    token = (data or {}).get("idToken", "")
    print(f"idToken length {len(token)}")

    # The rooms come and go, so keep looking for a while.
    wanted = [fold(name) for name in cfg.room_names]
    matching: list[dict] = []
    games: list[dict] = []
    for attempt in range(20):
        listing = client.request(
            "GET",
            f"{GAME_HOST}/api/public/game/custom?language=en",
            headers={"Authorization": f"Bearer {token}"},
        )
        games = (listing or {}).get("openGames", []) or []
        matching = [
            game
            for game in games
            if any(needle in fold(game.get("name", "")) for needle in wanted)
        ]
        print(
            f"attempt {attempt + 1}: {len(games)} open, {len(matching)} matching "
            f"-> {[g.get('name') for g in games]}"
        )
        if matching:
            break
        time.sleep(10)


    if not matching:
        print("no matching room - nothing to join")
        return 1

    target = matching[0]
    game_id = target.get("gameId") or target.get("id")
    print(f"\n== game socket for {target.get('name')!r} (id={game_id}) ==")

    # A custom game is entered directly: the game socket carries the
    # game id and the mode, there is no lobby step.
    socket = SocketIOClient(
        GAME_HOST,
        query={
            "gameId": game_id,
            "gameMode": "custom",
            "reconnect": "true",
            "ids": 1,
            "apiV": 1,
        },
        auth={"firebaseToken": token},
    )
    seen: list[str] = []
    started: dict[str, object] = {}

    def on_any(event: str, *args: object) -> None:
        seen.append(event)
        payload = args[0] if args else None
        if event.startswith("game-") or event.startswith("error-"):
            print(f"  event {event}: {str(payload)[:250]}")
        if event == "game-started":
            started["payload"] = payload

    socket.on_any(on_any)
    try:
        socket.start()
    except Exception as exc:  # noqa: BLE001
        print(f"connect failed: {exc}")
        return 1

    print("connected, waiting for the game to start...")
    time.sleep(12)
    print(f"events seen: {len(seen)}")
    print(f"first events: {seen[:25]}")
    if started:
        print(f"game-started: {json.dumps(started.get('payload'))[:800]}")
    socket.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
