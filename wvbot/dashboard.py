"""Status dashboard served by the bot.

The bot runs on the phone; the dashboard is read from the laptop over the
local network (or through ``adb forward``). It exposes:

* ``GET /``           - HTML status page (auto refreshing)
* ``GET /api/status`` - JSON snapshot
* ``GET /api/events`` - JSON list of recent events
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

log = logging.getLogger("wvbot.dashboard")


class StatusStore:
    """Thread-safe bag of counters the bot writes and the dashboard reads."""

    def __init__(self, max_events: int = 300):
        self._lock = threading.Lock()
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._data: dict[str, Any] = {
            "status": "starting",
            "phase": "lobby",
            "room": None,
            "game_id": None,
            "role": None,
            "players": 0,
            "xp_total": 0,
            "xp_last": 0,
            "games_played": 0,
            "wins": 0,
            "reconnects": 0,
            "errors": 0,
            "last_error": None,
            # From the account profile.
            "username": None,
            "level": None,
            "account_xp": None,
            "games_finished": None,
            # The XP that is still missing for the next level.
            "xp_until_next": None,
            # The XP rate and the time to the next level.
            "projection": {},
            # The XP of today and of all time, kept across restarts.
            "history": {},
            # The level of every player in the room.
            "levels": [],
            "started_at": time.time(),
            "updated_at": time.time(),
        }

    def update(self, **fields: Any) -> None:
        with self._lock:
            self._data.update(fields)
            self._data["updated_at"] = time.time()

    def get(self) -> dict[str, Any]:
        with self._lock:
            data = dict(self._data)
        data["uptime_sec"] = int(time.time() - data.get("started_at", time.time()))
        return data

    def add_event(self, kind: str, message: str, **extra: Any) -> None:
        entry = {"ts": time.time(), "kind": kind, "message": message, **extra}
        with self._lock:
            self._events.append(entry)

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)[-limit:]


PAGE = """<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="3">
<title>Wolvesville Bot - status</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
         margin: 0; padding: 24px; background: #14161a; color: #e6e6e6; }
  h1 { font-size: 18px; margin: 0 0 16px; color: #7dd3fc; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
          gap: 12px; margin-bottom: 24px; }
  .card { background: #1e2127; border: 1px solid #2b3038; border-radius: 8px; padding: 12px 14px; }
  .card .k { font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: #8b949e; }
  .card .v { font-size: 20px; margin-top: 6px; }
  .ok { color: #4ade80; } .warn { color: #fbbf24; } .bad { color: #f87171; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  td { padding: 4px 8px; border-bottom: 1px solid #262b33; vertical-align: top; }
  td.t { color: #8b949e; width: 80px; }
  button { background: #1e2127; color: #e6e6e6; border: 1px solid #2b3038;
           border-radius: 6px; padding: 6px 12px; margin-right: 8px;
           font-family: inherit; cursor: pointer; }
  button:hover { border-color: #7dd3fc; color: #7dd3fc; }
  .levels { margin-top: 8px; }
  .levels td.me { color: #4ade80; font-weight: bold; }
  .levels td.dead { color: #6e7681; text-decoration: line-through; }
</style>
</head>
<body>
<h1>Wolvesville bot - v0.0.2</h1>
<div class="grid" id="cards"></div>
<h1 style="color:#c9d1d9">Controls</h1>
<p><button onclick="ctl('pause')">Pause</button>
   <button onclick="ctl('resume')">Resume</button>
   <button onclick="ctl('restart')">Restart game</button>
   <button onclick="reward('daily')">Claim daily reward</button>
   <button onclick="reward('wheel')">Spin the golden wheel</button></p>
<h1 style="color:#c9d1d9">Players in the room</h1>
<table class="levels" id="levels"></table>
<h1 style="color:#c9d1d9">Recent events</h1>
<table id="events"></table>
<script>
const fmt = s => new Date(s * 1000).toLocaleTimeString();
async function ctl(action) {
  await fetch('/api/control', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({kind: 'control', action}),
  });
  refresh();
}
async function reward(action) {
  const res = await fetch('/api/control', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({kind: 'reward', action}),
  });
  const body = await res.json();
  if (!body.ok) alert('the request was refused - look at the log');
  refresh();
}
async function refresh() {
  const st = await (await fetch('/api/status')).json();
  const rate = st.uptime_sec > 60 ? Math.round(st.xp_total / (st.uptime_sec / 3600)) : 0;
  const proj = st.projection || {};
  const hist = st.history || {};
  const hours = proj.hours_to_next_level;
  const eta = hours === null || hours === undefined ? '-'
    : (hours < 48 ? hours + ' h' : (hours / 24).toFixed(1) + ' d');
  const cards = [
    ['status', st.status, st.status === 'running' || st.status === 'playing' ? 'ok' : 'warn'],
    ['account', st.username || '-', ''],
    ['level', st.level === null || st.level === undefined ? '-' : st.level, 'ok'],
    ['account xp', st.account_xp === null || st.account_xp === undefined ? '-' : st.account_xp, 'ok'],
    ['xp today', hist.today_xp || 0, 'ok'],
    ['xp all time', hist.total_xp || 0, 'ok'],
    ['games all time', hist.total_games || 0, ''],
    ['xp to next level', st.xp_until_next === null || st.xp_until_next === undefined
      ? '-' : st.xp_until_next, ''],
    ['next level in', eta, ''],
    ['phase', st.phase || '-', ''],
    ['room', st.room || '-', ''],
    ['role', st.role || '-', ''],
    ['players', st.players, ''],
    ['xp farmed', st.xp_total, 'ok'],
    ['xp / hour', rate, ''],
    ['xp last game', st.xp_last, ''],
    ['games', st.games_played, ''],
    ['wins', st.wins, 'ok'],
    ['reconnects', st.reconnects, st.reconnects ? 'warn' : ''],
    ['errors', st.errors, st.errors ? 'bad' : ''],
    ['uptime', Math.floor(st.uptime_sec / 60) + ' min', ''],
  ];
  document.getElementById('cards').innerHTML = cards.map(([k, v, c]) =>
    `<div class="card"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`).join('');

  const rows = st.levels || [];
  document.getElementById('levels').innerHTML = rows.length
    ? rows.map(p => {
        const cls = p.own ? 'me' : (p.alive ? '' : 'dead');
        const mark = p.own ? ' (you)' : '';
        return `<tr><td class="${cls}">${p.name || '?'}${mark}</td>` +
               `<td class="${cls}">lvl ${p.level}</td></tr>`;
      }).join('')
    : '<tr><td>no player data yet</td></tr>';

  const ev = await (await fetch('/api/events?limit=60')).json();
  document.getElementById('events').innerHTML = ev.map(e =>
    `<tr><td class="t">${fmt(e.ts)}</td><td>${e.kind}</td><td>${e.message}</td></tr>`).join('');
}
refresh();
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    store: StatusStore = None  # type: ignore[assignment]
    # Called with the requested action; returns True when it was handled.
    control: Any = None  # type: ignore[assignment]
    # Called with the requested reward action.
    reward: Any = None  # type: ignore[assignment]

    def log_message(self, fmt: str, *args: Any) -> None:  # silence access log
        log.debug("dashboard %s", fmt % args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # The game page may report its state directly to us.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        self._send(204, b"", "text/plain")

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        """The injected bot reports its state here (no websocket needed)."""
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, b'{"error":"bad json"}', "application/json")
            return
        if not isinstance(payload, dict):
            self._send(400, b'{"error":"bad payload"}', "application/json")
            return

        kind = payload.get("kind")
        if kind == "state":
            fields = payload.get("state")
            if isinstance(fields, dict):
                self.store.update(**fields)
        elif kind == "event":
            message = str(payload.get("message") or "")
            if message:
                self.store.add_event(
                    str(payload.get("level") or "info"), message
                )
        elif kind == "control":
            action = str(payload.get("action") or "")
            handled = False
            if self.control is not None:
                handled = bool(self.control(action))
            body = json.dumps({"ok": handled}).encode()
            self._send(200, body, "application/json")
            return
        elif kind == "reward":
            # A reward that has to be picked up right now: the daily
            # reward or a spin of the golden wheel.
            action = str(payload.get("action") or "")
            handled = False
            if self.reward is not None:
                handled = bool(self.reward(action))
            body = json.dumps({"ok": handled}).encode()
            self._send(200, body, "application/json")
            return
        self._send(200, b'{"ok":true}', "application/json")

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path.startswith("/api/status"):
            body = json.dumps(self.store.get(), indent=2).encode()
            self._send(200, body, "application/json")
        elif self.path.startswith("/api/events"):
            limit = 100
            if "?" in self.path:
                query = self.path.split("?", 1)[1]
                for part in query.split("&"):
                    if part.startswith("limit="):
                        try:
                            limit = max(1, min(500, int(part[6:])))
                        except ValueError:
                            pass
            body = json.dumps(self.store.events(limit), indent=2).encode()
            self._send(200, body, "application/json")
        elif self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        else:
            self._send(404, b'{"error":"not found"}', "application/json")


def start_dashboard(
    store: StatusStore,
    host: str = "0.0.0.0",
    port: int = 8787,
    control: Any = None,
    reward: Any = None,
):
    handler = type(
        "_BoundHandler",
        (_Handler,),
        {"store": store, "control": control, "reward": reward},
    )
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="dashboard", daemon=True)
    thread.start()
    log.info("dashboard listening on http://%s:%s", host, port)
    return server
