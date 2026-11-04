import pkg from "file:///D:/Project/android-device/wv-bot/web/node_modules/playwright/index.js";
const { chromium } = pkg;

const browser = await chromium.launch({
  headless: false,
  args: ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
});
const page = await browser.newPage({ viewport: { width: 1200, height: 1000 } });

const targets = ["https://www.rxzbot.com/"];
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
    await page.screenshot({ path: "D:/Temp/rxzbot-scrape/rxzbot.png", fullPage: false });
  } catch (err) {
    console.log(`=== ${url} FAILED: ${err.message}`);
  }
}
await browser.close();
