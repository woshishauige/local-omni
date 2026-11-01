/* 顶部导航在「纯文本」和「识图」两页之间到底哪里变了？
 *
 * 所有者反馈：「点击识图模式时顶端导航出现一点显示问题」。这个探针不做判断，只把两页的
 * 工具栏几何量记下来：整行高度（有没有换行）、每个子元素的 x/宽、状态行有没有被省略号截断、
 * 模式按钮的位置有没有因为状态文字变长而左右移动。同时各存一张截图。
 */
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = 9429;
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

const MEASURE = `(() => {
  const row = document.querySelector('.local-mode-row');
  if (!row) return JSON.stringify({ error: 'no .local-mode-row' });
  const box = node => { const r = node.getBoundingClientRect(); return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) }; };
  const kids = [...row.children].map(node => ({
    cls: (node.className || '').toString().slice(0, 24),
    text: (node.textContent || '').replace(/\\s+/g, ' ').slice(0, 26),
    ...box(node),
  }));
  const now = document.querySelector('.local-model-now');
  const apply = document.querySelector('.local-model-apply');
  const tabs = [...document.querySelectorAll('.local-mode-button')].map(node => ({
    label: (node.textContent || '').trim().slice(0, 12), ...box(node),
  }));
  return JSON.stringify({
    row: box(row),
    rowStyle: { display: getComputedStyle(row).display, wrap: getComputedStyle(row).flexWrap,
                gap: getComputedStyle(row).gap, height: getComputedStyle(row).height },
    kids, tabs,
    now: now ? { text: now.textContent, ...box(now), scroll: now.scrollWidth, client: now.clientWidth,
                 title: now.title, ellipsis: now.scrollWidth > now.clientWidth + 1 } : null,
    apply: apply ? { text: apply.textContent, ...box(apply), disabled: apply.disabled } : null,
    active: document.querySelector('.local-mode-button[aria-pressed="true"]')?.textContent.trim() || '',
    pageWidth: window.innerWidth,
  });
})()`;

(async () => {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'qv-navshot-'));
  const child = spawn(EDGE, ['--headless=new', `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profile}`, '--no-first-run', '--window-size=1440,900', 'about:blank'],
    { stdio: 'ignore' });
  const shots = path.join(__dirname, 'shots');
  fs.mkdirSync(shots, { recursive: true });
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

    const capture = async name => {
      const shot = await cdp(ws, 'Page.captureScreenshot',
        { format: 'png', clip: { x: 0, y: 0, width: 1440, height: 130, scale: 1 } });
      fs.writeFileSync(path.join(shots, name), Buffer.from(shot.data, 'base64'));
    };

    const click = tab => evaluate(ws, `(() => {
      const node = [...document.querySelectorAll('.local-mode-button')]
        .find(n => (n.textContent || '').includes(${JSON.stringify(tab)}));
      if (!node) return 'no tab';
      node.click();
      return 'clicked';
    })()`);

    // 纯文本
    await click('纯文本');
    await sleep(2000);
    const textPage = JSON.parse(await evaluate(ws, MEASURE));
    await capture('nav-text.png');

    // 识图
    await click('识图');
    await sleep(2500);
    const visionPage = JSON.parse(await evaluate(ws, MEASURE));
    await capture('nav-vision.png');

    const brief = page => ({
      row: `${page.row.w}×${page.row.h}`, display: page.rowStyle.display, wrap: page.rowStyle.wrap,
      active: page.active,
      now: page.now && `${page.now.text}  [${page.now.w}px${page.now.ellipsis ? ' ELLIPSIS' : ''}]`,
      apply: page.apply && `${page.apply.text}${page.apply.disabled ? ' (disabled)' : ''}`,
      tabs: page.tabs.map(t => `${t.label}@${t.x}`).join(' '),
    });
    console.log('文本页:', JSON.stringify(brief(textPage), null, 0));
    console.log('识图页:', JSON.stringify(brief(visionPage), null, 0));

    // 差异：模式按钮位置动了多少，行高变没变
    const moved = textPage.tabs.map((tab, i) => ({ label: tab.label, dx: visionPage.tabs[i] ? visionPage.tabs[i].x - tab.x : null }))
      .filter(item => item.dx !== 0);
    console.log('按钮位移:', JSON.stringify(moved));
    console.log('行高变化:', textPage.row.h, '->', visionPage.row.h);
    console.log('状态行:', JSON.stringify({ text: visionPage.now, apply: visionPage.apply }));
    console.log('截图:', path.join(shots, 'nav-text.png'), path.join(shots, 'nav-vision.png'));

    ws.close();
  } finally {
    child.kill();
    await sleep(600);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch { /* Edge may hold a handle */ }
  }
})().catch(error => { console.error('ERROR', error.message); process.exit(1); });
