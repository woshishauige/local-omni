/* Shared measurement plumbing for the injected-UI performance work.
 *
 * Three things about this page make naive instrumentation lie, and all three are handled
 * here rather than in each script:
 *
 *  1. The page reloads itself (a mode switch reloads it, and the app normalises its URL).
 *     A recorder installed with Page.addScriptToEvaluateOnNewDocument belongs to the
 *     document that was current when it ran, so after a reload nothing is measured and a
 *     run reports a serene zero. The recorder is therefore installed into the *live* main
 *     world after every navigation and re-armed only when it is genuinely gone.
 *  2. Re-arming resets the counters, which silently truncates a measurement window
 *     (observed: a 60 s window reporting 7.6 s). Re-arming is keyed on a label that only
 *     changes when the caller asks for a fresh window, never on a read.
 *  3. The app reassigns window.fetch while it boots. A plain wrapper is simply replaced.
 *     The wrapper here is an accessor property, so an assignment is absorbed instead of
 *     discarding the instrumentation.
 *
 * Requests are counted from performance.getEntriesByType('resource'), which is ground
 * truth for anything whether or not it went through window.fetch.
 */
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

const ROOT = 'D:\\AI\\Qwen-Local';
const BASE = 'http://127.0.0.1:8081';
const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(candidate => fs.existsSync(candidate));

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const step = message => console.log(`[${new Date().toISOString().slice(11, 19)}] ${message}`);

/* The ?v= each edited injected file carries in mode_server.py's INJECTION. Kept here so a
 * probe can deliver the edited layer to its own browser (see rewriteInjection) while the
 * running service still hands out the version it was imported with. */
const INJECTION_VERSIONS = {
  'mode-control.js': 17, 'image-panel.js': 16, 'video-panel.js': 20, 'voice-input.js': 7,
};
/* QWEN_PERF_LAYER=edited makes every probe measure the files on disk; anything else
 * measures what the running service serves. Both arms of a before/after comparison then
 * use identical instrumentation. */
const injectionGate = () => String(process.env.QWEN_PERF_LAYER || '').toLowerCase() === 'edited';

/* ---------------------------------------------------------------- CDP transport */
function attach(ws) {
  let serial = 0;
  return function send(method, params = {}, timeout = 120000) {
    const id = ++serial;
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
  };
}

function makeEval(send, iframeContextId) {
  return async function evaluate(expression, timeout = 120000) {
    const params = { expression, awaitPromise: true, returnByValue: true };
    if (iframeContextId) params.contextId = iframeContextId;
    const result = await send('Runtime.evaluate', params, timeout);
    if (result.exceptionDetails) {
      throw new Error('page threw: ' + result.exceptionDetails.text + ' :: ' +
        JSON.stringify((result.exceptionDetails.exception || {}).description || ''));
    }
    if (result.result.value === undefined && result.result.type !== 'undefined') {
      throw new Error('non-serialisable result: ' + JSON.stringify(result.result));
    }
    return result.result.value;
  };
}

/* ------------------------------------------------------- in-page recorder (once) */
/* The app reloads its own document shortly after boot (a mode/model change
 * re-navigates the SPA). A recorder lives in one document, so it is gone after that,
 * and every counter read would then throw or — worse, if swallowed — report zero.
 * The recorder therefore carries the navigation time it belongs to, and a read that
 * notices a different document re-arms instead of lying. */
const RECORDER_BODY = `(label => {
  const existing = window.__perf;
  if (existing && existing.label === label && existing.navTime === performance.timeOrigin) {
    existing.reused = (existing.reused || 0) + 1;
    // The app can replace its fetch implementation after we wrapped it; re-wrap only if
    // the accessor is gone, so a read never resets a window.
    try {
      const descriptor = Object.getOwnPropertyDescriptor(window, 'fetch');
      if (!descriptor || !descriptor.get || !String(descriptor.get).includes('__qvPerf')) {
        existing.rewrap = (existing.rewrap || 0) + 1;
        window.__qvPerfWrap(existing);
      }
    } catch (e) { existing.errors.push('rewrap: ' + e); }
    return { installed: false, reused: true, label, rearmed: false };
  }
  const PERF = {
    label, installedAt: Date.now(), t0: performance.now(), navTime: performance.timeOrigin,
    nav: location.href, fetches: [], tasks: [], longAnims: [], errors: [],
    totalTasks: 0, taskMs: 0, frames: 0,
    installCount: (existing ? (existing.installCount || 1) : 0) + 1,
  };
  window.__perf = PERF;
  window.__perfDocumentId = (window.__perfDocumentId || 0) + 1;
  try { performance.setResourceTimingBufferSize(8000); } catch (e) {}

  try {
    PERF.taskObserver = new PerformanceObserver(list => {
      for (const entry of list.getEntries()) {
        PERF.totalTasks += 1;
        PERF.taskMs += entry.duration;
        if (PERF.tasks.length < 400) {
          PERF.tasks.push({ start: +entry.startTime.toFixed(1), duration: +entry.duration.toFixed(1),
                            container: entry.name || 'self' });
        }
      }
    });
    PERF.taskObserver.observe({ type: 'longtask', buffered: true });
  } catch (e) { PERF.errors.push('taskObserver: ' + e); }

  try {
    let last = performance.now();
    const tick = t => {
      const delta = t - last; last = t;
      PERF.frames += 1;
      if (delta > 50 && PERF.longAnims.length < 300) PERF.longAnims.push(+delta.toFixed(1));
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  } catch (e) { PERF.errors.push('raf: ' + e); }

  window.addEventListener('error', event => {
    if (PERF.errors.length < 100) PERF.errors.push('error: ' + (event.message || ''));
  });

  window.__qvPerfWrap = target => {
    let current = (target.fetch || window.fetch).bind(window);
    const call = async function (input, init) {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      const start = performance.now();
      const method = (init && init.method) || (input && input.method) || 'GET';
      try {
        const response = await current(input, init);
        if (PERF.fetches.length < 8000) {
          const at = performance.now();
          PERF.fetches.push({ name: String(url).slice(0, 200), method,
            start: +start.toFixed(1), end: +at.toFixed(1), status: response.status });
        }
        return response;
      } catch (error) {
        if (PERF.fetches.length < 8000) {
          PERF.fetches.push({ name: String(url).slice(0, 200), method,
            start: +start.toFixed(1), end: +performance.now().toFixed(1), status: 'ERR' });
        }
        throw error;
      }
    };
    try {
      Object.defineProperty(target, 'fetch', {
        configurable: true, enumerable: true,
        get: () => call,
        set: value => { current = typeof value === 'function' ? value.bind(window) : value; },
      });
    } catch (e) { PERF.errors.push('fetchwrap: ' + e); }
  };
  window.__qvPerfWrap(window);
  return { installed: true, reused: false, label };
})`;

/* Reads the resource buffer, returning only entries not read before. Only fetch/xhr
 * initiators are reported: boot <script>/<link> loads are one-offs and would swamp the
 * idle figures they exist to describe. */
const DRAIN_JS = `JSON.stringify((() => {
  const seen = (window.__qvPerfSeen = window.__qvPerfSeen || new Set());
  const out = [];
  for (const entry of performance.getEntriesByType('resource')) {
    const key = entry.name + '|' + entry.startTime + '|' + entry.duration;
    if (seen.has(key)) continue;
    seen.add(key);
    const type = entry.initiatorType || 'other';
    if (type !== 'fetch' && type !== 'xmlhttprequest') continue;
    out.push({ name: entry.name, type, start: +entry.startTime.toFixed(1),
               duration: +entry.duration.toFixed(1), size: entry.transferSize || 0,
               encoded: entry.encodedBodySize || 0, decoded: entry.decodedBodySize || 0 });
  }
  return out;
})())`;

/* Snapshot of counters, read without touching the recorder's state. `docId` and
 * `navTime` are what make a reload visible instead of silent: a read whose values moved
 * belongs to a different document than the previous one. */
const COUNTERS_JS = `JSON.stringify({ tasks: __perf.totalTasks, taskMs: __perf.taskMs,
  frames: __perf.frames, t: performance.now(), calls: __perf.fetches.length,
  installCount: __perf.installCount, label: __perf.label, errors: __perf.errors,
  navTime: __perf.navTime, docId: window.__perfDocumentId || 1, href: location.href,
  reloads: window.__qvReloads || 0 })`;

/* Creates a reader bound to one websocket.
 *
 * `arm(label)` starts (or adopts) a named measurement window. Every read is prefixed
 * with the same arm call, so a document that reloaded underneath the window is
 * re-instrumented rather than reported as an empty window — the difference between
 * "the page went quiet" and "we stopped listening". The `rearmed` flag and `docId` in
 * the counters are how a caller tells those apart. */
function makeReader(evaluate) {
  let label = null;
  const installWatch = async () => {
    if (await evaluate('!!window.__qvReloadWatch').catch(() => false)) return;
    await evaluate(`(() => {
      if (window.__qvReloadWatch) return;
      window.__qvReloadWatch = true;
      window.addEventListener('pageshow', () => { window.__qvReloads = (window.__qvReloads || 0) + 1; });
    })()`);
  };
  const arm = async next => {
    if (next !== undefined) label = next;
    if (!label) throw new Error('arm() needs a label');
    const state = await evaluate(`(${RECORDER_BODY})(${JSON.stringify(label)})`);
    if (!state || !state.label) throw new Error('recorder did not answer');
    await installWatch();
    return state;
  };
  const ensure = async () => { if (!label) throw new Error('reader used before arm()'); return arm(); };
  /* The app navigates itself while a window is open. A read that arrives in the new
   * document must not be lost, so it re-arms once and records that it did. */
  const guarded = async run => {
    try { return await run(); }
    catch (error) {
      if (!/not defined|Execution context|Cannot find context|Inspected target navigated/i.test(String(error.message))) throw error;
      const state = await arm();
      const value = await run();
      if (value && typeof value === 'object') value.rearmedAfterNavigation = true;
      return { ...(typeof value === 'object' && value ? value : { value }),
               rearmed: true, rearmState: state };
    }
  };
  const counters = async () => guarded(async () => { await ensure(); return JSON.parse(await evaluate(COUNTERS_JS)); });
  const drain = async () => guarded(async () => { await ensure(); return JSON.parse(await evaluate(DRAIN_JS)); });
  const js = async expression => guarded(async () => { await ensure(); return JSON.parse(await evaluate(expression)); });
  const raw = async expression => guarded(async () => { await ensure(); return evaluate(expression); });
  return { arm, counters, drain, js, raw, evaluate, get label() { return label; } };
}

/* --------------------------------------------------------------- reporting */
function summarise(entries) {
  const byPath = new Map();
  let bytes = 0;
  for (const entry of entries) {
    let key = entry.name;
    try {
      const parsed = new URL(entry.name);
      key = parsed.pathname + (parsed.search ? parsed.search.replace(/=[^&]{16,}/g, '=<long>') : '');
    } catch (failure) { /* keep the raw name */ }
    const row = byPath.get(key) || { path: key, count: 0, bytes: 0, ms: 0, max: 0 };
    row.count += 1;
    row.bytes += entry.size;
    row.ms += entry.duration;
    row.max = Math.max(row.max, entry.duration);
    byPath.set(key, row);
    bytes += entry.size;
  }
  return { count: entries.length, bytes, rows: [...byPath.values()].sort((a, b) => b.count - a.count) };
}

function report(title, entries, seconds) {
  const summary = summarise(entries);
  console.log(`\n--- ${title} (${seconds}s) ---`);
  console.log(`  requests: ${summary.count}  (${(summary.count / seconds * 60).toFixed(1)}/min)`);
  console.log(`  transfer bytes: ${summary.bytes}  (${(summary.bytes / seconds / 1024 * 60).toFixed(1)} KiB/min)`);
  console.log('  by path:');
  for (const row of summary.rows.slice(0, 24)) {
    console.log(`    ${String(row.count).padStart(4)}x  ${String(Math.round(row.bytes / 1024)).padStart(5)} KiB  ` +
      `avg ${(row.ms / row.count).toFixed(0).padStart(4)}ms  max ${row.max.toFixed(0).padStart(5)}ms  ${row.path}`);
  }
  return summary;
}

/* --------------------------------------------------------------- browser control */

/* Serve the *edited* injected layer to a measuring browser without touching the running
 * service.
 *
 * INJECTION is a module-level Python constant, so a running gateway keeps handing out the
 * script URLs it was imported with: editing web/*.js and bumping ?v= changes nothing until
 * the service is restarted. That makes "measure the fixed layer" impossible on a busy
 * service unless the browser can be told to ask for the new URLs anyway — which is what
 * this does, by rewriting the ?v= of the injected script tags in the HTML response on its
 * way to the page. Everything else (the API, the app bundle, the gateway itself) is
 * untouched, and no second server is started.
 *
 * Usage: await lib.rewriteInjection(browser, { 'mode-control.js': 17, 'video-panel.js': 20 })
 */
async function rewriteInjection(browser, versions) {
  const patterns = Object.entries(versions).map(([file, version]) => ({
    file, version, needle: new RegExp(`(/local/${file.replace('.', '\\.')})\\?v=\\d+`, 'g'),
  }));
  const rules = patterns.map(({ file }) => ({ urlPattern: `*/local/${file}?*`, requestStage: 'Request' }));
  rules.push({ urlPattern: '*', requestStage: 'Response' });
  let rewrites = 0;

  browser.ws.addEventListener('message', async event => {
    let message;
    try { message = JSON.parse(event.data); } catch (failure) { return; }
    if (message.method !== 'Fetch.requestPaused') return;
    const { requestId, request, responseStatusCode, responseHeaders } = message.params;
    try {
      const wantsScript = patterns.some(rule => request.url.includes(`/local/${rule.file}`));
      if (wantsScript) {
        // Let the gateway answer; the body is rewritten below, on the way back.
        await browser.send('Fetch.continueRequest', { requestId });
        return;
      }
      if (!responseStatusCode) {
        // A document request paused at the response stage: the body arrives further on.
        await browser.send('Fetch.continueResponse', { requestId });
        return;
      }
      const type = String((responseHeaders || []).find(h => h.name.toLowerCase() === 'content-type')?.value || '');
      if (!type.includes('text/html')) { await browser.send('Fetch.continueRequest', { requestId }); return; }
      let body = (await browser.send('Fetch.getResponseBody', { requestId })).body || '';
      if (body && !/^[A-Za-z0-9+/=\s]+$/.test(body.slice(0, 200))) {
        // Already plain text rather than base64.
      } else if (body) {
        body = Buffer.from(body, 'base64').toString('utf8');
      }
      let changed = body;
      for (const rule of patterns) {
        changed = changed.replace(rule.needle, `$1?v=${rule.version}`);
      }
      if (changed !== body) {
        rewrites += 1;
        await browser.send('Fetch.fulfillRequest', {
          requestId, responseCode: responseStatusCode,
          responseHeaders: (responseHeaders || []).filter(h => !/^(content-length|content-encoding)$/i.test(h.name)),
          body: Buffer.from(changed, 'utf8').toString('base64'),
        });
      } else {
        await browser.send('Fetch.fulfillRequest', {
          requestId, responseCode: responseStatusCode, responseHeaders: responseHeaders || [],
          body: Buffer.from(body, 'utf8').toString('base64'),
        });
      }
    } catch (failure) {
      try { await browser.send('Fetch.continueRequest', { requestId }); } catch (ignored) { /* paused target gone */ }
    }
  });
  await browser.send('Fetch.enable', { patterns: rules });
  return { rules: patterns.length, get rewrites() { return rewrites; } };
}

async function launch({ port, width = 1600, height = 1000, extraArgs = [] }) {
  if (!EDGE) throw new Error('Edge not found');
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'qv-perf-'));
  const child = spawn(EDGE, [
    '--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check',
    '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream',
    '--autoplay-policy=no-user-gesture-required', `--window-size=${width},${height}`,
    ...extraArgs, 'about:blank',
  ], { stdio: 'ignore' });
  for (let i = 0; i < 80; i++) {
    try { await (await fetch(`http://127.0.0.1:${port}/json/version`)).json(); break; } catch { await sleep(300); }
  }
  const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  // Extensions register background_page targets in this profile; attaching to one of
  // those measures nothing while looking perfectly healthy.
  const pages = list.filter(entry => entry.type === 'page' && !entry.url.startsWith('devtools://'));
  if (!pages.length) { child.kill(); throw new Error('no page target'); }
  const page = pages[0];
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true });
    ws.addEventListener('error', () => reject(new Error('no CDP socket')), { once: true });
  });
  const send = attach(ws);
  await send('Runtime.enable');
  await send('Page.enable');
  await send('Page.addScriptToEvaluateOnNewDocument', {
    source: 'try { performance.setResourceTimingBufferSize(8000); } catch (e) {}',
  });
  const evaluate = makeEval(send);
  return {
    ws, send, evaluate, reader: makeReader(evaluate), page,
    async close() {
      try { ws.close(); } catch (failure) { /* already gone */ }
      child.kill();
      await sleep(400);
      try { fs.rmSync(profile, { recursive: true, force: true }); } catch (failure) { /* best effort */ }
    },
  };
}

async function login(browser, route = '/') {
  const token = fs.readFileSync(path.join(ROOT, 'logs', 'admin.token'), 'ascii').trim();
  const url = `${BASE}${route}${route.includes('?') ? '&' : '?'}admin=${token}`;
  await browser.send('Page.navigate', { url });
  const deadline = Date.now() + 45000;
  while (Date.now() < deadline) {
    const ready = await browser.evaluate('document.readyState === "complete"').catch(() => false);
    if (ready) return url;
    await sleep(250);
  }
  throw new Error('page never reached complete');
}

async function waitFor(evaluate, expression, timeout, label) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await evaluate(expression).catch(() => false)) return true;
    await sleep(250);
  }
  throw new Error('timed out waiting for ' + label);
}

module.exports = {
  ROOT, BASE, EDGE, sleep, step, launch, login, waitFor, rewriteInjection,
  INJECTION_VERSIONS, injectionGate,
  summarise, report, makeReader, makeEval,
};
