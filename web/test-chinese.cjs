const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require("./lib/playwright.cjs");

async function run() {
  const browser = await chromium.launch({ headless: true, channel: 'msedge' });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'en-US' });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/props*', route => route.fulfill({ json: {
      default_generation_settings: { n_ctx: 16384, params: { temperature: 0.7, top_p: 0.9, top_k: 20 } },
      modalities: { vision: true, audio: false, video: false }, model_alias: 'qwen3.8-27b-local',
      model_path: 'Qwen3.8-27B.gguf', total_slots: 1,
    } }));
    await page.route('**/v1/models*', route => route.fulfill({ json: { object: 'list', data: [{ id: 'qwen3.8-27b-local' }] } }));
    await page.route('**/tools*', route => route.fulfill({ json: [] }));
    await page.route('**/local/status', route => route.fulfill({ json: {
      mode: 'vision', state: 'ready', mtp_enabled: false, active_requests: 0, revision: 1,
    } }));
    await page.goto('http://127.0.0.1:8081/?qwen_ui=zh-CN-v3');
    const composer = page.locator('.conversation-chat-form textarea');
    await composer.waitFor();
    assert.equal(await composer.getAttribute('placeholder'), '输入消息…');
    assert.equal(await page.locator('html').getAttribute('lang'), 'zh-CN');
    await page.getByRole('button', { name: '设置', exact: true }).click();
    for (const section of ['常规', '显示', '工具', '智能体', '导入与导出', '采样与惩罚', '开发者']) {
      await page.getByRole('button', { name: section, exact: true }).first().click();
      await page.waitForTimeout(80);
      const text = await page.locator('[role="dialog"]').innerText();
      if (process.env.QWEN_ZH_VERBOSE === '1') console.log('SECTION ' + section + '\n' + text);
      const english = await page.locator('[role="dialog"]').evaluate(dialog => {
        const walker = document.createTreeWalker(dialog, NodeFilter.SHOW_TEXT);
        const result = [];
        while (walker.nextNode()) {
          const node = walker.currentNode;
          if (!node.parentElement.closest('textarea,input,script,style,pre,code') &&
              node.parentElement.getClientRects().length && /[A-Za-z]{3,}[ /&]+[A-Za-z]{3,}/.test(node.data)) result.push(node.data.trim());
        }
        return result;
      });
      assert.ok(english.every(value => /[\u4e00-\u9fff]/.test(value)), 'Untranslated settings text: ' + english.join('; '));
    }
    await page.keyboard.press('Escape');
    await page.locator('button[aria-label="展开导航"]').click();
    await page.getByText('暂无对话', { exact: true }).waitFor();
    await page.getByText('搜索', { exact: true }).click();
    await page.getByPlaceholder('搜索对话...').waitFor();
    await page.getByText('输入内容以查看结果', { exact: true }).waitFor();
    await page.keyboard.press('Escape');
    // The native chat attachment input; the generate view has its own file
    // inputs behind .qg-view (reference images) and the video view behind
    // .qv-view (.qv-file), so the selector must exclude both.
    await page.locator('input[type="file"]:not(.qg-reference-input):not(.qv-file)').setInputFiles('D:/AI/deployment/qwen-vision-test.png');
    await page.getByRole('button', { name: '移除', exact: true }).waitFor();
    await page.waitForFunction(() => document.querySelector('[data-mode="text"]').disabled);
    await page.getByRole('button', { name: '移除', exact: true }).click();
    await page.waitForFunction(() => !document.querySelector('[data-mode="text"]').disabled);
    await page.route('**/v1/chat/completions', async route => {
      const request = route.request().postDataJSON();
      assert.equal(request.messages[0].content, 'Keep this English user message unchanged.');
      assert.equal(request.tools[0].function.description, 'Returns the current local date and time in ISO 8601 format, with the IANA time zone name');
      const chunk = content => ({ id: 'local-translation-test', object: 'chat.completion.chunk',
        model: 'qwen3.8-27b-local', choices: [{ index: 0, delta: { content }, finish_reason: null }] });
      await route.fulfill({ contentType: 'text/event-stream', body:
        'data: ' + JSON.stringify(chunk('English content must remain unchanged.\n\n```js\nconsole.log("Hello");\n```')) + '\n\n' +
        'data: ' + JSON.stringify({ ...chunk(''), choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] }) + '\n\n' +
        'data: [DONE]\n\n' });
    });
    await composer.fill('Keep this English user message unchanged.');
    await page.locator('.conversation-chat-form button[type="submit"]').click();
    await page.getByText('English content must remain unchanged.', { exact: true }).waitFor();
    assert.match(page.url(), /#\/chat\//);
    await page.getByTitle('复制代码', { exact: true }).waitFor();
    assert.equal(await page.locator('pre code').innerText(), 'console.log("Hello");\n');
    await page.reload();
    await composer.waitFor();
    assert.equal(await composer.getAttribute('placeholder'), '输入消息…');
    await page.getByText('English content must remain unchanged.', { exact: true }).waitFor();
    assert.ok((await page.title()).includes('Qwen 本地'));
    const screenshots = path.join(__dirname, 'localization/screenshots');
    await fs.mkdir(screenshots, { recursive: true });
    const collapseNavigation = page.locator('button[aria-label="收起导航"]');
    if (await collapseNavigation.isVisible()) await collapseNavigation.click();
    for (const [width, height] of [[1440, 900], [390, 844], [320, 640]]) {
      await page.setViewportSize({ width, height });
      await page.waitForTimeout(200);
      const bounds = await page.locator('body').evaluate(() => {
        const toolbar = document.querySelector('#local-mode-toolbar').getBoundingClientRect();
        const form = document.querySelector('.conversation-chat-form').getBoundingClientRect();
        return { scroll: document.documentElement.scrollWidth, width: innerWidth,
          toolbarBottom: toolbar.bottom, formTop: form.top, formRight: form.right };
      });
      assert.ok(bounds.scroll <= width + 1, 'Horizontal page overflow at ' + width);
      assert.ok(bounds.formTop >= bounds.toolbarBottom && bounds.formRight <= width + 1, 'Overlapping composer at ' + width);
      await page.screenshot({ path: path.join(screenshots, 'chat-' + width + '.png') });
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('button[aria-label="展开导航"]').click();
    await page.getByText('设置', { exact: true }).click();
    await page.locator('[role="dialog"]').waitFor();
    await page.waitForTimeout(500);
    await page.screenshot({ path: path.join(screenshots, 'settings-390.png') });
    await page.keyboard.press('Escape');
    const auth = await context.newPage();
    await auth.setViewportSize({ width: 390, height: 844 });
    auth.on('pageerror', error => errors.push(error.message));
    await auth.route('**/props*', route => route.fulfill({ status: 401, json: { error: { message: 'access denied' } } }));
    await auth.route('**/v1/models*', route => route.fulfill({ status: 401, json: { error: { message: 'access denied' } } }));
    await auth.route('**/local/status', route => route.fulfill({ json: {
      mode: 'text', state: 'ready', mtp_enabled: true, active_requests: 0, revision: 1,
    } }));
    await auth.goto('http://127.0.0.1:8081/?qwen_ui=zh-CN-v3');
    await auth.getByRole('button', { name: '输入 API 密钥', exact: true }).click();
    await auth.locator('#api-key-input').fill('local-test-key');
    await auth.getByRole('button', { name: '保存并重试', exact: true }).click();
    await auth.getByText('API 密钥无效，请检查后重试', { exact: true }).waitFor();
    await auth.waitForTimeout(500);
    await auth.screenshot({ path: path.join(screenshots, 'authentication-390.png') });
    await auth.close();
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ passed: true, settingsSections: 7, search: true,
      attachmentGuard: true, conversationReload: true, contentAndCodePreserved: true,
      authentication: true, viewports: [1440, 390, 320], screenshots }));
  } finally {
    await browser.close();
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
