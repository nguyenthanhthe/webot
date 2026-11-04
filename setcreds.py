"""Store the Wolvesville account credentials on the phone.

Run inside Termux:

    python3 ~/wv-bot/setcreds.py

The credentials are written to ``~/.wv-bot/config.json`` (mode 600) and the
bot is restarted so it picks them up.  Nothing is sent anywhere else - the
config file never leaves the device and is never committed to git.
"""

from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("HOME", "/data/data/com.termux/files/home"))
STATE = HOME / ".wv-bot"
CONFIG = STATE / "config.json"
ROOT = HOME / "wv-bot"
PREFIX = Path("/data/data/com.termux/files/usr")
PIDFILE = PREFIX / "tmp/bot.pid"
LOG = PREFIX / "tmp/bot.log"


def _stop_bot() -> None:
    if not PIDFILE.is_file():
        return
    try:
        pid = int(PIDFILE.read_text(encoding="utf-8").strip())
        os.kill(pid, 15)
        print(f"stopped bot (pid {pid})")
    except (OSError, ValueError):
        pass


def _start_bot() -> None:
    if not (ROOT / "run.py").is_file():
        print("bot package is missing, cannot start")
        return
    with open(LOG, "ab") as log:
        process = subprocess.Popen(
            [sys.executable, "run.py"],
            cwd=str(ROOT),
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    PIDFILE.parent.mkdir(parents=True, exist_ok=True)
    PIDFILE.write_text(str(process.pid), encoding="utf-8")
    print(f"bot started (pid {process.pid})")


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if CONFIG.is_file():
        try:
            data = json.loads(CONFIG.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}

    print("Wolvesville account (stored on this device only)")
    email = input("email   : ").strip()
    password = getpass.getpass("password: ")
    if not email or not password:
        print("email and password are required")
        return 1

    data["email"] = email
    data["password"] = password
    CONFIG.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(CONFIG, 0o600)
    print(f"saved {CONFIG}")

    _stop_bot()
    time.sleep(1)
    _start_bot()
    print("dashboard: http://127.0.0.1:8787/  (adb forward tcp:8787 tcp:8787)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
