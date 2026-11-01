/**
 * 页面生命周期那一层的检查（第 3 档；纯 Node DOM 桩，不启浏览器、不占显卡）。
 *
 * 这个模块做两件事——页面每 20 秒发一次心跳、`pagehide` 时发一次"我关了"——而它一旦写错
 * （URL 拼错、监听器没挂上、抛异常、关页面后心跳还在发）症状是"关了页面后台照旧挂着"或
 * "页面开着却被收工"，都很难从界面上看出来。所以这里用最小 DOM 桩求值真文件，再真的派发事件。
 *
 *     node web/check_page_lifecycle.cjs
 */
const fs = require('node:fs');
const path = require('node:path');

const FILE = path.join(__dirname, 'page-lifecycle.js');
const ALIVE = '/local/lifecycle/page-alive';
const CLOSED = '/local/lifecycle/page-closed';
const results = [];

function check(label, ok, detail = '') {
  results.push(Boolean(ok));
  console.log(`${ok ? 'PASS' : 'FAIL'} ${label.padEnd(52)} ${detail}`);
}

function harness(options = {}) {
  const listeners = new Map();
  const docListeners = new Map();
  const beacons = [];
  const syncRequests = [];
  const timers = new Map();
  let nextTimer = 1;
  let beaconThrows = false;

  const navigator = {
    sendBeacon(url, body) {
      if (beaconThrows) throw new Error('beacon refused');
      beacons.push({ url, body });
      return true;
    },
  };
  const window = {
    addEventListener(name, handler) {
      listeners.set(name, [...(listeners.get(name) || []), handler]);
    },
  };
  const document = {
    hidden: false,
    addEventListener(name, handler) {
      docListeners.set(name, [...(docListeners.get(name) || []), handler]);
    },
  };
  function XMLHttpRequestStub() {}
  XMLHttpRequestStub.prototype.open = function (method, url) {
    this.method = method; this.url = url;
  };
  XMLHttpRequestStub.prototype.send = function () {
    syncRequests.push({ method: this.method, url: this.url });
  };

  let threw = null;
  if (options.load !== false) {
    try {
      const load = new Function('window', 'navigator', 'document', 'XMLHttpRequest',
        'setInterval', 'clearInterval', fs.readFileSync(FILE, 'utf8'));
      load(window, navigator, document, XMLHttpRequestStub,
        (fn, ms) => { timers.set(nextTimer, { fn, ms }); return nextTimer++; },
        id => { timers.delete(id); });
    } catch (error) {
      threw = error;
    }
  }

  return {
    listeners, docListeners, beacons, syncRequests, timers,
    get threw() { return threw; },
    fire(name, event = {}) { for (const handler of listeners.get(name) || []) handler(event); },
    fireDoc(name) { for (const handler of docListeners.get(name) || []) handler(); },
    beatAll() { for (const { fn } of timers.values()) fn(); },
    refuseBeacons() { beaconThrows = true; },
  };
}

const app = harness();
check('the module evaluates in a bare DOM stub', app.threw === null, app.threw ? app.threw.message : '');
check('it says "still open" once on load', app.beacons.length === 1 && app.beacons[0].url === ALIVE,
  JSON.stringify(app.beacons[0] || null));
check('it schedules a heartbeat', app.timers.size === 1,
  `timers=${app.timers.size} every ${[...app.timers.values()][0]?.ms}ms`);
check('it listens for pagehide', (app.listeners.get('pagehide') || []).length === 1);
check('it also covers beforeunload', (app.listeners.get('beforeunload') || []).length === 1);

app.beatAll();
check('the heartbeat fires again on its timer',
  app.beacons.length === 2 && app.beacons[1].url === ALIVE, `beacons=${app.beacons.length}`);

app.fireDoc('visibilitychange');
check('coming back to the tab beats once more', app.beacons.length === 3,
  `beacons=${app.beacons.length}`);

app.fire('pagehide');
const closed = app.beacons.filter(entry => entry.url === CLOSED);
check('pagehide tells the gateway the page closed', closed.length === 1, JSON.stringify(closed[0] || null));
check('the heartbeat stops once the page is closing', app.timers.size === 0,
  `timers=${app.timers.size}`);

const before = app.beacons.length;
app.fire('beforeunload');
check('beforeunload is idempotent for the gateway', app.beacons.length === before + 1,
  `beacons=${app.beacons.length}`);
app.beatAll();
check('a closing page never beats again', app.beacons.length === before + 1,
  `beacons=${app.beacons.length}`);

const fallback = harness();
fallback.refuseBeacons();
delete fallback.beacons;
check('a refused beacon is swallowed', fallback.threw === null,
  fallback.threw ? fallback.threw.message : '');

console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
process.exit(results.every(Boolean) ? 0 : 1);

