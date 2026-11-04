# wv-web-bot - Wolvesville custom-game bot (web + websocket injection)

Headless bot that plays Wolvesville custom games ("vill win" rooms) on its
own.  The browser only does what a browser has to do (Cloudflare, the
login); the game itself is played over socket.io from an injected script -
the same technique the Zoro extension uses.

```
node bot.mjs --login     # log in once in a visible browser, store the session
node bot.mjs             # run headless, 24/7
node bot.mjs --headed    # run with a visible browser (debugging)
```

The dashboard is at <http://127.0.0.1:8787/>.

## How it works

| Piece | File | What it does |
|---|---|---|
| Browser supervisor | `bot.mjs` | Playwright Chromium, session handling, restart on failure |
| Injected bot | `payload.js` | socket.io session, room list, join, vote, XP claim |
| Status page | `dashboard.mjs` | `/` (auto refresh), `/api/status`, `/api/events` |

`payload.js` is injected before any page script runs and:

1. reads the firebase token from `localStorage['authtokens']` (fallback:
   the firebase web session key),
2. hooks `fetch()` to pick up the Cloudflare attestation token (`Cf-JWT`)
   and to swallow the site's automation probes,
3. hooks `MessageEvent.data` to learn the `game-joined` payload (game id +
   game server URL),
4. opens its **own** socket.io session against the game server with
   `firebaseToken`, `gameId`, `ids`, `apiV` and `Cf-JWT`,
5. lists the open custom rooms, joins one that matches `room_names`,
6. plays: votes during the day, kills during the night, claims double XP
   when the award allows it,
7. rejoins automatically after every game and after every kick.

### Protocol notes

* Votes and other game actions are sent as **JSON strings**
  (`socket.emit("game-day-vote-set", JSON.stringify({ targetPlayerId }))`);
  only `videoAdClaimReward` takes a plain object.
* Role metadata comes from `localStorage['roles-meta-data'].roles[roleId]`
  (`team` is `VILLAGER` or `WEREWOLF`).
* `game-players-killed` -> `{victims: [{targetPlayerId, ...}]}`
* `game-werewolves-set-roles` -> `{werewolves: {playerId: roleId}}`
* `game-cupid-lover-ids-and-roles` -> `{loverPlayerIds: [], loverRoles: []}`
* `game-over-awards-available` -> `{playerAward: {awardedTotalXp, awardedLevels, canClaimDoubleXp}}`

## Configuration (`config.json`)

```json
{
  "room_names": ["vill win"],
  "language": "en",
  "game_mode": "custom",
  "headless": true,
  "reconnect_delay_sec": 5,
  "claim_double_xp": true,
  "dashboard": { "host": "127.0.0.1", "port": 8787 }
}
```

`email`/`password` are optional.  Without them the bot opens a visible
browser once and waits for you to log in - the password is then never
stored anywhere.  The resulting session lives in `state/auth.json`
(git-ignored).

## Requirements

* Node 18+ (`node --version`)
* `npm install && npx playwright install chromium`

## Docker (optional)

```bash
docker build -t wv-web-bot .
docker run -d --name wv-bot -p 8787:8787 -v "$PWD/state:/app/state" wv-web-bot
```

For the interactive login use `docker run -it --rm -v "$PWD/state:/app/state" wv-web-bot node bot.mjs --login`
with the port of the container published.
