// End-to-end check of the video view through the real page: pick a save location
// with the folder chooser, set two shots with different per-shot step counts, press
// 开始生成, and report the film the gateway actually produced. Also prints what each
// submission carried, so per-shot tuning is verified against ComfyUI's own record
// rather than the panel's own claims.
//
// Run from Qwen-Local with:  node web/check-video-generate-live.cjs
const path = require("node:path");
const fs = require("node:fs");
const { chromium } = require("./lib/playwright.cjs");

const origin = "http://127.0.0.1:8081";
const comfy = "http://127.0.0.1:8188";
const project = process.env.DSH_CHECK_PROJECT || "panel-check";
// Where the chooser should end up: a real folder on another drive path, so the run
// proves the save location takes effect instead of only the default.
const saveTo = process.env.DSH_CHECK_OUTPUT || "D:\\AI\\_audit\\save-location-check";
const parent = path.dirname(saveTo);
const leaf = path.basename(saveTo);

(async () => {
  fs.mkdirSync(saveTo, { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: "msedge" });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(`pageerror: ${error.message}`));
  page.on("console", message => { if (message.type() === "error") errors.push(`console: ${message.text()}`); });
  let submitted = null;
  page.on("request", request => {
    if (request.method() === "POST" && request.url().endsWith("/local/video/generate")) {
      submitted = request.postDataJSON();
    }
  });
  try {
    await page.goto(origin + "/", { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1500);
    await page.locator('.local-mode-button[data-mode="video"]').click();
    await page.locator("#local-video-panel:not([hidden])").waitFor({ timeout: 20000 });
    await page.waitForFunction(() => document.querySelectorAll(".qv-shot").length === 1, null, { timeout: 15000 });
    await page.waitForFunction(() => document.querySelector('input.qv-file[type="text"]')?.value, null, { timeout: 15000 });

    const order = await page.evaluate(() => [...document.querySelectorAll("#local-video-panel .qv-pane:not([hidden]) > .qv-card")]
      .map(card => card.querySelector(".qv-card-title")?.textContent || "(无标题)"));
    console.log("卡片顺序:", JSON.stringify(order));
    const saveField = page.locator('input.qv-file[type="text"]').first();
    console.log("默认存储路径:", await saveField.inputValue());

    // Click a folder button by its exact name; hasText is a substring match and
    // would happily pick a sibling.
    const clickNamed = (selector, wanted, strip = "") => page.evaluate(([css, text, prefix]) => {
      const button = [...document.querySelectorAll(css)]
        .find(el => el.textContent.replace(prefix, "").trim() === text);
      if (!button) throw new Error(`找不到：${text}`);
      button.click();
    }, [selector, wanted, strip]);
    const atFolder = folder => page.waitForFunction(
      target => document.querySelector(".qv-browser-path")?.textContent === target, folder, { timeout: 15000 });

    // The folder chooser: drive root, then each level down to the target folder.
    await page.getByRole("button", { name: "选择…" }).click();
    await page.locator(".qv-browser:not([hidden])").waitFor({ timeout: 10000 });
    await page.waitForFunction(() => document.querySelectorAll(".qv-browser-roots button").length > 0, null, { timeout: 10000 });
    const drive = path.parse(saveTo).root;
    await clickNamed(".qv-browser-roots button", drive);
    await atFolder(drive);
    const levels = parent.slice(drive.length).split(path.sep).filter(Boolean);
    for (let index = 0; index < levels.length; index += 1) {
      await clickNamed(".qv-browser-folder", levels[index], "📁 ");
      await atFolder(path.join(drive, ...levels.slice(0, index + 1)));
    }
    await clickNamed(".qv-browser-folder", leaf, "📁 ");
    await atFolder(saveTo);
    await page.getByRole("button", { name: "用这个文件夹" }).click();
    console.log("选择后存储路径:", await saveField.inputValue());

    await page.locator(".qv-shot-count .qv-recipe", { hasText: "2 段" }).click();
    await page.waitForFunction(() => document.querySelectorAll(".qv-shot").length === 2, null, { timeout: 5000 });

    const shots = page.locator(".qv-shot");
    const params = index => shots.nth(index).locator(".qv-shot-param-input");
    const seeds = [];
    for (let index = 0; index < 2; index += 1) {
      seeds.push(await params(index).nth(2).inputValue());
      await params(index).nth(0).fill("5");
    }
    await params(0).nth(1).fill("1");
    await params(1).nth(1).fill("2");
    console.log("默认种子:", JSON.stringify(seeds));
    await shots.nth(1).locator(".qv-seed-random").click();
    const rerolled = await params(1).nth(2).inputValue();
    console.log("第 2 段点「随机」后:", rerolled, "changed:", rerolled !== seeds[1]);

    await page.locator(".qv-textarea").first().fill("夜晚雨后的城市街道，霓虹在水面反光，镜头缓缓前推");
    await page.locator('input.qv-file[type="text"]').last().fill(project);

    const before = await page.evaluate(async () => {
      const payload = await (await fetch("/local/video/history", { cache: "no-store" })).json();
      return payload.history.length;
    });
    await page.getByRole("button", { name: "开始生成" }).click();
    await page.waitForTimeout(4000);
    console.log("提交内容:", JSON.stringify(submitted, null, 1));
    console.log("历史条数 before:", before);
    if (!submitted) throw new Error("点击开始生成后没有提交请求");

    const began = Date.now();
    let last = "";
    for (;;) {
      const job = await page.evaluate(async () => {
        const payload = await (await fetch("/local/video/progress", { cache: "no-store" })).json();
        return payload.job;
      });
      const line = `[${Math.round((Date.now() - began) / 1000)}s] state=${job?.state} phase=${job?.phase} progress=${job?.progress} ${job?.notes?.slice(-1)[0]?.message || ""}`;
      if (line !== last) { console.log(line); last = line; }
      if (!job || ["done", "error", "cancelled"].includes(job.state)) {
        console.log("最终状态:", JSON.stringify({ state: job?.state, video: job?.video, error: job?.error }));
        console.log("报告:", (job?.report || "").slice(0, 700));
        break;
      }
      if (Date.now() - began > 45 * 60 * 1000) throw new Error("等待超时");
      await page.waitForTimeout(10000);
    }

    const view = await page.evaluate(() => ({
      status: document.querySelector(".qv-status")?.textContent,
      src: document.querySelector(".qv-video")?.getAttribute("src"),
      visible: !document.querySelector(".qv-video")?.hidden,
      history: [...document.querySelectorAll(".qv-history-item")].map(li => li.textContent),
    }));
    console.log("面板结果:", JSON.stringify(view, null, 1));
    const film = path.join(saveTo, project, "master.mp4");
    console.log("成片落盘:", film, fs.existsSync(film) ? `${fs.statSync(film).size} bytes` : "缺失");
    if (view.src) {
      const served = await (await fetch(origin + view.src)).arrayBuffer();
      console.log("面板取回成片:", view.src.slice(0, 80), served.byteLength, "bytes");
    }

    // ComfyUI is another origin, so its history is read from Node rather than
    // from the page (a page fetch would be blocked by CORS).
    const history = await (await fetch(`${comfy}/history`)).json();
    console.log("ComfyUI 记录:", JSON.stringify(Object.values(history).map(entry => {
      const director = (entry.prompt?.[2] || {})["50"]?.inputs || {};
      return { status: entry.status?.status_str, shot_index: director.shot_index,
               action: director.action, steps: director.steps, seed: director.seed,
               output_root: director.output_root };
    }), null, 1));
    console.log("页面错误:", JSON.stringify(errors));
    await page.screenshot({ path: path.join(__dirname, "../logs/image-panel-validation/video-generate-live.png"), fullPage: true });
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
