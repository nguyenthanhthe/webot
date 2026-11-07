"""Status dashboard served by the bot / farm.

The bot runs on the phone; the dashboard is read from the laptop over the
local network (or through ``adb forward``). It exposes:

* ``GET /``           - HTML status page (SSE live push + polling fallback)
* ``GET /api/status`` - JSON snapshot of active bot
* ``GET /api/farm``   - JSON snapshot of all farm bots
* ``GET /api/stream`` - Server-Sent Events (SSE) live push
* ``GET /api/events`` - JSON list of recent events
"""

from __future__ import annotations

import inspect
import json
import logging
import queue
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__

log = logging.getLogger("wvbot.dashboard")


class StatusStore:
    """Thread-safe bag of counters the bot writes and the dashboard reads."""

    def __init__(self, max_events: int = 300):
        self._lock = threading.Lock()
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._data: dict[str, Any] = {
            "status": "starting",
            "version": __version__,
            "phase": "lobby",
            "room": None,
            "game_id": None,
            "role": None,
            "players": 0,
            "alive": None,
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
            # The build of the game, to notice a patch.
            "update": {},
            # When a server ban lifts, as a UTC clock string.
            "banned_until": None,
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
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wolvesville Farm Dashboard</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
         margin: 0; padding: 24px; background: #0f1115; color: #e6e6e6; }
  header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
  h1 { font-size: 20px; margin: 0; color: #38bdf8; }
  .badge { font-size: 11px; padding: 3px 8px; border-radius: 999px; background: #1e293b; color: #94a3b8; }
  .badge.live { background: #064e3b; color: #34d399; }
  .badge.warn { background: #78350f; color: #fde047; }
  .summary-bar { display: flex; gap: 12px; margin-bottom: 24px; flex-wrap: wrap; }
  .stat-pill { background: #181b20; border: 1px solid #282e38; border-radius: 8px; padding: 10px 14px; flex: 1; min-width: 140px; }
  .stat-pill .lbl { font-size: 11px; text-transform: uppercase; color: #8b949e; letter-spacing: .06em; }
  .stat-pill .val { font-size: 18px; font-weight: bold; margin-top: 4px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .bot-card { background: #181b20; border: 1px solid #282e38; border-radius: 10px; padding: 16px; position: relative; }
  .bot-card.playing { border-color: #059669; }
  .bot-card.banned { border-color: #dc2626; }
  .bot-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .bot-name { font-size: 16px; font-weight: bold; color: #f1f5f9; }
  .status-tag { font-size: 11px; text-transform: uppercase; padding: 2px 6px; border-radius: 4px; }
  .status-tag.playing { background: #064e3b; color: #34d399; }
  .status-tag.looking { background: #1e3a5f; color: #60a5fa; }
  .status-tag.waiting { background: #1e293b; color: #cbd5e1; }
  .status-tag.banned { background: #7f1d1d; color: #fca5a5; }
  .status-tag.crashed, .status-tag.failed { background: #881337; color: #fda4af; }
  .bot-details { font-size: 12px; line-height: 1.6; color: #cbd5e1; margin-bottom: 12px; }
  .bot-details span { color: #94a3b8; }
  .bot-actions { display: flex; gap: 6px; flex-wrap: wrap; }
  button { background: #22272e; color: #e6e6e6; border: 1px solid #373e47; border-radius: 6px; padding: 5px 10px; font-size: 12px; font-family: inherit; cursor: pointer; }
  button:hover { border-color: #38bdf8; color: #38bdf8; }
  button.danger { border-color: #ef4444; color: #ef4444; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 10px; }
  td, th { padding: 5px 8px; border-bottom: 1px solid #242930; text-align: left; }
  th { color: #8b949e; font-weight: normal; }
  td.t { color: #8b949e; width: 75px; }
  .levels td.me { color: #34d399; font-weight: bold; }
  .levels td.dead { color: #64748b; text-decoration: line-through; }
  .sec-title { font-size: 15px; margin: 24px 0 10px; color: #cbd5e1; }
</style>
</head>
<body>
<header>
  <div>
    <h1>🐺 Wolvesville Farm Enterprise</h1>
    <div style="font-size:12px; color:#94a3b8; margin-top:4px;">Headless Cluster on Samsung Galaxy A03s &bull; v<span id="farm-version">0.0.6</span></div>
  </div>
  <div id="conn-badge" class="badge">Connecting...</div>
</header>

<div class="summary-bar" id="summary-bar">
  <div class="stat-pill"><div class="lbl">Total Bots</div><div class="val" id="sum-bots">0</div></div>
  <div class="stat-pill"><div class="lbl">Playing Now</div><div class="val" id="sum-playing" style="color:#34d399">0</div></div>
  <div class="stat-pill"><div class="lbl">Farm XP Today</div><div class="val" id="sum-xp" style="color:#38bdf8">0</div></div>
  <div class="stat-pill"><div class="lbl">Games Finished</div><div class="val" id="sum-games">0</div></div>
  <div class="stat-pill"><div class="lbl">Errors Total</div><div class="val" id="sum-errors">0</div></div>
</div>

<div class="sec-title">🤖 Active Bot Instances</div>
<div class="grid" id="bot-grid"></div>

<div class="sec-title">🎮 Current Room Players</div>
<table class="levels" id="levels"></table>

<div class="sec-title">📜 Event Stream</div>
<table id="events"></table>

<script>
const fmt = s => new Date(s * 1000).toLocaleTimeString();

async function sendControl(action, bot = null) {
  await fetch('/api/control', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({kind: 'control', action, bot}),
  });
}

async function sendReward(action, bot = null) {
  await fetch('/api/control', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({kind: 'reward', action, bot}),
  });
}

function renderFarm(farm) {
  const bots = farm.bots || [];
  document.getElementById('farm-version').innerText = farm.version || '0.0.6';
  document.getElementById('sum-bots').innerText = bots.length;
  document.getElementById('sum-playing').innerText = farm.playing || 0;
  document.getElementById('sum-xp').innerText = (farm.total_xp || 0).toLocaleString();
  document.getElementById('sum-games').innerText = farm.games || 0;
  document.getElementById('sum-errors').innerText = farm.errors || 0;

  const grid = document.getElementById('bot-grid');
  if (!bots.length) {
    grid.innerHTML = '<div style="color:#64748b; padding:12px;">No active bots in roster.</div>';
    return;
  }

  grid.innerHTML = bots.map(b => {
    const st = b.status || 'idle';
    let tagClass = 'waiting';
    if (st === 'playing') tagClass = 'playing';
    else if (st.includes('looking')) tagClass = 'looking';
    else if (st === 'banned') tagClass = 'banned';
    else if (st.startsWith('crashed') || st === 'failed') tagClass = 'failed';

    const username = b.username || b.name || 'unnamed';
    const level = b.level !== null && b.level !== undefined ? b.level : '-';
    const room = b.room || '-';
    const role = b.role || '-';
    const xpToday = b.history?.today_xp || b.xp_total || 0;
    const errors = b.errors || 0;

    return `
      <div class="bot-card ${tagClass}">
        <div class="bot-header">
          <div class="bot-name">${username}</div>
          <div class="status-tag ${tagClass}">${st}</div>
        </div>
        <div class="bot-details">
          <div><span>Role:</span> <strong>${role}</strong> | <span>Phase:</span> ${b.phase || '-'}</div>
          <div><span>Room:</span> ${room}</div>
          <div><span>Level:</span> <strong>${level}</strong> | <span>XP Today:</span> +${xpToday}</div>
          <div><span>Errors:</span> ${errors} ${b.banned_until ? `| <span>Banned until:</span> ${b.banned_until}` : ''}</div>
        </div>
        <div class="bot-actions">
          <button onclick="sendControl('pause', '${b.name}')">Pause</button>
          <button onclick="sendControl('resume', '${b.name}')">Resume</button>
          <button onclick="sendControl('restart', '${b.name}')">Restart</button>
          <button onclick="sendReward('daily', '${b.name}')">Daily</button>
        </div>
      </div>
    `;
  }).join('');

  // Update players table from first active room
  const activeBot = bots.find(b => b.levels && b.levels.length > 0);
  const rows = activeBot ? activeBot.levels : [];
  document.getElementById('levels').innerHTML = rows.length
    ? rows.map(p => {
        const cls = p.own ? 'me' : (p.alive ? '' : 'dead');
        const mark = p.own ? ' (bot)' : '';
        return `<tr><td class="${cls}">${p.name || '?'}${mark}</td><td class="${cls}">lvl ${p.level}</td><td class="${cls}">${p.alive ? 'Alive' : 'Dead'}</td></tr>`;
      }).join('')
    : '<tr><td colspan="3" style="color:#64748b">No active game player data</td></tr>';
}

async function fetchEvents() {
  try {
    const res = await fetch('/api/events?limit=40');
    if (res.ok) {
      const ev = await res.json();
      document.getElementById('events').innerHTML = ev.map(e =>
        `<tr><td class="t">${fmt(e.ts)}</td><td>${e.kind}</td><td>${e.message}</td></tr>`).join('');
    }
  } catch (_) {}
}

function initSSE() {
  const badge = document.getElementById('conn-badge');
  if (!window.EventSource) {
    badge.innerText = 'Polling (3s)';
    setInterval(pollFarm, 3000);
    return;
  }

  const es = new EventSource('/api/stream');
  es.onopen = () => {
    badge.innerText = '🟢 Live Stream';
    badge.className = 'badge live';
  };
  es.addEventListener('farm', e => {
    try { renderFarm(JSON.parse(e.data)); } catch (_) {}
  });
  es.addEventListener('init', e => {
    try { renderFarm(JSON.parse(e.data)); } catch (_) {}
  });
  es.onerror = () => {
    badge.innerText = '🟡 Reconnecting...';
    badge.className = 'badge warn';
  };
}

async function pollFarm() {
  try {
    const res = await fetch('/api/farm');
    if (res.ok) {
      const data = await res.json();
      renderFarm(data);
    }
  } catch (_) {}
  fetchEvents();
}

initSSE();
pollFarm();
setInterval(fetchEvents, 4000);
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    store: StatusStore | None = None
    control: Any = None
    reward: Any = None
    rooms: Any = None
    farm: Any = None
    hub: Any = None

    def log_message(self, fmt: str, *args: Any) -> None:  # silence access log
        log.debug("dashboard %s", fmt % args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(204, b"", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
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
        bot_target = payload.get("bot")

        if kind == "state" and self.store is not None:
            fields = payload.get("state")
            if isinstance(fields, dict):
                self.store.update(**fields)
        elif kind == "event" and self.store is not None:
            message = str(payload.get("message") or "")
            if message:
                self.store.add_event(str(payload.get("level") or "info"), message)
        elif kind == "control":
            action = str(payload.get("action") or "")
            handled = False
            if self.control is not None:
                sig = inspect.signature(self.control)
                if len(sig.parameters) > 1:
                    handled = bool(self.control(action, bot_name=bot_target))
                else:
                    handled = bool(self.control(action))
            body = json.dumps({"ok": handled}).encode()
            self._send(200, body, "application/json")
            return
        elif kind == "reward":
            action = str(payload.get("action") or "")
            handled = False
            if self.reward is not None:
                sig = inspect.signature(self.reward)
                if len(sig.parameters) > 1:
                    handled = bool(self.reward(action, bot_name=bot_target))
                else:
                    handled = bool(self.reward(action))
            body = json.dumps({"ok": handled}).encode()
            self._send(200, body, "application/json")
            return

        self._send(200, b'{"ok":true}', "application/json")

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/api/stream"):
            if self.hub is None:
                self._send(503, b'{"error":"stream hub not configured"}', "application/json")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            client_queue = self.hub.subscribe()
            try:
                # Send initial snapshot frame
                initial = (
                    self.farm.snapshot()
                    if self.farm is not None
                    else (self.store.get() if self.store is not None else {})
                )
                self.wfile.write(f"event: init\ndata: {json.dumps(initial)}\n\n".encode("utf-8"))
                self.wfile.flush()

                while True:
                    try:
                        kind, data = client_queue.get(timeout=15.0)
                        self.wfile.write(f"event: {kind}\ndata: {data}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                self.hub.unsubscribe(client_queue)

        elif self.path.startswith("/api/farm"):
            if self.farm is not None:
                body = json.dumps(self.farm.snapshot(), indent=2).encode()
            elif self.store is not None:
                st = self.store.get()
                body = json.dumps(
                    {
                        "version": __version__,
                        "status": "single bot",
                        "bots": [{"name": "main", **st}],
                        "count": 1,
                        "playing": 1 if st.get("status") == "playing" else 0,
                        "total_xp": st.get("xp_total", 0),
                        "games": st.get("games_played", 0),
                        "errors": st.get("errors", 0),
                    },
                    indent=2,
                ).encode()
            else:
                body = b'{"bots":[],"count":0}'
            self._send(200, body, "application/json")

        elif self.path.startswith("/api/status"):
            if self.store is not None:
                data = self.store.get()
            elif self.farm is not None:
                snap = self.farm.snapshot()
                bots = snap.get("bots") or []
                data = dict(bots[0]) if bots else {"version": __version__, "status": "farm running"}
                data.setdefault("version", __version__)
                data.setdefault("status", "farm running")
            else:
                data = {"version": __version__, "status": "idle"}
            body = json.dumps(data, indent=2).encode()
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
            if self.store is not None:
                evs = self.store.events(limit)
            elif self.farm is not None:
                evs = []
                for slot in self.farm._slots:
                    if slot.bot is not None and hasattr(slot.bot, "store"):
                        evs.extend(slot.bot.store.events(limit))
                evs.sort(key=lambda x: x.get("ts", 0))
                evs = evs[-limit:]
            else:
                evs = []
            body = json.dumps(evs, indent=2).encode()
            self._send(200, body, "application/json")

        elif self.path.startswith("/api/rooms"):
            language = "en"
            host = ""
            if "?" in self.path:
                for part in self.path.split("?", 1)[1].split("&"):
                    if part.startswith("language="):
                        language = part[9:] or "en"
                    elif part.startswith("host="):
                        host = part[5:]
            result: dict[str, Any] = {}
            if self.rooms is None:
                result = {"error": "no room probe"}
            else:
                try:
                    result = {"rooms": self.rooms(language, host)}
                except Exception as exc:  # noqa: BLE001
                    result = {"error": str(exc)}
            body = json.dumps(result, indent=2).encode()
            self._send(200, body, "application/json")

        elif self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        else:
            self._send(404, b'{"error":"not found"}', "application/json")


def start_dashboard(
    store: StatusStore | None = None,
    host: str = "0.0.0.0",
    port: int = 8787,
    control: Any = None,
    reward: Any = None,
    rooms: Any = None,
    farm: Any = None,
    hub: Any = None,
):
    handler = type(
        "_BoundHandler",
        (_Handler,),
        {
            "store": store,
            "control": control,
            "reward": reward,
            "rooms": rooms,
            "farm": farm,
            "hub": hub,
        },
    )
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="dashboard", daemon=True)
    thread.start()
    log.info("dashboard listening on http://%s:%s", host, port)
    return server
