/* One-off: find which element inside the voice rail is widening it.
 *
 * The rail measured 244px at its left edge but 1166px wide, which pushed the transcript to
 * zero. This prints the computed grid and each child's width so the culprit is named instead
 * of guessed at.
 *
 * Usage: node measure_voice_rail.cjs [port]
 */
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = Number(process.argv[2] || 9421);
const ROOT = 'D:\\AI\\Qwen-Local';
const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(candidate => fs.existsSync(candidate));
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

(async () => {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'qv-rail-'));
  const child = spawn(EDGE, ['--headless=new', `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profile}`, '--no-first-run', '--window-size=1440,900', 'about:blank'],
    { stdio: 'ignore' });
  try {
    for (let i = 0; i < 50; i++) {
      try { await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json(); break; } catch { await sleep(400); }
    }
    const tabs = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
    const ws = new WebSocket(tabs.find(t => t.type === 'page').webSocketDebuggerUrl);
    await new Promise(resolve => ws.addEventListener('open', resolve, { once: true }));
    let counter = 0;
    const send = (method, params) => new Promise(resolve => {
      const id = ++counter;
      const handler = event => {
        const message = JSON.parse(event.data);
        if (message.id !== id) return;
        ws.removeEventListener('message', handler);
        resolve(message.result);
      };
      ws.addEventListener('message', handler);
      ws.send(JSON.stringify({ id, method, params }));
    });
    await send('Runtime.enable');
    await send('Page.enable');
    const link = fs.readFileSync(path.join(ROOT, 'logs', 'admin-link.txt'), 'ascii').trim();
    await send('Page.navigate', { url: link });
    await sleep(9000);
    await send('Runtime.evaluate', { expression:
      "document.querySelector('.local-mode-button[data-mode=\"voice\"]').click()" });
    await sleep(2500);

    const probe = `(() => {
      const panel = document.querySelector('#local-voice-panel');
      const body = panel.querySelector('.local-voice-body');
      const side = panel.querySelector('.local-voice-side');
      const main = panel.querySelector('.local-voice-log');
      const box = node => { const r = node.getBoundingClientRect(); return Math.round(r.width); };
      return JSON.stringify({
        sheets: [...document.styleSheets].map(s => String(s.href || 'inline'))
          .filter(href => href.includes('voice-panel')),
        bodyColumns: getComputedStyle(body).gridTemplateColumns,
        bodyWidth: box(body),
        sideWidth: box(side), sideScroll: side.scrollWidth,
        sideMinWidth: getComputedStyle(side).minWidth,
        mainWidth: box(main),
        children: [...side.children].map(child =>
          (child.className || child.tagName) + ' w=' + box(child) + ' scroll=' + child.scrollWidth),
        rows: [...side.querySelectorAll('.local-voice-history-item')].slice(0, 3)
          .map(row => box(row) + '/' + row.scrollWidth),
        titles: [...side.querySelectorAll('.local-voice-history-title')].slice(0, 3)
          .map(node => box(node) + ' :: ' + node.textContent.slice(0, 30)),
      }, null, 1);
    })()`;
    const result = await send('Runtime.evaluate', { expression: probe, returnByValue: true });
    console.log(result.result.value);
  } finally {
    child.kill();
    await sleep(500);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (failure) { /* handle held */ }
  }
})().catch(error => { console.error('ERROR', error.message); process.exit(1); });
