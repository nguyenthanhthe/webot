"""Entry point: ``python -m wvbot``.

The configuration always comes from the state directory
(``~/.wv-bot/config.json``); there is no path on the command line, so no
argument can ever point the bot somewhere else.
"""

from __future__ import annotations

import logging
import os
import signal
import threading

from .bot import Bot
from .config import STATE_DIR, load_config


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

    roster_file = STATE_DIR / "accounts.json"
    if roster_file.is_file() or os.environ.get("WV_BOT_FARM"):
        from .farm import Farm, default_bot_factory, load_roster

        roster = load_roster(STATE_DIR)
        farm = Farm(roster, lambda acc: default_bot_factory(acc, farm_root=STATE_DIR), farm_root=STATE_DIR)
        farm.start_dashboard(cfg.dashboard_host, cfg.dashboard_port)
        farm.start()

        def _shutdown_farm(signum, _frame):
            logging.getLogger("wvbot").info("signal %s received, stopping farm", signum)
            farm.stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _shutdown_farm)
            except (ValueError, OSError):
                pass

        while not farm.stop_event.is_set():
            farm.stop_event.wait(1.0)
        return 0

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
