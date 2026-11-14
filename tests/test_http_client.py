"""Tests of the HttpClient URL validation, retries, and Cf-JWT refresh."""

from __future__ import annotations

import io
import socket
import urllib.error
import unittest
from unittest import mock

import wvbot.http_client as http_mod
from wvbot.http_client import (
    BlockedUrlError,
    CloudflareChallenge,
    HttpClient,
    HttpError,
    validate_url,
)


def _http_error(url: str, code: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url,
        code,
        "err",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(body.encode("utf-8")),
    )


class _FakeResp:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *args) -> None:
        pass


class ValidateUrlTest(unittest.TestCase):
    def test_rejects_non_https_and_unlisted_hosts(self) -> None:
        with self.assertRaises(BlockedUrlError):
            validate_url("http://core.api-wolvesville.com/dailyRewards")
        with self.assertRaises(BlockedUrlError):
            validate_url("https://evil.example.com/steal")
        with self.assertRaises(BlockedUrlError):
            validate_url("https:///no-host")

    def test_rejects_private_and_loopback_dns_resolutions(self) -> None:
        for ip in ("127.0.0.1", "10.0.0.5", "169.254.169.254"):
            addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]
            with mock.patch("socket.getaddrinfo", return_value=addr):
                with self.assertRaises(BlockedUrlError):
                    validate_url("https://core.api-wolvesville.com/inventory")

    def test_allows_public_allowlisted_host(self) -> None:
        addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        with mock.patch("socket.getaddrinfo", return_value=addr):
            parts = validate_url("https://core.api-wolvesville.com/inventory")
        self.assertEqual(parts.hostname, "core.api-wolvesville.com")


class HttpClientRetryTest(unittest.TestCase):
    def _client(self, max_retries: int = 2, token_provider=None) -> HttpClient:
        return HttpClient(max_retries=max_retries, token_provider=token_provider)

    def test_403_cloudflare_jwt_assigns_fresh_token_and_retries(self) -> None:
        url = "https://core.api-wolvesville.com/inventory"
        seen_headers: list[dict[str, str]] = []
        caller_headers = {"Authorization": "Bearer id", "Cf-JWT": "stale-jwt"}

        def fake_open(req, timeout=20.0):
            hdrs = {k.lower(): v for k, v in req.header_items()}
            seen_headers.append(hdrs)
            if len(seen_headers) == 1:
                raise _http_error(url, 403, '{"message":"Invalid cloudflare jwt"}')
            return _FakeResp('{"ok": true}')

        client = self._client(max_retries=2, token_provider=lambda: "fresh-jwt")
        with mock.patch.object(http_mod, "_is_public_address", return_value=True):
            with mock.patch.object(client._opener, "open", side_effect=fake_open):
                out = client.request("GET", url, headers=caller_headers)

        self.assertEqual(out, {"ok": True})
        self.assertEqual(seen_headers[0]["cf-jwt"], "stale-jwt")
        self.assertEqual(seen_headers[1]["cf-jwt"], "fresh-jwt")
        # Caller dict is not mutated in-place.
        self.assertEqual(caller_headers["Cf-JWT"], "stale-jwt")

    def test_403_cloudflare_jwt_raises_challenge_when_retries_exhaust(self) -> None:
        url = "https://core.api-wolvesville.com/inventory"
        provider = mock.Mock(return_value="still-bad")
        client = self._client(max_retries=2, token_provider=provider)

        def always_403(req, timeout=20.0):
            raise _http_error(url, 403, "Invalid cloudflare jwt")

        with mock.patch.object(http_mod, "_is_public_address", return_value=True):
            with mock.patch.object(client._opener, "open", side_effect=always_403):
                with self.assertRaises(CloudflareChallenge):
                    client.request("GET", url)
        self.assertEqual(provider.call_count, 2)

    def test_403_cloudflare_jwt_raises_challenge_when_provider_returns_empty(self) -> None:
        url = "https://core.api-wolvesville.com/inventory"
        client = self._client(max_retries=2, token_provider=lambda: None)
        with mock.patch.object(http_mod, "_is_public_address", return_value=True):
            with mock.patch.object(
                client._opener,
                "open",
                side_effect=_http_error(url, 403, "Invalid cloudflare jwt"),
            ):
                with self.assertRaises(CloudflareChallenge):
                    client.request("GET", url)

    def test_403_cloudflare_without_jwt_and_500_retry(self) -> None:
        url = "https://core.api-wolvesville.com/inventory"
        client = self._client(max_retries=1)
        with mock.patch.object(http_mod, "_is_public_address", return_value=True):
            with mock.patch.object(
                client._opener,
                "open",
                side_effect=_http_error(url, 403, "Attention Required! | Cloudflare"),
            ):
                with self.assertRaises(CloudflareChallenge):
                    client.request("GET", url)

            with mock.patch("time.sleep"):
                with mock.patch.object(
                    client._opener,
                    "open",
                    side_effect=[
                        _http_error(url, 502, "bad gateway"),
                        _FakeResp('"plain text"'),
                    ],
                ):
                    self.assertEqual(client.request("GET", url), "plain text")

            with mock.patch("time.sleep"):
                with mock.patch.object(
                    client._opener,
                    "open",
                    side_effect=_http_error(url, 500, "boom"),
                ):
                    with self.assertRaises(HttpError) as ctx:
                        client.request("GET", url, retry=False)
                    self.assertEqual(ctx.exception.status, 500)


if __name__ == "__main__":
    unittest.main()
