// Live check of the generate view's two sub-modules (生成图片 / 超分放大).
const fs = require("node:fs");
const { chromium } = require("./lib/playwright.cjs");

const ORIGIN = "http://127.0.0.1:8081";
const REFERENCE = "D:/AI/Qwen-Local/logs/pipeline-comparison/fp16-lcm-8step.png";
const CLIP = "D:/AI/ComfyUI-BSAI_v010/ComfyUI/input/upscale-test-1s.mp4";
const OUT = "D:/AI/Qwen-Local/logs/upscale-check";

const readView = () => ({
  submodes: [...document.querySelectorAll(".qg-submode")].map(n => n.textContent + ":" + n.dataset.active),
  switchTitle: document.querySelector(".qg-switch .qg-card-title").textContent,
  recipes: [...document.querySelectorAll(".qg-switch-chip")].map(n => n.textContent.trim()),
  promptHidden: document.querySelector(".qg-prompt").hidden,
  negativeHidden: document.querySelector(".qg-negative-input").hidden,
  widthHidden: document.querySelector('.qg-field-input[name="width"]').closest(".qg-field").hidden,
  scaleHidden: document.querySelector('.qg-field-input[name="scale"]').closest(".qg-field").hidden,
  scale: document.querySelector('.qg-field-input[name="scale"]').value,
  presetsHidden: document.querySelector(".qg-preset-card").hidden,
  accepts: [...document.querySelectorAll(".qg-reference-input")].map(n => n.accept),
  button: document.querySelector(".qg-actions .qg-button-primary").textContent,
  pipeline: document.querySelector(".qg-pipeline-select").value,
});

const bodySummary = body => ({
  pipeline: body.pipeline, prompt: body.prompt, scale: body.scale, steps: body.steps, cfg: body.cfg,
  hasImage: Boolean(body.image_1 && String(body.image_1).startsWith("data:image/")),
});

async function waitForJob(page, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await page.waitForTimeout(3000);
    const state = await page.evaluate(async () => {
      const payload = await (await fetch("/local/image/progress", { cache: "no-store" })).json();
      return { busy: payload.busy, state: payload.job?.state, phase: payload.job?.phase,
               pipeline: payload.job?.params?.pipeline, scale: payload.job?.params?.scale };
    });
    if (!state.busy && state.state && state.state !== "running") return state;
  }
  return null;
}

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
  const posted = [];
  page.on("request", request => {
    if (request.method() === "POST" && request.url().includes("/local/image/generate")) {
      try { posted.push(bodySummary(request.postDataJSON())); } catch { /* ignore */ }
    }
  });
  try {
    await page.goto(ORIGIN + "/", { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1500);
    await page.locator(".local-mode-button", { hasText: "生图" }).first().click();
    await page.waitForFunction(() => document.querySelectorAll(".qg-submode").length === 2, null, { timeout: 30000 });
    await page.waitForTimeout(1200);
    console.log("生成图片:", JSON.stringify(await page.evaluate(readView)));
    await page.screenshot({ path: OUT + "/submode-generate.png" });

    await page.locator(".qg-submode", { hasText: "超分放大" }).click();
    await page.waitForFunction(() => {
      const field = document.querySelector('.qg-field-input[name="scale"]');
      return field && !field.closest(".qg-field").hidden;
    }, null, { timeout: 40000 });
    await page.waitForTimeout(1500);
    console.log("超分放大:", JSON.stringify(await page.evaluate(readView)));
    await page.screenshot({ path: OUT + "/submode-upscale.png" });

    // A clip must be refused by the image-only reference input.
    await page.locator(".qg-reference-input").first().setInputFiles(CLIP);
    await page.waitForTimeout(800);
    console.log("clip refused:", JSON.stringify(await page.evaluate(() => ({
      files: [...document.querySelectorAll(".qg-reference-input")].map(n => n.files.length),
      status: document.querySelector(".qg-status-line").textContent,
    }))));

    await page.locator(".qg-reference-input").first().setInputFiles(REFERENCE);
    await page.waitForTimeout(600);
    await page.locator(".qg-actions .qg-button-primary").click();
    const settled = await waitForJob(page, 300000);
    console.log("panel run:", JSON.stringify(settled), "| submitted:", JSON.stringify(posted));
    await page.screenshot({ path: OUT + "/submode-upscale-result.png" });

    await page.locator(".qg-submode", { hasText: "生成图片" }).click();
    await page.waitForFunction(() => document.querySelector(".qg-prompt").hidden === false, null, { timeout: 40000 });
    await page.waitForTimeout(1500);
    console.log("回到生成图片:", JSON.stringify(await page.evaluate(readView)));
    console.log("errors:", JSON.stringify([...new Set(errors)].slice(0, 4)));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
