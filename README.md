# Wolvesville bot

A bot that plays Wolvesville custom games ("vill win" rooms) on its own,
24/7, headless - and a dashboard that shows what it is doing.

The bot speaks to the game server **directly**: it mints its own firebase
token from a refresh token, lists the open rooms over REST and plays over
socket.io.  No browser, no screen, no clicking - just the Python standard
library, so it runs happily inside Termux on a phone.

```
┌──────────────┐   REST + socket.io   ┌──────────────────────┐
│ wvbot        │─────────────────────▶│ game.api-wolvesville │
│ (python)     │◀─────────────────────│                      │
└──────┬───────┘                      └──────────────────────┘
       │ state / events
       ▼
┌──────────────┐                      ┌──────────────────────┐
│ dashboard    │─────────────────────▶│ laptop browser       │
│ :8787        │                      │ (adb forward)        │
└──────────────┘                      └──────────────────────┘
```

## The one thing that needs a browser

Only the **first login**.  The sign-in endpoint wants a Cloudflare
attestation that only a real browser can produce.  Once you have logged
in once, the `refreshToken` is the durable credential: it mints new
idTokens for as long as it lives, and nothing else needs a browser.

```bash
cd web
npm install
node login.mjs            # opens a visible browser - you log in there
```

`login.mjs` stores the session in `web/state/` and prints the refresh
token.  Copy it to the phone (see below); from then on the phone runs
completely on its own.

## Run it on the phone (the mini PC)

The bot runs as an **Android background service**, not as a Termux tab:
closing every terminal window leaves it running.

```bash
# on the phone, inside Termux
sh ~/deploy.sh             # unpack the package, store the refresh token
sh ~/bot_ctl.sh start      # start the service
```

| Command | What it does |
|---|---|
| `sh ~/bot_ctl.sh start` | start the bot service |
| `sh ~/bot_ctl.sh stop` | stop it |
| `sh ~/bot_ctl.sh restart` | stop and start again |
| `sh ~/bot_ctl.sh status` | is it running, and what is in the log |

* The service is the Termux background service
  (`com.termux/.app.RunCommandService`), so it outlives every session.
* `~/.termux/boot/wvbot` starts it again after a reboot (`Termux:Boot`).
* A JobScheduler watchdog restarts it every 15 minutes if the system
  killed the Termux app (`termux-job-scheduler`).
* The bot keeps the CPU awake (`termux-wake-lock`) and the Termux
  packages are whitelisted from doze.
* The dashboard is on port 8787 of the phone.  From the laptop:

```bash
adb forward tcp:8787 tcp:8787     # then open http://127.0.0.1:8787/
```

or open `http://<phone-ip>:8787/` directly on the same Wi-Fi.

## Check what it is doing

```bash
python tools/status.py --events
```

Prints status, phase, room, role, XP, restarts, errors, the processes on
the phone and the last events.  The same numbers are on the dashboard.

| Field | Meaning |
|---|---|
| `status` | `waiting for login` -> `looking for room` -> `joining` -> `playing` -> `game-over` -> `reconnecting` |
| `errors` / `reconnects` | both should stay flat while things are fine |
| `uptime_sec` | how long the current session has been running |
| `xp_total` / `games_played` | the harvest |
| `banned` | the server banned the account; the bot waits the ban out |

## The other host: a browser

The original design drove the official web client in a headless Firefox
and injected a script into the page.  That still exists and is useful
when the protocol mode runs into a Cloudflare challenge:

```bash
cd web
npm install && npx playwright install chromium
node bot.mjs --login     # log in once in a visible browser
node bot.mjs             # run headless
node bot.mjs --headed    # run with a visible browser (debugging)
```

Both runners inject the same `web/payload.js`, so the behaviour is
identical.

## Layout

```
run.py            launcher for the phone (protocol mode by default)
setcreds.py       stores the account credentials on the device (mode 600)
config.json       template (the real one lives in ~/.wv-bot/)
wvbot/            python side: supervisor, socket.io client, api, dashboard
web/              node side: playwright runner + the shared payload
tools/            packaging, deployment, the service scripts and the probes
tests/            stdlib unittest suite, run from the root (see below)
docs/RE-NOTES.md  everything that was learned about the protocol
changelog.md      what changed in which version
```

## The rooms

The bot joins the custom games whose name matches `room_names`
(`"vill win"` by default).  The matching is word based, so it also
covers "village win" and "24HR VILL WIN NT (E)".  Rooms with a password
are skipped - the server refuses the join without it.  Set
`allow_password_rooms` and `room_password` in the config to try them
anyway.

## Development

The bot is standard library only, and so are its tests:

```bash
python -m unittest discover -s tests -v
```

Every protocol fix lands with a test first; the suite covers the payload
parsing, the XP tables, the game events, the vote policy, the history,
the pacing and the config load order.

## The two modes

| Mode | Command | Needs |
|---|---|---|
| protocol | `python3 run.py` (default) | a refresh token |
| web | `python3 run.py --mode web` | a browser (headless Firefox on the phone) |

The protocol mode is the default because it needs nothing but the Python
standard library.
