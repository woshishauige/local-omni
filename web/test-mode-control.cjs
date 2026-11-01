const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("./lib/playwright.cjs");

const origin = process.env.QWEN_UI_TEST_ORIGIN || "http://127.0.0.1:8081";
const injectedHead = '<link rel="stylesheet" href="/local/mode-control.css"><script defer src="/local/mode-control.js"></script>';
const root = __dirname;
const fixtureOnly = process.env.QWEN_UI_TEST_FIXTURE === "1";
const fixture = `<!doctype html><html><head><style>
  * { box-sizing: border-box; } body { margin: 0; font-family: Arial; }
  body > div > .flex { display: flex; } aside { position: sticky; width: 56px; height: calc(100dvh - 18px); }
  .chat-screen { width: calc(100vw - 56px); min-height: calc(100dvh - 16px); display: flex; align-items: center; justify-content: center; }
  form { width: min(768px, calc(100% - 32px)); padding: 16px; border: 1px solid #ddd; } textarea { width: 100%; height: 80px; }
  @media(max-width:767px) { aside { position:fixed; left:8px; width:40px; height:40px; } .chat-screen { width:100%; } form { position:fixed; bottom:8px; width:calc(100% - 16px); } }
  </style></head><body><div style="display: contents"><div class="flex"><aside>Q</aside><main class="chat-screen"><form class="conversation-chat-form"><textarea></textarea><button type="submit">Send</button></form></main></div></div></body></html>`;

async function run() {
  const browser = await chromium.launch({ headless: true, channel: "msedge" });
  try {
    for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }, { width: 320, height: 640 }]) {
      const context = await browser.newContext({ viewport });
      const page = await context.newPage();
      const pageErrors = [];
      page.on('pageerror', error => pageErrors.push(error.message));
      if (process.env.QWEN_UI_METADATA_ORIGIN) {
        for (const endpoint of ['props', 'models', 'v1/models', 'health']) {
          await page.route(`**/${endpoint}*`, async route => {
            const response = await route.fetch({url: process.env.QWEN_UI_METADATA_ORIGIN + '/' + endpoint});
            await route.fulfill({response});
          });
        }
      }
      let status = { mode: "text", state: "ready", mtp_enabled: true, mtp_reason: "Native MTP", active_requests: 0, error: null, revision: 1 };
      let rejectSwitch = false;
      let statusUnavailable = false;
      let navigations = 0;
      let switches = 0;
      page.on("domcontentloaded", () => { navigations++; });
      await page.route("**/local/mode-control.js", route => route.fulfill({ path: path.join(root, "mode-control.js"), contentType: "text/javascript" }));
      await page.route("**/local/mode-control.css", route => route.fulfill({ path: path.join(root, "mode-control.css"), contentType: "text/css" }));
      await page.route("**/sw.js", route => route.fulfill({ path: path.join(root, "sw.js"), contentType: "application/javascript" }));
      await page.route("**/local/status", route => statusUnavailable ? route.abort() : route.fulfill({ json: status }));
      await page.route("**/local/mode", async route => {
        switches++;
        assert.equal(route.request().method(), "POST");
        assert.equal(route.request().headers()["content-type"], "application/json");
        if (rejectSwitch) return route.fulfill({ status: 409, json: { ...status, active_requests: 1 } });
        status = { ...status, mode: route.request().postDataJSON().mode, state: "switching", revision: status.revision + 1 };
        await route.fulfill({ status: 202, json: status });
      });
      await page.route(origin + "/**", async route => {
        if (route.request().resourceType() !== "document") return route.fallback();
        if (fixtureOnly) return route.fulfill({ contentType: "text/html", body: fixture.replace("</head>", injectedHead + "</head>") });
        const response = await route.fetch();
        const source = await response.text();
        const html = source.includes('/local/mode-control.js') ? source : source.replace("</head>", injectedHead + "</head>");
        await route.fulfill({ response, body: html });
      });
      await page.goto(origin + "/");
      const input = page.locator(".conversation-chat-form textarea");
      await input.waitFor();
      await page.locator('.local-mode-button[data-mode="text"][aria-pressed="true"]').waitFor();
      await page.waitForTimeout(900);
      assert.equal(navigations, 1, "initial status must not reload the native UI");
      await input.fill("Keep my unsent draft, including \u4e2d\u6587\u3002");
      await page.evaluate(() => localStorage.setItem("local-mode-test-history", "unchanged"));
      const screenshotName = `ui-modes-${fixtureOnly ? "fixture-" : ""}${viewport.width}.png`;
      if (process.env.QWEN_UI_SCREENSHOTS_STDOUT === "1") {
        console.log(JSON.stringify({ screenshot: screenshotName, base64: (await page.screenshot({ fullPage: true })).toString("base64") }));
      } else {
        await page.screenshot({ path: path.join(root, "../logs", screenshotName), fullPage: true });
      }
      const layout = await page.evaluate(() => {
        const toolbar = document.querySelector("#local-mode-toolbar").getBoundingClientRect();
        const aside = document.querySelector("body > div[style='display: contents'] > .flex > aside").getBoundingClientRect();
        const composer = document.querySelector(".conversation-chat-form").getBoundingClientRect();
        return { toolbar: toolbar.toJSON(), aside: aside.toJSON(), composer: composer.toJSON(), pageWidth: document.documentElement.scrollWidth, viewportWidth: innerWidth };
      });
      assert.ok(layout.aside.top >= layout.toolbar.bottom, "toolbar must not overlap navigation");
      assert.ok(layout.composer.top > layout.toolbar.bottom, "toolbar must not overlap composer");
      assert.ok(layout.composer.bottom <= viewport.height + 1, "composer must fit in viewport");
      assert.equal(layout.pageWidth, layout.viewportWidth, "no horizontal overflow");
      status.active_requests = 1;
      await page.waitForTimeout(2200);
      assert.ok(await page.locator('[data-mode="vision"]').isDisabled(), "mode switch blocked during generation");
      status.active_requests = 0;
      await page.waitForTimeout(2200);
      rejectSwitch = true;
      await page.locator('[data-mode="vision"]').click();
      await page.locator(".local-mode-error").waitFor();
      assert.equal(await input.inputValue(), "Keep my unsent draft, including \u4e2d\u6587\u3002");
      assert.equal(navigations, 1, "rejected switch must not reload");
      rejectSwitch = false;
      await page.waitForTimeout(2200);
      await page.locator('[data-mode="vision"]').click();
      await page.locator('#local-mode-toolbar[data-state="switching"]').waitFor();
      assert.ok(await page.locator('[data-mode="text"]').isDisabled());
      status = { ...status, state: "ready", mtp_enabled: false, mtp_reason: "Vision projector active" };
      await page.waitForFunction(() => document.querySelector('[data-mode="vision"]')?.getAttribute("aria-pressed") === "true" && document.querySelector(".local-mode-mtp")?.textContent.includes("\u5173\u95ed"));
      await page.waitForTimeout(1000);
      assert.equal(navigations, 2, "completed mode switch reloads native capability props once");
      if (await input.inputValue() !== "Keep my unsent draft, including \u4e2d\u6587\u3002") {
        console.log(JSON.stringify({pageErrors, draftDebug: await page.evaluate(() => ({url:location.href, toolbar:!!document.getElementById('local-mode-toolbar'), scripts:[...document.scripts].map(s=>s.src), saved:sessionStorage.getItem('qwen.local.mode-draft.v1'), textarea:document.querySelector('.conversation-chat-form textarea')?.outerHTML})) }));
      }
      assert.equal(await input.inputValue(), "Keep my unsent draft, including \u4e2d\u6587\u3002", "unsent draft survives reload");
      assert.equal(await page.evaluate(() => localStorage.getItem("local-mode-test-history")), "unchanged");
      assert.equal(switches, 2);
      statusUnavailable = true;
      await page.waitForTimeout(2200);
      await page.locator('#local-mode-toolbar[data-state="offline"]').waitFor();
      assert.ok(await page.locator(".local-mode-retry").isVisible());
      statusUnavailable = false;
      await page.locator(".local-mode-retry").click();
      await page.locator('#local-mode-toolbar[data-state="ready"]').waitFor();
      console.log(JSON.stringify({ fixtureOnly, viewport, passed: true, layout, navigations, switches }));
      await context.close();
    }
  } finally {
    await browser.close();
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
