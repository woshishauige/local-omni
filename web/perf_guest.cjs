/* The guest view of the same web layer.
 *
 * The owner's page and a shared visitor's page load the same injected scripts, but the
 * guest is fenced: some routes answer 403, the capability set differs, and the host
 * controls must not render. Two things are worth measuring separately: which injected
 * entry points are dead for a guest (a control that renders and then fails is worse than
 * one that is absent), and whether the idle cost differs.
 *
 * Usage: node perf_guest.cjs [--port 9422] [--idle 20] [--tag before]
 */
const fs = require('node:fs');
const path = require('node:path');
const lib = require('./perf_lib.cjs');

const argv = process.argv.slice(2);
const arg = (name, fallback) => {
  const at = argv.indexOf('--' + name);
  return at >= 0 && argv[at + 1] ? argv[at + 1] : fallback;
};
const PORT = Number(arg('port', 9422));
const IDLE = Number(arg('idle', 20));
const TAG = String(arg('tag', 'run'));

/* The guest page's own view of what the injection offered, and what each of those
 * controls does when used. Reported as data, not as a verdict. */
const GUEST_VIEW = `JSON.stringify((() => {
  const seen = selector => !!document.querySelector(selector);
  const cs = el => el ? getComputedStyle(el).display : null;
  const panel = document.querySelector('#local-share-panel');
  return {
    toolbar: seen('.local-mode-toolbar'),
    modeButtons: Array.from(document.querySelectorAll('.local-mode-button'))
      .map(b => b.dataset.mode + ':' + (b.disabled ? 'disabled' : 'enabled')),
    picker: seen('.local-model-select'),
    pickerDisabled: (document.querySelector('.local-model-select') || {}).disabled,
    chip: seen('.local-tool-permission'),
    chipText: (document.querySelector('.local-tool-permission') || {}).textContent || null,
    mic: seen('.qv-mic'),
    shareLauncher: seen('.local-share-launcher, .local-share-entry'),
    usageEntry: seen('.local-usage-entry, .local-usage-launcher'),
    ctxEntry: seen('.local-context-entry, .local-context-launcher'),
    panels: { image: !!window.ImagePanel, video: !!window.VideoPanel, voice: !!window.VoicePanel,
              share: !!window.ShareManager, usage: !!window.UsageLog, context: !!window.ContextControl },
    localStorageKeys: Object.keys(localStorage),
    globals: Object.keys(window).filter(k => /^(ImagePanel|VideoPanel|VoicePanel|ShareManager|UsageLog|ContextControl|LocalModes|QwenVoice)$/.test(k)),
  };
})())`;

/* Which injected endpoints answer for a guest. A 403 here is the fence working; a 500 is
 * a defect. The distinction is the whole point of running this. */
const PROBE_ROUTES = `(async () => {
  const routes = ['/local/status', '/tools', '/local/voice/config', '/local/context/config',
                  '/local/image/meta', '/local/image/gpu', '/local/video/progress', '/local/video/comfy',
                  '/local/video/recipes', '/local/video/history', '/local/video/output', '/local/video/folders',
                  '/local/usage', '/local/routing', '/local/gpu', '/local/voice/voices', '/local/voice/status',
                  '/local/image/history', '/local/image/workflows', '/local/context/status?conversation_id=x',
                  '/local/share/status', '/local/share/uploads', '/local/share/guest-policy'];
  const out = {};
  for (const route of routes) {
    try {
      const response = await fetch(route, { cache: 'no-store', credentials: 'same-origin' });
      let note = '';
      try {
        const text = await response.text();
        note = text.slice(0, 120).replace(/\\s+/g, ' ');
      } catch (e) { note = 'unreadable'; }
      out[route] = response.status + ' ' + note;
    } catch (error) { out[route] = 'THREW ' + error; }
  }
  return JSON.stringify(out);
})()`;

async function main() {
  const browser = await lib.launch({ port: PORT });
  if (lib.injectionGate()) lib.step('injection rewrite: ' + JSON.stringify(await lib.rewriteInjection(browser, lib.INJECTION_VERSIONS)));
  const out = { tag: TAG };
  try {
    const config = JSON.parse(fs.readFileSync(path.join(lib.ROOT, 'share.config.json'), 'utf8'));
    const token = String(config.access_token || '');
    lib.step(`guest token ${token ? 'present' : 'MISSING'} (${token.length} chars)`);

    /* Become a guest for real. Two traps here, both hit on the first attempt:
     *   · clearing cookies with document.cookie cannot remove an httpOnly session cookie,
     *     so the owner's session survives and every "guest" probe answers as the owner;
     *   · the login form answers 303, and fetch with redirect:manual leaves the cookie
     *     unset in a way that looks like success.
     * So the session is created over plain HTTP (Node is not the owner — no loopback
     * header, no admin cookie), and only the resulting cookie is injected into the browser. */
    const body = new URLSearchParams({ token });
    const login = await fetch(lib.BASE + '/__login', {
      method: 'POST', body, redirect: 'manual',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    });
    const setCookie = login.headers.get('set-cookie') || '';
    const cookie = setCookie.split(';')[0];
    if (!cookie.includes('=')) throw new Error('no session cookie returned: ' + login.status + ' ' + setCookie);
    lib.step(`device session issued over HTTP (${login.status}); cookie ${cookie.split('=')[0]}`);

    await browser.send('Network.enable');
    await browser.send('Network.clearBrowserCookies');
    await browser.send('Network.setCookie', {
      name: cookie.split('=')[0], value: cookie.slice(cookie.indexOf('=') + 1),
      domain: '127.0.0.1', path: '/', httpOnly: true,
    });
    await browser.send('Page.navigate', { url: lib.BASE + '/' });
    await lib.sleep(1500);
    await lib.waitFor(browser.evaluate, 'document.readyState === "complete"', 30000, 'guest page');
    await browser.reader.arm('guest');
    await lib.sleep(5000);

    // Prove the identity before reporting anything, so an owner-cookie leak cannot be
    // mistaken for a guest result.
    const identity = await browser.evaluate(`(async () => {
      const status = await fetch('/local/share/status', { cache: 'no-store', credentials: 'same-origin' });
      const uploads = await fetch('/local/share/uploads', { cache: 'no-store', credentials: 'same-origin' });
      const usage = await fetch('/local/usage', { cache: 'no-store', credentials: 'same-origin' });
      return JSON.stringify({ shareStatus: status.status, shareUploads: uploads.status, usage: usage.status,
        ownerControls: !!window.ShareManager, href: location.href });
    })()`);
    out.identity = JSON.parse(identity);
    console.log('\n--- IDENTITY CHECK ---');
    console.log('  ' + identity);
    if (out.identity.ownerControls) {
      throw new Error('this page still has an owner session: guest results would be meaningless');
    }

    const who = await browser.evaluate(`JSON.stringify({ href: location.href, title: document.title,
      body: document.body.innerText.slice(0, 220) })`);
    console.log('  page: ' + who);

    const view = await browser.reader.js(GUEST_VIEW);
    out.view = view;
    console.log('\n--- INJECTED CONTROLS PRESENT FOR THIS IDENTITY ---');
    for (const [key, value] of Object.entries(view)) {
      console.log(`  ${key.padEnd(18)} ${JSON.stringify(value)}`);
    }

    const resources = await browser.reader.drain();
    const summary = lib.report('GUEST BOOT', resources, 5);
    out.boot = summary;

    lib.step('guest idle start');
    await browser.reader.arm('guest-idle');
    /* Counted three ways for the same reason as the owner probe: a zero must be
     * distinguishable from a broken measurement. CDP events are the authority here, because
     * they see every renderer request regardless of which module owns window.fetch. */
    const cdp = [];
    let capturing = false;
    browser.ws.addEventListener('message', event => {
      let message;
      try { message = JSON.parse(event.data); } catch (failure) { return; }
      if (message.method !== 'Network.requestWillBeSent' || !capturing) return;
      const url = String((message.params.request || {}).url || '').replace(lib.BASE, '');
      if (url.startsWith('/')) cdp.push(url);
    });
    await browser.send('Network.enable');
    const before = await browser.reader.counters();
    capturing = true;
    await lib.sleep(IDLE * 1000);
    capturing = false;
    const idleEntries = await browser.reader.drain();
    const after = await browser.reader.counters();
    const byPath = new Map();
    for (const url of cdp) {
      let key = url;
      try { const parsed = new URL(lib.BASE + url); key = parsed.pathname + parsed.search; } catch (failure) { /* keep */ }
      byPath.set(key, (byPath.get(key) || 0) + 1);
    }
    const idleSummary = lib.report('GUEST IDLE, visible', idleEntries, IDLE);
    out.idle = { ...idleSummary, cdp: { count: cdp.length, byPath: Object.fromEntries(byPath) },
      blockingMs: Math.round(after.taskMs - before.taskMs),
      longTasks: after.tasks - before.tasks, navigated: before.navTime !== after.navTime };
    console.log(`  CDP counted ${cdp.length} request(s) (${(cdp.length / IDLE * 60).toFixed(1)}/min): ` +
      [...byPath.entries()].map(row => `${row[1]}x ${row[0]}`).join(', '));
    console.log(`  long tasks ${out.idle.longTasks}, blocking ${out.idle.blockingMs}ms, navigated=${out.idle.navigated}`);

    const routes = await browser.reader.js(PROBE_ROUTES);
    out.routes = routes;
    console.log('\n--- ROUTE ANSWERS AS A GUEST ---');
    for (const [route, answer] of Object.entries(routes)) console.log(`  ${route.padEnd(48)} ${answer}`);

    const outPath = path.join(lib.ROOT, 'web', `perf_guest_${TAG}.json`);
    fs.writeFileSync(outPath, JSON.stringify(out, null, 2), 'utf8');
    console.log(`\nwritten ${outPath}`);
  } finally {
    await browser.close();
  }
  return 0;
}

main().then(code => process.exit(code)).catch(error => { console.error('ERROR', error.stack || error.message); process.exit(1); });
