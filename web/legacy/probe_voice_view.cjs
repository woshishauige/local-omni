/* 打开语音页时到底发生了什么？
 *
 * 所有者反馈：「怎么切到语音通话的时候还在模式切换」。这个探针只观察、不改变：
 * 点「语音对话」标签前后，记录
 *   1. 前端有没有发出会切换后端的请求（/local/mode、/local/voice/mode、/local/model）；
 *   2. 后端 /local/status 的 mode / state / loaded_profile 有没有变；
 *   3. 工具栏自己的状态文字（"模式切换中"之类）与语音页的提示行。
 * 用真实 Edge + 面板同款入口，不占显卡（不点"开始说话"）。
 */
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = 9427;
const ROOT = 'D:\\AI\\Qwen-Local';
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function cdp(ws, method, params = {}, timeout = 30000) {
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
  const result = await cdp(ws, 'Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
  return result.result.value;
}

const status = ws => evaluate(ws, `fetch('/local/status').then(r => r.json())
  .then(s => JSON.stringify({ mode: s.mode, state: s.state, loaded: s.loaded_profile,
    ctx: s.context_size, requests: s.active_requests }))`);

(async () => {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'qv-voiceview-'));
  const child = spawn(EDGE, ['--headless=new', `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profile}`, '--no-first-run', '--window-size=1440,900', 'about:blank'],
    { stdio: 'ignore' });
  try {
    for (let i = 0; i < 60; i++) {
      try { await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json(); break; } catch { await sleep(400); }
    }
    const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
    const ws = new WebSocket(list.find(t => t.type === 'page').webSocketDebuggerUrl);
    await new Promise(resolve => ws.addEventListener('open', resolve, { once: true }));
    await cdp(ws, 'Runtime.enable');
    await cdp(ws, 'Page.enable');
    const token = fs.readFileSync(path.join(ROOT, 'logs', 'admin.token'), 'ascii').trim();
    await cdp(ws, 'Page.navigate', { url: `http://127.0.0.1:8081/?admin=${token}` });
    await sleep(9000);

    const before = JSON.parse(await status(ws));
    const toolbarBefore = await evaluate(ws,
      `(document.querySelector('.local-toolbar-status') || {}).textContent || ''`);

    // 只观察前端请求：包住 fetch，把"会切换后端"的那几个地址记下来（仍然照常放行）。
    await evaluate(ws, `(() => {
      window.__voiceViewCalls = [];
      if (window.__voiceViewWrapped) return 'already';
      window.__voiceViewWrapped = true;
      const raw = window.fetch.bind(window);
      window.__rawVoiceFetch = raw;
      window.fetch = (input, init) => {
        const url = typeof input === 'string' ? input : (input && input.url) || '';
        const method = (init && init.method) || 'GET';
        if (method !== 'GET' && /\\/local\\/(mode|model|voice\\/mode|recover)/.test(url)) {
          window.__voiceViewCalls.push({ url, method, body: (init && init.body) || '' });
        }
        return raw(input, init);
      };
      return 'wrapped';
    })()`);

    const clicked = await evaluate(ws, `(() => {
      const tab = [...document.querySelectorAll('.local-mode-button')]
        .find(node => (node.textContent || '').includes('语音'));
      if (!tab) return 'no voice tab';
      tab.click();
      return 'clicked';
    })()`);
    await sleep(2500);

    const afterClick = JSON.parse(await status(ws));
    const calls = JSON.parse(await evaluate(ws, `JSON.stringify(window.__voiceViewCalls)`));
    const voiceDom = await evaluate(ws, `(() => {
      const panel = document.querySelector('.local-voice-panel') || document.querySelector('.local-voice');
      const visible = panel ? !panel.hidden && getComputedStyle(panel).display !== 'none' : false;
      const hint = panel ? (panel.querySelector('.local-voice-hint, .local-voice-note, [data-hint]') || {}).textContent : '';
      return JSON.stringify({ mounted: Boolean(panel), visible,
        active: document.querySelector('.local-mode-button[aria-pressed="true"]')?.textContent || '',
        hint: (hint || '').slice(0, 160),
        toolbarStatus: (document.querySelector('.local-status') || {}).textContent || '' });
    })()`);

    // 截图：工具栏 + 语音页顶部，给所有者看"打开语音页什么都不载入"的样子。
    const bar = await evaluate(ws, `(() => {
      const row = document.querySelector('.local-toolbar') || document.querySelector('.local-models');
      const r = row ? row.getBoundingClientRect() : { width: 1440, height: 40 };
      return JSON.stringify({ height: Math.ceil(r.height) + 40 });
    })()`);
    const shot = await cdp(ws, 'Page.captureScreenshot',
      { format: 'png', clip: { x: 0, y: 0, width: 1440, height: Math.max(120, JSON.parse(bar).height), scale: 1 } });
    const out = path.join(__dirname, 'shots', 'voice-view.png');
    fs.mkdirSync(path.dirname(out), { recursive: true });
    fs.writeFileSync(out, Buffer.from(shot.data, 'base64'));
    console.log('shot   :', out);

    console.log('before :', JSON.stringify(before));
    console.log('toolbar:', toolbarBefore.replace(/\s+/g, ' ').slice(0, 80));
    console.log('click  :', clicked);
    console.log('after  :', JSON.stringify(afterClick));
    console.log('switch calls:', JSON.stringify(calls));
    console.log('voice dom  :', voiceDom);

    ws.close();
  } finally {
    child.kill();
    await sleep(600);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch { /* Edge may hold a handle */ }
  }
})().catch(error => { console.error('ERROR', error.message); process.exit(1); });
