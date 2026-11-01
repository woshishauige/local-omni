(function (root) {
  'use strict';

  // Shared-manager panel: who is reaching this machine through the tunnel or
  // LAN, how much they used, and the switches to remove them. The module only
  // defines the factory here; every DOM lookup happens inside create() so the
  // file can be loaded (and unit tested) without a browser.
  const STATUS_URL = '/local/share/status';
  const PIN_URL = '/local/share/pin';
  const ONLINE_URL = '/local/share/online';
  const STOP_URL = '/local/share/stop';
  const KICK_ALL_URL = '/local/share/sessions/revoke-all';
  const FORGET_UNUSED_URL = '/local/share/sessions/forget-unused';
  const UPLOADS_URL = '/local/share/uploads';
  const PIN_PATTERN = /^\d{6}-\d{6}-\d{6}$/;
  const PIN_HINT = '口令格式应为 123456-789012-345678';
  const POLL_INTERVAL = 10000;
  const REQUEST_TIMEOUT = 12000;
  const RELATIVE_STEPS = [
    [60, 1, '秒前'],
    [3600, 60, '分钟前'],
    [86400, 3600, '小时前'],
    [2592000, 86400, '天前'],
    [31536000, 2592000, '个月前'],
  ];
  const numberFormat = new Intl.NumberFormat('zh-CN');
  const byteFormat = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 });

  function numeric(value) { return typeof value === 'number' && Number.isFinite(value); }

  // Relative times are built from an injectable clock so the panel and its
  // tests agree on "now" without touching the real system time.
  function makeRelativeTime(clock) {
    const read = typeof clock === 'function' ? clock : () => Date.now();
    return function relativeTime(value, now) {
      if (!numeric(value)) return '—';
      const stamp = value < 1e11 ? value * 1000 : value;
      const reference = numeric(now) ? (now < 1e11 ? now * 1000 : now) : read();
      const elapsed = (reference - stamp) / 1000;
      if (!Number.isFinite(elapsed)) return '—';
      if (elapsed < 1) return '刚刚';
      for (const [limit, divisor, suffix] of RELATIVE_STEPS) {
        if (elapsed < limit) return Math.floor(elapsed / divisor) + ' ' + suffix;
      }
      return Math.floor(elapsed / 31536000) + ' 年前';
    };
  }

  function formatBytes(value) {
    if (!numeric(value)) return '—';
    const size = Math.abs(value);
    if (size < 1024) return numberFormat.format(Math.round(value)) + ' B';
    const units = ['KB', 'MB', 'GB', 'TB'];
    let scaled = size;
    let index = -1;
    while (scaled >= 1024 && index < units.length - 1) { scaled /= 1024; index++; }
    return byteFormat.format(value < 0 ? -scaled : scaled) + ' ' + units[index];
  }

  function validPin(value) { return typeof value === 'string' && PIN_PATTERN.test(value); }

  function randomPin(random) {
    const draw = typeof random === 'function' ? random : Math.random;
    const groups = [];
    // Math.random() never yields negative values, so a plain floor is exact.
    for (let index = 0; index < 3; index++) groups.push(String(Math.floor(draw() * 1000000)).padStart(6, '0'));
    return groups.join('-');
  }

  function create(options = {}) {
    const fetchRequest = typeof options.fetch === 'function' ? options.fetch : (typeof fetch === 'function' ? fetch.bind(root) : null);
    const confirmAction = typeof options.confirm === 'function' ? options.confirm : (typeof root.confirm === 'function' ? root.confirm.bind(root) : null);
    const relativeTime = makeRelativeTime(options.now);

    const state = {
      data: null,
      uploads: null,
      // The four guest capabilities, as the gateway reports them (see guest_policy on the
      // server side): the panel only relays and flips them.
      guestPolicy: null,
      hidden: false,
      busy: false,
      denied: false,
      serial: 0,
      lastUpdated: null,
      pollTimer: null,
      noticeTimer: null,
      controller: null,
      lastFocus: null,
    };

    function responseError(response, result, fallback) {
      const message = result?.error?.message;
      const failure = new Error(typeof message === 'string' && message.trim() ? message.trim() : fallback);
      failure.status = response?.status;
      failure.denied = response?.status === 403;
      return failure;
    }
    async function readJson(response) {
      try { return await response.json(); } catch (failure) { return null; }
    }
    async function send(url, init) {
      if (!fetchRequest) throw new Error('当前环境不支持网络请求，无法管理共享连接。');
      const controller = typeof AbortController === 'function' ? new AbortController() : null;
      const options = Object.assign({ cache: 'no-store', credentials: 'same-origin', signal: controller?.signal }, init);
      setTimeout(() => controller?.abort(), REQUEST_TIMEOUT);
      const response = await fetchRequest(url, options);
      const result = await readJson(response);
      if (!response.ok) throw responseError(response, result, '共享服务暂时不可用（HTTP ' + response.status + '），请稍后重试。');
      return result || {};
    }

    const NS = 'http://www.w3.org/2000/svg';
    function node(tag, className, text) {
      const result = document.createElement(tag);
      if (className) result.className = className;
      if (text !== undefined) result.textContent = text;
      return result;
    }
    function icon(name) {
      const paths = {
        share: ['M18 8a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z', 'M6 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z', 'M18 22a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z', 'M8.6 13.7l6.8 3.6', 'M15.4 6.7 8.6 10.3'],
        tower: ['M5 12.5a10 10 0 0 1 14 0', 'M8.5 15.8a5.2 5.2 0 0 1 7 0', 'M12 19.2h.01', 'M2 9.2a15 15 0 0 1 20 0'],
        offline: ['M5 12.5a10 10 0 0 1 14 0', 'M8.5 15.8a5.2 5.2 0 0 1 7 0', 'M12 19.2h.01', 'M3 3l18 18'],
        key: ['M15.5 8.5h.01', 'M21 2l-2 2', 'M9.6 14.4 3 21', 'M15.5 3a6.5 6.5 0 1 0-3.9 11.7', 'M19 6l2-2'],
        copy: ['M9 9h10v12H9z', 'M5 15V3h10'],
        close: ['M6 6l12 12', 'M18 6L6 18'],
        refresh: ['M20 7v5h-5', 'M19 12a7 7 0 1 1-2-5l3 3'],
        kick: ['M12 3v10', 'M6.6 6.6a8 8 0 1 0 10.8 0'],
        share2: ['M4 12v7a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-7', 'M12 3v12', 'M8 7l4-4 4 4'],
      };
      const svg = document.createElementNS(NS, 'svg');
      for (const [key, value] of Object.entries({ viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 1.7, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' })) svg.setAttribute(key, value);
      for (const d of paths[name] || paths.share) { const part = document.createElementNS(NS, 'path'); part.setAttribute('d', d); svg.append(part); }
      return svg;
    }
    function button(text, action, className = 'qs-button', iconName) {
      const result = node('button', className);
      result.type = 'button';
      if (iconName) result.append(icon(iconName));
      if (text) result.append(node('span', '', text));
      result.addEventListener('click', action);
      return result;
    }
    function field(labelText, key) {
      const wrap = node('label', 'qs-field qs-field-' + key);
      wrap.append(node('span', 'qs-field-label', labelText));
      const input = node('input', 'qs-input');
      input.type = 'text';
      input.autocomplete = 'off';
      input.spellcheck = false;
      wrap.append(input);
      return wrap;
    }

    const panel = node('section', 'qs-panel');
    panel.id = 'qwen-share-dialog';
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-modal', 'false');
    panel.setAttribute('aria-labelledby', 'qs-title');
    panel.hidden = true;

    const header = node('header', 'qs-header');
    const titleBlock = node('div', 'qs-title-block');
    titleBlock.append(node('div', 'qs-eyebrow', 'QWEN LOCAL · SHARE'));
    const title = node('h1', '', '共享管理器 · 谁在连接');
    title.id = 'qs-title';
    titleBlock.append(title, node('p', 'qs-subtitle', '查看通过隧道或局域网连接本机的设备，可踢出、换口令或一键断联'));
    const headerActions = node('div', 'qs-header-actions');
    const refreshButton = button('刷新', () => load(), 'qs-button', 'refresh');
    const closeButton = button('收起', () => close(), 'qs-button qs-button-primary', 'close');
    headerActions.append(refreshButton, closeButton);
    header.append(titleBlock, headerActions);

    const controls = node('div', 'qs-controls');
    const reachBox = node('div', 'qs-reach');
    const reachBadge = node('span', 'qs-badge');
    const reachNote = node('small', 'qs-muted', '正在读取共享状态');
    // The public address is the one thing the owner has to hand out, and a quick
    // tunnel changes it on every restart, so it is shown with its own copy button
    // rather than left in a log file.
    const linkRow = node('div', 'qs-link');
    const linkValue = node('code', 'qs-link-value', '— 未开启 —');
    const linkActions = node('div', 'qs-link-actions');
    const copyLinkButton = button('复制链接', copyLink, 'qs-button qs-button-small', 'copy');
    const copyBothButton = button('复制链接和口令', copyBoth, 'qs-button qs-button-small', 'copy');
    linkActions.append(copyLinkButton, copyBothButton);
    linkRow.append(node('span', 'qs-field-label', '共享地址'), linkValue, linkActions);
    const reachActions = node('div', 'qs-reach-actions');
    const onlineButton = button('一键断联', toggleOnline, 'qs-button qs-button-danger', 'offline');
    const stopButton = button('停止共享并退出', stopService, 'qs-button', 'close');
    reachActions.append(onlineButton, stopButton);
    reachBox.append(node('span', 'qs-field-label', '公网隧道'), reachBadge, reachNote, reachActions);

    const pinBox = node('div', 'qs-pin');
    pinBox.append(node('span', 'qs-field-label', '当前共享口令'));
    const pinValue = node('strong', 'qs-pin-value', '——————');
    const pinActions = node('div', 'qs-pin-actions');
    const copyButton = button('复制口令', copyPin, 'qs-button qs-button-small', 'copy');
    const rotateButton = button('更换口令', () => { rotateOpen = !rotateOpen; rotateForm.hidden = !rotateOpen; if (rotateOpen) rotateInput.focus(); }, 'qs-button qs-button-small', 'key');
    pinActions.append(copyButton, rotateButton);
    pinBox.append(pinValue, pinActions, node('p', 'qs-note', '更换口令不会打断已加入的用户，只影响之后新加入的人。'));
    reachBox.append(linkRow);

    const rotateForm = node('form', 'qs-rotate');
    rotateForm.hidden = true;
    const rotateField = field('新口令（3 组 6 位数字）', 'pin');
    const rotateInput = rotateField.querySelector('input');
    rotateInput.placeholder = '123456-789012-345678';
    const rotateActions = node('div', 'qs-rotate-actions');
    const diceButton = button('随机生成', () => { rotateInput.value = randomPin(); rotateInput.focus(); }, 'qs-button qs-button-small');
    const saveButton = node('button', 'qs-button qs-button-small qs-button-primary', '保存新口令');
    saveButton.type = 'submit';
    rotateActions.append(diceButton, saveButton);
    const rotateNotice = node('div', 'qs-notice');
    rotateNotice.setAttribute('role', 'status');
    rotateNotice.hidden = true;
    rotateForm.append(rotateField, rotateActions, rotateNotice);
    let rotateOpen = false;

    // Guest capabilities: what a shared visitor may do with this machine. The owner asked for
    // visitors who can actually use it — switch modes (and with them the model and its plugins),
    // generate, and keep their own files — so all four start open and each can be taken back on
    // its own. The host's own controls (this panel, the PIN, usage logs, stopping the service)
    // are not on this list because they were never a visitor's to have.
    const uploadBox = node('div', 'qs-uploads');
    uploadBox.append(node('span', 'qs-field-label', '访客可以做什么'));
    const uploadStatus = node('span', 'qs-badge');
    const uploadUsage = node('small', 'qs-muted', '正在读取访客权限');
    const guestList = node('div', 'qs-guest-features');
    const guestButtons = new Map();
    for (const feature of ['chat', 'media', 'switch', 'storage']) {
      const row = node('div', 'qs-guest-feature');
      const label = node('span', 'qs-guest-feature-label');
      const hint = node('small', 'qs-guest-feature-hint');
      const toggle = button('', () => toggleFeature(feature), 'qs-button qs-button-small');
      row.append(label, hint, toggle);
      guestList.append(row);
      guestButtons.set(feature, { row, label, hint, toggle });
    }
    const uploadActions = node('div', 'qs-pin-actions');
    const uploadToggle = button('全部开放', () => setAllFeatures(true), 'qs-button qs-button-small');
    const noneToggle = button('全部收回', () => setAllFeatures(false), 'qs-button qs-button-small');
    uploadActions.append(uploadToggle, noneToggle);
    uploadBox.append(
      uploadStatus, guestList, uploadUsage, uploadActions,
      node('p', 'qs-note', '四项能力各自独立，随时可单独收回（下一个请求就生效）。'
        + '访客上传与生成的文件都放在它自己的目录里（guests/<会话>），只看得见自己那份，'
        + '上传仍有额度与磁盘余量保护。'
        + '本机管理面板、口令轮换、用量日志、停止服务与抢占显卡始终只属于机主。'));
    controls.append(reachBox, pinBox, rotateForm, uploadBox);

    const success = node('div', 'qs-success');
    success.setAttribute('role', 'status');
    success.hidden = true;
    const error = node('div', 'qs-error');
    error.setAttribute('role', 'alert');
    error.hidden = true;
    const errorText = node('span');
    error.append(errorText, button('重试', () => load(), 'qs-button qs-button-small'));

    const tableWrap = node('div', 'qs-table-wrap');
    const table = node('table', 'qs-table');
    const caption = node('caption', 'qs-sr-only', '正在共享本机服务的设备与会话');
    const headRow = node('tr');
    for (const label of ['设备标签', '来源', '首次加入', '最近活动', '请求数', '流量', '操作']) {
      const th = node('th', '', label);
      th.scope = 'col';
      headRow.append(th);
    }
    const thead = node('thead');
    thead.append(headRow);
    const tbody = node('tbody');
    table.append(caption, thead, tbody);
    const empty = node('div', 'qs-empty');
    empty.append(icon('share'), node('h3', '', '暂时没有人通过共享地址访问'), node('p', '', '有人通过隧道或局域网加入后，这里会显示设备、来源与用量。'));
    tableWrap.append(table, empty);

    const freshness = node('div', 'qs-freshness', '打开面板时每 10 秒更新 · 变更会立刻生效');
    freshness.setAttribute('role', 'status');
    // Session-wide actions: one click removes every shared device, and the tidy-up
    // drops the records of clients that logged in but never used anything. Kicked
    // devices are deleted from the list rather than shown as inactive.
    const sessionActions = node('div', 'qs-session-actions');
    const kickAllButton = button('一键踢出全部', kickAll, 'qs-button qs-button-danger', 'offline');
    const forgetButton = button('清理未使用记录', forgetUnused, 'qs-button qs-button-small', 'refresh');
    sessionActions.append(kickAllButton, forgetButton);
    const scroll = node('div', 'qs-scroll');
    scroll.append(controls, error, success, freshness, sessionActions, tableWrap);
    panel.append(header, scroll);

    const entry = button('共享管理器', () => { void open(); }, 'qs-entry-button', 'tower');
    entry.setAttribute('aria-haspopup', 'dialog');
    entry.setAttribute('aria-controls', 'qwen-share-dialog');
    entry.setAttribute('aria-expanded', 'false');
    const mount = node('div', 'qs-root');
    mount.append(entry, panel);
    // The desktop entry opens this page in a standalone app window, where the page
    // title is what the taskbar button and the window title bar show.
    if (typeof document !== 'undefined' && document) {
      document.title = 'Qwen 共享面板';
    }

    function notice(message) {
      clearTimeout(state.noticeTimer);
      success.textContent = message;
      success.hidden = !message;
      if (message) state.noticeTimer = setTimeout(() => { success.hidden = true; }, 6000);
    }
    function showError(message) {
      errorText.textContent = message;
      error.hidden = false;
    }
    function clearError() { error.hidden = true; }

    function copyText(text, successMessage) {
      if (!text) return;
      try {
        if (root.navigator?.clipboard?.writeText) {
          void root.navigator.clipboard.writeText(text);
          notice(successMessage);
          return;
        }
      } catch (failure) { /* fall through to the visible fallback */ }
      notice(successMessage + '（请手动复制）' + text);
    }

    function copyPin() {
      const text = typeof state.data?.pin === 'string' ? state.data.pin : '';
      if (!text) return;
      copyText(text, '口令已复制到剪贴板。');
      pinValue.focus?.();
    }

    function copyLink() {
      const url = typeof state.data?.url === 'string' ? state.data.url : '';
      if (!url) return;
      copyText(url, '共享地址已复制到剪贴板。');
    }

    function copyBoth() {
      const url = typeof state.data?.url === 'string' ? state.data.url : '';
      const pin = typeof state.data?.pin === 'string' ? state.data.pin : '';
      if (!url || !pin) return;
      copyText('共享地址：' + url + '\n访问口令：' + pin, '共享地址和口令已一起复制，直接发给他即可。');
    }

    function absolute(epoch) {
      return numeric(epoch) ? new Date(epoch * 1000).toLocaleString('zh-CN', { hour12: false }) : '—';
    }
    function sessionLabel(session) {
      const label = typeof session.label === 'string' && session.label.trim() ? session.label.trim() : '';
      return label || (session.kind === 'admin' ? '本机管理会话' : '未命名设备');
    }
    function renderRow(session) {
      const tr = node('tr');
      const admin = session.kind === 'admin';
      const current = session.current === true;
      const device = node('div', 'qs-pair');
      device.append(node('span', '', sessionLabel(session)), node('small', 'qs-muted', String(session.token ?? '—')));
      if (admin || current) {
        const tags = node('div', 'qs-tags');
        if (admin) {
          const adminTag = node('span', 'qs-tag', '本机管理');
          adminTag.dataset.kind = 'admin';
          tags.append(adminTag);
        }
        if (current) {
          const currentTag = node('span', 'qs-tag', '当前设备');
          currentTag.dataset.kind = 'current';
          tags.append(currentTag);
        }
        device.append(tags);
      }
      const source = node('div', 'qs-pair');
      source.append(node('span', 'qs-source', session.last_source || '—'), node('small', 'qs-muted', session.last_endpoint || '暂无访问记录'));
      const joined = node('span', 'qs-nowrap', relativeTime(session.created_at));
      joined.title = absolute(session.created_at);
      const seen = node('span', 'qs-nowrap', relativeTime(session.last_seen));
      seen.title = absolute(session.last_seen);
      const requests = node('span', 'qs-nowrap', numeric(session.requests) ? numberFormat.format(session.requests) + ' 次' : '—');
      const traffic = node('span', 'qs-nowrap', formatBytes(session.bytes));
      const actions = node('div', 'qs-row-actions');
      if (admin) actions.append(node('span', 'qs-muted', '不可踢出'));
      else if (current) {
        const selfKick = button('当前设备', () => {}, 'qs-button qs-button-small qs-button-danger');
        selfKick.disabled = true;
        selfKick.title = '不能踢出当前浏览器会话';
        actions.append(selfKick);
      } else {
        const blocked = session.blocked === true;
        const kick = button(blocked ? '已踢出' : '踢出', () => revoke(session), 'qs-button qs-button-small qs-button-danger', blocked ? undefined : 'kick');
        kick.disabled = blocked;
        actions.append(kick);
      }
      for (const content of [device, source, joined, seen, requests, traffic, actions]) {
        const td = node('td');
        td.append(content);
        tr.append(td);
      }
      return tr;
    }
    function renderSessions() {
      const sessions = Array.isArray(state.data?.sessions) ? state.data.sessions : [];
      tbody.replaceChildren(...sessions.map(renderRow));
      table.hidden = sessions.length === 0;
      empty.hidden = sessions.length !== 0;
      const devices = sessions.filter(session => session.kind !== 'admin').length;
      caption.textContent = '正在共享本机服务的设备与会话，共 ' + numberFormat.format(sessions.length) + ' 条，其中设备 ' + numberFormat.format(devices) + ' 台';
    }
    function render() {
      const online = state.data?.online === true;
      reachBadge.textContent = online ? '公网可达' : '已断联';
      reachBadge.dataset.online = online ? 'true' : 'false';
      reachNote.textContent = online ? '外部用户可通过共享地址连接，本机始终可用。' : '公网隧道已关闭，外部用户无法连接，本机不受影响。';
      onlineButton.replaceChildren(icon(online ? 'offline' : 'tower'), node('span', '', online ? '一键断联' : '恢复连接'));
      onlineButton.disabled = false;
      pinValue.textContent = typeof state.data?.pin === 'string' && state.data.pin ? state.data.pin : '—— 未设置 ——';
      copyButton.disabled = !(typeof state.data?.pin === 'string' && state.data.pin);
      const url = typeof state.data?.url === 'string' ? state.data.url : '';
      linkValue.textContent = url || (online ? '正在获取…' : '— 未开启 —');
      copyLinkButton.disabled = !url;
      copyBothButton.disabled = !url || !state.data?.pin;
      renderUploads();
      renderSessions();
    }

    function renderUploads() {
      // Shown from the payload the routes themselves read, so the panel cannot claim a
      // capability is open while the route still refuses. If the status payload is
      // older than this feature, the section states that plainly instead of guessing.
      const info = state.uploads || state.data?.guest_uploads || null;
      const policy = state.guestPolicy || state.data?.guest_policy || null;
      if (!policy || !policy.features) {
        uploadStatus.textContent = '不可用';
        uploadStatus.dataset.on = 'false';
        uploadUsage.textContent = '当前服务未上报访客权限，请重启服务后再试。';
        for (const entry of guestButtons.values()) entry.toggle.disabled = true;
        uploadToggle.disabled = true;
        noneToggle.disabled = true;
        return;
      }
      const features = policy.features;
      const labels = policy.labels || {};
      const descriptions = policy.descriptions || {};
      for (const [name, entry] of guestButtons) {
        const open = features[name] === true;
        entry.label.textContent = labels[name] || name;
        entry.hint.textContent = descriptions[name] || '';
        entry.toggle.replaceChildren(node('span', '', open ? '收回' : '开放'));
        entry.toggle.dataset.on = open ? 'true' : 'false';
        entry.toggle.disabled = false;
        entry.row.dataset.on = open ? 'true' : 'false';
      }
      const openCount = Object.values(features).filter(Boolean).length;
      uploadStatus.textContent = openCount === 0 ? '全部仅本机管理员'
        : (openCount === Object.keys(features).length ? '四项全部开放给访客'
          : `已开放 ${openCount} / ${Object.keys(features).length} 项`);
      uploadStatus.dataset.on = openCount ? 'true' : 'false';
      uploadToggle.disabled = openCount === Object.keys(features).length;
      noneToggle.disabled = openCount === 0;
      const usage = (info && info.usage) || {};
      uploadUsage.textContent = usage.guest_files
        ? '访客已上传 ' + numberFormat.format(usage.guest_files) + ' 个片段 · '
          + formatBytes(usage.guest_bytes || 0) + ' / 上限 ' + formatBytes(usage.guest_total_limit || 0)
        : '访客还没有上传任何片段';
    }

    async function setFeature(feature, allowed, note) {
      try {
        const result = await send('/local/share/guest-policy', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ feature, allowed }),
        });
        state.guestPolicy = result;
        if (state.data) state.data.guest_policy = result;
        renderUploads();
        notice(note);
      } catch (failure) {
        showError(failure.message || '更新访客权限失败，请稍后重试。');
      }
    }

    function toggleFeature(feature) {
      const policy = state.guestPolicy || state.data?.guest_policy || {};
      const current = Boolean((policy.features || {})[feature]);
      const label = (policy.labels || {})[feature] || feature;
      const question = current
        ? `收回「${label}」？\n\n该能力立刻对访客关闭（已经在跑的任务不受影响），其余能力不变。`
        : `开放「${label}」给访客？\n\n访客将能使用这项能力；随时可以在这里收回。`;
      if (confirmAction && !confirmAction(question)) return;
      void setFeature(feature, !current,
        current ? `已收回「${label}」` : `已开放「${label}」给访客`);
    }

    async function setAllFeatures(allowed) {
      const question = allowed
        ? '把四项能力全部开放给访客？\n\n访客将能对话与语音、生图与生成视频、自己切换模式与模型，'
          + '并上传下载自己的文件。随时可以逐项收回。'
        : '把四项能力全部收回到仅本机管理员？\n\n注意：收回「对话与语音」后访客连聊天也会被拒。';
      if (confirmAction && !confirmAction(question)) return;
      try {
        // Sent one by one: the route owns one switch at a time, so a partial failure leaves a
        // state the panel can still describe honestly instead of an all-or-nothing claim.
        for (const feature of ['chat', 'media', 'switch', 'storage']) {
          const result = await send('/local/share/guest-policy', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ feature, allowed }),
          });
          state.guestPolicy = result;
          if (state.data) state.data.guest_policy = result;
        }
        renderUploads();
        notice(allowed ? '四项能力已全部开放给访客' : '四项能力已全部收回到仅本机管理员');
      } catch (failure) {
        showError(failure.message || '更新访客权限失败，请稍后重试。');
      }
    }

    // Always resolves, so callers (and the host page) can await one refresh.
    function load() {
      return (async () => {
        if (state.hidden || state.denied || panel.hidden) return;
        clearTimeout(state.pollTimer);
        state.controller?.abort();
        const request = ++state.serial;
        const controller = typeof AbortController === 'function' ? new AbortController() : null;
        state.controller = controller;
        const timeout = setTimeout(() => controller?.abort(), REQUEST_TIMEOUT);
        refreshButton.disabled = true;
        panel.setAttribute('aria-busy', 'true');
        try {
          const response = await fetchRequest(STATUS_URL, { cache: 'no-store', credentials: 'same-origin', signal: controller?.signal });
          const result = await readJson(response);
          if (request !== state.serial || panel.hidden) return;
          if (response.status === 403) { deny(); return; }
          if (!response.ok) throw responseError(response, result, '共享服务暂时不可用（HTTP ' + response.status + '），请稍后重试。');
          if (!result || !Array.isArray(result.sessions)) throw new Error('共享服务返回的数据格式不正确，请刷新重试。');
          state.data = result;
          state.uploads = result.guest_uploads || null;
          state.lastUpdated = new Date();
          clearError();
          render();
        } catch (failure) {
          if (request !== state.serial || panel.hidden || state.hidden) return;
          if (failure.denied) { deny(); return; }
          showError(failure.name === 'AbortError' ? '共享状态请求超时，稍后会自动重试。' : failure.message || '无法连接共享服务，请稍后重试。');
        } finally {
          clearTimeout(timeout);
          if (request === state.serial) {
            refreshButton.disabled = false;
            panel.setAttribute('aria-busy', 'false');
            schedule();
          }
        }
      })();
    }
    function schedule() {
      clearTimeout(state.pollTimer);
      if (state.hidden || state.denied || panel.hidden) return;
      state.pollTimer = setTimeout(() => document.hidden ? schedule() : load(), POLL_INTERVAL);
    }
    function stopPolling() {
      // The service is gone after a stop, so a polling loop would only produce
      // error banners against a closed port.
      state.hidden = true;
      clearTimeout(state.pollTimer);
      state.controller?.abort();
      entry.hidden = true;
    }
    // A 403 means this visitor is not the administrator: hide the entry point
    // and stop quietly instead of showing an error or retrying.
    function deny() {
      state.denied = true;
      state.hidden = true;
      clearTimeout(state.pollTimer);
      state.controller?.abort();
      entry.hidden = true;
      close();
    }

    async function revoke(session) {
      if (session.current === true || session.kind === 'admin') return;
      const prompt = '确定踢出「' + sessionLabel(session) + '」吗？它的记录会从列表里删除，该设备需要凭当前口令重新加入。';
      if (confirmAction && !confirmAction(prompt)) return;
      const label = sessionLabel(session);
      try {
        await send('/local/share/sessions/' + encodeURIComponent(String(session.token)) + '/revoke', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}),
        });
        if (Array.isArray(state.data?.sessions)) {
          state.data.sessions = state.data.sessions.filter(item => item.token !== session.token);
          renderSessions();
        }
        notice('已踢出「' + label + '」并删除其记录，该设备需要重新登录。');
        void load();
      } catch (failure) {
        showError(failure.message || '踢出失败，请稍后重试。');
      }
    }

    async function kickAll() {
      const devices = (state.data?.sessions || []).filter(item => item.kind !== 'admin').length;
      if (!devices) { notice('当前没有其他设备在使用，无需踢出。'); return; }
      const prompt = '一键踢出全部 ' + devices + ' 个设备吗？\n'
        + '它们的记录会从列表里删除，所有设备都需要凭当前口令重新加入。本机管理会话不受影响。';
      if (confirmAction && !confirmAction(prompt)) return;
      kickAllButton.disabled = true;
      try {
        const result = await send(KICK_ALL_URL, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}),
        });
        const kicked = numeric(result.kicked) ? result.kicked : devices;
        if (Array.isArray(state.data?.sessions)) {
          state.data.sessions = state.data.sessions.filter(item => item.kind === 'admin');
          renderSessions();
        }
        notice('已踢出全部 ' + kicked + ' 个设备并删除其记录，它们需要凭当前口令重新加入。');
        void load();
      } catch (failure) {
        showError(failure.message || '一键踢出失败，请稍后重试。');
      } finally {
        kickAllButton.disabled = false;
      }
    }

    async function forgetUnused() {
      const unused = (state.data?.sessions || []).filter(item => item.kind !== 'admin' && !item.requests).length;
      if (!unused) { notice('没有需要清理的未使用记录。'); return; }
      const prompt = '清理 ' + unused + ' 条「加入过但从未使用」的记录吗？\n'
        + '只删除请求数为 0 的记录，正在使用的设备不受影响。';
      if (confirmAction && !confirmAction(prompt)) return;
      forgetButton.disabled = true;
      try {
        const result = await send(FORGET_UNUSED_URL, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}),
        });
        const removed = numeric(result.removed) ? result.removed : unused;
        notice('已清理 ' + removed + ' 条未使用记录。');
        void load();
      } catch (failure) {
        showError(failure.message || '清理失败，请稍后重试。');
      } finally {
        forgetButton.disabled = false;
      }
    }
    async function toggleUploads() {
      // Kept as the plain "open/close the upload capability" action: the four-switch UI calls
      // setFeature directly, and this route still exists for older panels and the acceptance
      // script, so the panel keeps one code path for it.
      const info = state.uploads || state.data?.guest_uploads || null;
      const open = info?.policy === 'guests';
      const message = open
        ? '收回访客上传后，访客将无法再上传片段，也不能再下载此前上传的片段。\n'
          + '已经上传到 ComfyUI input 目录的文件不会被删除，需要你自行清理。确认收回吗？'
        : '开放后，共享访客可以上传视频片段到本机，并只能看到、下载自己上传的片段。确认开放吗？';
      if (confirmAction && !confirmAction(message)) return;
      uploadToggle.disabled = true;
      try {
        const result = await send(UPLOADS_URL, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ policy: open ? 'off' : 'guests' }),
        });
        state.uploads = result;
        state.guestPolicy = result.guest_policy || state.guestPolicy;
        if (state.data) {
          state.data.guest_uploads = result;
          if (result.guest_policy) state.data.guest_policy = result.guest_policy;
        }
        renderUploads();
        notice(open ? '已收回访客上传，访客不能再上传或下载片段。'
                    : '已开放访客上传：访客可以上传片段，并只能下载自己上传的片段。');
      } catch (failure) {
        uploadToggle.disabled = false;
        showError(failure.message || '切换访客上传失败，请稍后重试。');
      }
    }
    async function toggleOnline() {
      const next = !(state.data?.online === true);
      if (!next && confirmAction && !confirmAction('一键断联会立即关闭公网隧道，外部用户将立即无法访问，本机不受影响。确认断联吗？')) return;
      onlineButton.disabled = true;
      try {
        const result = await send(ONLINE_URL, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ online: next }) });
        if (state.data) state.data.online = result.online === true ? true : next;
        render();
        notice(next ? '已恢复公网连接，外部用户可以继续使用共享地址。' : '已一键断联，外部用户立即无法访问，本机不受影响。');
        void load();
      } catch (failure) {
        onlineButton.disabled = false;
        showError(failure.message || '切换公网状态失败，请稍后重试。');
      }
    }
    async function stopService() {
      const message = '停止共享并退出会关闭公网隧道，并结束本机服务，外部用户立即无法访问。\n'
        + '共享配置和口令会保留，下次从桌面「Qwen 共享面板」启动即可恢复。确认停止吗？';
      if (confirmAction && !confirmAction(message)) return;
      stopButton.disabled = true;
      try {
        await send(STOP_URL, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) });
      } catch (failure) {
        // The gateway may drop the connection as it exits, which is success here.
        const text = String(failure?.message || '');
        if (!/network|abort|fetch|连接/i.test(text)) {
          stopButton.disabled = false;
          showError(text || '停止失败，请稍后重试。');
          return;
        }
      }
      panel.replaceChildren(node('div', 'qs-stopped',
        '服务已停止，外部与本机都无法再访问。要再次共享，请双击桌面「Qwen 共享面板」。'));
      stopPolling();
    }

    function rotatePin(event) {
      event.preventDefault();
      const value = rotateInput.value.trim();
      rotateNotice.hidden = false;
      rotateNotice.dataset.state = 'error';
      if (!validPin(value)) { rotateNotice.textContent = PIN_HINT; return; }
      rotateNotice.dataset.state = 'pending';
      rotateNotice.textContent = '正在保存新口令…';
      saveButton.disabled = true;
      send(PIN_URL, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pin: value }) }).then(result => {
        saveButton.disabled = false;
        if (state.data) {
          state.data.pin = typeof result.pin === 'string' && result.pin ? result.pin : value;
          if (numeric(result.rotated_at)) state.data.pin_rotated_at = result.rotated_at;
        }
        rotateNotice.dataset.state = 'ok';
        rotateNotice.textContent = '新口令已生效，已加入的用户不受影响，只影响之后新加入的人。';
        render();
        void load();
      }).catch(failure => {
        saveButton.disabled = false;
        rotateNotice.dataset.state = 'error';
        rotateNotice.textContent = failure.message || '更换口令失败，请稍后重试。';
      });
    }
    rotateForm.addEventListener('submit', rotatePin);

    function open() {
      if (state.hidden || state.denied || !panel.hidden) return undefined;
      state.lastFocus = document.activeElement;
      panel.hidden = false;
      entry.setAttribute('aria-expanded', 'true');
      clearError();
      closeButton.focus?.();
      if (!state.data) tbody.replaceChildren();
      return load();
    }
    function close() {
      if (panel.hidden) return;
      state.serial++;
      state.controller?.abort();
      clearTimeout(state.pollTimer);
      clearTimeout(state.noticeTimer);
      panel.hidden = true;
      entry.setAttribute('aria-expanded', 'false');
      const focus = state.lastFocus;
      if (focus && focus.isConnected !== false && typeof focus.focus === 'function') focus.focus({ preventScroll: true });
    }
    panel.addEventListener('keydown', event => event.stopPropagation());
    document.addEventListener('visibilitychange', () => { if (!document.hidden && !panel.hidden) void load(); });

    function attach(target) {
      const parent = target || document.body;
      if (!parent) return null;
      if (mount.parentElement !== parent) parent.append(mount);
      return mount;
    }

    return {
      root: mount, panel: panel, entry: entry, state: state,
      get hidden() { return state.hidden; },
      attach: attach, open: open, close: close, load: load, render: render, schedule: schedule,
      visible: () => !panel.hidden && !state.hidden,
    };
  }

  // No top-level DOM work: mounting is the host page's (or the test's) call.
  function attach(target) {
    if (typeof document === 'undefined') return null;
    if (document.getElementById('qwen-share-dialog')) return null;
    try {
      const panel = create();
      panel.attach(target);
      return panel;
    } catch (failure) {
      if (typeof console !== 'undefined') console.error('Qwen 共享管理器加载失败', failure);
      return null;
    }
  }

  root.QwenShare = {
    create: create,
    attach: attach,
    relativeTime: makeRelativeTime(),
    formatBytes: formatBytes,
    validPin: validPin,
    pinFormat: validPin,
    randomPin: randomPin,
  };

  // The host injects this file with `defer`, so the document is already parsed and
  // the panel can mount immediately. Guarded on `document` so loading the module in
  // Node (the unit test, which mocks its own DOM) has no side effect, and
  // QWEN_SHARE_NO_AUTO_ATTACH lets a host that mounts the panel itself opt out.
  if (!root.QWEN_SHARE_NO_AUTO_ATTACH
      && typeof document !== 'undefined' && document
      && typeof document.createElement === 'function') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', attach, { once: true });
    } else {
      attach();
    }
  }
})(typeof window !== 'undefined' ? window : globalThis);
