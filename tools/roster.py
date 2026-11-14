"""Manage accounts in the Wolvesville bot farm roster.

Usage:
    python tools/roster.py list [--url URL] [--token TOKEN] [--state-dir DIR]
    python tools/roster.py add <name> [--refresh-token <token>]
    python tools/roster.py remove <name>
    python tools/roster.py status [--url URL] [--token TOKEN]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

_HERE = Path(__file__).resolve().parent
ROOT = _HERE if (_HERE / "wvbot").is_dir() else _HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wvbot import config as wv_config  # noqa: E402
from wvbot.config import REFRESH_TOKEN_FILE  # noqa: E402
from wvbot.farm import (  # noqa: E402
    ROSTER_FILE,
    clean_name,
    remove_account_from_roster,
    save_account_to_roster,
)

DEFAULT_URL = "http://127.0.0.1:8787"
_PROCESS_LOCK = threading.Lock()


def resolve_state_dir(override: str | Path | None = None) -> Path:
    if override:
        return Path(override).expanduser()
    getter = getattr(wv_config, "get_state_dir", None)
    if callable(getter):
        return Path(getter())
    env_dir = (os.environ.get("WV_BOT_STATE_DIR") or os.environ.get("STATE_DIR") or "").strip()
    if env_dir:
        return Path(env_dir).expanduser()
    return Path(wv_config.STATE_DIR)


def resolve_token(explicit: str | None = None, state_dir: Path | None = None) -> str:
    if explicit:
        return explicit.strip()
    env_tok = os.environ.get("WV_BOT_DASHBOARD_TOKEN") or os.environ.get("WV_DASHBOARD_TOKEN")
    if env_tok:
        return env_tok.strip()
    if state_dir is not None:
        tok_path = state_dir / "dashboard_token.txt"
        if tok_path.is_file():
            return tok_path.read_text(encoding="utf-8").strip()
    return ""


@contextlib.contextmanager
def _roster_lock(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / ".accounts.lock"
    with _PROCESS_LOCK:
        with lock_path.open("a+", encoding="utf-8") as fh:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp.{os.getpid()}")
    raw = json.dumps(data, indent=2) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, 0o600)
    try:
        os.write(fd, raw.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _atomic_write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp.{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, 0o600)
    try:
        os.write(fd, (value.strip() + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _api_request(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    token: str = "",
) -> Any:
    url = base_url.rstrip("/") + path
    headers: dict[str, str] = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Dashboard-Token"] = token
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=5) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def _is_connection_error(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(exc, (urllib.error.URLError, OSError, TimeoutError))


def _format_http_error(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace").strip()
    except Exception:
        body = ""
    return f"HTTP {exc.code}: {body or exc.reason}"


def _read_existing_roster_json(roster_path: Path) -> dict[str, Any]:
    raw = roster_path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        raise ValueError("accounts.json must contain a JSON object with an 'accounts' list")
    return data


def list_accounts(
    state_dir: Path | None = None,
    url: str | None = None,
    token: str = "",
    offline: bool = False,
) -> int:
    sdir = resolve_state_dir(state_dir)
    if not offline and url:
        try:
            data = _api_request(url, "GET", "/api/accounts", token=token)
            accounts = data.get("accounts", []) if isinstance(data, dict) else data
            print(f"Farm Roster ({len(accounts)} accounts via {url}):")
            for i, acc in enumerate(accounts, 1):
                name = acc.get("name", "?")
                ready = acc.get("token_present") or acc.get("ready") or acc.get("refresh_token")
                status_str = "ready" if ready else str(acc.get("status") or "configured")
                print(f"  {i}. {name:<18} [{status_str}]")
            if not accounts:
                classic = sdir / REFRESH_TOKEN_FILE
                if classic.is_file():
                    print(f"  [main] Classic single-account token active at {classic}")
            return 0
        except urllib.error.HTTPError as exc:
            print(f"Error querying dashboard: {_format_http_error(exc)}", file=sys.stderr)
            return 1
        except Exception as exc:
            if not _is_connection_error(exc):
                print(f"Error querying dashboard: {exc}", file=sys.stderr)
                return 1

    path = sdir / ROSTER_FILE
    if not path.is_file():
        print(f"Roster not found at {path}. Farm falls back to single account ('main').")
        classic = sdir / REFRESH_TOKEN_FILE
        if classic.is_file():
            print(f"  [main] Classic account active with refresh token at {classic}")
        return 0

    try:
        data = _read_existing_roster_json(path)
    except Exception as exc:
        print(f"Error reading roster {path}: {exc}", file=sys.stderr)
        return 1

    accounts = data.get("accounts", [])
    print(f"Farm Roster ({len(accounts)} accounts):")
    for i, acc in enumerate(accounts, 1):
        name = acc.get("name", "?")
        token_inline = bool(acc.get("refresh_token"))
        acc_dir = sdir / "accounts" / str(name)
        token_file = acc_dir / REFRESH_TOKEN_FILE
        token_present = token_inline or token_file.is_file()
        status_str = "ready" if token_present else "missing token"
        print(f"  {i}. {name:<18} [{status_str}] (dir: {acc_dir})")
    return 0


def add_account(
    name: str,
    refresh_token: str | None = None,
    state_dir: Path | None = None,
    url: str | None = None,
    token: str = "",
    offline: bool = False,
) -> int:
    sdir = resolve_state_dir(state_dir)
    safe = clean_name(name)
    if not safe:
        print("Error adding account: invalid account name", file=sys.stderr)
        return 1

    if not offline and url:
        try:
            payload: dict[str, Any] = {"name": safe}
            if refresh_token:
                payload["refresh_token"] = refresh_token.strip()
            resp = _api_request(url, "POST", "/api/accounts", payload=payload, token=token)
            print(f"Added '{resp.get('name', safe)}' to running farm.")
            return 0
        except urllib.error.HTTPError as exc:
            print(f"Error adding account via dashboard: {_format_http_error(exc)}", file=sys.stderr)
            return 1
        except Exception as exc:
            if not _is_connection_error(exc):
                print(f"Error adding account: {exc}", file=sys.stderr)
                return 1

    try:
        with _roster_lock(sdir):
            roster_path = sdir / ROSTER_FILE
            if roster_path.is_file():
                # Validate before save_account_to_roster so malformed JSON is never wiped.
                _read_existing_roster_json(roster_path)
            save_account_to_roster(sdir, safe, refresh_token)
            # Re-write via atomic 0o600 helper for durability on POSIX/Docker.
            data = _read_existing_roster_json(roster_path)
            _atomic_write_json(roster_path, data)
            if refresh_token:
                _atomic_write_secret(sdir / "accounts" / safe / REFRESH_TOKEN_FILE, refresh_token)
        print(f"Added '{safe}' to roster.")
        return 0
    except Exception as exc:
        print(f"Error adding account: {exc}", file=sys.stderr)
        return 1


def remove_account(
    name: str,
    state_dir: Path | None = None,
    url: str | None = None,
    token: str = "",
    offline: bool = False,
) -> int:
    sdir = resolve_state_dir(state_dir)
    safe = clean_name(name)
    if not safe:
        print(f"Account '{name}' not found or could not be removed.", file=sys.stderr)
        return 1

    if not offline and url:
        try:
            resp = _api_request(
                url,
                "POST",
                "/api/accounts",
                payload={"action": "remove", "name": safe},
                token=token,
            )
            if resp.get("removed"):
                print(f"Removed '{safe}' from running farm.")
                return 0
            print(f"Account '{safe}' not found or could not be removed.", file=sys.stderr)
            return 1
        except urllib.error.HTTPError as exc:
            print(f"Error removing account via dashboard: {_format_http_error(exc)}", file=sys.stderr)
            return 1
        except Exception as exc:
            if not _is_connection_error(exc):
                print(f"Error removing account: {exc}", file=sys.stderr)
                return 1

    with _roster_lock(sdir):
        if remove_account_from_roster(sdir, safe):
            print(f"Removed '{safe}' from roster.")
            return 0
    print(f"Account '{name}' not found or could not be removed.", file=sys.stderr)
    return 1


def farm_status(url: str = DEFAULT_URL, token: str = "") -> int:
    try:
        data = _api_request(url, "GET", "/api/farm", token=token)
    except urllib.error.HTTPError as exc:
        print(f"Farm dashboard returned {_format_http_error(exc)}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Could not reach farm dashboard: {exc}", file=sys.stderr)
        return 1

    print(f"Farm Status (v{data.get('version', '?')}): {data.get('status')}")
    print(f"Playing: {data.get('playing', 0)} / {data.get('count', 0)} bots | Total XP: {data.get('total_xp', 0)}")
    print("-" * 60)
    for bot in data.get("bots", []):
        name = bot.get("username") or bot.get("name")
        st = bot.get("status", "?")
        role = bot.get("role") or "-"
        room = bot.get("room") or "-"
        xp = bot.get("xp_total", 0)
        err = bot.get("errors", 0)
        print(f"  [{st:<10}] {name:<16} role={role:<14} room={room:<20} xp={xp} err={err}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    common.add_argument(
        "--url",
        help="Running dashboard base URL (default: http://127.0.0.1:8787)",
    )
    common.add_argument("--token", help="Dashboard auth token")
    common.add_argument("--state-dir", help="Override bot state directory")
    common.add_argument(
        "--offline",
        action="store_true",
        help="Operate on disk roster directly without calling the dashboard HTTP API",
    )

    parser = argparse.ArgumentParser(
        description="Wolvesville farm roster manager",
        parents=[common],
    )
    subparsers = parser.add_subparsers(dest="subcommand")

    subparsers.add_parser("list", parents=[common], help="List all accounts in the farm roster")
    add_parser = subparsers.add_parser("add", parents=[common], help="Add an account to the roster")
    add_parser.add_argument("name", help="Name of the account")
    add_parser.add_argument("--refresh-token", default=None, help="Firebase refresh token")

    rm_parser = subparsers.add_parser("remove", parents=[common], help="Remove an account from the roster")
    rm_parser.add_argument("name", help="Name of the account to remove")

    subparsers.add_parser("status", parents=[common], help="Query live farm status from dashboard")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.subcommand:
        parser.print_help()
        return 0

    url = getattr(args, "url", os.environ.get("WV_DASHBOARD_URL", DEFAULT_URL))
    raw_state_dir = getattr(args, "state_dir", None)
    raw_token = getattr(args, "token", None)
    offline = bool(getattr(args, "offline", False))

    sdir = resolve_state_dir(raw_state_dir)
    token = resolve_token(raw_token, sdir)

    if args.subcommand == "list":
        return list_accounts(state_dir=sdir, url=url, token=token, offline=offline)
    if args.subcommand == "add":
        return add_account(
            args.name,
            args.refresh_token,
            state_dir=sdir,
            url=url,
            token=token,
            offline=offline,
        )
    if args.subcommand == "remove":
        return remove_account(args.name, state_dir=sdir, url=url, token=token, offline=offline)
    if args.subcommand == "status":
        return farm_status(url=url, token=token)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
