// Live check for the panel's route row: three routes, no 20-step entry, and the first one
// selected — the panel reads the list from the gateway, so the DOM is the proof.
const fs = require("node:fs");
const { chromium } = require("../lib/playwright.cjs");

const origin = "http://127.0.0.1:8081";
const OUT = "D:/AI/Qwen-Local/logs/denoise-check";
const admin = fs.readFileSync("D:/AI/Qwen-Local/logs/admin.token", "utf8").trim();

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: "msedge" });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  // The gateway clocks off when the last owner page closes (收工): page-lifecycle.js
  // sends a sendBeacon on unload, and a scripted page must not fire it — an empty body
  // keeps the owner's service and ComfyUI running after the check finished.
  await page.route('**/page-lifecycle.js*', route =>
    route.fulfill({ body: '', contentType: 'application/javascript' }));
  await page.route('**/local/lifecycle/page-closed', route => route.abort());

  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const report = {};
  try {
    await page.goto(`${origin}/?admin=${encodeURIComponent(admin)}`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1200);
    await page.goto(origin + "/", { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1500);
    await page.locator(".local-mode-button", { hasText: "视频" }).first().click();
    await page.waitForTimeout(3000);
    report.recipes = await page.$$eval(".qv-recipe", nodes => nodes.map(node => node.textContent.trim()));
    report.selected = await page.$$eval(".qv-recipe[aria-pressed='true']",
                                        nodes => nodes.map(node => node.textContent.trim()));
    report.mentions20 = (await page.evaluate(() => document.body.innerText)).includes("20 步蒸馏");
    report.estimate = await page.evaluate(() => {
      const tile = [...document.querySelectorAll(".qv-metric")]
        .find(node => node.textContent.includes("整片预估"));
      return tile ? tile.querySelector(".qv-metric-value")?.textContent.trim() : null;
    });
    await page.screenshot({ path: `${OUT}/video-routes.png` });
  } catch (failure) {
    report.failure = failure.message;
  }
  report.pageErrors = errors;
  fs.writeFileSync(`${OUT}/video-routes.json`, JSON.stringify(report, null, 2), "utf8");
  console.log(JSON.stringify(report, null, 2));
  await browser.close();
})();
