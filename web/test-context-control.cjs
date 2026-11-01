const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, 'context-control.js'), 'utf8');

function fixture(saved) {
  const env = { requests: [], timers: new Map(), storage: new Map(saved === undefined ? [] : [['qwen.local.context-auto.v1', saved]]), status: { state: 'unchanged', message: '尚未达到阈值', updated_at: Date.now() / 1000 + 10 }, mutations: [] };
  let timerId = 0;
  class Element {
    constructor(tag) { this.tagName = tag; this.children = []; this.handlers = new Map(); this.attributes = new Map(); this.dataset = {}; this.className = ''; this.text = ''; }
    set innerHTML(_) { throw new Error('UI must render text, never HTML'); }
    set textContent(value) { this.text = String(value); this.children = []; }
    get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
    get isConnected() { return this === document || !!this.parentElement?.isConnected; }
    append(...children) { for (const child of children) { child.remove(); this.children.push(child); child.parentElement = this; } }
    insertBefore(child, other) { child.remove(); this.children.splice(this.children.indexOf(other), 0, child); child.parentElement = this; }
    remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this); this.parentElement = null; }
    setAttribute(key, value) { this.attributes.set(key, String(value)); }
    getAttribute(key) { return this.attributes.get(key); }
    addEventListener(type, callback) { const list = this.handlers.get(type) || []; list.push(callback); this.handlers.set(type, list); }
    async fire(type, event = {}) { for (const callback of this.handlers.get(type) || []) await callback(event); }
    all() { return this.children.flatMap(child => [child, ...child.all()]); }
    querySelectorAll(selector) { return this.all().filter(child => selector.startsWith('.') ? child.className.split(' ').includes(selector.slice(1)) : child.tagName === selector); }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    showModal() { this.open = true; } close() { this.open = false; void this.fire('close'); } focus() { document.activeElement = this; }
  }
  const document = new Element('document'); env.document = document;
  document.body = new Element('body'); document.append(document.body);
  document.createElement = tag => new Element(tag);
  document.createElementNS = (namespaceURI, tag) => Object.assign(new Element(tag), { namespaceURI });
  env.sidebar = new Element('aside'); document.body.append(env.sidebar);
  env.usage = new Element('div'); env.usage.className = 'qu-sidebar-footer'; env.sidebar.append(env.usage);
  const query = document.querySelector.bind(document);
  document.querySelector = selector => selector.startsWith('body > div') ? env.sidebar : query(selector);
  document.getElementById = id => document.all().find(child => child.id === id) || null;
  const location = { href: 'http://127.0.0.1:8081/#/chat/chat-a', origin: 'http://127.0.0.1:8081', hash: '#/chat/chat-a', reload() { throw new Error('Must never reload the native chat'); } };
  const window = new Element('window'); env.window = window; env.location = location;
  let native;
  native = async function(input, init) {
    const url = input instanceof Request ? input.url : String(input);
    env.requests.push({ input, init, url, receiver: this });
    if (url === '/local/context/config') {
      if (env.configFail) throw new Error('Offline');
      return new Response(JSON.stringify({ enabled: true, context_size: 49152, trigger_ratio: .75, reserve_tokens: 4096 }), { headers: { 'content-type': 'application/json' } });
    }
    if (url.startsWith('/local/context/status')) {
      if (env.statusFail) throw new Error('Status unavailable');
      return new Response(JSON.stringify(env.status), { headers: { 'content-type': 'application/json' } });
    }
    if (env.hold) return new Promise((resolve, reject) => {
      env.release = resolve;
      const signal = init?.signal || (input instanceof Request ? input.signal : null);
      signal?.addEventListener('abort', () => reject(Object.assign(new Error('Aborted'), { name: 'AbortError' })), { once: true });
    });
    if (env.fail) throw new Error('Connection refused');
    const response = new Response('data: preserved bytes\n\ndata: [DONE]\n\n', { headers: env.responseHeaders || { 'content-type': 'text/event-stream' } });
    env.lastResponse = response;
    response.clone = () => { throw new Error('Never clone or consume the native response stream'); };
    return response;
  };
  window.fetch = native;
  const localStorage = { getItem: key => env.storage.get(key) ?? null, setItem: (key, value) => env.storage.set(key, value) };
  const context = { window, document, location, localStorage, Headers, Request, Response, URL, AbortController, TextDecoder, Date, console,
    setTimeout(callback, delay) { const id = ++timerId; env.timers.set(id, { callback, delay }); return id; },
    clearTimeout: id => env.timers.delete(id), requestAnimationFrame: callback => callback(),
    MutationObserver: class { constructor(callback) { env.mutations.push(callback); } observe() {} },
  };
  env.run = () => vm.runInNewContext(source, context);
  env.run(); env.flush = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };
  env.find = name => document.querySelector('.' + name);
  env.post = (id = 'chat-a::Qwen', extra = {}, initExtra = {}) => window.fetch.call(window, './v1/chat/completions', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Conversation-Id': id }, body: JSON.stringify({ messages: [{ role: 'user', content: '原文应保持完整' }], stream: true, ...extra }), ...initExtra });
  env.lastChat = () => [...env.requests].reverse().find(request => request.url.includes('chat/completions'));
  env.poll = async () => { const item = [...env.timers].find(([, value]) => value.delay === 1000); assert.ok(item, 'active requests poll context status'); env.timers.delete(item[0]); await item[1].callback(); await env.flush(); };
  env.Element = Element;
  return env;
}

(async () => {
  const env = fixture(); await env.flush();
  assert.equal(env.sidebar.children[0].className, 'qc-sidebar-footer');
  assert.equal(env.sidebar.children[1], env.usage, 'both independent sidebar entries coexist');
  assert.match(env.find('qc-capacity').textContent, /48K.*36K.*4,096/);
  assert.equal(env.find('qc-toggle').querySelector('input').checked, true);
  const wrapped = env.window.fetch; env.run();
  assert.equal(env.window.fetch, wrapped); assert.equal(env.mutations.length, 1, 'duplicate script cannot leak observers or wrap fetch twice');

  const controller = new AbortController();
  const headers = new Headers({ 'Content-Type': 'application/json', 'X-Conversation-Id': 'chat-a::Qwen', Authorization: 'placeholder-test-only' });
  const body = JSON.stringify({ messages: [{ role: 'user', content: '不要更改原文' }], tools: [{ type: 'function' }], stream: true });
  const init = { method: 'POST', body, headers, signal: controller.signal, cache: 'no-store' };
  const response = await env.window.fetch.call(env.window, './v1/chat/completions', init);
  assert.equal(response, env.lastResponse, 'returns the exact native response, with its unread stream');
  assert.equal(response.bodyUsed, false);
  assert.equal(env.lastChat().init.body, body); assert.equal(env.lastChat().init.signal, controller.signal);
  assert.equal(env.lastChat().init.cache, 'no-store'); assert.equal(env.lastChat().receiver, env.window);
  assert.equal(env.lastChat().init.headers.get('X-Qwen-Context-Compression'), 'auto');
  assert.equal(headers.has('X-Qwen-Context-Compression'), false, 'does not mutate caller Headers');
  assert.equal(env.lastChat().init.headers.get('Authorization'), 'placeholder-test-only');
  await env.flush();
  assert.match(env.requests.find(request => request.url.startsWith('/local/context/status')).url, /chat-a%3A%3AQwen/);

  for (const extra of [{ n_predict: 0 }, { max_tokens: 0 }, { max_completion_tokens: 0 }]) {
    await env.post('chat-a::Qwen', extra); assert.equal(env.lastChat().init.headers['X-Qwen-Context-Compression'], undefined);
  }
  const titleOptions = { method: 'POST', body, headers: { 'Content-Type': 'application/json' } };
  await env.window.fetch('./v1/chat/completions', titleOptions); assert.equal(env.lastChat().init, titleOptions);
  const remoteOptions = { ...init, headers };
  await env.window.fetch('https://remote.invalid/v1/chat/completions', remoteOptions); assert.equal(env.lastChat().init, remoteOptions);
  assert.equal(headers.has('X-Qwen-Context-Compression'), false, 'cross-origin request untouched');
  const controllerOptions = { ...init };
  await env.window.fetch('./v1/chat/completions/control', controllerOptions); assert.equal(env.lastChat().init, controllerOptions);

  const toggle = env.find('qc-toggle').querySelector('input');
  toggle.checked = false; await toggle.fire('change'); await env.post(); await env.flush();
  assert.equal(env.lastChat().init.headers.get('X-Qwen-Context-Compression'), 'off');
  assert.equal(env.storage.get('qwen.local.context-auto.v1'), 'false');
  await env.find('qc-manual').fire('click'); assert.match(env.find('qc-feedback').textContent, /发送下一条消息/);
  env.fail = true; await assert.rejects(env.post(), /Connection refused/); env.fail = false; await env.flush();
  assert.equal(env.find('qc-manual').getAttribute('aria-pressed'), 'true', 'failed request preserves force plan');
  env.responseHeaders = { 'X-Qwen-Context-State': 'compressed', 'X-Qwen-Context-Original-Tokens': '39000', 'X-Qwen-Context-Effective-Tokens': '15000', 'X-Qwen-Context-Saved-Tokens': '24000', 'X-Qwen-Context-Cache-Hit': 'true' };
  env.status = { state: 'compressed', message: '<img src=x onerror=1>', original_tokens: 39000, prompt_tokens: 15000, saved_tokens: 24000, cache_hit: true, updated_at: Date.now() / 1000 + 10 };
  await env.post(); await env.flush();
  assert.equal(env.lastChat().init.headers.get('X-Qwen-Context-Compression'), 'force');
  assert.equal(env.find('qc-manual').getAttribute('aria-pressed'), 'false', 'successful compression consumes one-shot plan');
  assert.equal(env.find('qc-before').textContent, '39,000'); assert.equal(env.find('qc-after').textContent, '15,000');
  assert.match(env.find('qc-saving').textContent, /24,000.*复用/);
  assert.equal(env.find('qc-status-message').textContent, '<img src=x onerror=1>'); assert.equal(env.document.querySelectorAll('img').length, 0);
  await env.post(); await env.flush(); assert.equal(env.lastChat().init.headers.get('X-Qwen-Context-Compression'), 'off');

  const nativeRequest = new Request('http://127.0.0.1:8081/chat/completions', { method: 'POST', headers, body, signal: controller.signal });
  const requestResponse = await env.window.fetch.call(env.window, nativeRequest); await env.flush();
  assert.equal(env.lastChat().input, nativeRequest); assert.equal(nativeRequest.bodyUsed, false);
  assert.equal(env.lastChat().init.headers.get('X-Qwen-Context-Compression'), 'off'); assert.equal(requestResponse.bodyUsed, false);
  const zeroRequest = new Request('http://127.0.0.1:8081/chat/completions', { method: 'POST', headers, body: JSON.stringify({ messages: [], n_predict: 0 }) });
  await env.window.fetch(zeroRequest); assert.equal(env.lastChat().init, undefined); assert.equal(zeroRequest.bodyUsed, false);

  env.location.hash = '#/chat/chat-b'; await env.window.fire('hashchange');
  assert.equal(env.find('qc-status-label').textContent, '等待消息', 'switching conversations cannot display another conversation state');
  env.location.hash = '#/'; await env.window.fire('hashchange'); await env.find('qc-manual').fire('click');
  await env.post('chat-background::Qwen'); await env.flush();
  assert.equal(env.lastChat().init.headers.get('X-Qwen-Context-Compression'), 'off', 'new-chat plan cannot be stolen by background agent');
  env.location.hash = '#/chat/chat-new'; await env.window.fire('hashchange');
  await env.post('chat-new::Qwen'); await env.flush(); assert.equal(env.lastChat().init.headers.get('X-Qwen-Context-Compression'), 'force');

  env.location.hash = '#/chat/chat-new'; env.hold = true;
  const pendingController = new AbortController();
  const pending = env.post('chat-new::Qwen', {}, { signal: pendingController.signal }); await env.flush();
  env.status = { state: 'compressing', message: '正在整理较早的对话', updated_at: Date.now() / 1000 + 10 };
  await env.poll(); assert.equal(env.find('qc-status-label').textContent, '正在压缩');
  pendingController.abort(); await assert.rejects(pending, { name: 'AbortError' }); await env.flush();
  assert.equal([...env.timers.values()].filter(timer => timer.delay === 1000).length, 0, 'abort stops all request polling');
  env.hold = false; env.statusFail = true;
  const uninterrupted = await env.post('chat-new::Qwen'); await env.flush(); assert.equal(uninterrupted.bodyUsed, false, 'status outage never breaks chat');

  await env.find('qc-sidebar-button').fire('click'); assert.equal(env.document.getElementById('qwen-context-dialog').open, true);
  await env.find('qc-close').fire('click'); assert.equal(env.document.getElementById('qwen-context-dialog').open, false);
  const oldSidebar = env.sidebar; oldSidebar.remove(); env.sidebar = new env.Element('aside'); env.document.body.append(env.sidebar);
  env.mutations[0](); assert.equal(env.sidebar.children[0].className, 'qc-sidebar-footer', 'native sidebar recreation preserves entry');
  const persisted = fixture('false'); await persisted.flush(); assert.equal(persisted.find('qc-toggle').querySelector('input').checked, false);
  const concurrent = fixture('false'); await concurrent.flush(); await concurrent.find('qc-manual').fire('click');
  concurrent.hold = true;
  const first = concurrent.post(); await concurrent.flush(); const resolveFirst = concurrent.release;
  assert.equal(concurrent.lastChat().init.headers.get('X-Qwen-Context-Compression'), 'force');
  const second = concurrent.post(); await concurrent.flush(); const resolveSecond = concurrent.release;
  assert.equal(concurrent.lastChat().init.headers.get('X-Qwen-Context-Compression'), 'off', 'a concurrent request cannot claim the same one-shot force');
  resolveFirst(new Response('data: [DONE]\n\n', { headers: { 'X-Qwen-Context-State': 'compressed' } })); await first; await concurrent.flush();
  resolveSecond(new Response('data: [DONE]\n\n')); await second; await concurrent.flush();
  assert.equal(concurrent.find('qc-manual').getAttribute('aria-pressed'), 'false', 'successful original force still consumes plan after another request supersedes its display');
  const stale = fixture(); await stale.flush(); await stale.find('qc-manual').fire('click');
  stale.status = { state: 'compressed', message: '上轮结果', original_tokens: 10000, prompt_tokens: 1000, updated_at: 1 };
  await stale.post(); await stale.flush();
  assert.equal(stale.find('qc-manual').getAttribute('aria-pressed'), 'true', 'old status cannot consume the current force plan');
  assert.equal(stale.find('qc-status-message').textContent, '将在本轮整理较早对话。');
  const cached = fixture(); await cached.flush(); await cached.find('qc-manual').fire('click');
  cached.responseHeaders = { 'X-Qwen-Context-State': 'cached', 'X-Qwen-Context-Original-Tokens': '42000', 'X-Qwen-Context-Effective-Tokens': '12000', 'X-Qwen-Context-Saved-Tokens': '30000', 'X-Qwen-Context-Cache-Hit': 'true' };
  cached.status = { state: 'cached', message: '复用已经验证的摘要', original_tokens: 42000, prompt_tokens: 12000, saved_tokens: 30000, cache_hit: true, updated_at: Date.now() / 1000 + 10 };
  await cached.post(); await cached.flush();
  assert.equal(cached.find('qc-status-label').textContent, '复用已有摘要');
  assert.equal(cached.find('qc-manual').getAttribute('aria-pressed'), 'false', 'verified cached compression consumes one-shot force');
  assert.equal(cached.find('qc-after').textContent, '12,000');
  for (const [state, label] of Object.entries({ counting: '计算上下文长度', fallback: '使用原始上下文', disabled: '压缩已关闭', cancelled: '压缩已取消' })) {
    cached.responseHeaders = { 'X-Qwen-Context-State': state };
    cached.status = { state, message: '测试状态', updated_at: Date.now() / 1000 + 10 };
    await cached.post(); await cached.flush(); assert.equal(cached.find('qc-status-label').textContent, label);
  }
  console.log('PASS: context controls, exact payload/response/AbortSignal preservation, Request/Headers support, origin isolation, zero-token/title exclusion, single-use force/retry, per-conversation state, polling/cancel, XSS protection, sidebar coexistence, duplicate-load guard');
})().catch(error => { console.error(error); process.exitCode = 1; });
