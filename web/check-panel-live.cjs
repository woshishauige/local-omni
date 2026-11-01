// Focused check of the live generate view: does the workflow card render, do the
// one-click LoRA chips appear with the right availability, and does applying one
// set the composer parameters the accelerator asks for?
//
// Run from Qwen-Local with:  node web/debug-panel.cjs
const path = require("node:path");
const { chromium } = require("./lib/playwright.cjs");

const origin = "http://127.0.0.1:8081";

(async () => {
  const browser = await chromium.launch({ headless: true, channel: "msedge" });
  const context = await browser.newContext({ viewport: { width: 1440, height: 950 } });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(`pageerror: ${error.message}`));
  page.on("console", message => { if (message.type() === "error") errors.push(`console: ${message.text()}`); });
  page.on("response", response => {
    if (response.status() >= 400) console.log("HTTP", response.status(), response.request().method(), response.url());
  });
  try {
    await page.goto(origin + "/", { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1500);
    await page.locator('.local-mode-button[data-mode="generate"]').click();
    await page.locator(".qg-view:not([hidden])").waitFor({ timeout: 20000 });
    await page.waitForTimeout(1200);
    const first = await page.evaluate(() => ({
      pipeline: document.querySelector(".qg-pipeline-select")?.value,
      options: [...document.querySelectorAll(".qg-pipeline-select option")].map(option => option.value),
      rows: document.querySelectorAll(".qg-workflow-row").length,
      presetsHidden: document.querySelector(".qg-presets")?.hidden,
    }));
    console.log("initial:", JSON.stringify(first));
    if (first.pipeline !== "sdxl") {
      await page.locator(".qg-pipeline-select").selectOption("sdxl");
      await page.waitForFunction(() => document.querySelector(".qg-pipeline-select").value === "sdxl");
      await page.waitForTimeout(1500);
    }
    // The workflow card shows the last executed job while its badge says
    // “本次运行”, so the next-run values are read from the gateway payload too.
    const nextRun = await page.evaluate(async () => {
      const payload = await (await fetch("/local/image/workflow?pipeline=sdxl", { cache: "no-store" })).json();
      const row = id => payload.template.find(item => item.id === id);
      const field = (id, input) => row(id).fields.find(item => item.input === input)?.value;
      return {
        lora1: field("11", "lora_name"),
        sampler: field("4", "sampler_name"),
        scheduler: field("4", "scheduler"),
        overrides: payload.overrides,
      };
    });
    console.log("next run:", JSON.stringify(nextRun));
    const sdxl = await page.evaluate(() => ({
      rows: [...document.querySelectorAll(".qg-workflow-row")].map(row => row.dataset.node),
      chips: [...document.querySelectorAll(".qg-preset-chip")].map(chip => ({
        label: chip.textContent, role: chip.dataset.role, available: chip.dataset.available,
        active: chip.dataset.active, disabled: chip.disabled,
      })),
      hint: document.querySelector(".qg-preset-hint")?.textContent,
      steps: document.querySelector('.qg-field-input[name="steps"]')?.value,
      cfg: document.querySelector('.qg-field-input[name="cfg"]')?.value,
      sampler: document.querySelector('.qg-workflow-row[data-node="4"] .qg-field-input[data-input="sampler_name"]')?.value,
      scheduler: document.querySelector('.qg-workflow-row[data-node="4"] .qg-field-input[data-input="scheduler"]')?.value,
      lora1: document.querySelector('.qg-workflow-row[data-node="11"] select')?.value,
    }));
    console.log("sdxl:", JSON.stringify(sdxl, null, 1));
    const chip = page.locator(".qg-preset-chip", { hasText: "LCM-LoRA" });
    if (await chip.count()) {
      await chip.click();
      await page.waitForTimeout(2000);
      const applied = await page.evaluate(() => ({
        steps: document.querySelector('.qg-field-input[name="steps"]')?.value,
        cfg: document.querySelector('.qg-field-input[name="cfg"]')?.value,
        sampler: document.querySelector('.qg-workflow-row[data-node="4"] .qg-field-input[data-input="sampler_name"]')?.value,
        lora1: document.querySelector('.qg-workflow-row[data-node="11"] select')?.value,
        active: [...document.querySelectorAll(".qg-preset-chip[data-active='true']")].map(node => node.textContent),
        hint: document.querySelector(".qg-preset-hint")?.textContent,
        status: document.querySelector(".qg-status-line")?.textContent,
      }));
      console.log("applied lcm:", JSON.stringify(applied, null, 1));
      await page.screenshot({ path: path.join(__dirname, "../logs/image-panel-validation/presets-live.png"), fullPage: true });
      await page.locator(".qg-preset-clear").click();
      await page.waitForTimeout(4000);
      const cleared = await page.evaluate(() => ({
        steps: document.querySelector('.qg-field-input[name="steps"]')?.value,
        sampler: document.querySelector('.qg-workflow-row[data-node="4"] .qg-field-input[data-input="sampler_name"]')?.value,
        active: document.querySelectorAll(".qg-preset-chip[data-active='true']").length,
      }));
      const refetched = await page.evaluate(async () => {
        const payload = await (await fetch("/local/image/workflow?pipeline=sdxl", { cache: "no-store" })).json();
        const row = payload.template.find(item => item.id === "4");
        return row.fields.find(field => field.input === "sampler_name").value;
      });
      console.log("cleared:", JSON.stringify(cleared), "backend sampler:", refetched);
      // This check clears the saved recipe on purpose, so put it back: after the
      // check the machine must look the same as before it.
      const restored = await page.evaluate(async () => {
        for (const key of ["lcm", "photoreal"]) {
          await fetch("/local/image/presets", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ pipeline: "sdxl", key }),
          });
        }
        const payload = await (await fetch("/local/image/workflow?pipeline=sdxl", { cache: "no-store" })).json();
        return payload.presets.filter(preset => preset.active).map(preset => preset.key);
      });
      console.log("restored presets:", JSON.stringify(restored));
    }
    console.log("errors:", JSON.stringify(errors));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
