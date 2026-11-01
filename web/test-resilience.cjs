const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Deterministic control-flow regressions. No browser, backend or GPU is needed.
const toolbarSource = fs.readFileSync(path.join(__dirname, 'mode-control.js'), 'utf8');
const migrationSource = fs.readFileSync(path.join(__dirname, 'native/agent-defaults.js'), 'utf8');
const categoryKey = 'LlamaUi.disabledToolCategories';
const migrationKey = 'QwenLocal.agentDefaults.v2';
const draftKey = 'qwen.local.mode-draft.v1';
const quietConsole = { warn() {} };

function storage(seed = {}) {
  const values = new Map(Object.entries(seed));
  return { values, getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) };
}

function migrationTests() {
  const settings = storage({
    'QwenLocal.agentDefaults.v1': '1',
    [categoryKey]: JSON.stringify(['server', 'browser', 'mcp', 'custom']),
    'LlamaUi.disabledToolKeys': '["server:read_file"]',
    'LlamaUi.alwaysAllowedTools': '["browser:get_datetime"]',
    'LlamaUi.config': '{"temperature":0.42,"systemMessage":"Keep this."}',
  });
  const preserved = [...settings.values].filter(([key]) => key !== categoryKey);
  const apply = target => vm.runInNewContext(migrationSource, { localStorage: target, console: quietConsole });
  apply(settings);
  assert.deepEqual(JSON.parse(settings.getItem(categoryKey)), ['browser', 'mcp', 'custom']);
  assert.equal(settings.getItem(migrationKey), '1', 'v1 installations receive the newly requested agent default');
  for (const [key, value] of preserved) assert.equal(settings.getItem(key), value);
  settings.setItem(categoryKey, '["server"]');
  apply(settings);
  assert.equal(settings.getItem(categoryKey), '["server"]', 'a later deliberate preference is respected');

  const fresh = storage();
  apply(fresh);
  assert.equal(fresh.getItem(categoryKey), null, 'native fresh-profile defaults already enable server tools');
  assert.equal(fresh.getItem(migrationKey), '1');
  for (const invalid of ['{broken', '{}', 'null']) {
    const target = storage({ [categoryKey]: invalid });
    apply(target);
    assert.equal(target.getItem(categoryKey), invalid);
    assert.equal(target.getItem(migrationKey), null, 'failed migration can be retried after storage is repaired');
  }
  apply({ getItem() { throw new Error('Storage blocked'); } });
  const blocked = storage({ [categoryKey]: '["server"]' });
  blocked.setItem = () => { throw new Error('Quota exhausted'); };
  apply(blocked);
  assert.equal(blocked.getItem(migrationKey), null);

  const native = fs.readFileSync(path.join(__dirname, 'native/_app/immutable/bundle.H5ytpYYP.js'), 'utf8');
  assert.ok(native.includes('J1e={enabled:!0,maxTurns:100}'));
  assert.ok(native.includes('getEnabledToolsForLLM(d,h);if(p.length===0)return{handled:!1}'));
  const localized = fs.readFileSync(path.join(__dirname, 'native/_app/immutable/bundle.zh-CN.js'), 'utf8');
  for (const text of ['"aria-label":"停止生成"', '"aria-label":"更多授权选项"', '<span class=\\"sr-only\\">停止</span>']) {
    assert.ok(localized.includes(text), 'native activity indicator must match the shipped UI: ' + text);
  }
}

class Element {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.handlers = new Map();
    this.attributes = new Map();
    this.dataset = {};
    this.textContent = '';
    this.className = '';
    this.classList = { add() {}, toggle() {} };
    this.style = { setProperty() {} };
  }
  append(...children) { this.children.push(...children); }
  prepend(child) { this.children.unshift(child); }
  replaceChildren(...children) { this.children = [...children]; }
  setAttribute(name, value) { this.attributes.set(name, value); }
  getAttribute(name) { return this.attributes.get(name); }
  addEventListener(name, callback) {
    const list = this.handlers.get(name) || [];
    list.push(callback);
    this.handlers.set(name, list);
  }
  // Minimal descendant lookup for class, id and tag selectors. The image
  // history panel reads its own counter and rows through these.
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    const plain = selector.replace(/^[a-z]+/, '');
    const matches = node => {
      if (selector.startsWith('#') && node.id === selector.slice(1)) return true;
      if (selector.startsWith('.') && String(node.className).split(/\s+/).includes(plain.slice(1))) return true;
      if (!selector.startsWith('.') && !selector.startsWith('#') && !selector.startsWith('[') && node.tagName === selector) return true;
      if (selector.startsWith('[') && selector.endsWith(']')) {
        const [name, raw] = selector.slice(1, -1).split('=');
        const expected = raw === undefined ? undefined : raw.replace(/^"|"$/g, '');
        const value = name.startsWith('data-') ? node.dataset[name.slice(5).replace(/-([a-z])/g, (all, c) => c.toUpperCase())] : node.attributes.get(name);
        if (expected === undefined ? value !== undefined : String(value) === expected) return true;
      }
      return false;
    };
    const found = [];
    const walk = node => {
      for (const child of node.children || []) {
        if (!(child instanceof Element)) continue;
        if (matches(child)) found.push(child);
        walk(child);
      }
    };
    walk(this);
    return found;
  }
  async fire(name, event = {}) {
    for (const callback of this.handlers.get(name) || []) await callback(event);
  }
  getBoundingClientRect() { return { height: 60 }; }
  contains(node) { return this === node || this.children.includes(node); }
  closest() { return null; }
  matches(selector) { return selector === '.conversation-chat-form' && this.className === 'conversation-chat-form'; }
}

class TextArea extends Element {
  constructor() { super('textarea'); this.text = ''; this.selectionStart = 0; this.selectionEnd = 0; }
  get value() { return this.text; }
  set value(value) { this.text = value; }
  dispatchEvent() {}
  setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
  focus() {}
}

function ui(seed = {}) {
  const env = {
    state: { mode: 'text', state: 'ready', revision: 1, instance_id: 'first', active_requests: 0,
      mtp_enabled: true, agent_enabled: true },
    failStatus: false, attachments: false, nativeIndicator: false, nativeStop: false,
    input: new TextArea(), rich: null, requests: [], reloads: 0,
  };
  const elements = [];
  const timers = new Map();
  const intervals = new Map();
  let nextTimer = 0;
  const document = new Element('document');
  document.body = new Element('body');
  document.documentElement = new Element('html');
  document.createElement = tag => { const node = new Element(tag); elements.push(node); return node; };
  document.createElementNS = (namespaceURI, tag) => Object.assign(document.createElement(tag), { namespaceURI });
  document.getElementById = id => elements.find(node => node.id === id) || null;
  document.querySelector = selector => {
    if (selector === '.conversation-chat-form textarea') return env.input;
    if (selector.includes('[role="textbox"]')) return env.rich;
    if (selector.includes('button[aria-label="Remove"]')) return env.attachments ? new Element('button') : null;
    if (selector.includes('[aria-label="Stop generation"]')) return env.nativeIndicator ? new Element('button') : null;
    return null;
  };
  document.querySelectorAll = selector => {
    assert.equal(selector, '.conversation-chat-form button[type="button"] .sr-only');
    return env.nativeStop ? [{ textContent: '停止' }] : [];
  };
  const sessionStorage = storage(seed);
  const window = new Element('window');
  const context = {
    document, window, navigator: {}, Element, HTMLTextAreaElement: TextArea,
    Event: class { constructor(type) { this.type = type; } },
    sessionStorage, console: quietConsole, AbortSignal,
    ResizeObserver: class { observe() {} }, MutationObserver: class { observe() {} },
    location: { pathname: '/', search: '', hash: '#/chat/existing', reload() { env.reloads++; } },
    setTimeout(callback) { const id = ++nextTimer; timers.set(id, callback); return id; },
    clearTimeout: id => timers.delete(id),
    setInterval(callback) { const id = ++nextTimer; intervals.set(id, callback); return id; },
    clearInterval: id => intervals.delete(id),
    async fetch(url, options) {
      env.requests.push({ url, options });
      if (url === '/local/status') {
        if (env.pendingStatus) return env.pendingStatus;
        if (env.failStatus) throw new Error('Connection reset');
      } else if (url === '/local/recover') {
        if (env.pendingRecovery) return env.pendingRecovery;
      } else if (url === '/local/mode') {
        env.state = { ...env.state, mode: JSON.parse(options.body).mode, state: 'switching' };
      } else throw new Error('Unexpected request ' + url);
      return response(env.state);
    },
  };
  const response = value => ({ ok: true, json: async () => JSON.parse(JSON.stringify(value)) });
  vm.runInNewContext(toolbarSource, context);
  env.flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
  env.poll = async () => {
    await env.flush();
    assert.equal(timers.size, 1, 'only one status poll may be scheduled');
    const [id, callback] = timers.entries().next().value;
    timers.delete(id);
    await callback();
    await env.flush();
  };
  env.find = className => elements.find(element => element.className === className);
  env.button = mode => elements.find(element => element.dataset.mode === mode);
  env.document = document;
  env.window = window;
  env.sessionStorage = sessionStorage;
  env.intervals = intervals;
  env.response = response;
  return env;
}

async function toolbarTests() {
  for (const indicator of ['nativeIndicator', 'nativeStop']) {
    const env = ui();
    await env.flush();
    assert.equal(env.reloads, 0);
    env.input.value = '保留我未发送的内容';
    env.failStatus = true;
    await env.poll();
    assert.equal(env.find('local-mode-toolbar').dataset.state, 'offline');
    env[indicator] = true;
    env.failStatus = false;
    await env.poll();
    assert.equal(env.reloads, 0, 'reconnect cannot interrupt an agent approval / browser tool');
    assert.equal(env.button('vision').disabled, true);
    await env.button('vision').fire('click');
    assert.equal(env.requests.filter(request => request.url === '/local/mode').length, 0);
    env[indicator] = false;
    await env.poll();
    assert.equal(env.reloads, 1, 'completed agent session allows the recovery reload');
    assert.equal(JSON.parse(env.sessionStorage.getItem(draftKey)).text, '保留我未发送的内容');
  }

  const attached = ui();
  await attached.flush();
  attached.attachments = true;
  attached.state = { ...attached.state, instance_id: 'restarted' };
  await attached.poll();
  assert.equal(attached.reloads, 0, 'attachments survive a backend restart');
  attached.attachments = false;
  await attached.poll();
  assert.equal(attached.reloads, 1);

  const rich = ui();
  await rich.flush();
  rich.input = null;
  rich.rich = new Element('div');
  rich.rich.textContent = '```python\nprint("draft")\n```';
  await rich.button('vision').fire('click');
  assert.equal(rich.requests.filter(request => request.url === '/local/mode').length, 0);
  assert.ok(rich.find('local-mode-error').textContent.includes('输入草稿'));
  rich.failStatus = true;
  await rich.poll();
  let blocked = false;
  const enter = { key: 'Enter', target: rich.rich, preventDefault() { blocked = true; }, stopImmediatePropagation() {} };
  await rich.document.fire('keydown', enter);
  assert.equal(blocked, true, 'rich-editor Enter must be blocked while disconnected');
  blocked = false;
  await rich.document.fire('keydown', { ...enter, isComposing: true });
  assert.equal(blocked, false, 'Chinese IME confirmation must remain usable');
  rich.failStatus = false;
  await rich.poll();
  assert.equal(rich.reloads, 0, 'reconnection must not flatten or discard a rich draft');
  rich.rich.textContent = '';
  await rich.poll();
  assert.equal(rich.reloads, 1);

  const recovery = ui();
  await recovery.flush();
  recovery.state = { ...recovery.state, state: 'error', error: 'Backend exited' };
  await recovery.poll();
  let finishRecovery;
  recovery.pendingRecovery = new Promise(resolve => { finishRecovery = resolve; });
  await recovery.find('local-mode-retry').fire('click');
  await recovery.find('local-mode-retry').fire('click');
  assert.equal(recovery.requests.filter(request => request.url === '/local/recover').length, 1,
    'repeated retry clicks cannot start duplicate recoveries');
  assert.equal(recovery.find('local-mode-retry').disabled, true);
  recovery.state = { ...recovery.state, state: 'starting', error: null };
  finishRecovery(recovery.response(recovery.state));
  await recovery.flush();
  assert.equal(recovery.find('local-mode-toolbar').dataset.state, 'starting');

  const raced = ui();
  await raced.flush();
  raced.state = { ...raced.state, state: 'error', error: 'Backend exited' };
  await raced.poll();
  let finishOldPoll;
  raced.pendingStatus = new Promise(resolve => { finishOldPoll = resolve; });
  const oldPoll = raced.poll();
  await raced.flush();
  raced.state = { ...raced.state, state: 'starting', error: null };
  await raced.find('local-mode-retry').fire('click');
  await raced.flush();
  finishOldPoll(raced.response({ ...raced.state, state: 'ready', revision: 99 }));
  await oldPoll;
  assert.equal(raced.find('local-mode-toolbar').dataset.state, 'starting',
    'a status request from before recovery must not overwrite the recovery result');
  assert.equal(raced.reloads, 0);

  const restored = ui({ [draftKey]: JSON.stringify({ text: '旧草稿', path: '/#/chat/existing',
    time: Date.now(), start: 1, end: 2, focused: false }) });
  restored.sessionStorage.removeItem = () => { throw new Error('Storage became unavailable'); };
  for (const callback of restored.intervals.values()) callback();
  await restored.flush();
  assert.equal(restored.input.value, '旧草稿', 'storage cleanup failure does not break draft restore');
  const invalid = ui({ [draftKey]: '{"text":42,"time":"bad"}' });
  await invalid.flush();
  assert.equal(invalid.sessionStorage.getItem(draftKey), null);

  const waking = ui();
  await waking.flush();
  const before = waking.requests.length;
  await waking.window.fire('online');
  await waking.flush();
  await waking.window.fire('pageshow', { persisted: true });
  await waking.flush();
  assert.equal(waking.requests.length, before + 2, 'network and back-forward-cache restores trigger immediate health checks');
}

(async () => {
  migrationTests();
  await toolbarTests();
  console.log('PASS: agent defaults, permissions, agent-safe reconnect/switch, attachments, rich drafts, IME, retry deduplication, storage recovery, page resume');
})().catch(error => { console.error(error); process.exitCode = 1; });
