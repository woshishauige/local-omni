const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, 'share-manager.js'), 'utf8');

const PIN = '611761-992509-889670';
const TUNNEL = 'https://plain-paper-example.trycloudflare.com';
// Session timestamps are anchored to the mocked clock so relative times are exact.
const NOW = new Date(2026, 8, 7, 15, 30, 12).getTime() / 1000;
const STATUS = {
  instance_id: 'c8d9fb00174a4a74bdd9703252a0387c',
  updated_at: 1789366324.841,
  pin_rotated_at: 1789366000.0,
  online: true,
  url: TUNNEL,
  pin: PIN,
  sessions: [
    { token: 'aaaa1111bbbb2222', kind: 'admin', label: '本机浏览器', created_at: NOW - 7200, last_seen: NOW - 120,
      requests: 120, bytes: 2097152, last_endpoint: '/local/share/status', last_source: '127.0.0.1', blocked: false, current: true },
    { token: '3f2a1b9c8d7e6f50', kind: 'device', label: 'iPhone 15', created_at: NOW - 3600, last_seen: NOW - 180,
      requests: 42, bytes: 1048576, last_endpoint: '/v1/chat/completions', last_source: '10.36.22.9', blocked: false, current: false },
    { token: '5c4d3e2f10987654', kind: 'device', label: 'iPad Air', created_at: NOW - 600, last_seen: NOW - 60,
      requests: 7, bytes: 4096, last_endpoint: '/v1/models', last_source: '192.168.1.24', blocked: false, current: true },
    { token: '99ff88ee77dd66cc', kind: 'device', label: '<img src=x onerror=alert(1)>', created_at: NOW - 7200, last_seen: NOW - 3600,
      requests: 987654, bytes: 5368709120, last_endpoint: '/v1/models', last_source: '2001:db8::1', blocked: false, current: false },
  ],
  // The clip-upload switch, as /local/share/status reports it: closed until the owner
  // opens it, so a panel that guessed would be wrong here.
  guest_uploads: { policy: 'off', policies: ['off', 'guests'],
    limits: { session_bytes: 4294967296, total_bytes: 21474836480 },
    usage: { files: 0, guest_files: 0, guest_bytes: 0, guest_total_limit: 21474836480 } },
};

function fixture(options = {}) {
  const env = { now: new Date(2026, 8, 7, 15, 30, 12).getTime(), requests: [], timers: [], confirmations: [], fail: false, status: options.status || STATUS };
  let nextTimer = 0;

  class Element {
    constructor(tag) {
      this.tagName = tag.toLowerCase(); this.children = []; this.attributes = new Map(); this.handlers = new Map();
      this.dataset = {}; this.text = ''; this.open = false; this.className = ''; this.hidden = false;
      this.style = { values: new Map(), setProperty(key, value) { this.values.set(key, value); } };
      this.classList = { add: (...names) => { this.className = [...new Set([...this.className.split(' '), ...names])].join(' ').trim(); } };
    }
    set innerHTML(_) { throw new Error('HTML injection is forbidden in share UI'); }
    set textContent(value) { this.text = String(value); this.children = []; }
    get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
    set value(value) { this.inputValue = String(value); }
    get value() { return this.inputValue ?? ''; }
    set disabled(value) { this.isDisabled = Boolean(value); }
    get disabled() { return this.isDisabled === true; }
    get isConnected() { return this === env.document || !!this.parentElement?.isConnected; }
    get elements() { return this.querySelectorAll('input, select, textarea'); }
    append(...children) { for (const child of children) { child.remove(); child.parentElement = this; this.children.push(child); } }
    prepend(...children) { for (const child of [...children].reverse()) { child.remove(); child.parentElement = this; this.children.unshift(child); } }
    remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this); this.parentElement = null; }
    replaceChildren(...children) { for (const child of this.children) child.parentElement = null; this.children = []; this.text = ''; this.append(...children); }
    setAttribute(key, value) { const text = String(value); this.attributes.set(key, text); if (key === 'hidden') this.hidden = text !== 'false'; }
    getAttribute(key) { return this.attributes.get(key); }
    addEventListener(name, callback) { const list = this.handlers.get(name) || []; list.push(callback); this.handlers.set(name, list); }
    async fire(name, event = {}) { for (const callback of this.handlers.get(name) || []) await callback(event); }
    focus() { env.document.activeElement = this; }
    click() { return this.fire('click'); }
    close() { this.open = false; void this.fire('close'); }
    getBoundingClientRect() { return { right: this.right || 56 }; }
    matches(selector) {
      if (selector.startsWith('.')) return (this.className || '').split(' ').includes(selector.slice(1));
      if (selector.startsWith('#')) return this.id === selector.slice(1);
      const attribute = /^\[([^=\]]+)(?:="([^"]*)")?\]$/.exec(selector);
      if (attribute) return attribute[2] === undefined ? this.getAttribute(attribute[1]) !== undefined : this.getAttribute(attribute[1]) === attribute[2];
      return this.tagName === selector;
    }
    all() { return this.children.flatMap(child => [child, ...child.all()]); }
    querySelectorAll(selector) { return this.all().filter(child => selector.split(',').some(part => child.matches(part.trim()))); }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  }

  const document = new Element('document');
  env.document = document;
  document.body = new Element('body');
  document.append(document.body);
  document.createElement = tag => new Element(tag);
  document.createElementNS = (_, tag) => new Element(tag);
  document.getElementById = id => document.all().find(child => child.id === id) || null;

  const window = new Element('window');
  window.innerWidth = 1440;
  window.navigator = { clipboard: { writeText: async text => { env.copied = text; } } };
  window.confirm = message => { env.confirmations.push(message); return env.confirm !== false; };
  class TaskDate extends Date { constructor(...args) { super(...(args.length ? args : [env.now])); } static now() { return env.now; } }

  env.posts = () => env.requests.filter(request => (request.options?.method || 'GET') === 'POST');
  async function json(response) {
    if (env.fail) throw new Error('Connection refused');
    return { ok: response.status >= 200 && response.status < 300, status: response.status, json: async () => response.body };
  }
  window.fetch = async (url, options = {}) => {
    env.requests.push({ url, options });
    if (env.fail) throw new Error('Connection refused');
    if (url === '/local/share/status') return json({ status: env.statusCode || 200, body: env.status });
    if (url.endsWith('/revoke')) return json({ status: env.revokeCode || 200, body: env.revokeCode ? { error: { message: '该会话已被移除。' } } : { revoked: true, token: url.split('/')[4] } });
    if (url === '/local/share/pin') {
      const body = JSON.parse(options.body);
      env.pinBody = body;
      if (env.pinCode) return json({ status: env.pinCode, body: { error: { message: '口令已被其他管理员同时修改。' } } });
      return json({ status: 200, body: { pin: body.pin, rotated_at: env.now / 1000 } });
    }
    if (url === '/local/share/online') {
      const body = JSON.parse(options.body);
      env.onlineBody = body;
      return json({ status: 200, body: { online: body.online } });
    }
    if (url === '/local/share/stop') {
      env.stopped = (env.stopped || 0) + 1;
      if (env.stopFail) return json({ status: 409, body: { error: { message: '有请求正在生成，请等它结束后再停止。' } } });
      return json({ status: 200, body: { stopped: true } });
    }
    if (url === '/local/share/sessions/revoke-all') {
      env.kickedAll = (env.kickedAll || 0) + 1;
      const devices = (env.status.sessions || []).filter(item => item.kind !== 'admin');
      env.status.sessions = (env.status.sessions || []).filter(item => item.kind === 'admin');
      return json({ status: 200, body: { kicked: devices.length } });
    }
    if (url === '/local/share/sessions/forget-unused') {
      const unused = (env.status.sessions || []).filter(item => item.kind !== 'admin' && !item.requests);
      env.status.sessions = (env.status.sessions || []).filter(item => item.kind === 'admin' || item.requests);
      return json({ status: 200, body: { removed: unused.length } });
    }
    if (url === '/local/share/uploads') {
      if ((options.method || 'GET') === 'POST') {
        const body = JSON.parse(options.body);
        env.uploadBody = body;
        if (env.uploadCode) return json({ status: env.uploadCode, body: { error: { message: '访客上传设置保存失败。' } } });
        env.status.guest_uploads = Object.assign({}, env.status.guest_uploads, { policy: body.policy });
      }
      return json({ status: 200, body: env.status.guest_uploads });
    }
    throw new Error('Unexpected URL ' + url);
  };

  const context = {
    document, window, Intl, Date: TaskDate, URLSearchParams, AbortController, AbortSignal, navigator: window.navigator, console,
    setTimeout(callback, delay) { const id = ++nextTimer; env.timers.push({ id, callback, delay }); return id; },
    clearTimeout: id => { env.timers = env.timers.filter(timer => timer.id !== id); },
    // The panel mounts itself when it finds a document, which is what the browser
    // needs; the host is the test here, so the auto-mount is switched off and every
    // mount below is an explicit call the assertions can reason about.
    QWEN_SHARE_NO_AUTO_ATTACH: true,
  };
  window.QWEN_SHARE_NO_AUTO_ATTACH = true;
  vm.runInNewContext(source, context);

  env.QwenShare = context.window.QwenShare || context.QwenShare;
  env.flush = async () => { for (let index = 0; index < 25; index++) await Promise.resolve(); };
  // Attaches a panel to the mock document without opening it, so the test can
  // assert the collapsed state, the polling schedule and the 403 handling.
  env.create = async extra => {
    const panel = await env.QwenShare.create(Object.assign({ fetch: window.fetch, now: () => env.now, confirm: window.confirm }, extra));
    panel.attach(document.body);
    await env.flush();
    return panel;
  };
  env.find = className => document.querySelector('.' + className);
  env.button = text => document.querySelectorAll('button').find(button => button.textContent === text);
  env.rows = panel => panel.panel.querySelector('tbody').children;
  env.poll = async () => {
    const timer = env.timers.find(item => item.delay === 10000);
    assert.ok(timer, 'an open panel schedules one 10-second poll');
    env.timers = env.timers.filter(item => item !== timer);
    await timer.callback();
    await env.flush();
  };
  env.polls = () => env.timers.filter(item => item.delay === 10000).length;
  env.Element = Element;
  return env;
}

(async () => {
  const env = fixture();
  assert.equal(typeof env.QwenShare, 'object', 'the module exports QwenShare');
  assert.equal(typeof env.QwenShare.create, 'function');
  assert.equal(typeof env.QwenShare.attach, 'function');
  assert.equal(env.requests.length, 0, 'loading the module performs no request');

  // Relative time formatting.
  const relative = env.QwenShare.relativeTime;
  assert.equal(relative(env.now / 1000), '刚刚');
  assert.equal(relative(env.now / 1000 - 0.4), '刚刚');
  assert.equal(relative(env.now / 1000 - 30), '30 秒前');
  assert.equal(relative(env.now / 1000 - 60), '1 分钟前');
  assert.equal(relative(env.now / 1000 - 3 * 60), '3 分钟前');
  assert.equal(relative(env.now / 1000 - 3600), '1 小时前');
  assert.equal(relative(env.now / 1000 - 5 * 3600 - 30), '5 小时前');
  assert.equal(relative(env.now / 1000 - 86400), '1 天前');
  assert.equal(relative(env.now / 1000 - 9 * 86400), '9 天前');
  assert.equal(relative(env.now / 1000 - 40 * 86400), '1 个月前');
  assert.equal(relative(null), '—');
  assert.equal(relative(undefined), '—');
  assert.equal(relative('nope'), '—');

  // Traffic formatting.
  const bytes = env.QwenShare.formatBytes;
  assert.equal(bytes(0), '0 B');
  assert.equal(bytes(512), '512 B');
  assert.equal(bytes(1023), '1,023 B', 'byte counts use zh-CN grouping');
  assert.equal(bytes(1024), '1 KB');
  assert.equal(bytes(1536), '1.5 KB');
  assert.equal(bytes(1048576), '1 MB');
  assert.equal(bytes(1572864), '1.5 MB');
  assert.equal(bytes(5368709120), '5 GB');
  assert.equal(bytes(null), '—');
  assert.equal(bytes(Number.NaN), '—');

  // PIN format validation.
  const validPin = env.QwenShare.validPin;
  assert.equal(env.QwenShare.pinFormat, validPin, 'both export names point at the same check');
  assert.equal(validPin('123456-789012-345678'), true);
  assert.equal(validPin(PIN), true);
  assert.equal(validPin('123456'), false);
  assert.equal(validPin('12345-789012-345678'), false);
  assert.equal(validPin('1234567-789012-345678'), false);
  assert.equal(validPin('abcdef-789012-345678'), false);
  assert.equal(validPin('123456-78901-345678'), false);
  assert.equal(validPin('123456_789012_345678'), false);
  assert.equal(validPin(''), false);
  assert.equal(validPin(null), false);
  assert.equal(validPin(123456789012345678), false);
  const random = env.QwenShare.randomPin();
  assert.match(random, /^\d{6}-\d{6}-\d{6}$/, 'the generator always produces a valid PIN');

  // Closed panel: an entry button exists, nothing is fetched or scheduled.
  const panel = await env.create();
  assert.equal(env.document.getElementById('qwen-share-dialog'), panel.panel, 'the panel is mounted by attach()');
  assert.equal(env.find('qs-entry-button').textContent, '共享管理器');
  assert.equal(panel.panel.hidden, true, 'the panel starts collapsed');
  assert.equal(env.requests.length, 0, 'a collapsed panel must not poll');
  assert.equal(env.polls(), 0);

  // Opening loads the status once and schedules the 10-second refresh.
  await env.find('qs-entry-button').click();
  await env.flush();
  assert.equal(env.requests.length, 1);
  assert.equal(env.requests[0].url, '/local/share/status');
  assert.equal(env.requests[0].options.cache, 'no-store');
  assert.equal(env.requests[0].options.credentials, 'same-origin');
  assert.equal(panel.panel.hidden, false);
  assert.equal(env.find('qs-entry-button').getAttribute('aria-expanded'), 'true');
  assert.equal(env.polls(), 1, 'an open panel schedules exactly one poll');
  assert.equal(env.find('qs-badge').textContent, '公网可达');
  assert.equal(env.find('qs-badge').dataset.online, 'true');
  assert.equal(env.button('一键断联').disabled, false);
  assert.equal(env.find('qs-pin-value').textContent, PIN);

  const body = panel.panel.querySelector('tbody');
  assert.equal(env.rows(panel).length, 4);
  assert.equal(body.querySelectorAll('img').length, 0, 'labels are rendered as text, never HTML');
  assert.ok(body.textContent.includes('<img src=x onerror=alert(1)>'), 'the hostile label is shown literally');
  assert.ok(body.textContent.includes('本机管理'), 'admin sessions are marked as local management');
  assert.ok(body.textContent.includes('当前设备'), 'the current browser session is marked');
  assert.ok(body.textContent.includes('3 分钟前'), 'recent activity renders as relative time');
  assert.ok(body.textContent.includes('1 分钟前'), 'the other device activity renders as relative time');
  assert.ok(body.textContent.includes('1 小时前'), 'older activity renders as relative time');
  assert.ok(body.textContent.includes('2 小时前'), 'first-join time renders as relative time');
  assert.ok(body.textContent.includes('42 次'), 'request counts are formatted');
  assert.ok(body.textContent.includes('987,654 次'), 'large request counts use zh-CN grouping');
  assert.ok(body.textContent.includes('1 MB'), 'traffic uses the matching unit');
  assert.ok(body.textContent.includes('5 GB'), 'large traffic uses the matching unit');
  assert.ok(body.textContent.includes('10.36.22.9'));
  assert.ok(body.textContent.includes('2001:db8::1'));
  const timeCells = body.querySelectorAll('.qs-nowrap');
  assert.ok(timeCells[0].title.length > 4 && timeCells[0].title !== '—', 'hovering a timestamp reveals the absolute time');
  assert.equal(timeCells[0].textContent, '2 小时前', 'the first timestamp column is 首次加入');

  // Kick buttons: never for admin, disabled for the current device, live for the rest.
  const kickButtons = body.querySelectorAll('button');
  assert.equal(kickButtons.length, 3, 'admin sessions get no kick button at all');
  const adminRow = env.rows(panel)[0];
  assert.equal(adminRow.querySelectorAll('button').length, 0, 'the admin row exposes no action button');
  assert.ok(adminRow.textContent.includes('不可踢出'));
  const selfButton = env.rows(panel)[2].querySelector('button');
  assert.equal(selfButton.textContent, '当前设备');
  assert.equal(selfButton.disabled, true, 'the current browser session cannot kick itself');
  const deviceButton = env.rows(panel)[1].querySelector('button');
  assert.equal(deviceButton.textContent, '踢出');
  assert.equal(deviceButton.disabled, false);

  // Polling follows the clock and pauses nothing while visible.
  env.now += 120000;
  await env.poll();
  assert.equal(env.requests.filter(request => request.url === '/local/share/status').length, 2);
  assert.equal(env.rows(panel)[1].querySelectorAll('.qs-nowrap')[1].textContent, '5 分钟前', 'refreshed rows follow the current time');

  // Copying the shared PIN, then the pair the owner actually hands out.
  await env.button('复制口令').click();
  await env.flush();
  assert.equal(env.copied, PIN);
  assert.equal(env.find('qs-success').hidden, false);

  assert.equal(env.find('qs-link-value').textContent, TUNNEL, 'the public address is shown');
  await env.button('复制链接').click();
  await env.flush();
  assert.equal(env.copied, TUNNEL, 'the link alone can be copied');
  await env.button('复制链接和口令').click();
  await env.flush();
  assert.ok(env.copied.includes(TUNNEL) && env.copied.includes(PIN),
    'one click copies the address and the passphrase together');

  // Kicking a device asks for confirmation first.
  const hostileButton = env.rows(panel)[3].querySelector('button');
  env.confirm = false;
  await hostileButton.click();
  await env.flush();
  assert.equal(env.confirmations.length, 1);
  assert.ok(env.confirmations[0].includes('<img src=x onerror=alert(1)>'), 'the confirmation names the device');
  assert.ok(env.confirmations[0].includes('记录会从列表里删除'), 'the confirmation says the record is removed');
  assert.equal(env.posts().length, 0, 'a declined confirmation sends nothing');
  assert.equal(env.rows(panel).length, 4);
  env.confirm = true;
  await hostileButton.click();
  await env.flush();
  const revoke = env.posts().find(request => request.url.endsWith('/revoke'));
  assert.equal(revoke.url, '/local/share/sessions/99ff88ee77dd66cc/revoke');
  assert.equal(revoke.options.method, 'POST');
  assert.equal(env.rows(panel).length, 3, 'a revoked row disappears immediately');
  assert.ok(env.find('qs-success').textContent.includes('已踢出'));

  // Credentials are never sent for an admin session.
  assert.equal(env.posts().filter(request => request.url.includes('aaaa1111bbbb2222')).length, 0);

  // A failed kick surfaces the backend message and keeps the row.
  env.revokeCode = 409;
  const remaining = env.rows(panel)[1].querySelector('button');
  await remaining.click();
  await env.flush();
  assert.equal(env.find('qs-error').hidden, false);
  assert.ok(env.find('qs-error').textContent.includes('该会话已被移除。'));
  env.revokeCode = 0;

  // One-click kick-all: confirmation first, then every device row is gone while the
  // local management session stays.
  const rowsBeforeAll = env.rows(panel).length;
  assert.ok(rowsBeforeAll > 1, 'there are device rows to kick');
  env.confirm = false;
  const confirmationsBeforeAll = env.confirmations.length;
  await env.button('一键踢出全部').click();
  await env.flush();
  assert.equal(env.confirmations.length, confirmationsBeforeAll + 1, 'kicking everyone asks first');
  assert.ok(env.confirmations.at(-1).includes('记录会从列表里删除'));
  assert.ok(env.confirmations.at(-1).includes('本机管理会话不受影响'));
  assert.equal(env.kickedAll || 0, 0, 'a declined confirmation sends nothing');
  env.confirm = true;
  await env.button('一键踢出全部').click();
  await env.flush();
  assert.equal(env.kickedAll, 1, 'the kick-all endpoint is called once');
  assert.equal(env.rows(panel).length, 1, 'only the local management session remains');
  assert.ok(env.find('qs-success').textContent.includes('已踢出全部'));
  env.status.sessions = STATUS.sessions;
  await env.button('刷新').click();
  await env.flush();

  // Tidying up the records of clients that never used anything.
  env.status.sessions = [
    STATUS.sessions[0],
    { token: 'unused0000unused', kind: 'device', label: '从未使用', created_at: NOW - 30, last_seen: NOW - 20,
      requests: 0, bytes: 0, last_endpoint: '', last_source: '10.0.0.9', blocked: false, current: false },
  ];
  await env.button('刷新').click();
  await env.flush();
  assert.equal(env.rows(panel).length, 2);
  await env.button('清理未使用记录').click();
  await env.flush();
  assert.ok(env.confirmations.at(-1).includes('从未使用'));
  assert.equal(env.rows(panel).length, 1, 'only the unused record is dropped');
  env.status.sessions = STATUS.sessions;
  await env.button('刷新').click();
  await env.flush();

  // PIN rotation: local validation first, then the backend, then the explicit warning.
  assert.ok(env.find('qs-note').textContent.includes('更换口令不会打断已加入的用户，只影响之后新加入的人。'));
  await env.button('更换口令').click();
  const rotateForm = env.find('qs-rotate');
  assert.equal(rotateForm.hidden, false);
  const rotateInput = rotateForm.querySelector('input');
  rotateInput.value = '123456';
  await rotateForm.fire('submit', { preventDefault() {} });
  await env.flush();
  assert.equal(env.find('qs-notice').textContent, '口令格式应为 123456-789012-345678');
  assert.equal(env.find('qs-notice').dataset.state, 'error');
  assert.equal(env.posts().filter(request => request.url === '/local/share/pin').length, 0, 'invalid PINs never reach the backend');

  await env.button('随机生成').click();
  assert.match(rotateInput.value, /^\d{6}-\d{6}-\d{6}$/);
  rotateInput.value = '123456-789012-345678';
  await rotateForm.fire('submit', { preventDefault() {} });
  await env.flush();
  assert.equal(env.pinBody.pin, '123456-789012-345678');
  assert.equal(env.find('qs-notice').dataset.state, 'ok');
  assert.ok(env.find('qs-notice').textContent.includes('已加入的用户不受影响'));
  assert.equal(env.find('qs-pin-value').textContent, '123456-789012-345678');

  // A rejected rotation shows the backend error instead of pretending it worked.
  env.pinCode = 400;
  rotateInput.value = '999999-888888-777777';
  await rotateForm.fire('submit', { preventDefault() {} });
  await env.flush();
  assert.equal(env.find('qs-notice').dataset.state, 'error');
  assert.ok(env.find('qs-notice').textContent.includes('口令已被其他管理员同时修改。'));
  assert.equal(env.find('qs-pin-value').textContent, '123456-789012-345678', 'a rejected PIN is not shown as active');
  env.pinCode = 0;

  // Disconnecting the tunnel always asks for confirmation and explains the blast radius.
  await env.button('一键断联').click();
  await env.flush();
  const disconnectPrompt = env.confirmations.at(-1);
  assert.ok(disconnectPrompt.includes('外部用户将立即无法访问'), 'the confirmation states who is affected');
  assert.ok(disconnectPrompt.includes('本机不受影响'), 'the confirmation protects the local machine');
  await env.flush();
  assert.equal(env.onlineBody.online, false);
  assert.equal(env.find('qs-badge').textContent, '已断联');
  assert.equal(env.find('qs-badge').dataset.online, 'false');
  assert.equal(env.button('恢复连接').disabled, false, 'the button flips to the recovery wording');
  assert.ok(env.find('qs-success').textContent.includes('本机不受影响'));

  // Restoring needs no confirmation and flips back.
  const confirmationsBefore = env.confirmations.length;
  await env.button('恢复连接').click();
  await env.flush();
  assert.equal(env.confirmations.length, confirmationsBefore, 'restoring the tunnel is not destructive');
  assert.equal(env.onlineBody.online, true);
  assert.equal(env.find('qs-badge').textContent, '公网可达');
  assert.equal(env.button('一键断联').disabled, false);

  // The guest clip-upload switch: reported from the backend, closed until the owner
  // opens it, and never flipped without a confirmation.
  assert.equal(env.find('qs-uploads').querySelector('.qs-badge').textContent, '仅本机管理员',
    'a closed switch is reported as closed');
  assert.ok(env.find('qs-uploads').textContent.includes('生成、降噪、拼接、输出目录仍仅限本机管理员'),
    'the section states what stays owner-only');
  env.confirm = false;
  const uploadConfirmsBefore = env.confirmations.length;
  await env.button('开放给访客').click();
  await env.flush();
  assert.equal(env.confirmations.length, uploadConfirmsBefore + 1, 'opening guest uploads asks first');
  assert.ok(env.confirmations.at(-1).includes('只能看到、下载自己上传的片段'));
  assert.equal(env.uploadBody, undefined, 'a declined confirmation sends nothing');
  env.confirm = true;
  await env.button('开放给访客').click();
  await env.flush();
  assert.deepEqual(env.uploadBody, { policy: 'guests' }, 'opening posts the guests policy');
  assert.equal(env.find('qs-uploads').querySelector('.qs-badge').textContent, '已开放给访客');
  assert.equal(env.button('收回访客上传')?.disabled, false, 'the switch flips to the closing action');
  env.uploadCode = 409;
  await env.button('收回访客上传').click();
  await env.flush();
  assert.deepEqual(env.uploadBody, { policy: 'off' }, 'closing posts the off policy');
  assert.equal(env.find('qs-error').hidden, false, 'a refused switch is reported');
  assert.ok(env.find('qs-error').textContent.includes('访客上传设置保存失败'));
  env.uploadCode = 0;
  env.status.guest_uploads = Object.assign({}, env.status.guest_uploads, {
    policy: 'guests', usage: { files: 2, guest_files: 2, guest_bytes: 3145728, guest_total_limit: 21474836480 },
  });
  await env.button('刷新').click();
  await env.flush();
  assert.ok(env.find('qs-uploads').textContent.includes('访客已上传 2 个片段'),
    'usage is reported back to the owner');
  assert.ok(env.find('qs-uploads').textContent.includes('3 MB'), 'usage uses the traffic units');
  env.status.guest_uploads = Object.assign({}, env.status.guest_uploads, { policy: 'off', usage: STATUS.guest_uploads.usage });

  // Empty state.
  env.status.sessions = [];
  await env.button('刷新').click();
  await env.flush();
  assert.equal(panel.panel.querySelector('table').hidden, true);
  assert.equal(env.find('qs-empty').hidden, false);
  assert.equal(env.find('qs-empty').querySelector('h3').textContent, '暂时没有人通过共享地址访问');
  env.status.sessions = STATUS.sessions;

  // Network failure is reported and retryable.
  env.fail = true;
  await env.button('刷新').click();
  await env.flush();
  assert.equal(env.find('qs-error').hidden, false);
  assert.ok(env.find('qs-error').textContent.includes('Connection refused'));
  env.fail = false;
  await env.button('重试').click();
  await env.flush();
  assert.equal(env.find('qs-error').hidden, true);

  // Closing stops polling and cancels the in-flight request.
  await env.button('收起').click();
  await env.flush();
  assert.equal(panel.panel.hidden, true);
  assert.equal(env.polls(), 0, 'closing the panel stops the 10-second poll');

  // 403: the visitor is not the administrator, so the panel hides itself quietly.
  const denied = fixture({ status: { error: { message: '需要管理员权限。' } } });
  denied.statusCode = 403;
  const deniedPanel = await denied.create();
  assert.equal(deniedPanel.hidden, false, 'the panel is usable until the backend answers');
  await deniedPanel.open();
  await denied.flush();
  assert.deepEqual(denied.requests.map(request => request.url), ['/local/share/status']);
  assert.equal(deniedPanel.hidden, true, 'a 403 reports that the panel is not available');
  assert.equal(deniedPanel.entry.hidden, true, 'the floating entry is hidden');
  assert.equal(deniedPanel.panel.querySelector('.qs-error').hidden, true, 'no error panel is rendered for a 403');
  assert.equal(deniedPanel.panel.querySelector('.qs-success').hidden, true);
  assert.equal(denied.polls(), 0, 'a 403 does not schedule retries');
  assert.equal(deniedPanel.panel.hidden, true, 'the dialog closes itself');
  await deniedPanel.entry.click();
  await deniedPanel.open();
  await denied.flush();
  assert.equal(denied.requests.length, 1, 'a denied panel never retries');
  assert.equal(deniedPanel.panel.hidden, true);
  assert.equal(deniedPanel.visible(), false);

  // Guard rails on the plain-object API used by the host page.
  assert.equal(env.QwenShare.attach(env.document.body), null, 'a second attach is ignored while a panel is mounted');
  assert.equal(typeof panel.attach, 'function');
  assert.equal(typeof panel.open, 'function');
  assert.equal(typeof panel.close, 'function');

  // Stopping the service is terminal for the panel, so it runs last: it asks first,
  // then replaces the whole panel with how to start sharing again.
  await panel.open();
  await env.flush();
  const beforeStop = env.confirmations.length;
  await env.button('停止共享并退出').click();
  await env.flush();
  assert.equal(env.confirmations.length, beforeStop + 1, 'stopping asks for confirmation');
  assert.ok(env.confirmations.at(-1).includes('本机服务'), 'the confirmation says the local service ends too');
  assert.equal(env.stopped, 1, 'the stop endpoint is called once');
  assert.ok(panel.panel.textContent.includes('服务已停止'), 'the panel shows a terminal state');
  assert.ok(panel.panel.textContent.includes('Qwen 共享面板'), 'it says how to start sharing again');
  assert.equal(env.polls(), 0, 'a stopped service is not polled any more');

  console.log('PASS: 模块加载、相对时间、流量单位、口令校验、admin/current 踢出限制、踢出与断联/停止二次确认、共享链接与一键复制、换口令提示、错误态、403 静默隐藏、10 秒轮询');
})().catch(error => { console.error(error); process.exitCode = 1; });
