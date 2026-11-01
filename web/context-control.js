(() => {
  'use strict';
  if (window.__qwenContextControl) return;
  window.__qwenContextControl = { version: 1 };
  const originalFetch = window.fetch;
  const storageKey = 'qwen.local.context-auto.v1';
  const states = new Map();
  const actualIds = new Map();
  const armed = new Map();
  const active = new Map();
  const labels = { idle: '等待消息', checking: '检查上下文', counting: '计算上下文长度', compressing: '正在压缩', compressed: '已压缩', cached: '复用已有摘要', unchanged: '无需压缩', skipped: '已跳过', fallback: '使用原始上下文', disabled: '压缩已关闭', cancelled: '压缩已取消', error: '压缩未完成' };
  let enabled = true;
  let config = null;
  let configError = '';
  let hint = '';
  let sidebar = null;
  let sequence = 0;
  let lastFocus;
  try { enabled = localStorage.getItem(storageKey) !== 'false'; } catch { /* A session-only switch still works. */ }

  function element(tag, className, text) {
    const value = document.createElement(tag);
    if (className) value.className = className;
    if (text !== undefined) value.textContent = text;
    return value;
  }
  function icon() {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    for (const [key, value] of Object.entries({ viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.7', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' })) svg.setAttribute(key, value);
    for (const d of ['M4 4h16v16H4z', 'M8 8l4 4 4-4', 'M8 16l4-4 4 4']) {
      const path = document.createElementNS(svg.namespaceURI, 'path'); path.setAttribute('d', d); svg.append(path);
    }
    return svg;
  }
  function baseId(value) { return String(value || '').split('::')[0]; }
  function currentChat() {
    const match = /^#\/chat\/([^/?#]+)/.exec(location.hash || '');
    if (!match) return '';
    try { return decodeURIComponent(match[1]); } catch { return ''; }
  }
  function currentId() { return actualIds.get(currentChat()) || ''; }
  function planKey() { return currentChat() || '__next__'; }
  function numeric(value) { return typeof value === 'number' && Number.isFinite(value) && value >= 0; }
  function number(value) { return numeric(value) ? value.toLocaleString('zh-CN', { maximumFractionDigits: 0 }) : '—'; }
  function compact(value) { return numeric(value) ? `${(value / 1024).toLocaleString('zh-CN', { maximumFractionDigits: 1 })}K` : '—'; }
  function setState(id, value) {
    if (!value || !Object.hasOwn(labels, value.state)) return;
    states.delete(id); states.set(id, value);
    while (states.size > 32) states.delete(states.keys().next().value);
    render();
  }

  const footer = element('div', 'qc-sidebar-footer');
  const entry = element('button', 'qc-sidebar-button'); entry.type = 'button';
  entry.setAttribute('aria-label', '上下文压缩'); entry.setAttribute('aria-haspopup', 'dialog'); entry.setAttribute('aria-controls', 'qwen-context-dialog');
  const entryText = element('span', '', '上下文压缩');
  const dot = element('i', 'qc-dot'); dot.setAttribute('aria-hidden', 'true');
  entry.append(icon(), entryText, dot); footer.append(entry);
  const dialog = element('dialog', 'qc-dialog'); dialog.id = 'qwen-context-dialog'; dialog.setAttribute('aria-labelledby', 'qc-title');
  const top = element('div', 'qc-heading');
  const title = element('h2', '', '上下文压缩'); title.id = 'qc-title';
  const close = element('button', 'qc-close', '×'); close.type = 'button'; close.setAttribute('aria-label', '关闭上下文压缩');
  top.append(title, close);
  const intro = element('p', 'qc-intro', '在上下文接近上限时，用本地模型整理较早的对话，保留最近对话和原始聊天记录。');
  const capacity = element('div', 'qc-capacity', '正在读取窗口设置…');
  // Live headroom for the conversation on screen. The dialog used to describe only the
  // window size and the trigger point, so "how much is left" could not be read off it.
  const usageLine = element('div', 'qc-capacity qc-usage');
  const toggleLabel = element('label', 'qc-toggle');
  const toggle = element('input'); toggle.type = 'checkbox'; toggle.setAttribute('aria-label', '自动压缩上下文');
  toggleLabel.append(toggle, element('span', '', '自动压缩上下文'));
  const note = element('p', 'qc-note', '压缩需要一次本地推理；摘要可能省略细节，重要原文请在新消息中再次引用。');
  const status = element('div', 'qc-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  const statusLabel = element('strong', 'qc-status-label');
  const statusMessage = element('p', 'qc-status-message');
  const metrics = element('div', 'qc-metrics');
  const before = element('strong', 'qc-before'); const after = element('strong', 'qc-after');
  const beforeBlock = element('div'); beforeBlock.append(element('span', '', '压缩前'), before);
  const afterBlock = element('div'); afterBlock.append(element('span', '', '发送给模型'), after);
  metrics.append(beforeBlock, element('span', 'qc-arrow', '→'), afterBlock);
  const saving = element('p', 'qc-saving'); status.append(statusLabel, statusMessage, metrics, saving);
  const manual = element('button', 'qc-manual', '压缩下一轮'); manual.type = 'button';
  const feedback = element('p', 'qc-feedback'); feedback.setAttribute('role', 'status');
  dialog.append(top, intro, capacity, usageLine, toggleLabel, note, status, manual, feedback);
  document.body.append(dialog);

  function render() {
    const id = currentId(); const state = states.get(id);
    const plan = armed.get(planKey());
    const serverOff = config?.enabled === false;
    toggle.checked = enabled; toggle.disabled = serverOff;
    capacity.textContent = config ? `${compact(config.context_size)} 窗口 · 约 ${compact(config.context_size * config.trigger_ratio)} 自动触发 · 预留 ${number(config.reserve_tokens)} 输出 Token` : configError || '正在读取窗口设置…';
    const used = numeric(state?.original_tokens) ? state.original_tokens
      : numeric(state?.prompt_tokens) ? state.prompt_tokens : null;
    if (config && numeric(used)) {
      const trigger = config.context_size * config.trigger_ratio;
      const left = Math.max(0, config.context_size - used);
      const untilTrigger = trigger - used;
      usageLine.textContent = `本会话已用 ${number(used)} / ${number(config.context_size)} Tokens · 余量 ${number(left)}`
        + (untilTrigger > 0 ? ` · 再约 ${number(untilTrigger)} 触发自动压缩` : ' · 已达自动压缩点');
    } else {
      usageLine.textContent = config
        ? '本会话尚未统计：发送一条消息后显示已用与余量。'
        : (configError || '');
    }
    statusLabel.textContent = state ? labels[state.state] : '等待消息';
    statusMessage.textContent = state?.message || '发送消息后，这里显示本会话的实际压缩结果。';
    status.dataset.state = state?.state || 'idle';
    const effective = state?.prompt_tokens ?? state?.effective_tokens;
    metrics.hidden = !numeric(state?.original_tokens) || !numeric(effective);
    before.textContent = number(state?.original_tokens); after.textContent = number(effective);
    saving.hidden = !numeric(state?.saved_tokens);
    saving.textContent = numeric(state?.saved_tokens) ? `减少 ${number(state.saved_tokens)} Tokens${state.cache_hit || state.cached ? ' · 复用已有摘要' : ''}` : '';
    manual.disabled = serverOff || !!plan?.claim || !!active.get(id);
    manual.textContent = plan ? '取消下轮压缩' : '压缩下一轮';
    manual.setAttribute('aria-pressed', String(!!plan));
    feedback.textContent = serverOff ? '服务端未启用上下文压缩。' : plan ? '已安排。发送下一条消息时执行；压缩成功后自动取消本次安排。' : hint;
    dot.dataset.state = state?.state || 'idle';
    entry.title = `上下文压缩 · ${serverOff ? '服务端关闭' : enabled ? '自动开启' : '自动关闭'}${state ? ' · ' + labels[state.state] : ''}`;
  }
  async function localJson(url, signal) {
    const controller = new AbortController();
    const abort = () => controller.abort(signal?.reason);
    if (signal?.aborted) abort(); else signal?.addEventListener('abort', abort, { once: true });
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await Reflect.apply(originalFetch, window, [url, { cache: 'no-store', signal: controller.signal }]);
      if (!response.ok) throw new Error('本地压缩状态暂不可用');
      return await response.json();
    } finally { clearTimeout(timeout); signal?.removeEventListener('abort', abort); }
  }
  async function loadConfig() {
    try {
      const value = await localJson('/local/context/config');
      if (!numeric(value.context_size) || !numeric(value.trigger_ratio) || !numeric(value.reserve_tokens)) throw new Error('配置无效');
      config = value; configError = '';
    } catch { configError = '压缩设置暂不可用；聊天连接保持原有行为。'; }
    render();
  }
  function settlePlan(request, value) {
    const plan = armed.get(request.planKey);
    if (request.force && ['compressed', 'cached'].includes(value?.state) && plan?.claim === request.id) {
      armed.delete(request.planKey);
      if (currentChat() === baseId(request.conversation)) hint = '本轮压缩完成，后续继续使用自动压缩设置。';
    }
  }
  async function poll(request, final = false) {
    if (active.get(request.conversation) !== request) return;
    try {
      const value = await localJson('/local/context/status?conversation_id=' + encodeURIComponent(request.conversation), request.pollController.signal);
      if (active.get(request.conversation) !== request) return;
      // Ignore the previous round while this request is waiting for the model.
      if (!numeric(value.updated_at) || value.updated_at >= request.startedAt) {
        setState(request.conversation, value);
        if (final && numeric(value.updated_at) && value.updated_at >= request.startedAt) settlePlan(request, value);
      }
    } catch { /* Monitoring must never interrupt generation or automatic retries. */ }
    finally {
      if (!final && active.get(request.conversation) === request && !request.received) request.timer = setTimeout(() => void poll(request), 1000);
    }
  }
  function finish(request, keepClaim = false) {
    clearTimeout(request.timer); request.pollController.abort();
    request.signal?.removeEventListener('abort', request.onAbort);
    if (active.get(request.conversation) === request) active.delete(request.conversation);
    const plan = armed.get(request.planKey);
    if (!keepClaim && plan?.claim === request.id) plan.claim = null;
    render();
  }
  function responseState(response) {
    const state = response.headers?.get('X-Qwen-Context-State');
    if (!state || !Object.hasOwn(labels, state)) return null;
    const value = { state, message: '' };
    for (const [field, header] of Object.entries({ original_tokens: 'Original-Tokens', prompt_tokens: 'Effective-Tokens', saved_tokens: 'Saved-Tokens', summary_tokens: 'Summary-Tokens' })) {
      const raw = response.headers.get('X-Qwen-Context-' + header);
      if (raw !== null && raw !== '' && numeric(Number(raw))) value[field] = Number(raw);
    }
    value.cache_hit = response.headers.get('X-Qwen-Context-Cache-Hit') === 'true';
    return value;
  }

  /* Hand a response to the voice module's GPU gate, when it is present.
   *
   * The gate owns the question ("someone else has the card — wait, or take it?") and the
   * replay with ?gpu=preempt. Kept as a soft dependency: this file must work on its own if
   * the voice module is not injected. */
  async function gpuGate(response, request) {
    const gate = window.QwenVoice && window.QwenVoice.gpuGate;
    if (typeof gate !== 'function' || !response || response.status !== 409) return null;
    try {
      return await gate(response, request);
    } catch (failure) {
      return null;
    }
  }

  async function requestMetadata(input, init) {
    const isRequest = typeof Request !== 'undefined' && input instanceof Request;
    const url = new URL(isRequest ? input.url : String(input), location.href);
    const method = String(init?.method !== undefined ? init.method : isRequest ? input.method : 'GET').toUpperCase();
    if (url.origin !== location.origin || method !== 'POST' || !['/v1/chat/completions', '/chat/completions'].includes(url.pathname)) return null;
    const headers = new Headers(init?.headers !== undefined ? init.headers : isRequest ? input.headers : undefined);
    const conversation = headers.get('X-Conversation-Id');
    if (!conversation) return null; // Includes title generation and native pre-encoding.
    let body = init?.body;
    if (body === undefined && isRequest) {
      // Only inspect a cloned request. The original request body and response stream stay untouched.
      if (input.bodyUsed || !input.headers.get('content-type')?.includes('json')) return null;
      const clone = input.clone(); const reader = clone.body?.getReader();
      if (!reader) return null;
      const decoder = new TextDecoder(); let size = 0; body = '';
      try {
        for (;;) {
          const part = await reader.read(); if (part.done) break;
          size += part.value.byteLength;
          if (size > 8 * 1024 * 1024) { void reader.cancel().catch(() => {}); return null; }
          body += decoder.decode(part.value, { stream: true });
        }
        body += decoder.decode();
      } finally { reader.releaseLock(); }
    }
    if (typeof body !== 'string') return null;
    let parsed;
    try { parsed = JSON.parse(body); } catch { return null; }
    if (!parsed || typeof parsed !== 'object' || parsed.n_predict === 0 || parsed.max_tokens === 0 || parsed.max_completion_tokens === 0 || !Array.isArray(parsed.messages)) return null;
    return { headers, conversation, url: url.pathname + url.search, signal: init?.signal !== undefined ? init.signal : isRequest ? input.signal : undefined };
  }
  window.fetch = async function qwenContextFetch(input, init) {
    let metadata;
    try { metadata = await requestMetadata(input, init); } catch { /* Delegate invalid requests to the real fetch implementation. */ }
    if (!metadata) return Reflect.apply(originalFetch, this, arguments);
    const chat = baseId(metadata.conversation);
    actualIds.set(chat, metadata.conversation);
    while (actualIds.size > 64) actualIds.delete(actualIds.keys().next().value);
    if (armed.has('__next__') && !armed.has(chat) && currentChat() === chat) { armed.set(chat, armed.get('__next__')); armed.delete('__next__'); }
    const plan = armed.get(chat);
    const force = !!plan && !plan.claim && config?.enabled !== false;
    metadata.headers.set('X-Qwen-Context-Compression', force ? 'force' : enabled && config?.enabled !== false ? 'auto' : 'off');
    const request = { id: ++sequence, conversation: metadata.conversation, planKey: chat, force, signal: metadata.signal, startedAt: Date.now() / 1000, pollController: new AbortController(), received: false };
    if (force) plan.claim = request.id;
    const previous = active.get(metadata.conversation);
    if (previous) finish(previous, true);
    active.set(metadata.conversation, request);
    request.onAbort = () => finish(request);
    metadata.signal?.addEventListener('abort', request.onAbort, { once: true });
    setState(metadata.conversation, { state: 'checking', message: force ? '将在本轮整理较早对话。' : enabled ? '正在检查是否需要压缩。' : '本轮自动压缩已关闭。' });
    request.timer = setTimeout(() => void poll(request), 1000);
    try {
      let response = await Reflect.apply(originalFetch, this, [input, { ...init, headers: metadata.headers }]);
      // The GPU gate answers a crowded card with 409 and a question ("wait, or take it?").
      // Asking it here rather than from a second window.fetch wrapper is deliberate: the
      // bundled chat app assigns window.fetch itself while booting, and a wrapper that made
      // the property non-writable broke that assignment and left a blank page.
      const replacement = await gpuGate(response, { url: metadata.url, method: 'POST', headers: metadata.headers, body: init?.body, signal: metadata.signal });
      if (replacement) response = replacement;
      request.received = true; clearTimeout(request.timer);
      const value = responseState(response);
      if (value) {
        if (active.get(metadata.conversation) === request) setState(metadata.conversation, value);
        settlePlan(request, value);
      }
      // Status fetch is detached: the native UI receives the exact response immediately.
      void poll(request, true).finally(() => finish(request));
      return response;
    } catch (failure) {
      if (active.get(metadata.conversation) === request) setState(metadata.conversation, { state: 'error', message: metadata.signal?.aborted ? '请求已取消；手动压缩安排保留。' : '本轮请求未完成；手动压缩安排保留，可在下一轮重试。' });
      finish(request); throw failure;
    }
  };

  // Opening the dialog asks the server for this conversation's latest count, so the
  // headroom on screen is the current one rather than whatever the last request left.
  async function refreshCurrent() {
    const id = currentId();
    if (!id) return;
    try {
      setState(id, await localJson('/local/context/status?conversation_id=' + encodeURIComponent(id)));
    } catch { /* keep the last known numbers */ }
  }

  entry.addEventListener('click', () => { lastFocus = document.activeElement; hint = ''; render(); if (!dialog.open) dialog.showModal(); if (!config) void loadConfig(); void refreshCurrent(); });
  close.addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => { if (lastFocus?.isConnected) lastFocus.focus(); });
  toggle.addEventListener('change', () => {
    enabled = toggle.checked; hint = enabled ? '已开启，后续请求接近阈值时自动压缩。' : '已关闭自动压缩；仍可手动安排压缩下一轮。';
    try { localStorage.setItem(storageKey, String(enabled)); } catch { hint += ' 此次设置仅在当前页面有效。'; }
    render();
  });
  manual.addEventListener('click', () => {
    const key = planKey();
    if (armed.has(key)) { armed.delete(key); hint = '已取消下轮手动压缩。'; } else { armed.set(key, { claim: null }); hint = ''; }
    render();
  });
  window.addEventListener('storage', event => { if (event.key === storageKey) { enabled = event.newValue !== 'false'; render(); } });
  window.addEventListener('hashchange', () => { hint = ''; render(); });
  function attach() {
    const candidate = document.querySelector('body > div[style="display: contents"] > .flex > aside') || document.querySelector('aside[aria-label="侧边栏"], aside[aria-label="Sidebar"]');
    if (!candidate) return;
    sidebar = candidate;
    if (footer.parentElement !== sidebar) {
      const usage = sidebar.querySelector('.qu-sidebar-footer');
      if (usage) sidebar.insertBefore(footer, usage); else sidebar.append(footer);
    }
    if (!dialog.isConnected) document.body.append(dialog);
  }
  let attachPending = false;
  new MutationObserver(() => {
    if (attachPending) return;
    attachPending = true;
    requestAnimationFrame(() => { attachPending = false; attach(); });
  }).observe(document.body, { childList: true, subtree: true });
  attach(); render(); void loadConfig();
})();
