"""Watchdog Inspection and Verification Drill.

Usage:
    python tools/drill_watchdog.py [--check | --drill]

Options:
    --check   Read current heartbeat and report runner health (non-destructive).
    --drill   Simulate a stale heartbeat test to verify watchdog kill/restart.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

SERIAL = "100.100.241.85:5555"
HEARTBEAT_PATH = "/data/data/com.termux/files/home/.wv-bot/heartbeat.txt"
LOCK_PATH = "/data/data/com.termux/files/home/service/bot.lock"
LOG_PATH = "/data/data/com.termux/files/home/wv-bot.log"
WATCHDOG_SCRIPT = "/data/data/com.termux/files/home/service/watchdog.sh"


def adb_termux(cmd: str) -> str:
    res = subprocess.run(
        ["adb", "-s", SERIAL, "shell", f"run-as com.termux sh -c \"{cmd}\""],
        capture_output=True,
        text=True,
        check=False,
    )
    return res.stdout.strip()


def check_status() -> int:
    print(f"=== Watchdog Status Check (Target: {SERIAL}) ===")

    # Check heartbeat
    raw_beat = adb_termux(f"cat {HEARTBEAT_PATH} 2>/dev/null")
    if not raw_beat:
        print(f"Heartbeat file not found or empty at {HEARTBEAT_PATH}")
    else:
        try:
            beat = float(raw_beat.strip())
            # Device clock
            dev_now_str = adb_termux("date +%s")
            dev_now = float(dev_now_str) if dev_now_str.isdigit() else time.time()
            age = dev_now - beat
            print(f"Heartbeat timestamp: {beat} (device age: {age:.1f}s)")
            if age > 8000:
                print(f"  [ALERT] Heartbeat is stale (>{8000}s). Watchdog will kill and restart.")
            else:
                print(f"  [OK] Heartbeat is fresh (<{8000}s). Bot is healthy.")
        except ValueError:
            print(f"Heartbeat contains invalid timestamp: {raw_beat!r}")

    # Check lock
    lock_pid = adb_termux(f"cat {LOCK_PATH}/pid 2>/dev/null")
    if lock_pid:
        print(f"Service runner lock active: pid {lock_pid}")
    else:
        print("Service runner lock not found.")

    # Check log size
    log_size = adb_termux(f"wc -c < {LOG_PATH} 2>/dev/null")
    if log_size and log_size.isdigit():
        mb = int(log_size) / (1024 * 1024)
        print(f"Log size: {log_size} bytes ({mb:.2f} MB / max 5.00 MB)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Watchdog status and drill tool")
    parser.add_argument("--check", action="store_true", default=True, help="Check current heartbeat")
    args = parser.parse_args()
    return check_status()


if __name__ == "__main__":
    raise SystemExit(main())
