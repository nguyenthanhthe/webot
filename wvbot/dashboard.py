"""Status dashboard served by the bot / farm.

The bot runs on the phone/server; the dashboard exposes:

* ``GET /``             - HTML status page (SSE live push + polling fallback)
* ``GET /bot/<name>``   - Detailed telemetry & controls for a single bot
* ``GET /health``       - Unauthenticated Docker/liveness healthcheck
* ``GET /api/status``   - JSON snapshot of active bot
* ``GET /api/farm``     - JSON snapshot of all farm bots
* ``GET /api/stream``   - Server-Sent Events (SSE) live push
* ``GET /api/events``   - JSON list of recent events
* ``GET /api/accounts`` - Sanitized roster list
* ``GET /api/rooms``    - Live room probe
"""

from __future__ import annotations

import hmac
import inspect
import json
import logging
import os
import queue
import secrets
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__

log = logging.getLogger("wvbot.dashboard")

MAX_POST_BYTES = 65536
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _resolve_state_dir() -> Path:
    from . import config

    getter = getattr(config, "get_state_dir", None)
    return Path(getter()) if callable(getter) else Path(config.STATE_DIR)


def _write_token_file(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
            fh.flush()
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _resolve_dashboard_token(
    host: str,
    explicit_token: str | None = None,
    farm: Any = None,
    state_dir: Path | None = None,
) -> str:
    if explicit_token and explicit_token.strip():
        return explicit_token.strip()
    env_tok = (
        os.environ.get("WV_BOT_DASHBOARD_TOKEN")
        or os.environ.get("WV_DASHBOARD_TOKEN")
        or ""
    ).strip()
    if env_tok:
        return env_tok

    root = state_dir or (
        farm.farm_root if farm is not None and getattr(farm, "farm_root", None) else None
    )
    if root is not None:
        cfg_file = Path(root) / "config.json"
        if cfg_file.is_file():
            try:
                cfg_data = json.loads(cfg_file.read_text(encoding="utf-8"))
                if isinstance(cfg_data, dict):
                    cfg_tok = str(cfg_data.get("dashboard_token") or "").strip()
                    if cfg_tok:
                        return cfg_tok
            except (OSError, ValueError):
                pass

    if host.strip().lower() not in _LOOPBACK_HOSTS:
        token_root = Path(root) if root is not None else _resolve_state_dir()
        token_file = token_root / "dashboard_token.txt"
        if token_file.is_file():
            try:
                existing = token_file.read_text(encoding="utf-8").strip()
                if existing:
                    return existing
            except OSError:
                pass
        generated = secrets.token_urlsafe(24)
        try:
            _write_token_file(token_file, generated)
            log.info(
                "Dashboard bound to non-loopback %s: token auth enabled (stored in %s)",
                host,
                token_file,
            )
        except OSError as exc:
            log.warning("Could not persist dashboard_token.txt (%s); using ephemeral token", exc)
        return generated

    return ""


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
            "username": None,
            "level": None,
            "account_xp": None,
            "games_finished": None,
            "xp_until_next": None,
            "projection": {},
            "history": {},
            "update": {},
            "banned_until": None,
            "levels": [],
            "reflex_count": 0,
            "consecutive_games": 0,
            "games_to_bio_break": 12,
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


_SHARED_JS_HELPERS = """
function escHtml(v) {
  return String(v ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}
(function initTokenPersistence() {
  const params = new URLSearchParams(window.location.search);
  const urlTok = params.get('token');
  if (urlTok) {
    try { sessionStorage.setItem('wvbot_dashboard_token', urlTok); } catch (_) {}
    params.delete('token');
    const clean = window.location.pathname + (params.toString() ? '?' + params.toString() : '');
    try { history.replaceState(null, '', clean); } catch (_) {}
  }
})();
function getToken() {
  try { return sessionStorage.getItem('wvbot_dashboard_token') || ''; } catch (_) { return ''; }
}
function authHeaders(extra = {}) {
  const t = getToken();
  return t ? Object.assign({'X-Dashboard-Token': t}, extra) : extra;
}
function authUrl(path) {
  const t = getToken();
  if (!t) return path;
  const sep = path.includes('?') ? '&' : '?';
  return path + sep + 'token=' + encodeURIComponent(t);
}
function showNotice(text, isErr = false) {
  const el = document.getElementById('action-notice');
  if (!el) return;
  el.textContent = text;
  el.style.color = isErr ? '#fca5a5' : '#34d399';
  el.style.display = text ? 'block' : 'none';
}
"""


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
  a { color: #38bdf8; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .badge { font-size: 11px; padding: 3px 8px; border-radius: 999px; background: #1e293b; color: #94a3b8; }
  .badge.live { background: #064e3b; color: #34d399; }
  .badge.warn { background: #78350f; color: #fde047; }
  .summary-bar { display: flex; gap: 12px; margin-bottom: 24px; flex-wrap: wrap; }
  .stat-pill { background: #181b20; border: 1px solid #282e38; border-radius: 8px; padding: 10px 14px; flex: 1; min-width: 140px; }
  .stat-pill .lbl { font-size: 11px; text-transform: uppercase; color: #8b949e; letter-spacing: .06em; }
  .stat-pill .val { font-size: 18px; font-weight: bold; margin-top: 4px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 16px; margin-bottom: 24px; }
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
  button:hover:not(:disabled) { border-color: #38bdf8; color: #38bdf8; }
  button:disabled { opacity: 0.5; cursor: wait; }
  button.danger { border-color: #ef4444; color: #ef4444; }
  input[type="text"], input[type="password"] {
    background: #181b20; border: 1px solid #373e47; color: #e6e6e6;
    border-radius: 6px; padding: 5px 9px; font-family: inherit; font-size: 12px;
  }
  input:focus { border-color: #38bdf8; outline: none; }
  .badge-pill { font-size: 11px; padding: 2px 7px; border-radius: 4px; display: inline-block; font-weight: 500; }
  .badge-pill.ready { background: #064e3b; color: #34d399; }
  .badge-pill.missing { background: #78350f; color: #fde047; }
  .badge-pill.active { background: #1e3a5f; color: #60a5fa; }
  .badge-pill.inactive { background: #1e293b; color: #94a3b8; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 10px; }
  td, th { padding: 5px 8px; border-bottom: 1px solid #242930; text-align: left; }
  th { color: #8b949e; font-weight: normal; }
  td.t { color: #8b949e; width: 75px; }
  .levels td.me { color: #34d399; font-weight: bold; }
  .levels td.dead { color: #64748b; text-decoration: line-through; }
  .sec-title { font-size: 15px; margin: 24px 0 10px; color: #cbd5e1; }
  #action-notice { font-size: 12px; margin-bottom: 12px; display: none; }
</style>
</head>
<body>
<header>
  <div>
    <h1>Wolvesville Farm Enterprise</h1>
    <div style="font-size:12px; color:#94a3b8; margin-top:4px;">Headless Cluster &bull; v<span id="farm-version">__VERSION__</span></div>
  </div>
  <div id="conn-badge" class="badge">Connecting...</div>
</header>

<div id="action-notice"></div>

<div class="summary-bar" id="summary-bar">
  <div class="stat-pill"><div class="lbl">Total Bots</div><div class="val" id="sum-bots">0</div></div>
  <div class="stat-pill"><div class="lbl">Playing Now</div><div class="val" id="sum-playing" style="color:#34d399">0</div></div>
  <div class="stat-pill"><div class="lbl">Farm XP Today</div><div class="val" id="sum-xp" style="color:#38bdf8">0</div></div>
  <div class="stat-pill"><div class="lbl">Games Finished</div><div class="val" id="sum-games">0</div></div>
  <div class="stat-pill"><div class="lbl">Errors Total</div><div class="val" id="sum-errors">0</div></div>
</div>

<div class="sec-title">Active Bot Instances</div>
<div class="grid" id="bot-grid"></div>

<div class="sec-title" style="display:flex; justify-content:space-between; align-items:center;">
  <span>Farm Roster &amp; Accounts</span>
  <div style="display:flex; gap:8px;">
    <button id="btn-toggle-provision" style="background:#4338ca; border-color:#6366f1; color:#c7d2fe;">1-Click Temp Mail Farm</button>
    <button id="btn-toggle-add" style="background:#064e3b; border-color:#059669; color:#34d399;">+ Add Bot</button>
  </div>
</div>
<div id="provision-panel" style="display:none; background:#181b20; border:1px solid #4338ca; border-radius:8px; padding:12px; margin-bottom:16px;">
  <div style="font-weight:bold; margin-bottom:8px; font-size:13px; color:#818cf8;">1-Click Temp Email Farm Creator</div>
  <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:8px;">
    <input type="text" id="prov-url" placeholder="Worker URL (leave empty for Auto Zero-Config Mail.tm)" style="flex:2; min-width:220px;">
    <input type="password" id="prov-admin" placeholder="Admin Secret (optional if Auto)" style="flex:1; min-width:130px;">
    <input type="password" id="prov-site-auth" placeholder="Site Auth (optional)" style="flex:1; min-width:130px;">
    <input type="text" id="prov-domain" placeholder="Domain (optional)" style="width:130px;">
    <select id="prov-count" style="background:#181b20; border:1px solid #373e47; color:#e6e6e6; border-radius:6px; padding:5px 8px; font-size:12px;">
      <option value="1">1 Bot</option>
      <option value="2">2 Bots</option>
      <option value="3">3 Bots</option>
      <option value="4">4 Bots</option>
    </select>
    <button id="btn-submit-provision" style="background:#4f46e5; border-color:#818cf8; color:#fff; font-weight:bold;">Create &amp; Farm</button>
    <button id="btn-cancel-provision">Cancel</button>
  </div>
  <div id="prov-msg" style="font-size:11px; margin-top:6px; color:#94a3b8;">Requires interactive/browser registration bridge for Wolvesville Turnstile verification.</div>
</div>
<div id="add-account-panel" style="display:none; background:#181b20; border:1px solid #282e38; border-radius:8px; padding:12px; margin-bottom:16px;">
  <div style="font-weight:bold; margin-bottom:8px; font-size:13px; color:#38bdf8;">Add Account to Roster</div>
  <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center;">
    <input type="text" id="new-acc-name" placeholder="Account Name (e.g. bot2)" style="width:160px;">
    <input type="password" id="new-acc-token" placeholder="Refresh Token (optional / paste token)" style="flex:1; min-width:220px;">
    <button id="btn-submit-add" style="background:#0284c7; border-color:#38bdf8; color:#fff;">Save &amp; Add</button>
    <button id="btn-cancel-add">Cancel</button>
  </div>
  <div id="add-acc-msg" style="font-size:11px; margin-top:6px; color:#94a3b8;"></div>
</div>
<table id="roster-table" style="margin-bottom:24px;"></table>

<div class="sec-title">Current Room Players</div>
<table class="levels" id="levels"></table>

<div class="sec-title">Event Stream</div>
<table id="events"></table>

<script>
""" + _SHARED_JS_HELPERS + """
const fmt = s => new Date(s * 1000).toLocaleTimeString();

async function sendControl(action, bot = null, btn = null) {
  if (btn) btn.disabled = true;
  try {
    const res = await fetch('/api/control', {
      method: 'POST',
      headers: authHeaders({'Content-Type': 'application/json'}),
      body: JSON.stringify({kind: 'control', action, bot}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      showNotice(`Control '${action}' was not accepted (${data.error || res.status})`, true);
    } else {
      showNotice(`Sent '${action}'${bot ? ' to ' + bot : ''}`);
    }
  } catch (err) {
    showNotice('Control request error: ' + err, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function sendReward(action, bot = null, btn = null) {
  if (btn) btn.disabled = true;
  try {
    const res = await fetch('/api/control', {
      method: 'POST',
      headers: authHeaders({'Content-Type': 'application/json'}),
      body: JSON.stringify({kind: 'reward', action, bot}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      showNotice(`Reward '${action}' failed (${data.error || res.status})`, true);
    } else {
      showNotice(`Triggered '${action}'${bot ? ' on ' + bot : ''}`);
    }
  } catch (err) {
    showNotice('Reward request error: ' + err, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function fmtProjection(proj) {
  if (!proj || typeof proj !== 'object') return '-';
  const rate = proj.xp_per_hour ?? proj.rate_per_hour;
  const eta = proj.eta ?? proj.hours_to_next;
  const parts = [];
  if (rate !== undefined && rate !== null) parts.push(`${escHtml(rate)} XP/h`);
  if (eta !== undefined && eta !== null) parts.push(`ETA ${escHtml(eta)}`);
  return parts.length ? parts.join(' | ') : '-';
}

function renderFarm(farm) {
  const bots = farm.bots || [];
  document.getElementById('farm-version').textContent = farm.version || '__VERSION__';
  document.getElementById('sum-bots').textContent = bots.length;
  document.getElementById('sum-playing').textContent = farm.playing || 0;
  document.getElementById('sum-xp').textContent = (farm.total_xp || 0).toLocaleString();
  document.getElementById('sum-games').textContent = farm.games || 0;
  document.getElementById('sum-errors').textContent = farm.errors || 0;

  const grid = document.getElementById('bot-grid');
  if (!bots.length) {
    grid.innerHTML = '<div style="color:#64748b; padding:12px;">No active bots in roster.</div>';
    document.getElementById('levels').innerHTML =
      '<tr><td colspan="3" style="color:#64748b">No active game player data</td></tr>';
    return;
  }

  grid.innerHTML = bots.map(b => {
    const st = String(b.status || 'idle');
    let tagClass = 'waiting';
    if (st === 'playing') tagClass = 'playing';
    else if (st.includes('looking')) tagClass = 'looking';
    else if (st === 'banned') tagClass = 'banned';
    else if (st.startsWith('crashed') || st === 'failed') tagClass = 'failed';

    const botKey = String(b.name || 'main');
    const username = b.username || botKey || 'unnamed';
    const level = b.level !== null && b.level !== undefined ? b.level : '-';
    const room = b.room || '-';
    const role = b.role || '-';
    const phase = b.phase || '-';
    const xpToday = b.history?.today_xp ?? b.xp_total ?? 0;
    const xpNext = b.xp_until_next !== null && b.xp_until_next !== undefined ? b.xp_until_next : '-';
    const projStr = fmtProjection(b.projection);
    const reflexCount = b.reflex_count ?? 0;
    const bioBreak = b.games_to_bio_break ?? '-';
    const errors = b.errors || 0;
    const detailHref = escHtml(authUrl('/bot/' + encodeURIComponent(botKey)));

    return `
      <div class="bot-card ${escHtml(tagClass)}">
        <div class="bot-header">
          <div class="bot-name"><a href="${detailHref}">${escHtml(username)}</a></div>
          <div class="status-tag ${escHtml(tagClass)}">${escHtml(st)}</div>
        </div>
        <div class="bot-details">
          <div><span>Role:</span> <strong>${escHtml(role)}</strong> | <span>Phase:</span> ${escHtml(phase)}</div>
          <div><span>Room:</span> ${escHtml(room)}</div>
          <div><span>Level:</span> <strong>${escHtml(level)}</strong> | <span>XP Today:</span> +${escHtml(xpToday)} | <span>To Next:</span> ${escHtml(xpNext)}</div>
          <div><span>Projection:</span> ${projStr}</div>
          <div><span>Reflex:</span> ${escHtml(reflexCount)} | <span>Bio-Break:</span> ${escHtml(bioBreak)}g</div>
          <div><span>Errors:</span> ${escHtml(errors)} ${b.banned_until ? `| <span>Banned until:</span> ${escHtml(b.banned_until)}` : ''}</div>
        </div>
        <div class="bot-actions">
          <button data-control="pause" data-bot="${escHtml(botKey)}">Pause</button>
          <button data-control="resume" data-bot="${escHtml(botKey)}">Resume</button>
          <button data-control="restart" data-bot="${escHtml(botKey)}">Restart</button>
          <button data-reward="daily" data-bot="${escHtml(botKey)}">Daily</button>
          <button data-reward="lootbox" data-bot="${escHtml(botKey)}">Lootbox</button>
          <button data-reward="wheel" data-bot="${escHtml(botKey)}">Wheel</button>
        </div>
      </div>
    `;
  }).join('');

  const activeBot = bots.find(b => b.levels && b.levels.length > 0);
  const rows = activeBot ? activeBot.levels : [];
  document.getElementById('levels').innerHTML = rows.length
    ? rows.map(p => {
        const cls = p.own ? 'me' : (p.alive ? '' : 'dead');
        const mark = p.own ? ' (bot)' : '';
        return `<tr><td class="${escHtml(cls)}">${escHtml(p.name || '?')}${escHtml(mark)}</td><td class="${escHtml(cls)}">lvl ${escHtml(p.level)}</td><td class="${escHtml(cls)}">${p.alive ? 'Alive' : 'Dead'}</td></tr>`;
      }).join('')
    : '<tr><td colspan="3" style="color:#64748b">No active game player data</td></tr>';
}

async function fetchEvents() {
  try {
    const res = await fetch('/api/events?limit=40', {headers: authHeaders()});
    if (res.ok) {
      const ev = await res.json();
      document.getElementById('events').innerHTML = ev.map(e =>
        `<tr><td class="t">${escHtml(fmt(e.ts))}</td><td>${escHtml(e.kind)}</td><td>${escHtml(e.message)}</td></tr>`).join('');
    }
  } catch (_) {}
}

function toggleProvisionForm() {
  const panel = document.getElementById('provision-panel');
  panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
  document.getElementById('prov-msg').textContent = '';
}

async function submitProvision() {
  const btn = document.getElementById('btn-submit-provision');
  const url = document.getElementById('prov-url').value.trim();
  const admin = document.getElementById('prov-admin').value.trim();
  const site_auth = document.getElementById('prov-site-auth').value.trim();
  const domain = document.getElementById('prov-domain').value.trim();
  const count = parseInt(document.getElementById('prov-count').value || '1', 10);
  const msg = document.getElementById('prov-msg');
  const isAuto = (!url || url.toLowerCase() === 'auto' || url.toLowerCase() === 'mail.tm');
  if (!isAuto && !admin) {
    msg.textContent = 'Please provide Admin Secret for custom worker (or leave URL blank for Auto Zero-Config).';
    msg.style.color = '#ef4444';
    return;
  }
  btn.disabled = true;
  const modeStr = isAuto ? 'Auto Zero-Config' : 'Custom Worker';
  msg.textContent = 'Starting ' + modeStr + ' provisioning for ' + count + ' bot(s)... Check Event Stream.';
  msg.style.color = '#818cf8';
  try {
    const res = await fetch('/api/provision', {
      method: 'POST',
      headers: authHeaders({'Content-Type': 'application/json'}),
      body: JSON.stringify({url, admin, site_auth, domain, count})
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      msg.textContent = data.error || 'Provisioning failed.';
      msg.style.color = '#ef4444';
    } else {
      msg.textContent = data.message || 'Provisioning initiated! Watch Event Stream below.';
      msg.style.color = '#34d399';
    }
  } catch (err) {
    msg.textContent = 'Request error: ' + err;
    msg.style.color = '#ef4444';
  } finally {
    btn.disabled = false;
  }
}

function toggleAddForm() {
  const panel = document.getElementById('add-account-panel');
  panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
  document.getElementById('add-acc-msg').textContent = '';
}

async function submitAddAccount() {
  const btn = document.getElementById('btn-submit-add');
  const name = document.getElementById('new-acc-name').value.trim();
  const token = document.getElementById('new-acc-token').value.trim();
  const msg = document.getElementById('add-acc-msg');
  if (!name) {
    msg.textContent = 'Please provide an account name.';
    msg.style.color = '#ef4444';
    return;
  }
  btn.disabled = true;
  msg.textContent = 'Adding account...';
  msg.style.color = '#38bdf8';
  try {
    const res = await fetch('/api/accounts', {
      method: 'POST',
      headers: authHeaders({'Content-Type': 'application/json'}),
      body: JSON.stringify({name, refresh_token: token || null})
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      msg.textContent = `Added account '${data.name}' successfully!`;
      msg.style.color = '#34d399';
      document.getElementById('new-acc-name').value = '';
      document.getElementById('new-acc-token').value = '';
      setTimeout(() => { toggleAddForm(); }, 1200);
      fetchAccounts();
      pollFarm();
    } else {
      msg.textContent = `Error: ${data.error || 'failed to add'}`;
      msg.style.color = '#ef4444';
    }
  } catch (err) {
    msg.textContent = `Error: ${err.message}`;
    msg.style.color = '#ef4444';
  } finally {
    btn.disabled = false;
  }
}

async function removeAccount(name, btn = null) {
  if (!confirm(`Are you sure you want to remove account '${name}' from roster?`)) return;
  if (btn) btn.disabled = true;
  try {
    const res = await fetch(`/api/accounts/${encodeURIComponent(name)}`, {
      method: 'DELETE',
      headers: authHeaders(),
    });
    if (res.ok) {
      showNotice(`Removed '${name}'`);
      fetchAccounts();
      pollFarm();
    } else {
      const data = await res.json().catch(() => ({}));
      showNotice(`Failed to remove '${name}': ${data.error || res.status}`, true);
    }
  } catch (err) {
    showNotice('Delete failed: ' + err, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function fetchAccounts() {
  try {
    const res = await fetch('/api/accounts', {headers: authHeaders()});
    if (res.ok) {
      const data = await res.json();
      const accounts = data.accounts || [];
      const tbl = document.getElementById('roster-table');
      if (!accounts.length) {
        tbl.innerHTML = '<tr><td colspan="5" style="color:#64748b">No accounts configured</td></tr>';
        return;
      }
      tbl.innerHTML = '<tr><th>Account</th><th>Token</th><th>Farm Slot</th><th>Status</th><th>Actions</th></tr>' +
        accounts.map(a => {
          const tokBadge = a.ready
            ? '<span class="badge-pill ready">Ready</span>'
            : '<span class="badge-pill missing">Missing</span>';
          const actBadge = a.active
            ? '<span class="badge-pill active">Running</span>'
            : '<span class="badge-pill inactive">Inactive</span>';
          return `<tr>
            <td style="font-weight:bold; color:#f1f5f9;">${escHtml(a.name)}</td>
            <td>${tokBadge}</td>
            <td>${actBadge}</td>
            <td style="color:#94a3b8;">${escHtml(a.status)}</td>
            <td><button class="danger" data-remove-account="${escHtml(a.name)}">Remove</button></td>
          </tr>`;
        }).join('');
    }
  } catch (_) {}
}

let eventRefreshTimer = null;
function initSSE() {
  const badge = document.getElementById('conn-badge');
  if (!window.EventSource) {
    badge.textContent = 'Polling (3s)';
    setInterval(pollFarm, 3000);
    return;
  }

  const es = new EventSource(authUrl('/api/stream'));
  es.onopen = () => {
    badge.textContent = 'Live Stream';
    badge.className = 'badge live';
  };
  es.addEventListener('farm', e => {
    try { renderFarm(JSON.parse(e.data)); } catch (_) {}
  });
  es.addEventListener('init', e => {
    try { renderFarm(JSON.parse(e.data)); } catch (_) {}
  });
  es.addEventListener('event', () => {
    if (!eventRefreshTimer) {
      eventRefreshTimer = setTimeout(() => {
        eventRefreshTimer = null;
        fetchEvents();
      }, 250);
    }
  });
  es.onerror = () => {
    badge.textContent = 'Reconnecting...';
    badge.className = 'badge warn';
  };
}

async function pollFarm() {
  try {
    const res = await fetch('/api/farm', {headers: authHeaders()});
    if (res.ok) {
      const data = await res.json();
      renderFarm(data);
    }
  } catch (_) {}
  fetchEvents();
}

document.getElementById('bot-grid').addEventListener('click', e => {
  const btn = e.target.closest('button');
  if (!btn) return;
  if (btn.dataset.control) {
    sendControl(btn.dataset.control, btn.dataset.bot || null, btn);
  } else if (btn.dataset.reward) {
    sendReward(btn.dataset.reward, btn.dataset.bot || null, btn);
  }
});

document.getElementById('roster-table').addEventListener('click', e => {
  const btn = e.target.closest('button[data-remove-account]');
  if (!btn) return;
  removeAccount(btn.dataset.removeAccount, btn);
});

document.getElementById('btn-toggle-provision').addEventListener('click', toggleProvisionForm);
document.getElementById('btn-cancel-provision').addEventListener('click', toggleProvisionForm);
document.getElementById('btn-submit-provision').addEventListener('click', submitProvision);
document.getElementById('btn-toggle-add').addEventListener('click', toggleAddForm);
document.getElementById('btn-cancel-add').addEventListener('click', toggleAddForm);
document.getElementById('btn-submit-add').addEventListener('click', submitAddAccount);

initSSE();
pollFarm();
fetchAccounts();
setInterval(pollFarm, 5000);
setInterval(fetchAccounts, 6000);
</script>
</body>
</html>
""".replace("__VERSION__", __version__)


BOT_DETAIL_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bot Detail</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; margin: 0; padding: 24px; background: #0f1115; color: #e6e6e6; }
  a { color: #38bdf8; text-decoration: none; }
  .card { background: #181b20; border: 1px solid #282e38; border-radius: 10px; padding: 16px; margin-bottom: 16px; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; margin: 12px 0; }
  button { background: #22272e; color: #e6e6e6; border: 1px solid #373e47; border-radius: 6px; padding: 5px 10px; font-family: inherit; cursor: pointer; }
  button:hover:not(:disabled) { border-color: #38bdf8; color: #38bdf8; }
  button:disabled { opacity: 0.5; cursor: wait; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  td, th { padding: 5px 8px; border-bottom: 1px solid #242930; text-align: left; }
  #action-notice { font-size: 12px; margin: 8px 0; display: none; }
</style>
</head>
<body>
<div><a id="back-link" href="/">&larr; Back to Farm</a></div>
<h1 id="bot-title" style="color:#38bdf8; font-size:20px;"></h1>
<div id="action-notice"></div>
<div class="card" id="bot-telemetry"></div>
<div class="actions" id="bot-actions">
  <button data-control="pause">Pause</button>
  <button data-control="resume">Resume</button>
  <button data-control="restart">Restart</button>
  <button data-reward="daily">Daily</button>
  <button data-reward="lootbox">Lootbox</button>
  <button data-reward="wheel">Wheel</button>
</div>
<h3>Room Players</h3>
<table id="bot-levels"></table>
<h3>Recent Events</h3>
<table id="bot-events"></table>
<script>
""" + _SHARED_JS_HELPERS + """
const BOT_NAME = __BOT_NAME_JSON__;
document.getElementById('bot-title').textContent = 'Bot: ' + BOT_NAME;
document.getElementById('back-link').href = authUrl('/');

function renderBot(farm) {
  const bots = farm.bots || [];
  const b = bots.find(item => String(item.name || 'main') === BOT_NAME);
  const box = document.getElementById('bot-telemetry');
  if (!b) {
    box.textContent = 'Bot is not currently active.';
    return;
  }
  const proj = b.projection && typeof b.projection === 'object' ? JSON.stringify(b.projection) : '-';
  box.innerHTML = `
    <div><strong>Status:</strong> ${escHtml(b.status)} | <strong>Phase:</strong> ${escHtml(b.phase || '-')} | <strong>Role:</strong> ${escHtml(b.role || '-')}</div>
    <div><strong>Username:</strong> ${escHtml(b.username || '-')} | <strong>Level:</strong> ${escHtml(b.level ?? '-')} | <strong>Room:</strong> ${escHtml(b.room || '-')}</div>
    <div><strong>XP Total:</strong> ${escHtml(b.xp_total ?? 0)} | <strong>XP Last:</strong> ${escHtml(b.xp_last ?? 0)} | <strong>XP Until Next:</strong> ${escHtml(b.xp_until_next ?? '-')}</div>
    <div><strong>Projection:</strong> ${escHtml(proj)}</div>
    <div><strong>Games:</strong> ${escHtml(b.games_played ?? 0)} | <strong>Wins:</strong> ${escHtml(b.wins ?? 0)} | <strong>Reflex Count:</strong> ${escHtml(b.reflex_count ?? 0)} | <strong>Games to Bio-Break:</strong> ${escHtml(b.games_to_bio_break ?? '-')}</div>
    <div><strong>Reconnects:</strong> ${escHtml(b.reconnects ?? 0)} | <strong>Errors:</strong> ${escHtml(b.errors ?? 0)} | <strong>Last Error:</strong> ${escHtml(b.last_error || '-')}</div>
  `;
  const levels = b.levels || [];
  document.getElementById('bot-levels').innerHTML = levels.length
    ? levels.map(p => `<tr><td>${escHtml(p.name || '?')}</td><td>lvl ${escHtml(p.level)}</td><td>${p.alive ? 'Alive' : 'Dead'}</td></tr>`).join('')
    : '<tr><td colspan="3">No room player data</td></tr>';
}

async function refresh() {
  try {
    const r = await fetch('/api/farm', {headers: authHeaders()});
    if (r.ok) renderBot(await r.json());
  } catch (_) {}
  try {
    const r = await fetch('/api/events?limit=40', {headers: authHeaders()});
    if (r.ok) {
      const ev = await r.json();
      document.getElementById('bot-events').innerHTML = ev.map(
        e => `<tr><td>${escHtml(new Date(e.ts * 1000).toLocaleTimeString())}</td><td>${escHtml(e.kind)}</td><td>${escHtml(e.message)}</td></tr>`
      ).join('');
    }
  } catch (_) {}
}

document.getElementById('bot-actions').addEventListener('click', async e => {
  const btn = e.target.closest('button');
  if (!btn) return;
  btn.disabled = true;
  const kind = btn.dataset.control ? 'control' : 'reward';
  const action = btn.dataset.control || btn.dataset.reward;
  try {
    const res = await fetch('/api/control', {
      method: 'POST',
      headers: authHeaders({'Content-Type': 'application/json'}),
      body: JSON.stringify({kind, action, bot: BOT_NAME}),
    });
    const data = await res.json().catch(() => ({}));
    showNotice(res.ok && data.ok ? `Sent ${action} to ${BOT_NAME}` : `Failed: ${data.error || res.status}`, !(res.ok && data.ok));
  } catch (err) {
    showNotice('Error: ' + err, true);
  } finally {
    btn.disabled = false;
  }
});

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


_AUTH_PROMPT_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Dashboard Auth</title></head>
<body style="background:#0f1115;color:#e6e6e6;font-family:monospace;padding:32px;">
<h2>Dashboard Authentication Required</h2>
<form id="f">
  <input id="t" type="password" placeholder="Dashboard token" style="padding:6px;width:280px;">
  <button type="submit" style="padding:6px 12px;">Unlock</button>
</form>
<script>
document.getElementById('f').addEventListener('submit', e => {
  e.preventDefault();
  const tok = document.getElementById('t').value.trim();
  if (tok) window.location.href = '/?token=' + encodeURIComponent(tok);
});
</script></body></html>
"""


def get_accounts_summary(farm_root: Path, farm: Any = None) -> list[dict[str, Any]]:
    """Return sanitized account list for API/dashboard (no tokens exposed)."""
    from .config import REFRESH_TOKEN_FILE
    from .farm import load_roster

    try:
        roster = load_roster(farm_root)
    except Exception:
        roster = [{"name": "main"}]

    active_slots: dict[str, tuple[str, bool]] = {}
    if farm is not None and hasattr(farm, "_slots"):
        lock = getattr(farm, "_lock", None)
        if lock is not None:
            with lock:
                for slot in getattr(farm, "_slots", []):
                    active_slots[slot.name] = (slot.status, not getattr(slot, "removed", False))
        else:
            for slot in list(getattr(farm, "_slots", [])):
                active_slots[slot.name] = (slot.status, True)

    result: list[dict[str, Any]] = []
    for entry in roster:
        name = str(entry.get("name") or "main")
        acc_dir = (
            farm_root / "accounts" / name
            if name != "main" or (farm_root / "accounts" / name).is_dir()
            else farm_root
        )
        token_path = acc_dir / REFRESH_TOKEN_FILE
        has_token = False
        if token_path.is_file():
            try:
                has_token = len(token_path.read_text(encoding="utf-8").strip()) > 0
            except OSError:
                pass
        if not has_token and entry.get("refresh_token"):
            has_token = len(str(entry.get("refresh_token")).strip()) > 0

        slot_info = active_slots.get(name)
        status = slot_info[0] if slot_info is not None else ("inactive" if farm is not None else "ready")

        item: dict[str, Any] = {
            "name": name,
            "ready": has_token,
            "has_token": has_token,
            "active": slot_info is not None and slot_info[1],
            "status": status,
        }
        for k, v in entry.items():
            if k not in ("refresh_token", "name", "password"):
                item[k] = v
        result.append(item)
    return result


class _DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], handler_cls: type[BaseHTTPRequestHandler]):
        self.stop_event = threading.Event()
        super().__init__(server_address, handler_cls)

    def shutdown(self) -> None:
        self.stop_event.set()
        super().shutdown()

    def server_close(self) -> None:
        self.stop_event.set()
        super().server_close()


class _Handler(BaseHTTPRequestHandler):
    store: StatusStore | None = None
    control: Any = None
    reward: Any = None
    rooms: Any = None
    farm: Any = None
    hub: Any = None
    auth_token: str = ""
    state_dir: Path | None = None
    server_stop_event: threading.Event | None = None

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("dashboard %s", fmt % args)

    def _farm_root(self) -> Path:
        if self.farm is not None and getattr(self.farm, "farm_root", None) is not None:
            return Path(self.farm.farm_root)
        if self.state_dir is not None:
            return Path(self.state_dir)
        return _resolve_state_dir()

    def _send(self, code: int, body: bytes, content_type: str, extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self) -> bool:
        parsed = urlsplit(self.path)
        if self.command == "GET" and parsed.path == "/health":
            return True
        expected = self.auth_token
        if not expected:
            return True

        candidates: list[str] = []
        auth_hdr = self.headers.get("Authorization") or ""
        if auth_hdr[:7].lower() == "bearer ":
            candidates.append(auth_hdr[7:].strip())
        x_tok = (self.headers.get("X-Dashboard-Token") or "").strip()
        if x_tok:
            candidates.append(x_tok)
        qs = parse_qs(parsed.query)
        q_tok = (qs.get("token") or [""])[0].strip()
        if q_tok:
            candidates.append(q_tok)

        for cand in candidates:
            if cand and hmac.compare_digest(cand, expected):
                return True

        if parsed.path.startswith("/api/") or self.command != "GET":
            self._send(
                401,
                b'{"error":"unauthorized"}',
                "application/json",
                extra_headers={"WWW-Authenticate": "Bearer"},
            )
        else:
            self._send(
                401,
                _AUTH_PROMPT_HTML.encode("utf-8"),
                "text/html; charset=utf-8",
                extra_headers={"WWW-Authenticate": "Bearer"},
            )
        return False

    def _single_bot_envelope(self) -> dict[str, Any]:
        if self.store is None:
            return {
                "version": __version__,
                "status": "idle",
                "bots": [],
                "count": 0,
                "playing": 0,
                "total_xp": 0,
                "games": 0,
                "errors": 0,
            }
        st = self.store.get()
        return {
            "version": __version__,
            "status": "single bot",
            "bots": [{"name": "main", **st}],
            "count": 1,
            "playing": 1 if st.get("status") == "playing" else 0,
            "total_xp": st.get("xp_total", 0),
            "games": st.get("games_played", 0),
            "errors": st.get("errors", 0),
        }

    def _publish_event(self, kind: str, message: str) -> None:
        if self.store is not None:
            self.store.add_event(kind, message)
        if self.hub is not None:
            try:
                self.hub.publish("event", {"kind": kind, "message": message, "ts": time.time()})
            except Exception:
                pass

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Allow", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Dashboard-Token")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._check_auth():
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/api/accounts" or path.startswith("/api/accounts/"):
            name = ""
            if path.startswith("/api/accounts/"):
                name = unquote(path[len("/api/accounts/") :]).strip()
            else:
                qs = parse_qs(parsed.query)
                name = unquote((qs.get("name") or [""])[0]).strip()
            if not name:
                self._send(400, b'{"error":"missing account name"}', "application/json")
                return
            from .farm import RosterError, clean_name, remove_account_from_roster

            if "/" in name or "\\" in name or ".." in name or "\x00" in name or not clean_name(name):
                self._send(400, b'{"error":"invalid account name"}', "application/json")
                return

            root = self._farm_root()
            try:
                removed = remove_account_from_roster(root, name)
            except RosterError as exc:
                self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
                return
            if self.farm is not None and hasattr(self.farm, "remove_account"):
                self.farm.remove_account(name)
            self._send(
                200,
                json.dumps({"ok": True, "name": name, "removed": removed}).encode(),
                "application/json",
            )
            return
        self._send(404, b'{"error":"not found"}', "application/json")

    def do_POST(self) -> None:  # noqa: N802
        if not self._check_auth():
            return

        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            self._send(
                415,
                b'{"error":"Content-Type must be application/json"}',
                "application/json",
            )
            return

        raw_len = self.headers.get("Content-Length")
        if raw_len is not None:
            try:
                length = int(raw_len)
            except ValueError:
                self._send(400, b'{"error":"invalid Content-Length"}', "application/json")
                return
            if length < 0:
                self._send(400, b'{"error":"negative Content-Length"}', "application/json")
                return
            if length > MAX_POST_BYTES:
                self._send(413, b'{"error":"payload too large"}', "application/json")
                return
        else:
            length = 0

        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, b'{"error":"bad json"}', "application/json")
            return
        if not isinstance(payload, dict):
            self._send(400, b'{"error":"bad payload"}', "application/json")
            return

        path = urlsplit(self.path).path
        kind = payload.get("kind")
        bot_target = payload.get("bot")

        if path in ("/api/control", "/api/state", "/api/event"):
            if kind is None and path == "/api/state":
                kind = "state"
            elif kind is None and path == "/api/event":
                kind = "event"

            if kind == "state":
                fields = payload.get("state")
                if not isinstance(fields, dict):
                    self._send(400, b'{"error":"missing state object"}', "application/json")
                    return
                if self.store is not None:
                    self.store.update(**fields)
                self._send(200, b'{"ok":true}', "application/json")
                return

            if kind == "event":
                message = str(payload.get("message") or "")
                if message:
                    self._publish_event(str(payload.get("level") or "info"), message)
                self._send(200, b'{"ok":true}', "application/json")
                return

            if kind == "control":
                action = str(payload.get("action") or "")
                handled = False
                if self.control is not None:
                    sig = inspect.signature(self.control)
                    if len(sig.parameters) > 1:
                        handled = bool(self.control(action, bot_name=bot_target))
                    else:
                        handled = bool(self.control(action))
                self._send(200, json.dumps({"ok": handled}).encode(), "application/json")
                return

            if kind == "reward":
                action = str(payload.get("action") or "")
                handled = False
                if self.reward is not None:
                    sig = inspect.signature(self.reward)
                    if len(sig.parameters) > 1:
                        handled = bool(self.reward(action, bot_name=bot_target))
                    else:
                        handled = bool(self.reward(action))
                self._send(200, json.dumps({"ok": handled}).encode(), "application/json")
                return

            self._send(400, b'{"error":"unknown control kind"}', "application/json")
            return

        if path == "/api/accounts" or path.startswith("/api/accounts/"):
            action = str(payload.get("action") or "").lower()
            from .farm import RosterError, load_roster, remove_account_from_roster, save_account_to_roster

            root = self._farm_root()
            name = str(payload.get("name") or "").strip()
            if not name and path.startswith("/api/accounts/"):
                name = unquote(path[len("/api/accounts/") :]).strip()

            if action in ("remove", "delete") or kind == "remove_account":
                if not name:
                    self._send(400, b'{"error":"missing account name"}', "application/json")
                    return
                try:
                    removed = remove_account_from_roster(root, name)
                except RosterError as exc:
                    self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
                    return
                if self.farm is not None and hasattr(self.farm, "remove_account"):
                    self.farm.remove_account(name)
                self._send(
                    200,
                    json.dumps({"ok": True, "name": name, "removed": removed}).encode(),
                    "application/json",
                )
                return

            token = payload.get("refresh_token")
            if not name:
                self._send(400, b'{"error":"missing account name"}', "application/json")
                return
            existed_before = False
            try:
                if (root / "accounts.json").is_file():
                    existed_before = any(a.get("name") == name for a in load_roster(root))
            except RosterError:
                pass
            try:
                entry = save_account_to_roster(root, name, token)
            except Exception as exc:  # noqa: BLE001
                self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
                return

            if self.farm is not None and hasattr(self.farm, "add_account"):
                added = self.farm.add_account(entry)
                if not added:
                    if not existed_before:
                        try:
                            remove_account_from_roster(root, str(entry.get("name") or name))
                        except Exception:
                            pass
                    self._send(
                        409,
                        json.dumps(
                            {
                                "ok": False,
                                "error": "account already exists in farm or max_bots reached",
                            }
                        ).encode(),
                        "application/json",
                    )
                    return

            self._send(
                200,
                json.dumps({"ok": True, "name": entry.get("name")}).encode(),
                "application/json",
            )
            return

        if path == "/api/provision":
            from .provision import AccountProvisioner
            from .temp_email import TempEmailError

            url = str(payload.get("url") or payload.get("temp_email_url") or "").strip()
            admin = str(payload.get("admin") or payload.get("admin_password") or "").strip()
            domain = payload.get("domain") or None
            site_auth = payload.get("site_auth") or None

            raw_count = payload.get("count", 1)
            if isinstance(raw_count, bool):
                self._send(400, b'{"error":"invalid count"}', "application/json")
                return
            try:
                count = int(raw_count)
            except (TypeError, ValueError):
                self._send(400, b'{"error":"invalid count"}', "application/json")
                return
            if count < 1 or count > 20:
                self._send(400, b'{"error":"count must be between 1 and 20"}', "application/json")
                return

            clean_url = url.lower()
            if clean_url and clean_url not in ("auto", "mail.tm", "none") and not admin:
                self._send(400, b'{"error":"missing admin password for custom worker"}', "application/json")
                return

            try:
                provisioner = AccountProvisioner(
                    temp_email_url=url,
                    admin_password=admin,
                    site_password=site_auth,
                    domain=domain,
                )
            except TempEmailError as exc:
                self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
                return

            root = self._farm_root()

            def _do_provision() -> None:
                try:
                    self._publish_event("provision", f"Starting provisioning for {count} bot(s)...")
                    created = provisioner.batch_provision(
                        count=count,
                        farm_root=root,
                        farm_instance=self.farm,
                        progress_callback=lambda msg, _pct: self._publish_event("provision", msg),
                    )
                    self._publish_event(
                        "provision",
                        f"Successfully provisioned {len(created)} bot account(s) into farm!",
                    )
                except Exception as exc:  # noqa: BLE001
                    log.error("Provisioning failed: %s", exc)
                    self._publish_event("error", f"Provisioning failed: {exc}")

            threading.Thread(target=_do_provision, name="provisioner", daemon=True).start()
            self._send(
                200,
                json.dumps({"ok": True, "message": "Provisioning started in background"}).encode(),
                "application/json",
            )
            return

        self._send(404, b'{"error":"not found"}', "application/json")

    def do_GET(self) -> None:  # noqa: N802
        if not self._check_auth():
            return

        parsed = urlsplit(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/health":
            stopping = self.farm is not None and bool(self.farm.stop_event.is_set())
            if stopping:
                self._send(
                    503,
                    json.dumps({"ok": False, "status": "stopping", "version": __version__}).encode(),
                    "application/json",
                )
            else:
                self._send(
                    200,
                    json.dumps({"ok": True, "version": __version__}).encode(),
                    "application/json",
                )
            return

        if path == "/api/stream":
            if self.hub is None:
                self._send(503, b'{"error":"stream hub not configured"}', "application/json")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.close_connection = True

            client_queue = self.hub.subscribe()
            try:
                initial = (
                    self.farm.snapshot()
                    if self.farm is not None
                    else self._single_bot_envelope()
                )
                self.wfile.write(f"event: init\ndata: {json.dumps(initial)}\n\n".encode("utf-8"))
                self.wfile.flush()

                last_ping = time.monotonic()
                last_updated = (
                    self.store.get().get("updated_at")
                    if self.farm is None and self.store is not None
                    else None
                )
                while True:
                    if self.server_stop_event is not None and self.server_stop_event.is_set():
                        break
                    if self.farm is not None and self.farm.stop_event.is_set():
                        break
                    try:
                        kind, data = client_queue.get(timeout=0.5)
                        self.wfile.write(f"event: {kind}\ndata: {data}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        last_ping = time.monotonic()
                    except queue.Empty:
                        now_mono = time.monotonic()
                        if self.farm is None and self.store is not None:
                            cur_snap = self.store.get()
                            cur_updated = cur_snap.get("updated_at")
                            if cur_updated != last_updated:
                                last_updated = cur_updated
                                env = self._single_bot_envelope()
                                self.wfile.write(
                                    f"event: farm\ndata: {json.dumps(env)}\n\n".encode("utf-8")
                                )
                                self.wfile.flush()
                                last_ping = now_mono
                                continue
                        if now_mono - last_ping >= 15.0:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            last_ping = now_mono
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                self.hub.unsubscribe(client_queue)
            return

        if path == "/api/farm":
            if self.farm is not None:
                body = json.dumps(self.farm.snapshot(), indent=2).encode()
            else:
                body = json.dumps(self._single_bot_envelope(), indent=2).encode()
            self._send(200, body, "application/json")
            return

        if path == "/api/accounts":
            accs = get_accounts_summary(self._farm_root(), self.farm)
            body = json.dumps({"accounts": accs, "count": len(accs)}, indent=2).encode()
            self._send(200, body, "application/json")
            return

        if path == "/api/status":
            if self.farm is not None:
                snap = self.farm.snapshot()
                bots = snap.get("bots") or []
                data = dict(bots[0]) if bots else {"version": __version__, "status": snap.get("status", "farm running")}
                data.setdefault("version", __version__)
                data.setdefault("status", "farm running")
            elif self.store is not None:
                data = self.store.get()
            else:
                data = {"version": __version__, "status": "idle"}
            body = json.dumps(data, indent=2).encode()
            self._send(200, body, "application/json")
            return

        if path == "/api/events":
            limit = 100
            raw_limit = (qs.get("limit") or [None])[0]
            if raw_limit is not None:
                try:
                    limit = max(1, min(500, int(raw_limit)))
                except ValueError:
                    pass
            if self.farm is not None and hasattr(self.farm, "collect_events"):
                evs = self.farm.collect_events(limit)
            elif self.store is not None:
                evs = self.store.events(limit)
            else:
                evs = []
            body = json.dumps(evs, indent=2).encode()
            self._send(200, body, "application/json")
            return

        if path == "/api/rooms":
            language = (qs.get("language") or ["en"])[0] or "en"
            host = (qs.get("host") or [""])[0]
            if self.rooms is None:
                self._send(503, b'{"error":"no room probe"}', "application/json")
                return
            try:
                rooms_data = self.rooms(language, host)
            except Exception as exc:  # noqa: BLE001
                self._send(502, json.dumps({"error": str(exc)}).encode(), "application/json")
                return
            self._send(200, json.dumps({"rooms": rooms_data}, indent=2).encode(), "application/json")
            return

        if path.startswith("/bot/"):
            bot_name = unquote(path[len("/bot/") :]).strip()
            if not bot_name or "/" in bot_name or "\\" in bot_name or ".." in bot_name:
                self._send(404, b'{"error":"bot not found"}', "application/json")
                return
            snap = self.farm.snapshot() if self.farm is not None else self._single_bot_envelope()
            bots = snap.get("bots") or []
            if not any(str(b.get("name") or "main") == bot_name for b in bots):
                self._send(404, b'{"error":"bot not found"}', "application/json")
                return
            safe_bot_json = (
                json.dumps(bot_name)
                .replace("<", "\\u003c")
                .replace(">", "\\u003e")
                .replace("&", "\\u0026")
            )
            html = BOT_DETAIL_PAGE.replace("__BOT_NAME_JSON__", safe_bot_json)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return

        self._send(404, b'{"error":"not found"}', "application/json")


def start_dashboard(
    store: StatusStore | None = None,
    host: str = "127.0.0.1",
    port: int = 8787,
    control: Any = None,
    reward: Any = None,
    rooms: Any = None,
    farm: Any = None,
    hub: Any = None,
    token: str | None = None,
    state_dir: Path | None = None,
):
    auth_token = _resolve_dashboard_token(
        host=host,
        explicit_token=token,
        farm=farm,
        state_dir=state_dir,
    )
    server_stop_event = threading.Event()
    handler = type(
        "_BoundHandler",
        (_Handler,),
        {
            "store": store,
            "control": staticmethod(control) if control is not None else None,
            "reward": staticmethod(reward) if reward is not None else None,
            "rooms": staticmethod(rooms) if rooms is not None else None,
            "farm": farm,
            "hub": hub,
            "auth_token": auth_token,
            "state_dir": state_dir,
            "server_stop_event": server_stop_event,
        },
    )
    server = _DashboardHTTPServer((host, port), handler)
    server.stop_event = server_stop_event
    server.dashboard_token = auth_token
    thread = threading.Thread(target=server.serve_forever, name="dashboard", daemon=True)
    thread.start()
    log.info("dashboard listening on http://%s:%s", host, port)
    return server
