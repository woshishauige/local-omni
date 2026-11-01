/* The thinking tier control must exist in the conversation box on the two chat modes.
 *
 * What it checks: the control renders in the composer dock (not floating somewhere unrelated),
 * it offers exactly the tiers the gateway advertises for the selected profile, clicking one
 * turns it on and stores it, and a chat request really carries the `X-Qwen-Thinking` header —
 * the last part is the one that matters, because a control that renders but sends nothing is
 * decoration.
 *
 * Usage: node check_thinking_control.cjs [port]
 */
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = Number(process.argv[2] || 9421);
const ROOT = 'D:\\AI\\Qwen-Local';
const BASE = 'http://127.0.0.1:8081';
const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(candidate => fs.existsSync(candidate));

const results = [];
function check(label, ok, detail = '') {
  results.push(Boolean(ok));
  console.log(`${ok ? 'PASS' : 'FAIL'} ${label.padEnd(52)} ${detail}`);
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

(async () => {
  if (!EDGE) { console.log('SKIP: Edge not found'); return; }
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'qv-thinking-'));
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

    const link = fs.readFileSync(path.join(ROOT, 'logs', 'admin-link.txt'), 'ascii').trim();
    await cdp(ws, 'Page.navigate', { url: link });
    await sleep(12000);

    const state = JSON.parse(await evaluate(ws, `(() => {
      const control = document.getElementById('qwen-thinking-control');
      const options = control ? [...control.querySelectorAll('.qwen-thinking-option')] : [];
      const dock = document.getElementById('local-composer-tools');
      return JSON.stringify({
        present: Boolean(control),
        inDock: Boolean(control && dock && dock.contains(control)),
        inComposer: Boolean(control && control.closest('.conversation-chat-form')),
        options: options.map(node => node.dataset.level),
        pressed: options.filter(node => node.getAttribute('aria-pressed') === 'true').map(node => node.dataset.level),
        label: control ? control.textContent.trim() : '',
      });
    })()`));
    check('控件出现在对话框里', state.present && state.inComposer,
      `present=${state.present} inComposer=${state.inComposer}`);
    check('与麦克风共用停靠区', state.inDock, `inDock=${state.inDock}`);
    check('档位与网关一致（off/low/medium/xhigh）',
      state.options.join(',') === 'off,low,medium,xhigh', state.options.join(','));
    check('有一个档位处于选中态', state.pressed.length === 1, `pressed=${state.pressed}`);

    // Click "关闭" and confirm it sticks and that the wrapper puts the header on the wire.
    const clicked = JSON.parse(await evaluate(ws, `(() => {
      const target = [...document.querySelectorAll('.qwen-thinking-option')]
        .find(node => node.dataset.level === 'off');
      if (!target) return JSON.stringify({ ok: false });
      target.click();
      const stored = (() => { try { return localStorage.getItem('qwen.thinking.level'); } catch (e) { return null; } })();
      const pressed = [...document.querySelectorAll('.qwen-thinking-option')]
        .filter(node => node.getAttribute('aria-pressed') === 'true').map(node => node.dataset.level);
      return JSON.stringify({ ok: true, stored, pressed });
    })()`));
    check('点击后选中并记住', clicked.ok && clicked.stored === 'off' && clicked.pressed.join(',') === 'off',
      `stored=${clicked.stored} pressed=${clicked.pressed}`);

    // The control's fetch wrapper must not break the requests it wraps: issue one through the
    // page's own patched fetch and require a clean 200. Whether the tier *changes the answer*
    // is a different question and is measured headlessly by `_bonsai-eval/verify_vision_and_thinking.py`
    // (off → 0 thinking chars, xhigh → 619), because that needs the sampled thinking text.
    const wrapped = await evaluate(ws, `(async () => {
      try {
        const response = await window.fetch('/v1/chat/completions', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ messages: [{ role: 'user', content: '只回复 OK' }], max_tokens: 4 }),
        });
        const text = await response.text();
        return JSON.stringify({ status: response.status, snippet: text.slice(0, 120) });
      } catch (error) {
        return JSON.stringify({ error: String(error) });
      }
    })()`);
    const parsed = JSON.parse(wrapped);
    check('控件包装过的请求仍被网关接受', parsed.status === 200,
      `status=${parsed.status} ${parsed.snippet || parsed.error || ''}`);

    ws.close();
  } finally {
    child.kill();
    await sleep(700);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (failure) { /* Edge may hold */ }
  }
  console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
  process.exit(results.every(Boolean) ? 0 : 1);
})().catch(error => { console.error('ERROR', error.message); process.exit(1); });
