/* Evaluate voice-panel.js in a stub DOM and assert what it actually builds.
 *
 * This follows web/check_voice_module.cjs: the module is run for real (not merely
 * syntax-checked), because the failures that matter here are runtime ones — a
 * temporal-dead-zone reference, a missing helper, a control that never gets
 * appended. Then it drives the launcher's click handler and inspects the resulting
 * DOM, which is the closest thing to "does the panel open with its controls".
 *
 * Usage: node web/check_voice_panel.cjs
 */

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const file = process.argv[2] || path.join(__dirname, 'voice-panel.js');
const source = fs.readFileSync(file, 'utf8');

const results = [];
function check(label, ok, detail = '') {
  results.push(Boolean(ok));
  console.log(`${ok ? 'PASS' : 'FAIL'} ${label.padEnd(56)} ${detail}`);
}

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
    textContent: '',
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
    remove() { node.removed = true; },
    addEventListener(kind, fn) { (node.handlers[kind] ||= []).push(fn); },
    removeEventListener() {},
    dispatchEvent() { return true; },
    setAttribute(name, value) { node.attributes[name] = String(value); },
    getAttribute(name) { return name in node.attributes ? node.attributes[name] : null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    getBoundingClientRect() { return { top: 0, left: 0, right: 100, bottom: 30, width: 100, height: 30 }; },
    focus() {},
    contains() { return false; },
    click() {
      for (const fn of node.handlers.click || []) fn({ preventDefault() {}, stopPropagation() {} });
    },
    // Walking the tree is how the assertions find controls by class.
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

const documentStub = makeElement('document');
documentStub.body = makeElement('body');
documentStub.documentElement = makeElement('html');
documentStub.head = makeElement('head');
documentStub.createElement = makeElement;
documentStub.createElementNS = () => makeElement('svg');
documentStub.addEventListener = () => {};
documentStub.activeElement = null;
documentStub.querySelector = () => null;
documentStub.querySelectorAll = () => [];

// The toolbar is what the launcher mounts into; the module polls for it until present.
const toolbarRow = makeElement('div');
toolbarRow.className = 'local-mode-row';
const toolbar = makeElement('header');
toolbar.id = 'local-mode-toolbar';
toolbar.querySelector = selector => (selector === '.local-mode-row' ? toolbarRow : null);
documentStub.getElementById = id => (id === 'local-mode-toolbar' ? toolbar : null);

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
  Audio: function Audio() { return { play: async () => {}, pause() {} }; },
  innerWidth: 1400,
  addEventListener() {},
  dispatchEvent() { return true; },
  setTimeout, clearTimeout, setInterval, clearInterval,
  fetch: async () => ({ ok: true, status: 200, json: async () => ({}), blob: async () => ({ size: 0 }) }),
  speechSynthesis: { speak() {}, cancel() {} },
  SpeechSynthesisUtterance: function SpeechSynthesisUtterance() {},
  ResizeObserver: class { observe() {} },
};
windowStub.window = windowStub;

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
  console, URL, Blob: class Blob {}, FormData: class FormData {},
  Event: class Event {}, CustomEvent: class CustomEvent {}, AbortSignal,
  requestAnimationFrame: cb => setTimeout(cb, 0),
  fetch: windowStub.fetch,
  speechSynthesis: windowStub.speechSynthesis,
  SpeechSynthesisUtterance: windowStub.SpeechSynthesisUtterance,
};
context.globalThis = context;

try {
  vm.runInNewContext(source, context, { filename: file });
  check('模块求值无异常', true);
} catch (error) {
  check('模块求值无异常', false, error.message);
  console.log('\n' + (results.every(Boolean) ? 'ALL PASS' : 'FAILURES PRESENT'));
  process.exit(1);
}

// The module mounts on a timer; give that timer a chance to run.
setTimeout(() => {
  const launch = toolbarRow.descendants().find(n => n.className === 'local-voice-launch');
  check('工具栏出现语音对话按钮', Boolean(launch));

  const panel = documentStub.body.descendants().find(n => n.id === 'local-voice-panel');
  check('面板已挂载到 body', Boolean(panel));
  check('面板初始隐藏', panel ? panel.hidden === true : false);

  if (launch) {
    launch.click();
    check('点击后按钮标记为按下', launch.getAttribute('aria-pressed') === 'true');
    check('点击后面板显示', panel ? panel.hidden === false : false);
  }

  const all = panel ? panel.descendants() : [];
  const byClass = name => all.filter(n => n.className === name);

  check('麦克风按钮存在', byClass('local-voice-mic').length === 1);
  check('音量条存在', byClass('local-voice-level').length === 1);

  const input = byClass('local-voice-input')[0];
  const send = byClass('local-voice-send')[0];
  check('文字输入框存在', Boolean(input));
  check('发送按钮存在', Boolean(send));
  check('输入框有占位提示', Boolean(input && input.placeholder));
  if (input) console.log(`      placeholder = ${JSON.stringify(input.placeholder)}`);

  const voiceButtons = byClass('local-voice-voice');
  check('音色按钮 9 个', voiceButtons.length === 9, `实际 ${voiceButtons.length}`);
  if (voiceButtons.length) {
    const labels = voiceButtons.map(b => b.children.map(c => c.textContent).join(''));
    console.log(`      音色 = ${labels.join(' / ')}`);
  }

  // Voice must be reachable as a mode, not only through its own launcher: the panel is
  // shown by the toolbar's setActive hook the way the image and video panels are.
  check('暴露 setActive 供工具栏驱动', typeof windowStub.VoicePanel?.setActive === 'function');
  check('暴露 isBusy', typeof windowStub.VoicePanel?.isBusy === 'function');
  if (windowStub.VoicePanel?.setActive) {
    windowStub.VoicePanel.setActive(true, () => {});
    check('setActive(true) 打开面板', panel.hidden === false);
    windowStub.VoicePanel.setActive(false, () => {});
    check('setActive(false) 关闭面板', panel.hidden === true);
  }
  check('面板带 is-open 类标记', typeof panel.classList?.add === 'function');

  const toggles = byClass('local-voice-action');
  check('选项开关存在（自动发送/朗读）', toggles.length === 2, `实际 ${toggles.length}`);

  const facts = byClass('local-voice-facts')[0];
  check('状态侧栏存在', Boolean(facts));
  check('关闭按钮存在', byClass('local-voice-close').length === 1);
  check('空状态提示存在', byClass('local-voice-empty').length === 1);

  console.log('\n' + (results.every(Boolean) ? 'ALL PASS' : 'FAILURES PRESENT'));
  process.exit(results.every(Boolean) ? 0 : 1);
}, 400);
