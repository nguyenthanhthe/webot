"""Tests of the entry point: the daemon-thread death is logged."""

from __future__ import annotations

import logging
import types
import unittest


class ThreadExcepthookTest(unittest.TestCase):
    def test_a_dead_thread_is_logged_not_raised(self) -> None:
        from wvbot import __main__ as entry

        records = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record)
        log = logging.getLogger("wvbot.thread")
        log.addHandler(handler)
        try:
            entry._thread_excepthook(
                types.SimpleNamespace(
                    thread=types.SimpleNamespace(name="sio-reader"),
                    exc_type=RuntimeError,
                    exc_value=RuntimeError("boom"),
                    exc_traceback=None,
                )
            )
        finally:
            log.removeHandler(handler)
        self.assertTrue(any("sio-reader" in r.getMessage() for r in records))

    def test_an_anonymous_thread_is_handled(self) -> None:
        from wvbot import __main__ as entry

        # No thread object at all - the hook must not raise.
        entry._thread_excepthook(
            types.SimpleNamespace(
                thread=None,
                exc_type=OSError,
                exc_value=OSError("x"),
                exc_traceback=None,
            )
        )


if __name__ == "__main__":
    unittest.main()
