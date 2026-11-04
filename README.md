# Wolvesville bot

A bot that plays Wolvesville custom games ("vill win" rooms) on its own,
24/7, headless - and a dashboard that shows what it is doing.

The idea: the **browser** does what only a browser can do (Cloudflare,
the Turnstile widget, the login), and a small injected script does the
**game** over socket.io.  No clicking, no screen, no coordinates.

```
┌──────────────┐   socket.io    ┌──────────────────────┐
│ injected     │───────────────▶│ game.api-wolvesville │
│ payload.js   │◀───────────────│                      │
└──────┬───────┘                └──────────────────────┘
       │ state / events
       ▼
┌──────────────┐                ┌──────────────────────┐
│ supervisor   │                │ dashboard :8787      │
│ (python/ node)│───────────────▶│ (laptop browser)     │
└──────────────┘                └──────────────────────┘
```

## Two hosts, one payload

| Host | Runner | Use it for |
|---|---|---|
| **Android (mini PC)** | `wvbot/webbot.py` - Termux + headless Firefox + Marionette | the 24/7 bot, screen off, phone plugged in |
| **Laptop / Docker** | `web/bot.mjs` - Playwright + Chromium | development, debugging, or a server of its own |

Both inject the same `web/payload.js`, so the behaviour is identical.

## Run it on the phone (the mini PC)

```bash
# on the phone, inside Termux
python3 ~/wv-bot/setcreds.py     # once: store email + password, restart the bot
python3 ~/wv-bot/run.py           # the bot (web mode, the default)
```

* The bot starts itself after every reboot (`Termux:Boot`) and after every
  Termux session start, and it keeps the CPU awake (`termux-wake-lock`).
* The dashboard is on port 8787 of the phone.  From the laptop:

```bash
adb forward tcp:8787 tcp:8787     # then open http://127.0.0.1:8787/
```

or open `http://<phone-ip>:8787/` directly on the same Wi-Fi.

## Check what it is doing

```bash
python wv-bot/tools/status.py --events
```

Prints status, phase, room, role, XP, restarts, errors, the processes on
the phone and the last events.  The same numbers are on the dashboard.

| Field | Meaning |
|---|---|
| `status` | `waiting for login` -> `looking for room` -> `joining` -> `playing` -> `game-over` -> `reconnecting` |
| `errors` / `reconnects` | both should stay flat while things are fine |
| `uptime_sec` | how long the current session has been running |
| `xp_total` / `games_played` | the harvest |

## Run it on the laptop

```bash
cd web
npm install && npx playwright install chromium
node bot.mjs --login     # log in once in a visible browser
node bot.mjs             # run headless
```

Docker: see `web/Dockerfile` (the payload and the config are the same).

## Layout

```
wv-bot/
  run.py            launcher for the phone (web mode by default)
  setcreds.py       stores the account credentials on the device (mode 600)
  config.json       template (the real one lives in ~/.wv-bot/)
  wvbot/            python side: supervisor, marionette, firefox, dashboard, legacy protocol bot
  web/              node side: playwright runner + the shared payload
  tools/            packaging, probes and the status helper
  docs/RE-NOTES.md  everything that was learned about the protocol
```

## Legacy: the pure protocol bot

`python3 run.py --mode protocol` runs the original socket.io client that
speaks to the API directly.  It needs a valid `refreshToken` and a
`Cf-JWT` for the first sign-in, which is why the browser is the primary
path.  It is kept as a fallback for a machine that already has a token.
