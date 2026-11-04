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
from .http_client import HttpClient
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

# Click the innermost element whose text matches one of the patterns.
CLICK_TEXT_JS = """
var patterns = arguments[0];
var re = new RegExp(patterns.join('|'), 'i');
var nodes = Array.from(document.querySelectorAll(
    'button, a, div, span, label, input[type=submit], [role=button]'));
var matches = nodes.filter(function (node) {
  var label = (node.innerText || node.value || '') + ' ' + (node.getAttribute('aria-label') || '');
  return re.test(label.trim());
});
var deepest = matches.filter(function (node) {
  return !matches.some(function (other) {
    return other !== node && node.contains(other);
  });
});
if (!deepest.length) return false;
deepest[0].click();
return true;
"""

# A snapshot of the page, used to report why a login step did not work.
PAGE_STATE_JS = """
return {
  url: location.href,
  inputs: Array.from(document.querySelectorAll('input')).map(function (i) {
    return {type: i.type, name: i.name, id: i.id,
            placeholder: i.placeholder, value: i.value ? '***' : ''};
  }),
  buttons: Array.from(document.querySelectorAll('button, [role=button], a'))
    .map(function (b) { return (b.innerText || '').trim(); })
    .filter(function (t) { return t.length; })
    .slice(0, 25),
  text: (document.body.innerText || '').replace(/\\s+/g, ' ').slice(0, 700)
};
"""

HAS_SESSION_JS = """
for (var i = 0; i < Object.keys(window.localStorage).length; i++) {
  var key = Object.keys(window.localStorage)[i];
  var raw = window.localStorage.getItem(key);
  if (raw && (raw.indexOf('idToken') !== -1 || raw.indexOf('accessToken') !== -1)) return true;
}
return false;
"""

# Plant the session into the page so the official client believes it is
# logged in.  The client then refreshes the token and the Cloudflare
# attestation on its own, and the injected payload picks both up.
SEED_SESSION_JS = """
var tokens = arguments[0];
if (!tokens || !tokens.idToken) return false;
try {
  window.localStorage.setItem('authtokens', JSON.stringify({
    idToken: tokens.idToken,
    refreshToken: tokens.refreshToken || null
  }));
  return true;
} catch (err) {
  return false;
}
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
            "authHost": "https://auth.api-wolvesville.com",
            "refreshToken": self.cfg.refresh_token or None,
            "roomNames": self.cfg.room_names,
            "language": self.cfg.language,
            "gameMode": self.cfg.game_mode,
            "claimDoubleXp": self.cfg.claim_double_xp,
        }
        return f"window.__WV_CONFIG = {json.dumps(settings)};"

    # ------------------------------------------------------------------
    def _mint_id_token(self) -> str:
        """Mint an idToken from the refreshToken.

        The refresh path needs no Cloudflare attestation, so it works
        without a browser.  The idToken is good for an hour.
        """
        if not self.cfg.refresh_token:
            raise RuntimeError("no refresh token configured")
        client = HttpClient()
        data = client.request(
            "POST",
            "https://auth.api-wolesville.com/players/createIdToken",
            body={"refreshToken": self.cfg.refresh_token},
        )
        token = (data or {}).get("idToken")
        if not token:
            raise RuntimeError("createIdToken returned no idToken")
        return token

    def _seed_session(self, client: Marionette) -> bool:
        """Put a fresh session into the page."""
        token = self._mint_id_token()
        ok = bool(client.execute_script(SEED_SESSION_JS, [{"idToken": token, "refreshToken": self.cfg.refresh_token}]))
        self.log_event("login", f"session seeded (token length {len(token)})")
        return ok

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

        # A refreshToken is enough: it mints idTokens without a browser.
        if self.cfg.refresh_token:
            try:
                if self._seed_session(client):
                    time.sleep(2)
                    if client.execute_script(HAS_SESSION_JS):
                        return True
            except Exception as exc:  # noqa: BLE001 - network hiccup
                self.log_event("warn", f"session seeding failed: {exc}")
                return False

        if not (self.cfg.email and self.cfg.password):
            self.log_event("login", "no session yet - log in once in the browser")
            return False

        self.log_event("login", "logging in with the configured account")

        # 1. the consent screen (age + terms) has to be accepted first
        self._click_text(client, ["I am 16", "I have read", "16 years", "agree"])
        time.sleep(1)
        self._click_text(client, ["Accept", "Đồng ý", "OK", "START GAME OK"])
        time.sleep(2)

        # 2. open the login form
        opened = self._click_text(
            client, ["Log in", "Log In", "Login", "Sign in", "Sign In", "Đăng nhập"]
        )
        self.log_event("login", f"login form {'opened' if opened else 'not found'}")
        time.sleep(3)

        # 3. fill the form
        filled_email = client.execute_script(FILL_JS, ["email", self.cfg.email])
        filled_password = client.execute_script(FILL_JS, ["password", self.cfg.password])
        self.log_event(
            "login", f"form filled (email={filled_email}, password={filled_password})"
        )
        time.sleep(1)

        # 4. the Turnstile checkbox, if it is visible
        if self._solve_turnstile(client):
            self.log_event("login", "turnstile solved")
            time.sleep(2)

        # 5. submit
        self._click_text(
            client, ["Log in", "Log In", "Login", "Sign in", "Continue", "Tiếp tục"]
        )

        deadline = time.time() + self.cfg.request_timeout_sec * 3
        while time.time() < deadline:
            if client.execute_script(HAS_SESSION_JS):
                self.log_event("login", "login succeeded")
                return True
            time.sleep(2)

        state = client.execute_script(PAGE_STATE_JS)
        self.log_event("warn", f"login did not complete; page: {json.dumps(state)[:600]}")
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

        if not self._login(self.client):
            # A failed login is retried by the supervisor (new browser).
            raise RuntimeError("login failed")
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
