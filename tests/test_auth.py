"""Tests of the auth layer: JWT expiry and the rotated refresh token.

The fake token strings are assembled at runtime so the source carries
no literal that looks like a credential.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from wvbot import config as config_mod
from wvbot.auth import AuthManager, _jwt_exp
from wvbot.config import Config
from wvbot.http_client import HttpError

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

    def test_sign_in_persists_refresh_token_and_permissions(self):
        cfg = Config()
        tok = fake_jwt(int(time.time()) + 3600)
        http = FakeHttp({"idToken": tok, "refreshToken": NEW_TOKEN})
        mgr = AuthManager(cfg, http)
        got = mgr.sign_in("u@example.com", "pw")
        self.assertEqual(got, tok)
        token_file = self.state_dir / "refresh_token.txt"
        self.assertTrue(token_file.is_file())
        self.assertEqual(token_file.read_text(encoding="utf-8").strip(), NEW_TOKEN)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o600)

    def test_ensure_token_falls_back_to_sign_in_when_refresh_fails(self):
        cfg = Config(email="u@example.com", password="pw", refresh_token=OLD_TOKEN)
        tok = fake_jwt(int(time.time()) + 3600)

        def request_fn(method, url, body=None, headers=None, retry=True):
            if url.endswith("/createIdToken"):
                raise HttpError(400, "TOKEN_EXPIRED", "{}")
            if url.endswith("/signInWithEmailAndPassword"):
                return {"idToken": tok, "refreshToken": NEW_TOKEN}
            raise AssertionError(f"unexpected url: {url}")

        http = mock.Mock()
        http.request.side_effect = request_fn
        mgr = AuthManager(cfg, http)
        self.assertEqual(mgr.ensure_token(), tok)
        self.assertEqual(mgr.refresh_token, NEW_TOKEN)
        self.assertEqual(
            (self.state_dir / "refresh_token.txt").read_text(encoding="utf-8").strip(),
            NEW_TOKEN,
        )

    def test_ensure_token_raises_when_refresh_fails_without_credentials(self):
        cfg = Config(refresh_token=OLD_TOKEN)
        http = mock.Mock()
        http.request.side_effect = HttpError(400, "TOKEN_EXPIRED", "{}")
        mgr = AuthManager(cfg, http)
        with self.assertRaises(HttpError):
            mgr.ensure_token()

    def test_auth_headers_is_thread_safe_under_concurrent_refresh(self):
        cfg = Config(refresh_token=OLD_TOKEN)
        fresh = fake_jwt(int(time.time()) + 3600)
        http = mock.Mock()
        http.request.return_value = {"idToken": fresh, "refreshToken": NEW_TOKEN}
        mgr = AuthManager(cfg, http)
        mgr.id_token = fake_jwt(int(time.time()) - 10)
        mgr._token_exp = int(time.time()) - 10

        results: list[dict[str, str]] = []
        errors: list[Exception] = []

        def worker() -> None:
            try:
                results.append(mgr.auth_headers())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertTrue(all(r.get("Authorization") == f"Bearer {fresh}" for r in results))



if __name__ == "__main__":
    unittest.main()
