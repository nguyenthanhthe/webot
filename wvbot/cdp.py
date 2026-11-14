"""A tiny Chrome DevTools Protocol client (stdlib only).

The bot needs one thing a plain HTTP client cannot do: solve Cloudflare's
Turnstile once, in a real browser, to obtain the account's refresh token.
Chrome is driven through its DevTools protocol - no Playwright, no
Selenium, no install step.

Only the loopback interface is ever contacted: the DevTools port is opened
by this process itself and every request is built from a literal host and a
validated port.
"""

from __future__ import annotations

import http.client
import json
import logging
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from .ws import WebSocket, WebSocketError

log = logging.getLogger("wvbot.cdp")

CHROME_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/microsoft-edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)

CHROME_BINARIES = (
    "google-chrome",
    "chromium",
    "chromium-browser",
    "chrome",
    "msedge",
    "microsoft-edge",
)

# The DevTools endpoint only ever lives on the loopback interface of this
# machine.
ALLOWED_HOST = "127.0.0.1"


class CdpError(RuntimeError):
    pass


def find_browser() -> str | None:
    for candidate in CHROME_PATHS:
        if Path(candidate).is_file():
            return candidate
    for binary in CHROME_BINARIES:
        found = shutil.which(binary)
        if found:
            return found
    return None


def _validate_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise CdpError(f"invalid devtools port: {port!r}")
    return port


def _validate_ws_url(url: str) -> str:
    """Only allow DevTools websocket URLs on the loopback interface."""
    parts = urlsplit(url)
    if parts.scheme != "ws":
        raise CdpError(f"unexpected websocket scheme: {parts.scheme!r}")
    if parts.hostname != ALLOWED_HOST:
        raise CdpError(f"refusing to attach to host {parts.hostname!r}")
    return url


class Chrome:
    """Drive a Chrome instance over the DevTools protocol."""

    def __init__(self, port: int = 9222, user_data_dir: str = "", headless: bool = False):
        self.port = _validate_port(port)
        self.user_data_dir = user_data_dir
        self.headless = headless
        self.process: subprocess.Popen | None = None
        self._ws: WebSocket | None = None
        self._id = 0

    # ------------------------------------------------------------------
    def _http(self, method: str, path: str) -> str:
        """Talk to the DevTools HTTP endpoint on the loopback interface."""
        connection = http.client.HTTPConnection(ALLOWED_HOST, self.port, timeout=10)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            body = response.read().decode("utf-8", "replace")
            if response.status >= 400:
                raise CdpError(f"HTTP {response.status} for {method} {path}: {body[:200]}")
            return body
        finally:
            connection.close()

    def launch(self) -> None:
        binary = find_browser()
        if binary is None:
            raise CdpError("no Chrome/Edge binary found")
        command = [
            binary,
            f"--remote-debugging-port={self.port}",
        ]
        if self.user_data_dir:
            command.append(f"--user-data-dir={self.user_data_dir}")
        command.extend(
            [
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-features=Translate,OptimizationHints",
                "--window-size=1280,900",
                "about:blank",
            ]
        )
        if self.headless:
            command.append("--headless=new")
        log.info("launching browser: %s", " ".join(command))
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def wait_until_ready(self, timeout: float = 45.0) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise CdpError(f"browser exited early with code {self.process.returncode}")
            try:
                self._http("GET", "/json/version")
                return
            except Exception:  # noqa: BLE001 - browser not up yet
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(1.0, remaining))
        raise CdpError("browser did not expose the devtools port")

    # ------------------------------------------------------------------
    def targets(self) -> list[dict[str, Any]]:
        data = json.loads(self._http("GET", "/json/list"))
        return data if isinstance(data, list) else []

    def new_tab(self, url: str = "about:blank") -> str:
        safe_url = quote(url, safe=":/?=&%#")
        data = json.loads(self._http("PUT", f"/json/new?{safe_url}"))
        return str(data["webSocketDebuggerUrl"])

    def attach(self, ws_url: str) -> None:
        self._ws = WebSocket(_validate_ws_url(ws_url), timeout=60.0)
        self._ws.connect()

    def attach_first_page(self) -> str:
        for target in self.targets():
            if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                self.attach(str(target["webSocketDebuggerUrl"]))
                return str(target.get("url", ""))
        raise CdpError("no page target available")

    # ------------------------------------------------------------------
    def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        if self._ws is None:
            raise CdpError("not attached")
        self._id += 1
        message_id = self._id
        payload = json.dumps({"id": message_id, "method": method, "params": params or {}})
        self._ws.send_text(payload)
        deadline = time.monotonic() + max(0.0, timeout)
        sock = getattr(self._ws, "_sock", None)
        prev_timeout = sock.gettimeout() if sock is not None else None
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CdpError(f"{method} timed out")
                if sock is not None:
                    sock.settimeout(remaining)
                try:
                    raw = self._ws.recv_text()
                except (TimeoutError, socket.timeout) as exc:
                    raise CdpError(f"{method} timed out") from exc
                if raw is None:
                    raise CdpError("browser closed the connection")
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if message.get("id") == message_id:
                    if "error" in message:
                        raise CdpError(f"{method} failed: {message['error']}")
                    return dict(message.get("result", {}))
        finally:
            if sock is not None:
                try:
                    sock.settimeout(prev_timeout)
                except OSError:
                    pass

    def navigate(self, url: str) -> None:
        self.send("Page.enable")
        self.send("Page.navigate", {"url": url})

    def evaluate(self, expression: str, await_promise: bool = False) -> Any:
        result = self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": True,
            },
        )
        if result.get("exceptionDetails"):
            raise CdpError(f"evaluate failed: {result['exceptionDetails']}")
        return (result.get("result") or {}).get("value")

    # ------------------------------------------------------------------
    def click_at(self, x: float, y: float) -> None:
        """Send a real mouse click (needed for cross-origin iframes)."""
        for event_type in ("mousePressed", "mouseReleased"):
            self.send(
                "Input.dispatchMouseEvent",
                {
                    "type": event_type,
                    "x": x,
                    "y": y,
                    "button": "left",
                    "clickCount": 1,
                },
            )

    def wait_for(self, expression: str, timeout: float = 30.0, interval: float = 0.5) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            try:
                if self.evaluate(expression):
                    return True
            except (CdpError, WebSocketError, OSError):
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))
        return False

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
        if self.process is not None:
            try:
                self.process.terminate()
                self.process.wait(timeout=10)
            except Exception:  # noqa: BLE001
                try:
                    self.process.kill()
                    self.process.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
            self.process = None
