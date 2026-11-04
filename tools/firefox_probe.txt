"""Probe the Wolvesville web client from a headless Firefox on the phone.

Run inside Termux with PYTHONPATH pointing at the installed wv-bot package.
It reports what the login page looks like so the automated login can be
built against the real DOM instead of guesses.
"""

from __future__ import annotations

import json
import time

from wvbot.firefox import launch_headless, stop, wait_for_marionette
from wvbot.marionette import Marionette

OUT = "/data/data/com.termux/files/usr/tmp/probe_out.txt"
fh = open(OUT, "w")


def log(*parts):
    fh.write(" ".join(str(p) for p in parts) + "\n")
    fh.flush()


process = None
try:
    process = launch_headless()
    log("firefox pid:", process.pid)
    ready = wait_for_marionette(timeout=120)
    log("marionette ready:", ready)
    if not ready:
        raise SystemExit("marionette did not come up")

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
except SystemExit as exc:
    log("STOP:", exc)
except Exception:
    import traceback

    log("FATAL", traceback.format_exc())
finally:
    stop(process)
    fh.close()
