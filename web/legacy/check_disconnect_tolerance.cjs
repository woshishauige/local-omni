/* The toolbar must not call the service disconnected over a single missed poll.
 *
 * This exists because it did: one fetch failure — including the poll that is in flight when
 * the page reloads itself after a completed mode change — set the disconnected flag, which
 * shows "本地服务连接失败" and blocks the composer. A real browser run
 * (check_mode_switch_live.cjs) shows the routes are fine; this file pins the tolerance logic
 * itself, by driving the real module's poll against a scripted fetch and reading the words
 * the toolbar actually puts on screen.
 *
 * Usage: node check_disconnect_tolerance.cjs
 */
const fs = require('node:fs');
const vm = require('node:vm');

const FILE = 'D:\\AI\\Qwen-Local\\web\\mode-control.js';
const source = fs.readFileSync(FILE, 'utf8');
const results = [];

function check(label, ok, detail = '') {
  results.push(Boolean(ok));
  console.log(`${ok ? 'PASS' : 'FAIL'} ${label.padEnd(56)} ${detail}`);
}
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

/* A DOM stub whose elements remember their attributes, so the module's own toolbar can be
 * found by id afterwards. The previous version queried by selector and compared against
 * whatever the stub happened to hand back, which is how it managed to assert nothing. */
function makeElement(tag) {
  const node = {
    tagName: String(tag).toUpperCase(), children: [], dataset: {}, hidden: false,
    className: '', textContent: '', value: '', id: '', type: '', title: '', disabled: false,
    style: { setProperty() {}, removeProperty() {} },
    append(...kids) { node.children.push(...kids); },
    replaceChildren(...kids) { node.children = kids; },
    prepend(...kids) { node.children.unshift(...kids); },
    insertBefore(child) { node.children.push(child); },
    remove() {}, addEventListener() {}, removeEventListener() {}, focus() {}, blur() {},
    setAttribute(name, value) { node.dataset['attr:' + name] = String(value); },
    getAttribute(name) { return node.dataset['attr:' + name] ?? null; },
    removeAttribute() {}, contains() { return false; }, matches() { return false; },
    querySelector() { return makeElement('div'); }, querySelectorAll() { return []; },
    closest() { return null; },
    getBoundingClientRect() { return { top: 0, left: 0, width: 100, height: 20 }; },
    dispatchEvent() { return true; },
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  };
  return node;
}

const created = [];
const documentStub = makeElement('document');
documentStub.body = makeElement('body');
documentStub.head = makeElement('head');
documentStub.documentElement = makeElement('html');
documentStub.readyState = 'complete';
documentStub.hidden = false;
documentStub.createElement = tag => { const node = makeElement(tag); created.push(node); return node; };
documentStub.createElementNS = () => { const node = makeElement('svg'); created.push(node); return node; };
documentStub.createTextNode = text => ({ textContent: String(text) });
documentStub.getElementById = () => null;
documentStub.querySelector = () => null;
documentStub.querySelectorAll = () => [];
documentStub.addEventListener = () => {};
documentStub.removeEventListener = () => {};

/* One queued response per poll. Entries are a status number, the string 'throw' (which is
 * what an aborted or timed-out fetch looks like here) or an object returned as the payload. */
const responses = [];
let polls = 0;
function fetchStub(url) {
  polls += 1;
  const next = responses.length ? responses.shift() : 200;
  if (next === 'throw') return Promise.reject(new TypeError('Failed to fetch'));
  if (typeof next === 'number') {
    return Promise.resolve({ ok: next >= 200 && next < 300, status: next, json: async () => ({}) });
  }
  return Promise.resolve({ ok: true, status: 200, json: async () => next });
}

const status = {
  mode: 'text', state: 'ready', revision: 1, instance_id: 'abc',
  active_requests: 0, model_profiles: [],
};

const sandbox = {
  document: documentStub,
  navigator: { userAgent: 'node', serviceWorker: { register: () => Promise.resolve() } },
  location: { href: 'http://127.0.0.1:8081/', reload() { sandbox._reloaded = true; } },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  fetch: fetchStub,
  setTimeout, clearTimeout, setInterval, clearInterval,
  AbortSignal: { timeout: () => undefined },
  MutationObserver: class { observe() {} disconnect() {} },
  ResizeObserver: class { observe() {} disconnect() {} unobserve() {} },
  console, JSON, Number, Object, Array, String, Boolean, Math, Date, Error, Promise,
  RegExp, Map, Set,
};
sandbox.globalThis = sandbox;
sandbox.window = sandbox;
sandbox.addEventListener = () => {};
sandbox.removeEventListener = () => {};
sandbox.dispatchEvent = () => true;
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'mode-control.js' });

const toolbar = () => created.find(node => node.id === 'local-mode-toolbar');
const toolbarState = () => (toolbar() ? toolbar().dataset.state || '' : '');
/* Every word the module puts on screen, whichever node holds it: reading the whole tree is
 * what makes this assertion about behaviour rather than about element identity. */
function screenText() {
  const words = [];
  const visit = node => {
    if (!node || typeof node !== 'object') return;
    if (typeof node.textContent === 'string' && node.textContent) words.push(node.textContent);
    for (const child of node.children || []) visit(child);
  };
  visit(toolbar());
  return words.join(' ');
}

(async () => {
  await sleep(60);
  check('the module evaluated and painted its toolbar', Boolean(toolbar()), `${polls} polls`);

  // A single miss must show the waiting line, never the disconnected one.
  responses.length = 0;
  responses.push(status, status, 'throw', status, status, status);
  await sleep(4200);
  check('one failed poll does not say "连接失败"', !screenText().includes('连接失败'),
    `screen=${JSON.stringify(screenText().slice(0, 60))}`);
  check('one failed poll does not mark the toolbar offline', toolbarState() !== 'offline',
    `state=${toolbarState()}`);

  // Three misses in a row must still say it, or a real outage would be hidden.
  responses.length = 0;
  for (let i = 0; i < 10; i++) responses.push('throw');
  await sleep(6000);
  check('three failed polls do say "连接失败"',
    toolbarState() === 'offline' || screenText().includes('连接失败'),
    `state=${toolbarState()} screen=${JSON.stringify(screenText().slice(0, 60))}`);

  // ...and a recovery clears it.
  responses.length = 0;
  for (let i = 0; i < 6; i++) responses.push(status);
  await sleep(3600);
  check('a successful poll clears the offline state',
    toolbarState() !== 'offline' && !screenText().includes('连接失败'),
    `state=${toolbarState()}`);

  // A signed-out response is its own message, not a disconnection.
  responses.length = 0;
  responses.push(401, 401, status, status);
  await sleep(3600);
  check('401 is reported as a login problem, not a disconnect',
    !screenText().includes('连接失败'), `screen=${JSON.stringify(screenText().slice(0, 60))}`);

  check('the poll timeout is no longer 8 s', source.includes('AbortSignal.timeout(12000)'));
  check('offline now needs three consecutive failures',
    source.includes('OFFLINE_AFTER_FAILURES = 3'));
  check('a failed poll retries immediately rather than at the slow interval',
    source.includes('OFFLINE_RETRY_MS'));

  console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
  process.exit(results.every(Boolean) ? 0 : 1);
})().catch(error => { console.error('ERROR', error.message); process.exit(1); });
