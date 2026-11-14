# Wolvesville Enterprise Bot Farm (v1.1.0)

An autonomous, multi-account, headless farming cluster for Wolvesville custom games ("vill win" / TF rooms) with 24/7 reliability, real-time SSE dashboard, and hot-roster management.

The bot communicates directly with Wolvesville servers over REST and Socket.IO. It mints its own Firebase tokens from stored refresh tokens, queries room lobbies, and plays matches autonomously with randomized human-like pacing.

**100% Python Standard Library** — zero pip dependencies, zero browser overhead in protocol mode. Runs seamlessly inside Termux on an Android device or inside a non-root Docker container.

```
┌───────────────────────────────── Termux / Docker ─────────────────────────────────┐
│                                                                                   │
│  Farm Supervisor (wvbot.farm)                                                     │
│   ├── accounts.json          Roster configuration (mode 0600)                     │
│   ├── Bot #1 (thread) ────┐                                                       │
│   ├── Bot #2 (thread) ────┼── Independent state dirs (STATE_DIR/accounts/<name>/) │
│   └── Bot #N (thread) ────┘   Independent sockets, pacing, and crash backoffs     │
│                                                                                   │
│  Live Dashboard & REST API (:8787)                                                │
│   ├── GET  /              Live Web UI (SSE Stream + Roster Manager + Bot Grid)    │
│   ├── GET  /bot/<name>    Per-bot detail view                                     │
│   ├── GET  /health        Unauthenticated container/process health probe          │
│   ├── GET  /api/status    Active bot state snapshot                               │
│   ├── GET  /api/farm      All bot instances snapshot                              │
│   ├── GET  /api/stream    Server-Sent Events (SSE) live push                      │
│   ├── GET  /api/accounts  Sanitized account enumeration & readiness probe         │
│   ├── POST /api/accounts  Hot-add bot account & launch slot dynamically           │
│   ├── DEL  /api/accounts  Graceful bot slot termination & roster cleanup          │
│   └── POST /api/provision Trigger temp-email farm provisioning job                │
└─────────────────────────────────────────┬─────────────────────────────────────────┘
                                          │
                  ┌───────────────────────┴───────────────────────┐
                  ▼                                               ▼
     ┌────────────────────────┐                      ┌────────────────────────┐
     │ game.api-wolvesville   │                      │ Laptop / Phone Browser │
     │ (REST + Socket.IO)     │                      │ (127.0.0.1:8787 / ADB) │
     └────────────────────────┘                      └────────────────────────┘
```

---

## Key Features

- **Multi-Account Autonomous Farm:** Run multiple bot accounts in isolated threads with dedicated state directories, independent pacing, and individual ban cooldowns.
- **Hot-Roster Management:** Dynamically add or remove bot accounts via Web Dashboard UI, REST API (`/api/accounts`), or CLI (`tools/roster.py`) without restarting the cluster.
- **Deterministic Reflex Engine (`wvbot/reflex.py`):** Pure stdlib regex reflex engine (~0.1 ms latency) answering seat/AFK/role checks with role-claim deduplication and typing jitter.
- **Turbo Vill-Win & Smart Room Selection:** Skips 0-XP rooms (`regularXp`), supports TF timer detection (`strict_tf_only`, `prefer_8p_rooms`, `strict_8p_only`), continuous `stay_in_room` rematch, bandwagon voting, and `vill_win_mode` fast rounds.
- **Fast Reconnect & Ban Protection:** Recovers from mid-game socket drops cleanly and sleeps strictly past `bannedUntilTime` without touching game endpoints while banned.
- **Temp Email Provisioning Helpers (`wvbot/temp_email.py`, `wvbot/provision.py`):** Supports zero-config `MailTmClient` (`api.mail.tm`) and self-hosted Cloudflare Temp Email (`TempEmailClient`), requiring an explicit browser registration callback (no CAPTCHA/Turnstile bypasses).
- **Dashboard Token Auth & Healthcheck:** Binds `127.0.0.1:8787` by default, exposes unauthenticated `/health`, and enforces bearer/header/query token auth (`dashboard_token.txt` / `WV_BOT_DASHBOARD_TOKEN`) automatically on non-loopback binds.
- **Process Watchdog & Auto-Healing:** Termux service runner (`tools/bot_ctl.sh`, `tools/service_runner.sh`) and POSIX `tools/job_watchdog.sh` check multi-account heartbeats, rotate logs in place, and restart hung bots.
- **Non-Root Docker Container:** Containerized on `python:3.12-slim` running as unprivileged user `wvbot` with `STATE_DIR=/data`.

---

## Quickstart

### 1. First Login & Token Retrieval
Wolvesville sign-in requires a browser session (with interactive Turnstile attestation if challenged). Retrieve your durable `refreshToken` once via the Node helper:

```bash
cd web
npm install
npm run login             # Log in once via visible browser window (saves state/tokens.json)
```
Or store an existing token directly from the repo root:

```bash
python setcreds.py
```

### 2. Run Locally (`run.py` / `python -m wvbot`)

```bash
python run.py                     # Auto-detects accounts.json farm vs single-account protocol mode
python run.py --mode farm         # Force farm mode
python run.py --mode protocol     # Force single-account protocol mode
```

### 3. Run with Docker Compose

```bash
docker compose up -d
```
The dashboard listens on `http://127.0.0.1:8787/` (health probe at `/health`). Persistent state lives in `./data` mounted to `/data` (`STATE_DIR=/data`).

### 4. Run on Android Phone (Termux)

```bash
# Package and deploy to connected ADB device
python tools/deploy.py

# Check live status & forward dashboard port
python tools/status.py --events
```
Open `http://127.0.0.1:8787/` in your laptop browser after `adb forward tcp:8787 tcp:8787`.

---

## Managing Accounts (Hot-Roster)

### Via Web Dashboard UI
1. Open `http://127.0.0.1:8787/`.
2. In the **Farm Roster & Accounts** panel, click `+ Add Bot`.
3. Enter an account name and paste the `refreshToken` (pass your dashboard token if `dashboard_token.txt` / `WV_BOT_DASHBOARD_TOKEN` is active).

### Via CLI (`tools/roster.py`)
```bash
# List all configured accounts and readiness
python tools/roster.py list

# Add a new account (hot-registers via HTTP API if running, atomic disk fallback if offline)
python tools/roster.py add bot2 --refresh-token "<refresh_token>"

# Remove an account
python tools/roster.py remove bot2

# Check overall farm status
python tools/roster.py status
```

### Via REST API
- `GET /health` — Lightweight liveness check (`{"ok": true}`).
- `GET /api/accounts` — Returns accounts list and token readiness (`token_present` / `ready`, credentials redacted).
- `POST /api/accounts` — Payload `{"name": "bot2", "refresh_token": "..."}` (or `{"action": "remove", "name": "bot2"}`).
- `DELETE /api/accounts/bot2` — Stops slot cleanly and removes from roster.

---

## Testing & Quality Assurance

The test suite relies exclusively on the Python standard library:

```bash
python -B -m unittest discover -s tests -v
python tools/package.py --check
python -m compileall -q wvbot tools run.py setcreds.py
```

For the optional Node/Playwright companion in `web/`:

```bash
cd web && npm run check
```

---

## Configuration Reference (`config.json`)

| Key | Default | Description |
|---|---|---|
| `room_names` | `["vill win"]` | Substrings matched against open custom rooms |
| `vill_win_mode` | `true` | Prefer villager/solo kills and rapid "vill win" ending tactics |
| `wolf_self_claim` | `true` | Announce wolf role in quick-claim rooms |
| `instant_kill` | `true` | Enable fast killer/hunter shots when appropriate |
| `prefer_8p_rooms` | `true` | Prioritize rooms near the 8-player start threshold |
| `strict_8p_only` | `false` | Require 8-player lobbies exclusively when true |
| `strict_tf_only` | `true` | Restrict joins strictly to TF-paced custom rooms |
| `stay_in_room` | `true` | Stay in fast rooms for consecutive rematches |
| `fast_reconnect_enabled` | `true` | Automatically rejoin mid-match socket drops |
| `reflex_chat_enabled` | `true` | Enable 0.1 ms deterministic chat reflex replies |
| `bio_break_enabled` | `true` | Simulate periodic human breaks between streaks |
| `delay_min_sec` / `delay_max_sec` | `45.0` / `150.0` | Random delay range between consecutive matches |
| `joins_per_hour` | `12` | Rolling join rate limit before applying backoff |
| `room_skip_chance` | `0.15` | Probability to skip a matching room |
| `silence_timeout_sec` | `300.0` | Timeout before resetting a silent game socket |
| `claim_daily_rewards` | `true` | Automatically claim daily ladder rewards between matches |
| `open_loot_boxes` | `true` | Automatically open accumulated loot boxes |
| `spin_golden_wheel` | `false` | Spin golden wheel when cost <= `wheel_max_roses` |
| `dashboard_host` | `"127.0.0.1"` | Bind address for HTTP dashboard and API server |
| `dashboard_port` | `8787` | Port for HTTP dashboard and API server |
| `dashboard_token` | `""` | Optional shared bearer token (auto-generated on non-loopback binds) |

---

## License

This project is licensed under the [MIT License](LICENSE).
