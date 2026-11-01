/* Voice must be visibly unavailable on a model that cannot serve it.
 *
 * The rule is the gateway's (`/local/voice/config`), and this checks that the toolbar relays
 * it. Since 2026-09-23 the voice profile is the ternary 27B (measured: brain @8192 plus the
 * recogniser came to 10725 of 12227 MiB), so the 9B is the one that must look unavailable:
 * tab disabled, reason on hover, and clicking it does not open the view. Switching models for
 * the test is the real thing, not a stub — the point is that the button follows the model.
 *
 * Usage: node check_voice_availability.cjs [port]
 */
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = Number(process.argv[2] || 9413);
const ROOT = 'D:\\AI\\Qwen-Local';
const BASE = 'http://127.0.0.1:8081';
const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(candidate => fs.existsSync(candidate));

const results = [];
function check(label, ok, detail = '') {
  results.push(Boolean(ok));
  console.log(`${ok ? 'PASS' : 'FAIL'} ${label.padEnd(56)} ${detail}`);
}
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function cdp(ws, method, params = {}, timeout = 40000) {
  const id = Math.floor(Math.random() * 1e9);
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(method + ' timed out')), timeout);
    const onMessage = event => {
      const message = JSON.parse(event.data);
      if (message.id !== id) return;
      clearTimeout(timer);
      ws.removeEventListener('message', onMessage);
      message.error ? reject(new Error(method + ': ' + message.error.message)) : resolve(message.result);
    };
    ws.addEventListener('message', onMessage);
  });
}

async function evaluate(ws, expression) {
  const result = await cdp(ws, 'Runtime.evaluate',
    { expression, awaitPromise: true, returnByValue: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
  return result.result.value;
}

/* Owner session, so the test can switch models the way the panel does. */
async function ownerSession() {
  const link = fs.readFileSync(path.join(ROOT, 'logs', 'admin-link.txt'), 'ascii').trim();
  const token = new URL(link).searchParams.get('admin');
  const login = await fetch(`${BASE}/?admin=${encodeURIComponent(token)}`, { redirect: 'manual' });
  const session = login.headers.getSetCookie().map(entry => entry.split(';')[0]).join('; ');
  if (!session) throw new Error('no owner session cookie');
  return session;
}

async function switchModel(session, profile, seconds = 300) {
  await fetch(`${BASE}/local/model`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', cookie: session },
    body: JSON.stringify({ profile }),
  });
  const deadline = Date.now() + seconds * 1000;
  while (Date.now() < deadline) {
    const status = await (await fetch(`${BASE}/local/status`, { headers: { cookie: session } })).json();
    if (status.state === 'ready' && status.model_profile === profile) return status;
    await sleep(3000);
  }
  throw new Error('model did not settle on ' + profile);
}

const voiceTab = `(() => {
  const button = document.querySelector('.local-mode-button[data-mode="voice"]');
  if (!button) return JSON.stringify({ missing: true });
  return JSON.stringify({
    disabled: button.disabled,
    unsupported: button.dataset.unsupported === 'true',
    title: button.title,
    label: button.textContent.trim(),
  });
})()`;

(async () => {
  if (!EDGE) { console.log('SKIP: Edge not found'); return; }
  const session = await ownerSession();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'qv-voice-avail-'));
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
    await cdp(ws, 'Runtime.enable');
    await cdp(ws, 'Page.enable');

    console.log('切到 9B（非语音档位）并打开页面…');
    await switchModel(session, 'qwen3.5-9b-uncensored');
    const link = fs.readFileSync(path.join(ROOT, 'logs', 'admin-link.txt'), 'ascii').trim();
    await cdp(ws, 'Page.navigate', { url: link });
    await sleep(10000);

    const blocked = JSON.parse(await evaluate(ws, voiceTab));
    check('9B 下语音按钮存在（不消失、布局不跳）', blocked.missing !== true, JSON.stringify(blocked));
    check('9B 下语音按钮不可选', blocked.disabled === true, `disabled=${blocked.disabled}`);
    check('9B 下标注为不支持的硬件', blocked.unsupported === true && blocked.label.includes('不支持'),
      `label=${blocked.label}`);
    check('鼠标指向时给出锁定说明',
      typeof blocked.title === 'string' && blocked.title.includes('27B')
        && blocked.title.includes('qwen3.8-27b'),
      blocked.title ? blocked.title.slice(0, 70) : '(空)');

    // Clicking it anyway must not open the voice view.
    await evaluate(ws, `(() => {
      document.querySelector('.local-mode-button[data-mode="voice"]').click();
      return true;
    })()`);
    await sleep(1500);
    const afterClick = JSON.parse(await evaluate(ws, `(() => {
      const panel = document.querySelector('#local-voice-panel');
      return JSON.stringify({ open: panel ? !panel.hidden : null });
    })()`));
    check('强行点击也不会进入语音视图', afterClick.open !== true, `open=${afterClick.open}`);

    console.log('切到 27B（语音档位）…');
    await switchModel(session, 'qwen3.8-27b');
    // The toolbar notices the model by polling, and returns the tab once it is allowed.
    const available = await (async () => {
      for (let i = 0; i < 20; i++) {
        await sleep(2000);
        const state = JSON.parse(await evaluate(ws, voiceTab));
        if (state.disabled === false) return state;
      }
      return JSON.parse(await evaluate(ws, voiceTab));
    })();
    check('27B 下语音按钮可选', available.disabled === false, `disabled=${available.disabled}`);
    check('27B 下标注恢复为型号', available.unsupported !== true && available.label.includes('27B'),
      `label=${available.label}`);

    ws.close();
  } finally {
    child.kill();
    await sleep(700);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (failure) { /* Edge may hold a handle */ }
  }
  console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
  process.exit(results.every(Boolean) ? 0 : 1);
})().catch(error => { console.error('ERROR', error.message); process.exit(1); });
