/* 临时探针：量视频/生图面板的版面与提示词框，用于把进度卡片锁死、把麦克风放进框里。
 * 用法：node probe_panels.cjs [port]
 */
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = Number(process.argv[2] || 9530);
const ROOT = 'D:\\AI\\Qwen-Local';
const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(candidate => fs.existsSync(candidate));
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

const SNAP = `(() => {
  const round = rect => ({ x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) });
  const body = document.querySelector('.qv-body');
  const cols = body ? [...body.children].map(node => ({ cls: (node.className || '').toString().slice(0, 30), rect: round(node.getBoundingClientRect()) })) : [];
  const monitor = document.querySelector('.qv-monitor');
  const photo = document.querySelector('.qg-prompt-input');
  const negative = document.querySelector('.qg-negative-input');
  const videoPrompt = document.querySelector('.qv-pane:not([hidden]) .qv-textarea');
  const shotPrompts = [...document.querySelectorAll('.qv-shot-prompt')];
  const describe = node => node ? {
    tag: node.tagName.toLowerCase(), cls: (node.className || '').toString().slice(0, 46),
    rect: round(node.getBoundingClientRect()),
    parent: node.parentElement ? { cls: (node.parentElement.className || '').toString().slice(0, 34), pos: getComputedStyle(node.parentElement).position, rect: round(node.parentElement.getBoundingClientRect()) } : null,
    pad: getComputedStyle(node).padding,
  } : null;
  return {
    body: body ? { rect: round(body.getBoundingClientRect()), columns: getComputedStyle(body).gridTemplateColumns } : null,
    columns: cols,
    monitor: monitor ? { rect: round(monitor.getBoundingClientRect()), scrollH: monitor.scrollHeight, clientH: monitor.clientHeight,
      children: [...monitor.children].map(node => ({ cls: (node.className || '').toString().slice(0, 30), rect: round(node.getBoundingClientRect()), hidden: node.hidden })) } : null,
    photo: describe(photo), negative: describe(negative), videoPrompt: describe(videoPrompt),
    shotPrompts: shotPrompts.map(describe),
    mics: [...document.querySelectorAll('.qv-mic')].map(node => ({ cls: node.className, rect: round(node.getBoundingClientRect()) })),
  };
})()`;

(async () => {
  if (!EDGE) { console.log('SKIP: Edge not found'); return; }
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'qv-panels-'));
  const child = spawn(EDGE, ['--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
    '--no-first-run', '--window-size=1600,1000', 'about:blank'], { stdio: 'ignore' });
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

    for (const mode of ['generate', 'video']) {
      await evaluate(ws, `(document.querySelector('.local-mode-button[data-mode="${mode}"]') || {click(){}}).click(), true`);
      await sleep(4000);
      console.log(`===== ${mode} =====`);
      console.log(JSON.stringify(await evaluate(ws, SNAP), null, 1));
    }
  } finally {
    try { child.kill(); } catch { /* ignore */ }
  }
})();
