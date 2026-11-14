"""Launch a headless Firefox inside Termux or desktop hosts.

Firefox provides a native browser TLS stack when a real browser session
is needed for one-off interactive login.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

log = logging.getLogger("wvbot.firefox")

PREFIX = Path("/data/data/com.termux/files/usr")
TERMUX_HOME = Path("/data/data/com.termux/files/home")
FIREFOX = PREFIX / "bin" / "firefox"
# The profile has to survive a reboot: it holds the logged-in session.
PROFILE_DIR = TERMUX_HOME / ".wv-bot" / "ffprofile"

_FALLBACK_FIREFOX_PATHS = (
    "/usr/bin/firefox",
    "/usr/bin/firefox-esr",
    "/snap/bin/firefox",
    "/Applications/Firefox.app/Contents/MacOS/firefox",
    r"C:\Program Files\Mozilla Firefox\firefox.exe",
    r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
)


def find_firefox() -> str:
    """Locate the Firefox executable across Termux, Linux, macOS, and Windows."""
    if FIREFOX.is_file():
        return str(FIREFOX)
    for name in ("firefox", "firefox-esr"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in _FALLBACK_FIREFOX_PATHS:
        if Path(candidate).is_file():
            return candidate
    raise FileNotFoundError("firefox binary not found")


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _configure_marionette_port(profile: Path, port: int) -> None:
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise ValueError(f"invalid marionette port: {port!r}")
    profile.mkdir(parents=True, exist_ok=True)
    user_js = profile / "user.js"
    lines: list[str] = []
    if user_js.is_file():
        try:
            lines = [
                line
                for line in user_js.read_text(encoding="utf-8").splitlines()
                if '"marionette.port"' not in line
            ]
        except OSError:
            lines = []
    lines.append(f'user_pref("marionette.port", {port});')
    user_js.write_text("\n".join(lines) + "\n", encoding="utf-8")


def launch_headless(profile: Path = PROFILE_DIR, marionette_port: int = 2828):
    """Start Firefox headless with Marionette enabled. Returns the process."""
    profile.mkdir(parents=True, exist_ok=True)
    _configure_marionette_port(profile, marionette_port)
    binary = find_firefox()

    env = dict(os.environ)
    env["MOZ_HEADLESS"] = "1"
    env["MOZ_DISABLE_CONTENT_SANDBOX"] = "1"
    env["MOZ_DISABLE_GMP_SANDBOX"] = "1"
    if TERMUX_HOME.is_dir():
        env["HOME"] = str(TERMUX_HOME)
    if (PREFIX / "tmp").is_dir():
        env["TMPDIR"] = str(PREFIX / "tmp")

    command = [
        binary,
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


def wait_for_marionette(
    port: int = 2828,
    timeout: float = 90.0,
    process: subprocess.Popen | None = None,
) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            return False
        if port_open("127.0.0.1", port):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(1.0, remaining))
    return False


def stop(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    if os.name == "posix" and hasattr(os, "killpg"):
        try:
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGTERM)
            process.wait(timeout=15)
            return
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pgid, signal.SIGKILL)
                process.wait(timeout=5)
                return
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001 - fall back to direct process handles
            pass

    try:
        process.terminate()
        process.wait(timeout=15)
    except Exception:  # noqa: BLE001 - best effort
        try:
            process.kill()
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
