"""Tests of the websocket layer: framing, fragmentation, control frames."""

from __future__ import annotations

import struct
import threading
import unittest
from unittest import mock

from wvbot.ws import (
    MAX_MESSAGE_BYTES,
    OPCODE_CLOSE,
    OPCODE_CONT,
    OPCODE_PING,
    OPCODE_TEXT,
    WebSocket,
    WebSocketError,
)


class FakeSock:
    """Replays pre-recorded recv data and records sends."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.sent = []

    def recv(self, count: int) -> bytes:
        if self.pos >= len(self.data):
            return b""
        chunk = self.data[self.pos : self.pos + count]
        self.pos += len(chunk)
        return chunk

    def sendall(self, raw: bytes) -> None:
        self.sent.append(raw)


def frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    first = (0x80 if fin else 0x00) | opcode
    if len(payload) < 126:
        header = struct.pack("!BB", first, len(payload))
    elif len(payload) < (1 << 16):
        header = struct.pack("!BBH", first, 126, len(payload))
    else:
        header = struct.pack("!BBQ", first, 127, len(payload))
    return header + payload


def client(data: bytes) -> WebSocket:
    ws = WebSocket("wss://game.example")
    ws._sock = FakeSock(data)
    return ws


class FrameTest(unittest.TestCase):
    def test_a_whole_text_message(self):
        ws = client(frame(OPCODE_TEXT, b"hello"))
        self.assertEqual(ws.recv_text(), "hello")

    def test_a_fragmented_message_arrives_whole(self):
        data = (
            frame(OPCODE_TEXT, b'42["game-', fin=False)
            + frame(OPCODE_CONT, b'started",', fin=False)
            + frame(OPCODE_CONT, b'{"role":1}]')
        )
        ws = client(data)
        self.assertEqual(ws.recv_text(), '42["game-started",{"role":1}]')

    def test_a_ping_is_ponged_and_skipped(self):
        data = frame(OPCODE_PING, b"1234") + frame(OPCODE_TEXT, b"2")
        ws = client(data)
        self.assertEqual(ws.recv_text(), "2")
        # The pong: FIN | 0xA, and the length byte carries the client
        # mask bit - every client frame is masked.
        self.assertEqual(ws._sock.sent[0][:2], b"\x8a\x84")

    def test_a_close_frame_ends_the_stream(self):
        ws = client(frame(OPCODE_CLOSE, b"\x03\xe8"))
        self.assertIsNone(ws.recv_text())
        self.assertTrue(ws.closed)

    def test_a_long_message_uses_the_16_bit_length(self):
        payload = b"x" * 200
        ws = client(frame(OPCODE_TEXT, payload))
        self.assertEqual(ws.recv_text(), payload.decode())

    def test_a_bogus_length_is_refused(self):
        # Declares 8 GB with the 64-bit form - refused before any read.
        header = struct.pack("!BBQ", 0x81, 127, 1 << 33)
        ws = client(header)
        with self.assertRaises(Exception):
            ws.recv_text()

    def test_a_continuation_without_a_start_is_an_error(self):
        ws = client(frame(OPCODE_CONT, b"orphan"))
        with self.assertRaises(Exception):
            ws.recv_text()

    def test_fragment_limit_is_enforced(self):
        part = b"y" * 100
        data = frame(OPCODE_TEXT, part, fin=False) + frame(
            OPCODE_CONT, b"z" * (MAX_MESSAGE_BYTES), fin=True
        )
        ws = client(data)
        with self.assertRaises(Exception):
            ws.recv_text()

    def test_interleaved_text_frame_mid_fragmentation_raises(self):
        data = (
            frame(OPCODE_TEXT, b"first-", fin=False)
            + frame(OPCODE_TEXT, b"interleaved", fin=True)
        )
        ws = client(data)
        with self.assertRaises(WebSocketError):
            ws.recv_text()


class ConnectAndCloseTest(unittest.TestCase):
    class _HandshakeSock:
        def __init__(self, response: bytes):
            self.response = response
            self.pos = 0
            self.closed = False
            self.timeout = None

        def settimeout(self, t):
            self.timeout = t

        def sendall(self, data: bytes):
            pass

        def recv(self, count: int) -> bytes:
            if self.pos >= len(self.response):
                return b""
            chunk = self.response[self.pos : self.pos + count]
            self.pos += len(chunk)
            return chunk

        def close(self):
            self.closed = True

    def test_connect_rejects_non_101_status_and_closes_socket(self):
        for bad_line in (
            b"HTTP/1.1 200 OK\r\n\r\n",
            b"HTTP/1.1 500 101 Bad\r\n\r\n",
            b"HTTP/1.1 1010 Weird\r\n\r\n",
        ):
            sock = self._HandshakeSock(bad_line)
            ws = WebSocket("ws://game.example/socket.io/")
            with mock.patch("socket.create_connection", return_value=sock):
                with self.assertRaises(WebSocketError):
                    ws.connect()
            self.assertTrue(sock.closed)
            self.assertIsNone(ws._sock)

    def test_connect_accepts_101_and_keeps_trailing_bytes(self):
        sock = self._HandshakeSock(b"HTTP/1.1 101 Switching Protocols\r\n\r\nEXTRA")
        ws = WebSocket("ws://game.example/socket.io/")
        with mock.patch("socket.create_connection", return_value=sock):
            ws.connect()
        self.assertIs(ws._sock, sock)
        self.assertEqual(ws._buffer, b"EXTRA")

    def test_send_after_close_raises_and_close_is_idempotent(self):
        sock = self._HandshakeSock(b"")
        ws = WebSocket("ws://game.example/socket.io/")
        ws._sock = sock
        ws.close()
        ws.close()
        self.assertTrue(sock.closed)
        self.assertIsNone(ws._sock)
        with self.assertRaises(WebSocketError):
            ws.send_text("hello")

    def test_concurrent_send_frames_do_not_interleave_or_error(self):
        sent_chunks: list[bytes] = []

        class _SyncSock:
            def sendall(self, raw: bytes):
                sent_chunks.append(raw)

            def close(self):
                pass

        ws = WebSocket("ws://game.example")
        ws._sock = _SyncSock()
        errors: list[Exception] = []

        def sender(idx: int) -> None:
            try:
                for j in range(10):
                    ws.send_text(f"{idx}-{j}")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=sender, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(sent_chunks), 40)


if __name__ == "__main__":
    unittest.main()
