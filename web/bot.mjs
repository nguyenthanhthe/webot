/**
 * Wolvesville web bot - headless browser + injected websocket client.
 *
 * The browser does what only a browser can do (Cloudflare, Turnstile, the
 * login).  Everything else is done by `payload.js`, which is injected into
 * the page and talks to the game server over socket.io - the same technique
 * the Zoro extension uses.
 *
 *   node bot.mjs              run the bot (headless)
 *   node bot.mjs --login      log in once with a visible browser, then exit
 *   node bot.mjs --headed     run with a visible browser (debugging)
 */

import { chromium } from "playwright";
import { createDashboard } from "./dashboard.mjs";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STATE_DIR = path.join(HERE, "state");
const AUTH_FILE = path.join(STATE_DIR, "auth.json");
const CONFIG_FILE = path.join(HERE, "config.json");

const GAME_URL = "https://www.wolvesville.com/";

const argv = process.argv.slice(2);
const LOGIN_ONLY = argv.includes("--login");
const TOKENS_ONLY = argv.includes("--tokens");
const HEADED = argv.includes("--headed") || LOGIN_ONLY || TOKENS_ONLY;

const TOKENS_FILE = path.join(HERE, "state", "tokens.json");
const PROFILE_DIR = path.join(HERE, "state", "profile");

/**
 * The tokens of the last manual login.
 *
 * The refreshToken is the durable credential: it can mint new idTokens
 * forever, so the bot survives an expired session.
 */
function loadTokens() {
  if (!existsSync(TOKENS_FILE)) return null;
  try {
    return JSON.parse(readFileSync(TOKENS_FILE, "utf-8"));
  } catch (err) {
    return null;
  }
}

/** Keep the firebase tokens around: the phone runner refreshes with them. */
function saveTokens(session) {
  if (!session || !session.refreshToken) return null;
  mkdirSync(STATE_DIR, { recursive: true });
  const payload = {
    idToken: session.idToken,
    refreshToken: session.refreshToken,
    savedAt: Date.now(),
  };
  writeFileSync(TOKENS_FILE, JSON.stringify(payload, null, 2));
  return payload;
}

function loadConfig() {
  const defaults = {
    email: "",
    password: "",
    // The durable credential from a manual login (state/tokens.json).
    refresh_token: "",
    room_names: ["vill win"],
    language: "en",
    game_mode: "custom",
    dashboard: { host: "127.0.0.1", port: 8787 },
    headless: true,
    reconnect_delay_sec: 5,
    login_timeout_sec: 120,
    claim_double_xp: true,
  };
  if (!existsSync(CONFIG_FILE)) return defaults;
  const raw = readFileSync(CONFIG_FILE, "utf-8");
  try {
    return { ...defaults, ...JSON.parse(raw) };
  } catch (err) {
    console.error("config.json is not valid JSON:", err.message);
    return defaults;
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* ------------------------------------------------------------------ */
/* login                                                               */
/* ------------------------------------------------------------------ */

async function readSession(page) {
  return page.evaluate(() => {
    const raw = window.localStorage.getItem("authtokens");
    if (raw) {
      try {
        const parsed = JSON.parse(raw);
        if (parsed.idToken) {
          return {
            source: "authtokens",
            idToken: parsed.idToken,
            refreshToken: parsed.refreshToken || null,
          };
        }
      } catch (err) {
        /* ignore */
      }
    }
    for (const key of Object.keys(window.localStorage)) {
      const value = window.localStorage.getItem(key);
      if (!value || value.indexOf("accessToken") === -1) continue;
      try {
        const parsed = JSON.parse(value);
        const manager = parsed.stsTokenManager || parsed;
        if (manager.accessToken) {
          return {
            source: key,
            idToken: manager.accessToken,
            refreshToken: manager.refreshToken || null,
          };
        }
      } catch (err) {
        /* ignore */
      }
    }
    return null;
  });
}

/**
 * Log in by hand in a visible browser: no password is ever written to a
 * file, and Cloudflare's Turnstile is solved by the human (or by the
 * browser, if it is invisible).  The resulting session is stored so the
 * bot can run headless afterwards.
 */
async function loginInteractive(context, page, cfg, dash) {
  dash.addEvent("login", "open the browser and log in - the bot continues automatically");
  await page.goto(GAME_URL, { waitUntil: "domcontentloaded" });

  const deadline = Date.now() + cfg.login_timeout_sec * 1000;
  while (Date.now() < deadline) {
    const session = await readSession(page);
    if (session) {
      dash.addEvent("login", "login detected, saving the session");
      return session;
    }
    await page.waitForTimeout(2000);
  }
  throw new Error("login did not complete in time");
}

/** Click the Turnstile checkbox when it shows up. */
async function solveTurnstile(page) {
  try {
    const frame = page
      .frameLocator("iframe[src*='challenges.cloudflare.com']")
      .first();
    const box = frame.locator("input[type=checkbox], label, .cb-lb").first();
    await box.click({ timeout: 8000, force: true });
    return true;
  } catch (err) {
    return false;
  }
}

/** Log in with the credentials from config.json (used only if they exist). */
async function loginWithCredentials(page, cfg, dash) {
  dash.addEvent("login", "logging in with the credentials from config.json");
  await page.goto(GAME_URL, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(5000);

  // 1. the consent screen (age + terms)
  for (const pattern of [/I am 16/i, /I have read/i, /agree/i]) {
    const item = page.getByText(pattern).first();
    if (await item.count() > 0) {
      await item.click({ timeout: 5000, force: true }).catch(() => {});
      await page.waitForTimeout(500);
    }
  }
  const accept = page.getByText(/^(Accept|Đồng ý|OK|START GAME OK)$/i).first();
  if (await accept.count() > 0) {
    await accept.click({ timeout: 5000, force: true }).catch(() => {});
    await page.waitForTimeout(2500);
  }

  // 2. open the login form
  const trigger = page.getByText(/^(Log in|Log In|Login|Sign in|Sign In|Đăng nhập)$/i).first();
  if (await trigger.count() > 0) {
    await trigger.click({ timeout: 5000, force: true }).catch(() => {});
    await page.waitForTimeout(2500);
  }

  // 3. fill the form
  const email = page.locator('input[type="email"]').first();
  const password = page.locator('input[type="password"]').first();
  await email.waitFor({ state: "visible", timeout: 30000 });
  await email.fill(cfg.email);
  await password.fill(cfg.password);

  // 4. the Turnstile widget
  if (await solveTurnstile(page)) {
    dash.addEvent("login", "turnstile solved");
    await page.waitForTimeout(2000);
  }

  // 5. submit
  await password.press("Enter").catch(() => {});
  const submit = page.getByText(/^(Log in|Log In|Login|Sign in|Continue|Tiếp tục)$/i).first();
  if (await submit.count() > 0) {
    await submit.click({ timeout: 5000, force: true }).catch(() => {});
  }

  const deadline = Date.now() + cfg.login_timeout_sec * 1000;
  while (Date.now() < deadline) {
    const session = await readSession(page);
    if (session) return session;
    await page.waitForTimeout(2000);
  }
  throw new Error("login did not complete in time");
}

/* ------------------------------------------------------------------ */
/* session                                                             */
/* ------------------------------------------------------------------ */

async function runSession(browser, cfg, dash) {
  const storageState = existsSync(AUTH_FILE) ? AUTH_FILE : undefined;
  const context = await browser.newContext({
    storageState,
    viewport: { width: 1280, height: 900 },
    locale: "en-US",
  });
  const page = await context.newPage();

  await page.exposeBinding("__wvbot", async (_source, payload) => {
    if (!payload || typeof payload !== "object") return;
    if (payload.kind === "state") {
      dash.update(payload.state || {});
    } else if (payload.kind === "event") {
      dash.addEvent(payload.level || "info", payload.message || "");
    }
  });

  const tokens = loadTokens();
  const socketIoPath = path.join(HERE, "node_modules", "socket.io-client", "dist", "socket.io.min.js");
  const payloadPath = path.join(HERE, "payload.js");
  await page.addInitScript({
    content: `window.__WV_CONFIG = ${JSON.stringify({
      gameHost: "https://game.api-wolvesville.com",
      authHost: "https://auth.api-wolvesville.com",
      refreshToken: (tokens && tokens.refreshToken) || cfg.refresh_token || null,
      roomNames: cfg.room_names,
      language: cfg.language,
      gameMode: cfg.game_mode,
      claimDoubleXp: cfg.claim_double_xp,
    })};`,
  });
  await page.addInitScript({ path: socketIoPath });
  await page.addInitScript({ path: payloadPath });

  dash.addEvent("bot", "opening the game client");
  await page.goto(GAME_URL, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(5000);

  let session = await readSession(page);
  if (!session) {
    if (cfg.email && cfg.password) {
      session = await loginWithCredentials(page, cfg, dash);
    } else {
      session = await loginInteractive(context, page, cfg, dash);
    }
    mkdirSync(STATE_DIR, { recursive: true });
    await context.storageState({ path: AUTH_FILE });
    dash.addEvent("bot", `session saved to ${AUTH_FILE}`);
  } else {
    dash.addEvent("bot", "session restored from disk");
  }

  dash.update({ status: "starting", last_error: null });

  // The payload drives the game from here; we only watch for trouble.
  page.on("crash", () => {
    dash.addEvent("error", "the page crashed");
  });
  page.on("close", () => {
    dash.addEvent("warn", "the page closed");
  });

  while (!globalThis.__wvbotStop) {
    await page.waitForTimeout(5000);
    const alive = await page.evaluate(() => Boolean(window.__wvbotBot && window.__wvbotBot.alive)).catch(() => false);
    if (!alive) {
      throw new Error("the injected bot stopped answering");
    }
  }

  await context.close();
}

/* ------------------------------------------------------------------ */
/* main                                                                */
/* ------------------------------------------------------------------ */

/** Log in once with a visible browser and store the session. */
async function runLoginOnly(browser, cfg, dash) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  await page.goto(GAME_URL, { waitUntil: "domcontentloaded" });

  let session = await readSession(page);
  if (!session && cfg.email && cfg.password) {
    session = await loginWithCredentials(page, cfg, dash);
  }
  if (!session) {
    session = await loginInteractive(context, page, { ...cfg, login_timeout_sec: 900 }, dash);
  }
  mkdirSync(STATE_DIR, { recursive: true });
  await context.storageState({ path: AUTH_FILE });
  dash.addEvent("login", `session saved to ${AUTH_FILE}`);

  const tokens = saveTokens(session);
  if (tokens) {
    dash.addEvent("login", "tokens saved to state/tokens.json");
    console.log(tokens.refreshToken);
  }
  await context.close();
}

async function main() {
  const cfg = loadConfig();
  const dash = createDashboard(cfg.dashboard);
  await dash.listen();

  const browser = await chromium.launch({
    headless: cfg.headless && !HEADED,
    args: ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
  });

  if (LOGIN_ONLY || TOKENS_ONLY) {
    try {
      await runLoginOnly(browser, cfg, dash);
    } catch (err) {
      dash.update({ status: "error", last_error: String(err && err.message) });
      dash.addEvent("error", String(err && err.message));
      console.error(err && err.message);
    }
    await browser.close();
    await dash.close();
    return;
  }

  let stopping = false;
  const stop = async () => {
    stopping = true;
    globalThis.__wvbotStop = true;
    dash.addEvent("bot", "stopping");
    await browser.close().catch(() => {});
    await dash.close();
    process.exit(0);
  };
  process.on("SIGINT", stop);
  process.on("SIGTERM", stop);

  while (!stopping) {
    try {
      await runSession(browser, cfg, dash);
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      dash.update({
        status: "error",
        last_error: message,
        errors: dash.state.errors + 1,
      });
      dash.addEvent("error", message);
    }
    if (stopping) break;
    dash.update({ reconnects: dash.state.reconnects + 1, status: "reconnecting" });
    dash.addEvent("bot", `restarting in ${cfg.reconnect_delay_sec}s`);
    await sleep(cfg.reconnect_delay_sec * 1000);
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
