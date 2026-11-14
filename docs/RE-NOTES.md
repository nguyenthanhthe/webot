# RE notes - Wolvesville (com.werewolfapps.online / wolvesville.com)

Everything below was derived from the shipped clients and from live
probes.  It is the reference the bot implementation is based on.

## 1. The clients

| Client | What it is | Where it lives |
|---|---|---|
| Android app | React Native + **Hermes** bytecode | `assets/index.android.bundle` (magic `c61fbc03c103191f`) |
| Web client | Same RN app built for the browser | `https://www.wolvesville.com/static/js/main.a3393cef.js` (plain JS, ~6.6 MB) |

The web bundle is the readable one - it is the same code the phone runs,
uncompiled.  `hbctool` / `hermes-dec` were tried for the Hermes bundle but
were not needed in the end.

## 2. Hosts

| Host | Purpose |
|---|---|
| `auth.api-wolvesville.com` | sign-in, token refresh, Turnstile verification |
| `core.api-wolvesville.com` | player profile, inventory, rewards (wheel, loot boxes) |
| `game.api-wolvesville.com` | custom games, game sockets |
| `game-asia.api-wolvesville.com` | asia routing (alternative) |
| `api.wolvesville.com` | the public Bot API (68 endpoints, `Authorization: Bot <key>`) |

## 3. Authentication

```
POST {auth}/players/signInWithEmailAndPassword   {email, password} -> {idToken, refreshToken}
POST {auth}/players/createIdToken                 {refreshToken}    -> {idToken}
POST {auth}/cloudflareTurnstile/verify            {token, siteKey, idToken} -> {jwt}
```

* `signIn` **fails with `403 cloudflare jwt invalid`** unless a valid
  `Cf-JWT` header is attached - so the first login needs a browser.
* `createIdToken` (refresh path) needs **no** attestation.  A
  `refreshToken` is therefore the durable credential: mint idTokens
  forever, no Turnstile.
* The web client keeps the session in `localStorage['authtokens']`
  (`{idToken, refreshToken}`); the firebase web SDK key
  (`firebase:authUser:<apiKey>:[DEFAULT]`) is the fallback.
* Turnstile site keys: `0x4AAAAAAATLZS5RyqlMGxsL` (managed),
  `0x4AAAAAAATLZulAZ6iRPZsU` (visible).

## 4. Cloudflare behaviour (the expensive part)

Cloudflare sits in front of every host and answers **403** to clients that
do not look like a browser.  Measured on this machine:

| Client | Result |
|---|---|
| Windows Python (OpenSSL), Chrome UA | **403** |
| Windows `curl` (Schannel), no UA | **403** |
| Windows `curl` (Schannel), Chrome UA | **401** (= reached the API) |
| Termux `curl` 8.12 (OpenSSL 3.4), Chrome UA | **403** |
| Termux `curl` 8.12, `--tls-max 1.2` | **401** |
| **Termux Python 3.14 (OpenSSL 3.4)** | **401** |
| Real browser (Chrome / Firefox) | works |

Rules that were derived:

1. A **browser `User-Agent`** is mandatory - `curl/8.x` gets 403.
2. The **TLS fingerprint** matters as well: on Windows only the
   Schannel-based `curl` passed; OpenSSL-based Python did not.
3. On Termux, `curl` with **TLS 1.3** is blocked but **TLS 1.2** passes;
   Termux Python passes with either.
4. Conclusion: **do not fight it - use a real browser.**  The browser
   handles Cloudflare, the Turnstile widget and the login, and the bot
   logic runs inside the page.

### What changed later

The rule above is only needed for the **login**.  Once a
`refreshToken` is in hand, the whole bot runs without a browser:

| Call | From Termux Python | Note |
|---|---|---|
| `POST /players/createIdToken` | **works** | no attestation, mints an idToken |
| `GET /api/public/game/custom` | **works** | the open room list |
| socket.io handshake (game socket) | **works** | no `Cf-JWT` needed |

The `Cf-JWT` is only required by the **sign-in** endpoint (and the
official client refreshes it every 10 minutes for the REST calls it
makes from the page).  The socket handshake does without it.

So the practical split is:

* **once**, in a real browser on the laptop: log in and grab the
  `refreshToken` (`web/login.mjs` does this),
* **forever**, on the phone: pure Python, no browser, no screen, no
  clicking.

## 5. Socket.io protocol

Engine.IO v4 / socket.io v4, websocket-only, path `/socket.io/`.

### The game socket (custom games)

A custom game is played on the **lobby socket**, not on a separate game
socket.  The client emits `mg:join-open-game` with the room id and the
server sends the whole game - `game-started`, the votes, the awards -
on that same socket.

```
query = {firebaseToken, ids: 1, apiV: 1, b: <random 42..99>, deviceId}
auth  = {firebaseToken}
transports = ["websocket"]
emit  = "mg:join-open-game", {miniGameId: <room id>}
```

A **separate** socket with the room id in the query never works: the
server answers `error-game-connection-lost`, no matter whether
`reconnect` is set or not.  The room id belongs into the join event,
not into the handshake.

This was verified live from the phone: the bot joined a real room, was
listed in the player list of `game-started` with its own role, voted
and received the XP award at the end of the game.

### The lobby socket (quick games)

The official client also opens a lobby socket on the same host:

```
query = {ids: 1, apiV: 1, b: <random 42..99>}
auth  = {firebaseToken}
```

It carries the quick-game events (`mg:games-open`, `mg:game-running`,
`lobby:chat-msg:`, `player-joined-and-equipped-items`, ...).

### Custom games with a password

The room list carries a `hasPassword` flag.  The official client asks
for the password and sends it in the handshake of the game socket:

```
GET {game}/api/public/game/customGames/checkPasswordPresence?gameId=..
GET {game}/api/public/game/customGames/validatePassword?gameId=..&password=..
query = {firebaseToken, gameId, gameMode: "custom", password, deviceId, deviceSecurityLevel}
```

The bot skips rooms with a password by default (`allow_password_rooms`
in the config turns them back on, `room_password` sends the password).

### Server -> client

| Event | Payload |
|---|---|
| `game-settings-changed` | `{gameMode, allCoupled, ...}` |
| `game-started` | `{role: <roleId>, players: [...]}` |
| `game-cupid-lover-ids-and-roles` | `{loverPlayerIds: [], loverRoles: []}` |
| `game-werewolves-set-roles` | `{werewolves: {playerId: roleId}}` |
| `game-players-killed` | `{victims: [{targetPlayerId, targetPlayerRole, cause}]}` |
| `game-night-started` / `game-day-voting-started` | - |
| `game-werewolves-vote-set` | `{playerId, targetPlayerId}` |
| `game-day-vote-set` | `{targetPlayerId, playerId}` |
| `game:chat-public:msg` / `game:chat-werewolves:msg` | `{authorId, msg}` |
| `game-reconnect-set-players` | `{playerId: {isAlive, ...}}` |
| `game-over-awards-available` | `{playerAward: {awardedTotalXp, awardedLevels, awardedXp: [{reason: GAME_FINISHED|CONSECUTIVE_GAMES, xp, clanQuestXp, battlePassXp}], awardedInventoryRewards, awardedLootBoxes, canClaimDoubleXp, battlePassRewards, ...}}` |
| `error-user-banned` | `{bannedUntilTime, banReason, banReasonMsg}` |

The `awardedXp` reasons seen live: `GAME_FINISHED` (the base pay) and
`CONSECUTIVE_GAMES` (a streak bonus for playing back to back within a
window).  The full payload is ~660 characters and ends at
`inAppReviewEligible` - **it carries no win flag**, so a "wins"
counter has no data source in the observed protocol; the dashboard
does not show one.

### The room list, measured live (2026-11-05, ~05:00 UTC+7)

One entry of `GET {game}/api/public/game/custom?language=XX` carries:
`gameId, name, language, roles[] (the deal the room will draw),
hostName, hasPassword, nightDurationInMs, dayDiscussionDurationInMs,
dayVotingDurationInMs, voiceEnabled, regularXp, playerCount,
pinnedMessage, customRandomRoles[]`.

* **`regularXp` is a boolean flag**, not a number - "true" means the
  room pays the normal XP rate.  The room pick prefers rooms where it
  is true, which is the right semantics.
* The **language parameter scopes the regional room set** (surveyed
  en/vi/id/tr/de/es/fr/pt/ru/th + the asia host): the vill-win rooms
  live in the **EN set**; the ASIA host was completely empty; each
  other language had at most one regional room at night.
* **The night lull is real scarcity, not a bug**: at ~05:00 the whole
  EN set held one 8-player vill-win room, which the bot joined the
  moment its poll saw it.

### Client -> server

| Event | Payload |
|---|---|
| `mg:join-open-game` | **plain object** `{miniGameId}` (lobby socket, custom games) |
| `game-day-vote-set` | **JSON string** `{targetPlayerId}` |
| `game-vote-to-skip` | **no payload** |
| `game-werewolves-vote-set` | **JSON string** `{targetPlayerId}` |
| `game-priest-kill-player` / `game-vigilante-shoot` / `game-gunner-shoot-player` | **JSON string** `{targetPlayerId}` |
| `game-junior-werewolf-selected-player` | **JSON string** `{targetPlayerId}` |
| `game:chat-public:msg` / `game:chat-werewolves:msg` | **JSON string** `{msg}` |
| `videoAdClaimReward` | **plain object** `{adId: "DOUBLE_XP", type: "XP_DOUBLER"}` |
| `game-over-double-xp` | - |

> Every game action is sent as a **JSON string** - the ad reward event
> and the join event take a plain object.  This is what the official
> client does and the server expects.

### The room list and the room names

`GET {game}/api/public/game/custom?language=en` returns the open rooms:

```json
{"openGames": [{"gameId": "f6c917e0-...", "name": "🏆 24HR VILL WIN NT (E) 🏆",
                "playerCount": 8, "hasPassword": false, ...}]}
```

The field is **`gameId`** here, while the quick-game socket uses `id`.

The room names are set by the players and use all kinds of characters:
`E  VİLL WİN` (Turkish dotted I), `E - VILLAGE WIN - NT -TF`,
`🏆 24HR VILL WIN NT (E) 🏆`.  Matching therefore has to fold the name to
plain ASCII (NFD, drop the combining marks, map `İ`/`ı`) before
comparing.

### Banning

The server bans accounts that join too often or that leave a lobby during
the countdown:

```
error-user-banned {"bannedUntilTime": "2026-11-04T15:12:02.176Z",
                   "banReason": "LOBBY_EXIT_COUNTDOWN",
                   "banReasonMsg": "Please wait a bit before rejoining a match."}
```

The bot has to wait the ban out and must not touch the server before
`bannedUntilTime` - joining again during the ban makes it longer.

### Role metadata

`localStorage['roles-meta-data'].roles[roleId]` -> `{id, name, team}`
with `team` being `VILLAGER` or `WEREWOLF`.

### REST

```
GET {game}/api/public/game/custom?language=en          -> {openGames: [...]}
GET {game}/api/public/game/custom/settings?gameId=..&locale=en
GET {game}/api/public/game/customGames/checkPasswordPresence?gameId=..
GET {core}/players/meAndCheckAppVersion
```

## 6. What the official bot (Zoro) does - and where it is wrong

The third-party Zoro Chrome extension injects a socket.io client into the page
and plays custom games:

* it hooks `MessageEvent.prototype.data` to read the page's own frames
  (that is how it learns `gameId`/`serverUrl`),
* it opens a **parallel socket.io session** with the page's firebase token,
* it votes, kills, chats (`me`, `wc`, `solo`, `Who?`) and claims double XP,
* it re-enters by *clicking* "Play again" / "Continue" in the DOM,
* it blocks the site's automation probes (`/players/webBo...`,
  `/players/webAutomatio...`) and fakes `/api/public/game/running`.

Worth copying: the parallel socket, the JSON-string payloads, the
automation-probe blocking, the double-XP claim.

Worth fixing (its bugs): a wolf without a lover never votes at all, the
villager only votes after a chat keyword appears, the lovers array is
misaligned, `PLAYER.ID` vs `PLAYER.id` guards are no-ops, the socket is
never re-created on disconnect and the "Play again" poller multiplies.

## 7. The Android app (native client)

* Package `com.werewolfapps.online`, sideloaded XAPK (arm64 split).
* The UI is React Native, so `uiautomator` shows real text: room cards
  (`"Vill Win -TF- (E), MrNuii, , 7"`), the room modal with the
  **"Kết nối lại"** button, players as `"<number> <name>"`, and the
  game-over screen with **"Tiếp tục"**.
* A UI-driven bot is easy to write - and impossible to run headless: the
  screen has to stay on, which is a dealbreaker for a 24/7 bot.
* Useful anyway: it proves which rooms exist ("vill win" rooms are
  visible in the app even with a Vietnamese UI).

## 8. Device notes (Samsung Galaxy A03s, Termux)

* `run-as com.termux` has **no network** (SELinux): socket creation
  fails with EPERM.  The Termux *app session* has network.
* Android hands Termux a dead nameserver when Tailscale is up
  (`192.168.1.57`); the router (`192.168.1.1`) works.  `wvbot/netfix.py`
  probes candidates and writes the first that answers.
* The installed Termux packages were built against a **newer libc++** than
  the installed one, so `adb` and `firefox` failed with
  `_ZNSt6__ndk113__hash_memoryEPKvm`.  `apt install --only-upgrade libc++`
  (27c -> 30) fixed both.
* Headless Firefox with Marionette works in Termux and passes Cloudflare:
  that is what the phone-side runner uses.

## 9. Alternatives that were tried and rejected

| Idea | Why it was dropped |
|---|---|
| Pure protocol bot in Python on the phone | needs a `Cf-JWT` for the first sign-in; Turnstile needs a browser.  Kept as `run.py --mode protocol` for a machine with a valid token. |
| UI-click bot on the native app | the screen must stay on -> not headless, not 24/7 |
| TLS-impersonating client (`curl-impersonate`) | unnecessary: the phone's own browser already passes |
| Docker | not installed on the laptop; the plain runner is simpler.  A Dockerfile is provided for people who want it. |

## When the game is patched - the runbook

The bot notices a patch by itself: the update watcher polls the build
number (`appVersions/check`) and the client bundle hash
(`www.wolvesville.com/static/js/main.<hash>.js`) every 30 minutes and
logs "the game was patched" when either moves.  What the operator does
then:

1. **Download the new bundle** (the URL is in the watcher's log line -
   the hash part changes) and grep it for every event name this bot
   uses: `mg:join-open-game`, `mg:exit-game`, `game-day-vote-set`,
   `game-werewolves-vote-set`, `game-vote-to-skip`,
   `game-over-awards-available`, `videoAdClaimReward`.  A renamed emit
   is the usual breakage.
2. **Diff the handshake query** (`firebaseToken`, `ids`, `apiV`, `b`,
   `deviceId`) against `wvbot/bot.py::_play_game` - a new field here
   answers `error-game-connection-lost` on every join.
3. **Fix the code + the tests together** (`tests/` covers the payload
   parsing and every event), run `python -m unittest discover -s
   tests`, deploy with `python tools/deploy.py`.
4. **The room list fields** (`regularXp` is a boolean flag) live in
   `docs/RE-NOTES.md` - re-measure them with the `/api/rooms` gateway
   if the room list shape changed.

Historically, patches moved the bundle hash more often than the
protocol itself; most patches change nothing for the bot.
