# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [semantic versioning](https://semver.org/).

## [Unreleased]

Planned work, see `docs/FEATURE-PLAN.md` for the full list.

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
