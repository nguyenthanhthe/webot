"""Package the bot into a zip that can be dropped into Termux.

    python wv-bot/tools/package.py

The archive contains the ``wvbot`` package, the ``run.py`` launcher and a
default ``config.json``.  It is copied to the phone and unpacked into
``~/wv-bot`` by the Termux profile hook.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "wvbot.zip"

MEMBERS = [
    (ROOT / "wvbot" / "__init__.py", "wvbot/__init__.py"),
    (ROOT / "wvbot" / "__main__.py", "wvbot/__main__.py"),
    (ROOT / "wvbot" / "api.py", "wvbot/api.py"),
    (ROOT / "wvbot" / "auth.py", "wvbot/auth.py"),
    (ROOT / "wvbot" / "bot.py", "wvbot/bot.py"),
    (ROOT / "wvbot" / "config.py", "wvbot/config.py"),
    (ROOT / "wvbot" / "dashboard.py", "wvbot/dashboard.py"),
    (ROOT / "wvbot" / "farm.py", "wvbot/farm.py"),
    (ROOT / "wvbot" / "firefox.py", "wvbot/firefox.py"),
    (ROOT / "wvbot" / "game.py", "wvbot/game.py"),
    (ROOT / "wvbot" / "http_client.py", "wvbot/http_client.py"),
    (ROOT / "wvbot" / "history.py", "wvbot/history.py"),
    (ROOT / "wvbot" / "marionette.py", "wvbot/marionette.py"),
    (ROOT / "wvbot" / "netfix.py", "wvbot/netfix.py"),
    (ROOT / "wvbot" / "rewards.py", "wvbot/rewards.py"),
    (ROOT / "wvbot" / "sio.py", "wvbot/sio.py"),
    (ROOT / "wvbot" / "stream.py", "wvbot/stream.py"),
    (ROOT / "wvbot" / "updates.py", "wvbot/updates.py"),
    (ROOT / "wvbot" / "ws.py", "wvbot/ws.py"),
    (ROOT / "wvbot" / "webbot.py", "wvbot/webbot.py"),
    (ROOT / "run.py", "run.py"),
    (ROOT / "setcreds.py", "setcreds.py"),
    (ROOT / "config.json", "config.json"),
    # A small diagnostic that checks the API from the phone itself.
    (ROOT / "tools" / "probe_phone.py", "probe_phone.py"),
    (ROOT / "tools" / "probe_socket.py", "probe_socket.py"),
    (ROOT / "tools" / "check_hosts.py", "check_hosts.py"),
    (ROOT / "tools" / "show_lines.py", "show_lines.py"),
    (ROOT / "tools" / "probe_own_id.py", "probe_own_id.py"),
    (ROOT / "tools" / "probe_rewards.py", "probe_rewards.py"),
    (ROOT / "tools" / "roster.py", "roster.py"),
    # The payload is shared with the Node/Playwright runner and lives at the
    # root of the package on the phone.
    (ROOT / "web" / "payload.js", "payload.js"),
]


def main() -> None:
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, arcname in MEMBERS:
            if not path.is_file():
                raise SystemExit(f"missing member: {path}")
            zf.write(path, arcname)
    digest = hashlib.sha256(OUT.read_bytes()).hexdigest()
    print(f"{OUT}  {OUT.stat().st_size} bytes  sha256={digest}")


if __name__ == "__main__":
    main()
