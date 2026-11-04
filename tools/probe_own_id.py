"""Read the own player id from the profile (diagnostic).

Run inside Termux:
    python3 probe_own_id.py
"""

from __future__ import annotations

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
    print("idToken length:", len(token))

    api = GameApi(cfg, http, auth.auth_headers())
    try:
        own = api.get_own_player_id("wvbot-device")
        print("own player id:", own)
    except Exception as exc:  # noqa: BLE001
        print("FAILED:", exc)
        return 1

    print("\nraw profile response:")
    raw = http.request(
        "PUT",
        "https://core.api-wolvesville.com/players/meAndCheckAppVersion",
        body={
            "versionNumber": "0",
            "platform": "web",
            "steamId": "",
            "fcmToken": "",
            "pushTokenDeletedOnLogout": True,
            "deviceId": "wvbot-device",
            "deviceSecurityLevel": "UNKNOWN",
            "locale": "en",
        },
        headers=auth.auth_headers(),
    )
    text = str(raw)
    print(text[:1200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
