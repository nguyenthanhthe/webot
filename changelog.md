# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [semantic versioning](https://semver.org/).

## [Unreleased]

Planned work, see `docs/FEATURE-PLAN.md` for the full list.

## [1.0.2] - 2026-11-07

Multi-Account Hot-Roster REST API and Real-Time Accounts UI.

### Added

- **Multi-Account Hot-Roster REST API**:
  - `GET /api/accounts`: Secure enumeration of accounts with token readiness probe (`ready: bool`, `has_token: bool`) without exposing credentials.
  - `POST /api/accounts`: Dynamic hot-registration of accounts, auto-creating directory structures and hot-launching bot slots into running Farm.
  - `DELETE /api/accounts/<name>`: Graceful bot thread termination, slot release, and roster removal.
  - Safe Roster Cleanup: When removing the last account, unlinks `accounts.json` cleanly restoring single-account fallback without triggering `RosterError`.
- **Interactive Farm Roster Dashboard UI**:
  - Real-time accounts manager with status badges (`Ready`, `Missing Token`, `Running`, `Inactive`).
  - Modal form for adding bot accounts directly from browser.
  - 1-click account removal with instant farm refresh.
- **Concurrency & Thread-Safety Hardening**:
  - All Farm slot iterations utilize `list(self._slots)` to eliminate race conditions during hot-adds and removals.

## [1.0.1] - 2026-11-07

Network resilience, protocol serialization fix, and multi-mode watchdog.

### Added

- **Network Blip Fast Reconnect**:
  - Automatically handles unexpected mid-game socket disconnects via `fast_reconnect_max_attempts` and exponential backoff.
  - Emits `mg:join-open-game` with original `miniGameId`, syncing game state via `game-reconnect-set-players` without forfeiting match or accumulating leave penalties.
- **Continuous Heartbeat in Match Loop**:
  - Calls `_touch_heartbeat()` every 30s during gameplay to prevent watchdog false positives on long matches.
- **Watchdog Drill Tool (`tools/drill_watchdog.py`)**:
  - Inspects heartbeat age, service runner lock, and log rotation via Termux environment.

### Fixed

- **Chat Payload Wire Format**:
  - Serializes `chat()` payload as JSON string (`json.dumps({"msg": message})`) conforming to official Wolvesville socket protocol.
- **Service Watchdog Multi-Mode Pattern**:
  - Updated regex/grep patterns in `job_watchdog.sh`, `service_runner.sh`, and `bot_ctl.sh` to match `farm`, `protocol`, and `-m wvbot` modes uniformly.

## [1.0.0] - 2026-11-07

The Enterprise release: from a single bot to a multi-account autonomous farm.

### Added

- **Multi-Account Farm Supervisor (`wvbot.farm`)**: Runs and supervises
  isolated bot threads from `accounts.json`, with per-account state directories,
  automatic crash recovery, and thread isolation.
- **Enterprise Dashboard v2 with Real-Time SSE**: `/api/stream` pushes live status
  and events via Server-Sent Events without polling; multi-bot overview grid
  with individual status pills, cards, and per-bot control actions.
- **Roster Management CLI (`tools/roster.py`)**: 1-click CLI to list, add,
  remove, and inspect accounts in `accounts.json`.
- **Per-Account State Isolation**: `state_path(name, base=...)` and
  `load_config(base=...)` allow isolated credentials and state directories.
- **Docker & Docker Compose**: Fully headless, containerized runtime with
  healthcheck for servers or cloud VMs.
- **GitHub Actions CI**: Automated test matrix across Python versions.

## [0.0.6] - 2026-11-07

Token renewal and credential resilience hardening.

### Fixed

- **Token expiry during idle search**: `AuthManager.auth_headers()` now
  automatically detects when a token expires or is within 300s of expiry
  and refreshes it via `ensure_token()`, preventing HTTP 401 loops when
  waiting for rooms over extended periods.
- **Dynamic auth headers in `GameApi`**: `GameApi.auth_headers` is now a
  property supporting callable headers so API requests always query fresh
  tokens dynamically.
- **HTTP 401 auto-recovery**: `bot.session()` ensures a valid token before
  scanning rooms, and the main supervisor loop forces credential refresh
  (`ensure_token(force=True)`) upon encountering an HTTP 401 error.

## [0.0.5] - 2026-11-05

The 24/7 hardening release: every way the run could die now has a
recovery behind it, and the farming got smarter.

### Added

- **The room choice prefers the richer rooms** (`regularXp`), still
  random within the top pool.
- **A nightly self-restart** at `daily_restart_hour` via `os.execv`;
  the restart day and every other "already done today" marker live in
  the state dir, because the exec wipes the memory.
- **A heartbeat for the outside watchdog**: the run loop touches
  `heartbeat.txt`; a bot that holds the lock but stopped beating for
  over two hours is killed by the JobScheduler watchdog and started
  fresh.  The watchdog also **rotates `wv-bot.log` in place** past
  5 MB.
- **`/api/rooms` on the dashboard**: the raw room list of any region
  (optionally the asia host), fetched with the bot's own attestation -
  the gateway the region survey runs through.  The survey found: the
  vill-win rooms live in the EN set, the night lull is real scarcity,
  and `regularXp` is a boolean flag.
- **The daily reward waits for its date** and is attempted **once per
  day**; a refused claim logs the server's answer instead of a fake
  success.
- **An optional greeting** (off by default) and the running version
  in `tools/status.py`.
- Daemon threads that die are logged (`threading.excepthook`).

### Fixed

- **The vote could land on a dead player**, the room could make the
  bot **vote for itself**, and `reset()` leaked the own player id.
- **Fragmented websocket frames** were parsed as separate messages -
  big game events arrived truncated and the bot played blind.
- **A dead game socket** hung the session until the silence watchdog;
  the reader dispatches "disconnect" now and a bogus frame length is
  refused.
- **The service stop killed the bot with SIGKILL** - every restart
  looked like fleeing the village.  SIGTERM first, 12 s grace.
- **Every game action hesitated on the reader thread**, blocking the
  engine pings; actions run in queue workers now.
- **A chatty handshake** (pings before the namespace ack) exhausted
  the 10-packet budget and failed the connect.
- The installer no longer overwrites a **rotated refresh token**;
  a rotated token is persisted the moment it arrives.
- A failing **nightly exec** is logged and the loop carries on.
- `tools/status.py` showed 1970 event times; the store keeps seconds.

## [0.0.4] - 2026-11-05

The farming is smarter and the 24/7 run survives its own restarts.

### Added

- The **room choice prefers the richer rooms**: among the matches only
  the ones within 80% of the best `regularXp` are considered, and the
  pick stays random between them.
- A **nightly self-restart**: at `daily_restart_hour` (default 4 local)
  the process re-executes itself between two games.  `os.execv` keeps
  the pid and the lock valid, the service and the watchdog never
  notice, and the fresh process authenticates from the persisted
  refresh token.
- The **daily reward waits for its date**: a rung whose
  `canBeClaimedDate` has not arrived is left alone instead of being
  posted again every loop (found with `tools/probe_daily.py`).
- An **optional greeting**: with `chat_messages` filled and
  `chat_enabled` on, the bot says one of the lines with a 30% chance
  after a human pause when the game starts.  Off by default.
- `tools/status.py` prints the **running version** next to the status.
- `tools/deploy.py` stages the runner, the control script and the
  phone's own installer, so **one command updates everything** on the
  device.

### Fixed

- A **dead player** could receive the day vote - the accusation counts
  did not skip dead players, the vote would have been wasted.
- The room could make the bot **vote for itself**; the counts leave the
  bot's own id out now.
- `reset()` kept the **own player id** of the last game.
- A `gridIdx` of **0** (the first slot) was read as -1.
- A **dead game socket** left the session hanging until the silence
  watchdog (300 s): the reader thread swallowed packet errors and the
  "disconnect" handler was dead code.  It dispatches now, and a broken
  packet ends the session the same way.
- The websocket layer handed every **continuation frame** to the parser
  as its own message - a game event the server split into several
  frames became truncated JSON and the bot played blind.  Fragments
  are joined now; a bogus frame length is refused instead of being
  allocated.
- The service runner killed the bot with **SIGKILL on the stop flag** -
  the bot never got to announce `mg:exit-game`, so every service
  restart looked like fleeing the village.  SIGTERM first, 12 s of
  grace, SIGKILL only as the last resort.  `bot_ctl.sh` waits up to
  20 s for the lock.
- The installer **overwrote a rotated refresh token** with the stale
  home copy; the state file wins once it exists.
- A refused **daily reward claim** was logged as a success; the log
  carries the server's answer now.
- The dashboard **phase card** stayed on "started" after the first
  phase change; it follows the night and the day vote now.

### Known issues

- The **daily reward ladder** reported an open rung whose claim POST
  came back empty; the claim now waits for its `canBeClaimedDate`,
  which made the retries stop.

## [0.0.3] - 2026-11-04

The plan from `docs/FEATURE-PLAN.md` is implemented; the account plays
like a cautious human and the dashboard shows the value.

### Added

- **Rewards.**  The daily reward is claimed between two games, the loot
  boxes in the inventory are opened, and the golden wheel spins when the
  config allows the roses.  After the award the bot also claims the
  double XP when the server offers it.
- **Player levels.**  The lobby delivers the level of every player with
  the equipped-items events, so the dashboard lists the room with each
  player's level and marks the bot's own row.
- **XP projection.**  The dashboard shows the XP per hour and how long
  the next level takes, from the session rate and the server's own
  "xp until next level" figure.
- **Patch watcher.**  The build number and the client bundle hash are
  checked now and then; a game patch is logged instead of silently
  breaking the bot.
- **The bot knows itself.**  The own player id is learned from the
  profile, a rename of the account is followed, and a ban end time from
  the profile is waited out before the bot touches the server again.
- **Skip a room now and then.**  A matching room is left alone this
  round with a small chance (default 15%); the bot looks again after
  the normal wait.  A skipped room does not count towards the hourly
  join limit.
- **A test suite.**  94 stdlib tests (`python -m unittest discover -s
  tests` from `wv-bot/`) cover the payload parsing, the XP tables, the
  game events, the policy, the history, the pacing and the config.

### Fixed

- A **dead player** could receive the day vote - the accusation counts
  did not skip dead players, the vote would have been wasted.
- The room could make the bot **vote for itself**; the counts leave the
  bot's own id out now.
- `reset()` kept the **own player id** of the last game.
- A `gridIdx` of **0** (the first slot) was read as -1.
- The stop or restart of the service **dropped the game socket
  silently** - exactly the fleeing-the-village behaviour the server
  bans for.  The play loop now ends on the request and announces
  `mg:exit-game` before the socket closes.
- A refused **daily reward claim** was logged as a success; the log
  carries the server's answer now.
- A **dead game socket** left the session hanging until the silence
  watchdog (300 s): the reader thread swallowed packet errors and the
  "disconnect" handler was dead code.  It dispatches now, and a broken
  packet ends the session the same way.
- The websocket layer handed every **continuation frame** to the parser
  as its own message - a game event the server split into several
  frames became truncated JSON and the bot played blind.  Fragments
  are joined now; a bogus frame length is refused instead of being
  allocated.
- The service runner killed the bot with **SIGKILL on the stop flag** -
  the bot never got to announce `mg:exit-game`, so every service
  restart looked like fleeing the village.  SIGTERM first, 12 s of
  grace, SIGKILL only as the last resort.  `bot_ctl.sh` waits up to
  20 s for the lock.
- The installer **overwrote a rotated refresh token** with the stale
  home copy; the state file wins once it exists.
- The dashboard **phase card** stayed on "started" after the first
  phase change; it follows the night and the day vote now.

### Changed

- `tools/deploy.py` replaces the hand-typed adb chain: one command
  packages, pushes, unpacks (run-as), swaps the directory, restarts
  the service and verifies the version on the dashboard.

### Known issues

- The **daily reward ladder** reports an open rung ("TALISMAN") whose
  claim POST comes back with an empty body and no effect - the rung
  stays open and the bot retries it every loop.  Under investigation
  with `tools/probe_daily.py`; until then the log line is the honest
  "server answered nothing" instead of a fake success.

## [0.0.2] - 2026-11-04

The bot plays a real game end to end and runs as a background service.

### Added

- **The bot plays a real game.**  The custom game is joined on the lobby
  socket with `mg:join-open-game`; the server then sends the whole game
  - `game-started`, the votes and the award - on that socket.  Verified
  live: the bot was listed in the player list with its own role, voted
  and received the XP award at the end of the game.
- **Runs as an Android service.**  The bot is started through the Termux
  background service (`com.termux/.app.RunCommandService`) instead of a
  terminal session, so closing every Termux tab does not stop it.
  - `~/bot_ctl.sh start|stop|restart|status` controls the service.
  - `~/.termux/boot/wvbot` starts it again after a reboot.
  - A JobScheduler watchdog (`termux-job-scheduler`, every 15 minutes)
    restarts the bot if the system killed the Termux app.
  - The Termux packages are whitelisted from doze, and the runner takes
    a wake lock so the CPU does not sleep mid-game.
- A lock file makes sure only one bot runs; the runner cleans up
  leftovers of an earlier start.
- `tools/probe_rooms.py` lists the open rooms with their password flag
  and shows which ones the bot would join.
- `tools/probe_join.py` joins one room and checks whether the bot
  really shows up in the player list.

### Fixed

- The bot acts **once per phase**.  The server repeats the day vote for
  every player that votes, so the killer role shot its victim fourteen
  times in half a minute.  The same happened to the werewolf vote, the
  protection and the day vote.
- Leaving a room is announced with `mg:exit-game`.  A socket that
  simply disappears counts as "fleeing the village" and the server bans
  the account for it.
- A server error (`error-*`) ends the game session right away instead of
  waiting for the silence timeout.  The bot used to sit in a dead game
  for five minutes.
- The room matching is word based, so "vill win" also matches
  "village win" and "24HR VILL WIN NT (E)".  Rooms with a password are
  skipped by default; the server refuses the join without it.
- The socket query carries the firebase token and the random nonce the
  official client sends.
- The room list is read again for every session, so a room that closed
  in the meantime is not joined.

### Changed

- The game is played on the lobby socket.  A separate game socket with
  the room id in the handshake always answered
  `error-game-connection-lost`.

## [0.0.1] - 2026-11-04

The first version: the bot runs headless on the phone.

### Added

- A pure Python bot (`wvbot`) that runs on the phone without a browser:
  it mints a firebase token from a refresh token, reads the room list
  over REST and plays the game over socket.io.
- The login chain: a browser is only needed once, to log in by hand.
  The refresh token that comes out of it is stored in `~/.wv-bot` and
  lets the bot mint id tokens forever.
- A status dashboard on port 8787, readable from the laptop.
- A supervisor loop: the bot rejoins a room when it is kicked, and it
  backs off on its own when the server bans the account.
- Human-like pacing: the wait between games is drawn from a range and
  the bot limits itself to a number of joins per hour.
- A watchdog that restarts a game that goes quiet.

### Known issues

- The bot never got past the handshake: the server closed every game
  socket with `error-game-connection-lost`.  Fixed in 0.0.2.
