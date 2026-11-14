"""Tests of the GameApi layer."""

from __future__ import annotations

import unittest
from unittest import mock

from wvbot.api import GameApi
from wvbot.config import Config


class GameApiHeadersTest(unittest.TestCase):
    def test_static_dict_headers(self) -> None:
        cfg = Config()
        http = mock.Mock()
        api = GameApi(cfg, http, {"Authorization": "Bearer static"})
        self.assertEqual(api.auth_headers, {"Authorization": "Bearer static"})

    def test_callable_headers(self) -> None:
        cfg = Config()
        http = mock.Mock()
        token = "first"

        def get_headers() -> dict[str, str]:
            return {"Authorization": f"Bearer {token}"}

        api = GameApi(cfg, http, get_headers)
        self.assertEqual(api.auth_headers, {"Authorization": "Bearer first"})

        token = "second"
        self.assertEqual(api.auth_headers, {"Authorization": "Bearer second"})

    def test_auth_headers_setter(self) -> None:
        cfg = Config()
        http = mock.Mock()
        api = GameApi(cfg, http, {"Authorization": "Bearer initial"})
        self.assertEqual(api.auth_headers, {"Authorization": "Bearer initial"})
        api.auth_headers = {"Authorization": "Bearer updated"}
        self.assertEqual(api.auth_headers, {"Authorization": "Bearer updated"})

    def test_fold_mathematical_unicode_fonts(self) -> None:
        # Fancy Unicode math double-struck characters used in room names like "🌺|𝕍𝕚𝕝𝕝 𝕎𝕚𝕟 𝔼 TF"
        folded = GameApi.fold("🌺|𝕍𝕚𝕝𝕝 𝕎𝕚𝕟 𝔼 TF")
        self.assertIn("vill win", folded)
        self.assertIn("tf", folded)

    def test_query_parameters_are_url_encoded(self) -> None:
        cfg = Config()
        http = mock.Mock()
        http.request.return_value = {}
        api = GameApi(cfg, http, {"Authorization": "Bearer t"})

        api.list_custom_games("en&admin=1")
        self.assertIn("?language=en%26admin%3D1", http.request.call_args[0][1])

        api.check_password_presence("id/with?x=1")
        self.assertIn("?gameId=id%2Fwith%3Fx%3D1", http.request.call_args[0][1])

        api.get_game_settings("g&1=2", locale="en/US")
        self.assertIn(
            "?gameId=g%261%3D2&locale=en%2FUS",
            http.request.call_args[0][1],
        )

    def test_find_rooms_filters_regular_xp_and_locked_rooms(self) -> None:
        cfg = Config(allow_password_rooms=False)
        http = mock.Mock()
        http.request.return_value = {
            "openGames": [
                {"name": "Vill Win Free", "regularXp": True, "hasPassword": False},
                {"name": "Vill Win Locked", "regularXp": True, "hasPassword": True},
                {"name": "Vill Win ZeroXP", "regularXp": False, "hasPassword": False},
                {"name": "Casual", "regularXp": True, "hasPassword": False},
            ]
        }
        api = GameApi(cfg, http, {})
        rooms = api.find_rooms(["vill win"])
        self.assertEqual([r["name"] for r in rooms], ["Vill Win Free"])
        self.assertEqual(api.last_matching, 2)
        self.assertEqual(api.last_locked, 1)


if __name__ == "__main__":
    unittest.main()
