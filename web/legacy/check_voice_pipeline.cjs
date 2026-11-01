/* Check that synthesis and playback really overlap.
 *
 * The reported symptom was a stutter at every sentence boundary, which comes from
 * rendering a sentence only after the previous one finished playing. This drives the
 * module with a fake TTS endpoint whose latency is recorded, then asserts that a
 * second render starts before the first has been played.
 *
 * Usage: node web/check_voice_pipeline.cjs
 */

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, 'voice-panel.js'), 'utf8');

const events = [];

function makeElement(tag) {
  const node = {
    tagName: String(tag).toUpperCase(), children: [], handlers: {}, dataset: {}, attributes: {},
    style: { setProperty() {}, removeProperty() {} },
    classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
    hidden: false, disabled: false, className: '', textContent: '', value: '',
    append(...kids) { node.children.push(...kids); },
    appendChild(k) { node.children.push(k); return k; },
    prepend(...kids) { node.children.unshift(...kids); },
    replaceChildren(...kids) { node.children = kids; },
    removeChild(k) { const i = node.children.indexOf(k); if (i >= 0) node.children.splice(i, 1); return k; },
    remove() {}, addEventListener(k, fn) { (node.handlers[k] ||= []).push(fn); },
    removeEventListener() {}, dispatchEvent() { return true; },
    setAttribute(n, v) { node.attributes[n] = String(v); },
    getAttribute(n) { return n in node.attributes ? node.attributes[n] : null; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    getBoundingClientRect() { return { top: 0, left: 0, right: 100, bottom: 30, width: 100, height: 30 }; },
    focus() {}, contains() { return false; },
    click() { for (const fn of node.handlers.click || []) fn({ preventDefault() {}, stopPropagation() {} }); },
    descendants(out = []) {
      for (const kid of node.children) {
        if (!kid || typeof kid !== 'object') continue;
        out.push(kid);
        if (typeof kid.descendants === 'function') kid.descendants(out);
      }
      return out;
    },
  };
  return node;
}

// --- audio stub: records when each clip starts and ends -----------------------
const audioLog = [];
class FakeAudio {
  constructor(url) { this.url = url; this.started = 0; }
  addEventListener(kind, fn) { (this.handlers ||= {})[kind] = fn; }
  play() {
    this.started = Date.now();
    events.push({ kind: 'play-start', at: this.started });
    // Each clip lasts 400 ms of wall clock in this harness.
    setTimeout(() => {
      events.push({ kind: 'play-end', at: Date.now() });
      if (this.handlers && this.handlers.ended) this.handlers.ended();
    }, 400);
    return Promise.resolve();
  }
  pause() {}
}

const documentStub = makeElement('document');
documentStub.body = makeElement('body');
documentStub.documentElement = makeElement('html');
documentStub.createElement = makeElement;
documentStub.createElementNS = () => makeElement('svg');
documentStub.addEventListener = () => {};
documentStub.activeElement = null;
documentStub.querySelector = () => null;
documentStub.querySelectorAll = () => [];
const toolbarRow = makeElement('div');
toolbarRow.className = 'local-mode-row';
const toolbar = makeElement('header');
toolbar.id = 'local-mode-toolbar';
toolbar.querySelector = sel => (sel === '.local-mode-row' ? toolbarRow : null);
documentStub.getElementById = id => (id === 'local-mode-toolbar' ? toolbar : null);

const storage = new Map();

// --- fake gateway: replies with a 3-sentence SSE stream ----------------------
function sseStream(sentences) {
  const encoder = new TextEncoder();
  return new ReadableStream({
    async start(controller) {
      for (const sentence of sentences) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(sentence)}\n\n`));
        await new Promise(r => setTimeout(r, 20));
      }
      controller.enqueue(encoder.encode('event: stats\ndata: {"first_token_seconds":0.3,"seconds":0.9}\n\n'));
      controller.enqueue(encoder.encode('event: done\ndata: {}\n\n'));
      controller.close();
    },
  });
}

let renderCount = 0;

const windowStub = {
  document: documentStub,
  location: { href: 'http://127.0.0.1:8081/' },
  navigator: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [] }) } },
  localStorage: {
    getItem: k => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
    removeItem: k => storage.delete(k),
  },
  MediaRecorder: function MediaRecorder() {},
  AudioContext: function AudioContext() {
    return { createMediaStreamSource: () => ({ connect() {} }),
             createAnalyser: () => ({ fftSize: 1024, getByteTimeDomainData() {}, connect() {} }),
             close() {} };
  },
  Audio: FakeAudio,
  innerWidth: 1400,
  addEventListener() {}, dispatchEvent() { return true; },
  setTimeout, clearTimeout, setInterval, clearInterval,
  speechSynthesis: { speak() {}, cancel() {} },
  SpeechSynthesisUtterance: function SpeechSynthesisUtterance() {},
  ResizeObserver: class { observe() {} },
  fetch: async (url, options) => {
    if (String(url).includes('/local/voice/chat')) {
      return { ok: true, status: 200, body: sseStream(['第一句话。', '第二句话。', '第三句话。']) };
    }
    if (String(url).includes('/local/voice/speak')) {
      // 600 ms per render: longer than the 400 ms clip, which is the real ratio and
      // exactly the case that stutters when rendering waits for playback.
      renderCount += 1;
      events.push({ kind: 'render-start', at: Date.now() });
      await new Promise(r => setTimeout(r, 600));
      events.push({ kind: 'render-end', at: Date.now() });
      return { ok: true, status: 200, blob: async () => ({ size: 5000 }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  },
};
windowStub.window = windowStub;

const context = {
  window: windowStub, document: documentStub, navigator: windowStub.navigator,
  localStorage: windowStub.localStorage, MediaRecorder: windowStub.MediaRecorder,
  AudioContext: windowStub.AudioContext, Audio: FakeAudio,
  ResizeObserver: windowStub.ResizeObserver, setTimeout, clearTimeout, setInterval, clearInterval,
  console, URL: { createObjectURL: () => 'blob:x', revokeObjectURL() {} },
  Blob: class Blob {}, FormData: class FormData {}, Event: class Event {},
  CustomEvent: class CustomEvent {}, AbortSignal, TextDecoder, ReadableStream,
  requestAnimationFrame: cb => setTimeout(cb, 0), fetch: windowStub.fetch,
  speechSynthesis: windowStub.speechSynthesis,
  SpeechSynthesisUtterance: windowStub.SpeechSynthesisUtterance,
};
context.globalThis = context;

// Surface anything the module throws while mounting, instead of only seeing the
// downstream "VoicePanel missing" symptom.
context.console = {
  log: (...args) => console.log('[module]', ...args),
  warn: (...args) => console.log('[module warn]', ...args),
  error: (...args) => console.log('[module error]', ...args),
};
process.on('uncaughtException', error => {
  console.log('[uncaught]', error && error.message);
  console.log((error && error.stack || '').split('\n').slice(0, 4).join('\n'));
});

vm.runInNewContext(source, context, { filename: 'voice-panel.js' });

setTimeout(async () => {
  const launch = toolbarRow.descendants().find(n => n.className === 'local-voice-launch');
  if (!launch) { console.log('FAIL 找不到启动按钮'); process.exit(1); }
  launch.click();

  // mount() runs on a timer of its own, so wait for the handle rather than assuming
  // it exists on the next tick.
  for (let i = 0; i < 40 && !context.VoicePanel; i += 1) {
    await new Promise(r => setTimeout(r, 50));
  }
  if (!context.VoicePanel) { console.log('FAIL VoicePanel 未挂载'); process.exit(1); }

  await context.VoicePanel.ask('测试并行');

  const renderStarts = events.filter(e => e.kind === 'render-start').map(e => e.at);
  const playStarts = events.filter(e => e.kind === 'play-start').map(e => e.at);
  const playEnds = events.filter(e => e.kind === 'play-end').map(e => e.at);

  const results = [];
  const check = (label, ok, detail = '') => {
    results.push(ok);
    console.log(`${ok ? 'PASS' : 'FAIL'} ${label.padEnd(46)} ${detail}`);
  };

  check('三轮渲染都发生', renderStarts.length === 3, `实际 ${renderStarts.length}`);
  check('三段音频都播放', playStarts.length === 3, `实际 ${playStarts.length}`);

  // The whole point: the second render must begin before the first clip finishes.
  if (renderStarts.length >= 2 && playEnds.length >= 1) {
    const overlap = renderStarts[1] < playEnds[0];
    check('第二句在第一句播完前已开始合成', overlap,
          `render#2 @${renderStarts[1] - renderStarts[0]}ms, play#1 end @${playEnds[0] - renderStarts[0]}ms`);
  } else {
    check('第二句在第一句播完前已开始合成', false, '样本不足');
  }

  const gaps = [];
  for (let i = 1; i < playStarts.length; i += 1) gaps.push(playStarts[i] - playEnds[i - 1]);
  const worst = gaps.length ? Math.max(...gaps) : 0;
  check('相邻音频间隙 < 150ms（无卡顿）', gaps.length === 0 || worst < 150, `最大间隙 ${worst}ms`);

  console.log('\n' + (results.every(Boolean) ? 'ALL PASS' : 'FAILURES PRESENT'));
  process.exit(results.every(Boolean) ? 0 : 1);
}, 600);
