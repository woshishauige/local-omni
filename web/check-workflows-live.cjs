// Live check of the one-click workflow switcher: read the chips and the
// description, switch twice, and report what the panel and the gateway did.
//
//   node web/check-workflows-live.cjs
const { chromium } = require("./lib/playwright.cjs");

const origin = "http://127.0.0.1:8081";

const readPanel = () => ({
  chips: [...document.querySelectorAll(".qg-switch-chip")].map(chip => ({
    label: chip.textContent, active: chip.dataset.active, available: chip.dataset.available, disabled: chip.disabled,
  })),
  note: document.querySelector(".qg-switch-note")?.textContent,
  pipeline: document.querySelector(".qg-pipeline-select")?.value,
  steps: document.querySelector('.qg-field-input[name="steps"]')?.value,
  cfg: document.querySelector('.qg-field-input[name="cfg"]')?.value,
  denoise: document.querySelector('.qg-field-input[name="denoise"]')?.value,
  lora1: document.querySelector('.qg-workflow-row[data-node="11"] select')?.value,
  lora2: document.querySelector('.qg-workflow-row[data-node="12"] select')?.value,
  status: document.querySelector(".qg-status-line")?.textContent,
});

(async () => {
  const browser = await chromium.launch({ headless: true, channel: "msedge" });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(`pageerror: ${error.message}`));
  page.on("console", message => { if (message.type() === "error") errors.push(`console: ${message.text()}`); });
  try {
    await page.goto(origin + "/", { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1500);
    await page.locator('.local-mode-button[data-mode="generate"]').click();
    await page.locator(".qg-view:not([hidden])").waitFor({ timeout: 20000 });
    await page.waitForFunction(() => document.querySelectorAll(".qg-switch-chip").length > 0, null, { timeout: 20000 });
    await page.waitForTimeout(800);
    console.log("initial:", JSON.stringify(await page.evaluate(readPanel), null, 1));

    for (const label of ["极速试稿", "全量精修", "写实人像 · 推荐"]) {
      await page.locator(".qg-switch-chip", { hasText: label }).click();
      await page.waitForFunction(() => {
        const chip = document.querySelector(".qg-switch-chip[data-active='true']");
        return Boolean(chip) && chip.textContent.includes(window.__expected || "");
      }, null, { timeout: 15000 }).catch(() => {});
      await page.waitForTimeout(1500);
      const state = await page.evaluate(readPanel);
      const active = state.chips.filter(chip => chip.active === "true").map(chip => chip.label);
      console.log(`switched to ${label}: active=${JSON.stringify(active)} steps=${state.steps} cfg=${state.cfg} `
        + `denoise=${state.denoise} lora1=${state.lora1} lora2=${state.lora2} status=${state.status}`);
    }
    const gateway = await page.evaluate(async () => {
      const payload = await (await fetch("/local/image/workflows", { cache: "no-store" })).json();
      return { current: payload.current, workflows: payload.workflows.map(item => ({ key: item.key, label: item.label, active: item.active })) };
    });
    console.log("gateway:", JSON.stringify(gateway, null, 1));
    console.log("errors:", JSON.stringify([...new Set(errors)].slice(0, 6)));
    await page.screenshot({ path: "logs/image-panel-validation/workflow-switcher.png", fullPage: true });
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
