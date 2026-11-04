/**
 * Open the game in a browser and wait until you are logged in.
 *
 *   node login.mjs
 *
 * A normal Chrome window opens.  Log in there (consent, START GAME, sign
 * in - whatever the site asks for).  As soon as the session is there, the
 * script saves it and prints the refresh token:
 *
 *   state/auth.json   the browser session (cookies + local storage)
 *   state/tokens.json the firebase tokens
 *
 * The refresh token is what the phone runner needs: it can mint fresh id
 * tokens without a browser.
 */

import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STATE_DIR = path.join(HERE, "state");
const AUTH_FILE = path.join(STATE_DIR, "auth.json");
const TOKENS_FILE = path.join(STATE_DIR, "tokens.json");
const PROFILE_DIR = path.join(STATE_DIR, "profile");
const GAME_URL = "https://www.wolvesville.com/";

async function readTokens(page) {
  return page.evaluate(() => {
    const raw = window.localStorage.getItem("authtokens");
    if (raw) {
      try {
        const parsed = JSON.parse(raw);
        if (parsed.idToken) {
          return { idToken: parsed.idToken, refreshToken: parsed.refreshToken || null };
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
          return { idToken: manager.accessToken, refreshToken: manager.refreshToken || null };
        }
      } catch (err) {
        /* ignore */
      }
    }
    return null;
  });
}

/**
 * Get the durable credential.
 *
 * The web client only keeps the idToken (good for an hour) in
 * localStorage.  The refreshToken comes from the sign-in endpoint, which
 * needs the Cloudflare attestation the browser holds - so it is called
 * from inside the page.
 */
async function fetchRefreshToken(page, email, password) {
  return page.evaluate(
    async ({ mail, pass }) => {
      const cfJwt = window.localStorage.getItem("cloudflare-turnstile-jwt");
      const headers = { Accept: "application/json", "Content-Type": "application/json" };
      if (cfJwt) headers["Cf-JWT"] = cfJwt;
      const response = await fetch(
        "https://auth.api-wolvesville.com/players/signInWithEmailAndPassword",
        {
          method: "POST",
          headers,
          body: JSON.stringify({ email: mail, password: pass }),
        },
      );
      if (!response.ok) {
        return { error: `sign-in failed: HTTP ${response.status}` };
      }
      const data = await response.json();
      if (!data || !data.refreshToken) {
        return { error: "the server returned no refreshToken" };
      }
      return { idToken: data.idToken, refreshToken: data.refreshToken };
    },
    { mail: email, pass: password },
  );
}

async function main() {
  mkdirSync(STATE_DIR, { recursive: true });

  // A persistent profile keeps the session for the next run.
  const context = await chromium.launchPersistentContext(PROFILE_DIR, {
    headless: false,
    viewport: { width: 1100, height: 900 },
    args: ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
  });
  const pages = context.pages();
  const page = pages[0] || (await context.newPage());

  console.log("Opening the game - please log in in the browser window.");
  console.log("The script keeps watching and saves the session as soon as you are in.\n");
  await page.goto(GAME_URL, { waitUntil: "domcontentloaded" });

  const deadline = Date.now() + 15 * 60 * 1000;
  let tokens = null;
  while (Date.now() < deadline) {
    tokens = await readTokens(page).catch(() => null);
    if (tokens) break;
    await page.waitForTimeout(2000);
  }

  if (!tokens) {
    console.log("No session found - login did not finish.");
    await context.close();
    process.exit(1);
  }

  // The durable credential: mint it from the sign-in endpoint.
  if (!tokens.refreshToken) {
    const email = process.env.WV_EMAIL;
    const password = process.env.WV_PASSWORD;
    if (email && password) {
      const result = await fetchRefreshToken(page, email, password).catch((err) => ({
        error: String(err),
      }));
      if (result && result.refreshToken) {
        tokens = { idToken: result.idToken, refreshToken: result.refreshToken };
      } else {
        console.log("Could not get a refreshToken:", result && result.error);
      }
    }
  }

  if (!tokens.refreshToken) {
    console.log("No refreshToken - the bot would stop after one hour.");
    await context.close();
    process.exit(1);
  }

  await context.storageState({ path: AUTH_FILE });
  writeFileSync(
    TOKENS_FILE,
    JSON.stringify({ ...tokens, savedAt: Date.now() }, null, 2),
  );
  console.log(`session saved to ${AUTH_FILE}`);
  console.log("\n=== refresh token (for the phone) ===");
  console.log(tokens.refreshToken);
  await context.close();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
