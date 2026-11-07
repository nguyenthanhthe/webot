"""Deploy the packaged bot to the phone.

    python wv-bot/tools/deploy.py

The flow that actually works (RUN_COMMAND from adb shell is refused -
the shell uid does not hold com.termux.permission.RUN_COMMAND):

1.  build ``wvbot.zip`` with tools/package.py
2.  ``adb push`` it to ``/data/local/tmp``
3.  unpack and swap the directory as the Termux user
    (``run-as com.termux``), so the files land with the right uid
4.  restart the service with the phone's own ``bot_ctl.sh``
5.  read the dashboard and report the running version

Everything runs over the USB transport, so the Wi-Fi IP does not
matter.  Git Bash path mangling is not an issue because every adb
argument is passed as a single pre-built string.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PORT = 8787
SERIAL = "R9PT609NVNT"
HOME = "/data/data/com.termux/files/home"
UNZIP = "/data/data/com.termux/files/usr/bin/unzip"
ZIP = "wvbot.zip"
ROOT = Path(__file__).resolve().parent.parent


def resolve_serial() -> str:
    res = subprocess.run(["adb", "devices"], capture_output=True, text=True, check=False)
    lines = [line.split()[0] for line in res.stdout.strip().splitlines()[1:] if "\tdevice" in line]
    if SERIAL in lines:
        return SERIAL
    if lines:
        return lines[0]
    return SERIAL


def adb(*args: str, timeout: int = 90) -> str:
    target = resolve_serial()
    result = subprocess.run(
        ["adb", "-s", target, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return result.stdout.strip()


def dashboard(path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def expected_version() -> str:
    init_py = ROOT / "wvbot" / "__init__.py"
    for line in init_py.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=")[1].strip().strip('"').strip("'")
    return "?"


def main() -> int:
    print("== packaging ==")
    subprocess.run(
        [sys.executable, str(ROOT / "tools" / "package.py")],
        cwd=ROOT,
        check=True,
    )

    print("== staging on the phone ==")
    adb("push", str(ROOT / ZIP), "/data/local/tmp/")
    adb("push", str(ROOT / "tools" / "service_runner.sh"), "/data/local/tmp/")
    adb("push", str(ROOT / "tools" / "bot_ctl.sh"), "/data/local/tmp/")
    adb("push", str(ROOT / "tools" / "job_watchdog.sh"), "/data/local/tmp/")
    adb("push", str(ROOT / "tools" / "deploy_phone.sh"), "/data/local/tmp/deploy.sh")

    print("== installing (the phone's own installer) ==")
    install = adb(
        "shell",
        "run-as com.termux sh -c '"
        + "PATH=/data/data/com.termux/files/usr/bin; export PATH; sh /data/local/tmp/deploy.sh"
        + "'",
    )
    print(install or "(no output)")

    print("== restarting the service ==")
    restart = adb(
        "shell",
        "run-as com.termux sh -c '" + f"{HOME}/bot_ctl.sh restart" + "'",
    )
    print(restart or "(no output)")

    print("== waiting for the dashboard ==")
    adb("forward", f"tcp:{PORT}", f"tcp:{PORT}")
    status = {}
    for _ in range(20):
        time.sleep(3)
        try:
            status = dashboard("/api/status")
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
