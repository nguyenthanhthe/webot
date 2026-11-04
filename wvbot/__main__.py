"""Entry point: ``python -m wvbot``.

The configuration always comes from the state directory
(``~/.wv-bot/config.json``); there is no path on the command line, so no
argument can ever point the bot somewhere else.
"""

from __future__ import annotations

import logging
import signal

from .bot import Bot
from .config import load_config


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> int:
    cfg = load_config()
    _setup_logging(cfg.log_level)

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
