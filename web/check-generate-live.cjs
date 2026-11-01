// End-to-end check through the real UI: apply the LCM accelerator in the panel,
// type a prompt, press 生成, and report the job the gateway actually ran plus the
// image it produced. Run from Qwen-Local with:  node web/check-generate-live.cjs
const { chromium } = require("./lib/playwright.cjs");

const origin = "http://127.0.0.1:8081";
const pipeline = process.env.DSH_CHECK_PIPELINE || "sdxl";
const preset = process.env.DSH_CHECK_PRESET || "LCM-LoRA";
const prompt = "photo of a handsome young asian man, athletic build, natural sunlight, detailed skin texture, looking at camera, 50mm portrait";
const negative = "lowres, bad anatomy, bad hands, watermark, text";

(async () => {
  const browser = await chromium.launch({ headless: true, channel: "msedge" });
  const context = await browser.newContext({ viewport: { width: 1440, height: 950 } });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  try {
    await page.goto(origin + "/", { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1500);
    await page.locator('.local-mode-button[data-mode="generate"]').click();
    await page.locator(".qg-view:not([hidden])").waitFor({ timeout: 20000 });
    // Force a real switch so the pipeline's own defaults (832x1216 for SDXL) load.
    await page.locator(".qg-pipeline-select").selectOption("flux2");
    await page.waitForFunction(() => document.querySelector('.qg-workflow-row[data-node="13"]'), null, { timeout: 20000 });
    await page.locator(".qg-pipeline-select").selectOption(pipeline);
    await page.waitForFunction(() => document.querySelector('.qg-workflow-row[data-node="10"] select'), null, { timeout: 20000 });
    await page.locator(".qg-preset-chip", { hasText: preset }).click();
    await page.waitForFunction(() => document.querySelector(".qg-preset-hint").textContent.includes("LCM"), null, { timeout: 15000 });
    await page.locator(".qg-prompt-input").fill(prompt);
    await page.locator(".qg-negative-input").fill(negative);
    const settings = await page.evaluate(() => ({
      steps: document.querySelector('.qg-field-input[name="steps"]').value,
      cfg: document.querySelector('.qg-field-input[name="cfg"]').value,
      width: document.querySelector('.qg-field-input[name="width"]').value,
      height: document.querySelector('.qg-field-input[name="height"]').value,
      seed: document.querySelector('.qg-field-input[name="seed"]').value,
      lora: document.querySelector('.qg-workflow-row[data-node="11"] select')?.value,
    }));
    console.log("panel settings:", JSON.stringify(settings));
    const before = await page.evaluate(async () => {
      const payload = await (await fetch("/local/image/history", { cache: "no-store" })).json();
      const newest = [...payload.entries].sort((a, b) => b.time - a.time)[0];
      return { count: payload.entries.length, id: newest ? newest.id : null };
    });
    await page.getByRole("button", { name: "生成图片" }).click();
    // A new history record is the proof that this click produced an image.
    await page.waitForFunction(async seen => {
      const payload = await (await fetch("/local/image/history", { cache: "no-store" })).json();
      return payload.entries.some(entry => entry.id !== seen.id && entry.state !== "running");
    }, before, { timeout: 600000, polling: 2000 });
    await page.waitForTimeout(2500);
    const record = await page.evaluate(async () => {
      const payload = await (await fetch("/local/image/history", { cache: "no-store" })).json();
      const entry = [...payload.entries].sort((a, b) => b.time - a.time)[0];
      return { state: entry.state, params: entry.params, timings: entry.timings, images: entry.images.length, error: entry.error };
    });
    console.log("gateway record:", JSON.stringify(record));
    const result = await page.evaluate(() => {
      const cards = [...document.querySelectorAll(".qg-history-item")];
      const latest = cards[cards.length - 1];
      const metrics = [...document.querySelectorAll(".qg-stat-value")].map(node => node.textContent);
      return {
        state: latest?.dataset.state,
        images: latest?.querySelectorAll("img").length,
        thumb: latest?.querySelector("img")?.src?.slice(0, 60),
        elapsed: metrics[0],
        sampling: metrics[3],
        progress: document.querySelector(".qg-progress-percent")?.textContent,
      };
    });
    console.log("generation:", JSON.stringify(result));
    console.log("page errors:", JSON.stringify(errors));
    await page.screenshot({ path: "logs/image-panel-validation/live-generate.png", fullPage: true });
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
