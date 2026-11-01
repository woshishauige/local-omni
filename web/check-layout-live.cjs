// Layout check: 生成进度 on the left, the work in the middle, 当前模型 on the right.
const fs = require("node:fs");
const { chromium } = require("./lib/playwright.cjs");

const OUT = "D:/AI/Qwen-Local/logs/layout-check";

const readLayout = () => {
  const box = selector => {
    const element = document.querySelector(selector);
    if (!element) return null;
    const rect = element.getBoundingClientRect();
    return { x: Math.round(rect.x), width: Math.round(rect.width), height: Math.round(rect.height) };
  };
  const columns = [...document.querySelectorAll(".qg-grid > .qg-column")].map(node => {
    const title = node.querySelector(".qg-card-title")?.textContent || "";
    const rect = node.getBoundingClientRect();
    return { cls: node.className.replace("qg-column ", ""), title,
             x: Math.round(rect.x), width: Math.round(rect.width) };
  });
  const progress = document.querySelector(".qg-progress");
  const model = document.querySelector(".qg-model");
  const overflow = [...document.querySelectorAll(".qg-column-progress *")]
    .filter(node => node.scrollWidth > node.clientWidth + 2 && node.clientWidth > 0)
    .map(node => node.className).slice(0, 5);
  return {
    columns,
    progressInLeft: Boolean(progress && progress.closest(".qg-column-progress")),
    modelInRight: Boolean(model && model.closest(".qg-column-side")),
    progressX: progress ? Math.round(progress.getBoundingClientRect().x) : null,
    modelX: model ? Math.round(model.getBoundingClientRect().x) : null,
    stats: box(".qg-column-progress .qg-stats"),
    tiles: getComputedStyle(document.querySelector(".qg-column-progress .qg-stats")).gridTemplateColumns,
    clipped: overflow,
  };
};

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
    await page.goto("http://127.0.0.1:8081/", { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1500);
    await page.locator(".local-mode-button", { hasText: "生图" }).first().click();
    await page.waitForFunction(() => document.querySelector(".qg-progress"), null, { timeout: 30000 });
    await page.waitForTimeout(1200);

    for (const width of [1600, 1440, 1280, 1100, 900, 390]) {
      await page.setViewportSize({ width, height: 1100 });
      await page.waitForTimeout(700);
      const layout = await page.evaluate(readLayout);
      console.log(`viewport ${width}: progressX=${layout.progressX} modelX=${layout.modelX} `
        + `left=${layout.progressInLeft} right=${layout.modelInRight} tiles="${layout.tiles}"`);
      console.log(`   columns: ${JSON.stringify(layout.columns)}`);
      if (width === 1600 || width === 390) {
        await page.screenshot({ path: `${OUT}/layout-${width}.png`, fullPage: width === 390 });
      }
    }
    const layout = await page.evaluate(readLayout);
    console.log("clipped nodes in the progress column:", JSON.stringify(layout.clipped));
    console.log("errors:", JSON.stringify([...new Set(errors)].slice(0, 4)));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
