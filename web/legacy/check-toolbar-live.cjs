// Live check for the two toolbar complaints, driven by a stubbed /local/status:
//   1. a mode/revision change reported while the 视频 view is open must NOT reload the page,
//   2. the "waiting" state (ComfyUI busy) must show its message without disabling the toolbar.
const fs = require("node:fs");
const { chromium } = require("../lib/playwright.cjs");

const origin = "http://127.0.0.1:8081";
const OUT = "D:/AI/Qwen-Local/logs/denoise-check";
const admin = fs.readFileSync("D:/AI/Qwen-Local/logs/admin.token", "utf8").trim();

const base = {
  mode: "text", state: "ready", revision: 1, instance_id: "inst-1", active_requests: 0,
  model_profile: "qwen3.8-27b", model_profiles: [], mtp_enabled: true, error: null,
};
let served = { ...base };
let navigations = 0;

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
  page.on("framenavigated", frame => { if (frame === page.mainFrame()) navigations += 1; });
  await page.route("**/local/status", route => route.fulfill({ json: served }));
  const report = {};
  const toolbar = () => page.evaluate(() => {
    const buttons = [...document.querySelectorAll(".local-mode-button")];
    const select = document.querySelector("select");
    return {
      labels: buttons.map(node => node.textContent.trim().split("\n")[0]),
      disabled: buttons.map(node => Boolean(node.disabled)),
      selectDisabled: select ? Boolean(select.disabled) : null,
      message: document.body.innerText.includes("正在执行或排队"),
      toolbarState: document.querySelector(".local-modes")?.dataset.state || null,
    };
  });
  try {
    await page.goto(`${origin}/?admin=${encodeURIComponent(admin)}`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1500);
    await page.goto(origin + "/", { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(2500);
    report.beforeVideo = { navigations, ...(await toolbar()) };

    // Open the video view, then report the change a render causes (mode + revision move).
    await page.locator(".local-mode-button", { hasText: "视频" }).first().click();
    await page.waitForTimeout(2500);
    const afterOpen = navigations;
    served = { ...base, mode: "video", revision: 2, instance_id: "inst-2", state: "ready" };
    await page.waitForTimeout(6000);
    report.videoTabReload = { navigationsBefore: afterOpen, navigationsAfter: navigations,
                              reloaded: navigations > afterOpen };

    // Now the busy-ComfyUI state: the message shows, the toolbar stays usable.
    served = { ...base, mode: "video", revision: 3, instance_id: "inst-3", state: "waiting",
               error: "ComfyUI 正在执行或排队，将在任务结束后自动加载 Qwen。" };
    await page.waitForTimeout(6000);
    report.waiting = await toolbar();
    await page.screenshot({ path: `${OUT}/toolbar-waiting.png` });
  } catch (failure) {
    report.failure = failure.message;
  }
  report.navigations = navigations;
  report.pageErrors = errors;
  fs.writeFileSync(`${OUT}/toolbar.json`, JSON.stringify(report, null, 2), "utf8");
  console.log(JSON.stringify(report, null, 2));
  await browser.close();
})();
