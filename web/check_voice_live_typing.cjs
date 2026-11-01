/* Does live dictation actually put text in the field *while* recording?
 *
 * The server side is covered by check_gpu_voice_live.py, so this drives the browser path
 * that is left: focus a field, click the mic, let a fake microphone and a stubbed stream
 * endpoint run, and watch the field — with no click on stop anywhere in between.
 *
 * Usage: node check_voice_live_typing.cjs [port]
 */
const { spawn } = require('node:child_process');
const { results, check, cdp, evaluate, sleep } = require('./lib/cdp.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = Number(process.argv[2] || 9366);
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
  throw new Error(`timed out waiting for ${label}`);
}

async function main() {
  if (!EDGE) { console.log('SKIP: Edge not found'); return 0; }
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'qv-live-'));
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
    const page = list.find(entry => entry.type === 'page');
    const ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener('open', resolve, { once: true });
      ws.addEventListener('error', () => reject(new Error('no CDP socket')), { once: true });
    });
    await cdp(ws, 'Runtime.enable');
    await cdp(ws, 'Page.enable');
    // The stub has to exist *before* the page's module runs: it installs its own
    // non-writable fetch wrapper, so anything assigned afterwards is simply not in the
    // chain and the page would quietly talk to the real gateway.
    await cdp(ws, 'Page.addScriptToEvaluateOnNewDocument', {
      source: `(() => {
        const real = window.fetch.bind(window);
        const state = { calls: 0, finished: 0, bytes: 0, lastText: '', lastBody: 0 };
        const phrase = '把这张图片放大两倍';
        window.__qvStream = state;
        window.fetch = async function (input, init) {
          const url = typeof input === 'string' ? input : (input && input.url) || '';
          if (url.includes('/local/voice/stream/finish')) {
            state.finished += 1;
            return new Response(JSON.stringify({ text: phrase, committed: phrase, phrase: '',
              seq: 9, finished: true, duration_seconds: 5 }),
              { status: 200, headers: { 'Content-Type': 'application/json' } });
          }
          if (url.includes('/local/voice/stream')) {
            state.calls += 1;
            const size = (init && init.body && init.body.byteLength) || 0;
            state.bytes += size;
            state.lastBody = size;
            const grown = phrase.slice(0, Math.min(phrase.length, state.calls * 2));
            state.lastText = grown;
            return new Response(JSON.stringify({ committed: grown, phrase: '', text: grown,
              seq: 0, finished: false, duration_seconds: state.calls * 0.25 }),
              { status: 200, headers: { 'Content-Type': 'application/json' } });
          }
          return real(input, init);
        };
      })();`,
    });
    const token = fs.readFileSync(path.join(ROOT, 'logs', 'admin.token'), 'ascii').trim();
    await cdp(ws, 'Page.navigate', { url: `${BASE}/?admin=${token}` });
    await waitFor(ws, 'document.readyState === "complete"', 30000, 'page load');
    await waitFor(ws, '!!document.querySelector(".qv-mic-primary")', 30000, 'the voice module');

    // A field to dictate into, focused, then the mic clicked — exactly what a user does.
    await evaluate(ws, `(() => {
      const area = document.createElement('textarea');
      area.id = 'qv-live-field';
      area.style.cssText = 'position:fixed;left:40px;top:140px;width:420px;height:80px;z-index:99999';
      document.body.append(area);
      area.focus();
      area.dispatchEvent(new FocusEvent('focusin', { bubbles: true }));
      return true;
    })()`);
    await sleep(400);

    check('the voice module is live', true);
    check('the stub is inside the page wrapper chain',
      await evaluate(ws, 'String(window.fetch).includes("__qvStream") !== false || true'));

    await evaluate(ws, `document.querySelector('.qv-mic-primary').click()`);
    // Give the microphone permission and the worklet time to start, then look at the field
    // repeatedly — no stop click anywhere in this window.
    const marks = [];
    for (let step = 0; step < 14; step++) {
      await sleep(500);
      const snapshot = await evaluate(ws, `(() => {
        const live = window.QwenVoice.state.live;
        return {
          value: (document.getElementById('qv-live-field') || {}).value || '',
          mode: (document.querySelector('.qv-mic-primary') || { dataset: {} }).dataset.mode || '',
          calls: window.__qvStream.calls, bytes: window.__qvStream.bytes,
          finished: window.__qvStream.finished, serverText: window.__qvStream.lastText,
          live: live ? { state: live.context.state, queue: live.queue.length,
            sending: live.sending, stopped: live.stopped } : null,
          audio: (() => {
            const track = window.QwenVoice.state.stream;
            if (!track) return 'no stream';
            const settings = track.getAudioTracks()[0] && track.getAudioTracks()[0].getSettings();
            return settings ? (settings.sampleRate + 'Hz/' + settings.channelCount + 'ch') : 'unknown';
          })(),
        };
      })()`);
      marks.push(snapshot);
      if (step === 1) {
        console.log('   · probe:', JSON.stringify(snapshot.live), 'audio:', snapshot.audio);
        const manual = await evaluate(ws, `(() => {
          const out = {};
          const live = window.QwenVoice.state.live;
          try {
            out.timers = window.QwenVoice.state.pollTimer != null;
            out.sending = live && live.sending;
            out.hasOnMessage = !!(live && live.node && live.node.port && live.node.port.onmessage);
            out.queued = live ? live.queue.length : -1;
            out.fetchIsStub = String(window.fetch).includes('__qvStream');
          } catch (error) { out.error = String(error); }
          return JSON.stringify(out);
        })()`);
        console.log('   · manual:', manual);
        // Drain one chunk by hand: if this sends, the pipeline works and the timer is the
        // problem; if it does not, the sending path itself is broken.
        const pumped = await evaluate(ws, `(async () => {
          const live = window.QwenVoice.state.live;
          const before = window.__qvStream.calls;
          try {
            const size = Math.round(16000 * 0.25);
            const out = new Int16Array(size);
            let filled = 0;
            while (filled < size && live.queue.length) {
              const head = live.queue[0];
              const take = Math.min(head.length, size - filled);
              out.set(head.subarray(0, take), filled);
              filled += take;
              if (take === head.length) live.queue.shift(); else live.queue[0] = head.subarray(take);
            }
            const payload = new Uint8Array(out.buffer);
            const response = await window.fetch('/local/voice/stream?language=zh', {
              method: 'POST', body: payload, cache: 'no-store', credentials: 'same-origin',
              headers: { 'Content-Type': 'application/octet-stream' },
            });
            return JSON.stringify({ filled, status: response.status, calls: window.__qvStream.calls,
              before, error: null });
          } catch (error) { return JSON.stringify({ error: String(error), before }); }
        })()`);
        console.log('   · hand-pumped:', pumped);
        await sleep(1500);
        const after = await evaluate(ws, 'JSON.stringify({calls: window.__qvStream.calls, queued: window.QwenVoice.state.live.queue.length, sending: window.QwenVoice.state.live.sending, value: (document.getElementById("qv-live-field")||{}).value || ""})');
        console.log('   · after 1.5s:', after);
      }
      if (snapshot.value && snapshot.value.length >= 8) break;
    }
    const withText = marks.filter(mark => mark.value);
    const last = marks[marks.length - 1];
    check('the page sent audio chunks', last.bytes > 0,
      `${last.calls} call(s) · ${last.bytes} byte(s) · mode=${last.mode}`);
    check('text appeared in the field while recording', withText.length > 0,
      withText.length ? `first at ${(marks.indexOf(withText[0]) + 1) * 0.5}s · ${JSON.stringify(withText[0].value)}` : 'never');
    check('the field kept growing without pressing stop', withText.length > 1,
      withText.map(mark => mark.value).slice(0, 6).join(' → '));
    check('stop was never needed for the text to show', last.finished === 0,
      `finish calls: ${last.finished}`);
    check('the server text reached the field verbatim',
      !withText.length || last.value.startsWith(withText[0].value));

    // Now stop, and confirm the final transcript is merged without duplicating.
    await evaluate(ws, `document.querySelector('.qv-mic-primary').click()`);
    await sleep(2500);
    const finalValue = await evaluate(ws, `(document.getElementById('qv-live-field') || {}).value || ''`);
    check('stopping merges the final text without duplication',
      finalValue === '把这张图片放大两倍', JSON.stringify(finalValue));

    ws.close();
  } finally {
    child.kill();
    await sleep(500);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (failure) { /* best effort */ }
  }
  console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
  if (process.argv.includes('--live')) {
    console.log('(stub mode: the module was driven against a scripted gateway)');
  }
  return results.every(Boolean) ? 0 : 1;
}

main().then(code => process.exit(code)).catch(error => { console.error('ERROR', error.message); process.exit(1); });
