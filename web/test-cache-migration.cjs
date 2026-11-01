const assert = require("node:assert/strict");
const http = require("node:http");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("./lib/playwright.cjs");

const nativeOrigin = process.env.QWEN_CACHE_TEST_NATIVE_ORIGIN || "http://127.0.0.1:8082";
const injection = '<link rel="stylesheet" href="/local/mode-control.css"><script defer src="/local/mode-control.js"></script>';
const sentinel = "qwen-cache-migration-preserved";

async function run() {
  const oldHtmlResponse = await fetch(nativeOrigin + "/");
  assert.equal(oldHtmlResponse.status, 200);
  const oldHtml = await oldHtmlResponse.text();
  assert.ok(!oldHtml.includes("/local/mode-control.js"));
  assert.ok(!oldHtml.includes("bundle.zh-CN.js"));
  const localizedHtml = await fs.readFile(path.join(__dirname, "native/index.html"), "utf8");
  assert.ok(localizedHtml.includes("bundle.zh-CN.js"));
  const oldWorkerResponse = await fetch(nativeOrigin + "/sw.js");
  assert.equal(oldWorkerResponse.status, 200);
  const oldWorker = await oldWorkerResponse.text();
  assert.ok(oldWorker.includes("precacheAndRoute"), "test uses the native precaching worker");
  let upgraded = false;
  let rootNetworkRequests = 0;
  let workerUpgradeRequests = 0;
  const proxyErrors = [];
  const server = http.createServer(async (request, response) => {
    try {
      if (request.method !== "GET" && request.method !== "HEAD") {
        response.writeHead(405).end();
        return;
      }
      const url = new URL(request.url, "http://127.0.0.1");
      response.setHeader("Cache-Control", "no-store");
      if (url.pathname === "/") {
        rootNetworkRequests++;
        response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
        response.end(upgraded ? localizedHtml.replace("</head>", injection + "</head>") : oldHtml);
      } else if (url.pathname === "/sw.js") {
        if (upgraded) workerUpgradeRequests++;
        response.writeHead(200, { "Content-Type": "application/javascript", "Service-Worker-Allowed": "/" });
        response.end(upgraded ? await fs.readFile(path.join(__dirname, "sw.js")) : oldWorker);
      } else if (["/local/mode-control.js", "/local/mode-control.css"].includes(url.pathname)) {
        response.writeHead(200, { "Content-Type": url.pathname.endsWith(".js") ? "application/javascript" : "text/css" });
        response.end(await fs.readFile(path.join(__dirname, path.basename(url.pathname))));
      } else if (url.pathname === "/_app/immutable/bundle.zh-CN.js") {
        response.writeHead(200, { "Content-Type": "application/javascript" });
        response.end(await fs.readFile(path.join(__dirname, "native/_app/immutable/bundle.zh-CN.js")));
      } else if (url.pathname === "/local/status") {
        response.writeHead(200, { "Content-Type": "application/json" });
        response.end(JSON.stringify({ mode: "text", state: "ready", mtp_enabled: true, active_requests: 0, error: null, revision: 1 }));
      } else if (url.pathname === "/props") {
        response.writeHead(200, { "Content-Type": "application/json" });
        response.end(JSON.stringify({
          model_alias: "qwen-local-test", model_path: "qwen-local-test.gguf",
          modalities: { vision: false, video: false, audio: false },
          default_generation_settings: { n_ctx: 16384, params: { temperature: 0.7, top_p: 0.9, top_k: 20 } },
          total_slots: 1,
        }));
      } else if (["/models", "/v1/models"].includes(url.pathname)) {
        response.writeHead(200, { "Content-Type": "application/json" });
        response.end(JSON.stringify({ object: "list", data: [{ id: "qwen-local-test", object: "model", owned_by: "llamacpp" }] }));
      } else if (url.pathname === "/tools") {
        response.writeHead(200, { "Content-Type": "application/json" });
        response.end(JSON.stringify({ tools: [] }));
      } else if (url.pathname === "/health") {
        response.writeHead(200, { "Content-Type": "application/json" });
        response.end(JSON.stringify({ status: "ok" }));
      } else {
        // Native static files include the exact Workbox runtime and precache assets.
        const upstream = await fetch(nativeOrigin + request.url);
        response.writeHead(upstream.status, { "Content-Type": upstream.headers.get("content-type") || "application/octet-stream" });
        response.end(Buffer.from(await upstream.arrayBuffer()));
      }
    } catch (error) {
      proxyErrors.push(error.message);
      if (!response.headersSent) response.writeHead(502);
      response.end();
    }
  });
  let browser;
  try {
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", resolve);
    });
    const origin = `http://127.0.0.1:${server.address().port}`;
    browser = await chromium.launch({ channel: "msedge", headless: true });
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.goto(origin + "/", { waitUntil: "domcontentloaded" });
    await page.evaluate(async () => {
      await navigator.serviceWorker.register("/sw.js", { scope: "/" });
      await Promise.race([
        navigator.serviceWorker.ready,
        new Promise((_, reject) => setTimeout(() => reject(new Error("native worker did not activate")), 60000)),
      ]);
    });
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => !!navigator.serviceWorker.controller);
    const oldPage = await page.reload({ waitUntil: "domcontentloaded" });
    assert.ok(oldPage.fromServiceWorker(), "old root HTML must actually come from the native worker");
    assert.equal(await page.locator("#local-mode-toolbar").count(), 0);
    const input = page.locator(".conversation-chat-form textarea");
    await input.waitFor();
    assert.equal(await input.getAttribute("placeholder"), "Type a message...");
    await page.evaluate(async value => {
      localStorage.setItem("qwen-cache-migration-test", value);
      await new Promise((resolve, reject) => {
        const request = indexedDB.open("qwen-cache-migration-test", 1);
        request.onupgradeneeded = () => request.result.createObjectStore("history");
        request.onerror = () => reject(request.error);
        request.onsuccess = () => {
          const database = request.result;
          const transaction = database.transaction("history", "readwrite");
          transaction.objectStore("history").put(value, "sentinel");
          transaction.oncomplete = () => { database.close(); resolve(); };
          transaction.onerror = () => reject(transaction.error);
        };
      });
    }, sentinel);
    const oldCacheKeys = await page.evaluate(() => caches.keys());
    assert.ok(oldCacheKeys.some(key => key.includes("workbox-precache")));

    upgraded = true;
    // The launcher query must bypass the existing root precache without deleting it.
    const beforeMigration = rootNetworkRequests;
    const migratedPage = await page.goto(origin + "/?qwen_ui=zh-CN-v3", { waitUntil: "domcontentloaded" });
    assert.equal(migratedPage.fromServiceWorker(), false, "new launcher URL bypasses old precache");
    assert.ok(rootNetworkRequests > beforeMigration);
    await page.locator('#local-mode-toolbar[data-state="ready"]').waitFor();
    await page.locator('[data-mode="text"]').waitFor();
    await page.locator('[data-mode="vision"]').waitFor();
    await input.waitFor();
    assert.equal(await input.getAttribute("placeholder"), "\u8f93\u5165\u6d88\u606f\u2026");

    // Revisit the exact previously cached URL after the new worker has taken over.
    // A service-worker takeover can cancel the navigation in flight (Playwright
    // reports net::ERR_ABORTED); that is the very race this loop is retrying for,
    // so it must not escape as a test failure.
    let freshPage;
    for (let attempt = 0; attempt < 30; attempt++) {
      try {
        freshPage = await page.goto(origin + "/", { waitUntil: "domcontentloaded" });
      } catch (error) {
        if (!/ERR_ABORTED/.test(error.message)) throw error;
        await page.waitForTimeout(100);
        continue;
      }
      if (!freshPage.fromServiceWorker()) break;
      await page.waitForTimeout(100);
    }
    assert.ok(freshPage, "the root page must load again after the worker upgrade");
    assert.equal(freshPage.fromServiceWorker(), false, "root now comes from the network after worker upgrade");
    await page.locator('#local-mode-toolbar[data-state="ready"]').waitFor();
    await input.waitFor();
    assert.equal(await input.getAttribute("placeholder"), "\u8f93\u5165\u6d88\u606f\u2026");
    assert.ok(workerUpgradeRequests > 0);
    // A reload can hit the same takeover race, so retry it once as well.
    let refreshedPage;
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        refreshedPage = await page.reload({ waitUntil: "domcontentloaded" });
        break;
      } catch (error) {
        if (!/ERR_ABORTED/.test(error.message) || attempt === 2) throw error;
        await page.waitForTimeout(200);
      }
    }
    assert.equal(refreshedPage.fromServiceWorker(), false);
    await page.locator('#local-mode-toolbar[data-state="ready"]').waitFor();
    await input.waitFor();
    assert.equal(await input.getAttribute("placeholder"), "\u8f93\u5165\u6d88\u606f\u2026");
    const stored = await page.evaluate(async () => ({
      localStorage: localStorage.getItem("qwen-cache-migration-test"),
      indexedDB: await new Promise((resolve, reject) => {
        const request = indexedDB.open("qwen-cache-migration-test", 1);
        request.onerror = () => reject(request.error);
        request.onsuccess = () => {
          const database = request.result;
          const query = database.transaction("history", "readonly").objectStore("history").get("sentinel");
          query.onsuccess = () => { database.close(); resolve(query.result); };
          query.onerror = () => reject(query.error);
        };
      }),
      caches: await caches.keys(),
    }));
    assert.equal(stored.localStorage, sentinel);
    assert.equal(stored.indexedDB, sentinel);
    assert.ok(oldCacheKeys.every(key => stored.caches.includes(key)), "migration does not delete existing CacheStorage either");
    assert.deepEqual(proxyErrors, []);
    console.log(JSON.stringify({ passed: true, nativeWorker: "unmodified", oldRootFromPrecache: true, migratedToolbar: true, migratedChineseInput: true, rootRefreshToolbar: true, rootRefreshChineseInput: true, localStoragePreserved: true, indexedDBPreserved: true, workerUpgradeRequests }));
  } finally {
    if (browser) await browser.close();
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
