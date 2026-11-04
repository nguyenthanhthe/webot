"""Show what the bot is doing right now.

    python wv-bot/tools/status.py            # summary
    python wv-bot/tools/status.py --events   # + the last events

The phone publishes its dashboard on port 8787; this script opens an adb
forward to it and prints the result.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

PORT = 8787
SERIAL = "R9PT609NVNT"


def adb(*args: str) -> str:
    try:
        result = subprocess.run(
            ["adb", "-s", SERIAL, *args],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def fetch(path: str):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def uptime(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h{minutes:02d}m{secs:02d}s"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", action="store_true", help="show the event log")
    parser.add_argument("--count", type=int, default=8)
    args = parser.parse_args()

    devices = adb("devices")
    if "device" not in devices:
        print("no adb device - the phone is not connected")
        return 1

    adb("forward", f"tcp:{PORT}", f"tcp:{PORT}")
    time.sleep(0.5)

    status = fetch("/api/status")
    if status is None:
        print(f"the bot does not answer on port {PORT} - is it running?")
        print(adb("shell", "run-as", "com.termux", "tail", "-5",
                   "/data/data/com.termux/files/usr/tmp/bot.log"))
        return 1

    print("=== bot status ===")
    for key in (
        "status", "phase", "room", "game_id", "role", "players", "alive",
        "games_played", "wins", "xp_total", "xp_last", "kills",
        "reconnects", "errors", "uptime_sec",
    ):
        value = status.get(key)
        if key == "uptime_sec":
            value = uptime(value or 0)
        print(f"  {key:<14} {value}")
    if status.get("last_error"):
        print(f"  last_error     {status['last_error']}")

    print("=== processes on the phone ===")
    for line in adb("shell", "ps", "-A").splitlines():
        if "firefox" in line or "run.py" in line:
            fields = line.split()
            print("  " + " ".join(fields[-8:]))

    if args.events:
        events = fetch("/api/events") or []
        print(f"=== last {min(args.count, len(events))} events ===")
        for event in events[-args.count :]:
            stamp_value = event.get("t") or event.get("ts") or 0
            stamp = time.strftime("%H:%M:%S", time.localtime(stamp_value / 1000))
            print(f"  {stamp} [{event['kind']}] {event['message']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
