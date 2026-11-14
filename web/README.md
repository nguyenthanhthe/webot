# wv-web-bot - Wolvesville custom-game bot (web + websocket injection)

Headless bot that plays Wolvesville custom games ("vill win" rooms) on its
own.  The browser handles Cloudflare and initial login; the game itself is
driven over Socket.IO from an injected script (`payload.js`).

```
node login.mjs             # open visible browser once, save session + tokens
node bot.mjs               # run headless, 24/7
node bot.mjs --headed      # run with a visible browser (debugging)
node bot.mjs --login       # log in once with a visible browser and exit
npm run dashboard          # run standalone status dashboard
npm run check              # syntax-check all JS/MJS files
```

The dashboard is served on <http://127.0.0.1:8787/>.

## How it works

| Piece | File | What it does |
|---|---|---|
| Browser supervisor | `bot.mjs` | Playwright Chromium, session handling, Node-side token minting, restart on failure |
| Interactive login | `login.mjs` | Opens visible browser to capture `state/auth.json` and `state/tokens.json` (mode `0600`) |
| Injected bot | `payload.js` | Lobby/game Socket.IO handler, room filter, `mg:join-open-game`, voting, double-XP claim |
| Status page | `dashboard.mjs` | `/` (auto-refresh), `/api/status`, `/api/events`, `/health` |

`payload.js` is injected before any page script runs and:

1. obtains the Firebase `idToken` from `localStorage['authtokens']` or via `window.__wvMintIdToken()` on the Node host,
2. hooks `fetch()` to capture the Cloudflare attestation header (`Cf-JWT`) and mask web-automation probes,
3. connects to the Wolvesville game gateway Socket.IO server (`https://game.api-wolvesville.com`), lists open custom rooms, and joins matching rooms via `mg:join-open-game`,
4. plays day voting, night werewolf voting, and single-use killer actions directly over that lobby socket,
5. claims double XP when `canClaimDoubleXp` is offered, emits `mg:exit-game` cleanly after game over, and rejoins the next room.

### Protocol notes

* Game actions are sent as **JSON strings** (`socket.emit("game-day-vote-set", JSON.stringify({ targetPlayerId }))`); only `videoAdClaimReward` and `mg:*` room control messages take plain objects.
* Role metadata comes from `localStorage['roles-meta-data'].roles[roleId]` (`team` is `VILLAGER` or `WEREWOLF`).
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

`email`/`password` are optional.  Without them, run `npm run login` (`node login.mjs`) to sign in interactively in a visible browser window; the session is stored in `state/auth.json` and `state/tokens.json` (git-ignored, `0600` permissions).

## Requirements

* Node 20+ (`node --version`, required by `playwright@^1.64.1`)
* `npm install && npx playwright install chromium`

## Docker (root Python runner)

Container deployment is configured at the repository root (`Dockerfile` and `docker-compose.yml`) for the Python protocol/farm runner:

```bash
docker compose up -d
```
