/* Who is issuing that request? An idle client here sends the same endpoints repeatedly,
 * so "how many per minute" is not enough — the fix differs completely depending on which
 * module owns the call, and one of them (the packaged app) is not ours to change.
 *
 * This listens to CDP Network.requestWillBeSent and, for each request of interest, looks
 * up the performance entry with the same timestamp to get the initiator type, and records
 * the wall-clock offset since load so a call can be attributed to a phase.
 *
 * Usage: node perf_attribution.cjs [--port 9415] [--seconds 40] [--filter /tools,/local/status]
 */
const lib = require('./perf_lib.cjs');

const argv = process.argv.slice(2);
const arg = (name, fallback) => {
  const at = argv.indexOf('--' + name);
  return at >= 0 && argv[at + 1] ? argv[at + 1] : fallback;
};
const PORT = Number(arg('port', 9415));
const SECONDS = Number(arg('seconds', 40));
const FILTERS = String(arg('filter', '/tools,/local/status')).split(',');

async function main() {
  const browser = await lib.launch({ port: PORT });
  if (lib.injectionGate()) lib.step('injection rewrite: ' + JSON.stringify(await lib.rewriteInjection(browser, lib.INJECTION_VERSIONS)));
  const events = [];
  const started = Date.now();
  try {
    // Capture every request the renderer makes, from CDP rather than from the page: the
    // app is free to replace window.fetch and a page-side wrapper would miss its calls.
    browser.ws.addEventListener('message', event => {
      let message;
      try { message = JSON.parse(event.data); } catch (failure) { return; }
      if (message.method !== 'Network.requestWillBeSent') return;
      const request = message.params.request || {};
      const url = String(request.url || '');
      if (!FILTERS.some(fragment => url.includes(fragment))) return;
      events.push({
        atMs: Date.now() - started,
        url: url.replace(lib.BASE, ''),
        method: request.method,
        type: message.params.type,
        initiator: message.params.initiator || {},
        documentURL: message.params.documentURL,
      });
    });
    await browser.send('Network.enable');

    await lib.login(browser);
    await lib.waitFor(browser.evaluate, '!!window.__perf || !!document.querySelector(".local-mode-toolbar")', 40000, 'app');
    await lib.sleep(3000);
    const armResult = await browser.reader.arm('attribution');
    lib.step('recorder ' + JSON.stringify(armResult));

    const before = await browser.reader.counters();
    await lib.sleep(SECONDS * 1000);
    const after = await browser.reader.counters();
    const entries = await browser.reader.drain();

    console.log(`\n--- REQUESTS SEEN BY CDP (first ${SECONDS}s after settle) ---`);
    console.log(`  ${events.length} request(s) matching ${JSON.stringify(FILTERS)}`);
    const byUrl = new Map();
    for (const event of events) {
      const row = byUrl.get(event.url) || { url: event.url, count: 0, atMs: [], initiators: new Set() };
      row.count += 1;
      row.atMs.push(event.atMs);
      row.initiators.add(JSON.stringify(event.initiator));
      byUrl.set(event.url, row);
    }
    for (const row of [...byUrl.values()].sort((a, b) => b.count - a.count)) {
      console.log(`  ${String(row.count).padStart(4)}x ${row.url}`);
      console.log(`        at ms: ${row.atMs.join(', ')}`);
      for (const initiator of row.initiators) console.log(`        initiator: ${initiator}`);
    }

    console.log('\n--- RESOURCE TIMING CROSS-CHECK ---');
    for (const entry of entries) {
      const short = entry.name.replace(lib.BASE, '');
      if (!FILTERS.some(fragment => short.includes(fragment))) continue;
      console.log(`  ${short}  start=${entry.start} duration=${entry.duration} size=${entry.size}`);
    }

    console.log('\n--- COUNTERS DURING THE WINDOW ---');
    const wall = (after.t - before.t);
    console.log(`  page clock advanced ${wall.toFixed(1)}s (wall ${SECONDS}s) — a mismatch means the page reloaded`);
    console.log(`  long tasks ${after.tasks - before.tasks}, blocking ${Math.round(after.taskMs - before.taskMs)}ms, ` +
      `frames ${after.frames - before.frames}, recorder installs ${after.installCount}`);
    console.log(`  errors: ${JSON.stringify(after.errors.slice(0, 6))}`);
  } finally {
    await browser.close();
  }
  return 0;
}

main().then(code => process.exit(code)).catch(error => { console.error('ERROR', error.stack || error.message); process.exit(1); });
