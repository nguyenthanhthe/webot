"""HTTP client that speaks the way the official Wolvesville clients do.

Two things matter for the endpoints to answer at all:

* Cloudflare sits in front of every host and rejects requests that do not
  look like a browser. We therefore send a full browser header set.
* The clients send ``Cf-JWT`` (a Cloudflare attestation token), ``ids: 1``
  and ``Authorization: Bearer <firebase idToken>``.

The client is deliberately dependency-free (stdlib ``urllib``) so it runs on
Termux without a compiler.

Only the Wolvesville API hosts are reachable: the target host is checked
against an allowlist and refused if it resolves to a private/loopback
address, and redirects are re-validated instead of followed blindly.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterable

log = logging.getLogger("wvbot.http")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

ALLOWED_HOSTS = frozenset(
    {
        "auth.api-wolvesville.com",
        "core.api-wolvesville.com",
        "game.api-wolvesville.com",
        "game-asia.api-wolvesville.com",
        "api.wolvesville.com",
        # The website, used to notice a changed client bundle.
        "www.wolvesville.com",
    }
)


class HttpError(RuntimeError):
    def __init__(self, status: int, message: str, body: str = ""):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.body = body


class CloudflareChallenge(RuntimeError):
    """Raised when Cloudflare demands a fresh Cf-JWT attestation token."""


class BlockedUrlError(ValueError):
    """Raised when a URL is not allowed to be requested."""


def _is_public_address(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


def validate_url(url: str, allowed_hosts: Iterable[str] = ALLOWED_HOSTS) -> urllib.parse.SplitResult:
    """Validate a URL before it is requested.

    Requires https, requires the host to be on the allowlist, and refuses
    hosts that resolve to a private or loopback address.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise BlockedUrlError(f"only https is allowed, got {parts.scheme!r}")
    host = (parts.hostname or "").lower()
    if not host:
        raise BlockedUrlError("URL has no host")
    if host not in {h.lower() for h in allowed_hosts}:
        raise BlockedUrlError(f"host {host!r} is not allowed")
    if not _is_public_address(host):
        raise BlockedUrlError(f"host {host!r} resolves to a non-public address")
    return parts


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect target against the same allowlist."""

    def __init__(self, allowed_hosts: Iterable[str]):
        super().__init__()
        self.allowed_hosts = tuple(allowed_hosts)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl, self.allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpClient:
    def __init__(
        self,
        timeout: float = 20.0,
        max_retries: int = 3,
        token_provider: Callable[[], str | None] | None = None,
        user_agent: str = BROWSER_UA,
        allowed_hosts: Iterable[str] = ALLOWED_HOSTS,
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self.token_provider = token_provider
        self.user_agent = user_agent
        self.allowed_hosts = tuple(allowed_hosts)
        self._ctx = ssl.create_default_context()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._ctx),
            _ValidatingRedirectHandler(self.allowed_hosts),
        )

    # ------------------------------------------------------------------
    def base_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": "https://app.wolvesville.com",
            "Referer": "https://app.wolvesville.com/",
            "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "ids": "1",
        }
        if extra:
            headers.update({k: v for k, v in extra.items() if v is not None})
        return headers

    def request(
        self,
        method: str,
        url: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
        retry: bool = True,
    ) -> Any:
        """Perform a request and return the decoded JSON body (or None)."""
        validate_url(url, self.allowed_hosts)

        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")

        attempt = 0
        last_exc: Exception | None = None
        while attempt <= self.max_retries:
            attempt += 1
            req = urllib.request.Request(url, data=payload, method=method)
            for key, value in self.base_headers(headers).items():
                req.add_header(key, value)
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                    return self._decode(raw)
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")
                text = raw.lower()
                if exc.code == 403 and "cloudflare jwt" in text:
                    # The official client retries once with a fresh
                    # attestation token.
                    if self.token_provider is not None:
                        log.warning("403 cloudflare jwt - refreshing attestation token")
                        self.token_provider()
                        continue
                    raise CloudflareChallenge("403 cloudflare jwt invalid") from exc
                if exc.code == 403 and "cloudflare" in text:
                    raise CloudflareChallenge("403 blocked by Cloudflare") from exc
                message = raw[:400]
                if exc.code >= 500 and retry:
                    last_exc = HttpError(exc.code, message, raw)
                    time.sleep(min(2 ** attempt, 20))
                    continue
                raise HttpError(exc.code, message, raw) from exc
            except urllib.error.URLError as exc:
                last_exc = exc
                if not retry:
                    raise
                time.sleep(min(2 ** attempt, 20))
                continue

        raise last_exc or RuntimeError("request failed")

    @staticmethod
    def _decode(raw: str) -> Any:
        raw = raw.strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
