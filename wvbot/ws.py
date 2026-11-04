"""Minimal RFC 6455 WebSocket client (stdlib only).

Only what the socket.io layer needs: a client handshake over TLS, masked
text frames, ping/pong and close. Implemented directly so the bot has no
third-party dependency and installs cleanly on Termux (no compiler needed).
"""

from __future__ import annotations

import base64
import os
import socket
import ssl
import struct
from typing import Any
from urllib.parse import urlsplit

OPCODE_CONT = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class WebSocketError(RuntimeError):
    pass


class WebSocket:
    def __init__(self, url: str, timeout: float = 30.0, extra_headers: dict[str, str] | None = None):
        self.url = url
        self.timeout = timeout
        self.extra_headers = dict(extra_headers or {})
        self.user_agent = self.extra_headers.pop("User-Agent", DEFAULT_UA)
        self._sock: socket.socket | None = None
        self._buffer = b""
        self.closed = False

    # ------------------------------------------------------------------
    def connect(self) -> None:
        parts = urlsplit(self.url)
        if parts.scheme not in ("ws", "wss"):
            raise WebSocketError(f"unsupported scheme {parts.scheme!r}")

        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        raw = socket.create_connection((host, port), timeout=self.timeout)
        if parts.scheme == "wss":
            context = ssl.create_default_context()
            raw = context.wrap_socket(raw, server_hostname=host)
        raw.settimeout(self.timeout)
        self._sock = raw

        key = base64.b64encode(os.urandom(16)).decode()
        request = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            f"User-Agent: {self.user_agent}",
        ]
        request.extend(f"{k}: {v}" for k, v in self.extra_headers.items())
        request.extend(["", ""])
        raw.sendall("\r\n".join(request).encode())

        header = self._read_until(b"\r\n\r\n")
        status_line = header.split(b"\r\n", 1)[0].decode("latin1", "replace")
        if "101" not in status_line:
            raise WebSocketError(f"handshake failed: {status_line}")
        # The server may have sent frames right after the handshake; keep
        # whatever was read past the header.
        _, _, rest = header.partition(b"\r\n\r\n")
        self._buffer = rest

    # ------------------------------------------------------------------
    def _read_until(self, marker: bytes) -> bytes:
        assert self._sock is not None
        data = b""
        while marker not in data:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            data += chunk
            if len(data) > 65536:
                raise WebSocketError("handshake response too large")
        return data

    def _read_exactly(self, count: int) -> bytes:
        assert self._sock is not None
        while len(self._buffer) < count:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed")
            self._buffer += chunk
        out, self._buffer = self._buffer[:count], self._buffer[count:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        assert self._sock is not None
        header = bytearray()
        header.append(0x80 | opcode)
        length = len(payload)
        mask_bit = 0x80
        if length < 126:
            header.append(mask_bit | length)
        elif length < (1 << 16):
            header.append(mask_bit | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(mask_bit | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    # ------------------------------------------------------------------
    def send_text(self, text: str) -> None:
        self._send_frame(OPCODE_TEXT, text.encode("utf-8"))

    def recv_text(self) -> str | None:
        """Return the next text message, or None when the peer pinged."""
        while True:
            first, second = self._read_exactly(2)
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exactly(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exactly(8))[0]
            mask = self._read_exactly(4) if masked else b""
            payload = self._read_exactly(length) if length else b""
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == OPCODE_CLOSE:
                self.close()
                return None
            if opcode == OPCODE_PING:
                self._send_frame(OPCODE_PONG, payload)
                continue
            if opcode == OPCODE_PONG:
                continue
            if opcode == OPCODE_TEXT:
                return payload.decode("utf-8", "replace")
            if opcode == OPCODE_CONT:
                return payload.decode("utf-8", "replace")
            # binary frames are not used by the game protocol

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self._send_frame(OPCODE_CLOSE, b"\x03\xe8")
        except Exception:  # noqa: BLE001 - best effort
            pass
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "WebSocket":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
