"""See what the daily reward endpoint does when the reward is claimed.

Run inside Termux:

    python3 probe_daily.py

It reads the ladder, claims the reward and reads the ladder again, so
it is visible whether the server marks the reward as claimed.
"""

from __future__ import annotations

import json
import sys

sys.path.insert(0, ".")

from wvbot.auth import AuthManager  # noqa: E402
from wvbot.config import load_config  # noqa: E402
from wvbot.http_client import HttpClient  # noqa: E402
from wvbot.rewards import Rewards  # noqa: E402


def show(ladder: list[dict]) -> None:
    for item in ladder[:6]:
        print(
            "   ",
            item.get("type"),
            "x",
            item.get("amount"),
            "claimed=",
            item.get("claimed"),
            "canBeClaimedDate=",
            item.get("canBeClaimedDate"),
        )


def main() -> int:
    cfg = load_config()
    http = HttpClient()
    auth = AuthManager(cfg, http)
    auth.ensure_token()
    rewards = Rewards(cfg, http, auth.auth_headers())

    print("BEFORE:")
    show(rewards.daily_rewards())

    try:
        result = rewards.claim_daily_reward()
        print("CLAIM answered:", json.dumps(result)[:300])
    except Exception as exc:  # noqa: BLE001
        print("CLAIM failed:", exc)

    print("AFTER:")
    show(rewards.daily_rewards())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
