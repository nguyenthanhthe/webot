"""Check the version endpoint of the game.

Run inside Termux:
    python3 probe_versions.py

It asks the core API which build it expects.  A changed answer means
the game was patched and the protocol may have moved with it.
"""

from __future__ import annotations

import json
import sys

sys.path.insert(0, ".")

from wvbot.auth import AuthManager  # noqa: E402
from wvbot.config import load_config  # noqa: E402
from wvbot.http_client import HttpClient  # noqa: E402

CORE = "https://core.api-wolvesville.com"


def main() -> int:
    cfg = load_config()
    http = HttpClient()
    auth = AuthManager(cfg, http)
    token = auth.ensure_token()
    headers = auth.auth_headers()

    for version in ("0", "1", "99999"):
        url = f"{CORE}/appVersions/check"
        body = {"versionNumber": version, "platform": "web"}
        print(f"\nversionNumber={version}")
        try:
            result = http.request("PUT", url, body=body, headers=headers)
            print("   ", json.dumps(result)[:600])
        except Exception as exc:  # noqa: BLE001
            print("   failed:", exc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
