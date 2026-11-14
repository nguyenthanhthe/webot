"""Probe the Wolvesville web client from headless Firefox on Termux.

Run inside Termux or desktop repo root:
    python3 firefox_probe.py [--out PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE if (_HERE / "wvbot").is_dir() else _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from wvbot.firefox import launch_headless, stop, wait_for_marionette  # noqa: E402
from wvbot.marionette import Marionette  # noqa: E402

DEFAULT_OUT = Path(os.environ.get("TMPDIR", "/data/data/com.termux/files/usr/tmp")) / "probe_out.txt"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe Wolvesville login DOM via Firefox Marionette")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Log output path")
    args = parser.parse_args(argv)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    process = None
    with args.out.open("w", encoding="utf-8") as fh:
        def log(*parts: object) -> None:
            fh.write(" ".join(str(p) for p in parts) + "\n")
            fh.flush()

        try:
            process = launch_headless()
            log("firefox pid:", process.pid)
            ready = wait_for_marionette(timeout=120)
            log("marionette ready:", ready)
            if not ready:
                log("STOP: marionette did not come up")
                return 1

            client = Marionette()
            client.connect()
            client.new_session()
            client.set_script_timeout(30000)
            client.navigate("https://www.wolvesville.com/")
            time.sleep(10)

            log("url:", client.get_url())
            log("title:", client.get_title())
            info = client.execute_script(
                """
                return {
                  inputs: Array.from(document.querySelectorAll('input')).map(i => ({
                    type: i.type, name: i.name, id: i.id, placeholder: i.placeholder,
                    aria: i.getAttribute('aria-label')
                  })),
                  buttons: Array.from(document.querySelectorAll('button'))
                             .map(b => (b.innerText || '').trim()).filter(Boolean).slice(0, 30),
                  links: Array.from(document.querySelectorAll('a'))
                            .map(a => (a.innerText || '').trim()).filter(Boolean).slice(0, 30),
                  bodyText: (document.body.innerText || '').replace(/\\s+/g, ' ').slice(0, 700),
                  localStorageKeys: Object.keys(localStorage),
                  iframes: Array.from(document.querySelectorAll('iframe')).map(f => f.src).slice(0, 10)
                };
                """
            )
            log(json.dumps(info, indent=1)[:4000])
            client.delete_session()
        except Exception:
            log("FATAL", traceback.format_exc())
            return 1
        finally:
            stop(process)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
