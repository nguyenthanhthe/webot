"""Store the Wolvesville refresh token in the bot state directory.

Usage:

    python3 setcreds.py
    python3 setcreds.py --refresh-token "<token>"
    python3 setcreds.py --account bot2 --refresh-token "<token>"

The durable credential is written atomically to ``refresh_token.txt`` (mode
600) inside the active state directory (honoring ``WV_BOT_STATE_DIR`` /
``STATE_DIR``).  On Termux, if ``bot_ctl.sh`` is installed, the service is
restarted cleanly through it.
"""

from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PREFIX = Path("/data/data/com.termux/files/usr")
TERMUX_HOME = Path("/data/data/com.termux/files/home")
REFRESH_TOKEN_FILE = "refresh_token.txt"


def resolve_state_dir(explicit: str | Path | None = None) -> Path:
    """Resolve state directory honoring WV_BOT_STATE_DIR / STATE_DIR."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("WV_BOT_STATE_DIR") or os.environ.get("STATE_DIR")
    if env:
        return Path(env)
    if TERMUX_HOME.is_dir():
        return TERMUX_HOME / ".wv-bot"
    return Path.home() / ".wv-bot"


def _clean_account_name(value: str) -> str:
    kept = "".join(c if (c.isalnum() or c in "-_") else "" for c in str(value))
    return kept[:24]


def _atomic_write_secret(path: Path, content: str) -> None:
    """Atomically write secret content with 0o600 permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(tmp), flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _restart_termux_service() -> bool:
    """Delegate Termux service restart to bot_ctl.sh if available."""
    if not PREFIX.is_dir():
        return False
    for candidate in (TERMUX_HOME / "bot_ctl.sh", ROOT / "tools" / "bot_ctl.sh"):
        if candidate.is_file():
            res = subprocess.run(
                ["sh", str(candidate), "restart"],
                check=False,
                timeout=35,
            )
            return res.returncode == 0
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Store Wolvesville refresh token")
    parser.add_argument("--refresh-token", default=None, help="Firebase refresh token")
    parser.add_argument("--state-dir", default=None, help="Override state directory")
    parser.add_argument("--account", default=None, help="Optional farm account name")
    parser.add_argument("--no-restart", action="store_true", help="Do not restart Termux service")
    args = parser.parse_args(argv)

    state_dir = resolve_state_dir(args.state_dir)
    if args.account:
        safe_name = _clean_account_name(args.account)
        if not safe_name:
            print("invalid account name", file=sys.stderr)
            return 1
        target_dir = state_dir / "accounts" / safe_name
    else:
        target_dir = state_dir

    token = (args.refresh_token or "").strip()
    if not token:
        print("Wolvesville refresh token (stored on this device only, mode 600)")
        print("Tip: obtain a refresh token once with `cd web && node login.mjs`")
        if sys.stdin.isatty():
            token = getpass.getpass("refresh_token: ").strip()
        else:
            token = sys.stdin.readline().strip()

    if not token:
        print("refresh_token is required", file=sys.stderr)
        return 1

    token_path = target_dir / REFRESH_TOKEN_FILE
    _atomic_write_secret(token_path, token + "\n")
    print(f"saved {token_path} (mode 600)")

    if not args.no_restart and _restart_termux_service():
        print("service restarted via bot_ctl.sh")

    print("dashboard: http://127.0.0.1:8787/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
