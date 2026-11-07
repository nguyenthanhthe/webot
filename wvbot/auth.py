"""Authentication against auth.api-wolvesville.com.

Three endpoints matter:

* ``POST /players/signInWithEmailAndPassword``
  body ``{email, password, oneTimePassword?}`` -> ``{idToken, refreshToken}``
* ``POST /players/createIdToken``
  body ``{refreshToken}`` -> a fresh ``idToken`` (no Turnstile needed)
* ``POST /cloudflareTurnstile/verify``
  body ``{token, siteKey, idToken}`` -> ``{jwt}`` which becomes the
  ``Cf-JWT`` header.

A ``refreshToken`` is the durable credential: once we hold one, the bot can
mint idTokens indefinitely and only needs the Turnstile attestation when
Cloudflare challenges a request.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

from .config import AUTH_URL, Config, TURNSTILE_SITEKEY_MANAGED
from .http_client import CloudflareChallenge, HttpClient, HttpError

log = logging.getLogger("wvbot.auth")


def _jwt_exp(token: str) -> int | None:
    """Best-effort expiry of a JWT (seconds since epoch)."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        return int(exp) if exp else None
    except Exception:  # noqa: BLE001 - expiry is advisory only
        return None


class AuthManager:
    def __init__(self, cfg: Config, http: HttpClient, account_dir: Path | None = None):
        self.cfg = cfg
        self.http = http
        self.account_dir = account_dir
        self.id_token: str | None = None
        self.refresh_token: str | None = cfg.refresh_token or None
        self.cf_jwt: str | None = cfg.cf_jwt or None
        self._token_exp: int | None = None

    # ------------------------------------------------------------------
    def refresh_cf_jwt(self) -> str | None:
        """Re-read the attestation token written by the Turnstile helper."""
        from .config import state_path

        try:
            path = state_path(self.cfg.cf_jwt_file, base=self.account_dir)
        except ValueError:
            return self.cf_jwt
        if path.is_file():
            token = path.read_text(encoding="utf-8").strip()
            if token:
                self.cf_jwt = token
        return self.cf_jwt

    def auth_headers(self) -> dict[str, str]:
        # If we hold a token that is expired or expiring within 300s, refresh it
        if self.id_token and self._token_exp and (self._token_exp - int(time.time()) < 300):
            try:
                self.ensure_token()
            except Exception as exc:  # noqa: BLE001
                log.warning("automatic token refresh in auth_headers failed: %s", exc)
        headers: dict[str, str] = {}
        if self.id_token:
            headers["Authorization"] = f"Bearer {self.id_token}"
        if self.cf_jwt:
            headers["Cf-JWT"] = self.cf_jwt
        return headers

    # ------------------------------------------------------------------
    def login(self) -> str:
        """Obtain a usable idToken. Raises on failure."""
        if self.refresh_token:
            try:
                return self._create_id_token(self.refresh_token)
            except (HttpError, CloudflareChallenge) as exc:
                log.warning("refresh token path failed (%s); falling back to sign-in", exc)

        if not (self.cfg.email and self.cfg.password):
            raise RuntimeError(
                "no credentials: set refresh_token, or email + password"
            )
        return self.sign_in(self.cfg.email, self.cfg.password)

    def sign_in(self, email: str, password: str, one_time_password: str | None = None) -> str:
        body: dict[str, Any] = {"email": email, "password": password}
        if one_time_password:
            body["oneTimePassword"] = one_time_password

        headers: dict[str, str] = {}
        if self.cf_jwt:
            headers["Cf-JWT"] = self.cf_jwt

        data = self.http.request(
            "POST", f"{AUTH_URL}/players/signInWithEmailAndPassword", body, headers
        )
        if not isinstance(data, dict):
            raise RuntimeError(f"unexpected sign-in response: {data!r}")

        id_token = data.get("idToken") or data.get("firebaseIdToken")
        refresh = data.get("refreshToken")
        if refresh:
            self.refresh_token = refresh
        if not id_token:
            raise RuntimeError(f"sign-in response had no idToken: {list(data)}")

        self.id_token = id_token
        self._token_exp = _jwt_exp(id_token)
        log.info("signed in, token expires at %s", self._token_exp)
        return id_token

    def _create_id_token(self, refresh_token: str) -> str:
        headers: dict[str, str] = {}
        if self.cf_jwt:
            headers["Cf-JWT"] = self.cf_jwt

        data = self.http.request(
            "POST", f"{AUTH_URL}/players/createIdToken", {"refreshToken": refresh_token}, headers
        )
        if not isinstance(data, dict):
            raise RuntimeError(f"unexpected createIdToken response: {data!r}")
        id_token = data.get("idToken") or data.get("firebaseIdToken")
        if not id_token:
            raise RuntimeError(f"createIdToken had no idToken: {list(data)}")

        new_refresh = data.get("refreshToken")
        if new_refresh:
            self.refresh_token = new_refresh
            self._persist_refresh_token(new_refresh)
        self.id_token = id_token
        self._token_exp = _jwt_exp(id_token)
        log.info("minted a new idToken (expires at %s)", self._token_exp)
        return id_token

    def _persist_refresh_token(self, token: str) -> None:
        """Keep the durable credential current on disk.

        Firebase can hand out a rotated refresh token: the fresh one is
        then the only valid credential, so it is written back to the
        state file the moment it arrives.
        """
        try:
            from .config import REFRESH_TOKEN_FILE, state_path

            path = state_path(REFRESH_TOKEN_FILE, base=self.account_dir)
            path.write_text(token + "\n", encoding="utf-8")
            try:
                import os

                os.chmod(path, 0o600)
            except OSError:  # noqa: F841 - advisory only (e.g. Windows)
                pass
        except OSError as exc:
            log.warning("could not store the rotated refresh token: %s", exc)

    # ------------------------------------------------------------------
    def ensure_token(self, force: bool = False) -> str:
        """Return a valid idToken, minting a new one when it is stale."""
        now = int(time.time())
        if (
            not force
            and self.id_token
            and self._token_exp
            and self._token_exp - now > 300
        ):
            return self.id_token
        if self.refresh_token:
            return self._create_id_token(self.refresh_token)
        return self.login()

    def exchange_turnstile_token(self, turnstile_token: str, id_token: str | None = None) -> str:
        """Trade a solved Turnstile token for a Cf-JWT attestation token."""
        data = self.http.request(
            "POST",
            f"{AUTH_URL}/cloudflareTurnstile/verify",
            {
                "token": turnstile_token,
                "siteKey": TURNSTILE_SITEKEY_MANAGED,
                "idToken": id_token or self.id_token,
            },
        )
        if not isinstance(data, dict) or not data.get("jwt"):
            raise RuntimeError(f"turnstile verify failed: {data!r}")
        self.cf_jwt = data["jwt"]
        return self.cf_jwt
