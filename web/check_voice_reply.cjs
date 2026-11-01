/* Regression test for the reported voice-panel defects, driven through the real module.
 *
 * Two things were reported from the browser:
 *   1. the reply comes out as text, not as a voice bar, and there is no 转文字 control
 *   2. the answer appears twice
 *
 * Both come from the same place: askStreaming() built a voice bar and swapped it into
 * the log, and then renderTurns() cleared the log and rebuilt every turn as a plain
 * text bubble - destroying the bar and rendering the reply a second time. Nothing in
 * the existing harnesses covered the reply path, which is why it survived two passes.
 *
 * So this test asserts on the FINAL DOM after a typed turn: exactly one assistant row,
 * it holds a voice bar rather than text, and the transcript behind 转文字 is the reply.
 * It also samples the DOM mid-stream, because "the output is still text" is about what
 * is on screen while the reply is arriving.
 *
 * Usage: node web/check_voice_reply.cjs [path/to/voice-panel.js]
 */

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const file = process.argv[2] || path.join(__dirname, 'voice-panel.js');
const source = fs.readFileSync(file, 'utf8');

const results = [];
function check(label, ok, detail = '') {
  results.push(Boolean(ok));
  console.log(`${ok ? 'PASS' : 'FAIL'} ${label.padEnd(58)} ${detail}`);
}

const REPLY = '你好，我在。今天天气不错，我们出门走走吧。';

function makeElement(tag) {
  const node = {
    tagName: String(tag).toUpperCase(),
    children: [],
    handlers: {},
    style: { setProperty() {}, removeProperty() {} },
    dataset: {},
    attributes: {},
    classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
    hidden: false,
    disabled: false,
    className: '',
    value: '',
    type: '',
    placeholder: '',
    title: '',
    id: '',
    append(...kids) { for (const k of kids) node.children.push(k); },
    appendChild(kid) { node.children.push(kid); return kid; },
    prepend(...kids) { node.children.unshift(...kids); },
    replaceChildren(...kids) { node.children = kids; },
    removeChild(kid) {
      const i = node.children.indexOf(kid);
      if (i >= 0) node.children.splice(i, 1);
      return kid;
    },
    remove() {
      node.removed = true;
      for (const parent of parentsOf(node)) {
        const i = parent.children.indexOf(node);
        if (i >= 0) parent.children.splice(i, 1);
      }
    },
    addEventListener(kind, fn) { (node.handlers[kind] ||= []).push(fn); },
    removeEventListener() {},
    dispatchEvent() { return true; },
    setAttribute(name, value) { node.attributes[name] = String(value); },
    getAttribute(name) { return name in node.attributes ? node.attributes[name] : null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    getBoundingClientRect() { return { top: 0, left: 0, right: 100, bottom: 30, width: 100, height: 30 }; },
    focus() {},
    contains(kid) { return node.descendants().includes(kid); },
    click() {
      for (const fn of node.handlers.click || []) fn({ preventDefault() {}, stopPropagation() {} });
    },
    descendants(out = []) {
      for (const kid of node.children) {
        if (!kid || typeof kid !== 'object') continue;
        out.push(kid);
        if (typeof kid.descendants === 'function') kid.descendants(out);
      }
      return out;
    },
  };
  Object.defineProperty(node, 'parentElement', { get: () => parentsOf(node)[0] || null });
  // textContent has to behave like the real thing or renderTurns() cannot be measured:
  // reading it concatenates the subtree, and assigning "" clears the children. Treating
  // it as a plain field made every re-render append a second copy of the log instead of
  // replacing it.
  let ownText = '';
  Object.defineProperty(node, 'textContent', {
    get() {
      if (node.children.length) {
        return node.children.map(kid => (kid && kid.textContent) || '').join('');
      }
      return ownText;
    },
    set(value) {
      ownText = value == null ? '' : String(value);
      if (ownText === '') node.children = [];
    },
  });
  return node;
}

// remove() needs to know the parent; a WeakMap is filled in by append/prepend.
const parentMap = new WeakMap();
function parentsOf(node) {
  const parent = parentMap.get(node);
  return parent ? [parent] : [];
}
function link(parent, kids) {
  for (const kid of kids) if (kid && typeof kid === 'object') parentMap.set(kid, parent);
}
const rawAppend = makeElement('x').append;
function instrument(node) {
  node.append = (...kids) => { link(node, kids); for (const k of kids) node.children.push(k); };
  node.appendChild = kid => { link(node, [kid]); node.children.push(kid); return kid; };
  node.prepend = (...kids) => { link(node, kids); node.children.unshift(...kids); };
  node.replaceChildren = (...kids) => { link(node, kids); node.children = kids; };
  return node;
}
void rawAppend;

const documentStub = instrument(makeElement('document'));
documentStub.body = instrument(makeElement('body'));
documentStub.documentElement = makeElement('html');
documentStub.head = makeElement('head');
documentStub.createElement = tag => instrument(makeElement(tag));
documentStub.createElementNS = () => instrument(makeElement('svg'));
documentStub.addEventListener = () => {};
documentStub.activeElement = null;
documentStub.querySelector = () => null;
documentStub.querySelectorAll = () => [];

const toolbarRow = instrument(makeElement('div'));
toolbarRow.className = 'local-mode-row';
const toolbar = makeElement('header');
toolbar.id = 'local-mode-toolbar';
toolbar.querySelector = selector => (selector === '.local-mode-row' ? toolbarRow : null);
documentStub.getElementById = id => (id === 'local-mode-toolbar' ? toolbar : null);

// A real 3 s WAV: the panel reads the duration out of the header to decide how much
// audio to buffer before it starts playing, so a fake blob would stall the player.
function wav(seconds) {
  const byteRate = 48000;
  const dataBytes = byteRate * seconds;
  const header = Buffer.alloc(44);
  header.write('RIFF', 0);
  header.writeUInt32LE(36 + dataBytes, 4);
  header.write('WAVE', 8);
  header.write('fmt ', 12);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(1, 22);
  header.writeUInt32LE(24000, 24);
  header.writeUInt32LE(byteRate, 28);
  header.writeUInt16LE(2, 32);
  header.writeUInt16LE(16, 34);
  header.write('data', 36);
  header.writeUInt32LE(dataBytes, 40);
  return new Blob([header, Buffer.alloc(dataBytes)], { type: 'audio/wav' });
}

// The reply arrives as SSE: a few deltas, then stats, then done.
function sseStream(pieces) {
  const encoder = new TextEncoder();
  let index = 0;
  return new ReadableStream({
    pull(controller) {
      if (index < pieces.length) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(pieces[index++])}\n\n`));
        return;
      }
      if (index === pieces.length) {
        index += 1;
        controller.enqueue(encoder.encode('event: stats\ndata: {"seconds":1.2,"first_token_seconds":0.3}\n\n'));
        controller.enqueue(encoder.encode('event: done\ndata: {}\n\n'));
        return;
      }
      controller.close();
    },
  });
}

const calls = [];
const storage = new Map();
const windowStub = {
  document: documentStub,
  location: { href: 'http://127.0.0.1:8081/' },
  navigator: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [] }) } },
  localStorage: {
    getItem: key => (storage.has(key) ? storage.get(key) : null),
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: key => storage.delete(key),
  },
  MediaRecorder: function MediaRecorder() {},
  AudioContext: function AudioContext() {
    return { createMediaStreamSource: () => ({ connect() {} }), createAnalyser: () => ({
      fftSize: 1024, getByteTimeDomainData() {}, connect() {} }), close() {} };
  },
  // play() resolves and the element reports "ended" on the next tick, which is what
  // lets the playback loop move on to the following sentence.
  Audio: function Audio() {
    const handlers = {};
    return {
      play: async () => { setTimeout(() => (handlers.ended || []).forEach(fn => fn()), 0); },
      pause() {},
      addEventListener: (kind, fn) => { (handlers[kind] ||= []).push(fn); },
    };
  },
  innerWidth: 1400,
  addEventListener() {},
  dispatchEvent() { return true; },
  setTimeout, clearTimeout, setInterval, clearInterval,
  speechSynthesis: { speak() {}, cancel() {} },
  SpeechSynthesisUtterance: function SpeechSynthesisUtterance() {},
  ResizeObserver: class { observe() {} },
};
windowStub.window = windowStub;

const fetchStub = async (url, options = {}) => {
  const target = String(url);
  calls.push({ url: target, body: options.body });
  if (target.includes('/local/voice/mode')) {
    return jsonResponse({ brain_ready: true, asr_ready: true, profile: 'qwen3.5-9b-uncensored' });
  }
  if (target.includes('/local/voice/config')) {
    return jsonResponse({ locked: true, profile_label: '9B' });
  }
  if (target.includes('/local/voice/chat')) {
    return { ok: true, status: 200, body: sseStream(REPLY.match(/.{1,7}/gs)) };
  }
  if (target.includes('/local/voice/speak')) {
    return { ok: true, status: 200, blob: async () => wav(3) };
  }
  return jsonResponse({});
};

function jsonResponse(value) {
  return { ok: true, status: 200, json: async () => value, blob: async () => new Blob([]) };
}

windowStub.fetch = fetchStub;

const context = {
  window: windowStub,
  document: documentStub,
  navigator: windowStub.navigator,
  localStorage: windowStub.localStorage,
  MediaRecorder: windowStub.MediaRecorder,
  AudioContext: windowStub.AudioContext,
  Audio: windowStub.Audio,
  ResizeObserver: windowStub.ResizeObserver,
  setTimeout, clearTimeout, setInterval, clearInterval,
  console, Blob, FormData: class FormData {},
  Event: class Event {}, CustomEvent: class CustomEvent {}, AbortSignal,
  TextEncoder, TextDecoder, ReadableStream, Uint8Array, ArrayBuffer, Promise,
  // URL must be injected: playBlob() calls URL.createObjectURL, and without it here the
  // playback loop rejects, Promise.all rejects, and the turn lands in the error path
  // instead of ever reaching the rendering this test is about.
  URL,
  requestAnimationFrame: cb => setTimeout(cb, 0),
  fetch: fetchStub,
  speechSynthesis: windowStub.speechSynthesis,
  SpeechSynthesisUtterance: windowStub.SpeechSynthesisUtterance,
};
context.globalThis = context;

// The panel builds a replayable blob URL when it plays a sentence.
const created = [];
URL.createObjectURL = blob => { created.push(blob); return 'blob:stub/' + created.length; };
URL.revokeObjectURL = () => {};

try {
  vm.runInNewContext(source, context, { filename: file });
  check('the module evaluates', true);
} catch (error) {
  check('the module evaluates', false, error.message);
  console.log('\nFAILURES PRESENT');
  process.exit(1);
}

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

setTimeout(async () => {
  const launch = toolbarRow.descendants().find(n => n.className === 'local-voice-launch');
  check('the toolbar gained the voice button', Boolean(launch));
  if (!launch) return finish();
  launch.click();

  const panel = documentStub.body.descendants().find(n => n.id === 'local-voice-panel');
  const input = panel.descendants().find(n => n.className === 'local-voice-input');
  const composer = panel.descendants().find(n => n.className === 'local-voice-composer');
  check('the composer is present', Boolean(input && composer));
  if (!input || !composer) return finish();

  input.value = '你好';
  for (const fn of composer.handlers.submit || []) fn({ preventDefault() {} });

  // Sample what is on screen while the reply is still arriving: the complaint is that
  // the answer shows up as text, so this is the moment that matters.
  await wait(120);
  const log = panel.descendants().find(n => n.className === 'local-voice-log');
  const mid = log ? log.descendants() : [];
  const midBubbles = mid.filter(n => n.className === 'local-voice-bubble' && n.textContent.includes('你好，我在'));
  check('no reply text on screen mid-stream', midBubbles.length === 0,
        `bubbles=${midBubbles.length}`);

  // Let synthesis and playback finish, then inspect the settled DOM.
  await wait(2500);
  const nodes = log ? log.descendants() : [];
  const byClass = name => nodes.filter(n => n.className === name);
  const rows = nodes.filter(n => String(n.className).split(' ').includes('local-voice-turn'));
  console.log('   · rows: ' + rows.map(r => `${r.className} <${r.descendants().map(n => n.className).filter(Boolean).join(',')}>`).join(' | '));
  console.log('   · log children: ' + (log ? log.children.map(c => c.className || c.tagName).join(',') : 'none'));
  console.log('   · fetch calls: ' + calls.map(c => c.url.replace(/^.*\/local/, '/local') + (c.body ? ' ' + String(c.body).slice(0, 40) : '')).join(' ; '));
  const assistantRows = rows.filter(n => n.className.includes('is-assistant'));
  check('exactly one assistant turn', assistantRows.length === 1, `rows=${assistantRows.length}`);

  const bars = byClass('local-voice-bar');
  check('the reply is a voice bar', bars.length === 1, `bars=${bars.length}`);

  const textBubbles = byClass('local-voice-bubble')
    .filter(n => n.textContent.includes('今天天气不错'));
  check('the reply is not also a text bubble', textBubbles.length === 0,
        `text bubbles=${textBubbles.length}`);

  const transcripts = byClass('local-voice-transcript');
  check('the bar carries the transcript', transcripts.length === 1);
  if (transcripts.length) {
    check('the transcript is the whole reply',
          transcripts[0].textContent === REPLY,
          JSON.stringify(transcripts[0].textContent).slice(0, 60));
    check('the transcript starts hidden', transcripts[0].hidden === true);
  }

  const toggles = byClass('local-voice-bar-toggle');
  check('the 转文字 control exists', toggles.length === 1);
  if (toggles.length && transcripts.length) {
    const toggle = toggles[0];
    check('the control is labelled 转文字', toggle.textContent === '转文字');
    toggle.click();
    check('clicking it reveals the text', transcripts[0].hidden === false);
    check('the label flips to 收起文字', toggle.textContent === '收起文字');
    toggle.click();
    check('clicking again hides it', transcripts[0].hidden === true);
  }

  // One reply, rendered once. Counted over leaf nodes: every ancestor of the transcript
  // contains the text too, so counting all nodes would report nesting as duplication.
  const leaves = nodes.filter(n => n.children.length === 0 && String(n.textContent).includes('今天天气不错'));
  check('the reply is rendered in exactly one place', leaves.length === 1, `leaves=${leaves.length}`);

  // Input-kind routing: a typed turn must ask for the language model only. Asking for the
  // recogniser here is what made a typed message start a 1.2 GB speech model it can never
  // call, on the critical path.
  const modeCall = calls.find(c => c.url.includes('/local/voice/mode') && c.body);
  let need = null;
  try { need = JSON.parse(modeCall.body).need; } catch (failure) { /* reported below */ }
  check('a typed turn asks for text, not the recogniser', need === 'text', `need=${JSON.stringify(need)}`);

  const asrRow = panel.descendants().find(n => n.className === 'local-voice-fact-value'
    && /不需要|就绪|加载中/.test(String(n.textContent)));
  check('the 识别 row does not claim the recogniser is running', !asrRow || asrRow.textContent === '打字不需要',
        asrRow ? String(asrRow.textContent) : 'no row');

  finish();
}, 60);

function finish() {
  console.log(`\n${results.every(Boolean) ? 'ALL PASS' : 'FAILURES PRESENT'}  ${results.filter(Boolean).length}/${results.length}`);
  process.exit(results.every(Boolean) ? 0 : 1);
}
