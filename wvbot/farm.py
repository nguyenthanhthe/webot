"""The farm: several accounts, one service, one supervisor.

Phase 1 of the enterprise plan (docs/ENTERPRISE-PLAN.md).  The Farm
owns a roster of accounts and runs one bot thread per account; a bot
that dies is restarted with exponential backoff, and one that keeps
dying is parked as "failed" instead of burning joins.

The roster lives in ``accounts.json`` in the farm root:

    {"accounts": [{"name": "main", "refresh_token": "..."}]}

An absent roster falls back to the classic single-account setup, so
the existing deployment keeps working untouched.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .dashboard import StatusStore
from .stream import StreamHub

log = logging.getLogger("wvbot.farm")

ROSTER_FILE = "accounts.json"
DEFAULT_MAX_BOTS = 4
# A bot that died this often in a row is parked instead of restarted.
MAX_CONSECUTIVE_CRASHES = 3
CRASH_BACKOFF_BASE_SEC = 0.5
CRASH_BACKOFF_MAX_SEC = 60.0
HEALTHY_UPTIME_RESET_SEC = 300.0

_ROSTER_LOCK = threading.RLock()
_SECRET_FIELDS = frozenset({"email", "password", "refresh_token", "cf_jwt"})


def _resolve_state_dir() -> Path:
    from . import config

    getter = getattr(config, "get_state_dir", None)
    return Path(getter()) if callable(getter) else Path(config.STATE_DIR)


def _atomic_write_text(path: Path, content: str, mode: int | None = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        if mode is not None:
            try:
                os.chmod(tmp_path, mode)
            except OSError:
                pass
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp_path, path)
        if mode is not None:
            try:
                os.chmod(path, mode)
            except OSError:
                pass
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def clean_name(value: str) -> str:
    """A roster name is a directory name and a URL path segment."""
    kept = "".join(c if (c.isalnum() or c in "-_") else "" for c in str(value))
    return kept[:24]


class RosterError(ValueError):
    pass


def _is_unsafe_raw_name(value: Any) -> bool:
    if not isinstance(value, str):
        return True
    return any(tok in value for tok in ("/", "\\", os.sep, "..", "\x00"))


def _validate_account_name(value: Any) -> str:
    if _is_unsafe_raw_name(value):
        raise RosterError(f"unsafe account name: {value!r}")
    cleaned = clean_name(value)
    if not cleaned or cleaned in (".", ".."):
        raise RosterError("invalid account name")
    return cleaned


def _apply_config_overrides(cfg: Any, mapping: dict[str, Any], config_mod: Any) -> None:
    helper = getattr(config_mod, "apply_overrides", None)
    allowed = getattr(config_mod, "CONFIG_FIELDS", None)
    if allowed is None:
        allowed = frozenset(f.name for f in dataclasses.fields(config_mod.Config))
    filtered = {k: v for k, v in mapping.items() if k in allowed}
    if callable(helper):
        helper(cfg, filtered)
        return
    for k, v in filtered.items():
        setattr(cfg, k, v)


def default_bot_factory(account: dict[str, Any], farm_root: Path | None = None) -> Any:
    """Build a production Bot instance for a roster entry."""
    from . import config as config_mod
    from .bot import Bot

    raw_name = account.get("name")
    if "name" not in account or raw_name is None:
        raw_name = "main"
    name = _validate_account_name(raw_name)
    root = farm_root if farm_root is not None else _resolve_state_dir()
    acc_dir = root / "accounts" / name if name != "main" or (root / "accounts" / name).is_dir() else root
    if not acc_dir.resolve().is_relative_to(root.resolve()):
        raise RosterError(f"account directory escapes farm root: {name!r}")
    acc_dir.mkdir(parents=True, exist_ok=True)

    cfg = config_mod.load_config(base=acc_dir)

    # Inherit root config.json defaults (non-secret fields) if not set in account config or env
    if acc_dir != root:
        root_cfg_path = root / "config.json"
        acc_cfg_path = acc_dir / "config.json"
        if root_cfg_path.is_file():
            try:
                root_data = json.loads(root_cfg_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                root_data = {}
            acc_data: dict[str, Any] = {}
            if acc_cfg_path.is_file():
                try:
                    loaded_acc = json.loads(acc_cfg_path.read_text(encoding="utf-8"))
                    if isinstance(loaded_acc, dict):
                        acc_data = loaded_acc
                except (OSError, ValueError):
                    acc_data = {}
            if isinstance(root_data, dict):
                inherited = {
                    k: v
                    for k, v in root_data.items()
                    if k not in _SECRET_FIELDS
                    and k not in acc_data
                    and f"WV_BOT_{k.upper()}" not in os.environ
                }
                if inherited:
                    _apply_config_overrides(cfg, inherited, config_mod)

    # Never overwrite rotated refresh_token.txt or cf_jwt_file on disk if non-empty
    token_path = acc_dir / config_mod.REFRESH_TOKEN_FILE
    disk_has_refresh = False
    if token_path.is_file():
        try:
            disk_has_refresh = bool(token_path.read_text(encoding="utf-8").strip())
        except OSError:
            pass

    disk_has_cf_jwt = False
    try:
        jwt_path = config_mod.state_path(cfg.cf_jwt_file, base=acc_dir)
        if jwt_path.is_file():
            disk_has_cf_jwt = bool(jwt_path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        pass

    account_overrides: dict[str, Any] = {}
    for k, v in account.items():
        if k == "refresh_token" and (disk_has_refresh or not v):
            continue
        if k == "cf_jwt" and (disk_has_cf_jwt or not v):
            continue
        account_overrides[k] = v

    if not disk_has_refresh and account_overrides.get("refresh_token"):
        token_text = str(account_overrides["refresh_token"]).strip()
        if token_text:
            _atomic_write_text(token_path, token_text, mode=0o600)
            account_overrides["refresh_token"] = token_text

    _apply_config_overrides(cfg, account_overrides, config_mod)
    return Bot(cfg, account_dir=acc_dir, start_web_dashboard=False)


def load_roster(farm_root: Path) -> list[dict[str, Any]]:
    """Read the roster, or fall back to the single classic account.

    Every account carries a clean unique name and whatever credentials
    it needs; unknown fields are kept for the bot factory.
    """
    path = farm_root / ROSTER_FILE
    with _ROSTER_LOCK:
        if not path.is_file():
            return [{"name": "main"}]
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RosterError(f"the roster is unreadable: {exc}") from exc
    accounts = raw.get("accounts") if isinstance(raw, dict) else None
    if not isinstance(accounts, list) or not accounts:
        raise RosterError("the roster holds no accounts")
    seen: set[str] = set()
    roster: list[dict[str, Any]] = []
    for entry in accounts:
        if not isinstance(entry, dict):
            raise RosterError("an account entry is not an object")
        name = _validate_account_name(entry.get("name") or "")
        if name in seen:
            raise RosterError(f"duplicate account name: {name}")
        seen.add(name)
        account = dict(entry)
        account["name"] = name
        roster.append(account)
    return roster


def save_account_to_roster(
    farm_root: Path,
    name: str,
    refresh_token: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    safe_name = _validate_account_name(name)
    path = farm_root / ROSTER_FILE

    with _ROSTER_LOCK:
        data: dict[str, Any] = {"accounts": []}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise RosterError(f"cannot update unreadable roster: {exc}") from exc
            if not isinstance(loaded, dict) or not isinstance(loaded.get("accounts"), list):
                raise RosterError("roster file must be a JSON object with an 'accounts' list")
            data = loaded

        accounts: list[Any] = data["accounts"]
        account_entry = None
        for acc in accounts:
            if isinstance(acc, dict) and acc.get("name") == safe_name:
                account_entry = acc
                break
        if account_entry is None:
            account_entry = {"name": safe_name}
            accounts.append(account_entry)

        if overrides:
            for k, v in overrides.items():
                if k not in ("name", "refresh_token"):
                    account_entry[k] = v

        _atomic_write_text(path, json.dumps(data, indent=2), mode=0o600)

        from .config import REFRESH_TOKEN_FILE

        acc_dir = farm_root / "accounts" / safe_name
        acc_dir.mkdir(parents=True, exist_ok=True)
        if refresh_token and str(refresh_token).strip():
            token_path = acc_dir / REFRESH_TOKEN_FILE
            _atomic_write_text(token_path, str(refresh_token).strip() + "\n", mode=0o600)

    return account_entry


def remove_account_from_roster(farm_root: Path, name: str) -> bool:
    if _is_unsafe_raw_name(name):
        return False
    safe_name = clean_name(name)
    if not safe_name:
        return False
    path = farm_root / ROSTER_FILE

    with _ROSTER_LOCK:
        if not path.is_file():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RosterError(f"cannot read roster for removal: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
            raise RosterError("roster file must be a JSON object with an 'accounts' list")

        accounts = data["accounts"]
        before = len(accounts)
        remaining = [acc for acc in accounts if not (isinstance(acc, dict) and acc.get("name") == safe_name)]
        if len(remaining) == before:
            return False

        if not remaining:
            try:
                path.unlink()
            except OSError:
                pass
            return True

        data["accounts"] = remaining
        _atomic_write_text(path, json.dumps(data, indent=2), mode=0o600)
        return True


class _Slot:
    """One supervised bot: its thread, its state, its crash count."""

    def __init__(self, account: dict[str, Any]):
        self.account = dict(account)
        self.name = str(account.get("name") or "?")
        self.bot: Any = None
        self.thread: threading.Thread | None = None
        self.status = "idle"
        self.crashes = 0
        self.started_at = 0.0
        self.next_retry_at = 0.0
        self.removed = False


class Farm:
    """Runs one bot thread per account and keeps them alive.

    ``bot_factory(account) -> bot`` builds the real bot (the tests pass
    a fake).  A bot must offer ``run()``, ``stop_event`` and ``store``;
    the farm never touches anything else, so the contract stays small.
    """

    def __init__(
        self,
        roster: list[dict[str, Any]],
        bot_factory: Callable[[dict[str, Any]], Any],
        max_bots: int = DEFAULT_MAX_BOTS,
        farm_root: Path | None = None,
    ):
        self.bot_factory = bot_factory
        self.max_bots = max(1, int(max_bots))
        self.farm_root = farm_root
        self.hub = StreamHub()
        self.store = StatusStore()
        self.stop_event = threading.Event()
        self._lock = threading.RLock()
        self._slots: list[_Slot] = []
        self.crash_backoff_base = CRASH_BACKOFF_BASE_SEC
        self.crash_backoff_max = CRASH_BACKOFF_MAX_SEC
        self.healthy_uptime_reset_sec = HEALTHY_UPTIME_RESET_SEC
        self._dashboard_server: Any = None
        for account in roster[: self.max_bots]:
            self._slots.append(_Slot(account))
        self._supervisor: threading.Thread | None = None
        self._last_publish = 0.0
        self._last_heartbeat = 0.0

    # ------------------------------------------------------------------
    def _room_probe(self, language: str = "en", host: str = "") -> Any:
        with self._lock:
            bots = [s.bot for s in self._slots if not s.removed and s.bot is not None]
        for bot in bots:
            probe = getattr(bot, "_room_probe", None)
            if callable(probe):
                return probe(language, host)
        raise RuntimeError("no active bot available for room probe")

    def collect_events(self, limit: int = 100) -> list[dict[str, Any]]:
        evs = list(self.store.events(limit))
        with self._lock:
            bots = [s.bot for s in self._slots if not s.removed and s.bot is not None]
        for bot in bots:
            if hasattr(bot, "store"):
                try:
                    evs.extend(bot.store.events(limit))
                except Exception:
                    pass
        evs.sort(key=lambda x: x.get("ts", 0))
        return evs[-limit:]

    def control(self, action: str, bot_name: str | None = None) -> bool:
        """Handle a control action (pause/resume/restart) across bots."""
        handled = False
        with self._lock:
            bots = [
                s.bot
                for s in self._slots
                if not s.removed and (bot_name is None or s.name == bot_name) and s.bot is not None
            ]
        for bot in bots:
            if hasattr(bot, "_control") and bot._control(action):
                handled = True
        return handled

    def reward(self, action: str, bot_name: str | None = None) -> bool:
        """Handle a reward action (daily/wheel/lootbox) across bots."""
        handled = False
        with self._lock:
            bots = [
                s.bot
                for s in self._slots
                if not s.removed and (bot_name is None or s.name == bot_name) and s.bot is not None
            ]
        for bot in bots:
            if hasattr(bot, "_reward") and bot._reward(action):
                handled = True
        return handled

    def add_account(self, account: dict[str, Any]) -> bool:
        """Add a new account to the running farm."""
        try:
            name = _validate_account_name(account.get("name") or "")
        except RosterError:
            return False

        clean_acc = dict(account)
        clean_acc["name"] = name

        with self._lock:
            if any(s.name == name for s in self._slots):
                return False
            if len(self._slots) >= self.max_bots:
                return False
            slot = _Slot(clean_acc)
            should_launch = (
                self._supervisor is not None
                and self._supervisor.is_alive()
                and not self.stop_event.is_set()
            )
            if should_launch:
                slot.status = "launching"
            self._slots.append(slot)

        if should_launch:
            self._launch(slot)
        return True

    def remove_account(self, name: str) -> bool:
        """Stop and remove an account from the running farm."""
        if _is_unsafe_raw_name(name):
            return False
        safe_name = clean_name(name)
        if not safe_name:
            return False
        bot_to_stop = None
        with self._lock:
            for i, slot in enumerate(self._slots):
                if slot.name == safe_name:
                    slot.removed = True
                    bot_to_stop = slot.bot
                    self._slots.pop(i)
                    break
            else:
                return False
        if bot_to_stop is not None and hasattr(bot_to_stop, "stop_event"):
            bot_to_stop.stop_event.set()
        return True

    def start_dashboard(
        self,
        host: str = "127.0.0.1",
        port: int = 8787,
        token: str | None = None,
    ) -> Any:
        from .dashboard import start_dashboard

        server = start_dashboard(
            self.store,
            host=host,
            port=port,
            control=self.control,
            reward=self.reward,
            rooms=self._room_probe,
            farm=self,
            hub=self.hub,
            token=token,
            state_dir=self.farm_root,
        )
        with self._lock:
            self._dashboard_server = server
        return server

    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """What the dashboard shows: every bot at a glance.

        The bot's own status (playing, hunting, banned) is the signal;
        the farm's lifecycle status (crashed, failed) wins when a bot
        is no longer running at all.
        """
        with self._lock:
            slot_items = [(s.name, s.status, s.crashes, s.bot) for s in self._slots if not s.removed]

        bots = []
        for name, status, crashes, bot in slot_items:
            data: dict[str, Any] = {"name": name, "status": status, "crashes": crashes}
            if bot is not None:
                try:
                    data.update(bot.store.get())
                except Exception:  # noqa: BLE001 - a dead store must not hide the bot
                    pass
            if status.startswith("crashed") or status in ("failed", "stopped"):
                data["status"] = status
            bots.append(data)
        return {
            "version": __version__,
            "status": "stopping" if self.stop_event.is_set() else "farm running",
            "bots": bots,
            "count": len(bots),
            "playing": sum(1 for b in bots if b.get("status") == "playing"),
            "total_xp": sum(int(b.get("xp_total") or 0) for b in bots),
            "games": sum(int(b.get("games_played") or 0) for b in bots),
            "errors": sum(int(b.get("errors") or 0) for b in bots),
        }

    def stop(self) -> None:
        self.stop_event.set()
        with self._lock:
            slots = list(self._slots)
            supervisor = self._supervisor
            server = self._dashboard_server
            self._dashboard_server = None
            for slot in slots:
                if slot.bot is not None and hasattr(slot.bot, "stop_event"):
                    slot.bot.stop_event.set()

        cur = threading.current_thread()
        if supervisor is not None and supervisor is not cur and supervisor.is_alive():
            supervisor.join(timeout=3.0)

        deadline = time.monotonic() + 5.0
        for slot in slots:
            thread = slot.thread
            if thread is not None and thread is not cur and thread.is_alive():
                remaining = max(0.05, deadline - time.monotonic())
                thread.join(timeout=remaining)

        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _touch_root_heartbeat(self, force: bool = False) -> None:
        if self.farm_root is None:
            return
        now = time.time()
        if not force and (now - self._last_heartbeat) < 60.0:
            return
        self._last_heartbeat = now
        try:
            _atomic_write_text(Path(self.farm_root) / "heartbeat.txt", f"{now}\n", mode=None)
        except OSError:
            pass

    def start(self) -> None:
        self._touch_root_heartbeat(force=True)
        with self._lock:
            slots = list(self._slots)
        for slot in slots:
            self._launch(slot)
        with self._lock:
            self._supervisor = threading.Thread(
                target=self._supervise, name="farm-supervisor", daemon=True
            )
            self._supervisor.start()

    def _launch(self, slot: _Slot) -> None:
        with self._lock:
            if slot.removed or self.stop_event.is_set():
                return
            account_copy = dict(slot.account)

        try:
            bot = self.bot_factory(account_copy)
        except Exception as exc:  # noqa: BLE001 - a bad account is a finding
            with self._lock:
                if not slot.removed:
                    slot.status = "failed"
                    slot.bot = None
            log.warning("bot %s could not be built: %s", slot.name, exc)
            self.store.add_event("error", f"bot {slot.name} failed to build: {exc}")
            return

        if hasattr(bot, "hub"):
            bot.hub = self.hub
        bot.stop_event.clear()

        with self._lock:
            if slot.removed or self.stop_event.is_set():
                bot.stop_event.set()
                return
            slot.bot = bot
            slot.status = "starting"
            slot.started_at = time.monotonic()
            slot.next_retry_at = 0.0
            slot.thread = threading.Thread(
                target=self._run_bot,
                args=(slot, bot),
                daemon=True,
                name=f"bot-{slot.name}",
            )
            slot.thread.start()

    def _run_bot(self, slot: _Slot, bot: Any) -> None:
        crashed = False
        err_msg = ""
        try:
            bot.run()
        except Exception as exc:  # noqa: BLE001 - the farm survives anything
            crashed = True
            err_msg = str(exc)
            log.exception("bot %s crashed: %s", slot.name, exc)
        finally:
            with self._lock:
                intentional = (
                    self.stop_event.is_set()
                    or slot.removed
                    or bool(getattr(bot, "stop_event", None) and bot.stop_event.is_set())
                )
                if not intentional:
                    if (
                        slot.started_at > 0
                        and (time.monotonic() - slot.started_at) >= self.healthy_uptime_reset_sec
                    ):
                        slot.crashes = 0
                    slot.crashes += 1
                    delay = min(
                        self.crash_backoff_max,
                        self.crash_backoff_base * (2 ** max(0, slot.crashes - 1)),
                    )
                    slot.next_retry_at = time.monotonic() + delay
                    if crashed:
                        slot.status = f"crashed: {err_msg}"
                        self.store.add_event("error", f"bot {slot.name} crashed: {err_msg}")
                    else:
                        slot.status = "stopped"
                        self.store.add_event("warn", f"bot {slot.name} exited unexpectedly")
                else:
                    slot.status = "stopped"
                if slot.bot is bot:
                    slot.bot = None

    def _supervise(self) -> None:
        """Restart dead bots with exponential backoff; park repeated failures."""
        while not self.stop_event.is_set():
            to_launch: list[_Slot] = []
            now_mono = time.monotonic()
            with self._lock:
                for slot in list(self._slots):
                    if slot.removed or slot.status in ("failed", "launching"):
                        continue
                    if slot.thread is not None and slot.thread.is_alive():
                        if (
                            slot.crashes > 0
                            and slot.started_at > 0
                            and (now_mono - slot.started_at) >= self.healthy_uptime_reset_sec
                        ):
                            slot.crashes = 0
                            slot.next_retry_at = 0.0
                        bot = slot.bot
                        if bot is not None:
                            bot_status = None
                            if hasattr(bot, "store"):
                                try:
                                    bot_status = bot.store.get().get("status")
                                except Exception:
                                    bot_status = None
                            if bot_status and bot_status != "starting":
                                slot.status = str(bot_status)
                            elif slot.status == "starting":
                                slot.status = "running"
                        continue
                    if slot.crashes >= MAX_CONSECUTIVE_CRASHES:
                        log.warning("bot %s is parked after %d crashes", slot.name, slot.crashes)
                        slot.status = "failed"
                        self.store.add_event(
                            "error", f"bot {slot.name} parked after {slot.crashes} crashes"
                        )
                        continue
                    if now_mono < slot.next_retry_at:
                        continue
                    slot.status = "launching"
                    to_launch.append(slot)

            for slot in to_launch:
                if self.stop_event.is_set():
                    break
                log.warning("bot %s is gone - restarting it (crashes: %d)", slot.name, slot.crashes)
                self._launch(slot)

            now = time.time()
            if now - self._last_publish >= 3.0:
                self._last_publish = now
                self._touch_root_heartbeat()
                try:
                    self.hub.publish("farm", self.snapshot())
                except Exception:
                    pass

            self.stop_event.wait(0.25)
