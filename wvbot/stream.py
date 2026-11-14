"""Server-Sent Events for the dashboard, on the stdlib only.

The farm publishes; every connected browser receives a push.  SSE needs
nothing but a chunked response, so the dashboard stays dependency-free
and works over ``adb forward`` with no internet.

A slow client must never stall the farm: each subscription queue is
bounded and drops the oldest frames when the reader falls behind.
"""

from __future__ import annotations

import json
import queue
import threading
from typing import Any

# Frames a client may fall behind by before the oldest are dropped.
QUEUE_DEPTH = 200


class StreamHub:
    """Fans every published frame out to the connected viewers."""

    def __init__(self, depth: int = QUEUE_DEPTH):
        self._depth = depth
        self._clients: set[queue.Queue[tuple[str, str]]] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue[tuple[str, str]]:
        client: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=self._depth)
        with self._lock:
            self._clients.add(client)
        return client

    def unsubscribe(self, client: queue.Queue[tuple[str, str]]) -> None:
        with self._lock:
            self._clients.discard(client)

    def publish(self, kind: str, data: Any) -> None:
        """Send one frame to everyone; a dead or slow client is skipped."""
        with self._lock:
            if not self._clients:
                return
            clients = list(self._clients)
        frame = (kind, json.dumps(data, ensure_ascii=False))
        for client in clients:
            try:
                client.put_nowait(frame)
            except queue.Full:
                # The reader is gone or too slow: drop the oldest and
                # try once more, then give the client up.
                try:
                    client.get_nowait()
                    client.put_nowait(frame)
                except (queue.Empty, queue.Full):
                    pass

    def viewer_count(self) -> int:
        with self._lock:
            return len(self._clients)

    subscriber_count = viewer_count


EventHub = StreamHub
