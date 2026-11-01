// Live check of the accelerator mismatch warning: reproduce it, fix it with the
// button, and confirm a reload no longer warns about a saved preset.
const fs = require("node:fs");
const { chromium } = require("./lib/playwright.cjs");

const OUT = "D:/AI/Qwen-Local/logs/preset-check";

const readHint = () => {
  const hint = document.querySelector(".qg-preset-hint");
  return {
    warning: hint.dataset.warning === "true",
    text: hint.textContent,
    title: hint.title,
    button: document.querySelector(".qg-preset-restore")?.textContent || null,
    steps: document.querySelector('.qg-field-input[name="steps"]').value,
    cfg: document.querySelector('.qg-field-input[name="cfg"]').value,
    presets: [...document.querySelectorAll(".qg-preset-chip[data-active='true']")].map(chip => chip.textContent.trim()),
  };
};

async function openGenerate(page) {
  await page.goto("http://127.0.0.1:8081/", { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1500);
  await page.locator(".local-mode-button", { hasText: "生图" }).first().click();
  await page.waitForFunction(() => document.querySelectorAll(".qg-preset-chip").length > 0, null, { timeout: 40000 });
  await page.waitForTimeout(1200);
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: "msedge" });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1100 } });
  // The gateway clocks off when the last owner page closes (收工): page-lifecycle.js
  // sends a sendBeacon on unload, and a scripted page must not fire it — an empty body
  // keeps the owner's service and ComfyUI running after the check finished.
  await page.route('**/page-lifecycle.js*', route =>
    route.fulfill({ body: '', contentType: 'application/javascript' }));
  await page.route('**/local/lifecycle/page-closed', route => route.abort());

  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  try {
    // 1. make LCM the active accelerator, then drift away from its recipe.
    await openGenerate(page);
    await page.locator(".qg-preset-chip", { hasText: "LCM-LoRA" }).click();
    await page.waitForFunction(() => document.querySelector(".qg-preset-chip[data-active='true']"));
    await page.waitForTimeout(800);
    console.log("after applying LCM:", JSON.stringify(await page.evaluate(readHint)));

    await page.locator('.qg-field-input[name="steps"]').fill("4");
    await page.locator('.qg-field-input[name="cfg"]').fill("1");
    await page.waitForFunction(() => document.querySelector(".qg-preset-hint").dataset.warning === "true", null, { timeout: 15000 });
    const warning = await page.evaluate(readHint);
    console.log("drifted:", JSON.stringify(warning));
    await page.screenshot({ path: OUT + "/warning.png" });

    // 2. the warning itself carries the fix.
    await page.locator(".qg-preset-restore").click();
    await page.waitForFunction(() => document.querySelector(".qg-preset-hint").dataset.warning === undefined, null, { timeout: 15000 });
    await page.waitForTimeout(600);
    console.log("after 恢复推荐值:", JSON.stringify(await page.evaluate(readHint)));
    await page.screenshot({ path: OUT + "/restored.png" });

    // 3. a saved 4-step accelerator must survive a reload without warning, even
    //    though SDXL's own defaults are LCM-shaped (8 步 / CFG 1.5).
    await page.locator(".qg-switch-chip", { hasText: "极速试稿" }).click();
    await page.waitForFunction(() => {
      const chip = document.querySelector(".qg-switch-chip[data-active='true']");
      return chip && chip.textContent.includes("极速");
    }, null, { timeout: 20000 });
    await page.waitForTimeout(800);
    console.log("极速试稿 applied:", JSON.stringify(await page.evaluate(readHint)));

    await openGenerate(page);
    console.log("after reload:", JSON.stringify(await page.evaluate(readHint)));

    // 4. leave the machine on the recommended recipe.
    await page.locator(".qg-switch-chip", { hasText: "写实人像" }).click();
    await page.waitForFunction(() => {
      const chip = document.querySelector(".qg-switch-chip[data-active='true']");
      return chip && chip.textContent.includes("写实人像");
    }, null, { timeout: 20000 });
    await page.waitForTimeout(800);
    console.log("back to 写实人像:", JSON.stringify(await page.evaluate(readHint)));
    console.log("errors:", JSON.stringify([...new Set(errors)].slice(0, 4)));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
