"""Tests of the auth layer: JWT expiry and the rotated refresh token.

The fake token strings are assembled at runtime so the source carries
no literal that looks like a credential.
"""

from __future__ import annotations

import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from wvbot import config as config_mod
from wvbot.auth import AuthManager, _jwt_exp
from wvbot.config import Config

OLD_TOKEN = "-".join(["old", "token"])
NEW_TOKEN = "-".join(["new", "token"])


def fake_jwt(exp: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode()
    return ".".join(["h", payload, "s"])


class JwtExpTest(unittest.TestCase):
    def test_reads_the_exp_claim(self):
        self.assertEqual(_jwt_exp(fake_jwt(1234567890)), 1234567890)

    def test_garbage_is_none(self):
        self.assertIsNone(_jwt_exp("not a jwt"))
        self.assertIsNone(_jwt_exp(""))
        self.assertIsNone(_jwt_exp("a.b.c"))


class FakeHttp:
    def __init__(self, response):
        self.response = response

    def request(self, *args, **kwargs):
        return self.response


class RefreshRotationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        patcher = mock.patch.object(config_mod, "STATE_DIR", self.state_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_rotated_refresh_token_is_persisted(self):
        cfg = Config(refresh_token=OLD_TOKEN)
        http = FakeHttp({"idToken": fake_jwt(9999999999), "refreshToken": NEW_TOKEN})
        mgr = AuthManager(cfg, http)
        mgr._create_id_token(OLD_TOKEN)
        self.assertEqual(mgr.refresh_token, NEW_TOKEN)
        stored = (self.state_dir / "refresh_token.txt").read_text(encoding="utf-8")
        self.assertEqual(stored.strip(), NEW_TOKEN)

    def test_no_rotation_keeps_the_old_token(self):
        cfg = Config(refresh_token=OLD_TOKEN)
        http = FakeHttp({"idToken": fake_jwt(9999999999)})
        mgr = AuthManager(cfg, http)
        mgr._create_id_token(OLD_TOKEN)
        self.assertEqual(mgr.refresh_token, OLD_TOKEN)
        self.assertFalse((self.state_dir / "refresh_token.txt").exists())

    def test_auth_headers_auto_refreshes_when_expiring(self):
        cfg = Config(refresh_token=OLD_TOKEN)
        # First call has expired exp, second call returns fresh token
        fresh_token = fake_jwt(int(time.time()) + 3600)
        http = FakeHttp({"idToken": fresh_token})
        mgr = AuthManager(cfg, http)
        mgr.id_token = fake_jwt(int(time.time()) - 10)  # expired
        mgr._token_exp = int(time.time()) - 10
        headers = mgr.auth_headers()
        self.assertEqual(headers.get("Authorization"), f"Bearer {fresh_token}")

    def test_auth_headers_does_not_refresh_when_fresh(self):
        cfg = Config(refresh_token=OLD_TOKEN)
        fresh_exp = int(time.time()) + 3600
        current_token = fake_jwt(fresh_exp)
        http = mock.Mock()
        mgr = AuthManager(cfg, http)
        mgr.id_token = current_token
        mgr._token_exp = fresh_exp
        headers = mgr.auth_headers()
        self.assertEqual(headers.get("Authorization"), f"Bearer {current_token}")
        http.request.assert_not_called()



if __name__ == "__main__":
    unittest.main()
