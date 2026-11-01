/* The injected module must survive evaluation in a plain DOM.
 *
 * This exists because it did not: a `const` declared below the object literal that
 * referenced it threw a temporal-dead-zone ReferenceError, and the whole module died
 * silently — the button and the fetch hook simply never appeared, with nothing in the
 * page's console but "Uncaught". Evaluating the served file here turns that class of
 * mistake into a one-second failure.
 *
 * Usage: node check_voice_module.cjs [path-to-module]
 */
const fs = require('node:fs');
const vm = require('node:vm');

const file = process.argv[2] || 'D:\\AI\\Qwen-Local\\web\\voice-input.js';
const source = fs.readFileSync(file, 'utf8');
const results = [];

function check(label, ok, detail = '') {
  results.push(Boolean(ok));
  console.log(`${ok ? 'PASS' : 'FAIL'} ${label.padEnd(52)} ${detail}`);
}

function makeElement(tag) {
  return {
    tagName: String(tag).toUpperCase(), children: [], style: { setProperty() {} },
    dataset: {}, handlers: {}, hidden: false, className: '', textContent: '', value: '',
    append(...kids) { this.children.push(...kids); },
    replaceChildren(...kids) { this.children = kids; },
    remove() {}, addEventListener() {}, removeEventListener() {},
    setAttribute() {}, getAttribute() { return null; },
    querySelector() { return makeElement('span'); }, querySelectorAll() { return []; },
    getBoundingClientRect() { return { top: 10, right: 100, width: 90, height: 30 }; },
    focus() {}, dispatchEvent() { return true; }, contains() { return false; },
    classList: { toggle() {}, add() {} },
  };
}

const documentStub = makeElement('document');
documentStub.body = makeElement('body');
documentStub.documentElement = makeElement('html');
documentStub.readyState = 'loading';
documentStub.createElement = makeElement;
documentStub.createElementNS = () => makeElement('svg');
documentStub.querySelector = () => null;
documentStub.getElementById = () => null;
documentStub.addEventListener = () => {};
documentStub.execCommand = () => true;
documentStub.activeElement = null;

const windowStub = makeElement('window');
windowStub.document = documentStub;
windowStub.location = { href: 'http://127.0.0.1:8081/' };
windowStub.navigator = { mediaDevices: {} };
windowStub.MediaRecorder = function MediaRecorder() {};
windowStub.innerWidth = 1200;
windowStub.addEventListener = () => {};
windowStub.getSelection = () => null;
windowStub.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });

const context = {
  window: windowStub, document: documentStub, navigator: windowStub.navigator,
  MediaRecorder: windowStub.MediaRecorder,
  Request: class Request {}, Response: class Response {}, Blob: class Blob {},
  Event: class Event {}, FocusEvent: class FocusEvent {}, MutationObserver: class { observe() {} },
  setTimeout, clearTimeout, setInterval, clearInterval, console, URL,
  requestAnimationFrame: callback => setTimeout(callback, 0),
  fetch: windowStub.fetch,
};
context.globalThis = context;

try {
  vm.runInNewContext(source, context, { filename: file });
  check('the module evaluates without throwing', true);
} catch (error) {
  check('the module evaluates without throwing', false, error.message);
}

const exposed = (context.window || context).QwenVoice;
check('it exposes its API to the host page', Boolean(exposed && exposed.preempt && exposed.offerGpu),
  exposed ? Object.keys(exposed).join(', ') : 'missing');
check('the GPU gate is exported for the chat wrapper',
  Boolean(exposed && typeof exposed.gpuGate === 'function' && exposed.diagnostics));
check('it leaves window.fetch alone',
  !/Object\.defineProperty\(root, 'fetch'/.test(source) && !/root\.fetch\s*=\s*wrapped/.test(source),
  'hijacking window.fetch is what blanked the panel');
check('it never builds HTML from strings',
  !/innerHTML\s*=/.test(source), 'textContent-only, same rule as the share panel');

console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
process.exit(results.every(Boolean) ? 0 : 1);
