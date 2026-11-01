/* 浏览器检查脚本的公共脚手架。
 *
 * 以前每个 check_*.cjs 都各自复制一遍：启动 Edge、等调试端口、连 WebSocket、cdp()、
 * evaluate()、check()/results、读管理令牌、打开面板、finally 清理。同一段代码在 24 个文件里
 * 各有一份，改一处要改 24 处。现在只留这一份。
 *
 * 用法（最小）：
 *
 *     const { results, check, cdp, evaluate, sleep, openPanel } = require('./lib/cdp.cjs');
 *     const { ws, close } = await openPanel();          // 默认端口、默认 1440×900、自动登录
 *     check('something', await evaluate(ws, '1 + 1') === 2);
 *     await close();                                    // 关浏览器、删临时 profile
 *     finish();                                         // 打印 n/m passed 并以退出码收尾
 *
 * 需要特殊行为时：
 *   openPanel({ port, width, height, url, waitMs })   —— 自定义端口/窗口/地址/等页面就绪的时长
 *   evaluate(ws, expression, timeout)                 —— 自定义超时
 *   shot(ws, { path, width, height })                 —— 截一张图存盘
 */
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const EDGE_CANDIDATES = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
];
const GATEWAY = 'http://127.0.0.1:8081';

const results = [];
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

function check(label, ok, detail = '') {
  results.push(Boolean(ok));
  console.log(`${ok ? 'PASS' : 'FAIL'} ${String(label).padEnd(52)} ${detail}`);
  return Boolean(ok);
}

/** 打印汇总并以退出码收尾：全部通过 0，否则 1。 */
function finish({ label = '' } = {}) {
  const passed = results.filter(Boolean).length;
  console.log(`\n${passed}/${results.length} passed${label ? ' ' + label : ''}`);
  process.exit(passed === results.length ? 0 : 1);
}

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

async function evaluate(ws, expression, timeout = 30000) {
  const result = await cdp(ws, 'Runtime.evaluate',
    { expression, awaitPromise: true, returnByValue: true }, timeout);
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
  return result.result.value;
}

function edgePath() {
  for (const candidate of EDGE_CANDIDATES) {
    if (fs.existsSync(candidate)) return candidate;
  }
  throw new Error('找不到 Edge/Chrome 可执行文件（试过：' + EDGE_CANDIDATES.join(' / ') + '）');
}

function adminToken() {
  const file = path.join(ROOT, 'logs', 'admin.token');
  return fs.existsSync(file) ? fs.readFileSync(file, 'ascii').trim() : '';
}

/** 打开一个无头浏览器并登录面板，返回 { ws, close }。 */
async function openPanel({ port = 9499, width = 1440, height = 900, url = '', waitMs = 9000 } = {}) {
  const opened = await launchAndOpen({ port, width, height, url, waitMs });
  return { ws: opened.ws, close: opened.close, port, profile: opened.profile };
}

/**
 * 与 openPanel 相同，但返回**老脚本里用惯的那几个名字**（`child` / `profile` / `ws`），
 * 这样把"自己 spawn Edge"的那段代码换掉时，各文件末尾的 `finally { child.kill(); fs.rmSync(profile) }`
 * 可以原样保留，不必跟着改。
 */
async function launchAndOpen({ port = 9499, width = 1440, height = 900, url = '', waitMs = 9000 } = {}) {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'qv-check-'));
  const child = spawn(edgePath(), ['--headless=new', `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`, '--no-first-run', `--window-size=${width},${height}`, 'about:blank'],
    { stdio: 'ignore' });
  const close = async () => {
    try { child.kill(); } catch { /* 已经退了 */ }
    await sleep(600);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch { /* Edge 可能还占着句柄 */ }
  };
  for (let attempt = 0; attempt < 60; attempt++) {
    try {
      await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
      break;
    } catch {
      await sleep(400);
    }
  }
  const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const page = list.find(target => target.type === 'page');
  if (!page) throw new Error('调试端口上没有 page target');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise(resolve => ws.addEventListener('open', resolve, { once: true }));
  await cdp(ws, 'Runtime.enable');
  await cdp(ws, 'Page.enable');
  const target = url || `${GATEWAY}/?admin=${adminToken()}`;
  await cdp(ws, 'Page.navigate', { url: target });
  await sleep(waitMs);
  return { ws, child, profile, close };
}

/** 截一张图（默认只截顶部 130px 的工具栏），返回落盘路径。 */
async function shot(ws, { file, width = 1440, height = 130, x = 0, y = 0 } = {}) {
  const data = await cdp(ws, 'Page.captureScreenshot',
    { format: 'png', clip: { x, y, width, height, scale: 1 } });
  const target = file || path.join(ROOT, 'web', 'shots', `shot-${Date.now()}.png`);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, Buffer.from(data.data, 'base64'));
  return target;
}

module.exports = {
  ROOT, GATEWAY, EDGE_CANDIDATES, results, sleep,
  check, finish, cdp, evaluate, edgePath, adminToken, openPanel, launchAndOpen, shot,
};
