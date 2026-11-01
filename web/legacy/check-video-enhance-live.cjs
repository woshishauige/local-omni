// Live check for the three 提示词优化 changes in the video panel:
//   1. the flow rail gains a 提示词优化 stage only while the switch is on,
//   2. the film estimate grows by that step's cost (and follows the switch),
//   3. the old "ComfyUI 正在执行或排队，将在任务结束后自动加载 Qwen" sentence is gone.
// The running-job state is served by a route stub, so no H3 render is started.
const fs = require("node:fs");
const { chromium } = require("../lib/playwright.cjs");

const origin = "http://127.0.0.1:8081";
const OUT = "D:/AI/Qwen-Local/logs/denoise-check";
const admin = fs.readFileSync("D:/AI/Qwen-Local/logs/admin.token", "utf8").trim();

const stub = {
  busy: true,
  job: {
    id: "vid-check-1", recipe: "video_distill3", state: "running", phase: "enhancing",
    progress: 0.0, error: null, video: null, path: null, report: null,
    enhancer: { label: "Qwen3.8-27B 本地", used: true, reason: "", original: "一只猫",
                rewritten: "夜晚雨后的城市街道，一名穿风衣的女子撑伞走过霓虹灯下的水洼，镜头缓慢前推。",
                seconds: 2.4 },
    enhance_seconds: 2.4, submitted_by: "owner", elapsed: 3.2,
    notes: [{ t: 0.1, phase: "accepted", message: "video_distill3" },
            { t: 0.4, phase: "enhancing", message: "用当前模型优化提示词" }],
    shots: [{ index: 1, seconds: 7.5, steps: 3, seed: 12345, state: "pending", estimate: 223,
              started: null, finished: null, elapsed: null }],
    position: null, total: 1, eta: 320,
    settings: { width: 768, height: 512, enhance_prompt: true, structure_guard: true,
                lora_strength: 0.85, first_shot_context: null, output_root: null,
                project_name: null },
    prompt_id: "prompt-x",
  },
};

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

  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  let serveStub = false;
  await page.route("**/local/video/progress", async route => {
    if (!serveStub) return route.continue();
    return route.fulfill({ json: stub });
  });
  const report = {};
  const read = () => page.evaluate(() => {
    const tile = [...document.querySelectorAll(".qv-metric")]
      .find(node => node.textContent.includes("整片预估"));
    const text = document.body.innerText;
    return {
      steps: [...document.querySelectorAll(".qv-flow-step")].map(node => node.textContent.trim()),
      active: [...document.querySelectorAll(".qv-flow-step")]
        .filter(node => node.dataset.state === "active").map(node => node.textContent.trim()),
      estimate: tile ? tile.querySelector(".qv-metric-value")?.textContent.trim() : null,
      estimateTip: tile ? tile.title : null,
      promptResult: (() => {
        const box = document.querySelector(".qv-prompt-result");
        if (!box || box.hidden) return null;
        return {
          meta: box.querySelector(".qv-monitor-line")?.textContent.trim(),
          original: box.querySelectorAll(".qv-prompt-text")[0]?.textContent.trim(),
          rewritten: box.querySelectorAll(".qv-prompt-text")[1]?.textContent.trim(),
        };
      })(),
      legacyHint: text.includes("将在任务结束后自动加载"),
      hasEnhanceStage: text.includes("提示词优化"),
    };
  });
  try {
    await page.goto(`${origin}/?admin=${encodeURIComponent(admin)}`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1000);
    await page.goto(origin + "/", { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1200);
    await page.locator(".local-mode-button", { hasText: "视频" }).first().click();
    await page.waitForTimeout(2500);
    report.switchOff = await read();

    // The switch is the toggle whose label mentions the optimiser.
    await page.evaluate(() => {
      const row = [...document.querySelectorAll(".qv-toggle")]
        .find(node => node.textContent.includes("优化提示词"));
      row?.querySelector("input")?.click();
    });
    await page.waitForTimeout(600);
    report.switchOn = await read();
    await page.screenshot({ path: `${OUT}/video-enhance-flow.png` });

    // Now let the panel see a running film whose first step is the optimiser.
    serveStub = true;
    await page.evaluate(() => window.VideoPanel.setActive(true));
    await page.waitForTimeout(2000);
    report.running = await read();
    await page.screenshot({ path: `${OUT}/video-enhance-running.png` });
  } catch (failure) {
    report.failure = failure.message;
  }
  report.pageErrors = errors;
  fs.writeFileSync(`${OUT}/video-enhance.json`, JSON.stringify(report, null, 2), "utf8");
  console.log(JSON.stringify(report, null, 2));
  await browser.close();
})();
