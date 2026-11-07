# Wolvesville Enterprise Bot Farm (v1.0.2)

An autonomous, multi-account, headless farming cluster for Wolvesville custom games ("vill win" rooms) with 24/7 reliability, real-time SSE dashboard, and hot-roster management.

The bot communicates directly with Wolvesville servers over REST and Socket.IO. It mints its own Firebase tokens from stored refresh tokens, queries room lobbies, and plays matches autonomously with randomized human-like pacing.

**100% Python Standard Library** — zero pip dependencies, zero browser overhead in protocol mode. Runs seamlessly inside Termux on an Android device or inside a minimal Docker container.

```
┌───────────────────────────────── Termux / Docker ─────────────────────────────────┐
│                                                                                   │
│  Farm Supervisor (wvbot.farm)                                                     │
│   ├── accounts.json          Roster configuration (mode 600)                      │
│   ├── Bot #1 (thread) ────┐                                                      │
│   ├── Bot #2 (thread) ────┼── Independent state dirs (~/.wv-bot/accounts/<name>/) │
│   └── Bot #N (thread) ────┘   Independent sockets, pacing, and crash backoffs      │
│                                                                                   │
│  Live Dashboard & REST API (:8787)                                                │
│   ├── GET  /              Live Web UI (SSE Stream + Roster Manager + Bot Grid)    │
│   ├── GET  /api/status    Active bot state snapshot                              │
│   ├── GET  /api/farm      All bot instances snapshot                              │
│   ├── GET  /api/stream    Server-Sent Events (SSE) live push                      │
│   ├── GET  /api/accounts  Sanitized account enumeration & readiness probe         │
│   ├── POST /api/accounts  Hot-add bot account & launch slot dynamically           │
│   └── DEL  /api/accounts  Graceful bot slot termination & roster cleanup          │
└─────────────────────────────────────────┬─────────────────────────────────────────┘
                                          │
                  ┌───────────────────────┴───────────────────────┐
                  ▼                                               ▼
     ┌────────────────────────┐                      ┌────────────────────────┐
     │ game.api-wolvesville   │                      │ Laptop / Phone Browser │
     │ (REST + Socket.IO)     │                      │ (Tailscale / Port 8787)│
     └────────────────────────┘                      └────────────────────────┘
```

---

## Key Features

- **Multi-Account Autonomous Farm:** Run multiple bot accounts in isolated threads with dedicated state directories, independent pacing, and individual ban cooldowns.
- **Hot-Roster Management:** Dynamically add or remove bot accounts via Web Dashboard UI, REST API (`/api/accounts`), or CLI (`tools/roster.py`) without restarting the cluster.
- **Fast Network Reconnect:** Automatically recovers from socket drops mid-game without abandoning the match or accumulating leave countdown penalties.
- **Human Pacing & Ban Avoidance:** Random day/night delays (45–150s), room skip probabilities, and rolling join rate limiters.
- **Real-Time Live Dashboard:** Built-in web UI served on `:8787` with real-time SSE updates, per-bot status cards, room player levels, event stream, and 1-click reward claims (Daily Rewards, Lootboxes, Golden Wheel).
- **Process Watchdog & Auto-Healing:** Background cron + service runner checks heartbeat age, locks process PID, rotates logs in place, and revives failed bots automatically.
- **Zero-Dependency Docker Container:** Containerized with `python:3.12-slim` and built-in healthchecks.

---

## Quickstart

### 1. First Login & Token Retrieval
Wolvesville sign-in requires an initial Cloudflare attestation. Retrieve your durable `refreshToken` once using the local helper:

```bash
cd web
npm install
node login.mjs            # Log in once via visible browser window
```
The script writes the token to `web/state/` and displays it.

### 2. Run with Docker Compose

```bash
docker compose up -d
```
The dashboard is immediately accessible at `http://localhost:8787/`.

### 3. Run on Android Phone (Termux Mini-PC)

```bash
# Deploy package to device
python wv-bot/tools/deploy.py

# Forward dashboard port to laptop
adb forward tcp:8787 tcp:8787
```
Open `http://127.0.0.1:8787/` on your laptop browser.

---

## Managing Accounts (Hot-Roster)

### Via Web Dashboard UI
1. Open `http://127.0.0.1:8787/`.
2. In the **📋 Farm Roster & Accounts** section, click `+ Add Bot`.
3. Enter an account name and paste the `refreshToken`.
4. The bot will automatically initialize and join the active farm cluster.

### Via CLI (`tools/roster.py`)
```bash
# List all configured accounts and readiness
python wv-bot/tools/roster.py list

# Add a new account
python wv-bot/tools/roster.py add bot2 "<refresh_token>"

# Remove an account
python wv-bot/tools/roster.py remove bot2

# Check overall farm status
python wv-bot/tools/roster.py status
```

### Via REST API
- `GET /api/accounts` — Returns accounts list and readiness status (`ready: true`, credentials redacted).
- `POST /api/accounts` — Payload `{"name": "bot2", "refresh_token": "..."}`.
- `DELETE /api/accounts/bot2` — Gracefully stops slot and removes from roster.

---

## Testing & Quality Assurance

The test suite relies exclusively on the Python standard library:

```bash
cd wv-bot
python -m unittest discover -s tests -v
```
All 191+ test cases cover protocol serialization, auth lifecycle, game policy, fast reconnect, watchdog monitoring, and multi-account farm supervision.

---

## Configuration Reference (`config.json`)

| Key | Default | Description |
|---|---|---|
| `room_names` | `["vill win"]` | Substrings matched against open custom rooms |
| `delay_min_sec` / `delay_max_sec` | 45 / 150 | Random delay range between consecutive matches |
| `joins_per_hour` | 12 | Rolling join rate limit before applying backoff |
| `room_skip_chance` | 0.15 | Probability to skip a matching room to mimic human hesitation |
| `silence_timeout_sec` | 300 | Timeout before resetting a silent/hung game socket |
| `fast_reconnect_enabled` | true | Enable automatic fast rejoin on mid-match socket drops |
| `claim_daily_rewards` | true | Automatically claim daily ladder rewards between matches |
| `open_loot_boxes` | true | Automatically open accumulated loot boxes |
| `spin_golden_wheel` | false | Spin golden wheel when cost <= `wheel_max_roses` |
| `dashboard_port` | 8787 | Port for the HTTP dashboard and API server |

---

## License

This project is licensed under the [MIT License](LICENSE).
