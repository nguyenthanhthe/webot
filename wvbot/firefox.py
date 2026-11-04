"""Launch a headless Firefox inside Termux.

Firefox is the only TLS stack on the device that Cloudflare accepts without
a detour, so it is used for the one-off login (Turnstile) and then closed
again - the bot itself runs as plain Python.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import time
from pathlib import Path

log = logging.getLogger("wvbot.firefox")

PREFIX = Path("/data/data/com.termux/files/usr")
FIREFOX = PREFIX / "bin" / "firefox"
# The profile has to survive a reboot: it holds the logged-in session.
PROFILE_DIR = Path("/data/data/com.termux/files/home/.wv-bot/ffprofile")


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def launch_headless(profile: Path = PROFILE_DIR, marionette_port: int = 2828):
    """Start Firefox headless with Marionette enabled. Returns the process."""
    profile.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["MOZ_HEADLESS"] = "1"
    env["MOZ_DISABLE_CONTENT_SANDBOX"] = "1"
    env["MOZ_DISABLE_GMP_SANDBOX"] = "1"
    env["HOME"] = str(PREFIX / "files/home")
    env["TMPDIR"] = str(PREFIX / "tmp")

    command = [
        str(FIREFOX),
        "--headless",
        "--marionette",
        "--no-remote",
        "--profile",
        str(profile),
        "about:blank",
    ]
    log.info("launching firefox: %s", " ".join(command))
    process = subprocess.Popen(
        command,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return process


def wait_for_marionette(port: int = 2828, timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open("127.0.0.1", port):
            return True
        time.sleep(1)
    return False


def stop(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    try:
        process.terminate()
        process.wait(timeout=15)
    except Exception:  # noqa: BLE001 - best effort
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass
