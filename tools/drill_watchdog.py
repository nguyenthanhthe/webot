"""Watchdog inspection and verification drill.

Usage:
    python tools/drill_watchdog.py [--serial SERIAL] [--check | --drill]

Options:
    --check   Read current heartbeat and report runner health (non-destructive).
    --drill   Simulate a stale heartbeat test to verify watchdog kill/restart.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time

DEFAULT_SERIAL = os.environ.get("WV_ADB_SERIAL", "").strip()
HEARTBEAT_PATH = "/data/data/com.termux/files/home/.wv-bot/heartbeat.txt"
LOCK_PATH = "/data/data/com.termux/files/home/service/bot.lock"
LOG_PATH = "/data/data/com.termux/files/home/wv-bot.log"
WATCHDOG_SCRIPT = "/data/data/com.termux/files/home/service/watchdog.sh"
STALE_SEC = 8000


def parse_connected_serials(devices_output: str) -> list[str]:
    serials: list[str] = []
    for line in devices_output.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def resolve_serial(explicit: str | None = None) -> str:
    candidate = (explicit or os.environ.get("WV_ADB_SERIAL", "") or DEFAULT_SERIAL).strip()
    if candidate:
        return candidate
    try:
        res = subprocess.run(["adb", "devices"], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return ""
    serials = parse_connected_serials(res.stdout)
    return serials[0] if serials else ""


def adb_termux(cmd: str, serial: str) -> str:
    res = subprocess.run(
        ["adb", "-s", serial, "shell", f"run-as com.termux sh -c {shlex.quote(cmd)}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0 and res.stderr.strip():
        raise RuntimeError(f"adb command failed ({res.returncode}): {res.stderr.strip()}")
    return res.stdout.strip()


def check_status(serial: str) -> int:
    print(f"=== Watchdog Status Check (Target: {serial}) ===")

    raw_beat = adb_termux(f"cat {shlex.quote(HEARTBEAT_PATH)} 2>/dev/null || true", serial)
    if not raw_beat:
        print(f"Heartbeat file not found or empty at {HEARTBEAT_PATH}")
    else:
        try:
            beat = float(raw_beat.strip())
            dev_now_str = adb_termux("date +%s", serial)
            dev_now = float(dev_now_str) if dev_now_str.isdigit() else time.time()
            age = dev_now - beat
            print(f"Heartbeat timestamp: {beat} (device age: {age:.1f}s)")
            if age > STALE_SEC:
                print(f"  [ALERT] Heartbeat is stale (>{STALE_SEC}s). Watchdog will kill and restart.")
            else:
                print(f"  [OK] Heartbeat is fresh (<={STALE_SEC}s). Bot is healthy.")
        except ValueError:
            print(f"Heartbeat contains invalid timestamp: {raw_beat!r}")

    lock_pid = adb_termux(f"cat {shlex.quote(LOCK_PATH + '/pid')} 2>/dev/null || true", serial)
    if lock_pid:
        print(f"Service runner lock active: pid {lock_pid}")
    else:
        print("Service runner lock not found.")

    log_size = adb_termux(f"wc -c < {shlex.quote(LOG_PATH)} 2>/dev/null || true", serial).strip()
    if log_size.isdigit():
        mb = int(log_size) / (1024 * 1024)
        print(f"Log size: {log_size} bytes ({mb:.2f} MB / max 5.00 MB)")
    return 0


def run_drill(serial: str) -> int:
    print(f"=== Watchdog Stale-Heartbeat Drill (Target: {serial}) ===")
    old_pid = adb_termux(f"cat {shlex.quote(LOCK_PATH + '/pid')} 2>/dev/null || true", serial)
    orig_beat = adb_termux(f"cat {shlex.quote(HEARTBEAT_PATH)} 2>/dev/null || true", serial)
    print(f"Initial runner lock pid: {old_pid or '(none)'}")

    dev_now_str = adb_termux("date +%s", serial)
    dev_now = int(dev_now_str) if dev_now_str.isdigit() else int(time.time())
    stale_ts = max(1, dev_now - (STALE_SEC + 600))

    try:
        adb_termux(
            f"mkdir -p {shlex.quote(LOCK_PATH)} && "
            f"mkdir -p $(dirname {shlex.quote(HEARTBEAT_PATH)}) && "
            f"printf '%s\\n' {shlex.quote(str(stale_ts))} > {shlex.quote(HEARTBEAT_PATH)}",
            serial,
        )
        print(f"Injected stale heartbeat: {stale_ts} (age > {STALE_SEC}s)")
        output = adb_termux(f"sh {shlex.quote(WATCHDOG_SCRIPT)}", serial)
        print("Watchdog output:")
        print(output or "(no output)")
        time.sleep(3)
        new_pid = adb_termux(f"cat {shlex.quote(LOCK_PATH + '/pid')} 2>/dev/null || true", serial)
        print(f"Post-drill runner lock pid: {new_pid or '(none)'}")
    finally:
        if orig_beat:
            # Only restore if watchdog hasn't already written a fresh heartbeat.
            now_beat = adb_termux(f"cat {shlex.quote(HEARTBEAT_PATH)} 2>/dev/null || true", serial)
            if now_beat.strip() == str(stale_ts):
                adb_termux(
                    f"printf '%s\\n' {shlex.quote(orig_beat)} > {shlex.quote(HEARTBEAT_PATH)}",
                    serial,
                )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Watchdog status and drill tool")
    parser.add_argument(
        "--serial",
        default=None,
        help="ADB device serial (default: WV_ADB_SERIAL env var or auto-detected connected device)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Check current heartbeat (default)")
    mode.add_argument("--drill", action="store_true", help="Simulate stale heartbeat and trigger watchdog")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    serial = resolve_serial(args.serial)
    if not serial:
        print("ERROR: no ADB device connected and no --serial / WV_ADB_SERIAL specified", file=sys.stderr)
        return 1
    try:
        if args.drill:
            return run_drill(serial)
        return check_status(serial)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
