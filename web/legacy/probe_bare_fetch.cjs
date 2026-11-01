/* Does a guest page's own fetch() survive a page load? One wrapper at a time.
 *
 * Written because a probe inside the same page threw "Maximum call stack size exceeded"
 * on every route when a fetch wrapper was installed, which has two possible meanings —
 * the wrapper recursed, or the page's real fetch chain does — and they must not be
 * confused. This run installs nothing first, then exactly one wrapper, so the two answers
 * are directly comparable.
 *
 * Usage: node probe_bare_fetch.cjs [--port 9457]
 */
const fs = require('node:fs');
const path = require('node:path');
const lib = require('./perf_lib.cjs');

const argv = process.argv.slice(2);
const arg = (name, fallback) => {
  const at = argv.indexOf('--' + name);
  return at >= 0 && argv[at + 1] ? argv[at + 1] : fallback;
};
const PORT = Number(arg('port', 9457));

const PROBE = routes => `(async () => {
  const out = {};
  for (const route of ${JSON.stringify(routes)}) {
    try {
      const response = await fetch(route, { cache: 'no-store', credentials: 'same-origin' });
      const text = await response.text();
      out[route] = response.status + ' len=' + text.length;
    } catch (error) { out[route] = 'THREW ' + error; }
  }
  return JSON.stringify(out);
})()`;

async function main() {
  const browser = await lib.launch({ port: PORT });
  try {
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
    lib.step('guest page loading (no instrumentation installed)');
    await lib.sleep(12000);

    const identity = JSON.parse(await browser.evaluate(`JSON.stringify({
      fetchNative: String(window.fetch).includes('[native code]'),
      fetchName: window.fetch.name, fetchLength: String(window.fetch).length,
      contextWrapper: String(window.fetch).includes('qwenContextFetch'),
      href: location.href,
    })`));
    console.log('window.fetch identity: ' + JSON.stringify(identity));

    const routes = ['/local/status', '/tools', '/local/image/gpu', '/local/share/status'];
    const bare = JSON.parse(await browser.evaluate(PROBE(routes)));
    console.log('\nBARE probe (nothing installed):');
    for (const [route, answer] of Object.entries(bare)) console.log(`  ${route.padEnd(28)} ${answer}`);

    /* Exactly one wrapper, installed the same way the harness installs one, so a failure
     * here is a fact about wrapping this page's fetch rather than about the harness. */
    await browser.evaluate(`(() => {
      const native = window.fetch.bind(window);
      let current = native;
      window.__probeCalls = 0;
      window.__probeCall = async function (input, init) {
        window.__probeCalls += 1;
        return await current(input, init);
      };
      Object.defineProperty(window, 'fetch', {
        configurable: true, enumerable: true,
        get: () => window.__probeCall,
        set: value => { current = typeof value === 'function' ? value.bind(window) : value; },
      });
      return 'wrapped';
    })()`);
    const wrapped = JSON.parse(await browser.evaluate(PROBE(routes.slice(0, 2))));
    console.log('\nWITH exactly one wrapper installed:');
    for (const [route, answer] of Object.entries(wrapped)) console.log(`  ${route.padEnd(28)} ${answer}`);
    console.log('  wrapper was called: ' + await browser.evaluate('window.__probeCalls') + ' time(s)');

    // And the same wrapper on the owner page, so a guest-specific cause would show up.
    await lib.login(browser, '/');
    await lib.sleep(9000);
    await browser.evaluate(`(() => {
      const native = window.fetch.bind(window);
      let current = native;
      window.__ownerCalls = 0;
      window.__ownerCall = async function (input, init) { window.__ownerCalls += 1; return await current(input, init); };
      Object.defineProperty(window, 'fetch', {
        configurable: true, enumerable: true,
        get: () => window.__ownerCall,
        set: value => { current = typeof value === 'function' ? value.bind(window) : value; },
      });
      return 'wrapped';
    })()`);
    const owner = JSON.parse(await browser.evaluate(PROBE(routes)));
    console.log('\nOWNER page, one wrapper installed:');
    for (const [route, answer] of Object.entries(owner)) console.log(`  ${route.padEnd(28)} ${answer}`);
    console.log('  wrapper was called: ' + await browser.evaluate('window.__ownerCalls') + ' time(s)');
  } finally {
    await browser.close();
  }
  return 0;
}

main().then(code => process.exit(code)).catch(error => { console.error('ERROR', error.stack || error.message); process.exit(1); });
