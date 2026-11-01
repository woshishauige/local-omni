/* 悬浮任务窗（所有模式共用的壳级组件）
 *
 * 所有者要求：
 *   1. 不点开时是一个**小圆圈**悬浮，并且**可移动**；
 *   2. 展开后能看到**当前在执行什么任务**，以及**自己的任务排在等待队列的第几位**。
 *
 * 为什么做成独立注入模块：它不属于任何一个模式面板。脚本注入在所有模式共用的 head 里，
 * 只读两个进度接口（`/local/image/progress`、`/local/video/progress`），不依赖 `.qg-*` /
 * `.qv-*` 任何面板内部结构，所以切模式、切会话、开关面板都不会把它带走或打断它。
 *
 * 轮询是克制的：页面不在前台就停（回来立刻补一次），没有任务时 8 秒，有任务时 4 秒；
 * 请求永远带 `preview=-1`——那 79 KB 的采样预览图只有生图面板需要它。
 */
(() => {
  'use strict';
  if (window.__qwenTaskHudInstalled) return;
  window.__qwenTaskHudInstalled = true;

  const POS_KEY = 'qwen.taskHud.position';
  const OPEN_KEY = 'qwen.taskHud.open';
  const IDLE_TICK_MS = 8000;
  const ACTIVE_TICK_MS = 4000;
  const EDGE = 10;
  // 网关把别的设备的任务脱敏成这一句；用它来区分"我的"和"别人的"，
  // 而不是靠猜自己是不是机主（admin cookie 是 HttpOnly，脚本看不到）。
  const OTHER_CALLER_PROMPT = '（其他设备提交的任务）';

  const words = {
    title: '任务',
    idleHint: '现在没有任务。生图、生视频或语音开始后，这里会自动显示进度。',
    image: '生图',
    video: '视频',
    comfy: 'ComfyUI',
    comfyOrphan: 'ComfyUI 正在渲染（本网关重启前提交的任务）',
    comfyQueue: (n) => `ComfyUI 队列还有 ${n} 个任务`,
    queued: '排队',
    mine: '我的',
    other: '其他设备',
    position: (n) => `排队第 ${n} 位`,
    shot: (a, b) => `第 ${a}/${b} 个镜头`,
    hint: '拖动换位置，点一下展开',
    open: '展开任务窗',
    collapse: '收起',
  };

  const PHASES = {
    starting_comfy: '启动 ComfyUI',
    enhancing: '优化提示词',
    loading_model: '装载模型',
    queued: '已加入队列',
    queued_behind: '排队等待',
    sampling: '采样中',
    decoding: '解码中',
    restoring: '收尾（换回对话模型）',
    completed: '已完成',
    error: '失败',
    cancelled: '已取消',
    running: '进行中',
  };

  const root = document.createElement('div');
  root.className = 'qt-root';
  root.dataset.open = 'false';
  root.setAttribute('role', 'region');
  root.setAttribute('aria-label', words.title);

  const bubble = document.createElement('button');
  bubble.type = 'button';
  bubble.className = 'qt-bubble';
  bubble.setAttribute('aria-label', words.open);
  bubble.title = words.hint;
  bubble.innerHTML =
    '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
    '<path d="M12 3a9 9 0 1 0 9 9" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round"/><circle cx="12" cy="12" r="2.6" fill="currentColor"/></svg>' +
    '<span class="qt-count" hidden>0</span>';

  const panel = document.createElement('section');
  panel.className = 'qt-panel';
  panel.hidden = true;

  const head = document.createElement('header');
  head.className = 'qt-head';
  const grip = document.createElement('span');
  grip.className = 'qt-grip';
  grip.textContent = words.title;
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'qt-close';
  close.textContent = '×';
  close.setAttribute('aria-label', words.collapse);
  head.append(grip, close);

  const list = document.createElement('ul');
  list.className = 'qt-list';
  const empty = document.createElement('p');
  empty.className = 'qt-empty';
  empty.textContent = words.idleHint;

  panel.append(head, list, empty);
  root.append(bubble, panel);
  document.body.append(root);

  // -- 位置：可拖动，记在本机 ------------------------------------------------------
  function clamp(value, min, max) {
    return Math.min(Math.max(value, min), max);
  }

  function applyPosition(x, y) {
    const rect = root.getBoundingClientRect();
    const width = rect.width || 54;
    const height = rect.height || 54;
    root.style.left = `${clamp(x, EDGE, Math.max(EDGE, window.innerWidth - width - EDGE))}px`;
    root.style.top = `${clamp(y, EDGE, Math.max(EDGE, window.innerHeight - height - EDGE))}px`;
    root.style.right = 'auto';
    root.style.bottom = 'auto';
  }

  function defaultPosition() {
    // 默认贴右侧、竖直居中：不压输入框（底部中间），也不压右下角的提示条与共享入口。
    const rect = root.getBoundingClientRect();
    return { x: window.innerWidth - (rect.width || 54) - 16, y: window.innerHeight / 2 - 27 };
  }

  function loadPosition() {
    try {
      const saved = JSON.parse(localStorage.getItem(POS_KEY) || 'null');
      if (saved && Number.isFinite(saved.x) && Number.isFinite(saved.y)) return saved;
    } catch (failure) { /* 坏数据就当没存过 */ }
    return null;
  }

  function savePosition(x, y) {
    try {
      localStorage.setItem(POS_KEY, JSON.stringify({ x: Math.round(x), y: Math.round(y) }));
    } catch (failure) { /* 隐私模式下写不进去，不影响使用 */ }
  }

  function restore() {
    const saved = loadPosition();
    const spot = saved || defaultPosition();
    applyPosition(spot.x, spot.y);
  }

  let drag = null;

  function onPointerDown(event) {
    if (event.button !== undefined && event.button !== 0) return;
    const rect = root.getBoundingClientRect();
    drag = { dx: event.clientX - rect.left, dy: event.clientY - rect.top, moved: false };
    root.dataset.dragging = 'true';
    if (event.currentTarget.setPointerCapture) {
      try { event.currentTarget.setPointerCapture(event.pointerId); } catch (failure) { /* 忽略 */ }
    }
    event.preventDefault();
  }

  function onPointerMove(event) {
    if (!drag) return;
    const x = event.clientX - drag.dx;
    const y = event.clientY - drag.dy;
    if (Math.abs(x - root.offsetLeft) > 3 || Math.abs(y - root.offsetTop) > 3) drag.moved = true;
    applyPosition(x, y);
  }

  function onPointerUp(event) {
    if (!drag) return;
    const moved = drag.moved;
    drag = null;
    delete root.dataset.dragging;
    if (event.currentTarget.releasePointerCapture) {
      try { event.currentTarget.releasePointerCapture(event.pointerId); } catch (failure) { /* 忽略 */ }
    }
    const rect = root.getBoundingClientRect();
    savePosition(rect.left, rect.top);
    // 拖动结束后不该顺手把窗口打开：只有"按下去没动"才算点击。
    if (!moved && event.currentTarget === bubble) setOpen(root.dataset.open !== 'true');
  }

  for (const handle of [bubble, grip]) {
    handle.addEventListener('pointerdown', onPointerDown);
    handle.addEventListener('pointermove', onPointerMove);
    handle.addEventListener('pointerup', onPointerUp);
    handle.addEventListener('pointercancel', onPointerUp);
  }
  close.addEventListener('click', () => setOpen(false));
  window.addEventListener('resize', () => {
    const rect = root.getBoundingClientRect();
    applyPosition(rect.left, rect.top);
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && root.dataset.open === 'true') setOpen(false);
  });

  function setOpen(open) {
    root.dataset.open = open ? 'true' : 'false';
    panel.hidden = !open;
    try { localStorage.setItem(OPEN_KEY, open ? '1' : '0'); } catch (failure) { /* 忽略 */ }
    if (open) void tick(true).then(schedule);
  }

  // -- 任务：读两个进度接口，拼出"现在在跑什么、我排第几" ------------------------------
  const state = { image: null, video: null, comfy: null, busy: false };
  const ACTIVE_STATES = new Set(['running', 'queued', 'queued_behind']);
  // ComfyUI 侧的 H3 监视会把最后一次的中断/完成状态留着，照抄就会变成一个散不掉的幽灵任务；
  // 只有"真的在干活"的阶段（或队列里确实有东西）才算任务。
  const COMFY_ACTIVE_PHASES = new Set(['sampling', 'loading', 'loading_model', 'unloading',
    'decoding', 'encoding', 'queued', 'preparing', 'warming', 'starting']);

  function comfyBusy(comfy) {
    if (!comfy) return false;
    if (comfy.queue_running || comfy.queue_pending) return true;
    return COMFY_ACTIVE_PHASES.has(String(comfy.phase || ''));
  }

  function where(job) {
    return PHASES[job.phase] || job.phase || PHASES[job.state] || job.state || '';
  }

  function rowForImage(job) {
    const who = job.prompt === OTHER_CALLER_PROMPT ? words.other : words.mine;
    if (job.state === 'queued_behind' || job.phase === 'queued_behind') {
      // 排队位只有一格，而且 pending_for 只会把**调用者自己**的等待任务给它，所以这里一定是"我的"。
      const place = Math.max(1, Number(job.queue_ahead || 1));
      return {
        kind: 'image', tone: 'queued',
        title: `${words.mine}${words.image} · ${words.queued}`,
        detail: place <= 1 ? words.position(1) : words.position(place),
      };
    }
    const percent = Math.round(Math.max(0, Math.min(1, job.progress || 0)) * 100);
    const step = job.phase === 'sampling' && job.max_step ? ` ${job.step}/${job.max_step}` : '';
    return {
      kind: 'image', tone: 'running',
      title: `${who}${words.image} · ${where(job)}${step}`,
      detail: `${percent}%`,
    };
  }

  function rowForVideo(job) {
    const who = (job.submitted_by && job.submitted_by !== '') ? words.other : words.mine;
    const shots = job.position && job.total ? ` · ${words.shot(job.position, job.total)}` : '';
    return {
      kind: 'video', tone: 'running',
      title: `${who}${words.video} · ${where(job)}${shots}`,
      detail: job.eta ? `剩余约 ${Math.round(job.eta)} 秒` : '',
    };
  }

  function rowForComfy(comfy) {
    // 网关自己的任务表是内存里的：重启会把"正在跑的任务"忘掉，而 ComfyUI 那边还在采样
    // （实测：访客的一条 H3 片子重启后继续跑了 1 个多小时，进度接口却回"没有任务"）。
    // 所以网关没有任务时，用 ComfyUI 自报的状态补一行，别让窗口在有活干的时候说"没任务"。
    const step = comfy.step && comfy.steps ? ` ${comfy.step}/${comfy.steps}` : '';
    const phase = PHASES[comfy.phase] || comfy.phase || '';
    const project = comfy.project ? `${comfy.project} · ` : '';
    const shots = comfy.shot && comfy.shots ? ` · ${words.shot(comfy.shot, comfy.shots)}` : '';
    const eta = comfy.sampling_eta_seconds
      ? `剩余约 ${Math.round(comfy.sampling_eta_seconds / 60)} 分钟` : '';
    return {
      kind: 'comfy', tone: 'running',
      title: `${words.comfyOrphan} · ${project}${phase}${step}${shots}`,
      detail: eta,
    };
  }

  function render() {
    const queue = [];
    const job = state.image;
    // 只显示还在跑或还在等的任务；已完成/失败/取消的由面板自己播报，这里不留残影。
    const active = job && (ACTIVE_STATES.has(job.state) || job.phase === 'queued_behind');
    if (active) queue.push(rowForImage(job));
    if (state.video) {
      queue.push(rowForVideo(state.video));
    } else if (comfyBusy(state.comfy)) {
      // 网关没有自己的视频任务，但 ComfyUI 有活：补一行，别在有任务时说"没任务"。
      queue.push(rowForComfy(state.comfy));
    }

    list.replaceChildren();
    for (const row of queue) {
      const item = document.createElement('li');
      item.className = 'qt-item';
      item.dataset.kind = row.kind;
      item.dataset.tone = row.tone;
      const title = document.createElement('span');
      title.className = 'qt-item-title';
      title.textContent = row.title;
      item.append(title);
      if (row.detail) {
        const detail = document.createElement('span');
        detail.className = 'qt-item-detail';
        detail.textContent = row.detail;
        item.append(detail);
      }
      list.append(item);
    }
    const waiting = queue.filter((row) => row.tone === 'queued').length;
    const running = queue.length - waiting;
    empty.hidden = queue.length > 0;
    root.dataset.busy = queue.length ? 'true' : 'false';
    const count = bubble.querySelector('.qt-count');
    count.hidden = queue.length === 0;
    count.textContent = String(queue.length);
    bubble.dataset.tone = running ? 'running' : (waiting ? 'queued' : 'idle');
    bubble.setAttribute('aria-label',
      queue.length ? `${words.title}：${queue.length} 项` : words.open);
  }

  async function fetchJson(path) {
    const response = await fetch(path, { credentials: 'same-origin', cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }

  // -- 轮询：前台才跑，有任务才快 ---------------------------------------------------
  let timer = null;

  async function tick() {
    if (document.visibilityState === 'hidden') return;
    try {
      const image = await fetchJson('/local/image/progress?since=0&preview=-1');
      state.image = image && image.job ? image.job : null;
      state.busy = Boolean(image && image.busy);
    } catch (failure) {
      state.image = null;
      state.busy = false;
    }
    try {
      const video = await fetchJson('/local/video/progress');
      state.video = video && video.busy && video.job ? video.job : null;
      state.comfy = state.video ? null : (video && video.comfy ? video.comfy : null);
    } catch (failure) {
      state.video = null;
      state.comfy = null;
    }
    render();
  }

  function schedule() {
    clearTimeout(timer);
    if (document.visibilityState === 'hidden') { timer = null; return; }
    const busy = Boolean(state.busy || state.video || comfyBusy(state.comfy));
    timer = setTimeout(() => { void tick().then(schedule); }, busy ? ACTIVE_TICK_MS : IDLE_TICK_MS);
  }

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') void tick().then(schedule);
    else { clearTimeout(timer); timer = null; }
  });

  restore();
  try { setOpen(localStorage.getItem(OPEN_KEY) === '1'); } catch (failure) { /* 默认收起 */ }

  const start = () => { void tick().then(schedule); };
  if (document.readyState === 'complete') start();
  else window.addEventListener('load', () => { restore(); start(); });

  window.QwenTaskHud = {
    version: 'task-hud/1',
    element: root,
    isOpen: () => root.dataset.open === 'true',
    setOpen,
    refresh: () => tick(),
    position: () => ({ x: root.offsetLeft, y: root.offsetTop }),
    // 轻量检查用：不跑真实任务，直接喂一份进度负载，看它怎么显示。
    _apply: (image, video, comfy) => {
      state.image = image || null;
      state.video = video || null;
      state.comfy = comfy || null;
      state.busy = Boolean(image);
      render();
    },
  };
})();
