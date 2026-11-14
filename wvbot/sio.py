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
import re
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
        safe_url = re.sub(
            r"((?:firebaseToken|token)=)[^&]+",
            r"\1***",
            url,
            flags=re.IGNORECASE,
        )
        log.debug("connecting to %s", safe_url)
        ws = WebSocket(url, timeout=self.timeout, extra_headers=self.headers)
        with self._lock:
            self.ws = ws
        try:
            ws.connect()

            raw = ws.recv_text()
            if raw is None or not raw.startswith("0"):
                raise SocketIOError(f"unexpected engine.io open packet: {raw!r}")
            try:
                payload = json.loads(raw[1:]) if len(raw) > 1 else {}
            except json.JSONDecodeError as exc:
                raise SocketIOError(f"malformed engine.io open packet: {raw!r}") from exc
            self.sid = payload.get("sid")

            # Namespace connect; the auth object is the payload of packet 40.
            connect_pkt = (
                "40" + json.dumps(self.auth, separators=(",", ":"))
                if self.auth
                else "40"
            )
            with self._lock:
                ws.send_text(connect_pkt)

            deadline_packets = 0
            # A chatty server may interleave pings and even events before
            # the namespace ack; the cap only guards against an endless
            # stream, it is not a small packet count.
            while deadline_packets < 64:
                raw = ws.recv_text()
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
                    with self._lock:
                        ws.send_text("3")
            raise SocketIOError("handshake did not complete")
        except Exception:
            with self._lock:
                if self.ws is ws:
                    self.ws = None
                ws.close()
            raise

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
        # close() from another thread can clear the socket between the
        # start and here; a loop without a socket simply does nothing.
        with self._lock:
            ws = self.ws
        if ws is None:
            return
        died = False
        try:
            while not self._closed.is_set():
                try:
                    raw = ws.recv_text()
                except (WebSocketError, OSError) as exc:
                    if not self._closed.is_set():
                        log.warning("socket read error: %s", exc)
                    died = True
                    break
                if raw is None:
                    died = True
                    break
                try:
                    if self._handle_packet(raw) is False:
                        died = True
                        break
                except Exception:  # noqa: BLE001 - a bad packet must not kill the reader
                    log.exception("packet handling failed")
                    died = True
                    break
        finally:
            self.connected = False
            with self._lock:
                if self.ws is ws:
                    self.ws = None
                try:
                    ws.close()
                except Exception:  # noqa: BLE001
                    pass
        # A socket that died on its own must not leave the session
        # hanging: the caller registered a "disconnect" handler and only
        # the dispatch below makes it fire.
        if died and not self._closed.is_set():
            self._dispatch("disconnect", [])

    def _handle_packet(self, raw: str) -> bool:
        if not raw:
            return True
        kind = raw[0]
        body = raw[1:]
        if kind == "1":  # engine.io close
            return False
        if kind == "2":  # engine.io ping
            with self._lock:
                if self.ws is not None and not self._closed.is_set():
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
            elif sub == "1":  # DISCONNECT
                return False
            elif sub == "2":  # EVENT
                try:
                    parsed = json.loads(data) if data else []
                except json.JSONDecodeError:
                    return True
                if not isinstance(parsed, list) or not parsed:
                    return True
                event = parsed[0]
                self._dispatch(event, parsed[1:])
            elif sub == "4":  # CONNECT_ERROR
                raise SocketIOError(f"connect_error: {data}")
        return True

    # ------------------------------------------------------------------
    def emit(self, event: str, *args: Any) -> None:
        payload = [event] + list(args)
        msg = "42" + json.dumps(payload, separators=(",", ":"))
        with self._lock:
            if not self.ws or self._closed.is_set():
                raise SocketIOError("socket is not connected")
            self.ws.send_text(msg)

    def start(self) -> None:
        """Connect and pump messages on a background thread."""
        self.connect()
        thread = threading.Thread(target=self._read_loop, name="sio-reader", daemon=True)
        thread.start()

    def close(self) -> None:
        self._closed.set()
        self.connected = False
        with self._lock:
            ws = self.ws
            self.ws = None
            if ws is not None:
                ws.close()

    @property
    def closed(self) -> bool:
        return self._closed.is_set() or self.ws is None
