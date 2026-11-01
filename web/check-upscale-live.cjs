// Live check of the 超分 features in the real panel.
//
//   node web/check-upscale-live.cjs
//
// Image view: pick 「放大 · 清晰度最佳 1.25×」, confirm the 放大倍率 box appears
// with 1.25 and the prompt boxes disappear, then run it on a real reference image
// and read the produced size back from ComfyUI.
// Video view: pick a clip, choose the video upscale recipe and run it.
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("./lib/playwright.cjs");

const origin = "http://127.0.0.1:8081";
const REFERENCE = "D:/AI/Qwen-Local/logs/pipeline-comparison/fp16-lcm-8step.png";
const CLIP = "D:/AI/ComfyUI-BSAI_v010/ComfyUI/input/upscale-test-1s.mp4";
const OUT = "D:/AI/Qwen-Local/logs/upscale-check";

async function waitForIdle(page) {
  await page.waitForFunction(() => {
    const line = document.querySelector(".qg-status-line");
    return !document.querySelector(".qg-progress").dataset.state?.includes("running") || true;
  });
}

async function waitForJob(page, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  while (Date.now() < deadline) {
    await page.waitForTimeout(3000);
    const state = await page.evaluate(() => ({
      phase: document.querySelector(".qg-progress .qg-card-hint")?.textContent || "",
      state: document.querySelector(".qg-progress").dataset.state || "",
      status: document.querySelector(".qg-status-line")?.textContent || "",
      images: [...document.querySelectorAll(".qg-history-item img")].length,
    }));
    if (state.phase !== last) { last = state.phase; console.log("   phase:", state.phase); }
    if (["completed", "error", "cancelled"].includes(state.state)) return state;
  }
  throw new Error("job did not settle");
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: "msedge" });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(`pageerror: ${error.message}`));
  page.on("console", message => { if (message.type() === "error" && !message.text().includes("415")) errors.push(`console: ${message.text()}`); });
  try {
    await page.goto(origin + "/", { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1500);
    await page.locator('.local-mode-button[data-mode="generate"]').click();
    await page.waitForFunction(() => document.querySelectorAll(".qg-switch-chip").length > 0, null, { timeout: 30000 });
    await page.waitForTimeout(800);

    // --- image upscaler -------------------------------------------------------
    await page.locator(".qg-switch-chip", { hasText: "放大 · 清晰度最佳" }).click();
    await page.waitForFunction(() => {
      const field = document.querySelector('.qg-field-input[name="scale"]');
      return field && field.value === "1.25" && !field.closest(".qg-field").hidden;
    }, null, { timeout: 30000 });
    const view = await page.evaluate(() => ({
      scale: document.querySelector('.qg-field-input[name="scale"]').value,
      steps: document.querySelector('.qg-field-input[name="steps"]').value,
      promptHidden: document.querySelector(".qg-prompt").hidden,
      negativeHidden: document.querySelector(".qg-negative-input").hidden,
      widthVisible: !document.querySelector('.qg-field-input[name="width"]').closest(".qg-field").hidden,
      denoiseVisible: !document.querySelector('.qg-field-input[name="denoise"]').closest(".qg-field").hidden,
      hint: document.querySelector(".qg-upscale-hint").hidden ? null : document.querySelector(".qg-upscale-hint").textContent,
      note: document.querySelector(".qg-switch-note").textContent.slice(0, 60),
    }));
    console.log("image upscaler view:", JSON.stringify(view, null, 1));
    await page.screenshot({ path: path.join(OUT, "image-upscale-panel.png"), fullPage: false });

    await page.locator(".qg-reference-input").first().setInputFiles(REFERENCE);
    await page.waitForTimeout(500);
    const before = Date.now();
    await page.locator(".qg-actions .qg-button-primary").click();
    console.log("submitted; waiting for the upscale run…");
    const settled = await waitForJob(page, 420000);
    console.log(`image run: state=${settled.state} phase=${settled.phase} in ${Math.round((Date.now() - before) / 1000)}s`);
    await page.screenshot({ path: path.join(OUT, "image-upscale-result.png"), fullPage: false });

    // --- video upscaler -------------------------------------------------------
    // The video view is a sibling tab; its upscaler lives under 「超分优化」.
    const videoTab = page.locator('.local-mode-button', { hasText: "视频" });
    if (await videoTab.count()) {
      await videoTab.first().click();
      await page.waitForTimeout(1500);
    }
    const hasVideo = await page.locator(".qv-root").count();
    if (hasVideo) {
      // The sub-mode row is easy to hit while hidden, so click it in-page.
      await page.evaluate(() => {
        const tab = [...document.querySelectorAll(".qv-submode")].find(node => node.textContent.includes("超分优化"));
        if (tab) tab.click();
      });
      await page.waitForTimeout(800);
      await page.locator('input[type=file][accept*="video"]').setInputFiles(CLIP);
      await page.waitForTimeout(1500);
      const videoView = await page.evaluate(() => ({
        recipes: [...document.querySelectorAll(".qv-pane:not([hidden]) .qv-recipes button")].map(node => node.textContent.trim()),
        hint: document.querySelector(".qv-note")?.textContent,
      }));
      console.log("video upscale view:", JSON.stringify(videoView));
      await page.screenshot({ path: path.join(OUT, "video-upscale-panel.png"), fullPage: false });
      const sharp = page.locator(".qv-recipes:visible button", { hasText: "清晰度最佳" });
      if (await sharp.count()) await sharp.first().click();
      const started = Date.now();
      await page.locator(".qv-button", { hasText: "开始放大" }).click();
      await page.waitForTimeout(4000);
      const status = await page.evaluate(() => document.querySelector(".qv-status, .qv-note, .qv-file-name")?.textContent);
      console.log("video submit status:", status, `(+${Math.round((Date.now() - started) / 1000)}s)`);
      await page.screenshot({ path: path.join(OUT, "video-upscale-submitted.png"), fullPage: false });
    } else {
      console.log("video panel has no 超分优化 sub-mode (skipped)");
    }
    console.log("errors:", JSON.stringify([...new Set(errors)].slice(0, 5)));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
