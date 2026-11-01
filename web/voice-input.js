/* Voice input for every prompt field, live text while speaking, and the GPU queue dialog.
 *
 * Loaded on the gateway's own pages, so it works in the chat composer, the image panel
 * and the video panel alike: one delegated focus listener plus a MutationObserver covers
 * inputs that exist now and inputs a panel creates later.
 *
 * Recognition is the machine's own model (see voice_input.py): the browser streams 16 kHz
 * PCM to the gateway and nothing leaves this computer.
 */
(function (root) {
  'use strict';

  const TRANSCRIBE_URL = '/local/voice/transcribe';
  const STREAM_URL = '/local/voice/stream';
  const STREAM_FINISH_URL = '/local/voice/stream/finish';
  const STREAM_CANCEL_URL = '/local/voice/cancel';
  const VOICE_STATUS_URL = '/local/voice/status';
  const GPU_STATUS_URL = '/local/gpu';
  const GPU_PREEMPT_URL = '/local/gpu/preempt';
  const GPU_RELEASE_URL = '/local/gpu/release';
  const RECORD_LIMIT_MS = 120000;
  const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;
  // How much audio one live chunk carries: small enough that the server sees text
  // building up, large enough that the per-request overhead stays irrelevant.
  const CHUNK_MS = 250;
  // The model wants 16 kHz mono; the browser records at the device rate and the worklet
  // resamples as it goes.
  const TARGET_RATE = 16000;
  const DEFAULT_LANGUAGE = 'zh';
  const MIME_CANDIDATES = [
    'audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4',
  ];
  const NEWS = {
    noMic: '这个浏览器不支持录音（需要通过 https 或 127.0.0.1 打开页面）。',
    denied: '拿不到麦克风权限：',
    noRecorder: '这个浏览器无法录音：',
    insertFailed: '识别成功，但无法写入该输入框，请手动粘贴。',
    empty: '没有识别到内容，请再说一次。',
    silent: '没有录到声音，请靠近麦克风重试。',
    tooLong: '录音过长，请缩短后重试。',
    failed: '语音识别失败：',
    streamDown: '实时识别连不上，已改为录完后识别。',
    streamUnavailable: '实时识别不可用，已改为录完后识别。',
    pickField: '请先点进一个输入框，再按语音输入。',
    gpuTitle: '显卡正被占用',
    gpuNote: '「打断任务」会取消访客正在跑的生图/生视频任务，并暂停访客的新任务；'
      + '正在进行中的文本对话无法中途打断，会在这一轮结束后停住。'
      + '「插队」不打断任何人，只是让访客的新请求排在你的后面。',
  };

  const state = {
    target: null,
    recorder: null,
    chunks: [],
    stream: null,
    live: null,
    caret: null,
    // Live dictation bookkeeping: `installed` is the exact text already placed in the
    // field, `hypothesis` the latest total the recognizer produced, and `phraseSeq` which
    // settled phrase that total includes.
    installed: '',
    hypothesis: '',
    phraseSeq: null,
    voice: null,
    startedAt: 0,
    timer: null,
    pollTimer: null,
    busy: false,
    gpu: null,
    modal: null,
  };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  /* The microphone glyph. Icon-only on purpose: this button now sits inside the composer next to
     the app's own controls, where a word would compete with the send button for the corner. */
  function micIcon() {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    for (const [key, value] of Object.entries({
      viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2',
      'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true',
    })) svg.setAttribute(key, value);
    for (const [tag, attributes] of [
      ['path', { d: 'M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z' }],
      ['path', { d: 'M19 10v1a7 7 0 0 1-14 0v-1' }],
      ['line', { x1: '12', x2: '12', y1: '18', y2: '22' }],
    ]) {
      const part = document.createElementNS(svg.namespaceURI, tag);
      for (const [key, value] of Object.entries(attributes)) part.setAttribute(key, value);
      svg.append(part);
    }
    return svg;
  }

  function isTextTarget(node) {
    if (!node || node.disabled || node.readOnly) return false;
    if (node.isContentEditable) return true;
    const tag = (node.tagName || '').toLowerCase();
    if (tag === 'textarea') return true;
    if (tag !== 'input') return false;
    const type = (node.getAttribute('type') || 'text').toLowerCase();
    return ['text', 'search', 'url', 'email', 'tel', ''].includes(type);
  }

  function describeError(failure) {
    if (!failure) return '未知错误';
    const name = failure.name || '';
    if (name === 'NotAllowedError') return '麦克风权限被拒绝（可在浏览器地址栏的权限里重新允许）';
    if (name === 'NotFoundError') return '找不到麦克风设备';
    if (name === 'NotReadableError') return '麦克风被其他程序占用';
    return failure.message || String(failure);
  }

  /* Strip the special tokens a speech model can emit, so nothing odd reaches the field. */
  function sanitize(text) {
    return String(text || '')
      .replace(/<\|[^|]*\|>/g, '')
      .replace(/\s+/g, ' ')
      .trim();
  }

  // ---------------------------------------------------------------- insertion
  /* Insert at the caret through execCommand: it fires the input/change events the hosting
   * framework listens to, and it participates in undo, which the panel's own buttons do
   * not. A `range` pins the position, which is what live dictation needs: text keeps
   * arriving at the spot captured when recording started even if focus moves on. */
  function insertText(node, text, range) {
    if (!node || !text) return false;
    try {
      node.focus();
      if (range) applyRange(range);
      if (document.execCommand && document.execCommand('insertText', false, text)) return true;
    } catch (failure) { /* fall through to the direct path */ }
    try {
      if (node.isContentEditable) insertIntoEditable(node, text, range);
      else insertIntoField(node, text, range);
      node.dispatchEvent(new Event('input', { bubbles: true }));
      node.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    } catch (failure) {
      return false;
    }
  }

  function applyRange(range) {
    const selection = root.getSelection && root.getSelection();
    if (!selection || !range) return;
    selection.removeAllRanges();
    selection.addRange(range);
  }

  function insertIntoField(node, text, range) {
    const start = range && typeof range.start === 'number' ? range.start
      : (typeof node.selectionStart === 'number' ? node.selectionStart : null);
    const end = range && typeof range.end === 'number' ? range.end
      : (typeof node.selectionEnd === 'number' ? node.selectionEnd : null);
    const before = node.value || '';
    if (start === null || end === null) {
      node.value = before + text;
    } else {
      node.value = before.slice(0, start) + text + before.slice(end);
      node.selectionStart = node.selectionEnd = start + text.length;
    }
  }

  function insertIntoEditable(node, text, range) {
    const selection = root.getSelection && root.getSelection();
    if (range) applyRange(range);
    if (selection && selection.rangeCount) {
      const live = selection.getRangeAt(0);
      live.deleteContents();
      live.insertNode(document.createTextNode(text));
      live.collapse(false);
      return;
    }
    node.textContent = (node.textContent || '') + text;
  }

  function captureCaret(node) {
    if (!node) return null;
    if (!node.isContentEditable) {
      if (typeof node.selectionStart !== 'number') return { node, start: null, end: null };
      // A field the button was clicked from was never focused: its selection sits at 0, and
      // dictating into position 0 would push the existing prompt aside instead of continuing it.
      if (document.activeElement !== node) {
        const end = (node.value || '').length;
        return { node, start: end, end };
      }
      return { node, start: node.selectionStart, end: node.selectionEnd };
    }
    const selection = root.getSelection && root.getSelection();
    if (!selection || !selection.rangeCount) return { node, range: null };
    const range = selection.getRangeAt(0).cloneRange();
    range.collapse(false);
    return { node, range };
  }

  function caretRange(caret) {
    if (!caret) return null;
    if (caret.range) return caret.range;
    if (typeof caret.start !== 'number') return null;
    const end = caret.start + (state.installed || '').length;
    return { start: end, end };
  }

  /* Take the server's snapshot and insert whatever is new.
   *
   * `committed` is every phrase the recognizer has settled, `phrase` the sentence it is
   * still revising, and `seq` says which settled phrase is already merged in — without
   * that, resuming after a pause would insert the whole transcript a second time. Text
   * already on screen is never removed: rewriting it every update is what makes a
   * dictated sentence jump backwards mid-word. */
  function acceptVoice(voice) {
    if (!voice || !state.live) return;
    const committed = sanitize(voice.committed);
    const phrase = sanitize(voice.phrase || '');
    const seq = typeof voice.seq === 'number' ? voice.seq : 0;
    const total = committed + phrase;
    state.phraseSeq = seq;
    state.hypothesis = total;
    const previous = state.installed || '';
    if (total === previous || !total.startsWith(previous)) return;
    const caret = state.caret;
    const node = caret && caret.node && caret.node.isConnected ? caret.node : state.target;
    if (!insertText(node, total.slice(previous.length), caretRange(caret))) return;
    state.installed = total;
  }

  // ---------------------------------------------------------------- mic buttons
  /* One engine, several buttons. The chat composer has its dock (shared with the permission
     chip); a plain field elsewhere gets the floating button; and every prompt box in our own
     panels gets a microphone of its own parked in the bottom-right corner of the box — "where do
     I dictate this prompt" should not require clicking into the field first, which is exactly
     what the floating button demanded. All of them drive the same dictation; the button that was
     clicked decides which field the text lands in. */
  const micButtons = new Set();
  const boxMics = new Map();          // field -> its own microphone
  const boxTargets = new WeakMap();   // microphone -> the field it belongs to
  /* Prompt boxes in our own panels (image, video). Declared here, not next to syncBoxMics, because
     the first sync runs from the dock pass — a `const` further down the file would be in its dead
     zone there and the whole module would stop with "Cannot access before initialization". */
  const BOX_MIC_SELECTOR = '.qg-prompt-input, .qg-negative-input, .qv-textarea';
  let boxMicSeq = 0;

  function makeMicButton(...classes) {
    const button = el('button', ['qv-mic', ...classes].filter(Boolean).join(' '));
    button.type = 'button';
    button.title = '语音输入（本地识别，边说边出字）';
    button.setAttribute('aria-label', '语音输入');
    // The label stays in the markup as the screen-reader name and as the status text; the
    // docked and boxed buttons paint the glyph and the timer only.
    button.append(micIcon(), el('span', 'qv-mic-label qv-sr-only', '语音'));
    button.append(el('span', 'qv-mic-timer', ''));
    button.hidden = true;
    button.addEventListener('click', () => toggleDictation(button));
    micButtons.add(button);
    return button;
  }

  /* The caret-following / composer-docked button, marked so it stays identifiable now that the
     prompt boxes have microphones of their own: `.qv-mic` alone is ambiguous in a page that shows
     the chat composer and a panel at once. */
  const mic = makeMicButton('qv-mic-primary');

  /* Where the conversation box is. The app's own composer lives in ``.conversation-chat-form``;
     the permission chip (mode-control.js) mounts the same dock, so both controls share one row
     at the bottom-right of the box instead of fighting over the corner. */
  function composerForm() {
    return document.querySelector('.conversation-chat-form');
  }

  function composerField() {
    return document.querySelector('.conversation-chat-form textarea')
      || document.querySelector('.conversation-chat-form [role="textbox"][contenteditable]');
  }

  function composerDock() {
    const host = composerForm();
    if (!host) return null;
    let dock = document.getElementById('local-composer-tools');
    if (!dock) {
      dock = el('div', 'local-composer-tools');
      dock.id = 'local-composer-tools';
    }
    if (dock.parentElement !== host) host.append(dock);
    return dock;
  }

  function docked() {
    return mic.classList.contains('qv-mic-docked');
  }

  /* Park the mic in the composer. Returns false when there is no composer on this page (the
     image and video panels), which is what keeps the old caret-following button for their
     prompt fields. */
  function dockMic() {
    const dock = composerDock();
    if (!dock) return false;
    if (mic.parentElement !== dock) dock.append(mic);
    mic.classList.add('qv-mic-docked');
    mic.style.left = '';
    mic.style.top = '';
    mic.hidden = false;
    return true;
  }

  function undockMic() {
    if (!docked()) return false;
    mic.classList.remove('qv-mic-docked');
    document.body.append(mic);
    return true;
  }

  function positionMic() {
    if (docked()) {
      const field = composerField();
      mic.hidden = !field;
      return;
    }
    const target = state.target;
    if (!target || !target.isConnected) { mic.hidden = true; return; }
    const rect = target.getBoundingClientRect();
    if (!rect || (!rect.width && !rect.height)) { mic.hidden = true; return; }
    mic.hidden = false;
    const height = Math.min(30, Math.max(22, rect.height - 10));
    mic.style.setProperty('--qv-mic-size', height + 'px');
    mic.style.left = Math.round(Math.min(rect.right - 8, root.innerWidth - 84)) + 'px';
    mic.style.top = Math.round(rect.top + 6) + 'px';
  }

  function attachTo(target) {
    if (!isTextTarget(target) || state.busy) return;
    if (target.dataset && target.dataset.boxMic) {
      // The box already carries its own microphone. Showing the caret-following pill here too put
      // two microphones on one field, and the owner read the extra one as a leftover from the
      // previous voice input ("生图和视频怎么还残留上一个语音输入的圆框").
      undockMic();
      mic.hidden = true;
      return;
    }
    state.target = target;
    const dock = document.getElementById('local-composer-tools');
    const insideComposer = !!(dock && dock.parentElement && dock.parentElement.contains(target));
    if (insideComposer) { dockMic(); return; }
    if (undockMic()) { positionMic(); return; }
    positionMic();
  }

  document.addEventListener('focusin', event => {
    if (isTextTarget(event.target)) attachTo(event.target);
  }, true);
  document.addEventListener('focusout', () => {
    // Delayed so a click on the mic (which blurs the field) still finds its target.
    setTimeout(() => {
      if (state.busy) return;
      if (docked()) return;
      const active = document.activeElement;
      if (!isTextTarget(active)) { state.target = null; mic.hidden = true; }
    }, 180);
  }, true);
  root.addEventListener('resize', positionMic, { passive: true });
  root.addEventListener('scroll', positionMic, { passive: true, capture: true });
  /* The composer is the conversation's own box and stays on screen while it is being used, so
     the mic is parked there from the start — and re-parked if the app rebuilds the box (a
     route change replaces the DOM node). A field outside the composer wins only while the
     owner is actually typing in it.
     
     Full rate while the page is visible; while the tab is in the background the composer is
     not on screen, so the pass slows down instead of querying document.activeElement and the
     dock every second. Coming back to the tab re-parks immediately, so no docking state is
     ever stale on screen. */
  const DOCK_TICK_MS = 1000;
  const DOCK_HIDDEN_TICK_MS = 5000;
  let dockTimer = null;
  function dockPass() {
    // Prompt boxes come and go with the panels (a shot added to the storyboard is a new
    // textarea), so the pass that parks the composer's microphone also gives any new prompt box
    // its own.
    syncBoxMics();
    if (state.busy || state.live || state.recorder) return;
    const active = document.activeElement;
    if (isTextTarget(active) && active.closest && !active.closest('.conversation-chat-form')) return;
    dockMic();
  }
  function scheduleDock() {
    if (dockTimer) clearTimeout(dockTimer);
    const delay = document.visibilityState === 'hidden' ? DOCK_HIDDEN_TICK_MS : DOCK_TICK_MS;
    dockTimer = setTimeout(() => {
      if (document.visibilityState !== 'hidden') dockPass();
      scheduleDock();
    }, delay);
  }
  dockTimer = setTimeout(() => { dockPass(); scheduleDock(); }, DOCK_TICK_MS);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') return;
    dockPass();
    scheduleDock();
  });
  dockMic();
  syncBoxMics();

  // ---------------------------------------------------------------- live capture
  const workletState = { moduleUrl: null };

  /* The recorder's own formats are heavy to decode and arrive in bursts; the recognizer
   * wants plain 16 kHz mono samples. A worklet converts as it goes, so the server can read
   * a short tail the moment it arrives — which is what makes text appear while the owner
   * is still speaking. */
  const WORKLET_SOURCE = `
    class QwenVoiceTap extends AudioWorkletProcessor {
      constructor() { super(); this.ratio = sampleRate / ${TARGET_RATE}; this.read = 0; }
      process(inputs) {
        const channel = inputs[0] && inputs[0][0];
        if (!channel || !channel.length) return true;
        const count = Math.max(1, Math.floor((channel.length + this.read) / this.ratio));
        const out = new Int16Array(count);
        for (let index = 0; index < count; index++) {
          const position = index * this.ratio + this.read;
          const left = Math.floor(position);
          const right = Math.min(left + 1, channel.length - 1);
          const weight = position - left;
          const value = channel[left] * (1 - weight) + channel[right] * weight;
          out[index] = Math.max(-32768, Math.min(32767, Math.round(value * 32767)));
        }
        this.read = (count * this.ratio + this.read) - channel.length;
        if (this.read < 0) this.read = 0;
        this.port.postMessage(out.buffer, [out.buffer]);
        return true;
      }
    }
    registerProcessor('qwen-voice-tap', QwenVoiceTap);
  `;

  async function startLive(stream, language) {
    const AudioContextClass = root.AudioContext || root.webkitAudioContext;
    if (!AudioContextClass || !root.AudioWorkletNode) return null;
    if (!workletState.moduleUrl) {
      const blob = new Blob([WORKLET_SOURCE], { type: 'application/javascript' });
      workletState.moduleUrl = URL.createObjectURL(blob);
    }
    const context = new AudioContextClass();
    await context.audioWorklet.addModule(workletState.moduleUrl);
    // Chrome starts an AudioContext suspended until a user gesture is seen *on the page*,
    // and a suspended context runs no worklet at all: the recorder looked live, the field
    // stayed empty, and only the stop click produced any text. Resuming is what turns the
    // graph on, and it has to be awaited before the node is wired.
    if (context.state === 'suspended' && typeof context.resume === 'function') {
      await context.resume();
    }
    const source = context.createMediaStreamSource(stream);
    const node = new root.AudioWorkletNode(context, 'qwen-voice-tap');
    // Not connected to the destination, so nothing is played back into the microphone.
    source.connect(node);
    if (context.state !== 'running' && typeof console !== 'undefined') {
      console.warn('Qwen voice: audio context is ' + context.state + '; live capture may not run');
    }
    return {
      context, node, source, language: language || DEFAULT_LANGUAGE,
      queue: [], sending: false, pending: 0, stopped: false, reported: false,
    };
  }

  function drainQueue(live) {
    const chunkSize = Math.round(TARGET_RATE * CHUNK_MS / 1000);
    const out = new Int16Array(chunkSize);
    let filled = 0;
    while (filled < chunkSize && live.queue.length) {
      const head = live.queue[0];
      const take = Math.min(head.length, chunkSize - filled);
      out.set(head.subarray(0, take), filled);
      filled += take;
      if (take === head.length) live.queue.shift();
      else live.queue[0] = head.subarray(take);
    }
    return filled === chunkSize ? new Uint8Array(out.buffer) : null;
  }

  async function sendChunk(live, payload) {
    live.sending = true;
    try {
      const response = await fetch(STREAM_URL + '?language=' + encodeURIComponent(live.language), {
        method: 'POST', body: payload, cache: 'no-store', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/octet-stream' },
      });
      if (response.ok) {
        live.pending = 0;
        state.voice = await response.json().catch(() => state.voice);
      } else {
        const detail = await response.json().catch(() => null);
        if (!live.reported) {
          live.reported = true;
          toast((detail && detail.error && detail.error.message) || NEWS.streamUnavailable, 'warn');
        }
      }
    } catch (failure) {
      live.pending += 1;
      if (live.pending > 6 && !live.reported) {
        live.reported = true;
        toast(NEWS.streamDown, 'warn');
      }
    } finally {
      live.sending = false;
    }
  }

  function pump(live) {
    if (live.stopped) return;
    const payload = drainQueue(live);
    if (payload && !live.sending) void sendChunk(live, payload);
  }

  function pollLive() {
    if (!state.live || state.live.stopped) return;
    pump(state.live);
    acceptVoice(state.voice);
    state.pollTimer = setTimeout(pollLive, 350);
  }

  async function finishLive(live) {
    const leftovers = [];
    while (live.queue.length) leftovers.push(live.queue.shift());
    if (leftovers.length) {
      const total = leftovers.reduce((sum, part) => sum + part.length, 0);
      const merged = new Int16Array(total);
      let offset = 0;
      for (const part of leftovers) { merged.set(part, offset); offset += part.length; }
      await sendChunk(live, new Uint8Array(merged.buffer));
    }
    const response = await fetch(STREAM_FINISH_URL + '?language=' + encodeURIComponent(live.language),
      { method: 'POST', body: new Uint8Array(0), cache: 'no-store', credentials: 'same-origin' });
    const result = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error((result && result.error && result.error.message) || ('HTTP ' + response.status));
    }
    return result;
  }

  async function dropLive(live) {
    live.stopped = true;
    try { live.node.port.onmessage = null; } catch (failure) { /* already gone */ }
    try { live.source.disconnect(); } catch (failure) { /* already gone */ }
    try { await live.context.close(); } catch (failure) { /* already gone */ }
    try {
      await fetch(STREAM_CANCEL_URL, { method: 'POST', body: new Uint8Array(0), cache: 'no-store' });
    } catch (failure) { /* the gateway drops an unknown session by itself */ }
  }

  // ---------------------------------------------------------------- recording
  function pickMime() {
    if (typeof root.MediaRecorder === 'undefined') return '';
    for (const candidate of MIME_CANDIDATES) {
      try {
        if (root.MediaRecorder.isTypeSupported(candidate)) return candidate;
      } catch (failure) { /* keep looking */ }
    }
    return '';
  }

  function tick() {
    const seconds = Math.floor((Date.now() - state.startedAt) / 1000);
    setMicTimers(Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0'));
    if (Date.now() - state.startedAt > RECORD_LIMIT_MS) void stopRecording();
  }

  /* Every microphone shows the same state: the floating one, the composer's docked one and each
     prompt box's own. The owner should be able to look at whichever one they clicked. */
  function setMicTimers(text) {
    for (const button of micButtons) {
      const timer = button.querySelector('.qv-mic-timer');
      if (timer) timer.textContent = text;
    }
  }

  function setMicMode(mode, label) {
    const text = label || '语音';
    for (const button of micButtons) {
      button.dataset.mode = mode;
      const node = button.querySelector('.qv-mic-label');
      if (node) node.textContent = text;
      button.classList.toggle('qv-mic-busy', mode === 'working');
      // Icon-only buttons: the state has to reach the tooltip, the accessible name and the colour.
      button.title = '语音输入（本地识别，边说边出字）' + (mode === 'idle' ? '' : ' · ' + text);
      button.setAttribute('aria-label', '语音输入：' + text);
    }
  }

  /* Which field a click dictates into: the box's own field for a box microphone, whatever was
     focused for the floating one, and the composer when nothing else is in hand. */
  function targetFor(button) {
    if (button && button !== mic) {
      const field = boxTargets.get(button);
      if (field && field.isConnected) return field;
    }
    if (isTextTarget(state.target)) return state.target;
    return composerField() || state.target;
  }

  function toggleDictation(button) {
    if (state.live || state.recorder) { void stopRecording(); return; }
    if (state.busy) return;
    const target = targetFor(button);
    if (isTextTarget(target)) state.target = target;
    void startRecording();
  }

  /* A microphone in the bottom-right corner of each prompt box in our own panels (image and
     video). Positioned from the field's own rectangle rather than the parent's, because a
     prompt's parent is sometimes the whole composer card (the negative-prompt input) — measuring
     the field keeps the button inside the box it belongs to whatever the panel does around it.
     The selector itself is declared up in the mic-button section, before the first sync runs. */
  function syncBoxMics() {
    for (const field of document.querySelectorAll(BOX_MIC_SELECTOR)) {
      if (field.closest('.conversation-chat-form')) continue;   // the composer has its own dock
      const parent = field.parentElement;
      if (!parent) continue;
      let button = boxMics.get(field);
      if (!button) {
        // The link is written on both elements: a prompt's parent may be the whole composer card,
        // so "the first microphone inside the parent" is not the same question as "this field's
        // microphone" — for the code, for a check, or for anyone reading the DOM.
        const id = String(++boxMicSeq);
        button = makeMicButton('qv-mic-box');
        button.dataset.boxMicFor = id;
        field.dataset.boxMic = id;
        boxMics.set(field, button);
        boxTargets.set(button, field);
        field.classList.add('qv-has-box-mic');
        parent.append(button);
      }
      if (button.parentElement !== parent) parent.append(button);
      if (getComputedStyle(parent).position === 'static') parent.style.position = 'relative';
      const parentRect = parent.getBoundingClientRect();
      const fieldRect = field.getBoundingClientRect();
      button.style.right = Math.round(parentRect.right - fieldRect.right + 8) + 'px';
      button.style.bottom = Math.round(parentRect.bottom - fieldRect.bottom + 8) + 'px';
      // A box too small to hold a button keeps the floating one instead of a button on top of
      // its text.
      button.hidden = fieldRect.width < 90 || fieldRect.height < 26;
    }
    for (const [field, button] of boxMics) {
      if (field.isConnected) continue;
      button.remove();
      boxMics.delete(field);
    }
  }

  function releaseStream() {
    if (state.stream) {
      for (const track of state.stream.getTracks()) track.stop();
      state.stream = null;
    }
  }

  function resetDictation() {
    state.installed = '';
    state.hypothesis = '';
    state.phraseSeq = null;
    state.caret = null;
    state.voice = null;
  }

  async function startRecording() {
    const target = state.target || document.activeElement;
    if (!isTextTarget(target)) {
      toast(NEWS.pickField, 'warn');
      return;
    }
    state.target = target;
    if (!root.navigator || !root.navigator.mediaDevices || !root.navigator.mediaDevices.getUserMedia) {
      toast(NEWS.noMic, 'error');
      return;
    }
    state.busy = true;
    setMicMode('starting', '启动…');
    resetDictation();
    state.caret = captureCaret(target);
    try {
      state.stream = await root.navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (failure) {
      state.busy = false;
      setMicMode('idle', '语音');
      toast(NEWS.denied + describeError(failure), 'error');
      return;
    }

    // Live first: text while you speak. Falling back to recording the whole clip is
    // better than refusing to work in a browser without AudioWorklet.
    let live = null;
    try {
      live = await startLive(state.stream, DEFAULT_LANGUAGE);
    } catch (failure) {
      live = null;
      if (typeof console !== 'undefined') console.warn('Qwen voice: live capture unavailable, using batch mode', failure);
    }
    if (live) {
      state.live = live;
      live.node.port.onmessage = event => { live.queue.push(new Int16Array(event.data)); };
      state.startedAt = Date.now();
      state.timer = setInterval(tick, 200);
      setMicMode('recording', '停止');
      setMicTimers('0:00');
      pollLive();
      return;
    }

    const mime = pickMime();
    try {
      state.recorder = mime ? new root.MediaRecorder(state.stream, { mimeType: mime })
        : new root.MediaRecorder(state.stream);
    } catch (failure) {
      releaseStream();
      state.busy = false;
      setMicMode('idle', '语音');
      toast(NEWS.noRecorder + describeError(failure), 'error');
      return;
    }
    state.chunks = [];
    state.recorder.addEventListener('dataavailable', event => {
      if (event.data && event.data.size) state.chunks.push(event.data);
    });
    state.recorder.addEventListener('error', event => {
      toast(NEWS.noRecorder + describeError(event.error), 'error');
      void cancelRecording();
    });
    state.recorder.start(250);
    state.startedAt = Date.now();
    state.timer = setInterval(tick, 200);
    setMicMode('recording', '停止');
    setMicTimers('0:00');
  }

  function finishRecording() {
    return new Promise(resolve => {
      const recorder = state.recorder;
      if (!recorder) { resolve(null); return; }
      recorder.addEventListener('stop', () => {
        const type = recorder.mimeType || 'audio/webm';
        resolve(state.chunks.length ? new Blob(state.chunks, { type }) : null);
      }, { once: true });
      try { recorder.stop(); } catch (failure) { resolve(null); }
    });
  }

  /* The final snapshot arrives after `state.live` was cleared, so it needs its own path:
   * everything it reports is text the field does not have yet. */
  function acceptFinal(final) {
    const text = sanitize(final && final.text);
    if (!text) return;
    const previous = state.installed || '';
    if (text === previous || !text.startsWith(previous)) return;
    const caret = state.caret;
    const node = caret && caret.node && caret.node.isConnected ? caret.node : state.target;
    if (insertText(node, text.slice(previous.length), caretRange(caret))) state.installed = text;
  }

  async function stopRecording() {
    clearInterval(state.timer);
    state.timer = null;
    setMicMode('working', state.live ? '收尾…' : '识别中…');

    if (state.live) {
      const live = state.live;
      state.live = null;
      clearTimeout(state.pollTimer);
      state.pollTimer = null;
      try {
        const final = await finishLive(live);
        if (final) acceptFinal(final);
        if (!state.installed) {
          const text = sanitize(final && final.text);
          if (text) {
            const caret = state.caret;
            const node = caret && caret.node && caret.node.isConnected ? caret.node : state.target;
            if (insertText(node, text, caretRange(caret))) state.installed = text;
            else toast(NEWS.insertFailed, 'warn');
          } else {
            toast((final && final.note) || NEWS.empty, 'warn');
          }
        }
      } catch (failure) {
        toast(NEWS.failed + describeError(failure), 'error');
      } finally {
        await dropLive(live);
        releaseStream();
        state.busy = false;
        setMicMode('idle', '语音');
        setMicTimers('');
        resetDictation();
      }
      return;
    }

    if (!state.recorder) return;
    const blob = await finishRecording();
    state.recorder = null;
    releaseStream();
    if (!blob || !blob.size) {
      state.busy = false;
      setMicMode('idle', '语音');
      setMicTimers('');
      toast(NEWS.silent, 'warn');
      return;
    }
    if (blob.size > MAX_UPLOAD_BYTES) {
      state.busy = false;
      setMicMode('idle', '语音');
      setMicTimers('');
      toast(NEWS.tooLong, 'warn');
      return;
    }
    await transcribe(blob);
  }

  async function cancelRecording() {
    clearInterval(state.timer);
    state.timer = null;
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
    if (state.live) {
      const live = state.live;
      state.live = null;
      await dropLive(live);
    }
    if (state.recorder) {
      try { state.recorder.stop(); } catch (failure) { /* already stopped */ }
      state.recorder = null;
    }
    releaseStream();
    state.busy = false;
    setMicMode('idle', '语音');
    setMicTimers('');
    resetDictation();
  }

  async function transcribe(blob) {
    const target = state.target;
    try {
      const response = await fetch(TRANSCRIBE_URL, {
        method: 'POST',
        headers: { 'Content-Type': blob.type || 'application/octet-stream' },
        body: blob,
        cache: 'no-store',
        credentials: 'same-origin',
      });
      const result = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error((result && result.error && result.error.message) || ('HTTP ' + response.status));
      }
      const text = sanitize(result && result.text);
      state.busy = false;
      setMicMode('idle', '语音');
      setMicTimers('');
      if (!text) {
        toast((result && result.note) || NEWS.empty, 'warn');
        return;
      }
      const node = target && target.isConnected ? target : document.activeElement;
      if (insertText(node, text)) toast('已插入识别结果（' + result.language + '）', 'ok');
      else toast(NEWS.insertFailed, 'warn');
    } catch (failure) {
      state.busy = false;
      setMicMode('idle', '语音');
      setMicTimers('');
      toast(NEWS.failed + describeError(failure), 'error');
    }
  }

  // The click handler is wired inside makeMicButton (every microphone shares toggleDictation).

  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && (state.live || state.recorder)) { void cancelRecording(); }
    // Ctrl/Cmd+Shift+M starts or stops, so the mouse is optional.
    if ((event.ctrlKey || event.metaKey) && event.shiftKey && (event.key === 'M' || event.key === 'm')) {
      event.preventDefault();
      if (state.live || state.recorder) void stopRecording(); else void startRecording();
    }
  }, true);

  // ---------------------------------------------------------------- notices
  let toastNode = null;
  let toastTimer = null;
  function toast(message, kind) {
    if (!toastNode) {
      toastNode = el('div', 'qv-toast');
      toastNode.setAttribute('role', 'status');
      document.body.append(toastNode);
    }
    toastNode.textContent = message;
    toastNode.dataset.kind = kind || 'ok';
    toastNode.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastNode.hidden = true; }, kind === 'error' ? 8000 : 4500);
  }

  // ---------------------------------------------------------------- GPU dialog
  function closeModal() {
    if (state.modal) { state.modal.remove(); state.modal = null; }
  }

  function busyRows(entries) {
    const rows = el('div', 'qvg-rows');
    if (!entries || !entries.length) {
      rows.append(el('p', 'qvg-muted', '当前没有登记在案的显卡任务；可能是刚结束或由后台进程占用。'));
      return rows;
    }
    for (const entry of entries) {
      const row = el('div', 'qvg-row');
      row.append(
        el('strong', '', entry.label || '未知调用方'),
        el('span', 'qvg-kind', entry.kind_label || entry.kind || ''),
        el('span', 'qvg-muted', entry.owner ? '本机' : '访客'),
        el('span', 'qvg-muted', '已运行 ' + Math.round(entry.elapsed_seconds || 0) + ' 秒'),
      );
      rows.append(row);
    }
    return rows;
  }

  async function openGpuModal(detail, onDone) {
    closeModal();
    const info = detail || {};
    const vram = info.vram || {};
    const overlay = el('div', 'qvg-overlay');
    const card = el('div', 'qvg-card');
    card.setAttribute('role', 'dialog');
    card.setAttribute('aria-modal', 'true');
    card.append(el('h2', '', NEWS.gpuTitle));
    card.append(el('p', 'qvg-lead',
      '你的这次请求需要显存，但当前可用显存不足'
      + (vram.free_mib != null ? '（可用 ' + Math.round(vram.free_mib) + ' MiB / 需要 '
        + (info.threshold_mib || 0) + ' MiB）' : '') + '。'));

    const who = el('section', 'qvg-who');
    who.append(el('h3', '', '现在是谁在使用'));
    who.append(busyRows(info.busy));
    card.append(who);

    const actions = el('div', 'qvg-actions');
    const interrupt = el('button', 'qvg-button qvg-danger', '打断任务并立即执行');
    const queue = el('button', 'qvg-button qvg-primary', '插队，等这一步结束就执行');
    const cancel = el('button', 'qvg-button qvg-ghost', '取消这次请求');
    actions.append(interrupt, queue, cancel);
    card.append(actions);
    card.append(el('p', 'qvg-note', NEWS.gpuNote));
    const progress = el('p', 'qvg-progress', '');
    card.append(progress);
    overlay.append(card);
    document.body.append(overlay);
    state.modal = overlay;

    const finish = choice => {
      closeModal();
      if (onDone) onDone(choice);
    };
    interrupt.addEventListener('click', () => { progress.textContent = '正在打断访客任务…'; finish('interrupt'); });
    queue.addEventListener('click', () => { progress.textContent = '正在插队…'; finish('queue'); });
    cancel.addEventListener('click', () => finish(''));
    overlay.addEventListener('click', event => { if (event.target === overlay) finish(''); });
  }

  // ---------------------------------------------------------------- GPU gate
  /* The gateway answers "the card is busy, what do you want to do?" with 409 and a `gpu`
   * payload. This used to be caught by wrapping window.fetch — which broke the chat app:
   * the bundled client assigns window.fetch itself while booting, and a non-writable
   * property made that assignment throw "Cannot assign to read only property 'fetch'",
   * leaving a blank page. Nothing global is touched now: the chat wrapper that already
   * exists (`context-control.js`) hands the response here, and this returns either a
   * usable response or null, meaning "this was not for me, pass it on". */
  async function gpuGate(response, request) {
    if (!response || response.status !== 409) return null;
    let payload = null;
    try {
      payload = await response.clone().json();
    } catch (failure) {
      return null;
    }
    const detail = payload && payload.gpu;
    if (!detail || detail.decision !== 'ask') return null;
    const choice = await new Promise(resolve => {
      void openGpuModal(detail, value => resolve(value || ''));
    });
    if (!choice) return null;
    try {
      await postJson(GPU_PREEMPT_URL, { mode: choice });
    } catch (failure) {
      toast('无法让出显卡：' + describeError(failure), 'error');
      return null;
    }
    toast(choice === 'interrupt' ? '已打断访客任务，正在执行你的请求…' : '已插队，正在执行你的请求…', 'ok');
    const target = withPreemptFlag(request && request.url ? request.url : request);
    return fetch(target, request || {});
  }

  /* Add the preempt flag to whatever was asked for, keeping the rest of the URL. */
  function withPreemptFlag(input) {
    try {
      const target = new URL(typeof input === 'string' ? input : (input && input.url) || '', root.location.href);
      target.searchParams.set('gpu', 'preempt');
      return target.pathname + target.search;
    } catch (failure) {
      return input;
    }
  }

  /* For a panel that catches the 409 itself and wants the question asked anyway. */
  function offerGpu(payload, retry) {
    const detail = payload && payload.gpu ? payload.gpu : payload;
    if (!detail || detail.decision !== 'ask') return false;
    void openGpuModal(detail, choice => {
      if (choice && typeof retry === 'function') retry(choice);
    });
    return true;
  }


  async function getJson(url) {
    const response = await fetch(url, { cache: 'no-store', credentials: 'same-origin' });
    const result = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error((result && result.error && result.error.message) || ('HTTP ' + response.status));
    }
    return result;
  }

  async function postJson(url, body) {
    const response = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}), cache: 'no-store', credentials: 'same-origin',
    });
    const result = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error((result && result.error && result.error.message) || ('HTTP ' + response.status));
    }
    return result;
  }


  // Declared before the export that exposes it: referencing it from the object literal
  // above its `const` is a temporal-dead-zone throw that kills the whole module.
  const diagnostics = { mountedAt: null, readyStateAtMount: null, micAttached: false };

  root.QwenVoice = {
    attachTo,
    insertText,
    captureCaret,
    acceptVoice,
    openGpuModal,
    offerGpu,
    gpuGate,
    diagnostics,
    gpuStatus: () => getJson(GPU_STATUS_URL),
    preempt: mode => postJson(GPU_PREEMPT_URL, { mode: mode }),
    release: () => postJson(GPU_RELEASE_URL, {}),
    voiceStatus: () => getJson(VOICE_STATUS_URL),
    state,
  };

  /* Re-measure at most once per frame.
   *
   * The hosting app mounts by mutating thousands of nodes, and reading
   * getBoundingClientRect() on every mutation forces a synchronous layout each time —
   * measured as a frozen renderer on this page (navigation stopped responding entirely).
   * Coalescing through requestAnimationFrame keeps the button glued to the field for free,
   * because the button only moves when the field does. */
  let pending = false;
  function scheduleMicPosition() {
    if (pending) return;
    pending = true;
    const run = () => { pending = false; positionMic(); };
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(run);
    else setTimeout(run, 16);
  }

  function mount() {
    document.body.append(mic);
    diagnostics.micAttached = true;
    const observer = new MutationObserver(scheduleMicPosition);
    // childList only: the app's per-keystroke class/style churn is not a reason to
    // re-measure, and watching attributes is what made this expensive.
    observer.observe(document.body, { childList: true, subtree: true });
    diagnostics.mountedAt = new Date().toISOString();
    diagnostics.readyStateAtMount = document.readyState;
    // window.fetch is deliberately left alone: the chat app assigns it while booting, and
    // taking that away is what blanked the panel.
    diagnostics.fetchUntouched = typeof root.fetch === 'function';
    positionMic();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount, { once: true });
  } else {
    mount();
  }
})(typeof window !== 'undefined' ? window : globalThis);
