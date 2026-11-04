"""Check the XP booster inventory of the account.

Run inside Termux:
    python3 probe_boosters.py

The booster is the "free 2x XP" of the reference bot: a booster item in
the inventory that can be activated for a number of hours.  This probe
only reads, so nothing is claimed and nothing is spent.
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
    ("GET", "/inventory/xpBoosters", None),
    ("GET", "/inventory", None),
    ("GET", "/gemOffers/purchases", None),
    ("GET", "/rewards/goldenWheelConditions", None),
]


def main() -> int:
    cfg = load_config()
    http = HttpClient()
    auth = AuthManager(cfg, http)
    token = auth.ensure_token()
    headers = auth.auth_headers()

    for method, path, body in PATHS:
        url = f"{CORE}{path}"
        print(f"\n{method} {path}")
        try:
            result = http.request(method, url, body=body, headers=headers)
            text = json.dumps(result)[:900] if result is not None else "null"
            print(f"   {text}")
        except Exception as exc:  # noqa: BLE001
            print(f"   failed: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
