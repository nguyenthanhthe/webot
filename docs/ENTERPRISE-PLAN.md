# Enterprise plan - from one bot to a farm product

The operator's ask (2026-11-05): a headless server farm with many
bots, a real-time dashboard that monitors every bot at once, an easy
login (email + password, or the Wolvesville way), and a GitHub
presence good enough to call the whole thing a product.  This document
is the blueprint; the phases below land one wave at a time.

## What was verified before writing this (spikes, 2026-11-05)

* **SSE works on the stdlib stack**: a `ThreadingHTTPServer` handler
  that streams `text/event-stream` chunks delivered three events to a
  plain client.  Real-time push needs no websocket server and no
  dependency.
* **The phone has room for a farm**: 3.9 GB total, 1.6 GB available;
  a bot process costs roughly 40-60 MB.  Four to six accounts fit on
  the A03s with the current single-process design.
* **The state layer is the one real coupling**: 14 call sites go
  through the module-global `STATE_DIR`.  Per-account state is a
  contained refactor: every call becomes `state_path(name, base=...)`
  with the farm passing each account's directory.

## Architecture

```
┌─────────────────────────── one Termux service ───────────────────┐
│                                                                  │
│  Farm (supervisor thread)                                        │
│   ├─ accounts.json          the roster: name, credentials, config│
│   ├─ Bot #1 (thread)  ──┐                                       │
│   ├─ Bot #2 (thread)  ──┤  each: own state dir, own store,      │
│   ├─ Bot #3 (thread)  ──┘        own pacing, own ban handling   │
│   └─ FarmStore            aggregated snapshot for the dashboard  │
│                                                                  │
│  Dashboard v2 (:8787)                                            │
│   ├─ GET  /              farm overview (grid of bot cards)       │
│   ├─ GET  /bot/<name>    per-bot detail (levels, events)         │
│   ├─ GET  /api/farm      one JSON snapshot of every bot          │
│   ├─ GET  /api/stream    SSE: status + events pushed live        │
│   ├─ POST /api/accounts  add an account (see "Login")            │
│   └─ token auth          every request carries a shared token    │
└──────────────────────────────────────────────────────────────────┘
```

Decisions and why:

* **Threads, not processes.** One service keeps the wake lock, the
  boot hook and the watchdog machinery that already work.  A hung bot
  is contained: its thread dies with a log, the farm restarts it, the
  heartbeat mechanism covers the whole farm.
* **Per-account state dirs** (`~/.wv-bot/accounts/<name>/`): refresh
  token, config, history, markers - nothing is shared, so one banned
  account never touches another.  `accounts.json` itself lives in the
  farm root with mode 600.
* **Backwards compatible**: one account in `accounts.json` behaves
  exactly like today's bot; the old `~/.wv-bot` state is migrated on
  first start.
* **The farm restarts a dead bot thread with the same backoff the
  supervisor already uses.**  A bot that crashes three times in a row
  is parked as "failed" on the dashboard instead of burning joins.

## Dashboard v2 - real-time and readable at a glance

* **SSE, not polling**: `/api/stream` pushes a `status` frame per bot
  every 5 s and every event as it happens; the page reconnects by
  itself.  Polling `/api/farm` stays as the fallback.
* **Vanilla JS/CSS only** - the dashboard must work over `adb forward`
  with no internet, so no CDN, no framework, no build step.  The UI
  vocabulary: dark cards, status pills (playing / hunting / banned /
  paused / failed), an XP sparkline per bot (inline SVG from the
  history), the room and role inline, the event stream color-coded by
  kind.
* **Farm overview first**: one card per bot, the farm totals on top
  (XP today across bots, games today, bots playing), each card links
  to its detail view.
* **Controls stay per bot**: pause, resume, restart; plus "claim
  rewards now".

## Login - two doors, both from the dashboard

1. **Paste a refresh token** (the Wolvesville way, works today): the
   account is live within seconds; the token is written to that
   account's state dir with mode 600.
2. **Email + password**: the sign-in endpoint demands a Cloudflare
   attestation.  The phone already runs headless Firefox through
   Cloudflare (Marionette, proven); the flow is: dashboard form ->
   the farm spins the one-shot helper -> `signInWithEmailAndPassword`
   with the fresh `Cf-JWT` -> refresh token persisted -> account
   joins the roster.  The laptop never needs a browser.

## GitHub - making the public repo a product

* **CI**: a GitHub Actions workflow that runs the unittest suite on
  every push (stdlib only - no install step, runs in seconds).
* **LICENSE** (MIT), a README with the architecture diagram, a
  quickstart, the config reference and screenshots of the dashboard.
* **Releases**: tag from the changelog (`0.0.5` was the last), GitHub
  Release notes generated from `changelog.md`.
* **The subtree split stays**: `android-device/wv-bot` is the source
  of truth; `webot` keeps receiving it.

## Security

* The dashboard **requires a token** once it serves more than one
  account: a random string in the farm root, sent as a query/header
  from the browser.  Today it binds 0.0.0.0 with no auth - acceptable
  for one account on a home LAN, not for a farm.
* Credentials at rest: mode 600 (already), never in the repo, never
  in the logs (the log lines show outcomes, not tokens).
* The http allowlist and the SSRF guards stay untouched.

## Risks

| Risk | Mitigation |
|---|---|
| RAM with 5+ bots | thread-per-bot is cheap; the farm caps at a configured `max_bots` (default 4) |
| N bots tripping the exit-countdown | pacing is per account and already random; the cap stays per bot |
| One account's ban punishing the farm | nothing is shared - separate sockets, separate state; the dashboard shows who is banned |
| Dashboard exposed on the LAN | token auth lands with phase 3, before multi-account goes live |
| The A03s rebooting under load | boot hook + watchdog already restart the whole farm |

## Phases

1. **Farm core** - `state_path(name, base=)`, `accounts.json`,
   `Farm` supervisor (start/stop/restart bots, aggregate store),
   single-account migration, tests for two bots with fake APIs.
2. **Dashboard v2** - SSE endpoint, farm overview page, detail page,
   sparklines; the old page retired.
3. **Accounts + auth** - the add-account endpoints (paste token
   first, then the headless-Firefox chain), dashboard token auth,
   farm totals.
4. **GitHub polish** - CI, LICENSE, README v2, screenshots, tag
   `v1.0.0` and the first GitHub Release.

Phases 1 and 2 are pure code on paths the test suite already covers;
phase 3's headless-Firefox chain is the one part that needs a live
window on the phone; phase 4 is repository work.

5. **Docker packaging** - the whole product as one image, landed after
   the phases above are done:

   * `Dockerfile`: `python:3.12-slim`, the `wvbot/` package copied in,
     `python -m wvbot` as the entrypoint.  The bot is stdlib only, so
     the image has **zero pip installs** - it builds in seconds and
     the attack surface is the Python runtime alone.
   * `docker-compose.yml`: the state directory as a named volume
     (`/data` -> the farm root: `accounts.json`, per-account state),
     `8787` published for the dashboard, `restart: unless-stopped`,
     and a **healthcheck** that curls `/api/farm` so the container
     supervisor restarts a wedged farm without phone-side watchdogs.
   * The config stays out of the image: everything durable is a
     volume, everything secret is mode-600 inside it.
   * Verification path: this laptop has no Docker; the image is built
     and smoke-tested by the GitHub Actions workflow (phase 4's CI)
     and by the operator's Docker machine.  The compose file carries
     the healthcheck so `docker compose up` is the whole manual.
