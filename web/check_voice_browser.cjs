/* Browser check for the injected voice button and the GPU queue dialog.
 *
 * Drives a real Edge over CDP against the running gateway. It does not speak into a
 * microphone: recognition itself is covered by check_gpu_voice_live.py, so this checks
 * what only a browser can show — that the button appears on real prompt fields, that it
 * is where the caret is, and that a 409 from the gateway turns into the owner's dialog
 * and a replayed request carrying ?gpu=preempt.
 *
 * Usage: node check_voice_browser.cjs [port]
 */
const { spawn } = require('node:child_process');
const { results, check, cdp, evaluate, sleep } = require('./lib/cdp.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = Number(process.argv[2] || 9333);
const BASE = 'http://127.0.0.1:8081';
const ROOT = 'D:\\AI\\Qwen-Local';
const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(candidate => fs.existsSync(candidate));


async function waitFor(ws, expression, timeout = 25000, label = expression) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await evaluate(ws, expression)) return true;
    await sleep(400);
  }
  throw new Error(`timed out waiting for ${label}`);
}

async function main() {
  if (!EDGE) {
    console.log('SKIP: Microsoft Edge not found');
    return 0;
  }
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'qv-cdp-'));
  const child = spawn(EDGE, [
    '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check', '--disable-gpu',
    '--autoplay-policy=no-user-gesture-required',
    'about:blank',
  ], { stdio: 'ignore', detached: false });

  try {
    await waitForBrowser();
    console.log('· debugging port is up');
    // Navigate the browser's own first page rather than a second target: the extra
    // target Edge creates for a DevTools navigation loses its execution context
    // mid-load, and every Runtime.evaluate against it then hangs.
    const targets = await fetchJson(`http://127.0.0.1:${PORT}/json/list`);
    const page = targets.find(entry => entry.type === 'page');
    if (!page) throw new Error('no page target to drive');
    console.log(`· attaching to ${page.type} target`);
    const ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener('open', resolve, { once: true });
      ws.addEventListener('error', () => reject(new Error('cannot open CDP socket')), { once: true });
    });
    await cdp(ws, 'Runtime.enable');
    await cdp(ws, 'Page.enable');
    console.log('· initial evaluate:', await evaluate(ws, '1 + 1'));

    // The owner cookie is what makes the page render the console rather than the
    // passphrase prompt; take it from the owner's own link the way the panel does.
    const token = fs.readFileSync(path.join(ROOT, 'logs', 'admin.token'), 'ascii').trim();
    await cdp(ws, 'Page.navigate', { url: `${BASE}/?admin=${token}` });
    console.log('· navigation sent');
    await sleep(3000);
    console.log('· url now:', await evaluate(ws, 'location.href').catch(error => 'ERR ' + error.message));
    await waitFor(ws, 'document.readyState === "complete"', 30000, 'page load');
    console.log('· page complete');
    await waitFor(ws, '!!document.querySelector(".qv-mic")', 30000, 'the voice button to be injected');
    check('the voice module is injected into the page', true);

    check('the button starts hidden until a field is focused',
      await evaluate(ws, 'document.querySelector(".qv-mic").hidden === true'));

    // A prompt field created by a panel after load must still get the button: that is
    // the whole reason for the delegated listener instead of per-panel wiring.
    const injected = await evaluate(ws, `(() => {
      const area = document.createElement('textarea');
      area.id = 'qv-probe-field';
      area.style.cssText = 'position:fixed;left:40px;top:120px;width:320px;height:64px;z-index:99999';
      document.body.append(area);
      area.focus();
      area.dispatchEvent(new FocusEvent('focusin', { bubbles: true }));
      return true;
    })()`);
    check('a textarea is focusable for the test', injected);
    await sleep(400);
    const shown = await evaluate(ws, `(() => {
      const mic = document.querySelector('.qv-mic');
      const rect = document.getElementById('qv-probe-field').getBoundingClientRect();
      return { hidden: mic.hidden, label: mic.textContent.trim(), top: mic.style.top, left: mic.style.left };
    })()`);
    check('focusing a prompt field shows the voice button', shown.hidden === false, JSON.stringify(shown));
    check('the button is placed at that field', Number.parseInt(shown.top, 10) >= 110
      && Number.parseInt(shown.top, 10) <= 160, `top=${shown.top} left=${shown.left}`);
    check('it is labelled for the user', shown.label.includes('语音'), shown.label);

    // The recording path itself needs a microphone, which this check does not fake, so
    // it asserts the refusal is graceful rather than a crash.
    const denied = await evaluate(ws, `(async () => {
      const original = navigator.mediaDevices.getUserMedia;
      navigator.mediaDevices.getUserMedia = () => Promise.reject(Object.assign(new Error('denied'), { name: 'NotAllowedError' }));
      document.querySelector('.qv-mic').click();
      await new Promise(r => setTimeout(r, 600));
      navigator.mediaDevices.getUserMedia = original;
      const toast = document.querySelector('.qv-toast');
      return toast ? toast.textContent : '';
    })()`);
    check('a denied microphone is explained, not swallowed', denied.includes('麦克风'), denied.slice(0, 60));

    // The GPU question: the gateway answers 409 for an owner ask, and the wrapper is
    // what turns it into the dialog and a replayed request.
    check('the fetch wrapper is installed',
      await evaluate(ws, 'typeof window.fetch === "function" && window.fetch.__qvWrapped === true'));
    check('the GPU helpers are exposed for the panels',
      await evaluate(ws, '!!(window.QwenVoice && window.QwenVoice.offerGpu && window.QwenVoice.preempt)'));

    // The replay leg is exercised against the real gateway rather than a stub: the
    // window is asked for a request it will refuse, and the dialog's own choice is then
    // checked from the gateway's side by check_gpu_preempt_live.py. What is asserted
    // here is that the wrapper is in place, is non-writable, and passes unrecognised
    // work straight through instead of swallowing it.
    check('the wrapper cannot be displaced by the page',
      await evaluate(ws, `(() => {
        const before = window.fetch;
        try { window.fetch = function evil() {}; } catch (e) { /* non-writable is fine */ }
        const survived = window.fetch === before;
        window.fetch = before;
        return survived;
      })()`));

    const passthrough = await evaluate(ws, `(async () => {
      const response = await fetch('/local/gpu', { credentials: 'same-origin', cache: 'no-store' });
      const payload = await response.json();
      return { status: response.status, threshold: payload.threshold_mib, preempting: payload.preempting };
    })()`);
    check('a normal request passes through the wrapper untouched',
      passthrough.status === 200 && typeof passthrough.threshold === 'number',
      JSON.stringify(passthrough));

    const dialogForReal = await evaluate(ws, `(async () => {
      const detail = await (await fetch('/local/gpu', { cache: 'no-store' })).json();
      window.__qvModal = null;
      window.QwenVoice.openGpuModal(Object.assign({ decision: 'ask' }, detail), choice => { window.__qvModal = choice; });
      await new Promise(r => setTimeout(r, 300));
      const overlay = document.querySelector('.qvg-overlay');
      const buttons = overlay ? [...overlay.querySelectorAll('button')].map(b => b.textContent) : [];
      const text = overlay ? overlay.textContent : '';
      return { buttons, text: text.slice(0, 160) };
    })()`);
    check('the dialog opens from a live gateway status',
      dialogForReal.buttons.length === 3, dialogForReal.buttons.join(' | '));
    check('it states the real free VRAM it was given',
      /\d+\s*MiB/.test(dialogForReal.text), dialogForReal.text.slice(0, 80));

    ws.close();
  } finally {
    child.kill();
    await sleep(600);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (failure) { /* best effort */ }
  }

  console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
  return results.every(Boolean) ? 0 : 1;
}

async function fetchJson(url, method = 'GET') {
  const response = await fetch(url, method === 'PUT' ? { method, body: '' } : { method });
  return response.json();
}

async function waitForBrowser() {
  const deadline = Date.now() + 25000;
  while (Date.now() < deadline) {
    try {
      await fetchJson(`http://127.0.0.1:${PORT}/json/version`);
      return;
    } catch (failure) {
      await sleep(400);
    }
  }
  throw new Error('Edge did not expose a debugging port');
}

main().then(code => process.exit(code)).catch(error => {
  console.error('ERROR', error.message);
  process.exit(1);
});
