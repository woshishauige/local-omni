// Focused live check of the image panel's split: the 超分 module must carry no denoise
// control and only upscale recipes, and the 降噪 module runs ffmpeg on its own picture.
const fs = require("node:fs");
const { chromium } = require("./lib/playwright.cjs");

const origin = "http://127.0.0.1:8081";
const PICTURE = "D:/AI/Qwen-Local/logs/upscale-check/image-upscale-result.png";
const OUT = "D:/AI/Qwen-Local/logs/denoise-check";
const admin = fs.readFileSync("D:/AI/Qwen-Local/logs/admin.token", "utf8").trim();

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
    if (request.method() === "POST" && request.url().includes("/local/image/denoise")) {
      const body = request.postDataJSON();
      posted.push({ strength: body.strength, imageChars: (body.image || "").length });
    }
  });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const report = {};
  try {
    // The owner's console link is loopback-only and hands the browser its session cookies.
    await page.goto(`${origin}/?admin=${encodeURIComponent(admin)}`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1200);
    await page.goto(origin + "/", { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1200);
    await page.locator(".local-mode-button", { hasText: "生图" }).first().click();
    await page.waitForTimeout(2000);

    const chip = async label => {
      await page.evaluate(text => {
        const tab = [...document.querySelectorAll(".qg-submode")].find(node => node.textContent.trim() === text);
        if (tab) tab.click();
      }, label);
      await page.waitForTimeout(600);
    };
    report.submodes = await page.$$eval(".qg-submode", nodes => nodes.map(node => node.textContent.trim()));

    await chip("超分");
    report.upscaleMode = await page.evaluate(() => {
      const card = document.querySelector(".qg-denoise-card");
      return {
        denoiseCardHidden: card.hidden,
        denoiseDialVisible: !card.hidden && card.offsetParent !== null,
        recipes: [...document.querySelectorAll(".qg-switch-row button")].map(node => node.textContent.trim()),
        composerVisible: !document.querySelector(".qg-composer").hidden,
      };
    });

    await chip("降噪");
    report.denoiseMode = await page.evaluate(() => {
      const card = document.querySelector(".qg-denoise-card");
      return {
        visible: !card.hidden,
        strength: card.querySelector(".qg-field-input").value,
        buttons: [...card.querySelectorAll("button")].map(node => node.textContent.trim()),
        composerHidden: document.querySelector(".qg-composer").hidden,
        switchHidden: document.querySelector(".qg-switch").hidden,
      };
    });
    const picker = await page.evaluateHandle(() => document.querySelector(".qg-denoise-card input[type=file]"));
    await picker.asElement().setInputFiles(PICTURE);
    await page.waitForTimeout(400);
    await page.evaluate(() => {
      [...document.querySelectorAll(".qg-denoise-card button")]
        .find(node => node.textContent.includes("开始降噪"))?.click();
    });
    for (let attempt = 0; attempt < 60; attempt += 1) {
      const note = await page.evaluate(() =>
        document.querySelector(".qg-denoise-card .qg-denoise-note").textContent);
      if (note.includes("已存到") || note.includes("失败")) { report.note = note; break; }
      await page.waitForTimeout(1000);
    }
    report.posted = posted;
    report.previewSrc = await page.evaluate(() => {
      const image = document.querySelector(".qg-denoise-preview");
      return image && !image.hidden ? image.getAttribute("src") : null;
    });
    await page.screenshot({ path: `${OUT}/image-denoise-pane.png` });
  } catch (failure) {
    report.failure = failure.message;
  }
  report.pageErrors = errors;
  console.log(JSON.stringify(report, null, 2));
  await browser.close();
})();
