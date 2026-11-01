/* Is the page's fetch() chain ever broken after load?
 *
 * A probe inside this page once threw "Maximum call stack size exceeded" for every route
 * while the page was visibly still loading. Either that is a real, reproducible window in
 * which the page cannot make a request at all, or it was a one-off. This samples the same
 * call at a fixed cadence for a minute, on the owner page and on a guest page, and reports
 * the first failure with the time it happened and what window.fetch looks like then.
 *
 * Usage: node probe_fetch_health.cjs [--port 9459] [--seconds 60] [--owner|--guest]
 */
const fs = require('node:fs');
const path = require('node:path');
const lib = require('./perf_lib.cjs');

const argv = process.argv.slice(2);
const arg = (name, fallback) => {
  const at = argv.indexOf('--' + name);
  return at >= 0 && argv[at + 1] ? argv[at + 1] : fallback;
};
const PORT = Number(arg('port', 9459));
const SECONDS = Number(arg('seconds', 60));
const AS_OWNER = argv.includes('--owner');

const SAMPLE = `(async () => {
  const out = { at: Math.round(performance.now()),
    chain: { native: String(window.fetch).includes('[native code]'),
             name: window.fetch.name, length: String(window.fetch).length,
             context: String(window.fetch).includes('qwenContextFetch') },
    readyState: document.readyState, resources: performance.getEntriesByType('resource').length };
  try {
    const response = await fetch('/local/status', { cache: 'no-store', credentials: 'same-origin' });
    out.status = response.status;
    out.len = (await response.text()).length;
  } catch (error) { out.error = String(error); }
  return JSON.stringify(out);
})()`;

async function main() {
  const browser = await lib.launch({ port: PORT });
  try {
    if (!AS_OWNER) {
      const config = JSON.parse(fs.readFileSync(path.join(lib.ROOT, 'share.config.json'), 'utf8'));
      const login = await fetch(lib.BASE + '/__login', {
        method: 'POST', redirect: 'manual',
        body: new URLSearchParams({ token: String(config.access_token || '') }),
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      });
      const cookie = (login.headers.get('set-cookie') || '').split(';')[0];
      await browser.send('Network.enable');
      await browser.send('Network.clearBrowserCookies');
      await browser.send('Network.setCookie', {
        name: cookie.split('=')[0], value: cookie.slice(cookie.indexOf('=') + 1),
        domain: '127.0.0.1', path: '/', httpOnly: true,
      });
      await browser.send('Page.navigate', { url: lib.BASE + '/' });
    } else {
      await lib.login(browser, '/');
    }
    const label = AS_OWNER ? 'OWNER' : 'GUEST';
    lib.step(`${label}: sampling fetch() health for ${SECONDS}s`);
    const samples = [];
    let failures = 0;
    for (let i = 0; i < SECONDS; i++) {
      const sample = JSON.parse(await browser.evaluate(SAMPLE).catch(error => JSON.stringify({ error: 'evaluate: ' + error.message })));
      samples.push(sample);
      if (sample.error || sample.status !== 200) {
        failures += 1;
        console.log(`  [${String(i).padStart(3)}s] ${sample.error ? sample.error : 'status ' + sample.status}  ` +
          `chain=${JSON.stringify(sample.chain)} resources=${sample.resources} readyState=${sample.readyState}`);
      }
      await lib.sleep(1000);
    }
    const ok = samples.filter(sample => sample.status === 200).length;
    console.log(`\n${label}: ${ok}/${samples.length} samples returned 200, ${failures} failure(s)`);
    const chainStates = new Set(samples.map(sample => JSON.stringify(sample.chain)));
    console.log('  distinct window.fetch shapes seen:');
    for (const shape of chainStates) console.log('    ' + shape);
    const outPath = path.join(lib.ROOT, 'web', `perf_fetch_health_${AS_OWNER ? 'owner' : 'guest'}.json`);
    fs.writeFileSync(outPath, JSON.stringify({ label, samples, failures }, null, 2), 'utf8');
    console.log('  written ' + outPath);
  } finally {
    await browser.close();
  }
  return 0;
}

main().then(code => process.exit(code)).catch(error => { console.error('ERROR', error.stack || error.message); process.exit(1); });
