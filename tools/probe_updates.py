"""Check the update watcher.

Run inside Termux:
    python3 probe_updates.py

It reads the build number of the game and the hash of the client
bundle, and reports what changed since the last run.
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from wvbot.auth import AuthManager  # noqa: E402
from wvbot.config import load_config  # noqa: E402
from wvbot.http_client import HttpClient  # noqa: E402
from wvbot.updates import UpdateWatcher  # noqa: E402


def main() -> int:
    cfg = load_config()
    http = HttpClient()
    auth = AuthManager(cfg, http)
    token = auth.ensure_token()

    watcher = UpdateWatcher(cfg, http, auth.auth_headers())
    print("first check:", watcher.check())
    print("second check:", watcher.check())

    profile = auth.auth_headers()
    print("headers carry the token:", "Authorization" in profile)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
