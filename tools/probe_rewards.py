"""Check the reward endpoints of the core API.

Run inside Termux:
    python3 probe_rewards.py

It only reads: the wheel conditions, the wheel items and the loot box
list.  Nothing is spun and nothing is opened - that is the job of the
bot, and only when the operator asks for it.
"""

from __future__ import annotations

import json
import sys

sys.path.insert(0, ".")

from wvbot.auth import AuthManager  # noqa: E402
from wvbot.config import load_config  # noqa: E402
from wvbot.http_client import HttpClient  # noqa: E402

CORE = "https://core.api-wolvesville.com"

PATHS = [
    ("GET", "/rewards/goldenWheelConditions", None),
    ("GET", "/rewards/goldenWheelItems/v2", None),
    ("GET", "/rewards/wheelItems/v2", None),
    ("GET", "/inventory/lootBoxes", None),
    ("GET", "/dailyRewards", None),
    ("GET", "/honor/seasonAndProgress", None),
    ("GET", "/players/self", None),
]


def main() -> int:
    cfg = load_config()
    http = HttpClient()
    auth = AuthManager(cfg, http)
    token = auth.ensure_token()
    headers = auth.auth_headers()
    print(f"token length {len(token)}")

    for method, path, body in PATHS:
        url = f"{CORE}{path}"
        try:
            result = http.request(method, url, body=body, headers=headers)
            text = json.dumps(result)[:600] if result is not None else "null"
            print(f"\n{method} {path}\n   {text}")
        except Exception as exc:  # noqa: BLE001
            print(f"\n{method} {path}\n   failed: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
