"""Client for dreamhunter2333/cloudflare_temp_email API.

Standard library only.  Used by the automated provisioning pipeline to create
ephemeral mailboxes, listen for verification emails from Wolvesville, and
extract activation codes.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, NamedTuple

log = logging.getLogger("wvbot.temp_email")


class Mailbox(NamedTuple):
    address: str
    jwt: str


class TempEmailError(RuntimeError):
    pass


class TempEmailClient:
    """Interface to cloudflare_temp_email Worker REST API."""

    def __init__(
        self,
        base_url: str,
        admin_password: str | None = None,
        site_password: str | None = None,
        timeout: float = 15.0,
    ):
        self.base_url = base_url.rstrip("/")
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

        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                err_body = exc.read().decode("utf-8", errors="replace")
            finally:
                exc.close()
            raise TempEmailError(f"HTTP {exc.code} for {url}: {err_body}") from exc
        except Exception as exc:
            raise TempEmailError(f"Request failed for {url}: {exc}") from exc

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
            return data
        if isinstance(data, dict):
            return data.get("mails") or data.get("messages") or []
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
        # Look for 6-digit codes
        code_match = re.search(r"\b(\d{6})\b", text)
        if code_match:
            return code_match.group(1)
        # Fallback to confirmation URLs
        url_match = re.search(r"https?://[^\s\"'<>]+(?:verify|confirm)[^\s\"'<>]+", text)
        if url_match:
            return url_match.group(0)
        return None

    def wait_for_verification_code(
        self,
        jwt: str,
        timeout_sec: float = 120.0,
        poll_interval_sec: float = 3.0,
    ) -> str | None:
        """Poll the mailbox until a Wolvesville verification code arrives."""
        deadline = time.time() + timeout_sec
        seen_ids: set[str] = set()

        while time.time() < deadline:
            try:
                messages = self.list_messages(jwt)
            except Exception as exc:
                log.debug("Polling temporary inbox failed: %s", exc)
                messages = []

            for msg in messages:
                msg_id = str(msg.get("id") or "")
                if msg_id and msg_id in seen_ids:
                    continue
                if msg_id:
                    seen_ids.add(msg_id)

                subject = str(msg.get("subject") or "")
                text_content = str(msg.get("text") or msg.get("html") or "")
                full_body = f"{subject}\n{text_content}"

                code = self.extract_verification_code(full_body)
                if code:
                    return code

            time.sleep(poll_interval_sec)

        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Cloudflare Temp Email helper")
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    create_parser = subparsers.add_parser("create", help="Create a new mailbox")
    create_parser.add_argument("--url", required=True, help="Base URL of worker")
    create_parser.add_argument("--admin", required=True, help="Admin password")
    create_parser.add_argument("--name", default=None, help="Desired mailbox name")
    create_parser.add_argument("--domain", default=None, help="Target domain")

    wait_parser = subparsers.add_parser("wait", help="Wait for verification code")
    wait_parser.add_argument("--url", required=True, help="Base URL of worker")
    wait_parser.add_argument("--jwt", required=True, help="Address JWT")
    wait_parser.add_argument("--timeout", type=float, default=120.0, help="Wait timeout in seconds")

    args = parser.parse_args()
    if args.cmd == "create":
        client = TempEmailClient(args.url, admin_password=args.admin)
        box = client.create_mailbox(args.name, args.domain)
        print(json.dumps({"address": box.address, "jwt": box.jwt}, indent=2))
        return 0

    if args.cmd == "wait":
        client = TempEmailClient(args.url)
        code = client.wait_for_verification_code(args.jwt, timeout_sec=args.timeout)
        if code:
            print(json.dumps({"ok": True, "code": code}))
            return 0
        print(json.dumps({"ok": False, "error": "timed out waiting for verification code"}))
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
