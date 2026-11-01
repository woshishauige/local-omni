// Focused live check of the video panel's 超分优化: switch the sub-mode, pick a
// clip, choose the 1.25× recipe and confirm the submitted body carries the scale.
const fs = require("node:fs");
const { chromium } = require("../lib/playwright.cjs");

const origin = "http://127.0.0.1:8081";
const CLIP = "D:/AI/ComfyUI-BSAI_v010/ComfyUI/input/upscale-test-1s.mp4";
const OUT = "D:/AI/Qwen-Local/logs/upscale-check";

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: "msedge" });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  // The gateway clocks off when the last owner page closes (收工): page-lifecycle.js
  // sends a sendBeacon on unload, and a scripted page must not fire it — an empty body
  // keeps the owner's service and ComfyUI running after the check finished.
  await page.route('**/page-lifecycle.js*', route =>
    route.fulfill({ body: '', contentType: 'application/javascript' }));
  await page.route('**/local/lifecycle/page-closed', route => route.abort());

  const posted = [];
  page.on("request", request => {
    if (request.method() === "POST" && request.url().includes("/local/image/generate")) {
      try { posted.push(request.postDataJSON()); } catch { posted.push({ unparsed: true }); }
    }
  });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  try {
    await page.goto(origin + "/", { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1500);
    await page.locator(".local-mode-button", { hasText: "视频" }).first().click();
    await page.waitForTimeout(2500);

    const switchToUpscale = async () => {
      await page.evaluate(() => {
        const tab = [...document.querySelectorAll(".qv-submode")].find(node => node.textContent.includes("超分优化"));
        if (tab) tab.click();
      });
      await page.waitForTimeout(700);
      return page.evaluate(() => {
        const panes = [...document.querySelectorAll(".qv-pane")];
        return panes.length > 1 && !panes[1].hidden;
      });
    };
    let onUpscale = await switchToUpscale();
    if (!onUpscale) onUpscale = await switchToUpscale();
    console.log("upscale sub-mode active:", onUpscale);

    await page.locator('input[type=file][accept*="video"]').setInputFiles(CLIP);
    await page.waitForTimeout(2500);
    if (!(await page.evaluate(() => ![...document.querySelectorAll(".qv-pane")][1].hidden))) await switchToUpscale();
    const view = await page.evaluate(() => ({
      recipes: [...document.querySelectorAll(".qv-pane:not([hidden]) .qv-recipes button")].map(node => node.textContent.trim()),
      note: document.querySelector(".qv-pane:not([hidden]) .qv-note")?.textContent?.slice(0, 60),
    }));
    console.log("video upscale row:", JSON.stringify(view));
    await page.screenshot({ path: OUT + "/video-upscale-panel.png" });

    const sharp = page.locator(".qv-pane:not([hidden]) .qv-recipes button", { hasText: "清晰度最佳" });
    if (await sharp.count()) await sharp.first().click();
    await page.waitForTimeout(400);
    await page.locator(".qv-pane:not([hidden]) .qv-button", { hasText: "开始放大" }).click();
    await page.waitForTimeout(6000);
    const status = await page.evaluate(() => [...document.querySelectorAll(".qv-pane:not([hidden]) p")].map(node => node.textContent).join(" | ").slice(0, 200));
    console.log("status:", status);
    console.log("submitted bodies:", JSON.stringify(posted, null, 1));
    await page.screenshot({ path: OUT + "/video-upscale-submitted.png" });
    console.log("errors:", JSON.stringify([...new Set(errors)].slice(0, 4)));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
