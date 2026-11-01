(() => {
  "use strict";

  if (document.getElementById("local-mode-toolbar")) return;
  document.documentElement.lang = "zh-CN";

  /* Adopting the worker without a second fetch.
   *
   * `register()` installs the worker or adopts the existing one, and on a page that
   * already has one it performs the update check itself — so the explicit
   * `registration.update()` that used to follow it fetched /sw.js a second time on every
   * page load. Measured: the one non-polling request in an otherwise silent hidden minute
   * (12 requests/min, of which 11 were the status poll and 1 was /sw.js). `updateViaCache:
   * 'none'` already makes that single check compare bytes instead of trusting the HTTP
   * cache, which is the behaviour this was for. */
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js", { scope: "/", updateViaCache: "none" })
      .catch(failure => console.warn("Qwen local UI worker registration failed", failure));
  }

  // Icon data copied from the Lucide icons in the native llama.cpp Web UI bundle.
  const icons = {
    text: [["path", { d: "M22 17a2 2 0 0 1-2 2H6.828a2 2 0 0 0-1.414.586l-2.202 2.202A.71.71 0 0 1 2 21.286V5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2z" }]],
    vision: [["rect", { width: "18", height: "18", x: "3", y: "3", rx: "2", ry: "2" }], ["circle", { cx: "9", cy: "9", r: "2" }], ["path", { d: "m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21" }]],
    image: [["rect", { width: "18", height: "18", x: "3", y: "3", rx: "2" }], ["path", { d: "m3 15 4-4a2 2 0 0 1 3 0l5 5" }], ["path", { d: "m14 14 1-1a2 2 0 0 1 3 0l3 3" }], ["circle", { cx: "8.5", cy: "8.5", r: "1.5" }]],
    generate: [["path", { d: "m12 3 1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3z" }], ["path", { d: "m19 16 .8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8L19 16z" }]],
    video: [["path", { d: "m16 13 5.223 3.482a.5.5 0 0 0 .777-.416V7.87a.5.5 0 0 0-.752-.432L16 10.5" }], ["rect", { x: "2", y: "6", width: "14", height: "12", rx: "2" }]],
    voice: [["path", { d: "M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" }],
            ["path", { d: "M19 10v1a7 7 0 0 1-14 0v-1" }],
            ["line", { x1: "12", y1: "19", x2: "12", y2: "22" }]],
    retry: [["path", { d: "M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8" }], ["path", { d: "M21 3v5h-5" }]],
    loading: [["path", { d: "M21 12a9 9 0 1 1-6.219-8.56" }]],
    dice: [["rect", { width: "18", height: "18", x: "3", y: "3", rx: "2", ry: "2" }], ["circle", { cx: "8.5", cy: "8.5", r: ".9", fill: "currentColor" }], ["circle", { cx: "15.5", cy: "15.5", r: ".9", fill: "currentColor" }], ["circle", { cx: "12", cy: "12", r: ".9", fill: "currentColor" }]],
    history: [["path", { d: "M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" }], ["path", { d: "M3 3v5h5" }], ["path", { d: "M12 7v5l3.5 2" }]],
    slash: [["circle", { cx: "12", cy: "12", r: "9" }], ["path", { d: "m5.6 5.6 12.8 12.8" }]],
    // Shield, for the global tool-permission switch.
    shield: [["path", { d: "M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z" }]],
  };
  const words = {
    text: "\u7eaf\u6587\u672c", vision: "\u8bc6\u56fe", generate: "\u751f\u56fe",
    // 导航只换页面；换模型是"选中 → 确认切换"两步，所以这里要有确认与状态的字。
    applyModel: "\u786e\u8ba4\u5207\u6362", loaded: "\u5df2\u8f7d\u5165",
    modeNow: "\u5f53\u524d", pendingModel: "\u5f85\u5207\u6362\u5230",
    video: "\u89c6\u9891\u751f\u6210", local: "\u672c\u5730",
    modes: "\u6a21\u578b\u6a21\u5f0f", connecting: "\u8fde\u63a5\u4e2d",
    starting: "\u6a21\u578b\u52a0\u8f7d\u4e2d", switching: "\u6a21\u5f0f\u5207\u6362\u4e2d",
    ready: "\u5df2\u5c31\u7eea", busy: "\u751f\u6210\u4e2d", error: "\u52a0\u8f7d\u5931\u8d25",
    enabled: "\u5f00\u542f", disabled: "\u5173\u95ed", pending: "\u5f85\u5c31\u7eea",
    retry: "\u91cd\u8bd5", offline: "\u672c\u5730\u670d\u52a1\u8fde\u63a5\u5931\u8d25",
    // 连不上持续久了，最可能的原因就是"服务已经按口径收工了"（关页面、或后台标签被冻结到超过
    // 窗口）。那时反复重试没有意义，要直接告诉主人怎么把它叫回来。
    offlineStopped: "本地服务已停止（关掉页面后会自动收工）。双击桌面「Qwen 共享面板」重新启动。",
    // Shown while a poll is failing but the service has not been declared offline yet. A
    // single miss is not a disconnection: the browser aborts in-flight polls on an
    // in-page reload, and a model switch can outlast one poll interval.
    waiting: "\u6b63\u5728\u7b49\u5f85\u672c\u5730\u670d\u52a1\u54cd\u5e94",
    signedOut: "\u767b\u5f55\u5df2\u8fc7\u671f\uff0c\u6b63\u5728\u91cd\u65b0\u767b\u5f55",
    wait: "\u751f\u6210\u7ed3\u675f\u540e\u53ef\u5207\u6362\u6a21\u5f0f",
    failed: "\u5207\u6362\u5931\u8d25", storage: "\u65e0\u6cd5\u4fdd\u5b58\u8f93\u5165\u8349\u7a3f\uff0c\u6a21\u5f0f\u5207\u6362\u5df2\u53d6\u6d88",
    attachments: "\u8bf7\u5148\u53d1\u9001\u6216\u79fb\u9664\u5f85\u53d1\u9001\u9644\u4ef6",
    waiting: "等待显存空闲", stopping: "服务正在停止", sleeping: "待机中",
    agentReady: "智能体已就绪", agentBusy: "智能体运行中",
    recovering: "本地服务正在恢复", refresh: "连接已恢复，移除附件后可刷新界面",
    richDraft: "请先发送或清空输入草稿，再刷新界面或切换模式",
    imageBusy: "生图任务进行中，完成后会自动恢复对话模型",
  };
  const draftKey = "qwen.local.mode-draft.v1";
  // Which tab the user opened, independent of the backend mode. The backend
  // unloads the language model while generating and comes back in text/vision,
  // so the generate view must not be tied to the reported mode.
  const tabKey = "qwen.local.tab.v1";
  let activeTab = null;
  try {
    const stored = localStorage.getItem(tabKey);
    if (["text", "vision", "voice", "generate", "video"].includes(stored)) activeTab = stored;
  } catch (failure) {
    // Blocked storage just means the tab choice is not remembered.
  }
  // The first generate-view release kept its history in the browser; records now
  // live on the server (logs/image-history.json), so drop the stale local copy.
  try {
    localStorage.removeItem("qwen.local.image-history.v1");
  } catch (failure) {
    // Blocked storage is not a reason to stop the toolbar from loading.
  }
  let current = null;
  let initialRevision;
  let initialMode;
  let initialInstance;
  let reconnectNeeded = false;
  let switching = false;
  let recovering = false;
  let polling = false;
  let reloading = false;
  let localError = "";
  let disconnected = false;
  let pollTimer;
  let modeRequestVersion = 0;
  let modelRequestVersion = 0;
  let initialAlias;
  /* The voice mode's own hardware rule, read from the gateway. Cached across polls because
   * it only changes when the model does, and a failed fetch leaves the last answer in place
   * rather than making a working button look unavailable. */
  const voiceConfig = { locked: false, supported: true, profile: "", reason: "" };
  let voiceConfigProfile = null;
  let voiceConfigFetched = false;
  /* How many polls in a row must fail before the toolbar says the service is gone. One
   * miss is normal: the page reloads itself after a completed mode change, which aborts
   * the poll in flight at that instant, and a model switch can outlast one interval. */
  const OFFLINE_AFTER_FAILURES = 3;
  const OFFLINE_RETRY_MS = 700;
  /* 连续失败到这么多次（约十几秒）就换口径：不再是"连接失败"，而是"服务已停止，双击面板重新
     启动"。3 次只说连接失败是对的——一次模式切换就能让两三次轮询落空。 */
  const OFFLINE_STOPPED_AFTER_FAILURES = 8;
  let pollsFailed = 0;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  /* Work that only matters while the page is on screen runs at full rate when it is
     visible and at a slow tick when it is not.
     
     Measured before this existed: a hidden tab still ran the toolbar's 1 s
     followConversation+mountComposerTools pass and voice-input's 1 s re-park, and the
     MutationObserver below re-rendered the toolbar on every change inside the composer
     (a streaming reply is thousands of them). None of that is visible to a request
     counter, which is why it survived. The intervals are not removed, only decelerated,
     and returning to the tab restores the full rate immediately — nothing that was
     noticed within one second on screen waits longer than one tick. */
  const HIDDEN_TICK_MS = 5000;
  function every(period, run, options) {
    const busy = (options && options.busy) || null;
    let timer = null;
    let stopped = false;
    const active = () => document.visibilityState !== "hidden" || Boolean(busy && busy());
    function tick() {
      const delay = active() ? period : HIDDEN_TICK_MS;
      timer = setTimeout(() => {
        if (stopped) return;
        if (active()) { try { run(); } catch (failure) { /* one pass must not stop the loop */ } }
        tick();
      }, delay);
    }
    function onVisibility() {
      if (document.visibilityState === "hidden") return;
      if (timer) clearTimeout(timer);
      try { run(); } catch (failure) { /* as above */ }
      tick();
    }
    tick();
    document.addEventListener("visibilitychange", onVisibility);
    return { period, stop() { stopped = true; if (timer) clearTimeout(timer); document.removeEventListener("visibilitychange", onVisibility); } };
  }

  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    for (const [key, value] of Object.entries({ viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" })) svg.setAttribute(key, value);
    for (const [tag, attributes] of icons[name]) {
      const part = document.createElementNS(svg.namespaceURI, tag);
      for (const [key, value] of Object.entries(attributes)) part.setAttribute(key, value);
      svg.append(part);
    }
    return svg;
  }

  words.voice = "\u8bed\u97f3\u5bf9\u8bdd";
  // Shown on the voice button when the selected model cannot serve it. Escaped ASCII like
  // every other string in this file, so an accidental encoding round-trip cannot mangle it.
  words.unsupported = "\u4e0d\u652f\u6301\u7684\u786c\u4ef6";

  // Model picker strings; escaped so this block stays ASCII.
  words.model = "\u6a21\u578b";
  words.tradeoffs = "\u5f53\u524d\u6a21\u578b\u7684\u4f18\u7f3a\u70b9";
  words.tradeoffsSuffix = " \u00b7 \u4f18\u7f3a\u70b9";
  words.pros = "\u4f18\u70b9";
  words.cons = "\u7f3a\u70b9";
  words.close = "\u5173\u95ed";
  words.notInstalled = "\uff08\u672a\u4e0b\u8f7d\u5b8c\u6210\uff09";
  words.modelSwitching = "\u6a21\u578b\u5207\u6362\u4e2d";
  words.modelFailed = "\u6a21\u578b\u5207\u6362\u5931\u8d25";
  // Model-comparison panel strings. Escaped like the block above.
  words.modelCompare = "\u5f53\u524d\u6a21\u578b";
  words.inUse = "\u5f53\u524d\u4f7f\u7528";
  words.switchTo = "\u5207\u6362\u5230\u6b64\u6a21\u578b";
  words.notInstalledNote = "\u672a\u4e0b\u8f7d\u5b8c\u6210\uff0c\u6682\u4e0d\u53ef\u7528\u3002";

  // Tool permission, per conversation. The chat UI asks "allow once / always allow" for every
  // tool call and remembers only the second answer, in localStorage, keyed by tool *type*:
  // ``browser:read_media`` / ``server:read_file`` / ``mcp-<name>:<tool>`` / ``custom:<tool>``.
  // Two measured facts about the bundled UI shape this control:
  //   · ``GET /tools`` answers with the server's seven tools only (read_file, file_glob_search,
  //     grep_search, exec_shell_command, write_file, edit_file, get_info). The browser tools the
  //     app owns — get_datetime, read_media, run_javascript — are not in that answer, so the old
  //     page-wide switch filled the list with ``server:*`` entries and the app still asked for
  //     read_media while the toolbar said 已全局放行.
  //   · the app reads that list once at boot and rewrites it whole on every decision, so a write
  //     made later could be dropped again by the app's own persist().
  // So the switch lives where the question appears — under the composer — its state is per
  // conversation, and writes to the app's list are merged rather than replaced.
  const permissionKeys = ["LlamaUi.alwaysAllowedTools", "LlamacppWebui.alwaysAllowedTools"];
  const permissionBaselineKey = "qwen.local.toolPermission.baseline.v1";
  const permissionFlagPrefix = "qwen.local.toolPermission.conversation.";
  const permissionAddedPrefix = "qwen.local.toolPermission.added.";
  // The browser tools the bundled UI defines for itself (its own tool enum). They have to be
  // named here because the gateway's /tools list is the server's list, not the app's.
  const BROWSER_TOOL_NAMES = ["get_datetime", "read_media", "run_javascript"];
  words.permission = "\u5de5\u5177\u6743\u9650";
  words.grantThisChat = "\u672c\u5bf9\u8bdd\u653e\u884c";
  words.grantedThisChat = "\u672c\u5bf9\u8bdd\u5df2\u653e\u884c";
  words.askingThisChat = "\u9010\u6b21\u8be2\u95ee";
  words.grantThisChatTitle = "\u672c\u5bf9\u8bdd\u653e\u884c\uff1a\u6a21\u578b\u5728\u8fd9\u6761\u5bf9\u8bdd\u91cc\u8c03\u7528\u5de5\u5177\u4e0d\u518d\u8be2\u95ee";
  words.askingThisChatTitle = "\u6bcf\u6b21\u8c03\u7528\u5de5\u5177\u90fd\u4f1a\u8be2\u95ee\uff08\u4ec5\u5141\u8bb8\u4e00\u6b21 / \u603b\u662f\u5141\u8bb8\uff09";
  words.grantConfirm = "\u672c\u5bf9\u8bdd\u653e\u884c\u540e\uff0c\u6a21\u578b\u5728\u300c\u8fd9\u6761\u5bf9\u8bdd\u300d\u91cc\u8c03\u7528\u4efb\u4f55\u5de5\u5177\u90fd\u4e0d\u518d\u5f39\u7a97\u8be2\u95ee\uff1a"
    + "\u8bfb\u53d6\u4efb\u610f\u6587\u4ef6\u3001\u5199\u5165\u6216\u4fee\u6539\u6587\u4ef6\u3001\u6267\u884c shell \u547d\u4ee4\u3001"
    + "\u8bfb\u53d6\u4f60\u53d1\u7684\u56fe\u7247\u4e0e\u89c6\u9891\u90fd\u4f1a\u76f4\u63a5\u6267\u884c\u3002"
    + "\u5207\u5230\u522b\u7684\u5bf9\u8bdd\u4ecd\u7136\u662f\u9010\u6b21\u8be2\u95ee\uff0c\u968f\u65f6\u53ef\u4ee5\u5728\u540c\u4e00\u4e2a\u6309\u94ae\u4e0a\u6536\u56de\u3002\u786e\u5b9a\u8981\u5f00\u542f\u5417\uff1f";

  const toolbar = element("header", "local-mode-toolbar");
  toolbar.id = "local-mode-toolbar";
  toolbar.setAttribute("aria-label", words.modes);
  const row = element("div", "local-mode-row");
  const brand = element("div", "local-mode-brand");
  brand.append(element("strong", "", "Qwen"), element("span", "", words.local));
  const modes = element("div", "local-mode-segments");
  modes.setAttribute("role", "group");
  modes.setAttribute("aria-label", words.modes);
  // Per-mode cost, shown as a small label on each tab. Voice states the model it is bound to, and
  // that is the gateway's answer (`/local/voice/config` -> voiceSize, applied per render below), not
  // this map's: the `"27B"` literal that used to sit here was a leftover from when voice was locked
  // to the ternary 27B, and it announced the wrong model on every render that ran before the config
  // arrived. Empty means "not known yet", which is what is actually true at this point.
  const modeSizes = { text: "快", vision: "投影", voice: "", generate: "Klein", video: "H3" };
  const sizeLabels = {};
  const buttons = {};
  for (const mode of ["text", "vision", "voice", "generate", "video"]) {
    const button = element("button", "local-mode-button");
    button.type = "button";
    button.dataset.mode = mode;
    button.setAttribute("aria-pressed", "false");
    // The size label is where a mode states what it costs; for voice it is also where the
    // "unsupported here" state is shown, because a disabled button alone explains nothing.
    const size = element("small", "local-mode-size", modeSizes[mode] || "");
    button.append(icon(mode), element("span", "", words[mode]), size);
    button.addEventListener("click", () => selectTab(mode));
    modes.append(button);
    buttons[mode] = button;
    sizeLabels[mode] = size;
  }
  // --- tool permission, per conversation -------------------------------------
  let toolKeys = [];
  let grantedChatId = currentChatId();

  /* The app's own route is ``/(chat)/chat/[id]``: an open conversation is ``/chat/<id>``, and
     the root is the not-yet-saved new one. That id is what makes the switch per conversation. */
  function currentChatId() {
    const match = /\/chat\/([^/?#]+)/.exec(location.pathname || "");
    try { return match ? decodeURIComponent(match[1]) : "new-chat"; } catch { return "new-chat"; }
  }

  function chatFlagKey(id) { return permissionFlagPrefix + id; }
  function chatAddedKey(id) { return permissionAddedPrefix + id; }

  function chatGranted(id) {
    try { return localStorage.getItem(chatFlagKey(id || currentChatId())) === "true"; } catch { return false; }
  }

  function readList(key) {
    try {
      const parsed = JSON.parse(localStorage.getItem(key) || "[]");
      return Array.isArray(parsed) ? parsed.map(String) : [];
    } catch { return []; }
  }

  function writeList(key, list) {
    try { localStorage.setItem(key, JSON.stringify([...new Set(list)])); } catch { /* private mode */ }
  }

  function readAllowedTools() {
    for (const key of permissionKeys) {
      let raw = null;
      try { raw = localStorage.getItem(key); } catch { /* private mode */ }
      if (raw !== null) {
        try {
          const parsed = JSON.parse(raw);
          if (Array.isArray(parsed)) return { key, list: parsed.map(String) };
        } catch { /* unreadable: written again below */ }
        return { key, list: [] };
      }
    }
    return { key: permissionKeys[0], list: [] };
  }

  /* What the app had allowed before this control ever touched the list. Kept so switching a
     conversation off gives the app's own decisions back instead of erasing them. */
  function permissionBaseline() {
    let stored = null;
    try { stored = localStorage.getItem(permissionBaselineKey); } catch { /* private mode */ }
    if (stored !== null) {
      try {
        const parsed = JSON.parse(stored);
        if (Array.isArray(parsed)) return parsed.map(String);
      } catch { /* rewrite below */ }
    }
    const captured = readAllowedTools().list;
    writeList(permissionBaselineKey, captured);
    return captured;
  }

  /* Which tools exist is the gateway's answer for the server side — a tool added there must be
     covered without touching this file — plus the browser tools the app defines for itself,
     which ``/tools`` never lists (that omission is why read_media kept asking).

     Answering costs 6.4 KiB and a gateway request. Measured before this was cached: this page
     asked three times before it finished booting (the attribution run attributed all three to
     loadToolKeys, at 70 ms, 90 ms and 418 ms), and it asked again on every reload — and this
     page reloads itself after a completed mode change. The answer is therefore kept for a
     short while, concurrent askers share one request, and the copy survives the reload
     through sessionStorage, which is scoped to this tab rather than to the profile.

     A TTL rather than forever: the tool list is the gateway's, and a tool installed while a
     tab is open must not stay invisible to the permission chip. The chip's own behaviour is
     unchanged — it still writes exactly the list it always wrote. */
  const TOOL_KEYS_TTL_MS = 5 * 60 * 1000;
  const TOOL_KEYS_CACHE = "qwen.local.toolKeys.v1";
  let toolKeysPromise = null;
  let toolKeysAt = 0;
  function browserToolKeys() {
    return BROWSER_TOOL_NAMES.map(name => "browser:" + name);
  }
  function cachedToolKeys() {
    try {
      const raw = sessionStorage.getItem(TOOL_KEYS_CACHE);
      if (!raw) return null;
      const stored = JSON.parse(raw);
      if (!stored || !Array.isArray(stored.keys) || !stored.keys.length) return null;
      if (!Number.isFinite(stored.at) || Date.now() - stored.at > TOOL_KEYS_TTL_MS) return null;
      return stored.keys.map(String);
    } catch { return null; }
  }
  function rememberToolKeys(keys) {
    try { sessionStorage.setItem(TOOL_KEYS_CACHE, JSON.stringify({ at: Date.now(), keys })); }
    catch { /* private mode: the in-memory copy still covers this page load */ }
  }
  async function loadToolKeys() {
    const browser = browserToolKeys();
    if (toolKeysPromise && Date.now() - toolKeysAt < TOOL_KEYS_TTL_MS) return toolKeysPromise;
    const remembered = cachedToolKeys();
    if (remembered) {
      toolKeys = remembered;
      toolKeysAt = Date.now();
      toolKeysPromise = Promise.resolve(toolKeys);
      return toolKeysPromise;
    }
    toolKeysAt = Date.now();
    toolKeysPromise = (async () => {
      try {
        const response = await fetch("/tools", { cache: "no-store", signal: AbortSignal.timeout(8000) });
        if (response.ok) {
          const tools = await response.json();
          if (Array.isArray(tools)) {
            const server = tools.filter(tool => tool && tool.tool)
              .map(tool => (tool.type || "server") + ":" + tool.tool);
            toolKeys = [...new Set(server.concat(browser))];
            rememberToolKeys(toolKeys);
            return toolKeys;
          }
        }
      } catch { /* keep whatever we had */ }
      if (!toolKeys.length) toolKeys = browser;
      // A failed ask is not cached, so the next caller can still succeed.
      toolKeysAt = 0;
      return toolKeys;
    })();
    return toolKeysPromise;
  }

  /* The list the app should hold right now: the baseline always, every tool while this
     conversation is granted. */
  function desiredAllowed() {
    const baseline = permissionBaseline();
    if (!chatGranted(grantedChatId)) return baseline;
    return [...new Set(baseline.concat(toolKeys))];
  }

  function applyPermission() {
    const current = readAllowedTools();
    const granted = chatGranted(grantedChatId);
    if (granted) {
      const baseline = permissionBaseline();
      writeList(chatAddedKey(grantedChatId), toolKeys.filter(key => !baseline.includes(key)));
      writeList(current.key, desiredAllowed());
      // A tool the owner explicitly disabled stays disabled: the always-allow list and the
      // disabled list are separate, and opening this must not resurrect them.
      return;
    }
    // Off: take back exactly what this conversation added, and leave everything else alone.
    const added = readList(chatAddedKey(grantedChatId));
    writeList(chatAddedKey(grantedChatId), []);
    writeList(current.key, current.list.filter(entry => !added.includes(entry)));
  }

  /* The app rewrites the whole list whenever it records a decision (its own persist()), which
     would drop what this control added. Merging on the way in is what makes the grant survive
     the app's own bookkeeping — and what keeps a revoked conversation revoked. */
  function guardAllowedWrites() {
    const native = Storage.prototype.setItem;
    if (native.qwenPermissionGuard) return;
    const guarded = function (key, value) {
      if (permissionKeys.includes(key)) {
        try {
          const incoming = JSON.parse(String(value) || "[]");
          if (Array.isArray(incoming)) {
            const granted = chatGranted(grantedChatId);
            const values = incoming.map(String);
            const next = granted
              ? [...new Set(values.concat(toolKeys))]
              : values.filter(entry => !readList(chatAddedKey(grantedChatId)).includes(entry));
            return native.call(this, key, JSON.stringify(next));
          }
        } catch { /* not a list: write it exactly as the app asked */ }
      }
      return native.call(this, key, value);
    };
    guarded.qwenPermissionGuard = true;
    Storage.prototype.setItem = guarded;
  }

  const permissionButton = element("button", "local-mode-permission local-composer-chip");
  permissionButton.type = "button";
  permissionButton.append(icon("shield"), element("span", "local-mode-permission-text", words.askingThisChat));

  const permissionDialog = element("div", "local-permission-dialog");
  permissionDialog.hidden = true;
  permissionDialog.setAttribute("role", "alertdialog");
  permissionDialog.setAttribute("aria-modal", "true");
  const permissionDialogTitle = element("h2", "", words.grantThisChat);
  const permissionDialogBody = element("p", "", words.grantConfirm);
  const permissionDialogActions = element("div", "local-permission-actions");
  const permissionConfirm = element("button", "local-permission-confirm", words.grantThisChat);
  permissionConfirm.type = "button";
  const permissionCancel = element("button", "local-permission-cancel", words.close);
  permissionCancel.type = "button";
  permissionDialogActions.append(permissionConfirm, permissionCancel);
  permissionDialog.append(permissionDialogTitle, permissionDialogBody, permissionDialogActions);
  toolbar.append(permissionDialog);

  permissionConfirm.addEventListener("click", async () => {
    permissionDialog.hidden = true;
    try { localStorage.setItem(chatFlagKey(grantedChatId), "true"); } catch { /* private mode */ }
    await loadToolKeys();
    applyPermission();
    renderPermission();
  });
  permissionCancel.addEventListener("click", () => { permissionDialog.hidden = true; });
  permissionButton.addEventListener("click", () => {
    if (!chatGranted(grantedChatId)) { permissionDialog.hidden = false; permissionConfirm.focus(); return; }
    try { localStorage.setItem(chatFlagKey(grantedChatId), "false"); } catch { /* private mode */ }
    applyPermission();
    renderPermission();
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && !permissionDialog.hidden) permissionDialog.hidden = true;
  });

  function renderPermission() {
    const granted = chatGranted(grantedChatId);
    permissionButton.dataset.granted = String(granted);
    permissionButton.setAttribute("aria-pressed", String(granted));
    const text = permissionButton.querySelector(".local-mode-permission-text");
    if (text) text.textContent = granted ? words.grantedThisChat : words.askingThisChat;
    permissionButton.title = granted ? words.grantThisChatTitle : words.askingThisChatTitle;
    permissionButton.setAttribute("aria-label",
      words.permission + "\uff08" + grantedChatId + "\uff09\uff1a" + (granted ? words.grantedThisChat : words.askingThisChat));
  }

  /* Every conversation carries its own answer, so the control follows the route: walking into
     another chat re-derives the list from *that* chat's flag. The history patching catches the
     app's own client-side navigation (a SvelteKit pushState, not a page load). */
  function followConversation() {
    const id = currentChatId();
    if (id === grantedChatId) return;
    const previous = grantedChatId;
    grantedChatId = id;
    const baseline = permissionBaseline();
    const added = readList(chatAddedKey(previous));
    if (added.length) {
      const { key, list } = readAllowedTools();
      writeList(chatAddedKey(previous), []);
      writeList(key, list.filter(entry => !added.includes(entry) || baseline.includes(entry)));
    }
    if (chatGranted(id)) applyPermission();
    renderPermission();
  }

  for (const method of ["pushState", "replaceState"]) {
    const native = history[method];
    if (typeof native === "function") {
      history[method] = function (...args) {
        const result = native.apply(this, args);
        setTimeout(followConversation, 0);
        return result;
      };
    }
  }
  window.addEventListener("popstate", () => setTimeout(followConversation, 0));
  window.addEventListener("hashchange", () => setTimeout(followConversation, 0));
  guardAllowedWrites();
  loadToolKeys().then(() => { applyPermission(); renderPermission(); });
  every(1000, () => { followConversation(); mountComposerTools(); });

  /* The dock under the composer: the permission chip and the microphone live together at the
     bottom-right of the box the question appears in. voice-input.js mounts its mic into this
     same element, so the two controls never fight over the corner. */
  function mountComposerTools() {
    const host = document.querySelector(".conversation-chat-form");
    if (!host) return null;
    let dock = document.getElementById("local-composer-tools");
    if (!dock) {
      dock = element("div", "local-composer-tools");
      dock.id = "local-composer-tools";
    }
    if (dock.parentElement !== host) host.append(dock);
    if (permissionButton.parentElement !== dock) dock.prepend(permissionButton);
    return dock;
  }
  mountComposerTools();
  renderPermission();


  // --- model picker ---------------------------------------------------------
  // Which model profile the backend serves. This is a peer of the mode buttons
  // rather than part of them: a mode picks what the backend does, a model picks
  // which weights do it, and only the latter needs a restart.
  const picker = element("label", "local-model-picker");
  const pickerLabel = element("span", "local-model-picker-label", words.model);
  const modelSelect = element("select", "local-model-select");
  modelSelect.setAttribute("aria-label", words.model);
  let modelOptions = [];
  let showingTradeoffs = false;

  const tradeoffs = element("section", "local-model-tradeoffs");
  tradeoffs.hidden = true;
  const tradeoffsTitle = element("h2", "local-model-tradeoffs-title");
  const tradeoffsBody = element("div", "local-model-tradeoffs-body");
  const tradeoffsClose = element("button", "local-model-tradeoffs-close");
  tradeoffsClose.type = "button";
  tradeoffsClose.textContent = words.close;
  tradeoffsClose.addEventListener("click", () => { showingTradeoffs = false; renderTradeoffs(); });
  tradeoffs.append(tradeoffsTitle, tradeoffsClose, tradeoffsBody);

  const tradeoffsButton = element("button", "local-model-info");
  tradeoffsButton.type = "button";
  tradeoffsButton.setAttribute("aria-label", words.tradeoffs);
  tradeoffsButton.title = words.tradeoffs;
  tradeoffsButton.textContent = "?";
  tradeoffsButton.addEventListener("click", event => {
    event.preventDefault();
    showingTradeoffs = !showingTradeoffs;
    renderTradeoffs();
  });

  /* 「确认切换」是**按页**的：它载入的是当前这一页要用的模型，而不是去换语言模型的档位。
   * 所有者口径：点导航只换页面，什么都不载入；要载入就按这个按钮（或者真正开始用的时候）。
   *   纯文本 / 识图 → 载入语言模型（识图页要的是带投影的那一档）
   *   语音对话     → 载入语音后端（语言权重 + 识别器这一对）
   *   生图 / 视频   → 由 ComfyUI 在真正生成时按需启动，这里不给假按钮
   * 旁边的 `modelNow` 一直写清楚"现在是什么页、载入了什么"，避免"以为已经载入了"。 */
  let pendingModel = null;

  function loadedProfileName() {
    return (current && (current.loaded_profile || current.model_profile)) || "";
  }

  function profileLabelOf(name) {
    const list = Array.isArray(current?.model_profiles) ? current.model_profiles : [];
    const found = list.find(profile => profile.name === name);
    return found ? (found.label || found.name) : (name || "\u2014");
  }

  function profileTaglineOf(name) {
    const list = Array.isArray(current?.model_profiles) ? current.model_profiles : [];
    const found = list.find(profile => profile.name === name);
    return found ? (found.tagline || "") : "";
  }

  /* 状态行里只放"能区分是哪一份"的最短名字（9B / 27B）：完整档位名（"27B 三元去审查"）会把这一行
   * 撑到 190px 以上，"已载入"那半句就被挤出去了（实测：切到 27B 之后这行被省略号切断）。全名在
   * 悬浮 title 和下拉里都有。 */
  function shortLabel(name) {
    const label = profileLabelOf(name);
    return label.split(/\s+/)[0];
  }

  const modeLabel = mode => ({
    text: words.text, vision: words.vision, voice: words.voice || "\u8bed\u97f3",
    generate: words.generate, image: words.generate, video: words.video,
  }[mode] || mode || "\u2014");

  /* 语音后端在不在：网关在语音对占着显卡期间把 mode 报成 "voice"（borrowed_by_voice 也会置位）。 */
  function voiceBackendLoaded() {
    return Boolean(current && (current.mode === "voice" || current.borrowed_by_voice));
  }

  /* 当前页要载入什么、已经载入了什么。`action` 为空 = 这一页没有可载入的东西（生图/视频按需启动）。
   * `text` 是要显示在工具栏里的**短句**（这一行只有 ~200px，长了就被省略号切掉，看起来就是"显示有
   * 问题"）；`detail` 是完整句子，挂在悬浮 title 上。 */
  function pageLoadState() {
    const view = activeTab || (current && current.mode) || "text";
    const loadedLabel = profileLabelOf(loadedProfileName());
    if (view === "voice") {
      const loaded = voiceBackendLoaded();
      return {
        view,
        loaded,
        text: loaded ? `\u5df2\u8f7d\u5165\u8bed\u97f3\u540e\u7aef` : "\u672a\u8f7d\u5165\u8bed\u97f3\u540e\u7aef",
        detail: loaded
          ? `\u8bed\u97f3\u540e\u7aef\u5df2\u8f7d\u5165\uff08${loadedLabel} \u00b7 \u8bc6\u522b\u5668\u5c31\u7eea\uff09`
          : "\u8bed\u97f3\u540e\u7aef\u8fd8\u6ca1\u8f7d\u5165\uff1a\u6309\u65c1\u8fb9\u7684\u6309\u94ae\uff0c\u6216\u76f4\u63a5\u5f00\u59cb\u8bf4\u8bdd",
        action: loaded ? "" : "\u8f7d\u5165\u8bed\u97f3\u540e\u7aef",
      };
    }
    if (view === "generate" || view === "video") {
      const job = current && current.image && current.image.state;
      const loaded = Boolean(job && job !== "idle" && job !== "error");
      return {
        view,
        loaded,
        text: loaded ? `ComfyUI \u5df2\u8f7d\u5165` : "ComfyUI \u6309\u9700\u542f\u52a8",
        detail: loaded
          ? `ComfyUI \u5df2\u8f7d\u5165\uff08${job}\uff09`
          : "ComfyUI \u4e0d\u9884\u5148\u5360\u7528\uff1a\u5f00\u59cb\u751f\u6210\u65f6\u624d\u542f\u52a8",
        action: "",
      };
    }
    // 纯文本 / 识图：这一页要的是语言模型，而且要在对应的档上（识图要投影）。
    const modeOk = Boolean(current && current.mode === view && current.state === "ready");
    const pending = pendingModel && pendingModel !== loadedProfileName();
    const backendMode = modeLabel(current && current.mode);
    // 短句只到"载入了什么"为止：状态行固定 ~200px，"未载入（后端现在是 纯文本）"实测 212px 会被切。
    const text = modeOk ? `\u5df2\u8f7d\u5165 ${shortLabel(loadedProfileName())}` : "\u672a\u8f7d\u5165";
    const detail = modeOk
      ? `\u8f7d\u5165\u7684\u662f ${loadedLabel}`
      : `\u672a\u8f7d\u5165\uff08\u540e\u7aef\u73b0\u5728\u662f ${backendMode}\uff09`;
    // 按钮文字也取短名（档位名截到第一个空格），整行宽度才不会随页面变化把模式按钮推来推去。
    const shortProfile = pending ? profileLabelOf(pendingModel).split(/\s+/)[0] : "";
    return {
      view,
      loaded: modeOk && !pending,
      text,
      detail,
      action: pending ? `\u52a0\u8f7d ${shortProfile}` : `\u8f7d\u5165${modeLabel(view)}`,
    };
  }

  const modelApplyButton = element("button", "local-model-apply");
  modelApplyButton.type = "button";
  modelApplyButton.addEventListener("click", () => {
    const page = pageLoadState();
    if (!page.action) { render(); return; }
    if (page.view === "voice") {
      // 语音后端由语音页自己带起来（它会复用同一套权重或按语音窗口重载），这里只是替主人按下去。
      if (window.VoicePanel && typeof window.VoicePanel.activate === "function") {
        void window.VoicePanel.activate("text");
      } else {
        localError = words.failed;
        render();
      }
      return;
    }
    if (!canChangeMode()) { localError = words.wait; render(); return; }
    if (pendingModel && pendingModel !== loadedProfileName()) { void changeModel(pendingModel); return; }
    void changeMode(page.view);
  });

  const modelNow = element("span", "local-model-now");
  modelNow.setAttribute("aria-live", "polite");

  picker.append(pickerLabel, modelSelect, modelApplyButton, tradeoffsButton);

  // Fill the options once from the gateway's report, so a model added on the
  // server side shows up without touching this file.
  function syncModelOptions(profiles) {
    if (!Array.isArray(profiles) || !profiles.length) return;
    const signature = profiles.map(p => p.name + ":" + p.installed).join("|");
    if (signature === modelOptions.join("|")) return;
    modelOptions = profiles.map(p => p.name + ":" + p.installed);
    modelSelect.textContent = "";
    for (const profile of profiles) {
      const option = document.createElement("option");
      option.value = profile.name;
      // An uninstalled profile is listed but unselectable: hiding it would make
      // the picker look broken while its weights are still downloading.
      option.disabled = !profile.installed;
      option.textContent = profile.installed ? profile.label : profile.label + words.notInstalled;
      // The label can be wider than the control, so the full text is always reachable
      // without opening the list.
      option.title = option.textContent;
      modelSelect.append(option);
    }
  }

  function activeProfile() {
    const list = Array.isArray(current?.model_profiles) ? current.model_profiles : [];
    return list.find(p => p.name === current?.model_profile) || null;
  }

  function renderTradeoffs() {
    tradeoffs.hidden = !showingTradeoffs;
    tradeoffsButton.setAttribute("aria-expanded", String(showingTradeoffs));
    if (tradeoffs.hidden) return;
    const profiles = Array.isArray(current?.model_profiles) ? current.model_profiles : [];
    if (!profiles.length) { tradeoffs.hidden = true; return; }
    // 这个小页面现在只介绍**当前正在用的那个模型**：切模型是上面下拉框的事，
    // 这里回答的是"我现在用的这份是什么来路、强在哪、弱在哪"。
    const active = profiles.find(p => p.name === current?.model_profile) || profiles[0];
    tradeoffsTitle.textContent = active.label
      ? `${words.modelCompare} · ${active.label}` : words.modelCompare;
    tradeoffsBody.textContent = "";

    const card = element("article", "local-model-card");
    card.dataset.active = "true";

    const head = element("header", "local-model-card-head");
    const title = element("div", "local-model-card-title");
    title.append(element("strong", "", active.label || active.name));
    if (active.tagline) title.append(element("span", "", active.tagline));
    head.append(title);
    card.append(head);

    if (active.size) card.append(element("div", "local-model-card-size", active.size));

    // Bars stay: with one model they read as "这份的脾气"，不再是比较用的条形图。
    const scores = active.scores || {};
    const keys = Object.keys(scores);
    if (keys.length) {
      const bars = element("div", "local-model-bars");
      for (const key of keys) {
        const row = element("div", "local-model-bar");
        row.append(element("span", "", key));
        const track = element("div", "local-model-bar-track");
        const fill = element("i", "");
        fill.style.width = Math.max(0, Math.min(5, Number(scores[key]) || 0)) / 5 * 100 + "%";
        track.append(fill);
        row.append(track);
        bars.append(row);
      }
      card.append(bars);
    }

    for (const [heading, items, className] of [
      [words.pros, active.pros || [], "is-pros"],
      [words.cons, active.cons || [], "is-cons"],
    ]) {
      if (!items.length) continue;
      const block = element("div", "local-model-tradeoff-group " + className);
      block.append(element("h3", "", heading));
      const list = element("ul", "");
      for (const item of items) list.append(element("li", "", String(item)));
      block.append(list);
      card.append(block);
    }

    if (!active.installed) card.append(element("p", "local-model-note", words.notInstalledNote));
    tradeoffsBody.append(card);
  }

  const status = element("div", "local-mode-status");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  const statusIcon = element("span", "local-mode-status-icon");
  statusIcon.append(icon("loading"));
  const statusText = element("span", "local-mode-status-text", words.connecting);
  const mtp = element("span", "local-mode-mtp", "MTP " + words.pending);
  status.append(statusIcon, statusText, mtp);
  const retry = element("button", "local-mode-retry");
  retry.type = "button";
  retry.title = words.retry;
  retry.setAttribute("aria-label", words.retry);
  retry.append(icon("retry"));
  retry.hidden = true;
  retry.addEventListener("click", () => {
    if (["error", "waiting"].includes(current?.state) && !disconnected) void recoverService();
    else {
      localError = "";
      void pollStatus();
    }
  });
  const error = element("div", "local-mode-error");
  error.setAttribute("role", "alert");
  error.hidden = true;
  row.append(brand, picker, modelNow, modes, status, retry);
  toolbar.append(row, error);
  toolbar.append(tradeoffs);

  // The generate view (prompt form, live progress, monitoring and history)
  // lives in web/image-panel.js and is toggled through setActive() below.

  document.body.prepend(toolbar);
  document.documentElement.classList.add("local-modes-enabled");

  function updateHeight() {
    document.documentElement.style.setProperty("--local-toolbar-height", toolbar.getBoundingClientRect().height + "px");
  }
  new ResizeObserver(updateHeight).observe(toolbar);
  updateHeight();

  function composer() {
    return document.querySelector(".conversation-chat-form textarea");
  }

  function richComposer() {
    return document.querySelector('.conversation-chat-form [role="textbox"][contenteditable]');
  }

  function hasAttachments() {
    return !!document.querySelector('.conversation-chat-form button[aria-label="Remove"], .conversation-chat-form button[aria-label="\u79fb\u9664"]');
  }

  function nativeAgentBusy() {
    // Between model requests the native agent can be executing a browser tool
    // or waiting for permission. The gateway's active_requests is then zero.
    // These controls remain mounted until that native session has finished.
    return !!document.querySelector('[aria-label="Stop generation"], [aria-label="停止生成"], [aria-label="More allow options"], [aria-label="更多授权选项"]') ||
      Array.from(document.querySelectorAll('.conversation-chat-form button[type="button"] .sr-only'))
        .some(label => ["Stop", "停止"].includes(label.textContent.trim()));
  }

  function forgetDraft() {
    try { sessionStorage.removeItem(draftKey); } catch (failure) {
      console.warn("Qwen local UI could not clear the saved draft", failure);
    }
  }

  function saveDraft() {
    const input = composer();
    if (!input) {
      // The rich editor encodes mentions and code in its DOM. Do not flatten
      // that content into plain text and silently lose an unsent draft.
      if (richComposer()?.textContent.trim()) throw new Error(words.richDraft);
      return;
    }
    const draft = {
      path: location.pathname + location.search + location.hash,
      text: input.value,
      start: input.selectionStart,
      end: input.selectionEnd,
      focused: document.activeElement === input,
      time: Date.now(),
    };
    try {
      sessionStorage.setItem(draftKey, JSON.stringify(draft));
    } catch (failure) {
      if (draft.text) throw new Error(words.storage);
    }
  }

  function restoreDraft() {
    let draft;
    try {
      draft = JSON.parse(sessionStorage.getItem(draftKey) || "null");
    } catch (failure) {
      return;
    }
    if (!draft) return;
    if (typeof draft.text !== "string" || typeof draft.path !== "string" ||
        !Number.isFinite(draft.time) || Date.now() - draft.time > 60 * 60 * 1000) {
      forgetDraft();
      return;
    }
    const deadline = Date.now() + 30000;
    const timer = setInterval(() => {
      // The native app normalizes its URL while it initializes.
      if (draft.path !== location.pathname + location.search + location.hash) {
        if (Date.now() > deadline) clearInterval(timer);
        return;
      }
      const input = composer();
      if (!input) {
        if (Date.now() > deadline) clearInterval(timer);
        return;
      }
      clearInterval(timer);
      if (!input.value && draft.text) {
        Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(input, draft.text);
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.setSelectionRange(draft.start, draft.end);
        if (draft.focused) input.focus({ preventScroll: true });
      }
      forgetDraft();
    }, 100);
  }

  function generateTab() {
    return activeTab === "generate" || (!activeTab && current?.mode === "generate");
  }

  // The video view holds the screen the same way generate does, and for much
  // longer: an H3 run takes tens of minutes, so it must survive the mode the
  // backend reports while ComfyUI owns the GPU.
  // The voice view holds the screen the way generate does, but entering it does
  // not switch models: the panel loads the voice pair only when a turn starts, so
  // the other views keep whatever model they were using.
  function voiceTab() {
    return activeTab === "voice";
  }

  function videoTab() {
    return activeTab === "video" || (!activeTab && current?.mode === "video");
  }

  function storeTab(tab) {
    activeTab = tab;
    try {
      localStorage.setItem(tabKey, tab);
    } catch (failure) {
      // The tab still works for this page load without storage.
    }
  }

  /* 切到/切离聊天模式（纯文本 ↔ 识图）时，立刻在打包应用里新开一个对话。
   *
   * 聊天窗口是应用自己的状态——标签在 `localStorage["LlamaUi.conversationTabs"]`，消息在
   * IndexedDB——所以网关这侧把记账分线、把进模型的提示词过滤，都改不了屏幕上仍留着的那串记录：
   * 所有者看到的仍是"上一个模式的对话还在"。他要的是"切换之后立刻新开一个对话"，于是这里点它
   * **自己的「新对话」按钮**：用应用自己的行为，而不是去改它的存储（改存储要么被它覆盖，要么留下
   * 一个它不认识的标签）。
   *
   * 只管这两个模式之间的切换：语音页本来就新开自己那条线，生图/视频生成是面板、不是聊天线程。
   */
  const CHAT_MODES = ["text", "vision"];

  function appNewChatButton() {
    const candidates = [...document.querySelectorAll("button, [role='button']")];
    return candidates.find(node => {
      // The app's sidebar can be collapsed, in which case the control is an icon whose name lives
      // in aria-label/title rather than in its text. Both forms mean the same button.
      const name = (node.getAttribute("aria-label") || node.title || node.textContent || "").trim();
      if (name !== "新对话") return false;
      // Our own panels carry a 新对话 too (the voice rail's). The app's is the one outside them.
      if (node.closest("#local-voice-panel, #local-mode-toolbar, .local-voice-panel")) return false;
      const className = String(node.className || "");
      return !className.startsWith("local-") && !className.startsWith("qv-");
    }) || null;
  }

  function conversationTabs() {
    try {
      return String(localStorage.getItem("LlamaUi.conversationTabs") || "");
    } catch (failure) {
      return "";
    }
  }

  /* 点应用自己的「新对话」，并且**以"屏幕上真的是一条新对话"为完成条件**。
   *
   * 实测出来的两件事决定了这里必须这么写：应用在流式回答时把按钮置灰；而切模式之后页面会按状态
   * 自动重载，重载会把刚开出来的那条新线**还原回上一个会话**（点击确实生效了，重载又抹掉了）。
   * 所以判据是应用自己的标签列表最后一项是不是那条"新对话"（`new-chat`），而不是"我点了一下"或者
   * "列表变过一次"；没达成就在 localStorage 留下待办，重载后由 `fulfilPendingFreshChat()` 接着做。
   */
  const FRESH_CHAT_KEY = "qwen.local.freshChatPending";
  const FRESH_CHAT_MAX_AGE_MS = 120000;

  function freshChatActive() {
    try {
      const list = JSON.parse(conversationTabs() || "[]");
      return Array.isArray(list) && list.length > 0 && list[list.length - 1] === "new-chat";
    } catch (failure) {
      return false;
    }
  }

  function clearFreshChatIntent() {
    try { localStorage.removeItem(FRESH_CHAT_KEY); } catch (failure) { /* blocked storage */ }
  }

  function chaseFreshChat(deadlineMs) {
    const started = Date.now();
    const tick = () => {
      if (freshChatActive()) return;
      const button = appNewChatButton();
      if (button && !button.disabled) {
        try {
          button.click();
        } catch (failure) { /* keep trying until the deadline */ }
      }
      if (Date.now() - started < deadlineMs) window.setTimeout(tick, 500);
    };
    tick();
  }

  function requestFreshChat() {
    try {
      localStorage.setItem(FRESH_CHAT_KEY, JSON.stringify({ at: Date.now() }));
    } catch (failure) { /* the click below still happens without the fallback */ }
    // Deliberately **not** clearing the intent here: the mode switch reloads the page, and the app
    // restores the previous conversation on load — so a new chat that looked successful a moment
    // ago can be gone a second later. Clearing is the post-load pass's job, once it has watched
    // the state settle.
    chaseFreshChat(6000);
  }

  /* 重载之后接着做那次"新开一个对话"。判据是重载后**稳定**看到新对话：应用在加载时会恢复上一次的
   * 会话，所以"刚点完就看到了"不算数——这也是上一次切换看起来成功、回到页面却又变回旧会话的原因。
   * 而且重载后 1.2 秒时应用自己的 DOM 常常还没挂载好（它还没渲染出「新对话」按钮），所以这一轮要
   * 守得久一点：最多约 30 秒，一旦真的出现新对话就清掉待办。超过两分钟的待办直接丢弃，免得下次
   * 打开页面时莫名其妙地新开一个。 */
  function fulfilPendingFreshChat() {
    let raw = null;
    try { raw = localStorage.getItem(FRESH_CHAT_KEY); } catch (failure) { return; }
    if (!raw) return;
    let intent = null;
    try { intent = JSON.parse(raw); } catch (failure) { intent = null; }
    const at = intent && Number(intent.at) || 0;
    if (!at || Date.now() - at > FRESH_CHAT_MAX_AGE_MS) {
      clearFreshChatIntent();
      return;
    }
    chaseFreshChat(30000);
    window.setTimeout(() => { if (freshChatActive()) clearFreshChatIntent(); }, 1500);
  }

  // A tab click that arrives while the backend is still finishing the previous turn used to be
  // dropped in silence: `changeMode` returns early when a request is in flight, so the owner
  // clicked 识图, saw nothing happen, and (measured right after a restart) the switch never
  // happened at all. The wanted tab is remembered instead and applied as soon as the backend is
  // idle again.
  function canChangeMode() {
    return Boolean(current) && !switching && !recovering && !current.active_requests
      && !nativeAgentBusy() && !(window.ImagePanel && window.ImagePanel.isBusy());
  }

  // Tab selection is a view choice: it switches the backend only when the
  // backend mode has to change for that view to work. Generating unloads the
  // language model and comes back in text/vision, so the open generate view is
  // kept instead of being closed by the reported mode.
  function selectTab(tab) {
    // The button is disabled in this state, but a click can still arrive from a keyboard
    // shortcut or a stale render: the tab must not open for a model that cannot serve it.
    if (tab === "voice" && voiceBlockedFor(current)) {
      localError = voiceReasonFor(current);
      render();
      return;
    }
    // Both full-screen views are opened locally, so the backend is switched only
    // implicitly by whichever job they start.
    if (tab === "voice") {
      // Opening the voice view is a look, not a commitment. The panel swaps the card
      // over to the 9B voice pair only when a turn actually starts, so nothing is
      // released at this point and a confirmation here would describe an eviction
      // that has not happened. The panel states the cost in its own hint line instead.
      storeTab(tab);
      render();
      return;
    }
    if (tab === "generate" || tab === "video") {
      storeTab(tab);
      render();
      return;
    }
    // 纯文本 ↔ 识图：立刻新开一个对话，再切后端（见 requestFreshChat）。
    //
    // 判据不能只看本地 tab 状态：第一次打开页面时 `activeTab` 还没落定（存储里没有值就是 null），
    // 于是一次"文字 → 识图"会被当成"没有切换"而漏掉新对话（实测就是这个：第二次切换才生效）。
    // 后端正在服务的模式也是事实，两者任一说明刚才在聊天模式即可。
    const wasChatMode = CHAT_MODES.includes(activeTab) || CHAT_MODES.includes(current && current.mode);
    if (tab !== activeTab && CHAT_MODES.includes(tab) && wasChatMode) {
      requestFreshChat();
    }
    // 顶部导航只换页面（所有者要求）：文字/识图不再调 `/local/mode`。真的需要换后端时
    // （带图的回合）由下面的 fetch 钩子在**使用时刻**切到识图。
    storeTab(tab);
    render();
  }

  function render() {
    const state = disconnected ? "offline" : switching && current?.state === "ready" ? "switching" : current?.state || "starting";
    const loading = switching || recovering || ["starting", "switching", "stopping", "waiting"].includes(state);
    /* "waiting" is the gateway holding a retry — measured: ComfyUI is rendering, so the language
     * model cannot take the card yet, and the toolbar says so ("ComfyUI 正在执行或排队…"). Showing
     * it must not also lock the page: choosing another view is a local decision (顶部导航只换页面),
     * and an action that really needs the backend (确认切换 models, a turn with an image) is answered
     * by canChangeMode() or the use-time switch with an explanation instead of a silent failure. */
    const locked = switching || recovering || ["starting", "switching", "stopping"].includes(state);
    const busy = (current?.active_requests || 0) > 0 || nativeAgentBusy();
    const attachments = hasAttachments();
    /* Voice mode is offered only while the model that can serve it is selected. The rule and
     * its wording come from the gateway (`/local/voice/config`), which is the same rule the
     * voice routes enforce, so the button cannot disagree with the backend about who may
     * talk. Unknown yet (the first status poll can land before the config does) is treated as
     * available: a button that briefly looks enabled is better than one that flickers off. */
    const profile = current?.model_profile || "";
    const voiceBlocked = voiceBlockedFor(current);
    const voiceReason = voiceReasonFor(current);
    const voiceSize = modeSizes.voice;
    toolbar.dataset.state = state;
    toolbar.setAttribute("aria-busy", String(loading));
    const generate = generateTab();
    const video = videoTab();
    const voice = voiceTab();
    for (const [mode, button] of Object.entries(buttons)) {
      /* 高亮跟的是**页面**，不是后端模式。导航已经不切后端了，如果这里还拿 `current.mode` 判断，
       * 点「识图」之后后端仍是 text，高亮就会留在「纯文本」上——所有者反馈的"点识图时顶部导航
       * 显示有点问题"就是这个（实测：识图页上 aria-pressed 仍在纯文本，且模式按钮整体右移 10px）。
       * 语音/生图/视频三个视图本来就是按 `activeTab` 判断的，这里与它们对齐。 */
      const pressed = mode === "generate" ? generate
        : mode === "video" ? video
        : mode === "voice" ? voice
        : !generate && !video && !voice && (activeTab || current?.mode) === mode;
      button.setAttribute("aria-pressed", String(pressed));
      /* 导航按钮**只在"连不上/还没拿到状态"时禁用**。它现在只换页面（不碰后端），所以正在生成、
       * 正在切模式、有附件、正在等后端恢复——都不该让导航变灰：实测生图作业一跑起来整排按钮
       * 都是灰的，所有者点「识图」毫无反应，看上去就是"顶部导航显示有问题"。
       * 真正需要后端的动作（「确认切换」、开始生成）各自有自己的可用性判断。 */
      button.disabled = disconnected || !current;
      button.title = words[mode];
      // Voice mode is memory-bound: it shares the 9B's weights and adds the recogniser, and
      // the 27B's 7.8 GB resident leaves no room for both on a 12 GB card. A button that is
      // merely disabled explains nothing, so the size label is shown and the reason is both
      // on hover and on the label itself.
      if (mode === "voice") {
        const size = button.querySelector("small");
        if (voiceBlocked) {
          button.disabled = true;
          button.dataset.unsupported = "true";
          button.title = voiceReason;
          button.setAttribute("aria-disabled", "true");
          if (size) size.textContent = words.unsupported;
        } else {
          delete button.dataset.unsupported;
          button.removeAttribute("aria-disabled");
          if (size) size.textContent = voiceSize;
        }
      }
    }
    // The generate view owns the screen area; image-panel.js mounts itself and
    // receives the toolbar renderer so its status line follows the backend.
    const panel = window.ImagePanel;
    // It also stays open while a job runs or while the model is restored.
    if (panel) panel.setActive(Boolean(generate && current), render);
    // The video view is a peer of generate with the same mounting contract.
    const videoPanel = window.VideoPanel;
    if (videoPanel) videoPanel.setActive(Boolean(video && current), render);
    const voicePanel = window.VoicePanel;
    if (voicePanel) voicePanel.setActive(Boolean(voice && current), render);
    statusIcon.classList.toggle("is-loading", loading);
    statusIcon.classList.toggle("is-ready", state === "ready" && !loading);
    statusText.textContent = disconnected || recovering ? words.recovering : loading ? words[state] || words.switching : busy ? current?.agent_enabled ? words.agentBusy : words.busy : state === "ready" && current?.sleeping ? words.sleeping : state === "ready" && current?.agent_enabled ? words.agentReady : words[state] || words.connecting;
    mtp.textContent = "MTP " + (state === "ready" ? current.mtp_enabled ? words.enabled : words.disabled : words.pending);
    mtp.dataset.enabled = String(state === "ready" && current?.mtp_enabled === true);
    mtp.title = current?.mtp_reason || "";
    // The gateway owns the profile list, so refresh the picker from each status
    // rather than from anything cached in this file.
    syncModelOptions(current?.model_profiles);
    modelSelect.value = pendingModel || current?.model_profile || "";
    const page = pageLoadState();
    const loadedName = loadedProfileName();
    const pendingLabel = pendingModel && pendingModel !== loadedName
      ? profileLabelOf(pendingModel) : "";
    /* 状态行永远是"什么页 · 载入了什么"：所有者要的就是这一句能一眼读到，按钮才知道按下去会做什么。
     * 可见文字用短句（长度稳定，不会把右边的模式按钮推来推去）；完整句子和待切换的档位挂到 title。 */
    modelNow.textContent = `${words.modeNow} ${modeLabel(page.view)} \u00b7 ${page.text}`;
    modelNow.title = [`${words.modeNow} ${modeLabel(page.view)} \u00b7 ${page.detail || page.text}`,
      pendingLabel ? `${words.pendingModel} ${pendingLabel}\uff08\u6309\u53f3\u8fb9\u7684\u6309\u94ae\u624d\u751f\u6548\uff09` : ""]
      .filter(Boolean).join("\n");
    modelApplyButton.textContent = page.action || words.applyModel;
    modelApplyButton.title = page.action
      ? `${page.action}\uff08\u5378\u6389\u5f53\u524d\u5360\u7528\u7684\u6743\u91cd\u540e\u8f7d\u5165\uff09`
      : `${page.text}\uff08\u65e0\u9700\u52a8\u4f5c\uff09`;
    modelApplyButton.disabled = !page.action || !current || switching || recovering || !canChangeMode();
    // Full name and the trade-off note on hover: the control is narrow, and the label
    // is the only place the chosen model is named.
    const shownProfile = activeProfile();
    modelSelect.title = shownProfile ? shownProfile.label : "";
    modelSelect.disabled = locked || busy || attachments || disconnected || !current;
    renderTradeoffs();

    const detail = localError || current?.error || "";
    const errors = {
      "ComfyUI has queued jobs. Wait for them to finish.": "ComfyUI \u4ecd\u6709\u6392\u961f\u6216\u6267\u884c\u4e2d\u7684\u4efb\u52a1\uff0c\u8bf7\u7b49\u5f85\u5b8c\u6210\u540e\u91cd\u8bd5\u3002",
      "Model process exited. Select a mode to reload.": "\u6a21\u578b\u8fdb\u7a0b\u5df2\u9000\u51fa\uff0c\u8bf7\u9009\u62e9\u6a21\u5f0f\u91cd\u65b0\u52a0\u8f7d\u3002",
      "Model loading timed out. See logs.": "\u6a21\u578b\u52a0\u8f7d\u8d85\u65f6\uff0c\u8bf7\u67e5\u770b\u672c\u5730\u65e5\u5fd7\u3002",
      "Backend context size differs from configured context size.": "\u540e\u7aef\u4e0a\u4e0b\u6587\u957f\u5ea6\u4e0e\u914d\u7f6e\u4e0d\u4e00\u81f4\u3002",
      "Backend MTP status differs from validated configuration.": "\u540e\u7aef MTP \u72b6\u6001\u4e0e\u914d\u7f6e\u4e0d\u4e00\u81f4\u3002",
      "Backend vision capability differs from selected mode.": "\u540e\u7aef\u8bc6\u56fe\u80fd\u529b\u4e0e\u6240\u9009\u6a21\u5f0f\u4e0d\u4e00\u81f4\u3002",
      "Model exited during text initialization. See logs.": "\u7eaf\u6587\u672c\u6a21\u5f0f\u52a0\u8f7d\u5931\u8d25\uff0c\u8bf7\u67e5\u770b\u672c\u5730\u65e5\u5fd7\u3002",
      "Model exited during vision initialization. See logs.": "\u8bc6\u56fe\u6a21\u5f0f\u52a0\u8f7d\u5931\u8d25\uff0c\u8bf7\u67e5\u770b\u672c\u5730\u65e5\u5fd7\u3002",
    };
    error.textContent = errors[detail] || detail;
    error.hidden = !detail;
    retry.hidden = !(detail || disconnected || state === "error");
    retry.disabled = switching || recovering || loading && state !== "waiting" && !disconnected;
    document.documentElement.dataset.localModelState = disconnected ? "offline" : loading ? "switching" : state;
    updateHeight();
  }

  /* The panels' "close" buttons switch the view back to text, and they need the toolbar to do
   * it. This global had never been published, so `window.LocalModes` was undefined at runtime
   * and the voice panel's close silently fell back to only hiding itself — the mode stayed on
   * "voice" with no panel to show for it. Published here rather than patched in the panels:
   * the toolbar owns tab selection, so it is the only thing that can change it. */
  window.LocalModes = {
    select: selectTab,
    current: () => current,
    /* Start a fresh conversation in the packaged app, the way its own 新对话 button does, and keep
     * chasing it until the app really shows one (the page reloads after a mode switch, which would
     * otherwise restore the previous thread). Exposed so a check can prove the switch did it. */
    startNewChat: () => { requestFreshChat(); return chaseFreshChat(0) === undefined; },
    freshChatActive,
    hasNewChatControl: () => Boolean(appNewChatButton()),
    /* Bring the backend to vision and report the outcome, for callers that can only work in
     * that slice (video analysis is one). Resolves only once the backend really reports it, so
     * the caller does not have to guess how long a switch takes. */
    ensureVision: async () => {
      const deadline = Date.now() + 180000;
      while (Date.now() < deadline) {
        const status = current;
        if (status && status.state === "ready") {
          if (status.mode === "vision") return true;
          // 这里是"使用时"的入口：直接让后端切到识图，不再借导航按钮（导航已经不切后端了）。
          if (!switching) void changeMode("vision");
        }
        await new Promise(resolve => setTimeout(resolve, 500));
      }
      return false;
    },
  };

  function acceptStatus(next) {
    // "voice" belongs in this list: the gateway reports it while the voice pair is up
    // (mode_server sets manager.mode = "voice" when a turn takes the card). Leaving it
    // out made every /local/status poll throw, which the toolbar shows as
    // "本地服务连接失败" - the badge that appeared the moment a voice message was sent.
    if (!next || !["text", "vision", "voice", "generate", "video"].includes(next.mode) || !["starting", "ready", "switching", "waiting", "stopping", "error"].includes(next.state) || !Number.isInteger(next.revision)) throw new Error(words.offline);
    if (initialRevision === undefined) {
      initialRevision = next.revision;
      initialMode = next.mode;
      initialInstance = next.instance_id;
    }
    const previousMode = current ? current.mode : null;
    current = next;
    disconnected = false;
    pollsFailed = 0;
    render();
    // Native UI capabilities are read at startup. Reload after a completed mode
    // change, but never while the generate view is open: a generation cycle
    // changes the backend mode by itself, and reloading there would close the
    // view the user is watching (and drop its live progress).
    // Never on a report that voice mode is running. A spoken turn switches the backend
    // mode, and reloading on that would tear the panel down in the middle of answering -
    // losing the reply, its audio and the transcript. Checking both sides of the change
    // matters because the panel's own launcher opens the view without going through the
    // toolbar tab, so voiceTab() alone does not cover it: observed in a headless run,
    // the page reloaded mid-turn and the whole reply vanished with it.
    if (next.state === "ready" && !generateTab() && !videoTab() && !voiceTab() && next.mode !== "voice" && previousMode !== "voice" && (next.revision !== initialRevision || next.mode !== initialMode || next.instance_id !== initialInstance || reconnectNeeded) && !reloading) {
      try {
        if (hasAttachments() || next.active_requests > 0 || nativeAgentBusy()) {
          if (hasAttachments()) localError = words.refresh;
          render();
          return;
        }
        saveDraft();
        reloading = true;
        location.reload();
      } catch (failure) {
        localError = failure.message;
        render();
      }
    }
  }

  /* Voice availability for a status payload. Kept as functions rather than inline tests so
   * the render path and the click path answer from exactly the same rule. */
  /* 语音跟着当前选中的模型走，所以"能不能点"只由网关的 supported 决定；`locked` 那条老规则
   * 留着兼容旧服务端（它那时会把语音锁死在 27B，选中别的档位就该变灰）。 */
  function voiceBlockedFor(status) {
    if (voiceConfig.supported === false) return true;
    const profile = (status && status.model_profile) || "";
    return Boolean(voiceConfig.locked) && Boolean(profile) && profile !== voiceConfig.profile;
  }

  function voiceReasonFor(status) {
    if (!voiceBlockedFor(status)) return words.voice;
    return voiceConfig.reason
      || "这一档不能做语音对话：语音的大脑和识别器要同时驻留，请换一个能装下这一对的模型。";
  }

  /* Which model the voice mode can serve, and why, straight from the gateway.
   *
   * The rule lives on the server (`/local/voice/config`, the same one the voice routes
   * enforce), so the button and the backend cannot disagree about who may talk. Ownership is
   * not re-checked here: a refused fetch must not disable a button that works. */
  async function loadVoiceConfig(profile) {
    if (voiceConfigFetched && profile && profile === voiceConfigProfile) return;
    try {
      const response = await fetch("/local/voice/config", { cache: "no-store" });
      if (!response.ok) return;
      const value = await response.json();
      if (!value || typeof value !== "object") return;
      voiceConfig.locked = value.locked === true;
      voiceConfig.supported = value.supported !== false;
      voiceConfig.profile = typeof value.profile === "string" ? value.profile : "";
      voiceConfig.reason = typeof value.reason === "string" ? value.reason : "";
      voiceConfigProfile = profile;
      voiceConfigFetched = true;
      render();
    } catch (failure) {
      // Keep the last known rule: a network blip is not evidence about hardware.
    }
  }

  async function pollStatus() {
    if (polling || reloading) return;
    clearTimeout(pollTimer);
    pollTimer = undefined;
    polling = true;
    const requestVersion = modeRequestVersion;
    // Set when the failure path has already booked the next poll, so the shared tail below
    // does not book a second one and leave two poll loops running against each other.
    let retryBooked = false;
    try {
      /* 12 s, not 8: the toolbar's poll shares the gateway with a mode or model switch, and
       * this machine has served this endpoint slowly enough that an 8 s budget reported
       * "本地服务连接失败" on a service that was working. */
      const response = await fetch("/local/status", { cache: "no-store", signal: AbortSignal.timeout(12000) });
      if (response.status === 401 || response.status === 403) {
        // Signed out is not the same failure as unreachable, and it is recoverable: the
        // poll either picks up the renewed session or the owner logs in again. Reporting it
        // as a disconnect sent people looking for a broken service that was never broken.
        pollsFailed = 0;
        disconnected = false;
        if (localError === words.offline || !localError) localError = words.signedOut;
        render();
        return;
      }
      if (!response.ok) throw new Error(words.offline);
      const next = await response.json();
      if (requestVersion !== modeRequestVersion || switching || recovering) return;
      if (disconnected || pollsFailed) localError = "";
      pollsFailed = 0;
      // Asked for once per selected model, and after the response is accepted so the button
      // can be gated in the same paint as the model label.
      void loadVoiceConfig(next.model_profile);
      acceptStatus(next);
    } catch (failure) {
      if (requestVersion !== modeRequestVersion) return;
      /* Three consecutive misses before saying the service is gone. One is normal here:
       * `acceptStatus` navigates the page after a completed mode change, which aborts the
       * poll that is in flight at that moment, and a switch can outlast a single interval.
       * Claiming a disconnection for either is what made the owner chase a phantom. */
      pollsFailed += 1;
      if (pollsFailed >= OFFLINE_AFTER_FAILURES) {
        disconnected = true;
        reconnectNeeded = true;
        /* 一直连不上最常见的原因就是"服务已经按口径收工了"（页面关闭、或后台标签被浏览器冻结
         * 到超过窗口）。那时反复重试没有意义，要直接告诉主人怎么把它叫回来。 */
        localError = pollsFailed >= OFFLINE_STOPPED_AFTER_FAILURES
          ? words.offlineStopped : words.offline;
      } else if (!localError || localError === words.offline
                 || localError === words.offlineStopped) {
        localError = words.waiting;
      }
      render();
      // An immediate retry, so a blip is resolved by the next poll rather than after the
      // full interval the failure path would otherwise wait.
      pollTimer = setTimeout(pollStatus, OFFLINE_RETRY_MS);
      retryBooked = true;
    } finally {
      polling = false;
      if (!reloading && !retryBooked && !pollTimer) {
        pollTimer = setTimeout(pollStatus, document.hidden ? 5000 : current?.state === "ready" && !switching ? 2000 : 750);
      }
    }
  }

  async function recoverService() {
    if (recovering || switching) return;
    try {
      recovering = true;
      modeRequestVersion += 1;
      localError = "";
      render();
      const response = await fetch("/local/recover", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
        signal: AbortSignal.timeout(8000),
      });
      if (!response.ok) throw new Error(words.failed);
      acceptStatus(await response.json());
    } catch (failure) {
      localError = failure.message;
    } finally {
      recovering = false;
      render();
      void pollStatus();
    }
  }

  async function changeMode(mode, retrying = false) {
    // A generation cycle owns the backend while it runs: switching the tab is
    // free, but changing the model has to wait until the cycle restores it.
    if (window.ImagePanel?.isBusy()) {
      localError = words.imageBusy;
      render();
      return;
    }
    if (!current || switching || recovering || !retrying && current.mode === mode || current.active_requests > 0 || nativeAgentBusy()) return;
    if (hasAttachments()) { localError = words.attachments; render(); return; }
    try {
      saveDraft();
      modeRequestVersion += 1;
      switching = true;
      localError = "";
      render();
      const response = await fetch("/local/mode", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }), signal: AbortSignal.timeout(15000),
      });
      const next = await response.json();
      if (!response.ok) {
        if (next.mode) acceptStatus(next);
        throw new Error(response.status === 409 ? words.wait : next.error || words.failed);
      }
      acceptStatus(next);
      // Mode switches are asynchronous (202). Keep polling until the
      // backend reports the requested mode is actually ready; otherwise the
      // UI briefly shows "切换失败"/disabled controls during normal loading.
      if (next.state === "switching" || next.state === "starting" || next.state === "waiting") {
        const target = mode;
        const deadline = Date.now() + 180000;
        while (Date.now() < deadline) {
          await new Promise(resolve => setTimeout(resolve, 800));
          const probe = await fetch("/local/status", { signal: AbortSignal.timeout(5000) });
          const status = await probe.json();
          if (status.mode === target && status.state === "ready") { acceptStatus(status); return; }
          if (status.state === "error") { acceptStatus(status); throw new Error(status.error || words.failed); }
        }
        throw new Error(words.failed);
      }
    } catch (failure) {
      localError = failure.message || words.failed;
    } finally {
      switching = false;
      render();
      void pollStatus();
    }
  }

  async function changeModel(profile) {
    // Same guards as a mode change: a running generation owns the backend, and a
    // model switch restarts it, so it must not interrupt work in flight.
    if (window.ImagePanel && window.ImagePanel.isBusy()) {
      localError = words.imageBusy;
      render();
      return;
    }
    if (!current || switching || recovering || current.active_requests > 0 || nativeAgentBusy()) return;
    if (hasAttachments()) { localError = words.attachments; render(); return; }
    try {
      saveDraft();
      modelRequestVersion += 1;
      switching = true;
      localError = "";
      modelSelect.disabled = true;
      render();
      const response = await fetch("/local/model", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile }), signal: AbortSignal.timeout(15000),
      });
      const next = await response.json();
      if (!response.ok) {
        if (next.mode) acceptStatus(next);
        throw new Error(response.status === 409 ? words.wait : next.error || words.modelFailed);
      }
      acceptStatus(next);
      // The switch is asynchronous (202). Poll until the new profile reports
      // ready, then acceptStatus sees the changed alias and reloads the page so
      // the native UI re-reads the backend's capabilities.
      const deadline = Date.now() + 300000;
      while (Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 900));
        const probe = await fetch("/local/status", { signal: AbortSignal.timeout(5000) });
        const status = await probe.json();
        if (status.model_profile === profile && status.state === "ready") {
          pendingModel = null;
          acceptStatus(status);
          return;
        }
        if (status.state === "error") { acceptStatus(status); throw new Error(status.error || words.modelFailed); }
      }
      throw new Error(words.modelFailed);
    } catch (failure) {
      localError = failure.message || words.modelFailed;
    } finally {
      switching = false;
      modelSelect.disabled = false;
      render();
      void pollStatus();
    }
  }

  // 顶部导航只换页面：选中的模型只是"待切换"，真正卸旧装新交给「确认切换」按钮。
  modelSelect.addEventListener("change", () => {
    pendingModel = modelSelect.value || null;
    render();
  });

  /* 顶部导航不再切换后端，所以"必须换后端才做得了"的识图改在**使用时**发生：
   * 一个回合里带图，就在它发出去之前把后端切到识图，切完再发。识别不了图的那种失败
   * （后端没投影层）不会再出现，而纯文字回合完全不受影响。 */
  if (typeof window.fetch === "function" && !window.__localVisionOnUse) {
    window.__localVisionOnUse = true;
    const rawFetch = window.fetch.bind(window);
    window.fetch = async function (input, init) {
      const url = typeof input === "string" ? input : (input && input.url) || "";
      const body = init && typeof init.body === "string" ? init.body : "";
      const isChat = /\/chat\/completions\/?$/.test(url);
      const carriesImage = body.includes('"image_url"') || body.includes('"input_image"');
      if (isChat && carriesImage && current && current.state === "ready"
          && current.mode !== "vision" && window.LocalModes?.ensureVision) {
        try {
          await window.LocalModes.ensureVision();
        } catch (failure) {
          // 切不过去也照发：后端也许本来就带投影层，让真正的错误由它报出来。
        }
      }
      return rawFetch(input, init);
    };
  }

  document.addEventListener("submit", event => {
    if (event.target.matches(".conversation-chat-form") && (!current || current.mode === "generate" || current.state !== "ready" || switching || recovering || disconnected)) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);
  document.addEventListener("keydown", event => {
    const input = composer() || richComposer();
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && input &&
        (event.target === input || input.contains(event.target)) &&
        (!current || current.mode === "generate" || current.state !== "ready" || switching || recovering || disconnected)) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) void pollStatus();
  });
  window.addEventListener("online", () => void pollStatus());
  window.addEventListener("pageshow", event => {
    if (event.persisted) void pollStatus();
  });
  /* Re-render when the composer changes — but only while the composer can be seen.
     Every mutation under .conversation-chat-form used to call render(), and render()
     reads the toolbar's height (a forced layout) and asks the panels to re-evaluate. A
     streaming reply mutates that subtree continuously, so the tab was re-rendering the
     toolbar at reply speed while nobody was looking at it. Hidden work is dropped and
     made up once on return, which is one render instead of thousands. */
  function composerChanged(records) {
    if (document.visibilityState === "hidden") return;
    if (records.some(record => record.target instanceof Element && record.target.closest('.conversation-chat-form'))) render();
  }
  new MutationObserver(composerChanged).observe(document.body, {childList:true, subtree:true});
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "hidden") render();
  });
  restoreDraft();
  render();
  // 上一次"切换模式要新开对话"如果被页面重载打断了，在这里补做。等得久一点：重载刚结束时应用
  // 自己的 DOM 可能还没挂载好（连「新对话」按钮都还没渲染出来）。
  window.setTimeout(fulfilPendingFreshChat, 1200);
  window.setTimeout(fulfilPendingFreshChat, 5000);
  // The per-conversation grant is applied at page load, so a conversation that was granted
  // earlier is in force before the first tool call of the session rather than only after a
  // click. The tool list comes from the gateway, so it is fetched before the first write.
  mountComposerTools();
  renderPermission();
  void loadToolKeys().then(() => { applyPermission(); renderPermission(); });
  void pollStatus();
})();
