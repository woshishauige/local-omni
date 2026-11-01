/* The web layer's real cost, measured from inside a live page.
 *
 * Answers, with numbers taken from the browser itself:
 *   · requests and bytes an idle client sends per minute, tab visible and tab hidden
 *   · how much the boot costs (bytes, long tasks)
 *   · main-thread blocking and animation-frame gaps
 *   · duplicate same-URL calls that overlap in time
 *
 * Usage: node perf_probe.cjs [--port 9411] [--idle 60] [--hidden 30] [--tag baseline]
 */
const fs = require('node:fs');
const path = require('node:path');
const lib = require('./perf_lib.cjs');

const argv = process.argv.slice(2);
const arg = (name, fallback) => {
  const at = argv.indexOf('--' + name);
  return at >= 0 && argv[at + 1] ? argv[at + 1] : fallback;
};
const PORT = Number(arg('port', 9411));
const IDLE_SECONDS = Number(arg('idle', 60));
const HIDDEN_SECONDS = Number(arg('hidden', 30));
const TAG = String(arg('tag', 'run'));

const LAYOUT_JS = `JSON.stringify((() => {
  const q = s => document.querySelector(s);
  const box = el => { if (!el) return null; const r = el.getBoundingClientRect();
    return { w: Math.round(r.width), h: Math.round(r.height), top: Math.round(r.top), left: Math.round(r.left) }; };
  return {
    toolbar: box(q('.local-mode-toolbar')),
    dock: box(q('#local-composer-tools')),
    mic: box(q('.qv-mic')),
    chip: box(q('.local-tool-permission')),
    composer: box(q('.conversation-chat-form')),
    domNodes: document.getElementsByTagName('*').length,
    appButtons: Array.from(document.querySelectorAll('button'))
      .map(b => ((b.getAttribute('aria-label') || b.textContent || '').trim())).filter(Boolean),
    localStorageKeys: Object.keys(localStorage),
    visibility: document.visibilityState,
    appFetchCalls: (window.__perf && window.__perf.fetches || []).length,
  };
})())`;

const HIDE_JS = `JSON.stringify((() => {
  window.__realHidden = Object.getOwnPropertyDescriptor(Document.prototype, 'hidden');
  window.__realVis = Object.getOwnPropertyDescriptor(Document.prototype, 'visibilityState');
  try {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
    document.dispatchEvent(new Event('visibilitychange'));
    return { hidden: document.hidden === true };
  } catch (e) { return { hidden: 'ERR ' + e }; }
})())`;

const SHOW_JS = `JSON.stringify((() => {
  try { Object.defineProperty(document, 'hidden', window.__realHidden);
        Object.defineProperty(document, 'visibilityState', window.__realVis);
        document.dispatchEvent(new Event('visibilitychange')); } catch (e) {}
  return { hidden: document.hidden };
})())`;

/* Pairs of calls to the same path whose lifetimes overlap: the same request issued again
 * before the first one answered. Every group carries the raw timestamps so the claim can
 * be re-checked instead of believed. */
const CONCURRENCY_JS = `JSON.stringify((() => {
  const calls = (window.__perf.fetches || []).slice();
  const pathOf = name => { try { return new URL(name, location.origin).pathname; } catch (e) { return name; } };
  const paths = new Map();
  for (const call of calls) {
    const key = pathOf(call.name);
    if (!paths.has(key)) paths.set(key, []);
    paths.get(key).push(call);
  }
  const groups = [];
  for (const [key, list] of paths) {
    list.sort((a, b) => a.start - b.start);
    let overlaps = 0; const samples = [];
    for (let i = 1; i < list.length; i++) {
      if (list[i].start < list[i - 1].end) {
        overlaps += 1;
        if (samples.length < 4) samples.push({ prevStart: list[i-1].start, prevEnd: list[i-1].end,
                                               start: list[i].start, end: list[i].end });
      }
    }
    groups.push({ key, count: list.length, overlaps, samples,
                  statuses: [...new Set(list.map(c => c.status))] });
  }
  groups.sort((a, b) => b.overlaps - a.overlaps);
  return { total: calls.length, groups: groups.slice(0, 16) };
})())`;

const TASKS_JS = `JSON.stringify({
  tasks: (window.__perf.tasks || []).slice().sort((a, b) => b.duration - a.duration).slice(0, 15),
  total: window.__perf.totalTasks, taskMs: Math.round(window.__perf.taskMs),
  frames: window.__perf.frames, longAnims: (window.__perf.longAnims || []).slice(0, 15),
  errors: window.__perf.errors,
  documentLoad: Math.round((performance.getEntriesByType('navigation')[0] || {}).domContentLoadedEventEnd || 0),
})`;

async function main() {
  const browser = await lib.launch({ port: PORT });
  if (lib.injectionGate()) lib.step('injection rewrite: ' + JSON.stringify(await lib.rewriteInjection(browser, lib.INJECTION_VERSIONS)));
  const out = { tag: TAG, idleSeconds: IDLE_SECONDS, hiddenSeconds: HIDDEN_SECONDS, phases: {} };
  /* A second, independent request counter.
   *
   * The resource-timing drain is the primary source — it carries bytes and durations — but
   * it can only see entries still inside the buffer and only in the document that is
   * current. When it reports zero there are two possible meanings: the page really went
   * quiet, or the measurement stopped working. Those must never be confused, so CDP counts
   * every renderer request for the life of the session, with no buffer and no page code
   * involved, and both numbers are printed for every window. */
  const cdpPhases = [];
  let cdpPhase = null;
  browser.ws.addEventListener('message', event => {
    let message;
    try { message = JSON.parse(event.data); } catch (failure) { return; }
    if (message.method !== 'Network.requestWillBeSent') return;
    if (!cdpPhase) return;
    const request = message.params.request || {};
    if (!String(request.url || '').startsWith(lib.BASE)) return;
    cdpPhase.push(String(request.url).replace(lib.BASE, ''));
  });
  await browser.send('Network.enable');
  const beginPhase = name => {
    const list = [];
    cdpPhases.push({ name, list });
    cdpPhase = list;
    return list;
  };
  const summariseCdp = list => {
    const counts = new Map();
    for (const url of list) {
      let key = url;
      try { const parsed = new URL(lib.BASE + url); key = parsed.pathname + parsed.search; } catch (failure) { /* keep */ }
      counts.set(key, (counts.get(key) || 0) + 1);
    }
    return { count: list.length, byPath: [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12) };
  };
  try {
    const navStart = Date.now();
    lib.step('navigating');
    await lib.login(browser);
    lib.step('installing recorder');
    out.arm = await browser.reader.arm('boot');
    await lib.sleep(4000);
    const bootMs = Date.now() - navStart;
    lib.step('settled');

    const boot = await browser.reader.drain();
    out.phases.boot = { ms: bootMs, ...lib.report('BOOT (navigate through settle)', boot, bootMs / 1000) };

    const beforeIdle = await browser.reader.counters();
    out.phases.boot.counters = beforeIdle;
    const layout = await browser.reader.js(LAYOUT_JS);
    out.layout = layout;
    console.log('\n--- LAYOUT / DOM ---');
    console.log('  toolbar       ', JSON.stringify(layout.toolbar));
    console.log('  composer dock ', JSON.stringify(layout.dock), ' mic', JSON.stringify(layout.mic),
      ' chip', JSON.stringify(layout.chip));
    console.log('  composer      ', JSON.stringify(layout.composer));
    console.log('  dom nodes     ', layout.domNodes, ' visibility', layout.visibility);
    console.log('  localStorage  ', JSON.stringify(layout.localStorageKeys));
    console.log('  app buttons   ', JSON.stringify(layout.appButtons.slice(0, 28)));

    // ---- idle, visible -----------------------------------------------------
    lib.step('idle visible start');
    out.arm = await browser.reader.arm('idle-visible');
    const idleCdp = beginPhase('idle-visible');
    const idleBefore = await browser.reader.counters();
    await lib.sleep(IDLE_SECONDS * 1000);
    const idleEntries = await browser.reader.drain();
    const idleAfter = await browser.reader.counters();
    const idleCdpSummary = summariseCdp(idleCdp);
    lib.step('idle visible done');
    const idleSummary = lib.report('IDLE, tab visible', idleEntries, IDLE_SECONDS);
    out.phases.idleVisible = { ...idleSummary, before: idleBefore, after: idleAfter,
      cdp: idleCdpSummary,
      blockingMs: Math.round(idleAfter.taskMs - idleBefore.taskMs),
      longTasks: idleAfter.tasks - idleBefore.tasks,
      frames: idleAfter.frames - idleBefore.frames,
      pageClock: +(idleAfter.t - idleBefore.t).toFixed(1),
      navigated: idleBefore.navTime !== idleAfter.navTime,
      reloads: idleAfter.reloads,
      invalid: idleBefore.navTime !== idleAfter.navTime ? 'the page reloaded inside this window' : null };
    console.log(`  CDP counted ${idleCdpSummary.count} request(s) in the same window ` +
      `(${(idleCdpSummary.count / IDLE_SECONDS * 60).toFixed(1)}/min): ` +
      idleCdpSummary.byPath.map(row => `${row[1]}x ${row[0]}`).join(', '));
    console.log(`  long tasks: ${out.phases.idleVisible.longTasks}, blocking ${out.phases.idleVisible.blockingMs}ms,` +
      ` frames ${out.phases.idleVisible.frames}, page clock ${out.phases.idleVisible.pageClock}s,` +
      ` navigated=${out.phases.idleVisible.navigated}`);

    // ---- idle, hidden ------------------------------------------------------
    // Emulate hidden through the flag the modules actually read. The renderer stays
    // alive, so this measures the modules' own discipline rather than the browser's
    // background throttling — which is exactly the part that is ours to fix.
    lib.step('idle hidden start');
    const hiddenOK = await browser.reader.js(HIDE_JS);
    out.arm = await browser.reader.arm('idle-hidden');
    const hiddenCdp = beginPhase('idle-hidden');
    const hiddenBefore = await browser.reader.counters();
    await lib.sleep(HIDDEN_SECONDS * 1000);
    const hiddenEntries = await browser.reader.drain();
    const hiddenAfter = await browser.reader.counters();
    const hiddenCdpSummary = summariseCdp(hiddenCdp);
    lib.step('idle hidden done');
    const hiddenSummary = lib.report('IDLE, tab hidden (document.hidden = true)', hiddenEntries, HIDDEN_SECONDS);
    out.phases.idleHidden = { ...hiddenSummary, before: hiddenBefore, after: hiddenAfter,
      cdp: hiddenCdpSummary,
      blockingMs: Math.round(hiddenAfter.taskMs - hiddenBefore.taskMs),
      longTasks: hiddenAfter.tasks - hiddenBefore.tasks,
      frames: hiddenAfter.frames - hiddenBefore.frames,
      pageClock: +(hiddenAfter.t - hiddenBefore.t).toFixed(1),
      navigated: hiddenBefore.navTime !== hiddenAfter.navTime,
      reloads: hiddenAfter.reloads,
      invalid: hiddenBefore.navTime !== hiddenAfter.navTime ? 'the page reloaded inside this window' : null };
    console.log(`  hidden emulation: ${JSON.stringify(hiddenOK)}`);
    console.log(`  CDP counted ${hiddenCdpSummary.count} request(s) in the same window ` +
      `(${(hiddenCdpSummary.count / HIDDEN_SECONDS * 60).toFixed(1)}/min): ` +
      hiddenCdpSummary.byPath.map(row => `${row[1]}x ${row[0]}`).join(', '));
    console.log(`  long tasks: ${out.phases.idleHidden.longTasks}, blocking ${out.phases.idleHidden.blockingMs}ms,` +
      ` frames ${out.phases.idleHidden.frames}, page clock ${out.phases.idleHidden.pageClock}s`);
    await browser.reader.js(SHOW_JS);
    await lib.sleep(1500);

    // ---- duplicates --------------------------------------------------------
    out.arm = await browser.reader.arm('analysis');
    const concurrency = await browser.reader.js(CONCURRENCY_JS);
    out.concurrency = concurrency;
    console.log('\n--- OVERLAPPING SAME-URL CALLS (duplicate concurrency) ---');
    console.log('  window.fetch calls observed:', concurrency.total);
    let any = false;
    for (const row of concurrency.groups) {
      if (!row.overlaps) continue;
      any = true;
      console.log(`    ${row.key}: ${row.count} calls, ${row.overlaps} overlapping, statuses ${JSON.stringify(row.statuses)}`);
      console.log(`      e.g. ` + row.samples.map(s => `[${s.prevStart}..${s.prevEnd}] vs [${s.start}..${s.end}]`).join('  '));
    }
    if (!any) console.log('    (no overlap detected)');

    const taskReport = await browser.reader.js(TASKS_JS);
    out.tasks = taskReport;
    console.log('\n--- MAIN THREAD ---');
    console.log(`  long tasks total ${taskReport.total}, cumulative blocking ${taskReport.taskMs}ms, frames ${taskReport.frames}`);
    console.log('  worst:', JSON.stringify(taskReport.tasks.slice(0, 8)));
    console.log('  worst frame gaps:', JSON.stringify(taskReport.longAnims.slice(0, 8)));
    console.log('  page errors:', JSON.stringify(taskReport.errors.slice(0, 8)));

    const outPath = path.join(lib.ROOT, 'web', `perf_${TAG}.json`);
    out.cdpPhases = cdpPhases.map(entry => ({ name: entry.name, ...summariseCdp(entry.list) }));
    fs.writeFileSync(outPath, JSON.stringify(out, null, 2), 'utf8');
    console.log(`\nwritten ${outPath}`);
  } finally {
    await browser.close();
  }
  return 0;
}

main().then(code => process.exit(code)).catch(error => { console.error('ERROR', error.stack || error.message); process.exit(1); });
