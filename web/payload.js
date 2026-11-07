/**
 * The injected half of the bot.
 *
 * It runs inside the Wolvesville web client and drives the game over
 * socket.io - the same technique the Zoro extension uses:
 *
 *   - it opens its own socket.io session ("parallel socket") with the
 *     page's firebase token and plays through it
 *   - it listens to the page's socket for the game id / server url
 *   - it hooks fetch() to pick up the Cloudflare attestation token and to
 *     hide the automation probes the site sends
 *
 * Everything the server expects as a payload is sent as a JSON *string*
 * (only the ad reward event takes a plain object) - that is what the
 * official client does.
 */
(() => {
  if (window.__wvbot) return;

  const CFG = Object.assign(
    {
      gameHost: "https://game.api-wolvesville.com",
      authHost: "https://auth.api-wolvesville.com",
      // The durable credential: it can mint new idTokens without a browser.
      refreshToken: null,
      roomNames: ["vill win", "say m", "ww say", "8p", "tf", "farm", "quick", "fast", "xp", "vw8"],
      language: "en",
      gameMode: "custom",
      requireAllCoupled: true,
      claimDoubleXp: true,
    },
    window.__WV_CONFIG || {},
  );

  const KILLER_EVENTS = {
    priest: "game-priest-kill-player",
    vigilante: "game-vigilante-shoot",
    gunner: "game-gunner-shoot-player",
  };
  const SOLO_EVIL_ROLES = [
    "serial-killer", "arsonist", "corruptor", "bandit", "cannibal",
    "evil-detective", "bomber", "alchemist", "siren", "illusionist",
    "blight", "sect-leader", "zombie",
  ];
  const WOLF_TEAMS = ["WEREWOLF", "WOLF", "EVIL"];

  // Outbound requests only ever go to the official API hosts, over https.
  const ALLOWED_API_HOSTS = [
    "game.api-wolvesville.com",
    "game-asia.api-wolvesville.com",
    "core.api-wolvesville.com",
    "auth.api-wolvesville.com",
  ];

  function apiUrl(path) {
    const url = new URL(path, CFG.gameHost);
    if (url.protocol !== "https:") {
      throw new Error(`only https is allowed, got ${url.protocol}`);
    }
    if (ALLOWED_API_HOSTS.indexOf(url.hostname) === -1) {
      throw new Error(`unexpected host: ${url.hostname}`);
    }
    return url.toString();
  }

  /** The auth host serves the token endpoints. */
  function authApiUrl(path) {
    const url = new URL(path, CFG.authHost);
    if (url.protocol !== "https:") {
      throw new Error(`only https is allowed, got ${url.protocol}`);
    }
    if (ALLOWED_API_HOSTS.indexOf(url.hostname) === -1) {
      throw new Error(`unexpected host: ${url.hostname}`);
    }
    return url.toString();
  }

  const state = {
    status: "booting",
    phase: "lobby",
    room: null,
    game_id: null,
    role: null,
    players: 0,
    alive: 0,
    xp_total: 0,
    xp_last: 0,
    games_played: 0,
    wins: 0,
    kills: 0,
    reconnects: 0,
    errors: 0,
    last_error: null,
  };

  const runtime = {
    idToken: null,
    refreshToken: null,
    cfJwt: null,
    deviceId: null,
    lobby: null,
    game: null,
    ownId: null,
    username: null,
    role: { id: "", name: "", team: "" },
    players: new Map(),
    dead: new Set(),
    dayVotes: new Map(),
    lovers: [],
    messages: [],
    settings: null,
    openRooms: [],
    joining: false,
    timer: null,
  };

  /* ---------------------------------------------------------------- */
  /* reporting                                                        */
  /* ---------------------------------------------------------------- */

  /**
   * Send a report to the supervisor.
   *
   * On the Node/Playwright host the browser exposes a binding
   * (`window.__wvbot`).  On the phone there is no binding: the supervisor
   * reads the state and the event log straight out of the page.
   */
  function post(payload) {
    try {
      if (typeof window.__wvbot === "function") {
        window.__wvbot(payload);
      }
    } catch (err) {
      /* ignore */
    }
  }

  const recentEvents = [];
  let lastEventKey = "";

  function reportEvent(level, message) {
    const entry = { t: Date.now(), level, message: String(message).slice(0, 300) };
    // The same message over and over (for example "not logged in yet")
    // would only flood the dashboard.
    const key = `${level}|${entry.message}`;
    if (key === lastEventKey) return;
    lastEventKey = key;
    recentEvents.push(entry);
    if (recentEvents.length > 200) recentEvents.shift();
    post({ kind: "event", level, message: entry.message });
  }

  function pushState(patch) {
    Object.assign(state, patch || {});
    state.players = runtime.players.size;
    state.alive = [...runtime.players.values()].filter((p) => p.isAlive).length;
    post({ kind: "state", state: { ...state } });
  }

  /* ---------------------------------------------------------------- */
  /* helpers                                                          */
  /* ---------------------------------------------------------------- */

  function parsePayload(raw) {
    let value = raw;
    for (let i = 0; i < 2; i += 1) {
      if (typeof value === "string") {
        try {
          value = JSON.parse(value);
        } catch (err) {
          return value;
        }
      } else {
        break;
      }
    }
    return value;
  }

  /** The official client keeps the session in localStorage['authtokens']. */
  function readAuthTokens() {
    let idToken = null;
    let refreshToken = null;
    try {
      const raw = window.localStorage.getItem("authtokens");
      if (raw) {
        const parsed = JSON.parse(raw);
        idToken = parsed.idToken || null;
        refreshToken = parsed.refreshToken || null;
      }
    } catch (err) {
      /* ignore */
    }
    if (!idToken) {
      try {
        for (const key of Object.keys(window.localStorage)) {
          const raw = window.localStorage.getItem(key);
          if (!raw || raw.indexOf("accessToken") === -1) continue;
          const parsed = JSON.parse(raw);
          const manager = parsed.stsTokenManager || parsed;
          if (manager.accessToken) {
            idToken = manager.accessToken;
            refreshToken = manager.refreshToken || refreshToken;
            break;
          }
        }
      } catch (err) {
        /* ignore */
      }
    }
    return { idToken, refreshToken };
  }

  /** The expiry of a JWT, in seconds since the epoch (0 when unknown). */
  function jwtExpiry(token) {
    try {
      const part = String(token).split(".")[1];
      const padded = part.replace(/-/g, "+").replace(/_/g, "/");
      const json = decodeURIComponent(
        atob(padded)
          .split("")
          .map((c) => `%${`00${c.charCodeAt(0).toString(16)}`.slice(-2)}`)
          .join(""),
      );
      return Number(JSON.parse(json).exp) || 0;
    } catch (err) {
      return 0;
    }
  }

  /**
   * Mint a fresh idToken from the refreshToken.
   *
   * The refresh path needs no Cloudflare attestation, so this works for as
   * long as the refreshToken is alive.  The idToken itself is only good for
   * an hour, so the bot renews it long before it expires.
   */
  async function mintIdToken(refreshToken) {
    const url = authApiUrl("/players/createIdToken");
    const response = await fetch(url, {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ refreshToken }),
    });
    if (!response.ok) throw new Error(`createIdToken -> HTTP ${response.status}`);
    const data = await response.json();
    if (!data || !data.idToken) throw new Error("createIdToken returned no idToken");
    return data.idToken;
  }

  /**
   * Make sure we have a usable idToken.
   *
   * The page session is preferred (its idToken matches the Cloudflare
   * attestation the page holds).  When the page is not logged in but a
   * refreshToken is configured, a new idToken is minted instead.
   */
  async function ensureToken() {
    const tokens = readAuthTokens();
    runtime.refreshToken = tokens.refreshToken || runtime.refreshToken;
    const expiry = jwtExpiry(tokens.idToken);
    const now = Math.floor(Date.now() / 1000);
    // The page token is good for an hour; renew it 5 minutes before that.
    if (tokens.idToken && (!expiry || expiry - now > 300)) {
      runtime.idToken = tokens.idToken;
      return runtime.idToken;
    }
    if (tokens.idToken && expiry) {
      runtime.idToken = tokens.idToken;
      return runtime.idToken;
    }
    if (runtime.refreshToken) {
      runtime.idToken = await mintIdToken(runtime.refreshToken);
      return runtime.idToken;
    }
    throw new Error("no firebase token available");
  }

  /** Role metadata: localStorage['roles-meta-data'].roles[roleId]. */
  function roleMeta(roleId) {
    if (!roleId) return null;
    try {
      const raw = window.localStorage.getItem("roles-meta-data");
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      const role = parsed && parsed.roles ? parsed.roles[roleId] : null;
      if (!role) return null;
      return {
        id: String(role.id || roleId),
        name: String(role.name || ""),
        team: String(role.team || ""),
      };
    } catch (err) {
      return null;
    }
  }

  function pick(list) {
    if (!list.length) return null;
    return list[Math.floor(Math.random() * list.length)];
  }

  function isWolf(meta) {
    if (!meta) return false;
    return WOLF_TEAMS.indexOf(String(meta.team || "").toUpperCase()) !== -1;
  }

  function gridNumber(playerId) {
    const player = runtime.players.get(String(playerId));
    if (!player) return null;
    return (player.gridIdx >= 0 ? player.gridIdx : 0) + 1;
  }

  /* ---------------------------------------------------------------- */
  /* fetch hook: attestation token + hide automation probes            */
  /* ---------------------------------------------------------------- */

  function installFetchHook() {
    if (window.__wvFetchHooked) return;
    window.__wvFetchHooked = true;
    const original = window.fetch.bind(window);
    window.fetch = function (input, init) {
      try {
        const url = typeof input === "string" ? input : input && input.url ? input.url : "";
        // The site phones home to detect automation - hide that.
        if (url.indexOf("/players/webBo") !== -1 || url.indexOf("/players/webAutomatio") !== -1) {
          return Promise.resolve(new Response("{}", { status: 200 }));
        }
        const headers = (init && init.headers) || (input && input.headers);
        if (headers) {
          try {
            const bearer = headers.get ? headers.get("authorization") : headers.Authorization;
            if (bearer && bearer.indexOf("Bearer ") === 0) runtime.idToken = bearer.slice(7);
            const cf = headers.get ? headers.get("cf-jwt") : headers["cf-jwt"];
            if (cf) runtime.cfJwt = cf;
          } catch (err) {
            /* ignore */
          }
        }
      } catch (err) {
        /* ignore */
      }
      return original(input, init);
    };
  }

  /* ---------------------------------------------------------------- */
  /* page socket interception (game id + server url)                   */
  /* ---------------------------------------------------------------- */

  function installPageSocketHook() {
    if (window.__wvSocketHooked) return;
    window.__wvSocketHooked = true;
    const descriptor = Object.getOwnPropertyDescriptor(MessageEvent.prototype, "data");
    if (!descriptor || !descriptor.get) return;
    const originalGet = descriptor.get;
    Object.defineProperty(MessageEvent.prototype, "data", {
      configurable: true,
      get() {
        const value = originalGet.call(this);
        try {
          if (typeof value === "string" && value.slice(0, 2) === "42") {
            const parsed = parsePayload(value.slice(2));
            if (Array.isArray(parsed) && parsed[0] === "game-joined") {
              const info = extractGameInfo(parsed[1]);
              if (info && info.gameId) {
                reportEvent("game", `page joined game ${info.gameId}`);
                openGameSocket(info);
              }
            }
          }
        } catch (err) {
          /* ignore */
        }
        return value;
      },
    });
  }

  function extractGameInfo(raw) {
    const data = parsePayload(raw);
    if (!data || typeof data !== "object") return null;
    const info = {};
    if (data.gameId !== undefined && data.gameId !== null) info.gameId = data.gameId;
    else if (data.id !== undefined && data.id !== null) info.gameId = data.id;
    for (const key of ["serverUrl", "server", "url"]) {
      if (typeof data[key] === "string" && data[key].indexOf("http") === 0) {
        info.serverUrl = data[key];
        break;
      }
    }
    const values = Object.values(data);
    if (info.gameId === undefined && values.length) info.gameId = values[0];
    if (!info.serverUrl && values.length > 1) {
      const candidate = values[1];
      if (typeof candidate === "string" && candidate.indexOf("http") === 0) info.serverUrl = candidate;
    }
    return info;
  }

  /* ---------------------------------------------------------------- */
  /* room list                                                        */
  /* ---------------------------------------------------------------- */

  /**
   * Fold a room name down to plain ASCII.
   *
   * The English rooms are named by players, and they use all kinds of
   * characters: "E  VİLL WİN" (Turkish dotted I), "E - VILLAGE WIN".
   * Comparing the folded names is what makes the match reliable.
   */
  function foldName(value) {
    return String(value || "")
      .normalize("NFD")
      .replace(/[̀-ͯ]/g, "")
      .replace(/İ/g, "I")
      .replace(/ı/g, "i")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, " ")
      .trim();
  }

  async function listRooms() {
    const token = runtime.idToken;
    if (!token) throw new Error("no firebase token available");
    const url = apiUrl(
      `/api/public/game/custom?language=${encodeURIComponent(CFG.language)}`,
    );
    const response = await fetch(url, {
      headers: { Authorization: `Bearer ${token}`, ids: "1" },
    });
    if (!response.ok) throw new Error(`custom game list -> HTTP ${response.status}`);
    const data = await response.json();
    const games = data.openGames || data.games || [];
    const wanted = CFG.roomNames.map((name) => foldName(name));
    return games.filter((room) => {
      const name = foldName(room && (room.name || room.gameName || room.title));
      return name && wanted.some((needle) => name.indexOf(needle) !== -1);
    });
  }

  function roomId(room) {
    return room.miniGameId || room.gameId || room.id || room._id || null;
  }

  function roomName(room) {
    return room.name || room.gameName || room.title || "";
  }

  /* ---------------------------------------------------------------- */
  /* sockets                                                          */
  /* ---------------------------------------------------------------- */

  function connectLobby() {
    if (runtime.lobby) {
      try {
        runtime.lobby.close();
      } catch (err) {
        /* ignore */
      }
    }
    // The official client puts the token in the handshake auth and keeps
    // the rest in the query (ids, apiV and a random "b" nonce).
    const socket = window.io(CFG.gameHost, {
      path: "/socket.io",
      transports: ["websocket"],
      query: { ids: 1, apiV: 1, b: randomNonce() },
      auth: socketAuth(),
      reconnection: true,
      reconnectionDelay: 2000,
      reconnectionDelayMax: 10000,
    });
    runtime.lobby = socket;

    socket.on("connect", () => {
      reportEvent("info", "lobby socket connected");
      pushState({ status: "looking for room", last_error: null });
      scheduleJoin(500);
    });
    socket.on("connect_error", (err) => {
      reportEvent("warn", `lobby connect error: ${err && err.message}`);
      pushState({ errors: state.errors + 1, last_error: `lobby: ${err && err.message}` });
    });
    socket.on("disconnect", (reason) => {
      reportEvent("warn", `lobby disconnected: ${reason}`);
      pushState({ status: "reconnecting", reconnects: state.reconnects + 1 });
      scheduleJoin(3000);
    });
    socket.onAny((event, payload) => {
      if (event === "game-joined" || event === "mg:running-game" || event === "mg:game-running") {
        const info = extractGameInfo(payload);
        if (info && info.gameId) openGameSocket(info);
      }
      // The server tells the lobby which rooms are open right now.
      if (event === "mg:games-open") {
        const data = parsePayload(payload);
        const games = (data && data.games) || [];
        if (games.length) {
          reportEvent("lobby", `lobby reports ${games.length} open room(s)`);
          runtime.openRooms = games;
        }
      }
    });
  }

  /** A random nonce between 42 and 99, like the official client uses. */
  function randomNonce() {
    const buffer = new Uint32Array(1);
    window.crypto.getRandomValues(buffer);
    const value = Math.floor((buffer[0] / 0x100000000) * 99);
    return value >= 42 ? value + 1 : value;
  }

  /** The handshake auth: the firebase token and the Cloudflare attestation. */
  function socketAuth() {
    const auth = {};
    if (runtime.idToken) auth.firebaseToken = runtime.idToken;
    if (runtime.cfJwt) auth["Cf-JWT"] = runtime.cfJwt;
    return auth;
  }

  function openGameSocket(info) {
    if (runtime.game) {
      try {
        runtime.game.close();
      } catch (err) {
        /* ignore */
      }
    }
    const query = {
      gameId: info.gameId,
      reconnect: "true",
      gameMode: CFG.gameMode,
      ids: 1,
      apiV: 1,
      b: randomNonce(),
    };
    if (runtime.deviceId) query.deviceId = runtime.deviceId;
    const socket = window.io(info.serverUrl || CFG.gameHost, {
      path: "/socket.io",
      transports: ["websocket"],
      query,
      auth: socketAuth(),
      reconnection: true,
      reconnectionDelay: 1500,
    });
    runtime.game = socket;

    socket.on("connect", () => {
      reportEvent("game", "game socket connected");
      pushState({ status: "playing", game_id: String(info.gameId), phase: "joined" });
    });
    socket.on("connect_error", (err) => {
      reportEvent("warn", `game socket error: ${err && err.message}`);
      pushState({ errors: state.errors + 1 });
    });
    socket.on("disconnect", (reason) => {
      reportEvent("warn", `game socket disconnected: ${reason}`);
      pushState({ status: "reconnecting", reconnects: state.reconnects + 1 });
      // Being kicked out of the room ends the session: look for a room
      // again instead of waiting for a game that will not come back.
      runtime.game = null;
      scheduleJoin(3000);
    });
    socket.onAny((event, payload) => handleGameEvent(socket, event, payload));
  }

  /* ---------------------------------------------------------------- */
  /* game logic                                                       */
  /* ---------------------------------------------------------------- */

  function handleGameEvent(socket, event, raw) {
    const data = parsePayload(raw);
    switch (event) {
      case "game-settings-changed":
        runtime.settings = data;
        break;
      case "game-started":
        onGameStarted(data);
        break;
      case "game-day-voting-started":
        pushState({ phase: "day-voting" });
        dayVote(socket);
        break;
      case "game-night-started":
        pushState({ phase: "night" });
        nightVote(socket);
        break;
      case "game-werewolves-vote-set":
        pushState({ phase: "night-voting" });
        nightVote(socket);
        break;
      case "game-day-vote-set":
        onDayVote(data);
        killerAction(socket);
        break;
      case "game-players-killed":
        onKilled(data);
        break;
      case "game-werewolves-set-roles":
        onWolfRoles(data);
        break;
      case "game-cupid-lover-ids-and-roles":
        onLovers(data);
        break;
      case "game-reconnect-set-players":
        onReconnectPlayers(data);
        break;
      case "game:chat-public:msg":
      case "game:chat-werewolves:msg":
        if (data && typeof data === "object") {
          runtime.messages.push(data);
          if (runtime.messages.length > 200) runtime.messages.shift();
        }
        break;
      case "game-over-awards-available":
        onGameOver(socket, data);
        break;
      case "game-game-over":
        pushState({ phase: "game-over" });
        break;
      default:
        break;
    }
  }

  function onGameStarted(data) {
    runtime.players = new Map();
    runtime.dead = new Set();
    runtime.dayVotes = new Map();
    runtime.messages = [];

    const roleId = data && data.role ? String(data.role) : "";
    runtime.role = roleMeta(roleId) || { id: roleId, name: roleId, team: "" };

    const players = (data && (data.players || data.gamePlayers)) || [];
    if (Array.isArray(players)) {
      for (const raw of players) {
        const player = parsePlayer(raw);
        if (player) runtime.players.set(String(player.id), player);
      }
    }
    // The player list may carry our own id.
    for (const player of runtime.players.values()) {
      if (player.username && runtime.username && player.username === runtime.username) {
        runtime.ownId = player.id;
      }
    }
    reportEvent(
      "game",
      `game started: role=${runtime.role.name || runtime.role.id || "?"} (${runtime.role.team || "?"}) players=${runtime.players.size}`,
    );
    pushState({
      role: runtime.role.name || runtime.role.id || null,
      phase: "started",
      status: "playing",
    });
  }

  function parsePlayer(raw) {
    if (!raw || typeof raw !== "object") return null;
    const id = raw.id || raw.playerId || raw._id;
    if (id === undefined || id === null) return null;
    let role = "";
    if (raw.role && typeof raw.role === "object") role = String(raw.role.id || raw.role._id || "");
    else if (raw.role) role = String(raw.role);
    return {
      id: String(id),
      username: String(raw.username || raw.name || ""),
      gridIdx: Number(raw.gridIdx === undefined ? raw.gridIndex : raw.gridIdx) || 0,
      isAlive: raw.isAlive === undefined ? true : Boolean(raw.isAlive),
      role,
      spectate: Boolean(raw.spectate),
    };
  }

  function onKilled(data) {
    const victims = (data && data.victims) || [];
    for (const victim of victims) {
      const id = victim && (victim.targetPlayerId || victim.id);
      if (id === undefined || id === null) continue;
      const key = String(id);
      runtime.dead.add(key);
      const player = runtime.players.get(key);
      if (player) player.isAlive = false;
    }
  }

  function onWolfRoles(data) {
    if (!data || typeof data !== "object") return;
    const wolves = data.werewolves || {};
    for (const [id, roleId] of Object.entries(wolves)) {
      const player = runtime.players.get(String(id));
      if (!player) continue;
      player.role = String(roleId);
      const meta = roleMeta(String(roleId));
      if (meta) player.team = meta.team;
    }
  }

  function onLovers(data) {
    if (!data || typeof data !== "object") return;
    const ids = data.loverPlayerIds || [];
    const roles = data.loverRoles || [];
    runtime.lovers = ids
      .map((id, index) => ({
        id: String(id),
        role: String(roles[index] || ""),
      }))
      .filter((lover) => lover.id !== String(runtime.ownId));
  }

  function onDayVote(data) {
    if (!data || typeof data !== "object") return;
    const target = data.targetPlayerId;
    if (target === undefined || target === null) return;
    const author = data.playerId || data.authorId || runtime.ownId;
    runtime.dayVotes.set(String(author), String(target));
  }

  function onReconnectPlayers(data) {
    if (!data || typeof data !== "object") return;
    for (const [id, info] of Object.entries(data)) {
      if (!info || typeof info !== "object") continue;
      const player = runtime.players.get(String(id));
      if (player && info.isAlive !== undefined) {
        player.isAlive = Boolean(info.isAlive);
        if (!player.isAlive) runtime.dead.add(String(id));
      }
      if (info && (info.isYou || info.you || info.isMe)) runtime.ownId = String(id);
      if (info && info.username && runtime.username && info.username === runtime.username) {
        runtime.ownId = String(id);
      }
    }
  }

  function alivePlayers() {
    return [...runtime.players.values()].filter((p) => p.isAlive && !runtime.dead.has(p.id));
  }

  /** How often each player is accused in the public chat. */
  function accusations() {
    const counts = new Map();
    for (const message of runtime.messages) {
      const text = String((message && message.msg) || "").toLowerCase();
      if (!text) continue;
      const author = message.authorId;
      for (const player of runtime.players.values()) {
        if (!player.username) continue;
        if (String(author) === player.id) continue;
        if (text.indexOf(String(player.username).toLowerCase()) !== -1) {
          counts.set(player.id, (counts.get(player.id) || 0) + 1);
        }
      }
    }
    // "me" / "wc" style confessions point at the speaker.
    for (const message of runtime.messages) {
      const text = String((message && message.msg) || "").trim().toLowerCase();
      if (["me", "m", "wc", "wolf", "im the wolf", "i am wolf"].indexOf(text) === -1) continue;
      const author = String(message.authorId);
      if (author === String(runtime.ownId)) continue;
      if (runtime.players.has(author)) counts.set(author, (counts.get(author) || 0) + 2);
    }
    return counts;
  }

  function dayVote(socket) {
    if (runtime.ownId && runtime.dead.has(String(runtime.ownId))) return;
    const alive = alivePlayers();
    if (!alive.length) return;
    const counts = accusations();
    let target = null;
    let best = 0;
    for (const [id, count] of counts) {
      if (count > best && alive.some((p) => p.id === id)) {
        best = count;
        target = id;
      }
    }
    if (target) {
      socket.emit("game-day-vote-set", JSON.stringify({ targetPlayerId: target }));
      reportEvent("action", `day vote -> ${target}`);
    } else {
      socket.emit("game-vote-to-skip");
      reportEvent("action", "day vote: skip");
    }
  }

  function nightVote(socket) {
    if (!isWolf(runtime.role)) return;
    const alive = alivePlayers().filter((p) => p.id !== String(runtime.ownId));
    if (!alive.length) return;
    const counts = accusations();
    const quiet = alive.filter((p) => !counts.get(p.id));
    const pool = quiet.length ? quiet : alive;
    const target = pick(pool);
    socket.emit("game-werewolves-vote-set", JSON.stringify({ targetPlayerId: target.id }));
    reportEvent("action", `werewolf vote -> ${target.username || target.id}`);
    state.kills += 1;
    pushState({ kills: state.kills });
  }

  function killerAction(socket) {
    const event = KILLER_EVENTS[String(runtime.role.id || "").toLowerCase()];
    if (!event) return;
    let target = null;
    for (const value of runtime.dayVotes.values()) target = value;
    if (!target) return;
    socket.emit(event, JSON.stringify({ targetPlayerId: target }));
    reportEvent("action", `${event} -> ${target}`);
  }

  function onGameOver(socket, data) {
    const award = (data && (data.playerAward || data.award)) || {};
    const xp = Number(award.awardedTotalXp || 0);
    const levels = Number(award.awardedLevels || 0);
    const won = Boolean(award.isWinner || award.won);
    pushState({
      phase: "game-over",
      status: "game-over",
      xp_last: xp,
      xp_total: state.xp_total + xp,
      games_played: state.games_played + 1,
      wins: state.wins + (won ? 1 : 0),
    });
    reportEvent("xp", `game over: +${xp} xp${levels ? ` (+${levels} levels)` : ""}${won ? " - won" : ""}`);

    if (CFG.claimDoubleXp && award.canClaimDoubleXp) {
      try {
        socket.emit("videoAdClaimReward", { adId: "DOUBLE_XP", type: "XP_DOUBLER" });
        socket.emit("game-over-double-xp");
        reportEvent("xp", "claimed double xp");
      } catch (err) {
        reportEvent("warn", `double xp failed: ${err}`);
      }
    }
    try {
      socket.close();
    } catch (err) {
      /* ignore */
    }
    runtime.game = null;
    scheduleJoin(4000);
  }

  /* ---------------------------------------------------------------- */
  /* joining                                                          */
  /* ---------------------------------------------------------------- */

  function scheduleJoin(delay) {
    if (runtime.timer) clearTimeout(runtime.timer);
    runtime.timer = setTimeout(joinRoom, delay);
  }

  async function joinRoom() {
    if (runtime.joining) return;
    runtime.joining = true;
    try {
      await ensureToken();
      if (!runtime.lobby || !runtime.lobby.connected) {
        connectLobby();
        return;
      }
      const rooms = await listRooms();
      if (!rooms.length) {
        reportEvent("lobby", "no matching room open");
        pushState({ status: "waiting for room", room: null });
        scheduleJoin(15000);
        return;
      }
      const room = pick(rooms);
      const id = roomId(room);
      if (!id) throw new Error("room has no id");
      reportEvent("lobby", `joining room "${roomName(room)}" (${id})`);
      pushState({ status: "joining", room: roomName(room) });
      // The server expects the room id under "miniGameId".
      runtime.lobby.emit("mg:join-open-game", { miniGameId: id });
      scheduleJoin(30000);
    } catch (err) {
      reportEvent("error", `join failed: ${err && err.message}`);
      pushState({ status: "error", last_error: String(err && err.message), errors: state.errors + 1 });
      scheduleJoin(10000);
    } finally {
      runtime.joining = false;
    }
  }

  /* ---------------------------------------------------------------- */
  /* boot                                                             */
  /* ---------------------------------------------------------------- */

  /**
   * The Cloudflare attestation the page holds.
   *
   * The official client keeps it in localStorage and puts it in the socket
   * handshake.  It is bound to the idToken it was verified with, so it is
   * only used together with the token the page is currently holding.
   */
  function readCfJwt() {
    try {
      const raw = window.localStorage.getItem("cloudflare-turnstile-jwt");
      if (raw) return raw;
    } catch (err) {
      /* ignore */
    }
    return runtime.cfJwt || null;
  }

  /**
   * Keep the token fresh.
   *
   * The page refreshes its own idToken every few minutes and writes it to
   * localStorage; the Cloudflare attestation follows along.  When the page
   * token changes, the sockets have to be rebuilt - they carry the token
   * in their handshake.
   */
  function startTokenWatchdog() {
    const timer = setInterval(async () => {
      try {
        const tokens = readAuthTokens();
        const expiry = jwtExpiry(tokens.idToken);
        const now = Math.floor(Date.now() / 1000);
        const stale = !tokens.idToken || (expiry && expiry - now < 300);
        const changed = Boolean(tokens.idToken) && tokens.idToken !== runtime.idToken;

        if (!stale && !changed) return;

        if (stale && runtime.refreshToken) {
          reportEvent("login", "renewing the firebase token");
          runtime.idToken = await mintIdToken(runtime.refreshToken);
        } else if (tokens.idToken) {
          runtime.idToken = tokens.idToken;
        }
        runtime.cfJwt = readCfJwt();
        // The sockets carry the token in their handshake, so they have to
        // be rebuilt with the new one.
        if (runtime.game) {
          runtime.game.close();
          runtime.game = null;
        }
        if (runtime.lobby) {
          runtime.lobby.close();
          runtime.lobby = null;
        }
        connectLobby();
      } catch (err) {
        reportEvent("warn", `token refresh failed: ${err && err.message}`);
      }
    }, 60 * 1000);
    return timer;
  }

  async function boot() {
    installFetchHook();
    installPageSocketHook();
    const tokens = readAuthTokens();
    runtime.idToken = tokens.idToken;
    runtime.refreshToken = tokens.refreshToken || CFG.refreshToken || null;
    runtime.cfJwt = readCfJwt();
    if (!runtime.idToken && !runtime.refreshToken) {
      reportEvent("error", "no firebase token yet (not logged in?)");
      pushState({ status: "waiting for login" });
      setTimeout(boot, 5000);
      return;
    }
    try {
      await ensureToken();
    } catch (err) {
      reportEvent("error", `login failed: ${err && err.message}`);
      pushState({ status: "waiting for login", last_error: String(err && err.message) });
      setTimeout(boot, 5000);
      return;
    }
    runtime.deviceId = (() => {
      try {
        return window.crypto.randomUUID();
      } catch (err) {
        return `wv-${Date.now()}`;
      }
    })();
    reportEvent("bot", "payload started");
    startTokenWatchdog();
    connectLobby();
  }

  window.__wvbotBot = Object.assign(function () {}, {
    alive: true,
    state,
    joinRoom,
    listRooms,
    /** Events the supervisor has not picked up yet. */
    drainEvents() {
      const events = recentEvents.slice();
      recentEvents.length = 0;
      return events;
    },
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
