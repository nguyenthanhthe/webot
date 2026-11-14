"""Tests of the StreamHub event fan-out hub."""

from __future__ import annotations

import threading
import unittest
from unittest import mock

import wvbot.stream as stream_mod
from wvbot.stream import EventHub, StreamHub


class StreamHubTest(unittest.TestCase):
    def test_subscribe_publish_and_unsubscribe(self) -> None:
        hub = StreamHub(depth=4)
        self.assertIs(EventHub, StreamHub)
        c1 = hub.subscribe()
        c2 = hub.subscribe()
        self.assertEqual(hub.viewer_count(), 2)
        self.assertEqual(hub.subscriber_count(), 2)

        hub.publish("state", {"ok": True})
        self.assertEqual(c1.get_nowait(), ("state", '{"ok": true}'))
        self.assertEqual(c2.get_nowait(), ("state", '{"ok": true}'))

        hub.unsubscribe(c1)
        hub.unsubscribe(c1)  # idempotent
        self.assertEqual(hub.viewer_count(), 1)

    def test_slow_client_drops_oldest_frame_without_blocking(self) -> None:
        hub = StreamHub(depth=2)
        client = hub.subscribe()
        hub.publish("n", 1)
        hub.publish("n", 2)
        hub.publish("n", 3)

        frames = [client.get_nowait(), client.get_nowait()]
        self.assertEqual(frames, [("n", "2"), ("n", "3")])

    def test_publish_with_no_clients_skips_serialization(self) -> None:
        hub = StreamHub()
        with mock.patch.object(stream_mod.json, "dumps") as dumps:
            hub.publish("tick", {"x": 1})
        dumps.assert_not_called()

    def test_concurrent_publish_subscribe_and_unsubscribe(self) -> None:
        hub = StreamHub(depth=10)
        errors: list[Exception] = []

        def publisher() -> None:
            try:
                for i in range(50):
                    hub.publish("msg", {"i": i})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def churner() -> None:
            try:
                for _ in range(25):
                    q = hub.subscribe()
                    hub.unsubscribe(q)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=publisher),
            threading.Thread(target=publisher),
            threading.Thread(target=churner),
            threading.Thread(target=churner),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(hub.viewer_count(), 0)


if __name__ == "__main__":
    unittest.main()
