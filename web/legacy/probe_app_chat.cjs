/* What the packaged app itself offers: its conversation controls and where it keeps a thread.
 *
 * The owner's rule is "switching 纯文本 ↔ 识图 starts a new conversation immediately", and the chat
 * window belongs to the packaged SPA — so before anything can be wired up, the app's own controls
 * and storage have to be found. This prints them.
 *
 * Usage: node probe_app_chat.cjs
 */
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = Number(process.env.PROBE_PORT || 9414);
const ROOT = 'D:\\AI\\Qwen-Local';
const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(candidate => fs.existsSync(candidate));
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
  const link = fs.readFileSync(path.join(ROOT, 'logs', 'admin-link.txt'), 'ascii').trim();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'qv-appch-'));
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
    await cdp(ws, 'Page.navigate', { url: link });
    await sleep(9000);

    const inventory = await evaluate(ws, `(async () => {
      const label = node => (node.getAttribute('aria-label') || node.title
        || (node.textContent || '').replace(/\\s+/g, ' ').trim()).slice(0, 40);
      const clickable = [...document.querySelectorAll('button, [role="button"], a')]
        .filter(node => node.offsetParent !== null)
        .map(node => ({
          tag: node.tagName.toLowerCase(),
          label: label(node),
          cls: String(node.className || '').slice(0, 60),
          testid: node.getAttribute('data-testid') || '',
        }));
      const storage = {};
      for (let i = 0; i < localStorage.length; i += 1) {
        const key = localStorage.key(i);
        storage[key] = String(localStorage.getItem(key) || '').slice(0, 90);
      }
      let databases = [];
      try { databases = (await indexedDB.databases()).map(entry => entry.name); } catch (error) { databases = ['(unavailable)']; }
      return JSON.stringify({ clickable: clickable.slice(0, 70), storage, databases });
    })()`);
    const data = JSON.parse(inventory);
    console.log('=== 可点控件（前 70）===');
    for (const entry of data.clickable) {
      console.log(`  ${entry.tag.padEnd(6)} ${entry.label.padEnd(42)} ${entry.testid} ${entry.cls}`);
    }
    console.log('=== localStorage ===');
    for (const [key, value] of Object.entries(data.storage)) console.log(`  ${key} = ${value}`);
    console.log('=== IndexedDB ===');
    console.log('  ' + data.databases.join(', '));

    // One real turn, so the app's message DOM and its tab bookkeeping can be looked at.
    await evaluate(ws, `(() => {
      const box = document.querySelector('textarea, [contenteditable="true"]');
      if (!box) return false;
      if (box.tagName === 'TEXTAREA') {
        const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
        setter.call(box, '只回答两个字：收到');
        box.dispatchEvent(new Event('input', { bubbles: true }));
      } else {
        box.textContent = '只回答两个字：收到';
        box.dispatchEvent(new Event('input', { bubbles: true }));
      }
      return true;
    })()`);
    await sleep(600);
    await evaluate(ws, `(() => {
      const send = [...document.querySelectorAll('button')]
        .find(node => (node.textContent || '').trim() === '发送' && node.offsetParent !== null);
      if (send && !send.disabled) { send.click(); return true; }
      const box = document.querySelector('textarea, [contenteditable="true"]');
      if (box) box.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
      return false;
    })()`);
    await sleep(9000);
    console.log('=== 一轮之后的页面结构 ===');
    console.log(await evaluate(ws, `(() => {
      const main = document.querySelector('main') || document.body;
      const counts = {};
      for (const node of main.querySelectorAll('*')) {
        for (const name of node.classList) {
          if (!/^(qc|qu|qs|qv|local)/.test(name)) counts[name] = (counts[name] || 0) + 1;
        }
      }
      const top = Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 14);
      return JSON.stringify({
        bubbles: top,
        messageNodes: document.querySelectorAll('[data-message-id], [data-testid*="message"]').length,
        text: (main.innerText || '').replace(/\\s+/g, ' ').slice(-160),
        tabs: localStorage.getItem('LlamaUi.conversationTabs'),
      }, null, 1);
    })()`));
    ws.close();
  } finally {
    child.kill();
    await sleep(700);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (failure) { /* ignore */ }
  }
})().catch(error => { console.error('ERROR', error.message); process.exit(1); });
