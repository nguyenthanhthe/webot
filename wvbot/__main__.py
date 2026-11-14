"""Entry point: ``python -m wvbot``.

The configuration always comes from the state directory
(``~/.wv-bot/config.json`` or ``STATE_DIR``); there is no path on the command
line, so no argument can ever point the bot somewhere else.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from . import config as config_mod
from .bot import Bot


def _resolve_state_dir() -> Path:
    getter = getattr(config_mod, "get_state_dir", None)
    return Path(getter()) if callable(getter) else Path(config_mod.STATE_DIR)


def _env_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


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


def _install_signal_handlers(handler) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wvbot", description="Wolvesville bot runner")
    parser.add_argument(
        "--mode",
        choices=("protocol", "farm"),
        default=None,
        help="Execution mode (defaults to farm when accounts.json or WV_BOT_FARM is set)",
    )
    args = parser.parse_args(argv)

    state_dir = _resolve_state_dir()
    cfg = config_mod.load_config(base=state_dir)
    _setup_logging(cfg.log_level)
    threading.excepthook = _thread_excepthook

    roster_file = state_dir / "accounts.json"
    if args.mode == "farm":
        use_farm = True
    elif args.mode == "protocol":
        use_farm = False
    else:
        use_farm = roster_file.is_file() or _env_truthy(os.environ.get("WV_BOT_FARM"))

    if use_farm:
        from .farm import Farm, RosterError, default_bot_factory, load_roster

        try:
            roster = load_roster(state_dir)
        except RosterError as exc:
            print(f"Error loading farm roster: {exc}", file=sys.stderr)
            return 1

        farm = Farm(
            roster,
            lambda acc: default_bot_factory(acc, farm_root=state_dir),
            farm_root=state_dir,
        )

        def _shutdown_farm(signum, _frame):
            logging.getLogger("wvbot").info("signal %s received, stopping farm", signum)
            farm.stop()

        _install_signal_handlers(_shutdown_farm)

        try:
            farm.start_dashboard(
                cfg.dashboard_host,
                cfg.dashboard_port,
                token=getattr(cfg, "dashboard_token", ""),
            )
            farm.start()
            while not farm.stop_event.is_set():
                farm.stop_event.wait(1.0)
        finally:
            farm.stop()
        return 0

    bot = Bot(cfg, account_dir=state_dir)

    def _shutdown(signum, _frame):
        logging.getLogger("wvbot").info("signal %s received, stopping", signum)
        bot.stop_event.set()

    _install_signal_handlers(_shutdown)

    bot.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
