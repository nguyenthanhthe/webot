"""Show what the bot is doing right now.

    python tools/status.py            # summary
    python tools/status.py --events   # + the last events

The phone publishes its dashboard on port 8787; this script opens an adb
forward to it and prints the result.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

PORT = 8787
SERIAL = "R9PT609NVNT"
PHONE_LOG = "/data/data/com.termux/files/home/wv-bot.log"


def parse_adb_devices(output: str) -> list[str]:
    """Return serials of devices in the 'device' state from `adb devices` output."""
    serials: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices"):
            continue
        if "\t" in line:
            serial, state = line.split("\t", 1)
            if state.strip() == "device" and serial.strip():
                serials.append(serial.strip())
        else:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                serials.append(parts[0])
    return serials


def has_connected_device(output: str, serial: str | None = None) -> bool:
    serials = parse_adb_devices(output)
    if not serials:
        return False
    if serial:
        return serial in serials
    return True


def _resolve_serial(explicit: str | None, devices_output: str) -> str:
    serials = parse_adb_devices(devices_output)
    if explicit:
        return explicit
    env_serial = os.environ.get("WV_ADB_SERIAL", "").strip()
    if env_serial:
        return env_serial
    if SERIAL in serials:
        return SERIAL
    return serials[0] if serials else SERIAL


def adb(*args: str, serial: str | None = None) -> str:
    cmd = ["adb"]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(args)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def _resolve_token(explicit: str | None = None) -> str:
    if explicit:
        return explicit.strip()
    env_tok = os.environ.get("WV_BOT_DASHBOARD_TOKEN") or os.environ.get("WV_DASHBOARD_TOKEN")
    if env_tok:
        return env_tok.strip()
    state_dir = Path(os.environ.get("WV_BOT_STATE_DIR") or os.environ.get("STATE_DIR") or (Path.home() / ".wv-bot"))
    tok_path = state_dir / "dashboard_token.txt"
    if tok_path.is_file():
        return tok_path.read_text(encoding="utf-8").strip()
    return ""


def fetch(path: str, port: int = PORT, token: str = ""):
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Dashboard-Token"] = token
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def uptime(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h{minutes:02d}m{secs:02d}s"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show live bot status over ADB")
    parser.add_argument("--events", action="store_true", help="show the event log")
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--serial", default=None, help="ADB device serial (or WV_ADB_SERIAL)")
    parser.add_argument("--port", type=int, default=PORT, help="Dashboard port (default: 8787)")
    parser.add_argument("--token", default=None, help="Dashboard auth token")
    args = parser.parse_args(argv)

    devices = adb("devices")
    if not has_connected_device(devices):
        print("no adb device - the phone is not connected")
        return 1

    target = _resolve_serial(args.serial, devices)
    if not has_connected_device(devices, target):
        print(f"adb device {target!r} is not connected")
        return 1

    adb("forward", f"tcp:{args.port}", f"tcp:{args.port}", serial=target)
    time.sleep(0.5)

    token = _resolve_token(args.token)
    status = fetch("/api/status", port=args.port, token=token)
    if status is None:
        print(f"the bot does not answer on port {args.port} - is it running?")
        print(adb("shell", "run-as", "com.termux", "tail", "-15", PHONE_LOG, serial=target))
        return 1

    print("=== bot status ===")
    for key in (
        "version", "status", "phase", "room", "game_id", "role", "players", "alive",
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
    for line in adb("shell", "ps", "-A", serial=target).splitlines():
        if "firefox" in line or "run.py" in line:
            fields = line.split()
            print("  " + " ".join(fields[-8:]))

    if args.events:
        events = fetch("/api/events", port=args.port, token=token) or []
        print(f"=== last {min(args.count, len(events))} events ===")
        for event in events[-args.count :]:
            stamp_value = event.get("t") or event.get("ts") or 0
            if stamp_value > 1e12:
                stamp_value /= 1000
            stamp = time.strftime("%H:%M:%S", time.localtime(stamp_value))
            print(f"  {stamp} [{event['kind']}] {event['message']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
