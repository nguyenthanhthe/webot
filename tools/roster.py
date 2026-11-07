"""Manage accounts in the Wolvesville bot farm roster.

Usage:
    python tools/roster.py list
    python tools/roster.py add <name> [--refresh-token <token>]
    python tools/roster.py remove <name>
    python tools/roster.py status
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wvbot.config import REFRESH_TOKEN_FILE, STATE_DIR
from wvbot.farm import (
    ROSTER_FILE,
    clean_name,
    load_roster,
    remove_account_from_roster,
    save_account_to_roster,
)


def _roster_path() -> Path:
    return STATE_DIR / ROSTER_FILE


def list_accounts() -> int:
    path = _roster_path()
    if not path.is_file():
        print(f"Roster not found at {path}. Farm falls back to single account ('main').")
        # Check classic refresh token
        classic = STATE_DIR / REFRESH_TOKEN_FILE
        if classic.is_file():
            print(f"  [main] Classic account active with refresh token at {classic}")
        return 0

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Error reading roster: {exc}")
        return 1

    accounts = data.get("accounts", [])
    print(f"Farm Roster ({len(accounts)} accounts):")
    for i, acc in enumerate(accounts, 1):
        name = acc.get("name", "?")
        token_inline = bool(acc.get("refresh_token"))
        acc_dir = STATE_DIR / "accounts" / name
        token_file = acc_dir / REFRESH_TOKEN_FILE
        token_present = token_inline or token_file.is_file()
        status_str = "ready" if token_present else "missing token"
        print(f"  {i}. {name:<18} [{status_str}] (dir: {acc_dir})")
    return 0


def add_account(name: str, refresh_token: str | None = None) -> int:
    try:
        save_account_to_roster(STATE_DIR, name, refresh_token)
        print(f"Added '{clean_name(name)}' to roster.")
        return 0
    except Exception as exc:
        print(f"Error adding account: {exc}")
        return 1


def remove_account(name: str) -> int:
    if remove_account_from_roster(STATE_DIR, name):
        print(f"Removed '{clean_name(name)}' from roster.")
        return 0
    print(f"Account '{name}' not found or could not be removed.")
    return 1


def farm_status() -> int:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8787/api/farm", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"Could not reach farm dashboard: {exc}")
        return 1

    print(f"Farm Status (v{data.get('version', '?')}): {data.get('status')}")
    print(f"Playing: {data.get('playing', 0)} / {data.get('count', 0)} bots | Total XP: {data.get('total_xp', 0)}")
    print("-" * 60)
    for b in data.get("bots", []):
        name = b.get("username") or b.get("name")
        st = b.get("status", "?")
        role = b.get("role") or "-"
        room = b.get("room") or "-"
        xp = b.get("xp_total", 0)
        err = b.get("errors", 0)
        print(f"  [{st:<10}] {name:<16} role={role:<14} room={room:<20} xp={xp} err={err}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Wolvesville farm roster manager")
    subparsers = parser.add_subparsers(dest="subcommand")

    subparsers.add_parser("list", help="List all accounts in the farm roster")
    add_parser = subparsers.add_parser("add", help="Add an account to the roster")
    add_parser.add_argument("name", help="Name of the account")
    add_parser.add_argument("--refresh-token", help="Firebase refresh token")

    rm_parser = subparsers.add_parser("remove", help="Remove an account from the roster")
    rm_parser.add_argument("name", help="Name of the account to remove")

    subparsers.add_parser("status", help="Query live farm status from dashboard")

    args = parser.parse_args()
    if args.subcommand == "list":
        return list_accounts()
    if args.subcommand == "add":
        return add_account(args.name, args.refresh_token)
    if args.subcommand == "remove":
        return remove_account(args.name)
    if args.subcommand == "status":
        return farm_status()

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
