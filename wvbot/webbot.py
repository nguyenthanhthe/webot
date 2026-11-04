"""Run the web bot inside a headless Firefox on the phone (mini PC).

The same ``payload.js`` that the Playwright runner injects is injected here
through Marionette, so both hosts behave identically:

* the browser handles Cloudflare and the login
* the injected script plays the game over socket.io
* the script reports to the local dashboard with a small HTTP POST

The supervisor keeps the browser alive: if Firefox dies, the page crashes or
the injected script stops answering, everything is restarted.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from .config import Config, load_config, state_path
from .dashboard import StatusStore, start_dashboard
from .firefox import launch_headless, stop, wait_for_marionette
from .marionette import Marionette

log = logging.getLogger("wvbot.webbot")

GAME_URL = "https://www.wolvesville.com/"
PAYLOAD_FILE = "payload.js"
ELEMENT_KEY = "element-6066-11e4-a52e-4f735466cecf"

# Marionette/WebDriver execute a *function body*: the arguments arrive as
# arguments[0], arguments[1], ... and the result has to be returned.

# Fill one of the login inputs the way a human would (React needs the
# native setter plus input/change events).
FILL_JS = """
var kind = arguments[0];
var value = arguments[1];
var inputs = Array.from(document.querySelectorAll('input'));
var hay = function (i) {
  return [i.type, i.name, i.id, i.placeholder, i.getAttribute('aria-label')]
    .filter(Boolean).join(' ').toLowerCase();
};
var el = null;
if (kind === 'email') {
  el = inputs.find(function (i) {
    return i.type === 'email' || hay(i).indexOf('email') !== -1 || hay(i).indexOf('mail') !== -1;
  });
} else if (kind === 'password') {
  el = inputs.find(function (i) {
    return i.type === 'password' || hay(i).indexOf('password') !== -1;
  });
}
if (!el) return false;
var proto = (el instanceof HTMLTextAreaElement)
    ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
el.dispatchEvent(new Event('input', {bubbles: true}));
el.dispatchEvent(new Event('change', {bubbles: true}));
return true;
"""

# Click the first element whose text matches one of the patterns.
CLICK_TEXT_JS = """
var patterns = arguments[0];
var re = new RegExp(patterns.join('|'), 'i');
var nodes = Array.from(document.querySelectorAll('button, a, div, span, label'));
for (var i = nodes.length - 1; i >= 0; i--) {
  var node = nodes[i];
  if (re.test((node.innerText || '').trim())) {
    node.click();
    return true;
  }
}
return false;
"""

HAS_SESSION_JS = """
for (var i = 0; i < Object.keys(window.localStorage).length; i++) {
  var key = Object.keys(window.localStorage)[i];
  var raw = window.localStorage.getItem(key);
  if (raw && (raw.indexOf('idToken') !== -1 || raw.indexOf('accessToken') !== -1)) return true;
}
return false;
"""

PAYLOAD_ALIVE_JS = "return Boolean(window.__wvbotBot && window.__wvbotBot.alive);"
PAYLOAD_STATE_JS = """
if (!window.__wvbotBot) return null;
return {state: window.__wvbotBot.state, events: window.__wvbotBot.drainEvents()};
"""


class WebBot:
    """Headless Firefox + injected payload, supervised."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = StatusStore()
        self.client: Marionette | None = None
        self.process = None
        self.stop_event = threading.Event()
        self._poll_error_shown = False

    # ------------------------------------------------------------------
    def _payload_path(self) -> Path:
        """The payload sits next to the package (copied in by the installer)."""
        here = Path(__file__).resolve().parent.parent
        candidate = here / PAYLOAD_FILE
        if candidate.is_file():
            return candidate
        fallback = state_path(PAYLOAD_FILE)
        if fallback.is_file():
            return fallback
        raise FileNotFoundError(f"{PAYLOAD_FILE} is missing next to the package")

    def _config_js(self) -> str:
        settings = {
            "gameHost": "https://game.api-wolvesville.com",
            "roomNames": self.cfg.room_names,
            "language": self.cfg.language,
            "gameMode": self.cfg.game_mode,
            "claimDoubleXp": self.cfg.claim_double_xp,
            "reportUrl": f"http://127.0.0.1:{self.cfg.dashboard_port}/report",
        }
        return f"window.__WV_CONFIG = {json.dumps(settings)};"

    # ------------------------------------------------------------------
    def _click_text(self, client: Marionette, patterns: list[str]) -> bool:
        try:
            return bool(client.execute_script(CLICK_TEXT_JS, [patterns]))
        except Exception as exc:  # noqa: BLE001 - the UI may not be there
            log.debug("click %s failed: %s", patterns, exc)
            return False

    def _solve_turnstile(self, client: Marionette) -> bool:
        """Click the Turnstile checkbox when it is visible."""
        try:
            found = client.send(
                "WebDriver:FindElement",
                {"using": "css selector", "value": "iframe[src*='challenges.cloudflare.com']"},
            )
            element = found.get(ELEMENT_KEY) or found.get("value", {}).get(ELEMENT_KEY)
            if not element:
                return False
            client.send("WebDriver:SwitchToFrame", {"element": element})
            box = client.send(
                "WebDriver:FindElement",
                {"using": "css selector", "value": "input[type=checkbox], label, .cb-lb"},
            )
            target = box.get(ELEMENT_KEY) or box.get("value", {}).get(ELEMENT_KEY)
            if target:
                client.send("WebDriver:ElementClick", {"id": target})
                client.send("WebDriver:SwitchToParentFrame", {})
                return True
        except Exception as exc:  # noqa: BLE001 - no challenge, or not clickable
            log.debug("turnstile click failed: %s", exc)
            try:
                client.send("WebDriver:SwitchToParentFrame", {})
            except Exception:  # noqa: BLE001
                pass
        return False

    # ------------------------------------------------------------------
    def _login(self, client: Marionette) -> bool:
        """Make sure the page has a session; returns True when it does."""
        if client.execute_script(HAS_SESSION_JS):
            return True
        if not (self.cfg.email and self.cfg.password):
            self.log_event("login", "no session yet - log in once in the browser")
            return False

        self.log_event("login", "logging in with the configured account")
        self._click_text(client, ["Accept", "Đồng ý", "START GAME OK", "I agree"])
        time.sleep(2)
        self._click_text(client, ["Log in", "Log In", "Sign in", "Đăng nhập", "Login"])
        time.sleep(3)

        client.execute_script(FILL_JS, ["email", self.cfg.email])
        client.execute_script(FILL_JS, ["password", self.cfg.password])
        time.sleep(1)
        self._solve_turnstile(client)
        self._click_text(client, ["Log in", "Log In", "Sign in", "Continue", "Tiếp tục"])

        deadline = time.time() + self.cfg.request_timeout_sec * 3
        while time.time() < deadline:
            if client.execute_script(HAS_SESSION_JS):
                self.log_event("login", "login succeeded")
                return True
            time.sleep(2)
        self.log_event("warn", "login did not complete")
        return False

    # ------------------------------------------------------------------
    def _inject(self, client: Marionette) -> None:
        client.execute_script(self._config_js())
        source = self._payload_path().read_text(encoding="utf-8")
        client.execute_script(source)
        present = client.execute_script("return typeof window.__wvbotBot;")
        self.log_event("bot", f"payload injected (present={present})")

    def _session(self) -> None:
        self.process = launch_headless()
        if not wait_for_marionette():
            raise RuntimeError("marionette did not come up")
        self.client = Marionette()
        self.client.connect()
        self.client.new_session()
        self.client.set_script_timeout(30000)
        self.client.navigate(GAME_URL)
        time.sleep(6)

        self._login(self.client)
        self._inject(self.client)
        self.store.update(status="playing", last_error=None)
        self.log_event("bot", "bot is running")

        reinjections = 0
        while not self.stop_event.is_set():
            time.sleep(5)
            try:
                alive = bool(self.client.execute_script(PAYLOAD_ALIVE_JS))
            except Exception as exc:  # noqa: BLE001 - browser died
                self.log_event("error", f"browser check failed: {exc}")
                break

            if not alive:
                # Either the page reloaded (the payload is gone) or the
                # payload crashed.  Re-inject first, restart the browser
                # only when that keeps failing.
                reinjections += 1
                if reinjections > 3:
                    self.log_event("warn", "the payload keeps dying - restarting the browser")
                    break
                self.log_event("warn", "payload not present - injecting it again")
                try:
                    self._inject(self.client)
                except Exception as exc:  # noqa: BLE001
                    self.log_event("error", f"re-injection failed: {exc}")
                continue
            reinjections = 0

            # The page cannot POST to the dashboard (CORS), so the
            # supervisor picks the state and the events up directly.
            try:
                snapshot = self.client.execute_script(PAYLOAD_STATE_JS)
            except Exception as exc:  # noqa: BLE001
                if not self._poll_error_shown:
                    self._poll_error_shown = True
                    self.log_event("error", f"state poll failed: {exc}")
                continue
            self._poll_error_shown = False
            if not isinstance(snapshot, dict):
                continue
            fields = snapshot.get("state")
            if isinstance(fields, dict):
                self.store.update(**fields)
            for event in snapshot.get("events") or []:
                if isinstance(event, dict) and event.get("message"):
                    self.store.add_event(
                        str(event.get("level") or "info"), str(event["message"])
                    )

    # ------------------------------------------------------------------
    def log_event(self, kind: str, message: str) -> None:
        log.info("%s", message)
        self.store.add_event(kind, message)

    def run(self) -> None:
        start_dashboard(self.store, self.cfg.dashboard_host, self.cfg.dashboard_port)
        self.log_event("bot", "web bot starting")
        backoff = self.cfg.reconnect_delay_sec
        while not self.stop_event.is_set():
            try:
                self._session()
                backoff = self.cfg.reconnect_delay_sec
            except Exception as exc:  # noqa: BLE001 - the supervisor never dies
                self.store.update(
                    status="error", last_error=str(exc), errors=self.store.get().get("errors", 0) + 1
                )
                self.log_event("error", f"session failed: {exc}")
            finally:
                if self.client is not None:
                    try:
                        self.client.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self.client = None
                stop(self.process)
                self.process = None
            if self.stop_event.is_set():
                break
            self.store.update(
                status="reconnecting",
                reconnects=self.store.get().get("reconnects", 0) + 1,
            )
            self.log_event("bot", f"restarting in {backoff}s")
            self.stop_event.wait(backoff)
            backoff = min(backoff * 2, self.cfg.reconnect_delay_max_sec)
        self.log_event("bot", "web bot stopped")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s", datefmt="%H:%M:%S"
    )
    cfg = load_config()
    bot = WebBot(cfg)

    def _shutdown(signum, _frame):
        bot.stop_event.set()

    import signal

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):
            pass

    bot.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
