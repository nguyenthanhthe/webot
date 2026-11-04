"""Check whether the bot really joins a custom game.

Run inside Termux:
    python3 probe_join.py

The official client plays a custom game on the *lobby* socket: it
emits "mg:join-open-game" with the room id and then receives
"game-started" with its own role on that same socket.  A separate
game socket always answers "error-game-connection-lost".

The probe joins one room through the lobby socket and checks whether
the own player id shows up in the player list of "game-started".
"""

from __future__ import annotations

import json
import sys
import time

sys.path.insert(0, ".")

from wvbot.api import GameApi  # noqa: E402
from wvbot.auth import AuthManager  # noqa: E402
from wvbot.config import load_config, state_path  # noqa: E402
from wvbot.http_client import HttpClient  # noqa: E402
from wvbot.sio import SocketIOClient  # noqa: E402

GAME_HOST = "https://game.api-wolvesville.com"

_lines: list[str] = []


def log(text: str = "") -> None:
    print(text, flush=True)
    _lines.append(text)


def nonce() -> str:
    import random

    return str(random.SystemRandom().randint(42, 99))


def wait_for_free_room(api: GameApi, seconds: float = 300.0) -> dict | None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        rooms = api.list_custom_games()
        free = [
            room for room in rooms
            if not room.get("hasPassword") and int(room.get("playerCount") or 0) > 0
        ]
        log(f"   {len(rooms)} open, {len(free)} free")
        if free:
            return free[0]
        time.sleep(15)
    return None


def main() -> int:
    cfg = load_config()
    http = HttpClient()
    auth = AuthManager(cfg, http)
    token = auth.ensure_token()
    api = GameApi(cfg, http, auth.auth_headers())

    own_id = api.get_own_player_id("wvbot-device")
    log(f"own player id: {own_id}")

    log("waiting for a room without a password ...")
    target = wait_for_free_room(api)
    if not target:
        log("no free room appeared")
        return 1

    room_id = api._room_id(target)
    log(f"\njoining room {api._room_name(target)!r} id={room_id}")

    socket = SocketIOClient(
        GAME_HOST,
        query={"firebaseToken": token, "ids": 1, "apiV": 1, "b": nonce()},
        auth={"firebaseToken": token},
        headers={"Origin": "https://app.wolvesville.com"},
    )
    started: dict = {}

    def on_any(event: str, *args: object) -> None:
        payload = args[0] if args else None
        if event == "game-started":
            started["payload"] = payload
            log("   game-started!")
        elif event in ("game-joined", "mg:game-running", "game-set-game-status"):
            log(f"   {event}: {str(payload)[:250]}")

    socket.on_any(on_any)
    socket.start()
    time.sleep(3)

    log(f"   emitting mg:join-open-game: {room_id}")
    socket.emit("mg:join-open-game", json.dumps({"miniGameId": room_id}))

    deadline = time.time() + 120
    while time.time() < deadline and "payload" not in started:
        time.sleep(2)
    time.sleep(5)
    socket.close()

    payload = started.get("payload")
    if not payload:
        log("the game never started")
        return 1

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            pass

    role = payload.get("role") if isinstance(payload, dict) else None
    players = payload.get("players") if isinstance(payload, dict) else []
    log(f"\nrole: {role}")
    log(f"players: {len(players or [])}")
    found = False
    for player in players or []:
        if not isinstance(player, dict):
            continue
        log(f"   {player.get('username')} id={player.get('id')}")
        if own_id and player.get("id") == own_id:
            found = True
    log(f"\nown player id in the list: {found}")

    transcript = state_path("probe_join.log")
    transcript.write_text("\n".join(_lines), encoding="utf-8")
    log(f"transcript written to {transcript}")
    return 0 if found else 2


if __name__ == "__main__":
    raise SystemExit(main())
