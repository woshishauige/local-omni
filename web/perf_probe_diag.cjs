/* Where does a call's time actually go? Boots one page and reports what the injected
 * layer costs before any user gesture: DOM size, how long the toolbar takes to appear,
 * and which of our modules made a request. Also reports the page/target list so a
 * measurement run can prove it is attached to the real page and not a stray target.
 *
 * Usage: node perf_probe_diag.cjs [--port 9413]
 */
const { spawn } = require('node:child_process');
const { results, check, cdp, sleep } = require('./lib/cdp.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const argv = process.argv.slice(2);
const arg = (name, fallback) => {
  const at = argv.indexOf('--' + name);
  return at >= 0 && argv[at + 1] ? argv[at + 1] : fallback;
};
const PORT = Number(arg('port', 9413));
const BASE = 'http://127.0.0.1:8081';
const ROOT = 'D:\\AI\\Qwen-Local';
const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(candidate => fs.existsSync(candidate));

async function evaluate(ws, expression, timeout = 120000) {
  const result = await cdp(ws, 'Runtime.evaluate',
    { expression, awaitPromise: true, returnByValue: true }, timeout);
  if (result.exceptionDetails) {
    throw new Error('page threw: ' + result.exceptionDetails.text + ' :: ' +
      JSON.stringify((result.exceptionDetails.exception || {}).description || ''));
  }
  return result.result.value;
}

async function main() {
  if (!EDGE) { console.log('SKIP: Edge not found'); return 0; }
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'qv-diag-'));
  const child = spawn(EDGE, [
    '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check',
    '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream',
    '--window-size=1600,1000', 'about:blank',
  ], { stdio: 'ignore' });
  try {
    for (let i = 0; i < 60; i++) {
      try { await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json(); break; } catch { await sleep(400); }
    }
    const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
    console.log('--- TARGETS ---');
    for (const entry of list) console.log(`  ${entry.type}  ${entry.url}`);
    const page = list.find(entry => entry.type === 'page' && !entry.url.startsWith('devtools://')) || list[0];
    console.log('  attached to:', page.type, page.url);
    const ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener('open', resolve, { once: true });
      ws.addEventListener('error', () => reject(new Error('no CDP socket')), { once: true });
    });
    await cdp(ws, 'Runtime.enable');
    await cdp(ws, 'Page.enable');
    await cdp(ws, 'Page.addScriptToEvaluateOnNewDocument', { source: `
      window.__navStart = performance.now();
      window.__marks = [];
      try {
        new PerformanceObserver(list => {
          for (const entry of list.getEntries()) {
            if (entry.name && entry.name.includes('local-')) window.__marks.push([entry.name, entry.startTime + entry.duration]);
          }
        }).observe({ type: 'resource', buffered: true });
      } catch (e) {}
    ` });
    const token = fs.readFileSync(path.join(ROOT, 'logs', 'admin.token'), 'ascii').trim();
    await cdp(ws, 'Page.navigate', { url: `${BASE}/?admin=${token}` });
    for (let i = 0; i < 120; i++) {
      if (await evaluate(ws, 'document.readyState === "complete"', 15000).catch(() => false)) break;
      await sleep(250);
    }
    console.log('\n--- PROBE SANITY ---');
    console.log('  basic evaluate   :', await evaluate(ws, '1 + 1'));
    console.log('  install result   :', await evaluate(ws,
      `(() => { const __install = () => { window.__x = 7; return 'installed'; }; __install(); return 'ok'; })()`));
    console.log('  did it stick     :', await evaluate(ws, 'window.__x'));
    console.log('  new Function ok  :', await evaluate(ws, `(() => { try { return String(new Function('return 3')()); } catch (e) { return 'ERR ' + e; } })()`));
    console.log('  PerformanceObserver longtask:', await evaluate(ws,
      `(() => { try { new PerformanceObserver(() => {}).observe({ type: 'longtask', buffered: true }); return 'ok'; } catch (e) { return 'ERR ' + e; } })()`));
    console.log('  service worker   :', await evaluate(ws,
      `(async () => { try { const r = await navigator.serviceWorker.getRegistrations(); return r.length + ' registration(s)'; } catch (e) { return 'ERR ' + e; } })()`));

    await sleep(4000);
    const facts = await evaluate(ws, `JSON.stringify({
      domNodes: document.getElementsByTagName('*').length,
      toolbarAt: (document.querySelector('.local-mode-toolbar')||{}).offsetTop,
      micDocked: !!document.querySelector('#local-composer-tools .qv-mic'),
      chipDocked: !!document.querySelector('#local-composer-tools .local-tool-permission'),
      panels: { image: !!window.ImagePanel, video: !!window.VideoPanel, voice: !!window.VoicePanel },
      panelHidden: { image: (window.ImagePanel && window.ImagePanel.root.hidden),
                     video: (document.querySelector('#local-video-panel')||{}).hidden },
      marks: (window.__marks||[]).slice(-14),
      navStart: window.__navStart,
      injected: performance.getEntriesByType('resource').filter(e => e.name.includes('/local/'))
        .map(e => ({ name: e.name.replace('http://127.0.0.1:8081',''), start: Math.round(e.startTime), end: Math.round(e.startTime + e.duration), size: e.transferSize })),
      appScripts: performance.getEntriesByType('resource').filter(e => e.initiatorType === 'script' || e.name.endsWith('.js')).length,
      allResources: performance.getEntriesByType('resource').length,
    })`);
    console.log('\n--- PAGE FACTS ---');
    console.log(JSON.stringify(JSON.parse(facts), null, 1));
    ws.close();
  } finally {
    child.kill();
    await sleep(400);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (failure) { /* best effort */ }
  }
  return 0;
}
main().then(code => process.exit(code)).catch(error => { console.error('ERROR', error.stack || error.message); process.exit(1); });
