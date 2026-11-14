/**
 * Diagnostic page scraper using Playwright from ../web/node_modules or NODE_PATH.
 */
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

function loadPlaywright() {
  const candidates = [
    process.env.PLAYWRIGHT_PATH,
    "playwright",
    path.resolve(__dirname, "../web/node_modules/playwright/index.js"),
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      return require(candidate);
    } catch {
      // try next resolution candidate
    }
  }
  throw new Error("Could not resolve 'playwright'. Install it in web/ (`npm install`) or set PLAYWRIGHT_PATH.");
}

async function main() {
  const { chromium } = loadPlaywright();
  const outDir = path.resolve(process.env.OUT_DIR || path.join(__dirname, "../.scratch/rxzbot-scrape"));
  fs.mkdirSync(outDir, { recursive: true });

  const browser = await chromium.launch({
    headless: false,
    args: ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
  });
  const page = await browser.newPage({ viewport: { width: 1200, height: 1000 } });

  const targets = ["https://www.rxzbot.com/"];
  try {
    for (const url of targets) {
      try {
        const response = await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });
        await page.waitForTimeout(9000);
        console.log(`=== ${url} -> ${response && response.status()} ===`);
        console.log("final url:", page.url());
        const info = await page.evaluate(() => {
          const text = (document.body.innerText || "").replace(/\n{2,}/g, "\n").slice(0, 4000);
          const links = Array.from(document.querySelectorAll("a"))
            .map((a) => `${(a.innerText || "").trim()} -> ${a.href}`)
            .filter((l) => l.split(" -> ")[0])
            .slice(0, 60);
          const headings = Array.from(document.querySelectorAll("h1,h2,h3"))
            .map((h) => h.innerText.trim())
            .filter(Boolean)
            .slice(0, 40);
          return { title: document.title, text, links, headings };
        });
        console.log("TITLE:", info.title);
        console.log("\nHEADINGS:\n" + info.headings.join("\n"));
        console.log("\nTEXT:\n" + info.text);
        console.log("\nLINKS:\n" + info.links.join("\n"));
        const screenshotPath = path.join(outDir, "rxzbot.png");
        await page.screenshot({ path: screenshotPath, fullPage: false });
        console.log("Screenshot saved to:", screenshotPath);
      } catch (err) {
        console.log(`=== ${url} FAILED: ${err.message}`);
      }
    }
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
