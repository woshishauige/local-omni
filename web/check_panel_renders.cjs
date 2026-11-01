/* The desktop panel must render the chat, not a blank page.
 *
 * This exists because it did not: a wrapper in the injected voice module made
 * `window.fetch` non-writable, the bundled chat app assigns that property while booting,
 * and the resulting TypeError ("Cannot assign to read only property 'fetch'") stopped the
 * app dead. The page was white, with the error only visible in the browser console.
 *
 * The check drives the same URL the desktop entry opens and requires: no uncaught
 * exception, the composer present, and real text on screen.
 *
 * Usage: node check_panel_renders.cjs [port]
 */
const { spawn } = require('node:child_process');
const { results, check, cdp, evaluate, sleep, launchAndOpen } = require('./lib/cdp.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = Number(process.argv[2] || 9399);
const ROOT = 'D:\\AI\\Qwen-Local';
const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(candidate => fs.existsSync(candidate));


(async () => {
  if (!EDGE) { console.log('SKIP: Edge not found'); return; }
  // 浏览器、调试端口、登录与导航都在 web/lib/cdp.cjs 里（原来每个脚本各复制一遍）。
  const { profile, child, ws } = await launchAndOpen({ port: 9499 });
  try {
    const state = await evaluate(ws, `(() => {
      const text = document.body ? document.body.innerText : '';
      return JSON.stringify({
        readyState: document.readyState,
        title: document.title,
        htmlLength: document.body ? document.body.innerHTML.length : 0,
        textLength: text.length,
        hasComposer: !!document.querySelector('textarea, [contenteditable="true"]'),
        hasToolbar: !!document.querySelector('.local-mode-toolbar'),
        hasPicker: !!document.querySelector('.local-model-select'),
        hasMic: !!document.querySelector('.qv-mic'),
        greeting: /开始对话|新对话/.test(text),
      });
    })()`);
    const page = JSON.parse(state);

    check('the page finished loading', page.readyState === 'complete', page.readyState);
    check('no uncaught exception stopped the app', problems.length === 0,
      problems.length ? problems[0].slice(0, 120) : 'none');
    check('the shell rendered real content', page.htmlLength > 20000 && page.textLength > 60,
      `${page.htmlLength} bytes · ${page.textLength} chars of text`);
    check('the chat composer is present', page.hasComposer === true);
    check('the console toolbar is present', page.hasToolbar === true);
    check('the model picker is present', page.hasPicker === true);
    check('the voice button was injected', page.hasMic === true);
    check('the empty-conversation prompt is visible', page.greeting === true);

    ws.close();
  } finally {
    child.kill();
    await sleep(700);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (failure) { /* Edge may still hold a handle */ }
  }
  console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
  process.exit(results.every(Boolean) ? 0 : 1);
})().catch(error => { console.error('ERROR', error.message); process.exit(1); });
