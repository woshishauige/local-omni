// Focused live check of the split between the two modules: the 超分重建 pane must carry no
// denoise control and only upscale recipes, and the 降噪 pane must run ffmpeg on its own clip.
const fs = require("node:fs");
const { chromium } = require("../lib/playwright.cjs");

const origin = "http://127.0.0.1:8081";
const CLIP = "D:/AI/Qwen-Local/runtime/denoise-module-check.mp4";
const OUT = "D:/AI/Qwen-Local/logs/denoise-check";
const fsNode = require("node:fs");
const admin = fsNode.readFileSync("D:/AI/Qwen-Local/logs/admin.token", "utf8").trim();

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

  const posted = [];
  page.on("request", request => {
    if (request.method() === "POST" && request.url().includes("/local/video/denoise-source")) {
      try { posted.push(request.postDataJSON()); } catch { posted.push({ unparsed: true }); }
    }
  });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));

  const report = {};
  try {
    // The owner's console link is loopback-only and hands the browser its session cookies.
    await page.goto(`${origin}/?admin=${encodeURIComponent(admin)}`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1500);
    await page.goto(origin + "/", { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1500);
    await page.locator(".local-mode-button", { hasText: "视频" }).first().click();
    await page.waitForTimeout(2500);

    const clickSubmode = async label => {
      await page.evaluate(text => {
        const tab = [...document.querySelectorAll(".qv-submode")].find(node => node.textContent.includes(text));
        if (tab) tab.click();
      }, label);
      await page.waitForTimeout(800);
    };

    report.submodes = await page.$$eval(".qv-submode", nodes => nodes.map(node => node.textContent.trim()));

    // The upscale module: no denoise dial, and the denoise recipe is not in its row.
    await clickSubmode("超分重建");
    report.upscalePane = await page.evaluate(() => {
      const pane = [...document.querySelectorAll(".qv-pane")][1];
      return {
        visible: !pane.hidden,
        hasDenoiseDial: Boolean(pane.querySelector(".qv-denoise-strength")),
        recipes: [...pane.querySelectorAll(".qv-recipe")].map(node => node.textContent.trim()),
        limitNote: Boolean(pane.textContent.includes("各倍率支持的最长视频")),
      };
    });
    await page.screenshot({ path: `${OUT}/upscale-pane.png`, fullPage: false });

    // The denoise module: its own clip, its own strength, its own button.
    await clickSubmode("降噪");
    report.denoisePane = await page.evaluate(() => {
      const pane = [...document.querySelectorAll(".qv-pane")][2];
      return {
        visible: !pane.hidden,
        hasDial: Boolean(pane.querySelector(".qv-denoise-strength")),
        dialValue: pane.querySelector(".qv-denoise-strength input")?.value || null,
        buttons: [...pane.querySelectorAll("button")].map(node => node.textContent.trim()),
        hintMentionsOrder: pane.textContent.includes("先") || pane.textContent.includes("顺序"),
      };
    });
    const picker = await page.evaluateHandle(() =>
      [...document.querySelectorAll(".qv-pane")][2].querySelector("input[type=file]"));
    await picker.asElement().setInputFiles(CLIP);
    await page.waitForTimeout(400);
    await page.evaluate(() => {
      const pane = [...document.querySelectorAll(".qv-pane")][2];
      [...pane.querySelectorAll("button")].find(node => node.textContent.includes("开始降噪"))?.click();
    });
    for (let attempt = 0; attempt < 60; attempt += 1) {
      const note = await page.evaluate(() => {
        const pane = [...document.querySelectorAll(".qv-pane")][2];
        return [...pane.querySelectorAll(".qv-note")].map(node => node.textContent).join(" | ");
      });
      if (note.includes("已切换到降噪版") || note.includes("失败")) { report.note = note; break; }
      await page.waitForTimeout(1000);
    }
    report.posted = posted;
    report.videoSrc = await page.evaluate(() => document.querySelector(".qv-video")?.getAttribute("src") || "");
    await page.screenshot({ path: `${OUT}/denoise-pane.png`, fullPage: false });
  } catch (failure) {
    report.failure = failure.message;
  }
  report.pageErrors = errors;
  console.log(JSON.stringify(report, null, 2));
  await browser.close();
})();
