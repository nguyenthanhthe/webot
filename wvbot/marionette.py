"""A very small Marionette client (Firefox remote protocol).

Marionette speaks a simple framing: ``<length>:<json>``.  We only need a
handful of commands to drive a headless Firefox: open a page, run JavaScript,
read the storage.  No third-party dependency, so it installs on Termux
without a compiler.

The point of using a real browser is that Cloudflare's Turnstile widget and
the TLS fingerprint both behave like they do for a human - the bot itself
never needs them again once it holds a refresh token.
"""

from __future__ import annotations

import json
import logging
import socket
import time
from typing import Any

log = logging.getLogger("wvbot.marionette")

DEFAULT_PORT = 2828


class MarionetteError(RuntimeError):
    pass


class Marionette:
    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT, timeout: float = 60.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._msg_id = 0
        self._buffer = b""

    # ------------------------------------------------------------------
    def connect(self, retries: int = 30, delay: float = 1.0) -> None:
        last: Exception | None = None
        for _ in range(retries):
            try:
                self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
                self._sock.settimeout(self.timeout)
                self._read_packet()  # server handshake
                return
            except OSError as exc:
                last = exc
                time.sleep(delay)
        raise MarionetteError(f"cannot reach marionette: {last}")

    # ------------------------------------------------------------------
    def _read_exact(self, count: int) -> bytes:
        assert self._sock is not None
        while len(self._buffer) < count:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise MarionetteError("connection closed by marionette")
            self._buffer += chunk
        out, self._buffer = self._buffer[:count], self._buffer[count:]
        return out

    def _read_packet(self) -> list[Any]:
        digits = b""
        while True:
            char = self._read_exact(1)
            if char == b":":
                break
            if not char.isdigit():
                raise MarionetteError(f"bad packet length prefix: {digits!r}")
            digits += char
        length = int(digits)
        payload = self._read_exact(length)
        return json.loads(payload.decode("utf-8"))

    def send(self, command: str, params: dict[str, Any] | None = None) -> Any:
        if self._sock is None:
            raise MarionetteError("not connected")
        self._msg_id += 1
        message = [0, self._msg_id, command, params or {}]
        payload = json.dumps(message).encode("utf-8")
        self._sock.sendall(str(len(payload)).encode("ascii") + b":" + payload)

        while True:
            packet = self._read_packet()
            if not isinstance(packet, list) or len(packet) < 4:
                continue
            if packet[0] == 1 and packet[1] == self._msg_id:
                error = packet[2]
                result = packet[3]
                if error:
                    raise MarionetteError(f"{command} failed: {error}")
                return result

    # -- commands ------------------------------------------------------
    def new_session(self) -> Any:
        return self.send("WebDriver:NewSession", {"capabilities": {}})

    def navigate(self, url: str) -> Any:
        return self.send("WebDriver:Navigate", {"url": url})

    def execute_script(self, script: str, args: list[Any] | None = None) -> Any:
        result = self.send(
            "WebDriver:ExecuteScript",
            {"script": script, "args": args or [], "newSandbox": False},
        )
        # Marionette wraps the return value in {"value": ...}.
        if isinstance(result, dict) and "value" in result:
            return result["value"]
        return result

    def get_url(self) -> str:
        return self.send("WebDriver:GetCurrentURL", {})

    def get_title(self) -> str:
        return self.send("WebDriver:GetTitle", {})

    def set_script_timeout(self, milliseconds: int) -> Any:
        return self.send("WebDriver:SetTimeouts", {"script": milliseconds})

    def delete_session(self) -> Any:
        try:
            return self.send("WebDriver:DeleteSession", {})
        except Exception:  # noqa: BLE001 - firefox may be gone already
            return None

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
