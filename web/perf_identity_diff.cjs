/* The same page, two identities.
 *
 * Every injected control is decided at runtime from what the gateway says about the
 * caller, so the only honest way to review the injection layer for a shared visitor is to
 * load it twice and diff. This run captures, for each identity: every button on screen
 * (label plus a usable selector), every injected global, and the trace (a text label plus a
 * selector) of each injected module that is actually attached to the page.
 *
 * Usage: node perf_identity_diff.cjs [--port 9430] [--tag before]
 */
const fs = require('node:fs');
const path = require('node:path');
const lib = require('./perf_lib.cjs');

const argv = process.argv.slice(2);
const arg = (name, fallback) => {
  const at = argv.indexOf('--' + name);
  return at >= 0 && argv[at + 1] ? argv[at + 1] : fallback;
};
const PORT = Number(arg('port', 9430));
const TAG = String(arg('tag', 'run'));

const INVENTORY = `JSON.stringify((() => {
  const describe = el => {
    if (!el) return null;
    const id = el.id ? '#' + el.id : '';
    const cls = (typeof el.className === 'string' && el.className)
      ? '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.') : '';
    return el.tagName.toLowerCase() + id + cls;
  };
  const buttons = Array.from(document.querySelectorAll('button, [role="button"], a[href]'))
    .map(el => ({
      label: (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 40),
      selector: describe(el),
      hidden: el.hidden || getComputedStyle(el).display === 'none',
      disconnected: !el.isConnected,
    }))
    .filter(row => row.label);
  const globals = ['QwenShare', 'QwenUsage', 'QwenContext', 'QwenVoice', 'LocalModes',
                   'ImagePanel', 'VideoPanel', 'VoicePanel', 'ShareManager', 'UsageLog', 'ContextControl']
    .map(name => name + '=' + (typeof window[name]));
  const modules = ['.local-mode-toolbar', '#local-composer-tools', '.qv-mic', '#local-image-panel',
                   '#local-video-panel', '#local-voice-panel', '#qwen-share-dialog', '#qwen-usage-dialog',
                   '.qs-entry-button', '.qu-entry-button', '.local-context-footer']
    .map(selector => selector + '=' + (document.querySelector(selector) ? 'present' : 'absent'));
  return { buttons, globals, modules,
           bodyText: document.body.innerText.replace(/\\s+/g, ' ').slice(0, 400),
           href: location.href };
})())`;

async function snapshot(browser, label) {
  await lib.waitFor(browser.evaluate, 'document.readyState === "complete"', 30000, label + ' load');
  await lib.sleep(5000);
  // Read through Runtime.evaluate directly: this inventory is a pure DOM report and must
  // not depend on the traffic recorder being armed in this document.
  const inventory = JSON.parse(await browser.evaluate(INVENTORY));
  const identity = JSON.parse(await browser.evaluate(`(async () => {
    const probe = async path => { try {
      const r = await fetch(path, { cache: 'no-store', credentials: 'same-origin' });
      return path + '=' + r.status;
    } catch (e) { return path + '=THREW'; } };
    const results = [];
    for (const path of ['/local/usage', '/local/share/status', '/local/share/uploads',
                        '/local/routing', '/local/image/history', '/local/video/history']) {
      results.push(await probe(path));
    }
    return JSON.stringify({ label: ${JSON.stringify(label)}, href: location.href, probes: results });
  })()`));
  console.log(`\n===== ${label} =====`);
  console.log('  ' + JSON.stringify(identity));
  console.log('  globals: ' + inventory.globals.join('  '));
  console.log('  modules: ' + inventory.modules.join('  '));
  console.log('  buttons:');
  for (const row of inventory.buttons) {
    console.log(`    ${row.hidden ? '[hidden]' : '        '} ${row.selector.padEnd(34)} ${row.label}`);
  }
  console.log('  body: ' + inventory.bodyText);
  return { identity: JSON.parse(identity), inventory };
}

async function main() {
  const browser = await lib.launch({ port: PORT });
  if (lib.injectionGate()) lib.step('injection rewrite: ' + JSON.stringify(await lib.rewriteInjection(browser, lib.INJECTION_VERSIONS)));
  const out = { tag: TAG };
  try {
    await lib.login(browser, '/');
    out.owner = await snapshot(browser, 'owner');

    // A visitor: session cookie created over plain HTTP, so no loopback admin cookie exists.
    const config = JSON.parse(fs.readFileSync(path.join(lib.ROOT, 'share.config.json'), 'utf8'));
    const login = await fetch(lib.BASE + '/__login', {
      method: 'POST', redirect: 'manual',
      body: new URLSearchParams({ token: String(config.access_token || '') }),
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    });
    const cookie = (login.headers.get('set-cookie') || '').split(';')[0];
    if (!cookie.includes('=')) throw new Error('no guest session cookie: ' + login.status);
    lib.step('guest session ' + cookie.split('=')[0]);
    await browser.send('Network.enable');
    await browser.send('Network.clearBrowserCookies');
    await browser.send('Network.setCookie', {
      name: cookie.split('=')[0], value: cookie.slice(cookie.indexOf('=') + 1),
      domain: '127.0.0.1', path: '/', httpOnly: true,
    });
    await browser.send('Page.navigate', { url: lib.BASE + '/' });
    out.guest = await snapshot(browser, 'guest');

    const ownerLabels = new Set(out.owner.inventory.buttons.map(row => row.label));
    const guestLabels = new Set(out.guest.inventory.buttons.map(row => row.label));
    console.log('\n===== DIFF =====');
    console.log('  only for the owner: ' +
      JSON.stringify([...ownerLabels].filter(label => !guestLabels.has(label))));
    console.log('  only for the guest: ' +
      JSON.stringify([...guestLabels].filter(label => !ownerLabels.has(label))));
    const ownerModules = new Set(out.owner.inventory.modules.filter(row => row.endsWith('present')));
    const guestModules = new Set(out.guest.inventory.modules.filter(row => row.endsWith('present')));
    console.log('  module only for the owner: ' + JSON.stringify([...ownerModules].filter(row => !guestModules.has(row))));
    console.log('  module only for the guest: ' + JSON.stringify([...guestModules].filter(row => !ownerModules.has(row))));
    out.diff = {
      ownerOnlyLabels: [...ownerLabels].filter(label => !guestLabels.has(label)),
      guestOnlyLabels: [...guestLabels].filter(label => !ownerLabels.has(label)),
      ownerOnlyModules: [...ownerModules].filter(row => !guestModules.has(row)),
      guestOnlyModules: [...guestModules].filter(row => !ownerModules.has(row)),
    };

    const outPath = path.join(lib.ROOT, 'web', `perf_identity_${TAG}.json`);
    fs.writeFileSync(outPath, JSON.stringify(out, null, 2), 'utf8');
    console.log(`\nwritten ${outPath}`);
  } finally {
    await browser.close();
  }
  return 0;
}

main().then(code => process.exit(code)).catch(error => { console.error('ERROR', error.stack || error.message); process.exit(1); });
