"""Probe the socket.io lobby and custom game flow from the phone (Termux).

Run inside Termux:
    python3 probe_socket.py [--wait SEC]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE if (_HERE / "wvbot").is_dir() else _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from wvbot.api import fold  # noqa: E402
from wvbot.config import load_config  # noqa: E402
from wvbot.http_client import HttpClient  # noqa: E402
from wvbot.sio import SocketIOClient  # noqa: E402

GAME_HOST = "https://game.api-wolvesville.com"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe Wolvesville Socket.IO custom-game join")
    parser.add_argument("--wait", type=float, default=12.0, help="Seconds to observe events after joining")
    args = parser.parse_args(argv)

    cfg = load_config()
    client = HttpClient()
    print("client.allowed_hosts:", sorted(client.allowed_hosts))
    try:
        data = client.request(
            "POST",
            "https://auth.api-wolvesville.com/players/createIdToken",
            body={"refreshToken": cfg.refresh_token},
        )
    except Exception as exc:  # noqa: BLE001
        print("request failed:", exc)
        return 1

    token = (data or {}).get("idToken", "")
    print(f"idToken length {len(token)}")

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
    print(f"\n== joining {target.get('name')!r} (id={game_id}) via lobby socket ==")

    socket = SocketIOClient(
        GAME_HOST,
        query={
            "firebaseToken": token,
            "ids": 1,
            "apiV": 1,
            "b": random.SystemRandom().randint(42, 99),
            "deviceId": str(uuid.uuid4()),
        },
        auth={"firebaseToken": token},
        headers={"Origin": "https://app.wolvesville.com"},
    )
    seen: list[str] = []
    started: dict[str, object] = {}

    def on_any(event: str, *event_args: object) -> None:
        seen.append(event)
        payload = event_args[0] if event_args else None
        if event.startswith("game-") or event.startswith("error-") or event.startswith("mg:"):
            print(f"  event {event}: {str(payload)[:250]}")
        if event == "game-started":
            started["payload"] = payload

    socket.on_any(on_any)
    try:
        socket.start()
        time.sleep(getattr(cfg, "socket_join_delay_sec", 1.0))
        socket.emit("mg:join-open-game", {"miniGameId": game_id})
    except Exception as exc:  # noqa: BLE001
        print(f"connect/join failed: {exc}")
        socket.close()
        return 1

    print("connected, waiting for game events...")
    time.sleep(max(1.0, args.wait))
    print(f"events seen: {len(seen)}")
    print(f"first events: {seen[:25]}")
    if started:
        print(f"game-started: {json.dumps(started.get('payload'))[:800]}")
    try:
        socket.emit("mg:exit-game")
    except Exception:
        pass
    socket.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
