/* The steady-state cost of a page that is open and unattended, with the tab visible.
 *
 * This is the metric that matters for a client left open next to a conversation: an idle
 * page should be nearly free. It is measured three ways at once so a zero is believable —
 * CDP request events, the resource-timing drain, and a request counter inside the page —
 * and the main thread is profiled at the same time.
 *
 * The window starts after the page has settled *and* after the app's own post-boot
 * reload, which is why the first thing reported is how many navigations happened.
 *
 * Usage: node perf_idle.cjs [--port 9450] [--seconds 60] [--tag before]
 */
const fs = require('node:fs');
const path = require('node:path');
const lib = require('./perf_lib.cjs');

const argv = process.argv.slice(2);
const arg = (name, fallback) => {
  const at = argv.indexOf('--' + name);
  return at >= 0 && argv[at + 1] ? argv[at + 1] : fallback;
};
const PORT = Number(arg('port', 9450));
const SECONDS = Number(arg('seconds', 60));
const TAG = String(arg('tag', 'run'));
const HIDDEN = argv.includes('--hidden');

/* A page-side request counter that does not care who owns window.fetch: it wraps the
 * prototype method the app itself ends up calling, and it is installed by this script
 * after the page has settled, so it counts the steady state rather than the boot. */
const COUNTER_INSTALL = `(() => {
  if (window.__qvIdleCount) return 'already';
  const state = { total: 0, byPath: {}, first: null, last: null, api: 0 };
  window.__qvIdleCount = state;
  const native = window.fetch;
  const bump = url => {
    state.total += 1;
    let key = String(url);
    try { const parsed = new URL(key, location.origin); key = parsed.pathname + parsed.search; } catch (e) {}
    state.byPath[key] = (state.byPath[key] || 0) + 1;
    const now = Date.now();
    state.first = state.first || now;
    state.last = now;
  };
  window.fetch = function (...args) {
    const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
    bump(url);
    return native.apply(this, args);
  };
  // XHR is not used by this layer, but counting it costs nothing and rules it out.
  const open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) { bump(url); return open.call(this, method, url, ...rest); };
  return 'installed';
})()`;

const READ_COUNTER = `JSON.stringify(window.__qvIdleCount || { total: -1 })`;

const LAYOUT_PATCH = `(() => {
  if (window.__qvLayoutReads) return 'already';
  window.__qvLayoutReads = 0;
  const original = Element.prototype.getBoundingClientRect;
  Element.prototype.getBoundingClientRect = function (...args) {
    window.__qvLayoutReads += 1;
    return original.apply(this, args);
  };
  return 'patched';
})()`;

const READ_LAYOUT = `JSON.stringify({ reads: window.__qvLayoutReads || 0,
  mutations: window.__qvMutations || 0, raf: window.__qvRaf || 0 })`;

/* --hidden hides the tab the way the modules see it (document.hidden) without freezing the
 * renderer, so the measurement is about the layer's own discipline rather than the
 * browser's background throttling. */
const HIDE_JS = `JSON.stringify((() => {
  window.__realHidden = Object.getOwnPropertyDescriptor(Document.prototype, 'hidden');
  window.__realVis = Object.getOwnPropertyDescriptor(Document.prototype, 'visibilityState');
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
  document.dispatchEvent(new Event('visibilitychange'));
  return { hidden: document.hidden };
})())`;

async function main() {
  const browser = await lib.launch({ port: PORT });
  if (lib.injectionGate()) lib.step('injection rewrite: ' + JSON.stringify(await lib.rewriteInjection(browser, lib.INJECTION_VERSIONS)));
  const out = { tag: TAG, seconds: SECONDS };
  const cdp = [];
  let capturing = false;
  browser.ws.addEventListener('message', event => {
    let message;
    try { message = JSON.parse(event.data); } catch (failure) { return; }
    if (message.method !== 'Network.requestWillBeSent' || !capturing) return;
    const request = message.params.request || {};
    const url = String(request.url || '').replace(lib.BASE, '');
    if (!url.startsWith('/')) return;
    cdp.push(url);
  });
  await browser.send('Network.enable');
  try {
    const navigations = [];
    browser.ws.addEventListener('message', event => {
      let message;
      try { message = JSON.parse(event.data); } catch (failure) { return; }
      if (message.method === 'Page.frameNavigated' && !message.params.frame.parentId) {
        navigations.push({ at: Date.now(), url: message.params.frame.url });
      }
    });

    await lib.login(browser);
    await lib.waitFor(browser.evaluate, '!!document.querySelector(".local-mode-toolbar")', 40000, 'toolbar');
    // Settle, then wait past the reload the app performs after it accepts a status.
    await lib.sleep(6000);
    const settledAt = Date.now();
    lib.step(`settled; navigations so far: ${navigations.length}`);

    await browser.evaluate(LAYOUT_PATCH);
    await browser.evaluate(COUNTER_INSTALL);
    await browser.evaluate(`(() => {
      window.__qvMutations = 0;
      new MutationObserver(records => { window.__qvMutations += records.length; })
        .observe(document.body, { childList: true, subtree: true, attributes: true, characterData: true });
      window.__qvRaf = 0;
      const tick = () => { window.__qvRaf += 1; requestAnimationFrame(tick); };
      requestAnimationFrame(tick);
    })()`);
    await browser.reader.arm('idle');
    await browser.reader.drain();

    if (HIDDEN) {
      const state = JSON.parse(await browser.evaluate(HIDE_JS));
      lib.step('hidden emulation: ' + JSON.stringify(state));
      await lib.sleep(1500);
    }

    await browser.send('Profiler.enable');
    await browser.send('Profiler.setSamplingInterval', { interval: 200 });
    await browser.send('Profiler.start');
    const before = JSON.parse(await browser.evaluate(READ_COUNTER));
    const beforeLayout = JSON.parse(await browser.evaluate(READ_LAYOUT));
    capturing = true;
    lib.step(`capturing ${SECONDS}s of steady state`);
    await lib.sleep(SECONDS * 1000);
    capturing = false;
    const after = JSON.parse(await browser.evaluate(READ_COUNTER));
    const afterLayout = JSON.parse(await browser.evaluate(READ_LAYOUT));
    const { profile } = await browser.send('Profiler.stop');
    const entries = await browser.reader.drain();
    const counters = await browser.reader.counters();

    const byPath = new Map();
    for (const url of cdp) {
      let key = url;
      try { const parsed = new URL(lib.BASE + url); key = parsed.pathname + parsed.search; } catch (failure) { /* keep */ }
      byPath.set(key, (byPath.get(key) || 0) + 1);
    }
    const bytes = entries.reduce((sum, entry) => sum + entry.size, 0);

    console.log(`\n--- IDLE STEADY STATE (${SECONDS}s, tab ${HIDDEN ? 'hidden (document.hidden=true)' : 'visible'}) ---`);
    console.log(`  CDP request events      : ${cdp.length}  (${(cdp.length / SECONDS * 60).toFixed(1)}/min)`);
    console.log(`  resource-timing drain   : ${entries.length}  (${(entries.length / SECONDS * 60).toFixed(1)}/min), ${bytes} bytes transferred`);
    console.log(`  in-page fetch counter   : ${after.total - before.total}  (${(Math.max(after.total - before.total, 0) / SECONDS * 60).toFixed(1)}/min)`);
    console.log(`  page navigations        : ${navigations.filter(row => row.at > settledAt).length} during the window, ${navigations.length} since launch`);
    console.log('  by path (CDP):');
    for (const [key, count] of [...byPath.entries()].sort((a, b) => b[1] - a[1])) {
      console.log(`    ${String(count).padStart(4)}x ${(count / SECONDS * 60).toFixed(1)}/min  ${key}`);
    }
    console.log('  by path (in-page):');
    for (const [key, count] of Object.entries(after.byPath || {}).sort((a, b) => b[1] - a[1])) {
      console.log(`    ${String(count - (before.byPath[key] || 0)).padStart(4)}  ${key}`);
    }

    const nodes = profile.nodes;
    const total = nodes.reduce((sum, node) => sum + (node.hitCount || 0), 0);
    const interval = (profile.endTime - profile.startTime) / Math.max(total, 1);
    const self = new Map();
    for (const node of nodes) {
      const frame = node.callFrame || {};
      const name = `${frame.functionName || '(anonymous)'} @ ${String(frame.url || '').split('/').pop()}:${(frame.lineNumber || 0) + 1}`;
      self.set(name, (self.get(name) || 0) + (node.hitCount || 0));
    }
    const ranked = [...self.entries()].sort((a, b) => b[1] - a[1]).slice(0, 16)
      .map(([name, hits]) => ({ name, ms: Math.round(hits * interval / 1000) }));
    const ours = ranked.filter(row => /mode-control|voice-input|context-control|image-panel|video-panel|voice-panel|usage-log|share-manager/.test(row.name));
    const busyMs = ranked.filter(row => !/\(idle\)|\(program\)|\(garbage collector\)/.test(row.name))
      .reduce((sum, row) => sum + row.ms, 0);

    console.log('\n  main thread during the window:');
    console.log(`    total sampled cpu (excluding idle): ${busyMs}ms over ${SECONDS}s wall`);
    console.log(`    forced layout reads (getBoundingClientRect): ${afterLayout.reads - beforeLayout.reads} ` +
      `(${((afterLayout.reads - beforeLayout.reads) / SECONDS).toFixed(2)}/s)`);
    console.log(`    DOM mutations: ${afterLayout.mutations - beforeLayout.mutations}, animation frames: ${afterLayout.raf - beforeLayout.raf}`);
    console.log('    top self time:');
    for (const row of ranked.slice(0, 10)) console.log(`      ${String(row.ms).padStart(5)}ms  ${row.name}`);
    console.log('    of which the injected layer:');
    for (const row of ours) console.log(`      ${String(row.ms).padStart(5)}ms  ${row.name}`);

    out.cdp = { count: cdp.length, byPath: Object.fromEntries(byPath) };
    out.drain = { count: entries.length, bytes };
    out.inPage = { count: after.total - before.total, byPath: after.byPath };
    out.navigations = navigations.length;
    out.layout = { reads: afterLayout.reads - beforeLayout.reads,
                   mutations: afterLayout.mutations - beforeLayout.mutations,
                   frames: afterLayout.raf - beforeLayout.raf };
    out.cpu = { busyMs, ranked, injectedMs: ours.reduce((sum, row) => sum + row.ms, 0), injected: ours };
    out.counters = counters;

    const outPath = path.join(lib.ROOT, 'web', `perf_idle_${TAG}.json`);
    fs.writeFileSync(outPath, JSON.stringify(out, null, 2), 'utf8');
    console.log(`\nwritten ${outPath}`);
  } finally {
    await browser.close();
  }
  return 0;
}

main().then(code => process.exit(code)).catch(error => { console.error('ERROR', error.stack || error.message); process.exit(1); });
