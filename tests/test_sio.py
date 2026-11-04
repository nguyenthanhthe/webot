"""Tests of the socket.io layer, with a fake websocket - no network."""

from __future__ import annotations

import unittest

from wvbot.sio import SocketIOClient, SocketIOError


class FakeSocket:
    """Replays packets, then the peer closes."""

    def __init__(self, packets):
        self.packets = list(packets)
        self.sent = []

    def recv_text(self):
        if self.packets:
            return self.packets.pop(0)
        return None

    def send_text(self, raw):
        self.sent.append(raw)

    def close(self):
        self.packets.clear()


def client_with(packets) -> SocketIOClient:
    client = SocketIOClient("https://game.example")
    client.ws = FakeSocket(packets)
    return client


class HandlePacketTest(unittest.TestCase):
    def test_event_packet_reaches_the_handlers(self):
        client = client_with([])
        seen = []
        client.on_any(lambda event, *a: seen.append(("any", event)))
        client.on("game-started", lambda *a: seen.append(("direct", len(a))))
        client._handle_packet('42["game-started","{}"]')
        self.assertIn(("any", "game-started"), seen)
        self.assertIn(("direct", 1), seen)

    def test_ping_is_answered(self):
        client = client_with([])
        client._handle_packet("2")
        self.assertEqual(client.ws.sent, ["3"])

    def test_pong_is_ignored(self):
        client = client_with([])
        client._handle_packet("3")
        self.assertEqual(client.ws.sent, [])

    def test_connect_packet_marks_connected(self):
        client = client_with([])
        client._handle_packet("40{}")
        self.assertTrue(client.connected)

    def test_garbage_is_dropped(self):
        client = client_with([])
        client._handle_packet("42not json")
        client._handle_packet("42[]")
        self.assertEqual(client.ws.sent, [])

    def test_emit_requires_a_socket(self):
        client = SocketIOClient("https://game.example")
        with self.assertRaises(SocketIOError):
            client.emit("mg:join-open-game", {"miniGameId": 1})


class DeathPathTest(unittest.TestCase):
    def test_a_dead_socket_dispatches_disconnect(self):
        client = client_with([])
        fired = []
        client.on("disconnect", lambda *a: fired.append(1))
        client._read_loop()
        self.assertEqual(fired, [1])
        self.assertFalse(client.closed)

    def test_a_bad_packet_ends_the_session(self):
        client = client_with(["42broken{"])
        fired = []
        client.on("disconnect", lambda *a: fired.append(1))
        client._read_loop()
        self.assertEqual(fired, [1])

    def test_an_intentional_close_does_not_dispatch(self):
        client = client_with([])
        fired = []
        client.on("disconnect", lambda *a: fired.append(1))
        client.close()
        client._read_loop()
        self.assertEqual(fired, [])

    def test_engine_open_is_ignored(self):
        client = client_with([])
        client._handle_packet("0{}")
        self.assertFalse(client.connected)


if __name__ == "__main__":
    unittest.main()
