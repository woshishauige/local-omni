const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, 'usage-log.js'), 'utf8');

function fixture() {
  const env = { now: new Date(2026, 8, 7, 15, 30, 12).getTime(), requests: [], timers: new Map(), downloads: [], fail: false };
  let nextTimer = 0;
  class Element {
    constructor(tag) {
      this.tagName = tag.toLowerCase(); this.children = []; this.attributes = new Map(); this.handlers = new Map();
      this.dataset = {}; this.text = ''; this.open = false; this.className = ''; this.style = { values: new Map(), setProperty(key, value) { this.values.set(key, value); } };
      this.classList = { add: (...names) => { this.className = [...new Set([...this.className.split(' '), ...names])].join(' ').trim(); } };
    }
    set innerHTML(_) { throw new Error('HTML injection is forbidden in usage UI'); }
    set textContent(value) { this.text = String(value); this.children = []; }
    get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
    set value(value) { this.inputValue = String(value); }
    get value() { return this.inputValue ?? (this.tagName === 'select' ? this.children[0]?.value ?? '' : ''); }
    get isConnected() { return this === env.document || !!this.parentElement?.isConnected; }
    append(...children) { for (const child of children) { child.remove(); child.parentElement = this; this.children.push(child); } }
    remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this); this.parentElement = null; }
    replaceChildren(...children) { for (const child of this.children) child.parentElement = null; this.children = []; this.text = ''; this.append(...children); }
    setAttribute(key, value) { this.attributes.set(key, String(value)); }
    getAttribute(key) { return this.attributes.get(key); }
    addEventListener(name, callback) { const list = this.handlers.get(name) || []; list.push(callback); this.handlers.set(name, list); }
    async fire(name, event = {}) { for (const callback of this.handlers.get(name) || []) await callback(event); }
    focus() { env.document.activeElement = this; }
    click() { if (this.tagName === 'a') env.downloads.push({ href: this.href, filename: this.download }); return this.fire('click'); }
    showModal() { this.open = true; }
    close() { this.open = false; void this.fire('close'); }
    reportValidity() { return true; }
    getBoundingClientRect() { return { right: this.right || 56 }; }
    matches(selector) {
      if (selector.startsWith('.')) return (this.className || this.getAttribute('class') || '').split(' ').includes(selector.slice(1));
      if (selector.startsWith('#')) return this.id === selector.slice(1);
      const attribute = /^\[([^=]+)="([^"]+)"\]$/.exec(selector);
      if (attribute) return this.getAttribute(attribute[1]) === attribute[2];
      return this.tagName === selector;
    }
    all() { return this.children.flatMap(child => [child, ...child.all()]); }
    querySelectorAll(selector) { return this.all().filter(child => selector.split(',').some(part => child.matches(part.trim()))); }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  }
  const document = new Element('document');
  env.document = document;
  document.body = new Element('body'); document.append(document.body);
  document.createElement = tag => new Element(tag);
  document.createElementNS = (_, tag) => new Element(tag);
  document.getElementById = id => document.all().find(child => child.id === id) || null;
  const nativeQuery = document.querySelector.bind(document);
  document.querySelector = selector => selector === 'body > div[style="display: contents"] > .flex > aside' ? env.sidebar : nativeQuery(selector);
  env.sidebar = new Element('aside'); document.body.append(env.sidebar);
  env.composer = new Element('textarea'); env.composer.value = '保留未发送草稿'; document.body.append(env.composer);
  const window = new Element('window'); window.innerWidth = 1440;
  class TaskDate extends Date { constructor(...args) { super(...(args.length ? args : [env.now])); } static now() { return env.now; } }
  env.payload = {
    rows: [{ id: 1, model: '<img src=x onerror=alert(1)>', source: 'Web', started_at: env.now / 1000 - 20, finished_at: env.now / 1000 - 5,
      mode: 'text', stream: false, agent: true, endpoint: '/v1/chat/completions', status: 'success', http_status: 200,
      input_tokens: 12, output_tokens: 8, cache_read_tokens: 0, cache_write_tokens: null, ttft_ms: null, duration_ms: 15000,
      tokens_per_second: 2, cost: 0.000008 }],
    total: 51, summary: { requests: 51, success: 51, error: 0, cancelled: 0, incomplete: 0, running: 0, input_tokens: 12,
      output_tokens: 8, total_tokens: 20, cache_read_tokens: 0, cache_write_tokens: null, unknown_usage_requests: 0, cost: 0.000008,
      rpm: 0.15, tpm: 1.2, duration_avg_ms: 15000, ttft_avg_ms: null },
    series: [{ start: new Date(2026, 8, 7, 10).getTime() / 1000, model: 'Qwen', requests: 1, total_tokens: 20, cost: 0.000008 },
      { start: new Date(2026, 8, 7, 12).getTime() / 1000, model: 'Qwen', requests: 1, total_tokens: 30, cost: 0.00001 }], models: ['Qwen'],
  };
  const context = {
    document, window, Intl, Date: TaskDate, URLSearchParams, AbortController, AbortSignal,
    URL: { createObjectURL: () => 'blob:test-export', revokeObjectURL() {} },
    ResizeObserver: class { observe() {} disconnect() {} },
    MutationObserver: class { constructor(callback) { env.mutate = callback; } observe() {} },
    requestAnimationFrame: callback => callback(),
    setTimeout(callback, delay) { const id = ++nextTimer; env.timers.set(id, { callback, delay }); return id; },
    clearTimeout: id => env.timers.delete(id),
    async fetch(url, options = {}) {
      env.requests.push({ url, options });
      if (env.fail) throw new Error('Connection refused');
      if (url === '/local/usage/preferences') return { ok: true, json: async () => ({ input_per_million: 0, output_per_million: 0, cache_read_per_million: 0, cache_write_per_million: 0, currency: 'CNY' }) };
      if (url.startsWith('/local/usage/export?')) return { ok: true, blob: async () => ({ type: 'text/csv' }) };
      if (url.startsWith('/local/routing/export?')) return { ok: true, blob: async () => ({ type: 'text/csv' }) };
      if (url.startsWith('/local/routing?')) {
        const p = new URL(url, 'http://localhost').searchParams;
        assert.equal(p.has('mode'), false, 'usage-only filters must not reach routing API');
        return { ok: true, json: async () => ({ rows: [{ id: 'route-test', decided_at: env.now / 1000,
          tier: 'coding', model: 'gpt-5.6-sol', effort: 'high', classifier_mode: 'qwen', qwen_tier: 'coding', qwen_effort: 'high',
          classifier_confidence: 'high', conclusion: '需要跨组件代码修改和验证，使用 Sol / high。<img src=x onerror=alert(1)>',
          classifier_kind: 'coding', gpu_free_mib: 2048, classifier_ms: 1234,
          cost_currency: 'CNY', cost_group: 'default', cost_group_multiplier: 1, cost_actual: 0.42, cost_baseline: 2.10,
          cost_saved: 1.68, cost_actual_base: 0.42, cost_baseline_base: 2.10, cost_input_tokens: 1200, cost_cached_tokens: 400,
          cost_cache_write_tokens: 150,
          cost_output_tokens: 800, cost_requests: 3, cost_estimated: true, cost_source: 'config', cost_note: '缓存单价按输入 1/10 估算' },
          { id: 'route-upgraded', decided_at: env.now / 1000 - 60, tier: 'max', model: 'gpt-6-astra', effort: 'max',
            classifier_mode: 'rules_fallback', conclusion: '升档到最高档。', cost_currency: 'CNY', cost_actual: 0.72,
            cost_baseline: 0.42, cost_saved: -0.30 },
          { id: 'route-legacy', decided_at: env.now / 1000 - 120, tier: 'cheap', model: 'gpt-5.6-luna', effort: 'low',
            classifier_mode: 'rules_guard', conclusion: '历史记录未计费。', cost_actual: null }], total: 26,
          page: Number(p.get('page')), page_size: 25, summary: { routes: 26, qwen_routes: 26, coding_routes: 26, fallback_routes: 0,
          cost_routes: 12, cost_actual_total: 5.04, cost_baseline_total: 25.20, cost_saved_total: 20.16, cost_currency: 'CNY' } }) };
      }
      if (!url.startsWith('/local/usage?')) throw new Error('Unexpected URL ' + url);
      const params = new URL(url, 'http://localhost').searchParams;
      assert.ok(['all', 'text', 'vision'].includes(params.get('mode')), 'API mode default must be accepted by the backend');
      if (env.hold) return new Promise((resolve, reject) => {
        env.finish = resolve;
        options.signal.addEventListener('abort', () => reject(Object.assign(new Error('Aborted'), { name: 'AbortError' })), { once: true });
      });
      return { ok: true, json: async () => ({ ...env.payload, page: Number(params.get('page')), page_size: Number(params.get('page_size')),
        range: { start: Number(params.get('start')), end: Number(params.get('end')) } }) };
    },
  };
  vm.runInNewContext(source, context);
  env.flush = async () => { for (let index = 0; index < 15; index++) await Promise.resolve(); };
  env.find = className => document.querySelector('.' + className);
  env.button = text => document.querySelectorAll('button').find(button => button.textContent === text);
  env.input = key => env.find('qu-filter-' + key).querySelector('input') || env.find('qu-filter-' + key).querySelector('select');
  env.lastQuery = () => new URL([...env.requests].reverse().find(request => request.url.startsWith('/local/usage?')).url, 'http://localhost').searchParams;
  env.poll = async () => {
    const timer = [...env.timers].find(([, value]) => value.delay === 10000);
    assert.ok(timer, 'an open panel schedules one 10-second poll');
    env.timers.delete(timer[0]); await timer[1].callback(); await env.flush();
  };
  env.Element = Element;
  return env;
}

(async () => {
  const env = fixture();
  assert.equal(env.requests.length, 0, 'closed dashboard must not poll');
  assert.equal(env.sidebar.children.at(-1).className, 'qu-sidebar-footer', 'entry belongs at bottom of native sidebar');
  await env.button('使用日志').click(); await env.flush();
  assert.equal(env.composer.value, '保留未发送草稿');
  const query = env.lastQuery();
  assert.equal(query.get('mode'), 'all');
  assert.equal(Number(query.get('end')), env.now / 1000, 'rolling end uses current second, not future midnight');
  assert.equal(Number(query.get('start')), new Date(2026, 8, 7).getTime() / 1000);
  assert.equal(Number(query.get('timezone_offset_minutes')), -new Date().getTimezoneOffset());
  assert.equal(env.input('start').type, 'datetime-local');
  assert.ok(env.find('qu-table').textContent.includes('<img src=x onerror=alert(1)>'), 'model name is rendered as literal text');
  assert.ok(env.find('qu-table').textContent.includes('— 写入'), 'unreported cache-write metrics stay unknown');
  assert.equal(env.document.querySelectorAll('img').length, 0);
  assert.equal([...env.timers.values()].filter(timer => timer.delay === 10000).length, 1);

  env.now += 120000; await env.poll();
  assert.equal(Number(env.lastQuery().get('end')), env.now / 1000, 'automatic refresh follows current time');
  env.input('start').value = '2026-09-07T09:15';
  env.input('end').value = '2026-09-07T11:45';
  await env.input('end').fire('change'); await env.flush();
  assert.equal(env.find('qu-follow-now').querySelector('input').checked, false);
  assert.equal(Number(env.lastQuery().get('start')), new Date('2026-09-07T09:15').getTime() / 1000);
  const fixedEnd = Number(env.lastQuery().get('end'));
  env.now += 120000; await env.poll();
  assert.equal(Number(env.lastQuery().get('end')), fixedEnd, 'manual end remains fixed');
  await env.button('重置筛选').click(); await env.flush();
  assert.equal(env.find('qu-follow-now').querySelector('input').checked, true);

  await env.button('调用分析').click();
  assert.equal(env.document.getElementById('qu-panel-analytics').hidden, false);
  const metric = env.document.querySelector('[aria-label="图表统计指标"]');
  metric.value = 'cost'; await metric.fire('change');
  assert.ok(env.find('qu-charts').textContent.includes('估算费用趋势'));
  assert.ok(env.find('qu-charts').textContent.includes('¥0.000008'), 'small nonzero estimated costs are not rounded to zero');
  const beforeToggle = env.requests.length;
  await env.button('面积图').click();
  assert.equal(env.requests.length, beforeToggle, 'chart presentation changes need no backend request');
  assert.equal(env.button('面积图').getAttribute('aria-pressed'), 'true');
  assert.ok(env.find('qu-chart-svg').querySelector('path'));
  assert.ok(!env.find('qu-chart-svg').textContent.includes('NaN'));

  await env.button('请求明细').click();
  await env.button('下一页').click(); await env.flush();
  assert.equal(env.lastQuery().get('page'), '2');
  env.input('mode').value = 'vision'; await env.input('mode').fire('change'); await env.flush();
  assert.equal(env.lastQuery().get('page'), '1');
  assert.equal(env.lastQuery().get('mode'), 'vision');
  await env.button('详情').click();
  const detail = env.find('qu-secondary');
  assert.ok(detail.textContent.includes('首 Token 延迟—'));
  assert.ok(detail.textContent.includes('缓存写入 Token—'));
  detail.close();
  await env.button('费用设置').click(); await env.flush();
  const preferences = env.find('qu-secondary');
  assert.ok(preferences.textContent.includes('修改仅影响新请求'));
  const inputs = preferences.querySelectorAll('input');
  inputs[0].value = '1.25';
  await preferences.querySelector('form').fire('submit', { preventDefault() {} }); await env.flush();
  const priceRequest = env.requests.find(request => request.options.method === 'POST');
  assert.equal(JSON.parse(priceRequest.options.body).input_per_million, 1.25);
  assert.equal(JSON.parse(priceRequest.options.body).currency, 'CNY');

  await env.button('导出 CSV').click(); await env.flush();
  assert.equal(env.downloads.length, 1);
  const exportQuery = new URL(env.requests.find(request => request.url.startsWith('/local/usage/export?')).url, 'http://localhost').searchParams;
  assert.equal(exportQuery.get('mode'), 'vision');
  assert.equal(exportQuery.has('page'), false, 'CSV exports matching records across pages');

  env.fail = true; await env.button('刷新').click(); await env.flush();
  assert.equal(env.find('qu-error').hidden, false);
  assert.ok(env.find('qu-freshness').textContent.includes('更新失败'));
  env.fail = false; await env.button('重试').click(); await env.flush();
  assert.equal(env.find('qu-error').hidden, true);

  const priorSidebar = env.sidebar;
  await env.button('路由记录').click(); await env.flush();
  assert.equal(env.document.getElementById('qu-panel-routing').hidden, false);
  assert.equal(env.input('mode').parentElement.hidden, true);
  assert.equal(env.input('tier').parentElement.hidden, false);
  assert.ok(env.find('qu-route-conclusion').textContent.includes('跨组件代码修改'));
  assert.equal(env.document.querySelectorAll('img').length, 0, 'rationale is rendered as text, never HTML');
  const routePanel = env.document.getElementById('qu-panel-routing');
  const routeStrip = routePanel.querySelector('.qu-summary-strip').textContent;
  assert.ok(routeStrip.includes('累计节省 ¥20.16 CNY'), 'routing strip reports cumulative savings with the currency');
  assert.ok(routeStrip.includes('实际 ¥5.04 / 基准 ¥25.20'), 'routing strip reports actual and baseline totals');
  assert.ok(routeStrip.includes('12 条已计费'), 'routing strip reports how many decisions were priced');
  const routeRows = routePanel.querySelector('table').textContent;
  assert.ok(routeRows.includes('节省 ¥1.68（实际 ¥0.42 / 基准 ¥2.10）'), 'priced rows show the saving against the baseline');
  assert.ok(routeRows.includes('+¥0.30（升档）'), 'negative savings are shown as an upgrade premium');
  assert.ok(routeRows.includes('未计费'), 'rows without a cost are marked as unpriced');
  env.input('reason').value = '跨组件'; await env.input('reason').fire('change'); await env.flush();
  assert.ok(env.requests.at(-1).url.includes('reason='));
  await env.document.getElementById('qu-panel-routing').querySelector('button').click();
  const routeDetail = env.find('qu-secondary');
  assert.ok(routeDetail.textContent.includes('跨组件代码修改'));
  assert.ok(routeDetail.textContent.includes('计费分组default ×1'));
  assert.ok(routeDetail.textContent.includes('实际费用¥0.42 CNY（估算）'));
  assert.ok(routeDetail.textContent.includes('基准费用¥2.10 CNY（估算）'));
  assert.ok(routeDetail.textContent.includes('节省¥1.68 CNY（估算）'));
  assert.ok(routeDetail.textContent.includes('计费 Token输入 1,200 / 缓存读 400 / 缓存写 150 / 输出 800，共 3 次请求'));
  assert.ok(routeDetail.textContent.includes('计费来源config'));
  assert.ok(routeDetail.textContent.includes('计费说明缓存单价按输入 1/10 估算'));
  routeDetail.close();
  const routeButtons = routePanel.querySelectorAll('button');
  await routeButtons[2].click();
  const unpricedDetail = env.find('qu-secondary');
  assert.ok(unpricedDetail.textContent.includes('计费分组—'), 'missing billing group renders as a dash');
  assert.ok(unpricedDetail.textContent.includes('实际费用—'), 'missing cost renders as a dash');
  assert.ok(unpricedDetail.textContent.includes('计费 Token—'), 'missing billed tokens render as a dash');
  unpricedDetail.close();
  await env.button('导出 CSV').click(); await env.flush();
  assert.ok(env.downloads.at(-1).filename.includes('路由记录'));
  assert.ok(env.requests.at(-1).url.startsWith('/local/routing/export?'));
  assert.equal(new URL(env.requests.at(-1).url, 'http://localhost').searchParams.has('page'), false);
  await env.poll();
  assert.ok(env.requests.at(-1).url.startsWith('/local/routing?'));
  await env.button('请求明细').click(); await env.flush();
  assert.equal(env.input('mode').parentElement.hidden, false);
  assert.ok(env.requests.at(-1).url.startsWith('/local/usage?'));
  env.sidebar = new env.Element('aside'); priorSidebar.remove(); env.document.body.append(env.sidebar);
  env.mutate();
  assert.equal(env.sidebar.children.at(-1).className, 'qu-sidebar-footer', 'entry reattaches when native sidebar is recreated');
  env.hold = true;
  const pending = env.button('刷新').click(); await env.flush();
  await env.button('返回对话').click(); await pending; await env.flush();
  assert.equal(env.document.getElementById('qwen-usage-dialog').open, false);
  assert.equal([...env.timers.values()].filter(timer => timer.delay === 10000).length, 0, 'closing stops polling and aborts current request');
  assert.equal(env.composer.value, '保留未发送草稿');
  assert.equal(env.requests.some(request => request.url.includes('chat/completions')), false);
  console.log('PASS: sidebar, draft preservation, accurate rolling/custom dates, API filters, XSS-safe rows, unknown metrics, charts/costs, pagination, details, preferences, CSV, reconnect, close/abort');
})().catch(error => { console.error(error); process.exitCode = 1; });
