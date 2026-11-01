// Diagnose three reported layout problems in one browser run:
//   1. the image panel's 超分 view showing two highlighted buttons,
//   2. the image panel's seed field too narrow to read,
//   3. the video panel's 视频拼接 pane layout.
const fs = require("node:fs");
const { chromium } = require("../lib/playwright.cjs");

const origin = "http://127.0.0.1:8081";
const OUT = "D:/AI/Qwen-Local/logs/denoise-check";
const admin = fs.readFileSync("D:/AI/Qwen-Local/logs/admin.token", "utf8").trim();

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: "msedge" });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });
  // The gateway clocks off when the last owner page closes (收工): page-lifecycle.js
  // sends a sendBeacon on unload, and a scripted page must not fire it — an empty body
  // keeps the owner's service and ComfyUI running after the check finished.
  await page.route('**/page-lifecycle.js*', route =>
    route.fulfill({ body: '', contentType: 'application/javascript' }));
  await page.route('**/local/lifecycle/page-closed', route => route.abort());

  const report = {};
  try {
    await page.goto(`${origin}/?admin=${encodeURIComponent(admin)}`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1200);
    await page.goto(origin + "/", { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1200);

    // -- image panel ---------------------------------------------------------
    await page.locator(".local-mode-button", { hasText: "生图" }).first().click();
    await page.waitForTimeout(2500);
    await page.evaluate(() => {
      [...document.querySelectorAll(".qg-submode")].find(node => node.textContent.trim() === "超分")?.click();
    });
    await page.waitForTimeout(1800);
    report.imageUpscale = await page.evaluate(() => {
      const describe = node => ({
        text: node.textContent.trim(),
        active: node.dataset.active,
        aria: node.getAttribute("aria-pressed"),
        pressedClass: node.className,
      });
      const seedGroup = document.querySelector(".qg-input-group");
      const seedInput = seedGroup?.querySelector("input");
      const composerActions = [...document.querySelectorAll(".qg-actions button")]
        .map(node => ({ text: node.textContent.trim(), cls: node.className }));
      return {
        submodes: [...document.querySelectorAll(".qg-submode")].map(describe),
        recipes: [...document.querySelectorAll(".qg-switch-row button")].map(describe),
        seed: seedInput ? {
          value: seedInput.value,
          clientWidth: seedInput.clientWidth,
          scrollWidth: seedInput.scrollWidth,
          box: seedInput.getBoundingClientRect().width,
          groupBox: seedGroup.getBoundingClientRect().width,
        } : null,
        composerButtons: composerActions,
      };
    });
    await page.screenshot({ path: `${OUT}/diag-image-upscale.png` });

    await page.evaluate(() => {
      [...document.querySelectorAll(".qg-submode")].find(node => node.textContent.trim() === "生成图片")?.click();
    });
    await page.waitForTimeout(1200);
    report.imageGenerate = await page.evaluate(() => {
      const describe = node => ({ text: node.textContent.trim(), active: node.dataset.active });
      const seed = document.querySelector(".qg-input-group input");
      return {
        submodes: [...document.querySelectorAll(".qg-submode")].map(describe),
        recipes: [...document.querySelectorAll(".qg-switch-row button")].map(describe).slice(0, 6),
        seed: seed ? { value: seed.value, clientWidth: seed.clientWidth, scrollWidth: seed.scrollWidth } : null,
      };
    });
    await page.screenshot({ path: `${OUT}/diag-image-generate.png` });

    // -- video panel: the stitching pane -------------------------------------
    await page.locator(".local-mode-button", { hasText: "视频" }).first().click();
    await page.waitForTimeout(2500);
    await page.evaluate(() => {
      [...document.querySelectorAll(".qv-submode")].find(node => node.textContent.includes("拼接"))?.click();
    });
    await page.waitForTimeout(1000);
    report.videoStitch = await page.evaluate(() => {
      const pane = [...document.querySelectorAll(".qv-pane")][3];
      const card = pane.querySelector(".qv-stitch");
      const slots = [...pane.querySelectorAll(".qv-stitch-slot")].map(slot => {
        const box = slot.getBoundingClientRect();
        const input = slot.querySelector("input[type=file]");
        const name = slot.querySelector(".qv-file-name");
        return {
          box: { x: Math.round(box.x), y: Math.round(box.y), w: Math.round(box.width), h: Math.round(box.height) },
          inputBox: input ? { w: Math.round(input.getBoundingClientRect().width),
                              h: Math.round(input.getBoundingClientRect().height) } : null,
          nameBox: name ? { w: Math.round(name.getBoundingClientRect().width),
                            text: name.textContent.trim(),
                            overflow: name.scrollWidth > name.clientWidth } : null,
        };
      });
      return {
        paneWidth: Math.round(pane.getBoundingClientRect().width),
        cardWidth: card ? Math.round(card.getBoundingClientRect().width) : null,
        slots,
        buttons: [...pane.querySelectorAll("button")].map(node => node.textContent.trim()),
        note: pane.querySelector(".qv-note")?.textContent.trim().slice(0, 60),
      };
    });
    await page.screenshot({ path: `${OUT}/diag-video-stitch.png` });
  } catch (failure) {
    report.failure = failure.message;
  }
  fs.writeFileSync(`${OUT}/diag.json`, JSON.stringify(report, null, 2), "utf8");
  console.log(`wrote ${OUT}/diag.json`);
  await browser.close();
})();
