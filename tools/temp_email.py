"""CLI wrapper and shared exports for Wolvesville temp-email clients.

All implementation lives in :mod:`wvbot.temp_email`; this module re-exports the
public API for standalone scripts/tests and provides a CLI supporting both
zero-config Mail.tm (default ``--url auto``) and self-hosted Cloudflare Temp
Email workers.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE if (_HERE / "wvbot").is_dir() else _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from wvbot import temp_email as _impl  # noqa: E402
from wvbot.temp_email import (  # noqa: E402, F401
    Mailbox,
    MailTmClient,
    TempEmailClient,
    TempEmailError,
)

extract_verification_code = getattr(
    _impl,
    "extract_verification_code",
    TempEmailClient.extract_verification_code,
)
EmailMessage = getattr(_impl, "EmailMessage", dict)
site_password = getattr(_impl, "site_password", None)

__all__ = [
    "EmailMessage",
    "MailTmClient",
    "Mailbox",
    "TempEmailClient",
    "TempEmailError",
    "extract_verification_code",
    "main",
    "site_password",
]


def _build_client(
    url: str,
    admin_password: str | None = None,
    custom_site_password: str | None = None,
) -> MailTmClient | TempEmailClient:
    normalized = (url or "auto").strip()
    if normalized.lower() in {"", "auto", "mail.tm", "mailtm", "https://api.mail.tm"}:
        return MailTmClient()
    return TempEmailClient(
        normalized,
        admin_password=admin_password,
        site_password=custom_site_password,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Temp Email helper (Mail.tm & Cloudflare Temp Email)")
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    create_parser = subparsers.add_parser("create", help="Create a new mailbox")
    create_parser.add_argument(
        "--url",
        default="auto",
        help="Worker base URL, or 'auto'/'mail.tm' for Mail.tm (default: auto)",
    )
    create_parser.add_argument("--admin", default=None, help="Admin password (Cloudflare Temp Email)")
    create_parser.add_argument(
        "--site-auth",
        "--site-password",
        dest="site_password",
        default=None,
        help="Custom site auth password header (x-custom-auth)",
    )
    create_parser.add_argument("--name", default=None, help="Desired mailbox name")
    create_parser.add_argument("--domain", default=None, help="Target domain")

    wait_parser = subparsers.add_parser("wait", help="Wait for verification code")
    wait_parser.add_argument(
        "--url",
        default="auto",
        help="Worker base URL, or 'auto'/'mail.tm' for Mail.tm (default: auto)",
    )
    wait_parser.add_argument("--jwt", required=True, help="Address/account JWT")
    wait_parser.add_argument(
        "--site-auth",
        "--site-password",
        dest="site_password",
        default=None,
        help="Custom site auth password header (x-custom-auth)",
    )
    wait_parser.add_argument("--timeout", type=float, default=120.0, help="Wait timeout in seconds")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    client: Any = _build_client(
        args.url,
        admin_password=getattr(args, "admin", None),
        custom_site_password=getattr(args, "site_password", None),
    )

    try:
        if args.cmd == "create":
            box = client.create_mailbox(args.name, args.domain)
            print(json.dumps({"address": box.address, "jwt": box.jwt}, indent=2))
            return 0

        if args.cmd == "wait":
            code = client.wait_for_verification_code(args.jwt, timeout_sec=args.timeout)
            if code:
                print(json.dumps({"ok": True, "code": code}))
                return 0
            print(json.dumps({"ok": False, "error": "timed out waiting for verification code"}))
            return 1
    except TempEmailError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
