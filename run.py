"""Launcher for the Wolvesville bot (Termux, Linux, Windows, and Docker).

Usage:

    python3 run.py                 # protocol mode: pure Python over socket.io
    python3 run.py --mode farm     # multi-account farm supervisor + dashboard
    python3 run.py --mode web      # headless Firefox + injected payload

The protocol mode is the default because it needs nothing but the Python
standard library: it mints its own firebase token from the refresh token,
lists the open custom games over REST and plays them over socket.io.  No
browser, no screen, no clicking.

The web mode drives the official web client in a headless Firefox and
injects a script into it.  It is kept as a fallback for the cases where
the protocol mode runs into a Cloudflare challenge.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

PREFIX = Path("/data/data/com.termux/files/usr")
ROOT = Path(__file__).resolve().parent
WAKE_LOCK = PREFIX / "bin" / "termux-wake-lock"


def _wake_lock() -> None:
    """Ask Termux to keep the CPU awake while the bot runs."""
    if not PREFIX.is_dir():
        return
    try:
        subprocess.run(
            [str(WAKE_LOCK)],
            shell=False,
            check=False,
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 - never block startup
        print(f"wake lock failed: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wolvesville bot")
    parser.add_argument("--mode", choices=["web", "protocol", "farm"], default=None)
    args, extra = parser.parse_known_args(argv)

    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    if PREFIX.is_dir():
        _wake_lock()
        from wvbot.netfix import ensure_dns

        print(f"dns: {ensure_dns()}")
        print("dashboard: http://127.0.0.1:8787/  (adb forward tcp:8787 tcp:8787)")
    else:
        print("dashboard: http://127.0.0.1:8787/")

    if args.mode == "web":
        from wvbot.webbot import main as web_main

        return web_main()

    if args.mode == "farm":
        os.environ["WV_BOT_FARM"] = "1"

    forwarded: list[str] = []
    if args.mode in ("protocol", "farm"):
        forwarded.extend(["--mode", args.mode])
    forwarded.extend(extra)
    sys.argv = [sys.argv[0], *forwarded]

    from wvbot.__main__ import main as protocol_main

    return protocol_main()


if __name__ == "__main__":
    raise SystemExit(main())
