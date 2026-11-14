"""CLI for 1-click Bot Farm Provisioning (Mail.tm or Cloudflare Temp Email).

Usage:
    python tools/provision_farm.py --url auto --count 2
    python tools/provision_farm.py --url https://mail.example.com --admin myAdminPassword --count 2
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
ROOT = _HERE if (_HERE / "wvbot").is_dir() else _HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wvbot import config as wv_config  # noqa: E402
from wvbot.config import REFRESH_TOKEN_FILE  # noqa: E402
from wvbot.provision import AccountProvisioner, ProvisionError  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
    datefmt="%H:%M:%S",
)


def _resolve_state_dir(override: str | None = None) -> Path:
    if override:
        return Path(override).expanduser()
    getter = getattr(wv_config, "get_state_dir", None)
    if callable(getter):
        return Path(getter())
    env_dir = (os.environ.get("WV_BOT_STATE_DIR") or os.environ.get("STATE_DIR") or "").strip()
    if env_dir:
        return Path(env_dir).expanduser()
    return Path(wv_config.STATE_DIR)


def _resolve_token(explicit: str | None, state_dir: Path) -> str:
    if explicit:
        return explicit.strip()
    env_tok = os.environ.get("WV_BOT_DASHBOARD_TOKEN") or os.environ.get("WV_DASHBOARD_TOKEN")
    if env_tok:
        return env_tok.strip()
    tok_path = state_dir / "dashboard_token.txt"
    if tok_path.is_file():
        return tok_path.read_text(encoding="utf-8").strip()
    return ""


def _hot_register(api_url: str, token: str, account: dict[str, Any], state_dir: Path) -> None:
    name = str(account.get("name") or "").strip()
    if not name:
        return
    refresh_token = str(account.get("refresh_token") or "").strip()
    if not refresh_token:
        tok_file = state_dir / "accounts" / name / REFRESH_TOKEN_FILE
        if tok_file.is_file():
            refresh_token = tok_file.read_text(encoding="utf-8").strip()

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Dashboard-Token"] = token
    body = json.dumps({"name": name, "refresh_token": refresh_token}).encode("utf-8")
    req = urllib.request.Request(
        api_url.rstrip("/") + "/api/accounts",
        data=body,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"dashboard returned HTTP {resp.status}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="1-Click Temp Email Farm Provisioner")
    parser.add_argument("--url", default="auto", help="Temp-email worker URL, or 'auto' for Mail.tm")
    parser.add_argument("--admin", default=None, help="Admin password (Cloudflare Temp Email)")
    parser.add_argument("--site-auth", default=None, help="Optional x-custom-auth site password")
    parser.add_argument("--domain", default=None, help="Optional domain for email addresses")
    parser.add_argument("--count", type=int, default=1, help="Number of bot accounts to provision (default: 1)")
    parser.add_argument("--state-dir", default=None, help="Override bot state directory")
    parser.add_argument(
        "--api-url",
        default=None,
        help="Running dashboard URL to hot-register newly provisioned accounts",
    )
    parser.add_argument("--token", default=None, help="Dashboard auth token for --api-url")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state_dir = _resolve_state_dir(args.state_dir)
    token = _resolve_token(args.token, state_dir)

    print("=== 1-CLICK TEMP MAIL FARM PROVISIONER ===")
    print(f"Provider URL: {args.url}")
    print(f"Target count: {args.count}")
    print(f"State dir:    {state_dir}")

    provisioner = AccountProvisioner(
        temp_email_url=args.url,
        admin_password=args.admin,
        site_password=args.site_auth,
        domain=args.domain,
    )

    try:
        created = provisioner.batch_provision(
            count=args.count,
            farm_root=state_dir,
            progress_callback=lambda step, pct: print(f"[{int(pct * 100)}%] {step}"),
        )
    except ProvisionError as exc:
        print(f"\n[-] Provisioning failed: {exc}", file=sys.stderr)
        return 1

    print(f"\n[+] Successfully provisioned {len(created)} accounts into the Farm!")
    for acc in created:
        print(f"  - {acc['name']}: {acc['email']}")
        if args.api_url:
            try:
                _hot_register(args.api_url, token, acc, state_dir)
            except (urllib.error.URLError, OSError, RuntimeError) as exc:
                print(
                    f"[-] Hot-register failed for {acc['name']} via {args.api_url}: {exc}",
                    file=sys.stderr,
                )
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
