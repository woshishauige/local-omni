const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require("./lib/playwright.cjs");
const settings = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'settings.json'), 'utf8'));
const expectedContext = settings.context_size;
assert.ok(Number.isInteger(expectedContext) && expectedContext >= 512 && expectedContext <= 262144,
  'settings.json must contain a valid context_size');

(async () => {
  const browser = await chromium.launch({ headless: true, channel: 'msedge' });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    await page.goto('http://127.0.0.1:8081/');
    const input = page.locator('.conversation-chat-form textarea');
    await input.waitFor();
    const draft = 'Context validation draft';
    await input.fill(draft);
    for (const mode of ['vision', 'text']) {
      await page.locator(`[data-mode="${mode}"]`).click();
      await page.waitForFunction((name) => {
        const toolbar = document.querySelector('#local-mode-toolbar');
        return toolbar?.dataset.state === 'ready' &&
          document.querySelector(`[data-mode="${name}"]`)?.getAttribute('aria-pressed') === 'true';
      }, mode, { timeout: 180000 });
      await page.waitForFunction((text) => document.querySelector('.conversation-chat-form textarea')?.value === text,
                                 draft, { timeout: 30000 });
      const state = await page.evaluate(async () => ({
        status: await (await fetch('/local/status')).json(),
        props: await (await fetch('/props')).json(),
      }));
      assert.equal(state.status.mode, mode);
      assert.equal(state.status.mtp_enabled, mode === 'text' && settings.mtp_enabled === true);
      assert.equal(state.status.context_size, expectedContext);
      assert.equal(state.props.default_generation_settings.n_ctx, expectedContext);
      console.log(JSON.stringify({ mode, context: expectedContext, mtp: state.status.mtp_enabled, draftPreserved: true }));
    }
    for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }, { width: 320, height: 640 }]) {
      await page.setViewportSize(viewport);
      await page.waitForTimeout(400);
      const layout = await page.evaluate(() => {
        const toolbar = document.querySelector('#local-mode-toolbar').getBoundingClientRect();
        const composer = document.querySelector('.conversation-chat-form').getBoundingClientRect();
        return { toolbarBottom: toolbar.bottom, composerTop: composer.top, composerBottom: composer.bottom,
                 scrollWidth: document.documentElement.scrollWidth, innerWidth, innerHeight };
      });
      assert.ok(layout.composerTop >= layout.toolbarBottom);
      assert.ok(layout.composerBottom <= layout.innerHeight + 1);
      assert.equal(layout.scrollWidth, layout.innerWidth);
      const screenshot = await page.screenshot();
      console.log(JSON.stringify({ viewport, layout, screenshotBytes: screenshot.length,
        ...(process.env.QWEN_LIVE_IMAGES === '1' ? { image: screenshot.toString('base64') } : {}) }));
    }
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
