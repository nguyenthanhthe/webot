"""Temporary email clients for account provisioning (stdlib only).

Supports both self-hosted ``dreamhunter2333/cloudflare_temp_email`` Workers
(``TempEmailClient``) and the zero-config public ``api.mail.tm`` service
(``MailTmClient``).
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import random
import re
import string
import sys
import time
import urllib.error
import urllib.request
from typing import Any, NamedTuple
from urllib.parse import urlsplit

log = logging.getLogger("wvbot.temp_email")


class Mailbox(NamedTuple):
    address: str
    jwt: str


class TempEmailError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _validate_http_url(url: str, label: str = "URL") -> str:
    raw = (url or "").strip().rstrip("/")
    parts = urlsplit(raw)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise TempEmailError(f"Invalid {label} (must use http or https): {url!r}")
    return raw


def _http_json(
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None = None,
    timeout: float = 15.0,
    label: str = "HTTP",
) -> Any:
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise TempEmailError(f"Refusing non-HTTP URL: {url!r}")

    req_headers = dict(headers)
    data = None
    if payload is not None:
        req_headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        finally:
            exc.close()
        raise TempEmailError(
            f"{label} {exc.code} for {url}: {err_body}",
            status_code=exc.code,
        ) from exc
    except Exception as exc:
        raise TempEmailError(f"{label} request failed for {url}: {exc}") from exc


class TempEmailClient:
    """Interface to cloudflare_temp_email Worker REST API."""

    def __init__(
        self,
        base_url: str,
        admin_password: str | None = None,
        site_password: str | None = None,
        timeout: float = 15.0,
    ):
        self.base_url = _validate_http_url(base_url, "base_url")
        self.admin_password = admin_password
        self.site_password = site_password
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        jwt: str | None = None,
        use_admin: bool = False,
    ) -> Any:
        url = f"{self.base_url}{path}"
        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "wvbot-provisioner/1.0",
        }
        if jwt:
            headers["Authorization"] = f"Bearer {jwt}"
        if use_admin and self.admin_password:
            headers["x-admin-auth"] = self.admin_password
        if self.site_password:
            headers["x-custom-auth"] = self.site_password
        return _http_json(
            method,
            url,
            headers,
            payload=payload,
            timeout=self.timeout,
            label="HTTP",
        )

    def create_mailbox(
        self,
        name: str | None = None,
        domain: str | None = None,
    ) -> Mailbox:
        """Create a new address via the admin endpoint."""
        body: dict[str, Any] = {"enablePrefix": True}
        if name:
            body["name"] = name
        if domain:
            body["domain"] = domain

        data = self._request("POST", "/admin/new_address", payload=body, use_admin=True)
        if not isinstance(data, dict):
            raise TempEmailError(f"Unexpected response from new_address: {data}")
        address = str(data.get("address") or "")
        jwt = str(data.get("jwt") or "")
        if not address or not jwt:
            raise TempEmailError(f"Missing address or jwt in response: {data}")
        return Mailbox(address=address, jwt=jwt)

    def list_messages(self, jwt: str, limit: int = 20) -> list[dict[str, Any]]:
        """Fetch parsed messages for the given address JWT."""
        data = self._request("GET", f"/api/parsed_mails?limit={limit}", jwt=jwt)
        if isinstance(data, list):
            return data[:limit]
        if isinstance(data, dict):
            items = data.get("mails") or data.get("messages") or []
            return items[:limit] if isinstance(items, list) else []
        return []

    def get_message(self, jwt: str, message_id: str) -> dict[str, Any]:
        """Fetch content for a single message."""
        data = self._request("GET", f"/api/parsed_mail/{message_id}", jwt=jwt)
        return data if isinstance(data, dict) else {}

    @staticmethod
    def extract_verification_code(text: str) -> str | None:
        """Extract a 6-digit numeric verification code or confirmation URL."""
        if not text:
            return None
        cleaned = re.sub(r"(?is)<(style|script)\b[^>]*>.*?</\1>", " ", text)
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        cleaned = html.unescape(cleaned)
        no_urls = re.sub(r"https?://\S+", " ", cleaned)

        otp_pattern = r"(?<![#\w-])(\d{6})(?![\w-])"
        ctx_match = re.search(
            rf"(?i)(?:code|otp|pin|verif(?:y|ication)|confirm(?:ation)?)\D{{0,30}}{otp_pattern}",
            no_urls,
        )
        if ctx_match:
            return ctx_match.group(1)

        code_match = re.search(otp_pattern, no_urls)
        if code_match:
            return code_match.group(1)

        for candidate in (cleaned, text):
            url_match = re.search(
                r"https?://[^\s\"'<>]+(?:verify|confirm)[^\s\"'<>]+",
                candidate,
            )
            if url_match:
                return html.unescape(url_match.group(0))
        return None

    def wait_for_verification_code(
        self,
        jwt: str,
        timeout_sec: float = 120.0,
        poll_interval_sec: float = 3.0,
    ) -> str | None:
        """Poll the mailbox until a Wolvesville verification code arrives."""
        deadline = time.monotonic() + max(0.0, timeout_sec)
        seen_ids: set[str] = set()

        while time.monotonic() < deadline:
            try:
                messages = self.list_messages(jwt)
            except TempEmailError as exc:
                if exc.status_code in (401, 403):
                    raise
                log.warning("Polling temporary inbox failed: %s", exc)
                messages = []
            except Exception as exc:
                log.warning("Polling temporary inbox failed: %s", exc)
                messages = []

            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                msg_id = str(msg.get("id") or "")
                if msg_id and msg_id in seen_ids:
                    continue

                detail = msg
                if msg_id and not (msg.get("text") or msg.get("html")):
                    try:
                        detail = self.get_message(jwt, msg_id) or msg
                    except TempEmailError as exc:
                        if exc.status_code in (401, 403):
                            raise
                        log.debug("Failed to fetch message %s: %s", msg_id, exc)
                        continue
                    except Exception as exc:
                        log.debug("Failed to fetch message %s: %s", msg_id, exc)
                        continue

                if msg_id:
                    seen_ids.add(msg_id)

                subject = str(detail.get("subject") or "")
                text_body = str(detail.get("text") or "")
                html_body = str(detail.get("html") or "")
                for candidate in (f"{subject}\n{text_body}", html_body):
                    code = self.extract_verification_code(candidate)
                    if code:
                        return code

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_interval_sec, remaining))

        return None


class MailTmClient:
    """Zero-config open temporary email client using api.mail.tm."""

    def __init__(self, timeout: float = 15.0):
        self.base_url = "https://api.mail.tm"
        self.timeout = timeout

    def _req(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        jwt: str | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "wvbot-provisioner/1.1",
        }
        if jwt:
            headers["Authorization"] = f"Bearer {jwt}"
        return _http_json(
            method,
            url,
            headers,
            payload=payload,
            timeout=self.timeout,
            label="Mail.tm HTTP",
        )

    def get_domains(self) -> list[str]:
        data = self._req("GET", "/domains")
        items = data if isinstance(data, list) else (data.get("hydra:member") or []) if isinstance(data, dict) else []
        return [item["domain"] for item in items if isinstance(item, dict) and item.get("isActive") and item.get("domain")]

    def create_mailbox(
        self,
        name: str | None = None,
        domain: str | None = None,
    ) -> Mailbox:
        rng = random.SystemRandom()
        domains = self.get_domains()
        if not domains:
            raise TempEmailError("No active mail.tm domains available")
        if domain:
            if domain not in domains:
                raise TempEmailError(f"Domain {domain!r} is not available in active mail.tm domains: {domains}")
            chosen_domain = domain
        else:
            chosen_domain = domains[0]

        prefix = name or ("wvbot_" + "".join(rng.choices(string.ascii_lowercase, k=6)))
        address = f"{prefix}@{chosen_domain}"
        pwd = "P@" + "".join(rng.choices(string.ascii_letters + string.digits, k=10)) + "1!"

        try:
            self._req("POST", "/accounts", {"address": address, "password": pwd})
        except TempEmailError as exc:
            if exc.status_code not in (409, 422):
                raise
            suffix = "".join(rng.choices(string.ascii_lowercase + string.digits, k=4))
            address = f"{prefix}_{suffix}@{chosen_domain}"
            self._req("POST", "/accounts", {"address": address, "password": pwd})

        token_data = self._req("POST", "/token", {"address": address, "password": pwd})
        jwt = str(token_data.get("token") or "") if isinstance(token_data, dict) else ""
        if not jwt:
            raise TempEmailError(f"No token received from mail.tm for {address}")
        return Mailbox(address=address, jwt=jwt)

    def list_messages(self, jwt: str, limit: int = 20) -> list[dict[str, Any]]:
        data = self._req("GET", "/messages", jwt=jwt)
        if isinstance(data, list):
            return data[:limit]
        if isinstance(data, dict):
            items = data.get("hydra:member") or []
            return items[:limit] if isinstance(items, list) else []
        return []

    def get_message(self, jwt: str, message_id: str) -> dict[str, Any]:
        data = self._req("GET", f"/messages/{message_id}", jwt=jwt)
        return data if isinstance(data, dict) else {}

    def wait_for_verification_code(
        self,
        jwt: str,
        timeout_sec: float = 120.0,
        poll_interval_sec: float = 3.0,
    ) -> str | None:
        deadline = time.monotonic() + max(0.0, timeout_sec)
        seen_ids: set[str] = set()

        while time.monotonic() < deadline:
            try:
                messages = self.list_messages(jwt)
            except TempEmailError as exc:
                if exc.status_code in (401, 403):
                    raise
                log.warning("Polling mail.tm inbox failed: %s", exc)
                messages = []
            except Exception as exc:
                log.warning("Polling mail.tm inbox failed: %s", exc)
                messages = []

            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                msg_id = str(msg.get("id") or "")
                if msg_id and msg_id in seen_ids:
                    continue

                if msg_id:
                    try:
                        detail = self.get_message(jwt, msg_id)
                    except TempEmailError as exc:
                        if exc.status_code in (401, 403):
                            raise
                        log.debug("Failed to fetch mail.tm message %s: %s", msg_id, exc)
                        continue
                    except Exception as exc:
                        log.debug("Failed to fetch mail.tm message %s: %s", msg_id, exc)
                        continue
                    seen_ids.add(msg_id)
                else:
                    detail = msg

                subject = str(detail.get("subject") or "")
                text_body = str(detail.get("text") or "")
                intro_body = str(detail.get("intro") or "")
                raw_html = detail.get("html")
                if isinstance(raw_html, list):
                    html_body = "\n".join(str(part) for part in raw_html if part)
                else:
                    html_body = str(raw_html or "")

                plain_section = "\n".join(p for p in (subject, text_body, intro_body) if p)
                for candidate in (plain_section, html_body):
                    code = TempEmailClient.extract_verification_code(candidate)
                    if code:
                        return code

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_interval_sec, remaining))

        return None


extract_verification_code = TempEmailClient.extract_verification_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Temporary email helper (Cloudflare / Mail.tm)")
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    create_parser = subparsers.add_parser("create", help="Create a new mailbox")
    create_parser.add_argument("--url", default="auto", help="Worker base URL or 'auto' for Mail.tm")
    create_parser.add_argument("--admin", default=None, help="Admin password (required for custom Worker)")
    create_parser.add_argument("--site-auth", default=None, help="Optional x-custom-auth site password")
    create_parser.add_argument("--name", default=None, help="Desired mailbox name")
    create_parser.add_argument("--domain", default=None, help="Target domain")

    wait_parser = subparsers.add_parser("wait", help="Wait for verification code")
    wait_parser.add_argument("--url", default="auto", help="Worker base URL or 'auto' for Mail.tm")
    wait_parser.add_argument("--jwt", required=True, help="Address JWT")
    wait_parser.add_argument("--timeout", type=float, default=120.0, help="Wait timeout in seconds")

    args = parser.parse_args(argv)
    url = (args.url or "auto").strip()
    use_mailtm = url.lower() in ("", "auto", "mail.tm", "none")

    if args.cmd == "create":
        client = MailTmClient() if use_mailtm else TempEmailClient(
            url, admin_password=args.admin, site_password=args.site_auth
        )
        box = client.create_mailbox(args.name, args.domain)
        print(json.dumps({"address": box.address, "jwt": box.jwt}, indent=2))
        return 0

    if args.cmd == "wait":
        client = MailTmClient() if use_mailtm else TempEmailClient(url)
        code = client.wait_for_verification_code(args.jwt, timeout_sec=args.timeout)
        if code:
            print(json.dumps({"ok": True, "code": code}))
            return 0
        print(json.dumps({"ok": False, "error": "timed out waiting for verification code"}))
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
