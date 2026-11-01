/* Thinking tier for the two chat modes: 纯文字 and 识图.
 *
 * Why it exists: the two 27B profiles ship a chat template with a real reasoning-effort knob
 * (`low` / `medium` / `xhigh`, default `xhigh`), and `xhigh` is a lot of thinking for a
 * "translate this sentence" turn. The owner asked for the choice to sit in the conversation
 * box, so it is a small segmented control in the same dock the microphone uses.
 *
 * The selected tier rides a request header (`X-Qwen-Thinking`) rather than the body, because
 * the body belongs to the app's own bundle: the gateway translates the header into the
 * template arguments and clamps it to the values the current profile's template accepts (a
 * wrong `reasoning_effort` makes the backend raise).
 *
 * The 9B's template has no effort knob at all, so on that profile the control shows 关闭/开启
 * only — the list comes from the gateway, never from a copy kept here.
 */
(() => {
  'use strict';
  const STORAGE_KEY = 'qwen.thinking.level';
  const DEFAULT_LEVEL = 'xhigh';
  const LABELS = { off: '关闭', on: '开启', low: '低', medium: '中', xhigh: '高' };
  const TITLES = {
    off: '思考模式：关闭 —— 直接回答，最快',
    on: '思考模式：开启',
    low: '思考模式：低 —— 简短思考后作答',
    medium: '思考模式：中',
    xhigh: '思考模式：高（模型默认）—— 仔细推敲，最慢',
  };

  let levels = ['off', 'low', 'medium', 'xhigh'];
  let level = read();

  function read() {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      return stored || DEFAULT_LEVEL;
    } catch (failure) {
      return DEFAULT_LEVEL;
    }
  }

  function write(value) {
    level = value;
    try { localStorage.setItem(STORAGE_KEY, value); } catch (failure) { /* session-only */ }
    render();
  }

  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  /* Where the conversation box is, and the dock the microphone already created there. */
  function composerForm() { return document.querySelector('.conversation-chat-form'); }

  function dock() {
    const form = composerForm();
    if (!form) return null;
    let host = document.getElementById('local-composer-tools');
    if (!host) {
      host = el('div', 'local-composer-tools');
      host.id = 'local-composer-tools';
      form.append(host);
    }
    return host;
  }

  function render() {
    const host = dock();
    const existing = document.getElementById('qwen-thinking-control');
    if (!host) { if (existing) existing.remove(); return; }
    const control = existing || el('div', 'qwen-thinking');
    control.id = 'qwen-thinking-control';
    control.textContent = '';
    control.title = TITLES[level] || '';
    control.append(el('span', 'qwen-thinking-label', '思考'));
    for (const option of levels) {
      const button = el('button', 'qwen-thinking-option', LABELS[option] || option);
      button.type = 'button';
      button.dataset.level = option;
      button.setAttribute('aria-pressed', String(option === level));
      if (option === level) button.classList.add('is-on');
      button.title = TITLES[option] || option;
      button.addEventListener('click', event => {
        event.preventDefault();
        event.stopPropagation();
        write(option);
      });
      control.append(button);
    }
    if (!existing) host.append(control);
  }

  /* One fetch wrapper for the whole page, like the context control's. Only chat completions
   * carry the header; everything else is passed through untouched. */
  const originalFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      if (url.includes('/v1/chat/completions') || url.includes('/chat/completions')) {
        const headers = new Headers((init && init.headers) || (input && input.headers) || undefined);
        headers.set('X-Qwen-Thinking', level);
        return originalFetch.call(this, input, { ...(init || {}), headers });
      }
    } catch (failure) { /* a wrapper must never break the request it wraps */ }
    return originalFetch.apply(this, arguments);
  };

  /* The list of tiers comes from the gateway (it knows the profile's template), so a profile
   * switch can shrink it from four options to two without a reload. */
  async function refreshLevels() {
    try {
      const response = await originalFetch('/local/status', { cache: 'no-store' });
      if (!response.ok) return;
      const status = await response.json();
      const offered = status && status.thinking_levels;
      if (!Array.isArray(offered) || !offered.length) return;
      const changed = offered.join(',') !== levels.join(',');
      levels = offered.slice();
      if (!levels.includes(level)) level = levels.includes(DEFAULT_LEVEL) ? DEFAULT_LEVEL : levels[levels.length - 1];
      if (changed) render();
    } catch (failure) { /* keep whatever we had */ }
  }

  function isChatMode() {
    const body = document.body;
    if (!body) return false;
    // The mode control marks the page; 生图/视频/语音 views are full-screen panels and must not
    // carry this control. Absent the marker (before the toolbar boots) the composer decides.
    const mode = (document.querySelector('.local-mode-button[aria-pressed="true"]') || {}).dataset;
    if (mode && mode.mode && mode.mode !== 'text' && mode.mode !== 'vision') return false;
    return Boolean(composerForm());
  }

  let passes = 0;
  function tick() {
    if (isChatMode()) render();
    if (passes++ % 20 === 0) void refreshLevels();
    window.setTimeout(tick, 700);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => { void refreshLevels(); tick(); }, { once: true });
  } else {
    void refreshLevels();
    tick();
  }
})();
