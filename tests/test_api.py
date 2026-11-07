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


if __name__ == "__main__":
    unittest.main()
