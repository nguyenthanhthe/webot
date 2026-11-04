"""Minimal socket.io v4 client on top of :mod:`wvbot.ws`.

Engine.IO v4 handshake over the websocket transport only:

    GET wss://host/socket.io/?EIO=4&transport=websocket&<query>
    <- 0{"sid": ...}            server open
    -> 40{"firebaseToken":...}  client namespace connect (auth payload)
    <- 40{"sid": ...}           namespace connected
    <- 42["event", payload]     server event
    -> 42["event", payload]     client event
    <- 2                        engine ping  ->  3  engine pong

The official client also sends the Cloudflare attestation in the namespace
``auth`` payload, and the Firebase idToken in the handshake query string.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.parse
from typing import Any, Callable

from .ws import WebSocket, WebSocketError

log = logging.getLogger("wvbot.sio")

EVENT = "EVENT"


class SocketIOError(RuntimeError):
    pass


class SocketIOClient:
    """A single-namespace socket.io client."""

    def __init__(
        self,
        base_url: str,
        query: dict[str, Any] | None = None,
        auth: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        namespace: str = "/",
    ):
        self.base_url = base_url.rstrip("/")
        self.query = dict(query or {})
        self.auth = dict(auth or {})
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.namespace = namespace

        self.ws: WebSocket | None = None
        self.connected = False
        self.sid: str | None = None
        self._handlers: dict[str, list[Callable[..., Any]]] = {}
        self._any_handler: list[Callable[..., Any]] = []
        self._lock = threading.Lock()
        self._closed = threading.Event()

    # ------------------------------------------------------------------
    def _ws_url(self) -> str:
        scheme = "wss" if self.base_url.startswith("https://") else "ws"
        host = self.base_url.split("://", 1)[-1]
        params = {"EIO": "4", "transport": "websocket"}
        params.update({k: str(v) for k, v in self.query.items() if v is not None})
        query = urllib.parse.urlencode(params)
        return f"{scheme}://{host}/socket.io/?{query}"

    def on(self, event: str, handler: Callable[..., Any]) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def on_any(self, handler: Callable[..., Any]) -> None:
        self._any_handler.append(handler)

    # ------------------------------------------------------------------
    def connect(self) -> None:
        url = self._ws_url()
        log.debug("connecting to %s", url)
        self.ws = WebSocket(url, timeout=self.timeout, extra_headers=self.headers)
        self.ws.connect()

        raw = self.ws.recv_text()
        if raw is None or not raw.startswith("0"):
            raise SocketIOError(f"unexpected engine.io open packet: {raw!r}")
        payload = json.loads(raw[1:]) if len(raw) > 1 else {}
        self.sid = payload.get("sid")

        # Namespace connect; the auth object is the payload of packet 40.
        if self.auth:
            self.ws.send_text("40" + json.dumps(self.auth, separators=(",", ":")))
        else:
            self.ws.send_text("40")

        deadline_packets = 0
        while deadline_packets < 10:
            raw = self.ws.recv_text()
            if raw is None:
                raise SocketIOError("connection closed during handshake")
            deadline_packets += 1
            if raw.startswith("40"):
                self.connected = True
                log.info("socket connected (sid=%s)", self.sid)
                return
            if raw.startswith("44"):
                raise SocketIOError(f"namespace connect rejected: {raw}")
            if raw.startswith("2"):
                self.ws.send_text("3")
        raise SocketIOError("handshake did not complete")

    # ------------------------------------------------------------------
    def _dispatch(self, event: str, args: list[Any]) -> None:
        for handler in self._any_handler:
            try:
                handler(event, *args)
            except Exception:  # noqa: BLE001 - a bad handler must not kill the loop
                log.exception("any-handler failed for %s", event)
        for handler in self._handlers.get(event, []):
            try:
                handler(*args)
            except Exception:  # noqa: BLE001
                log.exception("handler failed for %s", event)

    def _read_loop(self) -> None:
        assert self.ws is not None
        while not self._closed.is_set():
            try:
                raw = self.ws.recv_text()
            except (WebSocketError, OSError) as exc:
                if not self._closed.is_set():
                    log.warning("socket read error: %s", exc)
                break
            if raw is None:
                break
            self._handle_packet(raw)
        self.connected = False

    def _handle_packet(self, raw: str) -> None:
        if not raw:
            return
        kind = raw[0]
        body = raw[1:]
        if kind == "2":  # engine.io ping
            self.ws.send_text("3")
        elif kind == "3":  # engine.io pong
            pass
        elif kind == "0":  # engine.io open (reconnect greeting)
            pass
        elif kind == "4":  # socket.io packet
            sub = body[0] if body else ""
            data = body[1:]
            if sub == "0":  # CONNECT
                self.connected = True
            elif sub == "2":  # EVENT
                try:
                    parsed = json.loads(data) if data else []
                except json.JSONDecodeError:
                    return
                if not isinstance(parsed, list) or not parsed:
                    return
                event = parsed[0]
                self._dispatch(event, parsed[1:])
            elif sub == "4":  # CONNECT_ERROR
                raise SocketIOError(f"connect_error: {data}")

    # ------------------------------------------------------------------
    def emit(self, event: str, *args: Any) -> None:
        if not self.ws or self.closed:
            raise SocketIOError("socket is not connected")
        payload = [event] + list(args)
        self.ws.send_text("42" + json.dumps(payload, separators=(",", ":")))

    def start(self) -> None:
        """Connect and pump messages on a background thread."""
        self.connect()
        thread = threading.Thread(target=self._read_loop, name="sio-reader", daemon=True)
        thread.start()

    def close(self) -> None:
        self._closed.set()
        self.connected = False
        if self.ws:
            self.ws.close()
        self.ws = None

    @property
    def closed(self) -> bool:
        return self._closed.is_set() or self.ws is None
