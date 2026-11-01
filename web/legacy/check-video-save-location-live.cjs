// Live check for the reworked 保存位置 card: the composed destination, whether it is already
// there, and the refusal of a name Windows would reject — all before anything is submitted.
const fs = require("node:fs");
const { chromium } = require("../lib/playwright.cjs");

const origin = "http://127.0.0.1:8081";
const OUT = "D:/AI/Qwen-Local/logs/denoise-check";
const admin = fs.readFileSync("D:/AI/Qwen-Local/logs/admin.token", "utf8").trim();

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
  const report = {};
  const read = () => page.evaluate(() => {
    const row = document.querySelector(".qv-output-preview");
    return {
      label: row.querySelector(".qv-output-preview-label").textContent.trim(),
      path: row.querySelector(".qv-output-preview-path").textContent.trim(),
      state: row.querySelector(".qv-output-preview-state").textContent.trim(),
      dataState: row.dataset.state,
      project: document.querySelectorAll(".qv-file")[1]?.value ?? null,
    };
  });
  const setName = async name => {
    await page.evaluate(value => {
      const field = [...document.querySelectorAll(".qv-field")]
        .find(node => node.textContent.includes("保存名称"));
      const input = field.querySelector("input");
      input.value = value;
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }, name);
    await page.waitForTimeout(1200);
    return read();
  };
  try {
    await page.goto(`${origin}/?admin=${encodeURIComponent(admin)}`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1000);
    await page.goto(origin + "/", { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1200);
    await page.locator(".local-mode-button", { hasText: "视频" }).first().click();
    await page.waitForTimeout(2500);

    report.initial = await read();
    report.existing = await setName("pian1");          // a real project folder
    report.fresh = await setName(`zz-check-${Date.now() % 10000}`);
    report.invalid = await setName("bad:name");
    report.blank = await setName("");
    await page.screenshot({ path: `${OUT}/video-save-location.png` });
  } catch (failure) {
    report.failure = failure.message;
  }
  report.pageErrors = errors;
  fs.writeFileSync(`${OUT}/video-save-location.json`, JSON.stringify(report, null, 2), "utf8");
  console.log(JSON.stringify(report, null, 2));
  await browser.close();
})();
