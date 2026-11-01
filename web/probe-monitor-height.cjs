// Measure the monitor rail: how tall is each block, and does the rail need its own
// scrollbar? Used while tuning the left rail so the whole card fits the viewport and
// nothing inside it moves when the page scrolls.
//
// Run from Qwen-Local with:  node web/probe-monitor-height.cjs
const { chromium } = require("./lib/playwright.cjs");

const origin = "http://127.0.0.1:8081";
const height = Number(process.env.DSH_PROBE_HEIGHT || 1000);
const width = Number(process.env.DSH_PROBE_WIDTH || 1440);

(async () => {
  const browser = await chromium.launch({ headless: true, channel: "msedge" });
  const context = await browser.newContext({ viewport: { width, height } });
  const page = await context.newPage();
  const shots = [1, 2, 3, 4].map(index => ({
    index, seconds: 7.5, steps: 20, seed: 1942213066 + index, state: index === 4 ? "running" : "done",
    estimate: 520, started: null, finished: null, elapsed: index === 4 ? 96 : 540,
  }));
  const notes = Array.from({ length: 26 }, (_, i) => ({
    t: i * 7, phase: i === 0 ? "accepted" : "generating",
    message: i === 0 ? "质量优先 · 20 步原生" : `第 ${Math.min(4, 1 + Math.floor(i / 7))}/4 段 · 第 ${(i % 20) + 1} 步`,
  }));
  const job = {
    id: "probe", recipe: "video_quality", state: "running", phase: "generating",
    progress: 0.18, elapsed: 96, video: null, path: null, report: null,
    shots, position: 4, total: 4, eta: 640, prompt_id: "probe-prompt",
    settings: { width: 768, height: 512, output_root: "D:/AI/_audit/h3-fight-ab",
                project_name: "check", structure_guard: false },
    notes,
  };
  await page.route("**/local/video/progress*", route => route.fulfill({
    status: 200, contentType: "application/json", body: JSON.stringify({ busy: true, job }) }));
  try {
    await page.goto(origin + "/", { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1500);
    const modeButton = page.locator('.local-mode-button[data-mode="video"]');
    if (await modeButton.isEnabled().catch(() => false)) await modeButton.click();
    else await page.evaluate(() => window.VideoPanel?.setActive(true));
    await page.locator("#local-video-panel:not([hidden])").waitFor({ timeout: 20000 });
    await page.waitForFunction(() => document.querySelectorAll(".qv-shot-row").length === 4, null, { timeout: 20000 });
    await page.waitForTimeout(1200);
    const report = await page.evaluate(() => {
      const rail = document.querySelector(".qv-column-monitor");
      const card = rail.querySelector(".qv-monitor") || rail.firstElementChild;
      const rows = [...card.children].map(node => ({
        cls: node.className, text: (node.textContent || "").trim().slice(0, 18),
        h: Math.round(node.getBoundingClientRect().height),
      }));
      return {
        viewport: window.innerHeight,
        railBox: { h: Math.round(rail.getBoundingClientRect().height),
                   scrollH: rail.scrollHeight, clientH: rail.clientHeight,
                   overflowY: getComputedStyle(rail).overflowY },
        cardBox: { h: Math.round(card.getBoundingClientRect().height),
                   marginBottom: getComputedStyle(card).marginBottom },
        rows,
      };
    });
    console.log(JSON.stringify(report, null, 1));
    const total = report.rows.reduce((sum, row) => sum + row.h, 0);
    console.log(`card children total: ${total}px · rail ${report.railBox.h}px of ${report.viewport}px viewport`);
    await page.screenshot({ path: "logs/image-panel-validation/video-monitor-probe.png", fullPage: false });
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
