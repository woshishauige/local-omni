/* Same live-typing path, but against the real gateway: no stub, fake microphone.
 *
 * The scripted version proves the client wiring; this one proves the whole path — worklet
 * → gateway → local model → text in the field — with the browser's own fake audio device.
 */
const { spawn } = require('node:child_process');
const { results, check, cdp, evaluate, sleep } = require('./lib/cdp.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = Number(process.argv[2] || 9377);
const BASE = 'http://127.0.0.1:8081';
const ROOT = 'D:\\AI\\Qwen-Local';
const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(candidate => fs.existsSync(candidate));


async function waitFor(ws, expression, timeout, label) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await evaluate(ws, expression)) return true;
    await sleep(300);
  }
  throw new Error('timed out waiting for ' + label);
}

(async () => {
  if (!EDGE) { console.log('SKIP: Edge not found'); return; }
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'qv-real-'));
  const child = spawn(EDGE, [
    '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check',
    '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream',
    '--autoplay-policy=no-user-gesture-required', 'about:blank',
  ], { stdio: 'ignore' });
  try {
    for (let i = 0; i < 60; i++) {
      try { await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json(); break; } catch { await sleep(400); }
    }
    const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
    const ws = new WebSocket(list.find(entry => entry.type === 'page').webSocketDebuggerUrl);
    await new Promise(resolve => ws.addEventListener('open', resolve, { once: true }));
    await cdp(ws, 'Runtime.enable');
    await cdp(ws, 'Page.enable');
    const token = fs.readFileSync(path.join(ROOT, 'logs', 'admin.token'), 'ascii').trim();
    await cdp(ws, 'Page.navigate', { url: `${BASE}/?admin=${token}` });
    await waitFor(ws, 'document.readyState === "complete"', 30000, 'page load');
    await waitFor(ws, '!!document.querySelector(".qv-mic")', 30000, 'voice module');

    await evaluate(ws, `(() => {
      const area = document.createElement('textarea');
      area.id = 'qv-real-field';
      area.style.cssText = 'position:fixed;left:40px;top:140px;width:420px;height:80px;z-index:99999';
      document.body.append(area);
      area.focus();
      area.dispatchEvent(new FocusEvent('focusin', { bubbles: true }));
      window.__qvBytes = 0;
      const real = window.fetch.bind(window);
      window.__qvReal = real;
      return true;
    })()`);

    await evaluate(ws, `document.querySelector('.qv-mic').click()`);
    let sawRequest = false;
    let textWhileRecording = '';
    for (let step = 0; step < 20; step++) {
      await sleep(500);
      const probe = await evaluate(ws, `(() => {
        const live = window.QwenVoice.state.live;
        const field = document.getElementById('qv-real-field');
        return {
          value: (field || {}).value || '',
          queued: live ? live.queue.length : -1,
          sending: live ? live.sending : null,
          state: live ? live.context.state : 'no live',
          voiceText: (window.QwenVoice.state.voice || {}).text || '',
          duration: (window.QwenVoice.state.voice || {}).duration_seconds || 0,
        };
      })()`);
      if (probe.duration > 0) sawRequest = true;
      if (probe.value) textWhileRecording = probe.value;
      if (step % 4 === 0 || probe.value) {
        console.log(`  +${(step + 1) * 0.5}s field=${JSON.stringify(probe.value)} server=${JSON.stringify(probe.voiceText)} heard=${probe.duration}s`);
      }
      if (probe.value) break;
    }
    await evaluate(ws, `document.querySelector('.qv-mic').click()`);
    await sleep(3000);
    const final = await evaluate(ws, `(document.getElementById('qv-real-field') || {}).value || ''`);
    console.log('final field:', JSON.stringify(final));
    console.log('audio reached the gateway:', sawRequest);
    console.log('text appeared while recording:', Boolean(textWhileRecording));
    console.log('RESULT:', sawRequest && textWhileRecording ? 'PASS' : 'FAIL');
  } finally {
    child.kill();
    await sleep(500);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (failure) { /* best effort */ }
  }
})().catch(error => { console.error('ERROR', error.message); process.exit(1); });
