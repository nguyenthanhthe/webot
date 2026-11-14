"""Tests of the rewards API helper."""

from __future__ import annotations

import unittest
from unittest import mock

from wvbot.config import CORE_URL, Config
from wvbot.rewards import Rewards


class RewardsTest(unittest.TestCase):
    def test_auth_headers_dict_and_callable_and_setter(self) -> None:
        http = mock.Mock()
        r_dict = Rewards(Config(), http, {"Authorization": "Bearer static"})
        self.assertEqual(r_dict.auth_headers, {"Authorization": "Bearer static"})

        tok = "one"
        r_call = Rewards(Config(), http, lambda: {"Authorization": f"Bearer {tok}"})
        self.assertEqual(r_call.auth_headers, {"Authorization": "Bearer one"})
        tok = "two"
        self.assertEqual(r_call.auth_headers, {"Authorization": "Bearer two"})

        r_call.auth_headers = {"Authorization": "Bearer replaced"}
        self.assertEqual(r_call.auth_headers, {"Authorization": "Bearer replaced"})

    def test_wheel_daily_and_loot_boxes(self) -> None:
        http = mock.Mock()
        r = Rewards(Config(), http, lambda: {"Authorization": "Bearer live"})

        http.request.return_value = {"spinCost": 5}
        self.assertEqual(r.golden_wheel_conditions(), {"spinCost": 5})
        http.request.assert_called_with(
            "GET",
            f"{CORE_URL}/rewards/goldenWheelConditions",
            headers={"Authorization": "Bearer live"},
        )

        http.request.return_value = {"items": ["rose"]}
        self.assertEqual(r.golden_wheel_state(), {"items": ["rose"]})

        http.request.return_value = ["prize"]
        self.assertEqual(r.spin_golden_wheel(), ["prize"])
        http.request.return_value = {"unexpected": True}
        self.assertEqual(r.spin_golden_wheel(), [])

        http.request.return_value = {"rewards": [{"type": "gold", "amount": 50}, "bad"]}
        self.assertEqual(r.daily_rewards(), [{"type": "gold", "amount": 50}])

        http.request.return_value = {"claimed": True}
        self.assertEqual(r.claim_daily_reward(), {"claimed": True})

        http.request.return_value = {"lootBoxes": [{"id": 1}, {"id": 2}]}
        self.assertEqual(r.loot_box_count(), 2)

        http.request.return_value = {"items": ["skin"]}
        self.assertEqual(r.open_loot_boxes(), ["skin"])


if __name__ == "__main__":
    unittest.main()
