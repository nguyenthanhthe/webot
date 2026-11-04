"""The rewards the account can pick up.

The official client offers a golden wheel, the daily rewards and the
loot boxes.  The endpoints came from the web client bundle:

    GET  {core}/rewards/goldenWheelConditions   - what a spin costs
    GET  {core}/rewards/goldenWheelItems/v2      - the wheel
    POST {core}/rewards/goldenWheelSpin          - spin it
    GET  {core}/dailyRewards                     - the daily ladder
    POST {core}/dailyRewards                     - claim the daily reward
    GET  {core}/inventory/lootBoxes/openAll      - open every loot box

Everything here is optional and off by default: a spin costs roses and
a claim gives gold, so the operator has to ask for it.
"""

from __future__ import annotations

import logging
from typing import Any

from .http_client import HttpClient

log = logging.getLogger("wvbot.rewards")

CORE_URL = "https://core.api-wolvesville.com"


class Rewards:
    """Claims the daily rewards and spins the golden wheel."""

    def __init__(self, cfg: Any, http: HttpClient, auth_headers: dict[str, str] | None = None):
        self.cfg = cfg
        self.http = http
        self.auth_headers = dict(auth_headers or {})

    # ------------------------------------------------------------------
    def golden_wheel_conditions(self) -> dict[str, Any]:
        """What a spin costs and what it can win."""
        data = self.http.request(
            "GET",
            f"{CORE_URL}/rewards/goldenWheelConditions",
            headers=self.auth_headers,
        )
        return data if isinstance(data, dict) else {}

    def golden_wheel_state(self) -> dict[str, Any]:
        """The wheel and whether it can be spun."""
        data = self.http.request(
            "GET",
            f"{CORE_URL}/rewards/goldenWheelItems/v2",
            headers=self.auth_headers,
        )
        return data if isinstance(data, dict) else {}

    def spin_golden_wheel(self) -> list[Any]:
        """Spin the wheel.  Costs roses, so the caller has to want it."""
        data = self.http.request(
            "POST",
            f"{CORE_URL}/rewards/goldenWheelSpin",
            headers=self.auth_headers,
        )
        if isinstance(data, list):
            return data
        return []

    # ------------------------------------------------------------------
    def daily_rewards(self) -> list[dict[str, Any]]:
        """The daily ladder, with what is already claimed."""
        data = self.http.request(
            "GET",
            f"{CORE_URL}/dailyRewards",
            headers=self.auth_headers,
        )
        if isinstance(data, dict):
            rewards = data.get("rewards")
            if isinstance(rewards, list):
                return [r for r in rewards if isinstance(r, dict)]
        return []

    def claim_daily_reward(self) -> dict[str, Any]:
        """Claim the reward of the day."""
        data = self.http.request(
            "POST",
            f"{CORE_URL}/dailyRewards",
            headers=self.auth_headers,
        )
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------
    def open_loot_boxes(self) -> list[Any]:
        """Open every loot box in the inventory."""
        data = self.http.request(
            "GET",
            f"{CORE_URL}/inventory/lootBoxes/openAll",
            headers=self.auth_headers,
        )
        if isinstance(data, dict):
            items = data.get("items")
            if isinstance(items, list):
                return items
        return []

    # ------------------------------------------------------------------
    def describe_daily(self) -> str:
        """A short summary of the daily ladder, for the log."""
        rewards = self.daily_rewards()
        if not rewards:
            return "no daily rewards"
        parts: list[str] = []
        for reward in rewards:
            kind = str(reward.get("type") or "?")
            amount = reward.get("amount")
            claimed = bool(reward.get("claimed"))
            mark = "claimed" if claimed else "open"
            parts.append(f"{kind}x{amount} ({mark})")
        return ", ".join(parts)
