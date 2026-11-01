/* 语音页的音色列表来自网关，而不是抄在脚本里。
 *
 * 之前 `voice-panel.js` 把 9 个音色硬编码，网关的 `GET /local/voice/voices` 没人调用：网关那边
 * 改名/增删之后，面板仍旧展示旧名字，直到真的用那个音色去合成才失败。这个脚本用 CDP 的 Fetch
 * 域把该接口换成一份**不同的**名单，断言面板跟着变——这才叫"从网关读"。
 *
 * 用法：node check_voice_list_live.cjs [port]
 */
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = Number(process.argv[2] || 9510);
const ROOT = 'D:\\AI\\Qwen-Local';
const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(candidate => fs.existsSync(candidate));
const STUBBED = [
  { id: 'stub_one', label: '测试甲', kind: '男声' },
  { id: 'stub_two', label: '测试乙', kind: '女声' },
];

const results = [];
function check(label, ok, detail = '') {
  results.push(Boolean(ok));
  console.log(`${ok ? 'PASS' : 'FAIL'} ${label.padEnd(46)} ${detail}`);
}
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

const pending = new Map();
function cdp(ws, method, params = {}, timeout = 30000) {
  const id = Math.floor(Math.random() * 1e9);
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(method + ' timed out')), timeout);
    pending.set(id, { resolve, reject, timer, method });
  });
}

async function evaluate(ws, expression) {
  const result = await cdp(ws, 'Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
  return result.result.value;
}

(async () => {
  if (!EDGE) { console.log('SKIP: Edge not found'); return; }
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'qv-voices-'));
  const child = spawn(EDGE, ['--headless=new', `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profile}`, '--no-first-run', '--no-default-browser-check',
    '--window-size=1440,900', 'about:blank'], { stdio: 'ignore' });
  try {
    for (let i = 0; i < 60; i++) {
      try { await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json(); break; } catch { await sleep(400); }
    }
    const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
    const ws = new WebSocket(list.find(t => t.type === 'page').webSocketDebuggerUrl);
    await new Promise(resolve => ws.addEventListener('open', resolve, { once: true }));
    const seen = { asked: 0 };
    ws.addEventListener('message', event => {
      const message = JSON.parse(event.data);
      if (message.id && pending.has(message.id)) {
        const entry = pending.get(message.id);
        clearTimeout(entry.timer);
        pending.delete(message.id);
        message.error ? entry.reject(new Error(entry.method + ': ' + message.error.message)) : entry.resolve(message.result);
        return;
      }
      if (message.method === 'Fetch.requestPaused') {
        const { requestId, request } = message.params;
        if (request.url.includes('/local/voice/voices')) {
          seen.asked += 1;
          // 网关会用另一份名单回答：面板必须跟着它变。
          cdp(ws, 'Fetch.fulfillRequest', {
            requestId, responseCode: 200,
            responseHeaders: [{ name: 'Content-Type', value: 'application/json' },
                              { name: 'Cache-Control', value: 'no-store' }],
            body: Buffer.from(JSON.stringify({ engine: 'stub', ready: true, voices: STUBBED })).toString('base64'),
          }).catch(() => { /* 请求可能已被导航取消 */ });
          return;
        }
        cdp(ws, 'Fetch.continueRequest', { requestId }).catch(() => { /* 同上 */ });
      }
    });
    await cdp(ws, 'Runtime.enable');
    await cdp(ws, 'Page.enable');
    await cdp(ws, 'Fetch.enable', { patterns: [{ urlPattern: '*/local/voice/voices*' }] });
    const token = fs.readFileSync(path.join(ROOT, 'logs', 'admin.token'), 'ascii').trim();
    await cdp(ws, 'Page.navigate', { url: `http://127.0.0.1:8081/?admin=${token}` });
    for (let i = 0; i < 40; i++) {
      await sleep(500);
      const ready = await evaluate(ws, `!!document.querySelector('.local-voice-voice')`).catch(() => false);
      if (ready) break;
    }
    await sleep(2500);

    const picker = await evaluate(ws, `(() => {
      const buttons = [...document.querySelectorAll('.local-voice-voice')];
      return {
        ids: buttons.map(node => (node.querySelector('span') || {}).textContent || ''),
        pressed: buttons.filter(node => node.getAttribute('aria-pressed') === 'true').map(node => (node.querySelector('span') || {}).textContent),
      };
    })()`);
    check('面板真的问了网关', seen.asked >= 1, `请求 ${seen.asked} 次`);
    check('面板用的是网关给的名单（不是我文件里的）',
      JSON.stringify(picker.ids) === JSON.stringify(STUBBED.map(v => v.label)), picker.ids.join(' / '));
    check('默认选中项也在网关名单里', picker.pressed.length === 1 && STUBBED.some(v => v.label === picker.pressed[0]),
      picker.pressed.join(','));

    // 兜底：接口挂了也要有音色可选（原来的硬编码表就是干这个的）。
    await cdp(ws, 'Fetch.disable');
    await cdp(ws, 'Network.enable');
    await cdp(ws, 'Network.setBlockedURLs', { urls: ['*/local/voice/voices*'] });
    await cdp(ws, 'Page.navigate', { url: `http://127.0.0.1:8081/?admin=${token}` });
    await sleep(9000);
    const fallback = await evaluate(ws, `document.querySelectorAll('.local-voice-voice').length`);
    check('网关答不上来时仍有音色可选（兜底表）', fallback >= 5, `${fallback} 个`);
    await cdp(ws, 'Network.setBlockedURLs', { urls: [] });
  } finally {
    try { child.kill(); } catch { /* ignore */ }
    console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
    process.exitCode = results.every(Boolean) ? 0 : 1;
  }
})();
