/* What the per-second timers actually cost, and what they do while nobody is looking.
 *
 * Three things in the injected layer run continuously without any network traffic, so a
 * request count cannot see them: mode-control's 1 s followConversation+mountComposerTools,
 * voice-input's 1 s dockMic, and the MutationObserver that calls render(). This measures
 * them with a sampling profiler (CPU profile over a window, aggregated by function name)
 * plus rAF stalls, so "the tab is idle but the CPU is not" becomes a number.
 *
 * A read-only layout probe wraps getBoundingClientRect on Document/Element prototypes to
 * count forced layouts during the window — the mechanism behind the earlier
 * "frozen renderer" note in voice-input.js. The wrappers are installed with
 * Object.defineProperty and left in place for the duration of the run only.
 *
 * Usage: node perf_render_cost.cjs [--port 9426] [--seconds 30] [--hidden] [--tag before]
 */
const fs = require('node:fs');
const path = require('node:path');
const lib = require('./perf_lib.cjs');

const argv = process.argv.slice(2);
const arg = (name, fallback) => {
  const at = argv.indexOf('--' + name);
  return at >= 0 && argv[at + 1] ? argv[at + 1] : fallback;
};
const PORT = Number(arg('port', 9426));
const SECONDS = Number(arg('seconds', 30));
const TAG = String(arg('tag', 'run'));
const HIDDEN = argv.includes('--hidden');

const LAYOUT_COUNTER = `(() => {
  if (window.__qvLayoutPatched) return 'already';
  window.__qvLayoutPatched = true;
  window.__qvLayoutCalls = { total: 0, byName: {} };
  const bucket = window.__qvLayoutCalls;
  const record = name => { bucket.total += 1; bucket.byName[name] = (bucket.byName[name] || 0) + 1; };
  const wrapMethod = (proto, method, name) => {
    const original = proto[method];
    if (typeof original !== 'function') return;
    Object.defineProperty(proto, method, {
      configurable: true, writable: true,
      value: function (...args) { record(name); return original.apply(this, args); },
    });
  };
  const wrapGetter = (proto, property, name) => {
    const descriptor = Object.getOwnPropertyDescriptor(proto, property);
    if (!descriptor || typeof descriptor.get !== 'function') return;
    Object.defineProperty(proto, property, {
      configurable: true, enumerable: descriptor.enumerable,
      get() { record(name); return descriptor.get.call(this); },
    });
  };
  wrapMethod(Element.prototype, 'getBoundingClientRect', 'Element.getBoundingClientRect');
  wrapGetter(Element.prototype, 'offsetWidth', 'Element.offsetWidth');
  wrapGetter(Element.prototype, 'offsetHeight', 'Element.offsetHeight');
  wrapGetter(Element.prototype, 'scrollHeight', 'Element.scrollHeight');
  wrapGetter(Element.prototype, 'clientHeight', 'Element.clientHeight');
  const nativeComputed = window.getComputedStyle.bind(window);
  window.getComputedStyle = function (...args) { record('getComputedStyle'); return nativeComputed(...args); };
  window.__qvLayoutCalls = bucket;
  return 'patched';
})()`;

const HIDE_JS = `JSON.stringify((() => {
  window.__realHidden = Object.getOwnPropertyDescriptor(Document.prototype, 'hidden');
  window.__realVis = Object.getOwnPropertyDescriptor(Document.prototype, 'visibilityState');
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
  document.dispatchEvent(new Event('visibilitychange'));
  return { hidden: document.hidden };
})())`;

/* Samples the page's own clock against the Node clock so the per-window totals can be
 * converted back to real time: in this headless profile performance.now() advances
 * 1000x faster than wall time, and a raw delta would be reported as 16 hours. */
const clockProbe = () => `JSON.stringify({ perf: performance.now(), nav: performance.timeOrigin })`;

const CALL_COUNTS = `JSON.stringify((() => {
  const bucket = window.__qvLayoutCalls || { total: 0, byName: {} };
  return { total: bucket.total, byName: bucket.byName,
           domNodes: document.getElementsByTagName('*').length,
           mutations: window.__qvMutations || 0,
           chipConnected: !!document.querySelector('#local-composer-tools .local-mode-permission'),
           micDocked: !!document.querySelector('#local-composer-tools .qv-mic') };
})())`;

async function profileWindow(browser, { seconds, stopOnName, label }) {
  await browser.send('Profiler.enable');
  await browser.send('Profiler.setSamplingInterval', { interval: 200 });
  await browser.send('Profiler.start');
  const start = await browser.evaluate(clockProbe());
  await lib.sleep(seconds * 1000);
  const end = await browser.evaluate(clockProbe());
  const { profile } = await browser.send('Profiler.stop');
  const wall = Date.now();

  // Aggregate self time per function, so "who burns the main thread" is answered by the
  // CPU rather than by reading the code.
  const byId = new Map();
  for (const node of profile.nodes) byId.set(node.id, node);
  const selfTime = new Map();
  const total = profile.nodes.reduce((sum, node) => {
    const frame = node.callFrame || {};
    const name = `${frame.functionName || '(anonymous)'} @ ${String(frame.url || '').split('/').pop()}:${(frame.lineNumber || 0) + 1}`;
    const samples = node.hitCount || 0;
    selfTime.set(name, (selfTime.get(name) || 0) + samples);
    return sum + samples;
  }, 0);
  const interval = (profile.endTime - profile.startTime) / Math.max(total, 1);
  const ranked = [...selfTime.entries()].sort((a, b) => b[1] - a[1]).slice(0, 18)
    .map(([name, hits]) => ({ name, hits, ms: Math.round(hits * interval / 1000) }));
  const interesting = stopOnName
    ? ranked.filter(row => stopOnName.test(row.name)) : ranked;
  return { label, wallMs: wall, samples: total, profileMs: Math.round((profile.endTime - profile.startTime) / 1000),
           ranked, interesting, clockStart: start, clockEnd: end };
}

async function main() {
  const browser = await lib.launch({ port: PORT });
  if (lib.injectionGate()) lib.step('injection rewrite: ' + JSON.stringify(await lib.rewriteInjection(browser, lib.INJECTION_VERSIONS)));
  const out = { tag: TAG, seconds: SECONDS, hidden: HIDDEN };
  try {
    await lib.login(browser);
    await lib.waitFor(browser.evaluate, '!!document.querySelector(".local-mode-toolbar")', 40000, 'toolbar');
    await lib.sleep(4000);
    const patched = await browser.evaluate(LAYOUT_COUNTER);
    lib.step('layout probe ' + patched);
    await browser.evaluate(`(() => {
      window.__qvMutations = 0;
      new MutationObserver(records => { window.__qvMutations += records.length; })
        .observe(document.body, { childList: true, subtree: true, attributes: true, characterData: true });
    })()`);
    await browser.reader.arm('render-cost');
    if (HIDDEN) {
      const state = await browser.reader.js(HIDE_JS);
      lib.step('hidden emulation ' + JSON.stringify(state));
    }
    await lib.sleep(1500);

    const before = await browser.reader.js(CALL_COUNTS);
    lib.step(`sampling ${SECONDS}s (hidden=${HIDDEN})`);
    const windowProfile = await profileWindow(browser, {
      seconds: SECONDS,
      stopOnName: /mode-control|voice-input|context-control|image-panel|video-panel|voice-panel|usage-log|share-manager|bundle/,
      label: HIDDEN ? 'hidden' : 'visible',
    });
    const after = await browser.reader.js(CALL_COUNTS);

    // performance.now() here runs ~1000x wall time; scale before printing seconds.
    const clockSpan = windowProfile.clockEnd.perf - windowProfile.clockStart.perf;
    out.clock = { span: +clockSpan.toFixed(1), seconds: SECONDS,
                  scale: clockSpan > 0 ? +(SECONDS * 1000 / clockSpan).toFixed(6) : 1 };

    console.log(`\n--- MAIN THREAD, ${HIDDEN ? 'HIDDEN' : 'VISIBLE'} (${SECONDS}s wall) ---`);
    console.log(`  samples ${windowProfile.samples}, profiled cpu ${windowProfile.profileMs}ms`);
    console.log('  top self time:');
    for (const row of windowProfile.ranked.slice(0, 14)) {
      console.log(`    ${String(row.ms).padStart(5)}ms ${String(row.hits).padStart(6)} hits  ${row.name}`);
    }
    out.ranked = windowProfile.ranked;
    out.ourCode = windowProfile.interesting.filter(row => row.ms > 0);
    console.log('  of which the injected layer:');
    for (const row of out.ourCode.slice(0, 14)) console.log(`    ${String(row.ms).padStart(5)}ms  ${row.name}`);

    console.log('\n--- FORCED LAYOUT / DOM CHURN DURING THE WINDOW ---');
    console.log(`  layout reads: ${after.total - before.total}`);
    const delta = {};
    for (const key of new Set([...Object.keys(before.byName), ...Object.keys(after.byName)])) {
      delta[key] = (after.byName[key] || 0) - (before.byName[key] || 0);
    }
    for (const [key, value] of Object.entries(delta).sort((a, b) => b[1] - a[1]).slice(0, 10)) {
      console.log(`    ${String(value).padStart(6)}  ${key}`);
    }
    console.log(`  DOM mutations: ${after.mutations - before.mutations}`);
    console.log(`  composer chip connected: ${after.chipConnected}, mic docked: ${after.micDocked}`);
    out.layout = { reads: after.total - before.total, byName: delta,
                   mutations: after.mutations - before.mutations,
                   chipConnected: after.chipConnected, micDocked: after.micDocked };

    const outPath = path.join(lib.ROOT, 'web', `perf_render_${TAG}${HIDDEN ? '_hidden' : ''}.json`);
    fs.writeFileSync(outPath, JSON.stringify(out, null, 2), 'utf8');
    console.log(`\nwritten ${outPath}`);
  } finally {
    await browser.close();
  }
  return 0;
}

main().then(code => process.exit(code)).catch(error => { console.error('ERROR', error.stack || error.message); process.exit(1); });
