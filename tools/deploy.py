"""Deploy the packaged bot to the phone.

    python tools/deploy.py [--serial SERIAL] [--token TOKEN]

Flow:
1. Build ``wvbot.zip`` with tools/package.py
2. ``adb push`` bundle + Termux service/boot scripts to ``/data/local/tmp``
3. Run ``deploy_phone.sh`` inside Termux via ``run-as com.termux``
4. Restart the service via ``~/bot_ctl.sh restart``
5. Forward the dashboard port and verify the deployed version
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PORT = 8787
SERIAL = "R9PT609NVNT"
HOME = "/data/data/com.termux/files/home"
ZIP = "wvbot.zip"
ROOT = Path(__file__).resolve().parent.parent


def resolve_serial(explicit: str | None = None) -> str:
    env_serial = explicit or os.environ.get("WV_ADB_SERIAL", "").strip()
    if env_serial:
        return env_serial
    res = subprocess.run(["adb", "devices"], capture_output=True, text=True, check=False)
    lines = [
        line.split("\t")[0].strip()
        for line in res.stdout.splitlines()
        if "\tdevice" in line and line.strip()
    ]
    if SERIAL in lines:
        return SERIAL
    if lines:
        return lines[0]
    return SERIAL


def adb(*args: str, serial: str | None = None, timeout: int = 90, check: bool = True) -> str:
    target = resolve_serial(serial)
    result = subprocess.run(
        ["adb", "-s", target, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"adb -s {target} {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def _resolve_token(explicit: str | None = None) -> str:
    if explicit:
        return explicit.strip()
    env_tok = os.environ.get("WV_BOT_DASHBOARD_TOKEN") or os.environ.get("WV_DASHBOARD_TOKEN")
    if env_tok:
        return env_tok.strip()
    state_dir = Path(os.environ.get("WV_BOT_STATE_DIR") or os.environ.get("STATE_DIR") or (Path.home() / ".wv-bot"))
    tok_file = state_dir / "dashboard_token.txt"
    if tok_file.is_file():
        return tok_file.read_text(encoding="utf-8").strip()
    return ""


def dashboard(path: str, port: int = PORT, token: str = "") -> dict:
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Dashboard-Token"] = token
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def expected_version() -> str:
    init_py = ROOT / "wvbot" / "__init__.py"
    for line in init_py.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return "?"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Package and deploy wvbot to an Android Termux device")
    parser.add_argument("--serial", default=None, help="ADB device serial (or WV_ADB_SERIAL)")
    parser.add_argument("--token", default=None, help="Dashboard auth token (if enabled)")
    args = parser.parse_args(argv)

    target = resolve_serial(args.serial)
    token = _resolve_token(args.token)

    print("== packaging ==")
    subprocess.run(
        [sys.executable, str(ROOT / "tools" / "package.py")],
        cwd=ROOT,
        check=True,
    )

    print(f"== staging on the phone ({target}) ==")
    stage_files = (
        (ROOT / ZIP, "/data/local/tmp/"),
        (ROOT / "tools" / "service_runner.sh", "/data/local/tmp/"),
        (ROOT / "tools" / "bot_ctl.sh", "/data/local/tmp/"),
        (ROOT / "tools" / "bot_start.sh", "/data/local/tmp/"),
        (ROOT / "tools" / "boot_wvbot", "/data/local/tmp/"),
        (ROOT / "tools" / "job_watchdog.sh", "/data/local/tmp/"),
        (ROOT / "tools" / "deploy_phone.sh", "/data/local/tmp/deploy.sh"),
    )
    for src, dst in stage_files:
        if not src.is_file():
            raise SystemExit(f"missing deploy artifact: {src}")
        adb("push", str(src), dst, serial=target)

    print("== installing (the phone's own installer) ==")
    install = adb(
        "shell",
        "run-as com.termux sh -c '"
        + "PATH=/data/data/com.termux/files/usr/bin; export PATH; sh /data/local/tmp/deploy.sh"
        + "'",
        serial=target,
    )
    print(install or "(no output)")

    print("== restarting the service ==")
    restart = adb(
        "shell",
        f"run-as com.termux sh -c '{HOME}/bot_ctl.sh restart'",
        serial=target,
    )
    print(restart or "(no output)")

    print("== waiting for the dashboard ==")
    adb("forward", f"tcp:{PORT}", f"tcp:{PORT}", serial=target)
    status: dict = {}
    for _ in range(20):
        time.sleep(3)
        try:
            status = dashboard("/api/status", token=token)
            break
        except Exception:  # noqa: BLE001 - the server needs a moment
            continue
    if not status:
        raise SystemExit("the dashboard never came up")

    print(
        "deployed: version {} status {}".format(
            status.get("version"),
            status.get("status"),
        )
    )
    expected = expected_version()
    if status.get("version") != expected:
        print(
            f"WARNING: the dashboard reports {status.get('version')} "
            f"but the package carries {expected}"
        )
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
