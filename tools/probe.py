"""Protocol probe: verifies the WebSocket/socket.io layer against the live server.

Run from the workspace root:

    python wv-bot/tools/probe.py

It does three things:
1. opens an Engine.IO v4 / socket.io v4 handshake against the game host
2. reports whether the namespace accepted the connection
3. calls the custom-game REST endpoint to confirm Cloudflare lets us through
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wvbot.config import GAME_URL, load_config  # noqa: E402
from wvbot.http_client import HttpClient  # noqa: E402
from wvbot.sio import SocketIOClient  # noqa: E402


def probe_socket() -> None:
    print("== socket.io handshake ==")
    client = SocketIOClient(
        GAME_URL,
        query={"ids": 1, "apiV": 1},
        auth={"firebaseToken": "probe-token"},
        headers={"Origin": "https://app.wolvesville.com"},
    )
    try:
        client.connect()
        print(f"connected={client.connected} sid={client.sid}")
        client.close()
    except Exception as exc:  # noqa: BLE001
        print(f"handshake result: {type(exc).__name__}: {exc}")


def probe_rest() -> None:
    print("\n== REST custom game list ==")
    cfg = load_config()
    http = HttpClient(timeout=cfg.request_timeout_sec, max_retries=1)
    url = f"{cfg.game_base_url}/api/public/game/custom?language={cfg.language}"
    try:
        data = http.request("GET", url, headers={})
        if isinstance(data, dict):
            games = data.get("openGames")
            print(f"http ok, openGames={len(games) if isinstance(games, list) else 'n/a'}")
            if isinstance(games, list) and games:
                sample = games[0]
                print("sample room keys:", sorted(sample.keys())[:25])
                print("sample room:", {k: sample.get(k) for k in list(sample)[:8]})
        else:
            print("response:", str(data)[:300])
    except Exception as exc:  # noqa: BLE001
        print(f"rest result: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    started = time.time()
    probe_socket()
    probe_rest()
    print(f"\ndone in {time.time() - started:.1f}s")
