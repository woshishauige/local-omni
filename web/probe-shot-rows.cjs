// Dump the monitor's per-shot rows in three states (idle / mixed / done) so a wrong or
// blank duration is visible without guessing. Also reports the rail's geometry.
//
// Run from Qwen-Local with:  node web/probe-shot-rows.cjs
const { chromium } = require("./lib/playwright.cjs");

const origin = "http://127.0.0.1:8081";

const shots = [
  { index: 1, seconds: 7.5, steps: 20, seed: 111, state: "done", estimate: 760, started: null, finished: null, elapsed: 640 },
  { index: 2, seconds: 7.5, steps: 20, seed: 222, state: "running", estimate: 1010, started: Date.now() / 1000 - 120, finished: null, elapsed: null },
  { index: 3, seconds: 7.5, steps: 20, seed: 333, state: "pending", estimate: 1010, started: null, finished: null, elapsed: null },
  { index: 4, seconds: 7.5, steps: 12, seed: 444, state: "error", estimate: 600, started: null, finished: null, elapsed: null },
];
const job = (state) => ({
  id: "probe", recipe: "video_quality", state, phase: state === "done" ? "done" : "generating",
  progress: state === "done" ? 1 : 0.42, elapsed: state === "done" ? 2600 : 780,
  video: null, path: null, report: state === "done" ? "{\"video\": \"x.mp4\", \"shots\": 4}" : null,
  shots, position: 2, total: 4, eta: 900, prompt_id: "probe-prompt",
  settings: { width: 768, height: 512, output_root: "D:/AI/_audit/h3-fight-ab",
              project_name: "probe", structure_guard: false },
  notes: [
    { t: 0, phase: "accepted", message: "质量优先 · 20 步原生" },
    { t: 0.05, phase: "starting_comfy", message: "" },
    { t: 0.2, phase: "building_graph", message: "第 1/4 段" },
    { t: 0.4, phase: "submitting", message: "第 1/4 段 · 36 个节点" },
    { t: 0.5, phase: "generating", message: "第 1/4 段 · 20 步" },
    { t: 650, phase: "generating", message: "第 2/4 段 · 20 步" },
  ],
});

async function dump(page, label) {
  await page.waitForTimeout(1200);
  const data = await page.evaluate(() => ({
    badge: document.querySelector(".qv-monitor-badge")?.textContent,
    percent: document.querySelector(".qv-progress-percent")?.textContent,
    rows: [...document.querySelectorAll(".qv-shot-row")].map(row => ({
      state: row.dataset.state,
      cells: [...row.children].map(child => `${child.className.replace("qv-shot-row-", "")}=${child.textContent}`),
      height: Math.round(row.getBoundingClientRect().height),
    })),
    flow: {
      percent: document.querySelector(".qv-flow-percent")?.textContent,
      steps: [...document.querySelectorAll(".qv-flow-step")].map(s => `${s.dataset.state}:${s.textContent}`),
    },
  }));
  console.log(`\n=== ${label} ===`);
  console.log(JSON.stringify(data, null, 1));
  return data;
}

function timeOf(data, index) {
  const row = data.rows[index];
  return row?.cells.find(cell => cell.startsWith("time=")) || "";
}

(async () => {
  const browser = await chromium.launch({ headless: true, channel: "msedge" });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  let payload = { busy: false, job: null };
  await page.route("**/local/video/progress*", route => route.fulfill({
    status: 200, contentType: "application/json", body: JSON.stringify(payload) }));
  try {
    await page.goto(origin + "/", { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1500);
    const modeButton = page.locator('.local-mode-button[data-mode="video"]');
    if (await modeButton.isEnabled().catch(() => false)) await modeButton.click();
    else await page.evaluate(() => window.VideoPanel?.setActive(true));
    await page.locator("#local-video-panel:not([hidden])").waitFor({ timeout: 20000 });
    await page.waitForFunction(() => document.querySelectorAll(".qv-shot-row").length > 0, null, { timeout: 15000 });
    await dump(page, "空闲（规划行）");

    // A live run: the running shot's clock must tick.
    payload = { busy: true, job: job("running") };
    await page.waitForTimeout(2600);
    const live1 = await dump(page, "运行中（1 完成 / 1 生成中 / 1 等待 / 1 失败）");
    await page.waitForTimeout(2600);
    const live2 = await dump(page, "运行中（2.6 秒后）");
    const ticking = timeOf(live1, 1) !== timeOf(live2, 1);
    console.log(`运行中第 2 段计时是否在走：${ticking}  「${timeOf(live1, 1)}」→「${timeOf(live2, 1)}」`);
    if (!ticking) throw new Error("运行中那一行没有计时");

    // The job ends while a row is still marked "running" (cancel / error leaves one
    // behind): that clock must freeze instead of counting for ever.
    payload = { busy: false, job: job("done") };
    await page.waitForTimeout(2600);
    const done1 = await dump(page, "已完成（其中一行仍是 running）");
    await page.waitForTimeout(3000);
    const done2 = await dump(page, "已完成（3 秒后）");
    const frozen = timeOf(done1, 1) === timeOf(done2, 1);
    console.log(`任务结束后那一行是否停住：${frozen}  「${timeOf(done1, 1)}」→「${timeOf(done2, 1)}」`);
    if (!frozen) throw new Error("任务结束后计时仍在空转");

    const geometry = await page.evaluate(() => {
      const rail = document.querySelector(".qv-column-monitor");
      const card = rail.querySelector(".qv-monitor");
      return { railHeight: Math.round(rail.getBoundingClientRect().height),
               railScroll: rail.scrollHeight, railClient: rail.clientHeight,
               cardHeight: Math.round(card.getBoundingClientRect().height),
               reportVisible: Boolean(document.querySelector(".qv-report")?.offsetParent),
               monitorTop: Math.round(rail.getBoundingClientRect().top),
               middleTop: Math.round(document.querySelector(".qv-column-main").getBoundingClientRect().top),
               viewport: window.innerHeight };
    });
    console.log("\n=== 几何 ===");
    console.log(JSON.stringify(geometry, null, 1));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
