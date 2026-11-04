"""Entry point: ``python -m wvbot``.

The configuration always comes from the state directory
(``~/.wv-bot/config.json``); there is no path on the command line, so no
argument can ever point the bot somewhere else.
"""

from __future__ import annotations

import logging
import signal
import threading

from .bot import Bot
from .config import load_config


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
        datefmt="%H:%M:%S",
    )


def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
    """A dead daemon thread must at least leave a trace.

    Threads that die silently (reader, dashboard, action workers) are
    the hardest failures to post-mortem on a headless phone.
    """
    logging.getLogger("wvbot.thread").error(
        "thread %s died: %s",
        getattr(args.thread, "name", "?"),
        args.exc_type.__name__ if args.exc_type else "?",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


def main() -> int:
    cfg = load_config()
    _setup_logging(cfg.log_level)
    threading.excepthook = _thread_excepthook

    bot = Bot(cfg)

    def _shutdown(signum, _frame):
        logging.getLogger("wvbot").info("signal %s received, stopping", signum)
        bot.stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):
            pass

    bot.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
