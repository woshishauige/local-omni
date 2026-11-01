/* Realtime voice conversation mode.
 *
 * A microphone button that turns speech into a chat turn: the clip is streamed to
 * the gateway's existing on-device recogniser, the transcript is sent to whichever
 * model the toolbar has loaded, and the reply is spoken back. It reuses the voice
 * endpoints the dictation button already depends on rather than adding a second
 * audio path, so there is one recogniser and one place its behaviour is defined.
 *
 * Speech output prefers the gateway's synthesised audio when it is available and
 * falls back to the browser's own voice, which keeps the mode usable while a TTS
 * backend is still being brought up.
 */
(() => {
  "use strict";

  if (document.getElementById("local-voice-panel")) return;

  const STREAM_URL = "/local/voice/stream";
  const FINISH_URL = "/local/voice/stream/finish";
  const CANCEL_URL = "/local/voice/cancel";
  const TRANSCRIBE_URL = "/local/voice/transcribe";
  const TTS_URL = "/local/voice/speak";
  // Voice turns go through the gateway's locked voice route rather than the chat
  // endpoint: that route re-checks the profile server-side, so a stale page cannot
  // send a spoken turn to the slower model.
  const CHAT_URL = "/local/voice/chat";
  const MODE_URL = "/local/voice/mode";
  const CONFIG_URL = "/local/voice/config";
  const STATUS_URL = "/local/status";

  // 250 ms matches the dictation path; the recogniser re-reads a short tail each
  // time, so a longer window would only add latency without improving accuracy.
  const CHUNK_MS = 250;
  const TARGET_RATE = 16000;
  const LANGUAGE = "zh";
  const SILENCE_RMS = 300;
  // How long a pause ends the turn. 1.1 s cost half a second on every reply for a
  // margin a normal speaker does not need; 0.8 s still tolerates a breath between
  // clauses. Raise it if mid-sentence pauses start cutting you off.
  const SILENCE_HOLD_MS = 800;
  const MIN_SPEECH_MS = 350;      // ignore taps and coughs
  const MAX_TURN_MS = 60000;

  const VOICES = [
    ["uncle_fu", "傅叔", "男声"],
    ["serena", "Serena", "女声"],
    ["vivian", "Vivian", "女声"],
    ["ryan", "Ryan", "男声"],
    ["aiden", "Aiden", "男声"],
    ["ono_anna", "Anna", "女声"],
    ["sohee", "Sohee", "女声"],
    ["eric", "Eric", "四川话"],
    ["dylan", "Dylan", "北京话"],
  ];

  const state = {
    open: false,
    recording: false,
    busy: false,
    streamId: null,
    chunks: [],
    sending: false,
    queued: 0,
    speechMs: 0,
    silenceMs: 0,
    startedAt: 0,
    voice: localStorage.getItem("qwen.voice.speaker") || "uncle_fu",
    // A natural-language delivery instruction, sent with every sentence. Empty means "let the
    // engine choose", which is the honest default: the styles are suggestions, not settings the
    // engine needs.
    style: localStorage.getItem("qwen.voice.style") || "",
    autoSend: localStorage.getItem("qwen.voice.autosend") !== "0",
    speakReplies: localStorage.getItem("qwen.voice.speak") !== "0",
    turns: [],
    history: [],
    // The conversation this visit is using, and whether anything was said in it. A visit that
    // ends without a turn is cleaned up rather than left as an empty row in the history rail.
    conversation: "",
    spokeThisVisit: false,
    // True right after "清空全部": the owner asked for an empty history, so nothing is
    // registered until a turn is actually sent.
    historyCleared: false,
    // Set while this visit's empty conversation is being deleted, so the next visit waits for
    // it instead of registering a second one.
    forgetting: null,
    // Settings (音色 / 选项 / 运行状态) live in a drawer of their own. They used to share the
    // left column with the history list, which is what made the rail read as a jumble.
    settings: false,
    // The window voice mode is locked to, read from /local/voice/config. Held so the page can
    // describe the window a spoken turn will run with even while the backend in front of it is
    // still serving text — the number is a decision, not a property of whatever is loaded.
    voiceWindow: 0,
    // Whether the history rail is showing. Persisted, because a rail the owner collapsed is a
    // rail they want collapsed next time too.
    railOpen: localStorage.getItem("qwen.voice.rail") !== "0",
  };

  let media = null, recorder = null, analyser = null, rafId = 0, tickId = 0;
  let audioCtx = null, currentAudio = null, lastStatus = null;

  // ---------------------------------------------------------------- helpers

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function icon(name) {
    const paths = {
      mic: [["path", { d: "M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" }],
            ["path", { d: "M19 10v1a7 7 0 0 1-14 0v-1" }], ["line", { x1: "12", y1: "19", x2: "12", y2: "22" }]],
      stop: [["rect", { x: "6", y: "6", width: "12", height: "12", rx: "2" }]],
      close: [["line", { x1: "6", y1: "6", x2: "18", y2: "18" }], ["line", { x1: "18", y1: "6", x2: "6", y2: "18" }]],
      speaker: [["path", { d: "M11 5 6 9H2v6h4l5 4V5z" }], ["path", { d: "M15.5 8.5a5 5 0 0 1 0 7" }]],
      mute: [["path", { d: "M11 5 6 9H2v6h4l5 4V5z" }], ["line", { x1: "22", y1: "9", x2: "16", y2: "15" }], ["line", { x1: "16", y1: "9", x2: "22", y2: "15" }]],
      spinner: [["path", { d: "M21 12a9 9 0 1 1-6.219-8.56" }]],
      // Settings live in their own surface now (see .local-voice-settings), so the header
      // needs a way in that reads as "settings" rather than as another action button.
      gear: [["circle", { cx: "12", cy: "12", r: "3.1" }],
             ["path", { d: "M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1.03 1.56V21a2 2 0 1 1-4 0v-.09A1.7 1.7 0 0 0 8.9 19.3a1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.56-1.03H3a2 2 0 1 1 0-4h.09A1.7 1.7 0 0 0 4.7 8.9a1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1.03-1.56V3a2 2 0 1 1 4 0v.09A1.7 1.7 0 0 0 15 4.7a1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.7 1.7 0 0 0 19.4 9v.09A1.7 1.7 0 0 0 20.96 10H21a2 2 0 1 1 0 4h-.09A1.7 1.7 0 0 0 19.4 15z" }]],
      trash: [["path", { d: "M4 7h16" }], ["path", { d: "M9 7V5.5A1.5 1.5 0 0 1 10.5 4h3A1.5 1.5 0 0 1 15 5.5V7" }],
              ["path", { d: "M6.5 7l.8 11.2A2 2 0 0 0 9.3 20h5.4a2 2 0 0 0 2-1.8L17.5 7" }],
              ["line", { x1: "10.5", y1: "11", x2: "10.5", y2: "16.5" }],
              ["line", { x1: "13.5", y1: "11", x2: "13.5", y2: "16.5" }]],
      plus: [["line", { x1: "12", y1: "5", x2: "12", y2: "19" }], ["line", { x1: "5", y1: "12", x2: "19", y2: "12" }]],
      pencil: [["path", { d: "M4 20h4l10-10a2.8 2.8 0 0 0-4-4L4 16v4z" }],
               ["line", { x1: "13.5", y1: "6.5", x2: "17.5", y2: "10.5" }]],
      chevron: [["path", { d: "M15 5l-7 7 7 7" }]],
    };
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.9");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    for (const [tag, attrs] of paths[name] || []) {
      const part = document.createElementNS(svg.namespaceURI, tag);
      for (const [k, v] of Object.entries(attrs)) part.setAttribute(k, v);
      svg.append(part);
    }
    return svg;
  }

  async function json(url, options) {
    const response = await fetch(url, options);
    let body = null;
    try { body = await response.json(); } catch (failure) { body = null; }
    if (!response.ok) {
      const message = (body && (body.detail || body.error)) || `HTTP ${response.status}`;
      const error = new Error(typeof message === "string" ? message : `HTTP ${response.status}`);
      // Kept on the error so a caller can act on the payload (the switch explanation, a
      // GPU verdict) instead of only printing the message.
      error.payload = body;
      error.status = response.status;
      throw error;
    }
    return body;
  }

  // ---------------------------------------------------------------- markup

  const launch = el("button", "local-voice-launch");
  launch.type = "button";
  launch.setAttribute("aria-pressed", "false");
  launch.title = "语音对话";
  launch.append(icon("mic"), el("span", "", "语音对话"), el("span", "local-voice-launch-dot"));

  const panel = el("section", "local-voice-panel");
  panel.id = "local-voice-panel";
  panel.hidden = true;
  panel.setAttribute("role", "region");
  panel.setAttribute("aria-label", "语音对话");

  const top = el("header", "local-voice-top");
  const title = el("div", "local-voice-title");
  const titleMain = el("strong", "", "语音对话");
  const titleSub = el("span", "", "在本机识别与合成");
  title.append(titleMain, titleSub);

  /* The three numbers that decide whether a spoken turn will work, on the header where they
   * are read at a glance. The same values are listed in full in the settings drawer, which is
   * where 合成 and 上轮耗时 live too — the header only carries what a turn needs. */
  const statusRow = el("div", "local-voice-status");
  const chips = {};
  for (const [key, label] of [["asr", "识别"], ["llm", "模型"], ["context", "上下文余量"]]) {
    const chip = el("span", "local-voice-chip");
    const value = el("span", "local-voice-chip-value", "—");
    chip.append(el("em", "", label), value);
    statusRow.append(chip);
    chips[key] = value;
  }

  const settingsBtn = el("button", "local-voice-settings-toggle");
  settingsBtn.type = "button";
  settingsBtn.setAttribute("aria-expanded", "false");
  settingsBtn.setAttribute("aria-controls", "local-voice-settings");
  settingsBtn.title = "音色与选项";
  settingsBtn.append(icon("gear"), el("span", "", "设置"));
  const closeBtn = el("button", "local-voice-close", "关闭");
  closeBtn.type = "button";
  const topActions = el("div", "local-voice-top-actions");
  topActions.append(settingsBtn, closeBtn);
  top.append(title, statusRow, topActions);

  const body = el("div", "local-voice-body");
  const log = el("div", "local-voice-log");
  log.setAttribute("role", "log");
  log.setAttribute("aria-live", "polite");
  const empty = el("div", "local-voice-empty");
  const emptyMark = el("span", "local-voice-empty-mark");
  emptyMark.append(icon("mic"));
  empty.append(emptyMark,
               el("strong", "", "按住麦克风开始说话"),
               el("span", "", "说完自动停止并发送 · 识别、思考、朗读全部在这台机器上完成"));
  log.append(empty);

  /* --- the left rail is the history, and nothing else ------------------------------
   *
   * It used to carry four unrelated blocks in one 244px column — 对话历史, 音色 (nine chips),
   * 运行状态 and 选项 — and they had to share the height between them. Measured in a real
   * browser: the history *group* came out 0px tall and its seven rows were squeezed to 13px
   * slivers, so the titles were invisible behind the voice chips. Owner's words: "历史对话和
   * 音色还混在一起，排版这么乱".
   *
   * So the column got one job. Everything that is a setting moved to the drawer
   * (.local-voice-settings) behind the 设置 button, and the three numbers a spoken turn
   * depends on moved up into the header.
   */
  const side = el("aside", "local-voice-side");
  const railHead = el("div", "local-voice-rail-head");
  const railTitle = el("div", "local-voice-rail-title");
  const historyCount = el("span", "local-voice-count", "0");
  railTitle.append(el("h3", "", "对话历史"), historyCount);
  // Collapsing is the rail's own control, in its own header: the one place that is always
  // reachable while the rail is open, and the strip below is the way back.
  const railToggle = el("button", "local-voice-rail-toggle");
  railToggle.type = "button";
  railToggle.append(icon("chevron"));
  railToggle.addEventListener("click", () => setRail(false));
  railTitle.append(railToggle);
  const newButton = el("button", "local-voice-link is-primary");
  newButton.type = "button";
  newButton.append(icon("plus"), el("span", "", "新对话"));
  newButton.addEventListener("click", () => startNewConversation());
  const clearButton = el("button", "local-voice-link is-danger", "清空全部");
  clearButton.type = "button";
  clearButton.title = "删除所有对话记录（含本机保存的语音条）";
  clearButton.addEventListener("click", () => void clearAllConversations());
  const headActions = el("div", "local-voice-history-actions");
  headActions.append(newButton, clearButton);
  // A history that is worth keeping is a history that gets long. Filtering is client-side: the
  // whole list is already in memory, so typing narrows the rows without another round trip.
  const searchBox = el("input", "local-voice-search");
  searchBox.type = "search";
  searchBox.placeholder = "搜索对话标题";
  searchBox.setAttribute("aria-label", "搜索对话");
  searchBox.autocomplete = "off";
  searchBox.addEventListener("input", () => renderConversations(lastConversations));
  railHead.append(railTitle, headActions, searchBox);
  const historyList = el("div", "local-voice-history-list");
  historyList.setAttribute("role", "listbox");
  historyList.setAttribute("aria-label", "对话历史");
  // What is left of the rail once it is collapsed: a way back and the count, so "where did my
  // history go" is answered by looking at it rather than by hunting for a control.
  const railStrip = el("div", "local-voice-rail-strip");
  const stripToggle = el("button", "local-voice-rail-toggle is-strip");
  stripToggle.type = "button";
  stripToggle.append(icon("chevron"));
  stripToggle.addEventListener("click", () => setRail(true));
  const stripCount = el("span", "local-voice-count", "0");
  railStrip.append(stripToggle, stripCount);
  side.append(railHead, historyList, railStrip);
  // Restore the rail to however the owner left it (see state.railOpen).
  setRail(state.railOpen);

  /* --- settings drawer -------------------------------------------------------------
   * 音色 / 选项 / 运行状态, one surface each, opened from the header. Kept inside the panel so
   * it is still one screen, and closed by the gear, its own ×, the scrim or Escape.
   */
  const settings = el("aside", "local-voice-settings");
  settings.id = "local-voice-settings";
  settings.hidden = true;
  const settingsHead = el("div", "local-voice-settings-head");
  settingsHead.append(el("h2", "", "设置"), el("span", "", "音色与选项"));
  const settingsClose = el("button", "local-voice-settings-close");
  settingsClose.type = "button";
  settingsClose.setAttribute("aria-label", "关闭设置");
  settingsClose.title = "关闭设置";
  settingsClose.append(icon("close"));
  settingsHead.append(settingsClose);

  const voiceGroup = el("section", "local-voice-group");
  voiceGroup.append(el("h3", "", "音色"));
  const voiceBox = el("div", "local-voice-voices");
  const voiceButtons = {};

  /* The list of speakers belongs to the gateway (`GET /local/voice/voices`): one added or renamed
     there has to appear without editing this file, and one removed there has to stop being
     offered — otherwise the panel advertises a voice that only fails once someone speaks with it.
     The VOICES table above is the fallback for a gateway that cannot answer, which is also why it
     stays: it is the list this panel shows offline, not a second source of truth. */
  function buildVoiceButtons() {
    voiceBox.replaceChildren();
    for (const key of Object.keys(voiceButtons)) delete voiceButtons[key];
    for (const [id, label, kind] of VOICES) {
      const button = el("button", "local-voice-voice");
      button.type = "button";
      button.append(el("span", "", label), el("small", "", kind));
      button.setAttribute("aria-pressed", "false");
      button.addEventListener("click", () => { state.voice = id; savePrefs(); renderVoices(); });
      voiceBox.append(button);
      voiceButtons[id] = button;
    }
    renderVoices();
  }

  async function syncVoices() {
    try {
      const response = await fetch("/local/voice/voices",
        { cache: "no-store", signal: AbortSignal.timeout(8000) });
      if (!response.ok) return;
      const payload = await response.json();
      const rows = Array.isArray(payload && payload.voices) ? payload.voices : [];
      const next = rows
        .map(voice => [String((voice && voice.id) || ""),
                       String((voice && (voice.label || voice.id)) || ""),
                       String((voice && voice.kind) || "")])
        .filter(row => row[0]);
      if (!next.length) return;
      if (JSON.stringify(next) === JSON.stringify(VOICES)) return;
      VOICES.length = 0;
      VOICES.push(...next);
      // A saved voice that no longer exists would otherwise be sent to a synthesiser that has
      // never heard of it; fall back to the first one the gateway does offer.
      if (!VOICES.some(row => row[0] === state.voice)) { state.voice = VOICES[0][0]; savePrefs(); }
      buildVoiceButtons();
    } catch { /* keep the fallback list */ }
  }

  buildVoiceButtons();
  voiceGroup.append(voiceBox);

  /* 语气 —— the engine's own render instruction, which this app could not reach before.
   *
   * `custom(..., instruct=...)` renders the string into the prompt as a user turn, so delivery
   * changes the way the model was trained to change it rather than by post-processing the audio.
   * The worker had it hardcoded to None, which is why every reply came out in the same flat read.
   */
  const styleGroup = el("section", "local-voice-group");
  styleGroup.append(el("h3", "", "语气"));
  const STYLES = [
    ["", "默认", "不作额外要求"],
    ["用温柔、亲切的语气说，语速稍慢", "温柔", "放慢、柔和"],
    ["用平稳、克制的语气说，像在陈述事实", "平静", "冷静、少起伏"],
    ["用轻快、有活力的语气说，带一点笑意", "轻快", "明快、有精神"],
    ["用严肃、有力的语气说，重点处加重", "严肃", "郑重、强调"],
    ["用很小的声音、贴近耳边的方式说，气息放轻", "耳语", "轻气息、低音量"],
  ];
  const styleBox = el("div", "local-voice-styles");
  const styleButtons = {};
  for (const [value, label, hint] of STYLES) {
    const button = el("button", "local-voice-style");
    button.type = "button";
    button.append(el("span", "", label), el("small", "", hint));
    button.addEventListener("click", () => { state.style = value; savePrefs(); renderStyles(); });
    styleBox.append(button);
    styleButtons[value] = button;
  }
  const styleInput = el("input", "local-voice-style-input");
  styleInput.type = "text";
  styleInput.maxLength = 80;
  styleInput.placeholder = "也可以自己写一句，例如：像在跟老朋友闲聊";
  // Committed on change rather than on every keystroke, so a half-typed sentence is never sent
  // to the engine as an instruction.
  styleInput.addEventListener("change", () => {
    const value = styleInput.value.trim();
    if (!STYLES.some(([preset]) => preset === value)) { state.style = value; savePrefs(); renderStyles(); }
  });
  function renderStyles() {
    for (const [value, button] of Object.entries(styleButtons)) {
      button.setAttribute("aria-pressed", String(value === state.style));
    }
    styleInput.value = STYLES.some(([preset]) => preset === state.style) ? "" : state.style;
  }
  styleGroup.append(styleBox, styleInput);
  renderStyles();

  /* 克隆音色 —— your own recording, used as the voice.
   *
   * The engine's anchor needs the reference's own words as well as its sound, so the recording
   * and its transcript are sent together and the engine is asked whether it can use them before
   * the voice is saved. A reference it cannot use is then refused once, here, instead of failing
   * every sentence of the next reply.
   */
  const cloneGroup = el("section", "local-voice-group");
  cloneGroup.append(el("h3", "", "克隆音色"));
  const cloneFile = el("input", "local-voice-clone-file");
  cloneFile.type = "file";
  cloneFile.accept = "audio/*,.wav,.mp3,.m4a,.flac,.ogg";
  const cloneName = el("input", "local-voice-clone-input");
  cloneName.type = "text";
  cloneName.maxLength = 20;
  cloneName.placeholder = "名字（可留空）";
  const cloneText = el("input", "local-voice-clone-input");
  cloneText.type = "text";
  cloneText.maxLength = 120;
  cloneText.placeholder = "这段录音里说的是什么（必填，几个字也行）";
  const cloneAdd = el("button", "local-voice-action");
  cloneAdd.type = "button";
  cloneAdd.append(el("span", "", "添加为音色"),
                  el("small", "", "用你自己的录音当音色，说一句话就够"));
  cloneAdd.addEventListener("click", () => void registerClone());
  cloneGroup.append(cloneFile, cloneName, cloneText, cloneAdd);

  async function registerClone() {
    const file = cloneFile.files && cloneFile.files[0];
    const said = cloneText.value.trim();
    if (!file) { showError("先选一个录音文件"); return; }
    if (!said) { showError("请填上这段录音里说的话：克隆需要参考音频和它的内容"); return; }
    cloneAdd.setAttribute("aria-busy", "true");
    showError("");
    try {
      const query = new URLSearchParams({ name: cloneName.value.trim(), text: said });
      const response = await fetch("/local/voice/voice/clone?" + query.toString(),
                                   { method: "POST", body: file });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
      cloneFile.value = "";
      cloneName.value = "";
      cloneText.value = "";
      await syncVoices();
      state.voice = payload.id;
      savePrefs();
      renderVoices();
      hint.textContent = `已添加音色「${payload.label}」`;
    } catch (failure) {
      showError("克隆失败：" + (failure.message || failure));
    } finally {
      cloneAdd.removeAttribute("aria-busy");
    }
  }

  const toggleGroup = el("section", "local-voice-group");
  toggleGroup.append(el("h3", "", "选项"));
  const autoBtn = el("button", "local-voice-action");
  autoBtn.type = "button";
  autoBtn.append(el("span", "", "自动发送"), el("small", "", "说完停顿片刻就发出去，不用再点一次"));
  autoBtn.addEventListener("click", () => { state.autoSend = !state.autoSend; savePrefs(); renderToggles(); });
  const speakBtn = el("button", "local-voice-action");
  speakBtn.type = "button";
  speakBtn.append(el("span", "", "朗读回复"), el("small", "", "回答在这里读出来，不只显示文字"));
  speakBtn.addEventListener("click", () => { state.speakReplies = !state.speakReplies; savePrefs(); renderToggles(); });
  toggleGroup.append(autoBtn, speakBtn);

  const factsGroup = el("section", "local-voice-group");
  factsGroup.append(el("h3", "", "运行状态"));
  const facts = el("dl", "local-voice-facts");
  const factRows = {};
  // "上下文余量" is the shared meter: the same number the chat side's compressor works from,
  // so a spoken turn cannot quietly fill a window the typed thread thinks is empty.
  for (const [key, label] of [["asr", "识别"], ["llm", "模型"], ["tts", "合成"],
                              ["context", "上下文余量"], ["last", "上轮耗时"]]) {
    const row = el("div", "");
    const value = el("dd", "", "—");
    row.append(el("dt", "", label), value);
    facts.append(row);
    // Hold the element directly rather than reading it back off the row: `lastChild`
    // is not dependable across DOM implementations, and a missing row would throw on
    // every status refresh.
    factRows[key] = value;
  }
  factsGroup.append(facts);
  settings.append(settingsHead, voiceGroup, styleGroup, cloneGroup, toggleGroup, factsGroup);

  const scrim = el("div", "local-voice-scrim");
  scrim.hidden = true;

  const main = el("div", "local-voice-main");
  main.append(log);

  const dock = el("div", "local-voice-dock");
  const dockInner = el("div", "local-voice-dock-inner");
  const heard = el("div", "local-voice-heard", "");
  const controls = el("div", "local-voice-controls");
  const mic = el("button", "local-voice-mic");
  mic.type = "button";
  mic.setAttribute("aria-label", "开始录音");
  mic.append(icon("mic"));
  const levelBar = el("div", "local-voice-level");
  const levelFill = el("i", "");
  levelBar.append(levelFill);
  const hint = el("div", "local-voice-hint", "点击麦克风开始 · 说完停顿片刻自动结束");

  // Typing is a first-class way in, not a fallback: a quiet room, a sore throat or a
  // meeting are all reasons a spoken turn is impossible, and the reply is spoken back
  // either way. The input and the level line share one field, so the meter sits under the
  // text it belongs to instead of floating between the microphone and the composer.
  const composer = el("form", "local-voice-composer");
  const field = el("div", "local-voice-field");
  const input = el("input", "local-voice-input");
  input.type = "text";
  input.placeholder = "也可以直接打字，回车发送";
  input.setAttribute("aria-label", "输入文字");
  input.autocomplete = "off";
  const send = el("button", "local-voice-send", "发送");
  send.type = "submit";
  field.append(input, levelBar);
  composer.append(field, send);
  composer.addEventListener("submit", event => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text || state.busy) return;
    input.value = "";
    void ask(text);
  });
  controls.append(mic, composer);
  dockInner.append(heard, controls, hint);
  dock.append(dockInner);
  main.append(dock);

  const errorBox = el("div", "local-voice-error");
  errorBox.hidden = true;

  body.append(side, main);
  panel.append(top, errorBox, body, scrim, settings);

  function savePrefs() {
    try {
      localStorage.setItem("qwen.voice.speaker", state.voice);
      localStorage.setItem("qwen.voice.style", state.style);
      localStorage.setItem("qwen.voice.autosend", state.autoSend ? "1" : "0");
      localStorage.setItem("qwen.voice.speak", state.speakReplies ? "1" : "0");
    } catch (failure) { /* storage may be blocked; the mode still works */ }
  }

  function renderVoices() {
    for (const [id, button] of Object.entries(voiceButtons)) {
      button.setAttribute("aria-pressed", String(id === state.voice));
    }
  }

  function renderToggles() {
    autoBtn.setAttribute("aria-pressed", String(state.autoSend));
    speakBtn.setAttribute("aria-pressed", String(state.speakReplies));
  }

  /* One value, two places: the full list in the settings drawer and the short chip in the
   * header. Five writers (config, meter, turn timing, first-token time, synthesis) used to
   * each poke `factRows` directly, so a second copy of the same number would have drifted. */
  function setFact(key, text) {
    const row = factRows[key];
    if (row) row.textContent = text;
    const chip = chips[key];
    if (chip) chip.textContent = text;
  }

  function showError(message) {
    errorBox.textContent = message || "";
    errorBox.hidden = !message;
  }

  // The server's list as last fetched, so the search box can re-render the rail without a
  // round trip (and without losing the rows while a request is in flight).
  let lastConversations = [];
  // Set while a live turn is on screen, so the streaming code can update the placeholder
  // without rebuilding the whole log on every delta.
  let liveNode = null;

  // One renderer for the whole log. A turn is either a user line, a spoken reply (which
  // renders as a voice bar and keeps its words behind 转文字), or an in-flight reply.
  // Keeping all three in one place is what stops a turn from being drawn twice: the old
  // code appended a streaming bubble by hand, swapped in a bar, and then cleared the log
  // and rebuilt every turn as text - which threw the bar away and showed the reply again.
  function renderTurns() {
    if (state.turns.length === 0) {
      // The log is cleared before showing the prompt, not just left as it was: switching to an
      // empty conversation must remove the previous one from the screen. Without this the
      // "new conversation" control emptied the state and the panel still showed the old turns,
      // which reads as the button having done nothing.
      // `replaceChildren()` rather than `textContent = ""`: both empty the node, but the
      // DOM-stub used by check_voice_panel.cjs only implements the former, so with
      // `textContent` the empty-state node stayed in the stub's tree *and* was appended again —
      // the stub then reported two empty states and the check failed on a non-bug.
      log.replaceChildren();
      liveNode = null;
      log.append(empty);
      return;
    }
    empty.remove();
    log.replaceChildren();
    liveNode = null;
    for (const turn of state.turns) {
      const row = el("div", "local-voice-turn is-" + turn.role);
      row.append(el("div", "local-voice-avatar", turn.role === "user" ? "我" : "AI"));
      const column = el("div", "");
      // Which turns are worth a bar: one that already holds its audio, one whose recording was
      // stored under its own id a moment ago, and any turn the ledger says was spoken. A turn
      // with nothing to play stays words - a bar whose play button does nothing is worse than
      // no bar, and the bar itself falls back to the words when its clip turns out to be absent.
      const spoken = Boolean(turn.segments && turn.segments.length)
        || Boolean(turn.clipId) || turn.mode === "voice";
      if (turn.live) {
        liveNode = el("div", "local-voice-bubble is-live", turn.live);
        column.append(liveNode);
      } else if (spoken) {
        column.append(createVoiceBar(turn));
      } else {
        column.append(el("div", "local-voice-bubble", turn.text));
      }
      if (turn.meta) column.append(el("div", "local-voice-state", turn.meta));
      row.append(column);
      log.append(row);
    }
    log.scrollTop = log.scrollHeight;
  }

  // ---------------------------------------------------------------- audio in

  function pickMime() {
    const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];
    for (const type of candidates) {
      if (window.MediaRecorder && MediaRecorder.isTypeSupported(type)) return type;
    }
    return "";
  }

  async function startRecording() {
    if (state.recording || state.busy) return;
    showError("");
    // The microphone is the point of commitment: this is where the card is swapped, and
    // the only path that needs the recogniser.
    if (!(await activateVoiceMode("voice"))) return;
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (failure) {
      showError("无法访问麦克风：" + (failure.message || failure.name) +
                "（请通过 https 或 127.0.0.1 打开本页）");
      return;
    }
    media = stream;
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const source = audioCtx.createMediaStreamSource(stream);
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 1024;
    source.connect(analyser);

    state.recording = true;
    state.chunks = [];
    state.sending = false;
    state.queued = 0;
    state.speechMs = 0;
    state.silenceMs = 0;
    state.startedAt = Date.now();
    heard.textContent = "";
    heard.classList.add("is-live");
    mic.dataset.recording = "true";
    mic.setAttribute("aria-label", "停止录音");
    mic.textContent = "";
    mic.append(icon("stop"));
    panel.dataset.phase = "listening";
    hint.textContent = "正在聆听… 说完停顿片刻自动结束";

    // Recognition runs on 250 ms slices through the streaming endpoint; the
    // server keeps the rolling tail, so a lost chunk degrades one reading rather
    // than the whole turn.
    const mime = pickMime();
    recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    recorder.ondataavailable = event => {
      if (!event.data || !event.data.size) return;
      state.chunks.push(event.data);
      void pump();
    };
    recorder.start(CHUNK_MS);

    try {
      const started = await json(STREAM_URL + `?language=${encodeURIComponent(LANGUAGE)}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
      });
      state.streamId = started && (started.stream_id || started.id || null);
    } catch (failure) {
      state.streamId = null;
    }

    measure();
    tickId = window.setInterval(pollHeard, 400);
  }

  function measure() {
    if (!analyser) return;
    const buffer = new Uint8Array(analyser.fftSize);
    const step = () => {
      if (!state.recording) return;
      analyser.getByteTimeDomainData(buffer);
      let sum = 0;
      for (let i = 0; i < buffer.length; i += 1) {
        const v = (buffer[i] - 128) / 128;
        sum += v * v;
      }
      const rms = Math.sqrt(sum / buffer.length) * 32768;
      levelFill.style.width = Math.min(100, (rms / 3000) * 100).toFixed(1) + "%";
      if (rms > SILENCE_RMS) {
        state.speechMs += 50;
        state.silenceMs = 0;
      } else if (state.speechMs > MIN_SPEECH_MS) {
        state.silenceMs += 50;
        if (state.silenceMs >= SILENCE_HOLD_MS) { void stopRecording(true); return; }
      }
      if (Date.now() - state.startedAt > MAX_TURN_MS) { void stopRecording(true); return; }
      rafId = window.setTimeout(step, 50);
    };
    step();
  }

  async function pump() {
    if (!state.streamId || state.sending) return;
    const blob = state.chunks.shift();
    if (!blob) return;
    state.sending = true;
    try {
      const buffer = await blob.arrayBuffer();
      await fetch(STREAM_URL, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream", "X-Voice-Stream": state.streamId },
        body: buffer,
      });
    } catch (failure) {
      /* a dropped slice is recovered by the next reading */
    } finally {
      state.sending = false;
      if (state.chunks.length) void pump();
    }
  }

  async function pollHeard() {
    if (!state.streamId) return;
    try {
      const status = await json(`/local/voice/status?stream=${encodeURIComponent(state.streamId)}`);
      const text = status && (status.text || status.transcript);
      if (text) heard.textContent = text;
    } catch (failure) { /* polling is best effort */ }
  }

  async function stopRecording(auto) {
    if (!state.recording) return;
    state.recording = false;
    window.clearInterval(tickId);
    window.clearTimeout(rafId);
    mic.dataset.recording = "false";
    heard.classList.remove("is-live");
    levelFill.style.width = "0%";
    if (recorder && recorder.state !== "inactive") {
      try { recorder.stop(); } catch (failure) { /* already stopped */ }
    }
    if (media) { media.getTracks().forEach(track => track.stop()); media = null; }
    analyser = null;

    const elapsed = Date.now() - state.startedAt;
    if (!auto || elapsed < MIN_SPEECH_MS || state.speechMs < MIN_SPEECH_MS) {
      panel.dataset.phase = "";
      hint.textContent = "点击麦克风开始 · 说完停顿片刻自动结束";
      return;
    }
    panel.dataset.phase = "thinking";
    hint.textContent = "正在识别…";
    /* The recording of what the microphone heard, taken before recognition consumes the
     * chunks: MediaRecorder's own output is the only copy of the owner's voice, and its WebM
     * container is playable as-is. The in-flight transcript in `heard` is not part of it. */
    const recording = state.chunks.length
      ? new Blob(state.chunks.slice(), { type: "audio/webm" })
      : null;
    const transcript = await finishRecognition();
    if (!transcript) {
      panel.dataset.phase = "";
      hint.textContent = "没有听清，请再说一次";
      return;
    }
    heard.textContent = transcript;
    if (state.autoSend) {
      await ask(transcript, { clip: recording });
    } else {
      panel.dataset.phase = "";
      hint.textContent = "已放入输入框，确认后发送";
      window.dispatchEvent(new CustomEvent("qwen-voice-transcript", { detail: { text: transcript } }));
    }
  }

  async function finishRecognition() {
    const id = state.streamId;
    state.streamId = null;
    try {
      if (id) {
        const done = await json(FINISH_URL, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ stream_id: id }),
        });
        const text = done && (done.text || done.transcript);
        if (text && text.trim()) return text.trim();
      }
    } catch (failure) { /* fall through to the one-shot path */ }

    // One-shot fallback: if the streaming endpoint gave nothing usable, send the
    // whole recording through the plain transcription route.
    if (!state.chunks.length) return "";
    try {
      const blob = new Blob(state.chunks, { type: "audio/webm" });
      const result = await json(TRANSCRIBE_URL + `?language=${encodeURIComponent(LANGUAGE)}`, {
        method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: blob,
      });
      return (result && (result.text || result.transcript || "")).trim();
    } catch (failure) {
      showError("识别失败：" + (failure.message || failure));
      return "";
    }
  }

  // ---------------------------------------------------------------- dialogue

  async function ask(text, options) {
    if (!text || state.busy) return;
    // A turn — typed or spoken — is what makes this visit's conversation worth keeping, and it
    // is also the moment a cleared history gets its next conversation.
    state.spokeThisVisit = true;
    // When the owner asked. The "首音" the turn reports is measured from here rather than from the
    // start of the stream, because the wait the owner actually feels begins the moment they send.
    state.turnStartedAt = performance.now();
    if (!state.conversation) state.conversation = conversationId();
    // Claimed before the await, not after: activateVoiceMode() can take seconds on the
    // first turn, and two entries through that gap would put two turns in flight and
    // speak the same reply twice.
    state.busy = true;
    // Typing is also a commitment: the reply has to come from the locked 9B. It is not a
    // spoken turn, so it asks for the language model only.
    if (!(await activateVoiceMode("text"))) { state.busy = false; return; }
    const turn = { role: "user", text };
    // What was actually said, when this turn came from the microphone: the transcript alone
    // loses the delivery, and this is the recording the history can replay.
    const clip = options && options.clip;
    if (clip) {
      turn.clipId = clipIdFor(turn);
      void storeClip(turn.clipId, clip);
    }
    state.turns.push(turn);
    // Filed by the gateway when the turn is sent, together with the answer: the identifier is
    // what ties this spoken turn to the same thread the chat app writes to.
    renderTurns();
    // Invalidates every queued sentence and in-flight render from the previous turn.
    // Without this the old reply keeps playing underneath the new one, which sounds
    // exactly like the same answer being repeated.
    stopSpeaking();
    panel.dataset.phase = "thinking";
    hint.textContent = "正在思考…";

    const started = Date.now();
    try {
      await askStreaming(text);
    } catch (failure) {
      showError("对话失败：" + (failure.message || failure));
      state.turns.push({ role: "assistant", text: "（出错了）", meta: String(failure.message || failure) });
      renderTurns();
    } finally {
      state.busy = false;
      panel.dataset.phase = "";
      hint.textContent = "点击麦克风开始 · 说完停顿片刻自动结束";
    }
  }

  // Reads the reply as server-sent events and speaks each sentence as soon as it is
  // complete. The first clause therefore reaches the speaker while the model is still
  // writing the rest, which is where most of the perceived latency used to go.

  /* ---------------------------------------------------------------- voice clips
   *
   * The history used to be text only, because the spoken audio was played once and dropped.
   * These keep it: each turn's recording (what was said, and what was spoken back) is stored
   * in IndexedDB, which survives a reload and holds megabytes without the localStorage quota
   * that a one-minute wav would blow through. Recordings stay on this machine; nothing is
   * uploaded.
   *
   * An in-memory map sits in front of the database because `renderTurns` runs during streaming
   * and must not wait on disk I/O for every repaint.
   */
  const audioDb = { ready: null, cache: new Map(), urls: new Map() };

  function openAudioDb() {
    if (audioDb.ready) return audioDb.ready;
    audioDb.ready = new Promise(resolve => {
      let request;
      try {
        request = indexedDB.open("qwen-voice-clips", 1);
      } catch (failure) {
        resolve(null);   // private mode, or a browser that refuses the database
        return;
      }
      request.onupgradeneeded = () => request.result.createObjectStore("clips");
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => resolve(null);
    });
    return audioDb.ready;
  }

  /* A clip's key has to be derivable from the turn alone.
   *
   * It used to be a random id minted when the clip was stored, kept on the in-memory turn and
   * written nowhere else. The gateway's ledger - which is what the history rail rebuilds a
   * conversation from - holds role, content, mode and tokens, and nothing about audio. So a
   * restored turn had no id to look up, no player was ever mounted, and the recordings sat
   * in IndexedDB unreachable: "历史记录里为什么还是没有办法保存语音内容".
   *
   * Deriving the key from the role and the text names the same clip on the way in and on the way
   * back, with no change to the ledger's schema and nothing to keep in step. Repeating a sentence
   * in one conversation reuses one recording, which is the behaviour a listener expects anyway.
   */
  function clipIdFor(turn) {
    const text = String((turn && (turn.text != null ? turn.text : turn.content)) || "");
    let hash = 0;
    for (let index = 0; index < text.length; index += 1) {
      hash = (hash * 31 + text.charCodeAt(index)) >>> 0;
    }
    return (turn.role || "assistant") + "-" + hash.toString(36) + "-" + text.length.toString(36);
  }

  /* Every key a clip for this turn may live under.
   *
   * The conversation half of the key is the id the ledger files the thread under, and the same
   * thread has two shapes: the panel mints `voice-…` (or `local-…`) when it starts a
   * conversation, and the gateway scopes it to `voice·…` when it files it. A visit that spoke
   * before the scoped id was adopted stored its recording under the bare id, while the history
   * rail can only ever hand back the scoped one - so the recording has to be looked for under
   * both, or "the audio is stored but the bar cannot find it" is exactly what happens. Two
   * players were never the problem; one unreachable key was.
   *
   * The turn keeps the short id it always had; the prefix is added here, in one place, rather
   * than at each call site. */
  function clipKeys(id) {
    if (!id) return [];
    if (id.includes("::")) return [id];              // already scoped to a conversation
    const base = state.conversation || "unsorted";
    const keys = [base + "::" + id];
    const cut = base.indexOf("·");
    if (cut > 0) {
      const bare = base.slice(cut + 1) + "::" + id;
      if (!keys.includes(bare)) keys.push(bare);
    }
    return keys;
  }

  async function storeClip(id, blob) {
    const keys = clipKeys(id);
    if (!keys.length || !blob) return;
    // Written under the shape the panel is holding now, and cached under both so a lookup later
    // in this same visit cannot miss because the conversation id changed shape underneath it.
    for (const key of keys) audioDb.cache.set(key, blob);
    const database = await openAudioDb();
    if (!database) return;
    try {
      const transaction = database.transaction("clips", "readwrite");
      transaction.objectStore("clips").put(blob, keys[0]);
    } catch (failure) { /* a clip that cannot be saved must not break the reply */ }
  }

  /* The stored recording and the key it was actually found under, tried in both shapes. This is
   * the one place the IndexedDB read lives, so `clipUrl` and the voice bar cannot drift apart
   * about what "this turn has a recording" means. */
  async function clipEntry(id) {
    const keys = clipKeys(id);
    if (!keys.length) return null;
    for (const key of keys) {
      if (audioDb.cache.has(key)) return { key, blob: audioDb.cache.get(key) };
    }
    const database = await openAudioDb();
    if (!database) return null;
    for (const key of keys) {
      const blob = await new Promise(resolve => {
        try {
          const request = database.transaction("clips").objectStore("clips").get(key);
          request.onsuccess = () => resolve(request.result || null);
          request.onerror = () => resolve(null);
        } catch (failure) {
          resolve(null);
        }
      });
      if (blob) {
        audioDb.cache.set(key, blob);
        return { key, blob };
      }
    }
    return null;
  }

  async function clipBlob(id) {
    const entry = await clipEntry(id);
    return entry ? entry.blob : null;
  }

  async function clipUrl(id) {
    // One URL per clip, cached: creating a fresh object URL on every render would leak one per
    // repaint, and a caller comparing two reads of the same recording should see them match.
    for (const key of clipKeys(id)) {
      if (audioDb.urls.has(key)) return audioDb.urls.get(key);
    }
    const entry = await clipEntry(id);
    if (!entry) return "";
    if (!audioDb.urls.has(entry.key)) audioDb.urls.set(entry.key, URL.createObjectURL(entry.blob));
    return audioDb.urls.get(entry.key);
  }

  /* Delete recordings. Without a conversation this clears the whole store; with one it removes
   * that conversation's clips and leaves the rest alone. */
  async function forgetClips(conversationId) {
    // Both shapes of the id, for the same reason the lookup tries both: a recording stored under
    // the bare id belongs to this conversation too, and leaving it behind would keep audio that
    // the owner believes they deleted.
    const prefixes = [];
    if (conversationId) {
      prefixes.push(conversationId + "::");
      const cut = conversationId.indexOf("·");
      if (cut > 0) {
        const bare = conversationId.slice(cut + 1) + "::";
        if (!prefixes.includes(bare)) prefixes.push(bare);
      }
    }
    const mine = key => !prefixes.length || prefixes.some(prefix => key.startsWith(prefix));
    for (const key of [...audioDb.cache.keys()]) {
      if (mine(key)) audioDb.cache.delete(key);
    }
    for (const [key, url] of [...audioDb.urls]) {
      if (mine(key)) {
        URL.revokeObjectURL(url);
        audioDb.urls.delete(key);
      }
    }
    const database = await openAudioDb();
    if (!database) return 0;
    return await new Promise(resolve => {
      try {
        const store = database.transaction("clips", "readwrite").objectStore("clips");
        const request = store.openCursor();
        let removed = 0;
        request.onsuccess = () => {
          const cursor = request.result;
          if (!cursor) { resolve(removed); return; }
          if (mine(String(cursor.key))) {
            cursor.delete();
            removed += 1;
          }
          cursor.continue();
        };
        request.onerror = () => resolve(removed);
      } catch (failure) {
        resolve(0);
      }
    });
  }

  function dropClipUrls() {
    for (const url of audioDb.urls.values()) URL.revokeObjectURL(url);
    audioDb.urls.clear();
  }

  /* Join the spoken sentences of one reply into a single clip.
   *
   * The local synthesiser answers with WAV, so the pieces can be joined exactly: read the
   * format off the first file and append every data chunk. Preferred over concatenating the
   * containers, which only works when they happen to share a header.
   *
   * Each piece carries the pause that belongs after it. The same silence is written here as the
   * player waits out, so the saved recording lasts as long as the performance did - otherwise
   * the bar would report a duration its own audio does not have.
   */
  async function concatenateWav(pieces) {
    // Only real audio can be joined. A sentence that had to be spoken by the browser carries
    // text and no blob, and it is dropped here rather than handed to arrayBuffer() as an object.
    const entries = (pieces || [])
      .map(piece => {
        if (piece instanceof Blob) return { blob: piece, pause: 0 };
        if (piece && piece.blob instanceof Blob) return { blob: piece.blob, pause: piece.pause || 0 };
        return null;
      })
      .filter(Boolean);
    if (!entries.length) return null;
    if (entries.length === 1) return entries[0].blob;
    const buffers = await Promise.all(entries.map(entry => entry.blob.arrayBuffer()));
    const first = new DataView(buffers[0]);
    let format = null;
    for (let offset = 12; offset + 8 <= buffers[0].byteLength;) {
      const id = String.fromCharCode(first.getUint8(offset), first.getUint8(offset + 1),
                                     first.getUint8(offset + 2), first.getUint8(offset + 3));
      const size = first.getUint32(offset + 4, true);
      if (id === "fmt ") {
        format = new Uint8Array(buffers[0], offset + 8, size);
        break;
      }
      offset += 8 + size + (size % 2);
    }
    if (!format) return entries[0].blob;   // not a wav after all: the first piece is still a clip
    // Silence in the clip's own format: a whole number of sample frames of zeroed PCM.
    const fmtView = new DataView(format.buffer, format.byteOffset, format.byteLength);
    const sampleRate = fmtView.getUint32(4, true) || 24000;
    const blockAlign = fmtView.getUint16(12, true) || 2;
    const silence = ms => new Uint8Array(Math.round((ms / 1000) * sampleRate) * blockAlign);
    const chunks = [];
    let total = 0;
    for (let index = 0; index < buffers.length; index += 1) {
      const buffer = buffers[index];
      const view = new DataView(buffer);
      for (let offset = 12; offset + 8 <= buffer.byteLength;) {
        const id = String.fromCharCode(view.getUint8(offset), view.getUint8(offset + 1),
                                       view.getUint8(offset + 2), view.getUint8(offset + 3));
        const size = view.getUint32(offset + 4, true);
        if (id === "data") {
          chunks.push(new Uint8Array(buffer, offset + 8, Math.min(size, buffer.byteLength - offset - 8)));
          total += chunks[chunks.length - 1].byteLength;
          break;
        }
        offset += 8 + size + (size % 2);
      }
      // Not after the last piece: a trailing silence would make the bar claim a pause that
      // nothing follows.
      if (index < buffers.length - 1 && entries[index].pause > 0) {
        chunks.push(silence(entries[index].pause));
        total += chunks[chunks.length - 1].byteLength;
      }
    }
    if (!chunks.length) return entries[0].blob;
    const header = new ArrayBuffer(44);
    const out = new DataView(header);
    const put = (offset, text) => {
      for (let index = 0; index < text.length; index += 1) {
        out.setUint8(offset + index, text.charCodeAt(index));
      }
    };
    put(0, "RIFF");
    out.setUint32(4, 36 + total, true);
    put(8, "WAVEfmt ");
    out.setUint32(16, format.byteLength, true);
    new Uint8Array(header, 20, format.byteLength).set(format);
    const dataOffset = 20 + format.byteLength;
    put(dataOffset, "data");
    out.setUint32(dataOffset + 4, total, true);
    return new Blob([header, ...chunks], { type: "audio/wav" });
  }

  // ---------------------------------------------------------------- voice bar

  function waveformBars(seed) {
    const box = el("div", "local-voice-bar-wave");
    // A stable pseudo-random profile per reply, so a bar looks like a waveform without
    // claiming to be a real amplitude plot of the audio.
    let state = 0;
    for (let i = 0; i < seed.length; i += 1) state = (state * 31 + seed.charCodeAt(i)) >>> 0;
    for (let i = 0; i < 24; i += 1) {
      state = (state * 1103515245 + 12345) >>> 0;
      const height = 6 + (state % 18);
      const bar = el("span", "");
      bar.style.height = height + "px";
      box.append(bar);
    }
    return box;
  }

  function durationLabel(ms) {
    return ((Number(ms) || 0) / 1000).toFixed(1) + "″";
  }

  /* How long a stored recording actually is.
   *
   * Read from the file rather than remembered: the bar that shows the duration is usually
   * rebuilt from the ledger, which never knew it, so the only surviving copy of the length is
   * inside the audio the browser decodes anyway. The element is never attached to the document,
   * so this costs one header read and puts no control on the page. */
  function blobMs(blob) {
    return new Promise(resolve => {
      let url = "";
      try { url = URL.createObjectURL(blob); } catch (failure) { resolve(0); return; }
      const probe = new Audio();
      probe.preload = "metadata";
      const finish = value => {
        probe.removeAttribute("src");
        URL.revokeObjectURL(url);
        resolve(value);
      };
      probe.addEventListener("loadedmetadata", () => {
        finish(Number.isFinite(probe.duration) ? probe.duration * 1000 : 0);
      }, { once: true });
      probe.addEventListener("error", () => finish(0), { once: true });
      probe.src = url;
    });
  }

  /* The voice bar: the one control a spoken turn gets, in the conversation and in the history.
   *
   * A live reply hands its segments over directly. A restored turn cannot - the ledger holds
   * role, content and mode and nothing about sound - so its bar fetches the clip stored under
   * the turn's own id when it is played, and turns into the plain words when there is no clip
   * to fetch. That is what replaced mounting a second, native <audio controls> beneath every
   * turn: one recording should not come with two players, and the browser's own chrome is not
   * part of this panel.
   */
  function createVoiceBar(turn) {
    const text = String(turn.text != null ? turn.text : (turn.content || ""));
    const segments = Array.isArray(turn.segments) ? turn.segments : [];
    const id = turn.clipId || clipIdFor(turn);

    const bar = el("div", "local-voice-bar");
    bar.tabIndex = 0;
    bar.setAttribute("role", "button");
    bar.title = "点击重播";
    bar.append(el("span", "local-voice-bar-play"), waveformBars(text));
    const time = el("span", "local-voice-bar-time", turn.totalMs ? durationLabel(turn.totalMs) : "");
    bar.append(time);

    const toggle = el("button", "local-voice-bar-toggle", "转文字");
    toggle.type = "button";
    const transcript = el("div", "local-voice-transcript", text);
    transcript.hidden = true;
    toggle.addEventListener("click", event => {
      event.stopPropagation();
      transcript.hidden = !transcript.hidden;
      toggle.textContent = transcript.hidden ? "转文字" : "收起文字";
    });

    const column = el("div", "local-voice-bar-text");
    column.append(bar, toggle, transcript);

    // Never a dead control: a turn with no recording anywhere becomes its own words instead.
    const asText = () => { column.replaceWith(el("div", "local-voice-bubble", text)); };

    if (segments.length) bar.dataset.audio = "ready";   // already in hand, nothing to fetch
    let clip;
    let looked = false;
    const pieces = async () => {
      if (segments.length) return segments;
      if (!looked) {
        looked = true;
        clip = await clipBlob(id);
      }
      return clip ? [{ blob: clip }] : [];
    };

    if (!segments.length) {
      void (async () => {
        const available = await pieces();
        if (!available.length) { asText(); return; }
        bar.dataset.audio = "ready";
        time.textContent = durationLabel(await blobMs(clip));
      })();
    }

    let replaying = false;
    const replay = async () => {
      if (replaying) return;
      replaying = true;
      bar.classList.add("is-playing");
      try {
        const available = await pieces();
        if (!available.length) { asText(); return; }
        let noAudio = 0;
        for (const segment of available) {
          if (segment.blob) await playBlob(segment.blob, speechToken);
          // A stored piece with no audio is a leftover from the era when a failed local render was
          // read out by the browser. This view does not use the browser's voice any more, so it is
          // skipped - and said out loud, because a bar that plays nothing is worse than one that
          // explains why.
          else if (segment.text) noAudio += 1;
        }
        if (noAudio) {
          setFact("tts", "这条语音没有本地音频");
          if (factRows.tts) {
            factRows.tts.title = `这条回复有 ${noAudio} 段是浏览器朗读时代留下的，没有本地音频；`
              + "重新说一次就会存成本地语音";
          }
        }
      } finally {
        replaying = false;
        bar.classList.remove("is-playing");
      }
    };
    bar.addEventListener("click", () => void replay());
    bar.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); void replay(); }
    });

    return column;
  }

  async function askStreaming(text) {
    const response = await fetch(CHAT_URL, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        // The gateway reads the history it recorded for this conversation, so the model is
        // reminded of what was typed as well as what was said. An empty array means "use
        // yours": the first turn of a fresh conversation.
        history: [],
        conversation_id: conversationId(),
        profile: voiceProfile, stream: true,
      }),
    });
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try { const body = await response.json(); detail = body.detail || body.error || detail; } catch (failure) { /* keep the status */ }
      throw new Error(detail);
    }
    if (!response.body) throw new Error("流式响应不可用");

    const liveTurn = addLiveTurn();
    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    let full = "";
    let stats = null;

    // Synthesis and playback run as two tasks fed by one queue. Playing one sentence
    // while the next is still being synthesised is what removes the gaps: synthesising
    // only after the previous one finished made every sentence boundary an audible
    // stall, because local synthesis takes longer than the speech it produces.
    //
    // Every piece of this pipeline captures the token of the turn that created it and
    // stops the moment that token is superseded. Without that, the previous turn's
    // queue kept rendering and speaking underneath the new reply — which arrived as a
    // second voice reading stale sentences.
    const token = speechToken;
    const queue = [];
    const speechBlobs = [];
    // Everything that was actually spoken this turn, in order, for replay.
    const spokenSegments = [];
    // Counted so the turn can say what it actually did. Without it a silent drop and a
    // browser-voice sentence look identical from the outside - which is how a failed
    // render went unnoticed while the panel still said "浏览器".
    const spoken = { local: 0, failed: 0 };
    let pipelineClosed = false;
    let speechStarted = false;
    let synthesisDone = false;
    // Timing kept for the turn's own line: how long until the first sound, and how many times the
    // player ran dry mid-reply (an audible gap, which is the one thing a shorter lead must not
    // cause). `ratios` holds this turn's measured synthesis cost per millisecond of audio.
    let firstAudioMs = 0;
    let stalls = 0;
    let drained = false;
    let firstUnitSent = false;
    const ratios = [];

    const active = () => token === speechToken;

    const synthesisLoop = async () => {
      while (active()) {
        if (!queue.length) {
          if (pipelineClosed) break;
          await new Promise(resolve => setTimeout(resolve, 25));
          continue;
        }
        const sentence = queue.shift();
        const synthStartedAt = performance.now();
        const blob = await renderSentence(sentence, token);
        if (!active()) break;
        const synthMs = performance.now() - synthStartedAt;
        // Attached to the piece, not to the loop: the player and the saved clip both read it
        // from here, which is what keeps the recording the same length as the performance.
        const pause = pauseAfterMs(sentence);
        if (blob) {
          const duration = await wavDurationMs(blob);
          // Compute spent per millisecond of audio. The player reads this to decide how much of a
          // head start to hold, so the lead follows the machine instead of a fixed guess.
          if (duration > 0 && synthMs > 0) {
            ratios.push(synthMs / duration);
            if (ratios.length > 8) ratios.shift();
          }
          speechBlobs.push({ blob, duration, pause });
          spokenSegments.push({ blob, duration, pause });
          spoken.local += 1;
        } else if (blob === null) {
          // The local synthesiser could not render this piece. The rule for this view is absolute:
          // never read it with the browser's voice. So nothing is queued, nothing is spoken, and the
          // failure is counted so the turn can say what did not happen instead of sounding like it
          // worked. It used to push a `{text}` piece for the browser to speak, which is how a whole
          // reply came out in the browser's voice - English-sounding on this machine, which has no
          // Chinese browser voice - while the text on screen was Chinese.
          spoken.failed += 1;
        }
      }
      synthesisDone = true;
    };

    const bufferedMs = () => speechBlobs.reduce((sum, item) => sum + (item.duration || 0), 0);

    /* How much of a head start the player needs.
     *
     * This used to be a constant 1800 ms, chosen for the cold engine (RTF 1.1-1.3 while the graphs
     * are still being built). Once warm the same unit costs about 0.7x its own length, so most of
     * that wait was silence before the first word.
     *
     * What actually has to be covered is a single boundary: while the current unit plays (d1), the
     * next one is synthesised, which costs r * d2 at the measured ratio r. The deficit is therefore
     * r * d2 - d1 - and that, not a fixed number, is what the buffer must hold. A short first clause
     * followed by a long sentence is the case it protects; two comparable units need almost nothing.
     * Synthesis and playback are both serial, so no deeper lookahead is needed. */
    const LEAD_FLOOR_MS = 250;
    const LEAD_CEILING_MS = 2000;
    const averageUnitMs = () => {
      if (!spokenSegments.length) return 1500;
      return spokenSegments.reduce((sum, s) => sum + (s.duration || 0), 0) / spokenSegments.length;
    };
    const leadTarget = () => {
      const first = speechBlobs[0];
      if (!first) return LEAD_FLOOR_MS;
      const r = ratios.length ? ratios.reduce((a, b) => a + b, 0) / ratios.length : 1.2;
      const deficit = r * averageUnitMs() - (first.duration || 0);
      return Math.round(Math.min(LEAD_CEILING_MS, Math.max(LEAD_FLOOR_MS, deficit)));
    };

    const playbackLoop = async () => {
      while (active()) {
        if (!speechBlobs.length) {
          if (pipelineClosed && synthesisDone) break;
          // Ran dry after audio had started: that is an audible gap, and counting them is the only
          // honest way to know whether the lead is still large enough.
          if (speechStarted && !drained) { stalls += 1; drained = true; }
          await new Promise(resolve => setTimeout(resolve, 25));
          continue;
        }
        if (bufferedMs() < leadTarget() && !(pipelineClosed && synthesisDone)) {
          await new Promise(resolve => setTimeout(resolve, 50));
          continue;
        }
        const item = speechBlobs.shift();
        if (!active()) break;
        if (!speechStarted && state.turnStartedAt) {
          firstAudioMs = performance.now() - state.turnStartedAt;
        }
        speechStarted = true;
        drained = false;
        if (item && item.blob) await playBlob(item.blob, token);
        // The gap between two sentences. Waited out here rather than baked into the audio so the
        // next sentence keeps being synthesised during the pause; the stored clip gets the same
        // silence written into it, so what is replayed is what was heard.
        if (item && item.pause && active()) {
          await new Promise(resolve => setTimeout(resolve, item.pause));
        }
      }
    };

    const speechHandle = { push: s => queue.push(s), close: () => { pipelineClosed = true; } };
    const synthesisTask = synthesisLoop();
    const playbackTask = playbackLoop();

    const consume = piece => {
      buffer += piece;
      full += piece;
      // The running text stays off screen. The reply is rendered once, at the end, as a
      // voice bar; drawing it here as well is the reported "the answer came out twice".
      // What is shown instead is how much of it has already been spoken.
      if (liveNode) {
        const spoken = spokenSegments.length;
        liveNode.textContent = spoken ? `正在朗读 · 已完成 ${spoken} 句` : "正在生成…";
      }
      log.scrollTop = log.scrollHeight;
      // Break on a sentence-final mark so a clause is never spoken half-written - except for the
      // very first unit, which may also break at a comma.
      //
      // This is the difference between waiting for a whole sentence to exist and starting to
      // speak: the first sentence of a reply is often 20-30 characters, and synthesising it before
      // any sound comes out is a second or more of silence. A clause cut at a comma is where a
      // speaker would breathe anyway, so the first unit is allowed to end there. Every unit after
      // it goes back to sentence-sized pieces, which is what keeps the rest of the prosody intact.
      const pattern = firstUnitSent ? /(?<=[。！？!?；;…])/ : /(?<=[。！？!?；;…，,、：:])/;
      const parts = buffer.split(pattern);
      buffer = parts.pop() || "";
      for (const sentence of parts) {
        const trimmed = sentence.trim();
        if (trimmed) {
          speechHandle.push(trimmed);
          firstUnitSent = true;
        }
      }
      // A long run without punctuation still gets spoken so nothing stalls the reply. The first
      // unit waits less: with no punctuation at all by 24 characters, saying that much beats
      // leaving the listener in silence for the rest of the sentence.
      const limit = firstUnitSent ? 60 : 24;
      if (buffer.length >= limit) {
        speechHandle.push(buffer.trim());
        buffer = "";
        firstUnitSent = true;
      }
    };

    let sseBuffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      sseBuffer += decoder.decode(value, { stream: true });
      const events = sseBuffer.split("\n\n");
      sseBuffer = events.pop() || "";
      for (const block of events) {
        let name = "message";
        const dataLines = [];
        for (const line of block.split("\n")) {
          if (line.startsWith("event:")) name = line.slice(6).trim();
          else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
        }
        const data = dataLines.join("\n");
        if (name === "done") continue;
        if (name === "stats") {
          try { stats = JSON.parse(data); } catch (failure) { /* stats are cosmetic */ }
          if (stats && stats.meter) setMeter(stats.meter);
          continue;
        }
        if (name === "error") throw new Error(data);
        try { consume(JSON.parse(data)); } catch (failure) { /* ignore a malformed chunk */ }
      }
    }

    if (buffer.trim()) speechHandle.push(buffer.trim());
    speechHandle.close();
    // Wait for both loops: the reply is not finished until its last sentence has been
    // synthesised and played.
    await Promise.all([synthesisTask, playbackTask]);

    const finalText = (full.trim() || "（模型没有返回内容）");
    const seconds = stats && stats.seconds != null
      ? stats.seconds : ((Date.now() - started) / 1000).toFixed(2);
    const first = stats && stats.first_token_seconds != null ? `首字 ${stats.first_token_seconds}s · ` : "";
    // The reply is already filed in the shared history by the gateway that produced it; the
    // panel no longer keeps a second copy, which is what used to make a spoken turn invisible
    // to the typed thread (and the typed thread invisible to the model's memory of the call).
    // The finished reply takes over the entry the live placeholder already occupies, and
    // the renderer below turns it into a voice bar. Pushing a second turn and rendering
    // again is what drew the reply twice and discarded the bar that had just been built.
    liveTurn.text = finalText;
    liveTurn.segments = spokenSegments;
    liveTurn.totalMs = spokenSegments.reduce((sum, s) => sum + (s.duration || 0), 0);
    liveTurn.meta = `${first}用时 ${seconds}s · 首音 ${(firstAudioMs / 1000).toFixed(2)}s`
      + ` · 本地合成 ${spoken.local} 句`
      + (spoken.failed ? ` · 本地合成失败 ${spoken.failed} 句（未用浏览器朗读）` : "")
      + (stalls ? ` · 停顿 ${stalls} 次` : "");
    delete liveTurn.live;
    renderTurns();
    // The reply's audio is kept, so the history can be listened to instead of only read. Done
    // after the render: the clip arrives a moment after the text, and a turn that is already on
    // screen gains its player without the log having to wait for the join.
    const spokenReply = await concatenateWav(spokenSegments);
    if (spokenReply) {
      liveTurn.clipId = clipIdFor(liveTurn);
      await storeClip(liveTurn.clipId, spokenReply);
      renderTurns();
    }
    setFact("last", seconds + "s");
    if (stats && stats.first_token_seconds != null) {
      setFact("llm", `首字 ${stats.first_token_seconds}s`);
    }
  }

  // The in-flight reply is a real entry in state.turns, so the single renderer owns the
  // whole log and there is nothing to swap out when the turn finishes. It shows progress,
  // never the reply text: the words belong to the bar's 转文字 panel.
  function addLiveTurn() {
    const turn = { role: "assistant", live: "正在生成…", text: "" };
    state.turns.push(turn);
    renderTurns();
    return turn;
  }

  // The lead is no longer a constant: it is computed per turn from the synthesis speed this turn
  // actually measured. See leadTarget() in the playback loop - LEAD_FLOOR_MS is the smallest
  // cushion, LEAD_CEILING_MS the largest, so a genuinely slow machine still waits as it used to.

  // Read the duration out of a WAV blob's header. The synthesiser returns a complete
  // WAV, so the format chunk is all that is needed and no decoding is involved.
  async function wavDurationMs(blob) {
    try {
      const header = new DataView(await blob.slice(0, 64).arrayBuffer());
      if (header.byteLength < 44) return 1200;
      let byteRate = 0;
      let dataBytes = 0;
      let offset = 12;
      while (offset + 8 <= header.byteLength) {
        const id = String.fromCharCode(
          header.getUint8(offset), header.getUint8(offset + 1),
          header.getUint8(offset + 2), header.getUint8(offset + 3));
        const size = header.getUint32(offset + 4, true);
        if (id === "fmt ") byteRate = header.getUint32(offset + 16, true);
        if (id === "data") { dataBytes = size; break; }
        offset += 8 + size + (size % 2);
      }
      if (byteRate > 0 && dataBytes > 0) return (dataBytes / byteRate) * 1000;
    } catch (failure) { /* fall through to the default below */ }
    return 1200;
  }

  // Render one sentence with the local voice.
  //   Blob      -> play it
  //   null      -> this sentence genuinely needs the browser voice
  //   undefined -> the turn was superseded; speak nothing at all
  // The third case is what used to be missing: a stale render returned null, the player
  // treated it as "use the browser voice", and the previous reply was read aloud in a
  // different voice underneath the new one.
  async function renderSentence(sentence, token) {
    if (!sentence) return undefined;
    try {
      const response = await fetch(TTS_URL, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: sentence, speaker: state.voice, language: "chinese",
                               instruct: state.style }),
      });
      if (token !== speechToken) return undefined;
      if (response.ok) {
        const blob = await response.blob();
        if (token !== speechToken) return undefined;
        if (blob.size > 512) {
          setFact("tts", state.voice);
          if (factRows.tts) factRows.tts.title = "本地合成 · " + state.voice;
          return blob;
        }
        setFact("tts", "本地合成无音频");
        if (factRows.tts) factRows.tts.title = `本地合成只返回了 ${blob.size} 字节`;
        return null;
      }
      // The gateway answers 503 with its own reason ("本地语音合成不可用"), and the usual cause is
      // that the engine could not start at all. Saying why beats the browser's voice appearing with
      // nothing on screen to explain it - which is how a broken local engine went unnoticed for
      // days while every reply was quietly read by the browser.
      let detail = `HTTP ${response.status}`;
      try {
        const payload = await response.json();
        detail = payload.detail || payload.error || detail;
      } catch (failure) { /* keep the status */ }
      setFact("tts", "本地合成失败");
      if (factRows.tts) factRows.tts.title = "本地合成失败：" + detail;
      return null;
    } catch (failure) {
      setFact("tts", "本地合成失败");
      if (factRows.tts) factRows.tts.title = "本地合成失败：" + String(failure.message || failure);
      return null;
    }
  }

  // ---------------------------------------------------------------- audio out

  // Speaking the reply in sentence-sized pieces is what makes the wait short: the
  // first sentence starts playing while the rest are still being synthesised, instead
  // of the user waiting for the whole paragraph. Synthesis runs at roughly 1.3x the
  // audio's own length, so one long clip would delay everything by the full duration.
  function splitSentences(text) {
    const parts = String(text)
      .split(/(?<=[。！？!?；;…\n])/)
      .map(part => part.trim())
      .filter(Boolean);
    if (parts.length <= 1) return parts.length ? parts : [];
    // Merge a trailing fragment into the previous piece so a stray "。" does not
    // become its own request.
    const merged = [];
    for (const part of parts) {
      const last = merged[merged.length - 1];
      if (last && last.length < 6) merged[merged.length - 1] = last + part;
      else merged.push(part);
    }
    return merged;
  }

  /* How long a person would pause after a sentence like this.
   *
   * The engine is asked for one sentence at a time and the results were being butted together
   * with no gap at all, which is the most robotic thing about concatenated speech: speech
   * breathes at the end of a sentence and stops longer at the end of a thought. The pause comes
   * from the punctuation the sentence already carries, so nothing has to be decided at synthesis
   * time, and the same number is used by the player and by the clip that gets stored. */
  function pauseAfterMs(sentence) {
    const text = String(sentence || "").trimEnd();
    if (!text) return 0;
    const last = text[text.length - 1];
    if ("。！？!?".includes(last)) return text.length >= 24 ? 380 : 300;
    if (last === "…") return 420;
    if ("；;".includes(last)) return 220;
    return 140;      // a clause cut for length rather than for meaning
  }

  // Bumped whenever a turn starts or speech is stopped. Every queued sentence and
  // every in-flight render checks it, which is what keeps a previous reply from
  // continuing to play under a new one.
  let speechToken = 0;

  function playBlob(blob, token) {
    return new Promise(resolve => {
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      currentAudio = audio;
      const done = () => { URL.revokeObjectURL(url); resolve(); };
      audio.addEventListener("ended", done, { once: true });
      audio.addEventListener("error", done, { once: true });
      audio.play().catch(done);
      // A stop() swaps the token; resolve so the loop can exit instead of hanging.
      const watcher = setInterval(() => {
        if (token !== speechToken) { clearInterval(watcher); done(); }
      }, 200);
      audio.addEventListener("ended", () => clearInterval(watcher), { once: true });
    });
  }

  /* Find a voice that can actually read Chinese.
   *
   * NOT USED BY THIS VIEW ANY MORE, and deliberately kept only as a record: the owner's rule for
   * voice conversation is that the browser's voice is never used at all, so every call site was
   * removed (a failed local render is now silent and counted instead). Do not re-wire these without
   * that rule changing - the whole point is that the browser cannot speak for this feature.
   *
   * `utterance.lang` states an intention; it does not select a voice. That distinction is why a
   * Chinese reply has been *heard* as English while the text on screen was Chinese: the local
   * engine could not start (out of memory), playback fell back to the browser, and the browser read
   * the characters with its default voice - an English one - because no Chinese voice had been
   * chosen. The list is also filled asynchronously, so an empty result on the first utterance means
   * "not yet", not "none". */
  function browserVoiceForChinese(waitMs = 400) {
    return new Promise(resolve => {
      if (!("speechSynthesis" in window)) { resolve(null); return; }
      const choose = () => {
        const voices = window.speechSynthesis.getVoices() || [];
        const zh = voices.filter(v => /^zh([-_]|$)/i.test(String(v.lang || "")));
        return zh.find(v => /^zh[-_]CN/i.test(v.lang)) || zh[0] || null;
      };
      const found = choose();
      if (found || !waitMs) { resolve(found); return; }
      const started = Date.now();
      const tick = setInterval(() => {
        const later = choose();
        if (later || Date.now() - started > waitMs) { clearInterval(tick); resolve(later); }
      }, 50);
    });
  }

  function browserSpeakAsync(text, token) {
    return (async () => {
      if (!("speechSynthesis" in window)) return;
      const voice = await browserVoiceForChinese();
      if (token !== speechToken) return;
      return new Promise(resolve => {
        const utterance = new SpeechSynthesisUtterance(text);
        if (voice) {
          utterance.voice = voice;
          utterance.lang = voice.lang;
        } else {
          // Nothing here can read Chinese, and pretending otherwise is what produced English audio
          // for Chinese text. Say so instead of letting the default voice mangle it.
          utterance.lang = "zh-CN";
          setFact("tts", "浏览器（无中文语音）");
          if (factRows.tts) {
            factRows.tts.title = "浏览器里没有中文语音，默认嗓音念中文会走音（装一个中文语音包可解决）";
          }
        }
        utterance.rate = 1.05;
        utterance.onend = resolve;
        utterance.onerror = resolve;
        window.speechSynthesis.speak(utterance);
        const watcher = setInterval(() => {
          if (token !== speechToken) { clearInterval(watcher); window.speechSynthesis.cancel(); resolve(); }
        }, 200);
      });
    })();
  }

  function stopSpeaking() {
    speechToken += 1;
    if (currentAudio) {
      try { currentAudio.pause(); } catch (failure) { /* already stopped */ }
      currentAudio = null;
    }
    if ("speechSynthesis" in window) window.speechSynthesis.cancel();
  }

  // ---------------------------------------------------------------- shell

  /* 语音不再锁死某个档位：跟着当前选中的模型走（9B 40960 / 三元 27B 8192 的语音窗口各用各的）。
   * 能不能用由网关说了算（`/local/voice/config`，与语音路由执行的是同一条规则）：没有语音窗口的
   * 档位（旧版 IQ4_XS）会被拒，理由由网关给出。这里的变量只是"这次语音要用哪一份"，从配置或
   * 启动回复里读，不再写死。 */
  let voiceProfile = "";
  let voiceProfileLabel = "";
  /* One conversation, whichever way the owner is talking. The chat app's identifier is used
   * when this page has it, and a stable one of our own otherwise, so a spoken turn lands in
   * the same history and the same context meter as the typed ones — including across a page
   * reload, which is what makes the thread continuous rather than per-session. */
  function conversationId() {
    if (state.conversation) return state.conversation;
    let stored = null;
    try { stored = localStorage.getItem("qwen.local.conversation.v1"); } catch (failure) { /* blocked */ }
    if (!stored) {
      stored = (window.QwenConversation && window.QwenConversation.id)
        || ("local-" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36));
      try { localStorage.setItem("qwen.local.conversation.v1", stored); } catch (failure) { /* blocked */ }
    }
    state.conversation = stored;
    return stored;
  }

  /* Keep the shared history in step with the browser's own thread element. The turn is
   * filed by the gateway (it owns the ledger), so nothing is sent from here except the
   * identifier; this only mirrors what the gateway recorded. */
  async function refreshMeter() {
    try {
      const value = await json("/local/conversation?conversation_id="
        + encodeURIComponent(conversationId()) + "&limit=1&window_kind=voice&scope=voice");
      if (value && value.meter) setMeter(value.meter);
    } catch (failure) { /* the meter is a nicety, never a reason to fail a turn */ }
  }

  // The last reserve the gateway reported, so a freshly started conversation can describe the
  // window it will have before the next status reply arrives (the reserve is the gateway's
  // number — this only remembers it rather than inventing one).
  let lastReserve = 4096;

  function setMeter(meter) {
    const row = factRows.context;
    if (!row || !meter) return;
    if (Number(meter.reserve_tokens) > 0) lastReserve = Number(meter.reserve_tokens);
    const left = Number(meter.remaining_tokens) || 0;
    const total = Number(meter.context_size) || 0;
    const label = left >= 1000 ? (left / 1000).toFixed(1) + "k" : String(left);
    // The window is printed in full rather than rounded: this is the number the owner asks
    // about ("语音不是 40960 吗"), and "41k" for 40960 would be a lie about the decision.
    const totalText = total.toLocaleString("en-US");
    const marks = { ok: "", tight: " · 偏紧", critical: " · 将满", full: " · 已满" };
    setFact("context", label + " / " + totalText + (marks[meter.level] || ""));
    // The header chip is the narrow one: same two numbers, without the level words.
    const chip = chips.context;
    if (chip) chip.textContent = label + " / " + totalText;
    const locked = meter.window_kind === "voice";
    const why = (locked ? "语音窗口 " + totalText + " tokens（锁定，首轮起生效）" : "正在服务的窗口 " + totalText)
      + " · 已用 " + meter.used_tokens + " tokens（" + (meter.source === "measured" ? "实测" : "估算")
      + "）· 保留 " + meter.reserve_tokens + " tokens";
    row.title = why;
    if (chip) chip.title = why + (marks[meter.level] ? "｜" + marks[meter.level].replace(" · ", "") : "");
    const level = meter.level && meter.level !== "ok" ? meter.level : "ok";
    statusRow.dataset.level = level;
  }

  // Whether this session has already swapped the card over to the voice pair. Kept out
  // of `state` because it describes the backend, not the panel's appearance.
  // Which capabilities this session has already brought up. Kept apart because the two
  // kinds of turn need different things, and the gateway starts exactly what it is asked
  // for: a typed turn needs the language model and never reaches a recogniser, so asking
  // for one would spend VRAM and startup time on a model the turn cannot call.
  let voiceModeReady = false;      // the language model is up
  let voiceModeAsrReady = false;   // the recogniser is up as well
  let voiceModeStarting = null;

  /* Show the conversation the owner is continuing, not a blank panel.
   *
   * The turns live in the gateway's shared history — the same thread the chat app writes to,
   * so a question typed there comes back here — but until this was wired up the panel only
   * ever rendered `state.turns`, which starts empty on every page load and panel open. That
   * is why the history looked lost even though the model still had it.
   */
  async function loadHistory(explicit) {
    const target = explicit || conversationId();
    try {
      const value = await json("/local/conversation?conversation_id="
        + encodeURIComponent(target) + "&limit=40&window_kind=voice&scope=voice");
      // A reply that arrives after the owner has moved on must not paint over the newer
      // conversation. Measured: opening voice starts a fresh conversation whose (empty) fetch
      // could land *after* a click on a past conversation, and the log went blank again.
      if (target !== state.conversation) return 0;
      if (value && value.meter) setMeter(value.meter);
      if (!value || !Array.isArray(value.history)) return 0;
      // Dropped when the panel already has turns: `renderTurns` rebuilds the whole log, so
      // mixing a live turn with a reloaded one would duplicate the reply just spoken.
      if (state.turns.length) return 0;
      const turns = value.history
        .filter(turn => turn && turn.content && (turn.role === "user" || turn.role === "assistant"))
        .map(turn => ({
          role: turn.role,
          text: turn.content,
          // Kept, not just folded into the caption: whether a turn was spoken is what decides
          // between a voice bar and a plain bubble once the log is rebuilt, and the ledger is
          // the only place that still knows.
          mode: turn.mode === "voice" ? "voice" : "text",
          meta: turn.mode === "voice"
            ? "语音 · " + new Date(turn.at * 1000).toLocaleTimeString()
            : "文字 · " + new Date(turn.at * 1000).toLocaleTimeString(),
        }));
      if (!turns.length) return 0;
      state.turns = turns;
      renderTurns();
      return turns.length;
    } catch (failure) {
      // A history that cannot be fetched is not a reason to refuse to talk.
      return 0;
    }
  }

  /* ---------------------------------------------------------------- conversation list */

  /* The stamp in the bottom-right corner of a row: date and time, not "3 分钟前".
   *
   * A relative age is friendlier for the last few minutes and useless for anything older ("43
   * 天前" does not say which day), and the owner asked for the date and time. The year is only
   * printed when it is not the current one, so today's rows stay short. */
  function stamp(seconds, full) {
    const at = new Date((Number(seconds) || 0) * 1000);
    if (Number.isNaN(at.getTime())) return "";
    const pad = value => String(value).padStart(2, "0");
    const day = `${pad(at.getMonth() + 1)}-${pad(at.getDate())}`;
    const clock = `${pad(at.getHours())}:${pad(at.getMinutes())}`;
    if (full) return `${at.getFullYear()}-${day} ${clock}`;
    return at.getFullYear() === new Date().getFullYear() ? `${day} ${clock}` : `${at.getFullYear()}-${day}`;
  }

  /* Rename in place: the title becomes an input where it already is, rather than a dialog on
   * top of the page. Enter or clicking away commits, Escape cancels, and a manual name is
   * final — the gateway records it as such, so a later turn cannot take it over. */
  function startRename(row, open, item) {
    if (row.dataset.editing === "true") return;
    const titleNode = open.querySelector(".local-voice-history-title");
    if (!titleNode) return;
    row.dataset.editing = "true";
    const input = el("input", "local-voice-history-rename-input");
    input.type = "text";
    input.value = item.title || item.display_title || "";
    input.maxLength = 40;
    input.setAttribute("aria-label", "对话标题");
    titleNode.replaceWith(input);
    input.focus();
    input.select();
    let settled = false;
    const finish = async commit => {
      if (settled) return;
      settled = true;
      const value = input.value.trim();
      const changed = commit && value && value !== (item.title || item.display_title || "");
      if (changed) {
        try {
          const saved = await json("/local/conversation?scope=voice&conversation_id=" + encodeURIComponent(item.id),
            { method: "PATCH", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ title: value }) });
          showError("");
          hint.textContent = `已重命名为「${(saved && saved.display_title) || value}」`;
        } catch (failure) {
          // The gateway owns the limit (40 characters) and says so; echoing its words is better
          // than inventing a client-side rule that could disagree with it.
          showError("重命名失败：" + (failure.message || failure));
        }
      }
      row.dataset.editing = "false";
      await refreshConversations();
    };
    input.addEventListener("click", event => event.stopPropagation());
    input.addEventListener("keydown", event => {
      // Escape and Space belong to the input while it is open: without this the page-level
      // handler would treat them as "close the panel" / "start recording mid-rename".
      event.stopPropagation();
      if (event.key === "Enter") { event.preventDefault(); void finish(true); }
      if (event.key === "Escape") { event.preventDefault(); void finish(false); }
    });
    input.addEventListener("blur", () => void finish(true));
  }

  /* Collapse the rail to a strip. The state is remembered, and the strip is the way back — a
   * sidebar that can vanish without leaving a handle is a sidebar nobody dares collapse. */
  function setRail(open) {
    state.railOpen = Boolean(open);
    panel.classList.toggle("is-rail-collapsed", !state.railOpen);
    railToggle.setAttribute("aria-expanded", String(state.railOpen));
    const label = state.railOpen ? "收起对话历史" : "展开对话历史";
    railToggle.title = label;
    railToggle.setAttribute("aria-label", label);
    stripToggle.setAttribute("aria-label", "展开对话历史");
    stripToggle.title = "展开对话历史";
    railStrip.hidden = state.railOpen;
    try { localStorage.setItem("qwen.voice.rail", state.railOpen ? "1" : "0"); } catch (failure) { /* blocked */ }
  }

  async function refreshConversations() {
    try {
      const value = await json("/local/conversation?scope=voice");
      const items = (value && value.conversations) || [];
      renderConversations(items);
      return items.length;
    } catch (failure) {
      // The list is a convenience: an unreachable gateway must not empty the panel.
      return -1;
    }
  }

  function renderConversations(items) {
    lastConversations = Array.isArray(items) ? items : [];
    historyList.replaceChildren();
    // Only threads that hold something get a row, plus the one this visit is on: it has to stay
    // visible or the "current conversation" mark would point at a row that is not there. A visit
    // killed before it spoke leaves an empty conversation behind, and a rail full of
    // `voice-x4amc… · 0 轮` rows is precisely the noise the owner reported.
    const current = conversationId();
    // The rail lists this mode's threads with their **scoped** ids (`voice·…`), while the panel may
    // still be holding the bare id it generated. Comparing both shapes is what keeps the current
    // conversation highlighted — and its empty row visible — across that seam.
    const isCurrent = id => Boolean(id) && (id === current || id.endsWith("·" + current)
                                            || current.endsWith("·" + id));
    const query = (searchBox.value || "").trim().toLowerCase();
    const shown = lastConversations
      .filter(item => item.turns > 0 || isCurrent(item.id))
      .filter(item => !query
        || String(item.title || "").toLowerCase().includes(query)
        || String(item.id).toLowerCase().includes(query));
    historyCount.textContent = String(shown.length);
    historyCount.title = shown.length ? `${shown.length} 条对话` : "还没有对话";
    stripCount.textContent = String(shown.length);
    stripCount.title = historyCount.title;
    if (!shown.length) {
      historyList.append(el("p", "local-voice-history-empty",
        query ? `没有标题匹配「${searchBox.value.trim()}」的对话` : "还没有对话 · 说一句或点「新对话」开始"));
      return;
    }
    for (const item of shown) {
      const row = el("div", "local-voice-history-item");
      row.setAttribute("role", "option");
      // The row carries its conversation id: a check (or a future control) can then name the
      // exact thread it is acting on instead of matching on the visible title.
      row.dataset.id = item.id;
      const active = isCurrent(item.id);
      row.setAttribute("aria-selected", String(active));
      row.dataset.active = String(active);
      const badge = item.last_mode === "voice" ? "语音" : "文字";
      // Continuing is the row's own job; renaming and deleting are separate controls so a
      // mis-click cannot destroy or rename a thread. The whole row still answers a click.
      const open = el("button", "local-voice-history-open");
      open.type = "button";
      // A conversation with no turns yet is called 新对话; its first turn renames it to the
      // owner's own words (ten characters, done by the gateway so every consumer agrees).
      const title = el("span", "local-voice-history-title",
                       item.display_title || item.title || item.id);
      const meta = el("span", "local-voice-history-meta");
      meta.append(
        el("span", "local-voice-history-count", `${item.turns} 轮 · ${badge}`),
        // Date and time, bottom right of the row: 轮数 answers "what is this", the stamp
        // answers "when was it".
        el("span", "local-voice-history-when", stamp(item.updated_at)),
      );
      open.append(title, meta);
      open.title = item.title
        ? `${item.title}（${stamp(item.updated_at, true)}）`
        : "新对话 · 第一句话会成为它的标题";
      open.addEventListener("click", () => continueConversation(item.id));
      // The row itself answers a click too, so the whole row is the target the comment above
      // claims it is: the open button does not cover the row's padding, and a click on that
      // strip used to do nothing at all.
      row.addEventListener("click", event => {
        if (event.target === row) void continueConversation(item.id);
      });
      const rename = el("button", "local-voice-history-rename");
      rename.type = "button";
      rename.append(icon("pencil"));
      rename.title = "重命名这条对话";
      rename.setAttribute("aria-label", `重命名对话 ${item.display_title || item.id}`);
      rename.addEventListener("click", event => {
        event.stopPropagation();
        startRename(row, open, item);
      });
      const remove = el("button", "local-voice-history-delete");
      remove.type = "button";
      remove.append(icon("trash"));
      remove.title = "删除这条对话（含本机记录与语音条）";
      remove.setAttribute("aria-label", `删除对话 ${item.display_title || item.id}`);
      remove.addEventListener("click", event => {
        event.stopPropagation();
        void deleteConversation(item.id, item.title);
      });
      row.append(open, rename, remove);
      historyList.append(row);
    }
  }

  async function continueConversation(id) {
    if (!id || id === conversationId()) return;
    if (state.busy) return;
    state.conversation = id;
    state.historyCleared = false;
    try { localStorage.setItem("qwen.local.conversation.v1", id); } catch (failure) { /* blocked */ }
    state.turns = [];
    // The blob URLs of the previous conversation's players are released here; the clips
    // themselves stay in IndexedDB under their own keys, so nothing is lost by switching.
    dropClipUrls();
    renderTurns();
    const loaded = await loadHistory(id);
    hint.textContent = loaded
      ? `已切到该对话（${loaded} 轮）· 继续说或直接打字`
      : "已切到该对话（暂无内容）· 从这个话题开始说";
    await refreshConversations();
  }

  async function startNewConversation() {
    if (state.busy) return;
    const fresh = "local-" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
    state.conversation = fresh;
    try { localStorage.setItem("qwen.local.conversation.v1", fresh); } catch (failure) { /* blocked */ }
    state.turns = [];
    dropClipUrls();
    renderTurns();
    resetMeter();
    hint.textContent = "已开始新对话 · 点击麦克风或直接打字";
    await refreshConversations();
  }

  /* Wait for the conversation the panel is still creating, so a delete or a "清空全部" is
   * never overtaken by a registration that lands just after it and puts an empty row back. */
  async function settleRegistration() {
    const pending = state.registering;
    if (!pending) return;
    try { await pending; } catch (failure) { /* nothing to wait for if it failed */ }
  }

  /* Delete one conversation: the recorded thread on the server, this browser's recordings of
   * it, and its row. Confirmed first, and the prompt names the thread so a mis-click on the
   * wrong row is visible before it happens. */
  async function deleteConversation(id, title) {
    const label = title ? `「${title}」` : "这条对话";
    const question = `删除${label}？\n\n会同时删掉：本机的对话记录、以及浏览器里保存的这段语音条。此操作不可撤销。`;
    if (typeof window.confirm === "function" && !window.confirm(question)) return false;
    await settleRegistration();
    try {
      await fetch("/local/conversation?scope=voice&conversation_id=" + encodeURIComponent(id),
                  { method: "DELETE" });
    } catch (failure) { /* the local half is still worth doing */ }
    await forgetClips(id);
    if (id === state.conversation) {
      state.turns = [];
      dropClipUrls();
      renderTurns();
      state.conversation = "";
      try { localStorage.removeItem("qwen.local.conversation.v1"); } catch (failure) { /* blocked */ }
    }
    hint.textContent = `已删除${label}`;
    await refreshConversations();
    return true;
  }

  /* Delete everything: every recorded thread plus every recording this browser holds. */
  async function clearAllConversations() {
    const question = "清空全部对话记录？\n\n会同时删掉：本机保存的所有对话、以及浏览器里保存的全部语音条。此操作不可撤销。";
    if (typeof window.confirm === "function" && !window.confirm(question)) return false;
    // Set before the request, not after: a registration that is still in flight (or one that
    // arrives while this is running) must not be able to reopen the history being cleared.
    state.historyCleared = true;
    await settleRegistration();
    let cleared = 0;
    try {
      const response = await fetch("/local/conversation/all", { method: "DELETE" });
      const body = await response.json().catch(() => null);
      if (response.ok && body) cleared = Number(body.cleared) || 0;
    } catch (failure) { /* reported below by the count that remains */ }
    const clips = await forgetClips();
    state.turns = [];
    dropClipUrls();
    renderTurns();
    state.conversation = "";
    state.spokeThisVisit = false;
    // Sit in the cleared state until the owner actually says something: this is what stops the
    // rail from repopulating itself with an empty row the instant the history is cleared.
    state.historyCleared = true;
    try { localStorage.removeItem("qwen.local.conversation.v1"); } catch (failure) { /* blocked */ }
    resetMeter();
    hint.textContent = `已清空 ${cleared} 条对话与 ${clips} 段语音 · 点击麦克风开始新的对话`;
    await refreshConversations();
    return { cleared, clips };
  }

  /* Entering voice opens a *new* conversation.
   *
   * It used to load whatever thread the chat app was on, so the panel opened full of written
   * history — reading as "the voice page is showing me the chat". The left rail is where
   * history belongs now: pick a past conversation there to continue it, or just start talking.
   * Nothing is lost by starting fresh, because every conversation stays listed and one click
   * away.
   *
   * An untouched fresh conversation is deleted again on the way out (see forgetIfEmpty), so
   * opening the page to look at it does not leave an empty row in the rail.
   */
  async function openFresh() {
    if (state.busy) return;
    // After "清空全部" the owner asked for an empty history. Registering a fresh conversation
    // right away would put a row back on screen, which reads as the clear having failed — so
    // the conversation is created lazily, on the first turn (see ask()).
    if (state.historyCleared) return;
    // A previous visit's cleanup may still be in flight. It deletes *its* conversation, and
    // registering a new one alongside it left one empty row per open/close cycle — so the new
    // conversation waits for the old one to be gone.
    if (state.forgetting) { try { await state.forgetting; } catch (failure) { /* ignore */ } }
    // The owner may have picked a past conversation while this was waiting (or the panel may
    // have been closed and reopened): starting fresh is only correct if nobody has chosen a
    // conversation in the meantime. Without this, a slow open wiped a selection made after it.
    if (state.turns.length) return;
    const fresh = "voice-" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
    const mine = (state.openToken || 0) + 1;
    state.openToken = mine;
    // Starting fresh explicitly lifts the post-clear gate: the owner has since asked for a
    // conversation, so registering one is what they want.
    state.historyCleared = false;
    state.conversation = fresh;
    state.spokeThisVisit = false;
    try { localStorage.setItem("qwen.local.conversation.v1", fresh); } catch (failure) { /* blocked */ }
    state.turns = [];
    dropClipUrls();
    renderTurns();
    // Each conversation must also exist on the server, so the meter has something to report on
    // before the first turn and the rail can mark it current — but that write is awaited, so a
    // visit that is closed immediately is cleaned up *after* this registration rather than
    // before it (the cleanup was racing the registration and losing, leaving an empty row per
    // visit).
    try {
      // Recorded while it is in flight so a delete or a "清空全部" that happens right now can
      // wait for it: otherwise this registration lands *after* the deletion and puts a row
      // back, which reads as the delete having failed.
      const registration = json("/local/conversation?conversation_id="
        + encodeURIComponent(fresh) + "&limit=1&window_kind=voice&scope=voice");
      state.registering = registration;
      const value = await registration;
      if (state.registering === registration) state.registering = null;
      // This request is only allowed to finish what it started: a selection made while it was
      // in flight wins, and the empty conversation it created is forgotten instead of kept.
      if (state.openToken !== mine || fresh !== state.conversation) {
        try {
          await fetch("/local/conversation?scope=voice&conversation_id=" + encodeURIComponent(fresh),
                      { method: "DELETE" });
        } catch (failure) { /* best effort */ }
        return;
      }
      // Adopt the identifier the gateway filed this conversation under (`voice·…`): from here on
      // the panel and the rail are talking about the same key, which is what keeps the current
      // row highlighted and lets rename/delete name it exactly.
      if (value && value.conversation_id) {
        state.conversation = value.conversation_id;
        try { localStorage.setItem("qwen.local.conversation.v1", value.conversation_id); } catch (failure) { /* blocked */ }
      }
      if (value && value.meter) setMeter(value.meter);
    } catch (failure) { /* the meter is a nicety */ }
    await refreshConversations();
  }

  async function forgetIfEmpty() {
    const id = state.conversation;
    if (!id || state.spokeThisVisit || state.turns.length) return;
    const work = (async () => {
      try {
        await fetch("/local/conversation?scope=voice&conversation_id=" + encodeURIComponent(id),
                    { method: "DELETE" });
      } catch (failure) { /* the row is cosmetic; a failed cleanup is not worth reporting */ }
    })();
    state.forgetting = work;
    try { await work; } finally { if (state.forgetting === work) state.forgetting = null; }
    state.conversation = "";
    try { localStorage.removeItem("qwen.local.conversation.v1"); } catch (failure) { /* blocked */ }
  }

  /* The settings drawer: one surface for 音色 / 选项 / 运行状态, so the history rail keeps its
   * single job. Kept inside the panel (not a modal over the whole page) so it still reads as
   * one screen, and closed by the gear, its ×, the scrim, Escape or leaving voice mode. */
  function setSettings(visible) {
    state.settings = Boolean(visible);
    settings.hidden = !state.settings;
    scrim.hidden = !state.settings;
    panel.classList.toggle("is-settings-open", state.settings);
    settingsBtn.setAttribute("aria-expanded", String(state.settings));
    if (state.settings) {
      // Land on the chosen voice rather than at the top of a scrolled drawer.
      const chosen = voiceButtons[state.voice];
      if (chosen && chosen.scrollIntoView) chosen.scrollIntoView({ block: "nearest" });
    }
  }

  function open() {
    // Opening is idempotent, and that is load-bearing rather than tidiness: the toolbar
    // re-renders on every status poll and calls setActive(true) each time. Re-entering this
    // function per poll started one new conversation per poll, so the history rail grew rows
    // by itself while nobody had said anything — and each poll's hint text wiped whatever the
    // owner had just read ("已删除…", "已清空…"). Only the first open of a visit does the work.
    const first = !state.open;
    state.open = true;
    panel.hidden = false;
    panel.classList.add("is-open");
    launch.setAttribute("aria-pressed", "true");
    if (!first) return;
    setSettings(false);
    // The subtitle is deliberately left to refreshFacts(), which reads the model actually selected
    // and names it. It used to be set here from a literal ("锁定 27B 三元去审查"), which was true when
    // voice was locked to the ternary 27B and became a lie the moment voice started following the
    // selection: the panel kept announcing 27B while the 9B answered, and because the correct value
    // only arrives with a successful config fetch, a failed fetch left that claim standing.
    hint.textContent = "点击麦克风或直接打字开始 · 需要时再载入语音后端";
    void refreshFacts();
    // A fresh conversation, not the chat app's current one: see openFresh. The rail is
    // populated so a previous conversation is one click away.
    void openFresh();
    /* 打开这一页**不载入任何东西**（所有者口径：顶部导航只换页面，载入要按「确认切换」，
     * 或者真正开始说话的时候）。以前这里会顺手把语音对拉起来做预热，于是"点一下语音"就变成
     * 一次后端切换：工具栏显示"模式切换中"、对话要等十几秒。现在预热挪到两处：
     * `window.VoicePanel.activate()`（工具栏按钮按下的那一刻）与 `startRecording`/发送时（真正用时）。 */
  }

  // Load the voice pair on first use. Safe to call repeatedly; it returns immediately
  // once the backend is up.
  /* Explain a switch in the owner's terms. The gateway decides whether entering voice
   * mode reused the weights already on the card or replaced them; the panel only relays
   * that decision, so it can never claim "no reload" while a model is being loaded. */
  function describeSwitch(info) {
    if (!info) return "";
    if (info.same_source) return "同一底座模型：只切换语音插件，未重新加载权重";
    if (info.reload) return "换底座模型：" + (info.reason || "整套切换");
    return "";
  }

  /* The profile the gateway says is serving voice turns. Read from the reply rather than
   * from the LOCKED constant so a failure to switch shows up as a mismatch instead of a
   * label that keeps claiming 27B while something else answers. */
  function servedLabel(result) {
    const profile = (result && typeof result.profile === "string" && result.profile)
      || voiceProfile || "";
    const label = (result && typeof result.profile_label === "string" && result.profile_label)
      || (profile === voiceProfile ? voiceProfileLabel : "");
    return label || profile || "—";
  }

  /* 这次语音要用哪一份：优先用网关报的档位，没有就现问一次配置。
   * 问不到就退给空串，让网关按"当前选中的档位"处理——服务端才是权威。 */
  async function voiceProfileFor() {
    if (voiceProfile) return voiceProfile;
    try {
      const config = await json(CONFIG_URL);
      rememberVoiceProfile(config);
    } catch (failure) { /* 用空串：网关按当前选中档位处理 */ }
    return voiceProfile;
  }

  function rememberVoiceProfile(config) {
    if (!config || typeof config !== "object") return;
    if (typeof config.profile === "string" && config.profile) voiceProfile = config.profile;
    if (typeof config.profile_label === "string" && config.profile_label) {
      voiceProfileLabel = config.profile_label;
    }
  }

  /* Bring up the backend this turn needs.
   *
   * `quiet` is for the pre-warm that runs as the panel opens. Entering voice mode is refused while
   * any request is in flight - including a reply being generated in another view - and a background
   * optimisation must not answer that refusal with a red error the owner never asked for. A quiet
   * call therefore waits its turn instead (see the catch); a turn's own call is not quiet, so a
   * real failure is still reported. */
  async function activateVoiceMode(need = "text", options = {}) {
    const quiet = Boolean(options && options.quiet);
    const wantAsr = need === "voice";
    const satisfied = () => voiceModeReady && (voiceModeAsrReady || !wantAsr);
    if (satisfied()) return true;
    // A stronger ask (recording after typing) has to wait for whatever is already in
    // flight and then escalate, rather than being answered by the weaker one's result.
    if (voiceModeStarting) {
      await voiceModeStarting;
      if (satisfied()) return true;
    }
    voiceModeStarting = (async () => {
      hint.textContent = wantAsr ? "正在准备语音后端…" : "正在准备对话后端…";
      panel.dataset.phase = "thinking";
      try {
        const result = await json(MODE_URL, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            profile: await voiceProfileFor(), need: wantAsr ? "voice" : "text",
          }),
        });
        rememberVoiceProfile(result);
        voiceModeReady = Boolean(result && result.brain_ready);
        voiceModeAsrReady = Boolean(result && result.asr_ready);
        setFact("asr", wantAsr ? (voiceModeAsrReady ? "就绪" : "加载中") : "打字不需要");
        setFact("llm", voiceModeReady ? servedLabel(result) : "加载中");
        const why = describeSwitch(result && result.switch);
        // The shared meter is shown as soon as the panel opens, so "how much room is left"
        // is answered from the thread the owner is actually continuing.
        void refreshMeter();
        const ready = satisfied();
        hint.textContent = ready
          ? (wantAsr ? "点击麦克风开始 · 说完停顿片刻自动结束" : "发送后回复会在这里读出来")
          : (why || "后端正在加载，请稍候…");
        return ready;
      } catch (failure) {
        // A 503 carries the gateway's own reason (for example the switch it attempted and
        // why it did not take), which is more useful than a bare HTTP code.
        const detail = failure && failure.payload && failure.payload.switch;
        const why = describeSwitch(detail);
        // 409 is the gateway saying a request is in flight, so the model cannot be swapped yet.
        // For a pre-warm that is not a failure but "not now": try again while the reply the owner
        // is actually waiting for finishes.
        const busy = Boolean(failure && failure.status === 409);
        if (quiet && busy && Date.now() < (options.until || 0)) {
          window.setTimeout(() => { void activateVoiceMode(need, options); }, 2500);
          return false;
        }
        if (!quiet) {
          showError("语音后端启动失败：" + (failure.message || failure) + (why ? "｜" + why : ""));
          hint.textContent = why || "启动失败";
        }
        return false;
      } finally {
        panel.dataset.phase = "";
        voiceModeStarting = null;
      }
    })();
    return voiceModeStarting;
  }

  function close() {
    // The mirror of open(): a poll that reports the panel should not be showing it must not
    // re-run the leaving work either, or every poll would fire another cleanup.
    const wasOpen = state.open;
    state.open = false;
    panel.hidden = true;
    panel.classList.remove("is-open");
    launch.setAttribute("aria-pressed", "false");
    if (!wasOpen) return;
    setSettings(false);
    if (state.recording) void stopRecording(false);
    stopSpeaking();
    // A visit that produced nothing is forgotten, so "open the page, look, leave" does not
    // accumulate empty conversations in the rail.
    void forgetIfEmpty();
    // Release the card only if this session actually took it, so merely opening and
    // closing the panel leaves whatever was running untouched.
    if (voiceModeReady) {
      voiceModeReady = false;
      voiceModeAsrReady = false;
      // Releasing is refused while a reply is still in flight (409), and swallowing that leaves the
      // card at the voice window with the synthesiser still resident - a failure the owner would
      // only notice as a text thread that never got its window back. Retry briefly instead.
      const release = attempt => {
        void json(MODE_URL, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ profile: "" }),
        }).catch(failure => {
          if (attempt < 8 && failure && failure.status === 409) {
            window.setTimeout(() => release(attempt + 1), 2500);
          }
        });
      };
      release(0);
    }
  }

  async function refreshFacts() {
    const set = setFact;
    try {
      const config = await json(CONFIG_URL);
      rememberVoiceProfile(config);
      const usable = config.supported !== false;
      titleSub.textContent = usable
        ? `跟随当前模型 ${config.profile_label || ""} · 仅本地`
        : (config.reason || "这一档不能语音");
      set("llm", usable ? (config.profile_label || "—") : "不可用");
      set("asr", "本地");
      // The voice window, straight from the gateway. Not derived from the running backend:
      // before the first spoken turn that backend is still serving the text window.
      if (Number(config.context_size) > 0) {
        state.voiceWindow = Number(config.context_size);
        resetMeter();
      }
    } catch (failure) {
      set("llm", "—");
      // A failed config read must not leave a previous - or placeholder - claim about which model
      // is answering. Say that it is unknown instead of naming the wrong one.
      titleSub.textContent = "档位未知 · 仅本地";
    }
  }

  /* Nothing is measured in a brand-new conversation, but the window is known — showing "0 / 0"
   * for a mode locked to 40,960 was the same wrong number in another form. Until the config
   * arrives the honest answer is "—", not zero. */
  function resetMeter() {
    const total = Number(state.voiceWindow) || 0;
    if (!total) {
      setFact("context", "—");
      const unknown = chips.context;
      if (unknown) { unknown.textContent = "—"; unknown.title = "正在读取语音窗口…"; }
      return;
    }
    setMeter({
      context_size: total, used_tokens: 0, remaining_tokens: Math.max(0, total - lastReserve),
      reserve_tokens: lastReserve, level: "ok", source: "unknown", window_kind: "voice",
    });
  }

  mic.addEventListener("click", () => {
    if (state.recording) void stopRecording(true); else void startRecording();
  });
  settingsBtn.addEventListener("click", () => setSettings(!state.settings));
  settingsClose.addEventListener("click", () => { setSettings(false); settingsBtn.focus(); });
  scrim.addEventListener("click", () => setSettings(false));
  closeBtn.addEventListener("click", () => {
    const modes = window.LocalModes;
    if (modes && modes.select) modes.select("text"); else close();
  });
  launch.addEventListener("click", () => (state.open ? close() : open()));
  document.addEventListener("keydown", event => {
    // Escape unwinds one layer at a time: the drawer first, the page second. Closing the whole
    // page from under an open drawer would lose the owner's place in the settings.
    if (event.key === "Escape" && state.open) {
      if (state.settings) setSettings(false); else close();
      return;
    }
    // Hold-to-talk: space opens the mic while the panel is focused. Not while the settings
    // drawer is open: there the space key belongs to the control that has focus.
    if (event.code === "Space" && state.open && !state.settings && !state.recording &&
        !["INPUT", "TEXTAREA", "BUTTON"].includes(document.activeElement?.tagName)) {
      event.preventDefault();
      void startRecording();
    }
  });
  document.addEventListener("keyup", event => {
    if (event.code === "Space" && state.open && state.recording) void stopRecording(true);
  });

  /* The launcher is hidden on purpose: voice is entered from the mode tabs like every other
   * view. The panel itself is a page, not a dialog — see .local-voice-panel in the stylesheet. */
  launch.setAttribute("aria-hidden", "true");

  renderVoices();
  renderToggles();
  // Ask the gateway which speakers it can render; the table above stays in force if it cannot
  // answer. Deliberately not awaited: the panel opens immediately, the picker corrects itself a
  // moment later if the gateway's list differs.
  void syncVoices();

  // The toolbar owns its own layout, so the launcher is inserted once it exists.
  function mount() {
    const toolbar = document.getElementById("local-mode-toolbar");
    if (!toolbar) { window.setTimeout(mount, 300); return; }
    const row = toolbar.querySelector(".local-mode-row") || toolbar;
    row.append(launch);
    document.body.append(panel);
    window.VoicePanel = {
      setActive: on => { if (on) open(); else close(); },
      isBusy: () => state.busy || state.recording,
      open, close, ask,
      /* Load the voice backend on request (the toolbar's 「确认切换」 on the voice page).
       * Opening the panel no longer does this by itself, so this is the explicit path;
       * `startRecording` and sending a turn still call it at use time as a fallback. */
      activate: (need = "text") => activateVoiceMode(need),
      ready: () => Boolean(voiceModeReady && voiceModeAsrReady),
      /* The clip store, exposed the way the other panels export their internals: a browser
       * check can then prove a recording survives a write/read round trip without needing a
       * microphone, which a headless run cannot provide. The state is exported too, so a check
       * can tell "the cleanup did not run" apart from "the cleanup ran and failed". */
      clips: { store: storeClip, url: clipUrl, drop: dropClipUrls },
      state,
      forgetIfEmpty,
      continueConversation,
    };
  }
  mount();
})();
