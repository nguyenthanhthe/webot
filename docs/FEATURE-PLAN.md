# Feature plan - what RXZBot does and what we build

RXZBot is a commercial Wolvesville XP farm.  Its landing page lists the
features it sells; the list below maps each of them onto what this bot
already has, what is missing, and how to build it.  The order follows
what actually protects the account, because the account is the thing
that makes the bot worth running.

Reference: <https://www.rxzbot.com/> (scraped with the local browser).

## What RXZBot claims

| Their feature | What it means in practice |
|---|---|
| Auto XP farming | joins rooms and plays them without input |
| Safe by design | random delays, natural behaviour, cooldowns |
| Live dashboard | see XP, session state and settings |
| Auto updates | follows the game after a patch |
| Auto username detection | the bot finds the account's name by itself |
| One-click rewards | spins the gold/rose wheels, opens loot boxes |
| See hidden levels | shows the level of every player in the room |
| Fully autonomous replay | detects a crash and rejoins by itself |
| Dual instance | two sessions at once for double XP |
| Free 2x XP boost | the premium XP doubler is included |
| Human behaviour | randomised timing so the pattern looks human |

## The plan

### 1. Human-like pacing - **highest value**

This is what keeps the account alive, and it is cheap to build.

* Randomise the delay between games: currently a flat 60 s, which is a
  fingerprint in itself.  Draw it from a range (45-150 s).
* Randomise the time spent in the day discussion before the vote.  The
  bot votes the moment the voting opens today.
* Randomise the night pause as well.
* Occasionally skip a room instead of always taking the first match.
* Add a small chance of leaving a game early (a human gets bored).

State: **done.**  The delay is drawn from 45-150 s, the day/night votes
wait a random 3-12 s, a matching room is skipped 15% of the time, and
the join count per hour backs off on its own.

### 2. Cooldowns and the ban

The server bans accounts that join too often (`LOBBY_EXIT_COUNTDOWN`).
We already wait the ban out, but the bot should also:

* read `bannedUntilTime` and sleep exactly until then,
* keep a rolling history of joins per hour and back off on its own,
* never touch the server while banned.

State: **done.**  The ban end time is read from the profile and the
error event, the bot sleeps until it lifts, and the rolling join
window slows itself down past 12 joins/hour.

### 3. Crash recovery - **partly done**

The supervisor restarts the session after any failure and the game
socket reconnects by itself (`reconnect: "true"`).  What is missing:

* the browser mode (playwright) does not restart the browser when the
  page dies - it does in the phone runner,
* a heartbeat that notices a silent hang (the socket stays open but the
  game is over).

State: **done.**  The phone runner supervises and a game that sends no
event for `silence_timeout_sec` is torn down and replaced.

### 4. Live dashboard - **mostly done**

The dashboard on port 8787 shows the status, the room, the role, the XP,
the error count and the event log.  What RXZBot has that we do not:

* an XP-per-hour figure and a projected level,
* a session log the user can scroll through,
* a small control panel (pause, resume, force a room change).

State: **done.**  The XP rate, the projection and the pause / resume /
restart controls are on the dashboard, next to the per-player levels.

### 5. Auto username detection

RXZBot finds the account's display name by itself.  We do not need the
name: the game id in the handshake identifies the session, and the
players arrive with their ids.  Nothing to build - but the bot should
still learn *its own* player id from `game-started` so it can recognise
itself in the player list.  State: **done** - the own id is learned
from the profile and from the role the server hands out.

### 6. One-click rewards

The gold wheel, the rose wheel and the loot boxes live in the core API
(`core.api-wolvesville.com`).  The bot could claim them on a schedule.
This is the most work of the list and the least urgent - it does not
increase the XP rate.  State: **done** - daily reward, loot boxes, the
golden wheel (roses capped in the config) and the double XP claim run
between two games.

### 7. See hidden levels

The player list in `game-started` does not carry levels, but the
lobby socket delivers `player-joined-and-equipped-items` with the level
of every player.  Joining the lobby socket alongside the game socket
would show the levels.  Nice for the dashboard, no effect on the XP.
State: **done** - the equipped-items events carry the levels and the
dashboard lists the whole room.

### 8. Dual instance

Two sessions of the same account in two rooms.  The server allows only
one session per player id, so this needs a second account.  Out of
scope.

### 9. Auto updates

Nothing to do: the protocol is re-implemented in this repository, so a
game patch means reading the new bundle and changing a few lines.  The
RE notes exist for exactly that.

## Order of work

1. ~~**Randomised pacing**~~ - done.
2. ~~**Rolling join history and self-backoff**~~ - done.
3. ~~**Watchdog for a silent hang**~~ - done.
4. ~~**XP rate and projection on the dashboard**~~ - done.
5. ~~**Learn the own player id**~~ - done.
6. ~~**Dashboard controls** (pause, resume, pick a room)~~ - done.
7. ~~**Rewards (wheels, loot boxes)**~~ - done.

Everything on the list is built.  What could come next, in the order of
value:

* **The test suite grows with the bot** - every protocol fix lands with
  a test first (the suite lives in `tests/`, stdlib only).
* **A second config profile** for a second account (the dual-instance
  idea without the second session in the same room).
* **Chat behaviour** - the payload and the event exist, the bot stays
  silent on purpose for now; if it ever chats, the messages need the
  same randomised pacing as the votes.

## What we deliberately do not copy

* Selling access or keys - this is a private tool.
* Blocking the game's automation probes (`/players/webBo...`) - the
  web payload does that, but the protocol mode never triggers it and
  hiding probes is exactly the behaviour that gets an account flagged.
* **Leaving a game early** - it forfeits the XP the game would have
  paid and an announced mid-game leave still counts against the
  lobby-exit countdown.  The bot finishes every game it joins.
* **A second account** - the dual-instance idea needs a second
  refresh token, which only a manual browser login can produce.
  The bot picks it up from the config when the operator provides one;
  nothing to build here until then.
