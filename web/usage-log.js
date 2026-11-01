(() => {
  'use strict';
  if (document.getElementById('qwen-usage-dialog')) return;

  const NS = 'http://www.w3.org/2000/svg';
  const colors = ['#279772', '#6584db', '#ba80d0', '#de9b44', '#42a8bd', '#d66f8b', '#79983e', '#927669'];
  const statusNames = { success: '成功', error: '失败', cancelled: '已取消', incomplete: '未完成', running: '进行中' };
  const modeNames = { text: '纯文本', vision: '识图' };
  const tierNames = { cheap: '廉价 · Luna', balanced: '均衡 · Terra', coding: '代码 · Sol', complex: '复杂 · GPT-6', max: '最高 · GPT-6', original: '保留原模型' };
  const classifierNames = { qwen: 'Qwen 判断', rules_fallback: '规则回退', rules_guard: '规则保护', manual: '手动指定', pinned: '上下文绑定' };
  const numberFormat = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 });
  let page = 1;
  let activeTab = 'requests';
  let data = null;
  let routeData = null;
  let pollTimer;
  let debounceTimer;
  let controller;
  let serial = 0;
  let lastFocus;
  let sidebar;
  let exporting = false;
  let lastUpdated = null;
  let areaChart = false;

  function node(tag, className, text) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined) result.textContent = text;
    return result;
  }
  function svg(tag, attrs = {}, text) {
    const result = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(attrs)) result.setAttribute(key, String(value));
    if (text !== undefined) result.textContent = text;
    return result;
  }
  function icon(name) {
    const paths = {
      chart: ['M4 3v17h17', 'M8 15v-4', 'M13 15V7', 'M18 15v-7'],
      close: ['M6 6l12 12', 'M18 6L6 18'],
      refresh: ['M20 7v5h-5', 'M19 12a7 7 0 1 1-2-5l3 3'],
      download: ['M12 3v12', 'M7 10l5 5 5-5', 'M5 16v4h14v-4'],
      settings: ['M4 7h16', 'M4 17h16', 'M8 4v6', 'M16 14v6'],
    };
    const root = svg('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 1.7, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': true });
    for (const d of paths[name]) root.append(svg('path', { d }));
    return root;
  }
  function button(text, action, className = 'qu-button', iconName) {
    const result = node('button', className);
    result.type = 'button';
    if (iconName) result.append(icon(iconName));
    if (text) result.append(node('span', '', text));
    result.addEventListener('click', action);
    return result;
  }
  function numeric(value) { return typeof value === 'number' && Number.isFinite(value); }
  function number(value) { return numeric(value) ? numberFormat.format(value) : '—'; }
  function money(value) {
    return numeric(value) ? '¥' + value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: value > 0 && value < 0.01 ? 6 : 4 }) : '—';
  }
  function duration(value) { return numeric(value) ? value < 1000 ? Math.round(value) + ' ms' : number(value / 1000) + ' s' : '—'; }
  function dateTime(epoch) {
    if (!numeric(epoch)) return '—';
    const date = new Date(epoch * 1000);
    return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { hour12: false });
  }
  function localDate(date) {
    return [date.getFullYear(), String(date.getMonth() + 1).padStart(2, '0'), String(date.getDate()).padStart(2, '0')].join('-');
  }
  function localDateTime(date) { return localDate(date) + 'T' + String(date.getHours()).padStart(2, '0') + ':' + String(date.getMinutes()).padStart(2, '0'); }

  const footer = node('div', 'qu-sidebar-footer');
  const entry = button('使用日志', open, 'qu-sidebar-button', 'chart');
  entry.setAttribute('aria-label', '使用日志');
  entry.setAttribute('aria-haspopup', 'dialog');
  entry.setAttribute('aria-controls', 'qwen-usage-dialog');
  entry.title = '使用日志';
  footer.append(entry);

  const dialog = node('dialog', 'qu-dashboard');
  dialog.id = 'qwen-usage-dialog';
  dialog.setAttribute('aria-labelledby', 'qu-title');
  const frame = node('div', 'qu-frame');
  const header = node('header', 'qu-header');
  const titleBlock = node('div', 'qu-title-block');
  const eyebrow = node('div', 'qu-eyebrow', 'QWEN LOCAL · USAGE');
  const title = node('h1', '', '使用日志');
  title.id = 'qu-title';
  titleBlock.append(eyebrow, title, node('p', 'qu-subtitle', '查看本地模型的请求、Token 消耗与运行表现'));
  const actions = node('div', 'qu-header-actions');
  const preferencesButton = button('费用设置', preferences, 'qu-button', 'settings');
  const refreshButton = button('刷新', () => load(), 'qu-button', 'refresh');
  const exportButton = button('导出 CSV', exportCsv, 'qu-button', 'download');
  const closeButton = button('返回对话', () => dialog.close(), 'qu-button qu-button-primary', 'close');
  actions.append(preferencesButton, refreshButton, exportButton, closeButton);
  header.append(titleBlock, actions);
  const tabs = node('div', 'qu-tabs');
  tabs.setAttribute('role', 'tablist');
  tabs.setAttribute('aria-label', '使用日志视图');
  const tabButtons = {};
  const tabKeys = ['requests', 'analytics', 'routing'];
  for (const [key, label] of [['requests', '请求明细'], ['analytics', '调用分析'], ['routing', '路由记录']]) {
    const tab = button(label, () => selectTab(key), 'qu-tab');
    tab.setAttribute('role', 'tab');
    tab.id = 'qu-tab-' + key;
    tab.setAttribute('aria-controls', 'qu-panel-' + key);
    tab.addEventListener('keydown', event => {
      if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
        event.preventDefault();
        const next = event.key === 'Home' ? tabKeys[0] : event.key === 'End' ? tabKeys[2] : tabKeys[(tabKeys.indexOf(key) + (event.key === 'ArrowLeft' ? 2 : 1)) % 3];
        selectTab(next);
        tabButtons[next].focus();
      }
    });
    tabs.append(tab);
    tabButtons[key] = tab;
  }
  const scroll = node('div', 'qu-scroll');
  const filterBox = node('section', 'qu-filters');
  filterBox.setAttribute('aria-label', '日志筛选');
  const filters = {};
  function filter(label, key, type, options) {
    const wrap = node('label', 'qu-filter qu-filter-' + key);
    wrap.append(node('span', 'qu-filter-label', label));
    const input = node(options ? 'select' : 'input', 'qu-input');
    if (options) for (const [value, text] of options) {
      const option = node('option', '', text);
      option.value = value;
      input.append(option);
    }
    else input.type = type;
    input.addEventListener(key === 'model' ? 'input' : 'change', () => {
      clearTimeout(debounceTimer);
      page = 1;
      if (key === 'end') followNow.checked = false;
      if (key === 'model') debounceTimer = setTimeout(load, 350);
      else void load();
    });
    wrap.append(input);
    filters[key] = input;
    filterBox.append(wrap);
    return input;
  }
  filter('开始时间', 'start', 'datetime-local');
  filter('结束时间', 'end', 'datetime-local');
  const quick = node('div', 'qu-quick');
  quick.setAttribute('aria-label', '快捷时间范围');
  for (const [days, text] of [[1, '今天'], [7, '近 7 天'], [30, '近 30 天']]) quick.append(button(text, () => { setRange(days); page = 1; void load(); }, 'qu-chip-button'));
  const followLabel = node('label', 'qu-follow-now');
  const followNow = node('input');
  followNow.type = 'checkbox'; followNow.checked = true;
  followNow.addEventListener('change', () => { page = 1; void load(); });
  followLabel.append(followNow, node('span', '', '结束时间跟随当前'));
  quick.append(followLabel);
  filterBox.append(quick);
  filter('模型', 'model', 'search').placeholder = '全部模型 / 搜索';
  filters.model.setAttribute('list', 'qu-models');
  const models = node('datalist');
  models.id = 'qu-models';
  filterBox.append(models);
  filter('分组', 'mode', null, [['all', '全部分组'], ['text', '纯文本'], ['vision', '识图']]);
  filter('状态', 'status', null, [['all', '全部状态'], ...Object.entries(statusNames)]);
  filter('类型', 'stream', null, [['all', '全部请求'], ['true', '流式'], ['false', '非流式']]);
  filter('路由档位', 'tier', null, [['', '全部档位'], ...Object.entries(tierNames)]);
  filter('判断来源', 'classifier_mode', null, [['', '全部来源'], ...Object.entries(classifierNames)]);
  filter('结论 / 原因', 'reason', 'search').placeholder = '搜索具体结论或原因';
  filterBox.append(button('重置筛选', () => {
    filters.model.value = ''; filters.mode.value = 'all'; filters.status.value = 'all'; filters.stream.value = 'all';
    filters.tier.value = ''; filters.classifier_mode.value = ''; filters.reason.value = '';
    setRange(1); page = 1; void load();
  }, 'qu-button qu-reset'));

  const error = node('div', 'qu-error');
  error.setAttribute('role', 'alert');
  error.hidden = true;
  const errorText = node('span');
  error.append(errorText, button('重试', () => load(), 'qu-button qu-button-small'));
  const freshness = node('div', 'qu-freshness', '打开面板时每 10 秒更新 · 记录从本功能启用后开始累计');
  freshness.setAttribute('role', 'status');
  const panels = {};
  for (const key of tabKeys) {
    const panel = node('section', 'qu-panel');
    panel.id = 'qu-panel-' + key;
    panel.setAttribute('role', 'tabpanel');
    panel.setAttribute('aria-labelledby', 'qu-tab-' + key);
    panels[key] = panel;
  }

  const summaryStrip = node('div', 'qu-summary-strip');
  const tableWrap = node('div', 'qu-table-wrap');
  const table = node('table', 'qu-table');
  const caption = node('caption', 'qu-sr-only', '模型请求明细');
  const thead = node('thead');
  const headRow = node('tr');
  for (const label of ['时间', '模型 / 来源', '分组', '状态 / 类型', '输入 / 输出', '缓存读 / 写', '估算费用', '首 Token / 总耗时', '生成速度', '']) {
    const th = node('th', '', label || '详情');
    th.scope = 'col';
    headRow.append(th);
  }
  thead.append(headRow);
  const tbody = node('tbody');
  table.append(caption, thead, tbody);
  const empty = emptyState('正在读取使用记录', '记录从启用本功能后开始累计，历史数据不会补造。');
  table.hidden = true;
  tableWrap.append(table, empty);
  const pagination = node('div', 'qu-pagination');
  const pageLabel = node('span', 'qu-muted');
  const pageControls = node('div', 'qu-page-controls');
  const previous = button('上一页', () => { page = Math.max(1, page - 1); void load(); });
  const next = button('下一页', () => { page++; void load(); });
  const pageSize = node('select', 'qu-input');
  pageSize.setAttribute('aria-label', '每页记录数');
  for (const size of [25, 50, 100]) { const option = node('option', '', size + ' 条 / 页'); option.value = String(size); pageSize.append(option); }
  pageSize.addEventListener('change', () => { page = 1; void load(); });
  pageControls.append(pageSize, previous, next);
  pagination.append(pageLabel, pageControls);
  panels.requests.append(summaryStrip, tableWrap, pagination);

  const kpis = node('div', 'qu-kpis');
  const cacheSummary = node('div', 'qu-cache-summary');
  const chartHeader = node('div', 'qu-chart-controls');
  chartHeader.append(node('p', 'qu-muted', '按模型分组；趋势仅累计已知用量，未报告的指标显示为 —。'));
  const chartOptions = node('div', 'qu-chart-options');
  const metricLabel = node('label', 'qu-inline-label', '统计指标');
  const metric = node('select', 'qu-input');
  metric.setAttribute('aria-label', '图表统计指标');
  for (const [value, text] of [['total_tokens', 'Token'], ['cost', '估算费用'], ['requests', '请求数']]) { const option = node('option', '', text); option.value = value; metric.append(option); }
  metric.addEventListener('change', renderChart);
  metricLabel.append(metric);
  const chartKinds = node('div', 'qu-chart-kinds');
  chartKinds.setAttribute('aria-label', '图表类型');
  const barsButton = button('柱状图', () => { areaChart = false; renderChart(); }, 'qu-chip-button');
  const areaButton = button('面积图', () => { areaChart = true; renderChart(); }, 'qu-chip-button');
  chartKinds.append(barsButton, areaButton);
  const bucketLabel = node('label', 'qu-inline-label', '时间粒度');
  const bucket = node('select', 'qu-input');
  bucket.setAttribute('aria-label', '图表时间粒度');
  for (const [value, text] of [['hour', '按小时'], ['day', '按天']]) { const option = node('option', '', text); option.value = value; bucket.append(option); }
  bucket.addEventListener('change', () => load());
  bucketLabel.append(bucket);
  chartOptions.append(metricLabel, bucketLabel, chartKinds);
  chartHeader.append(chartOptions);
  const charts = node('div', 'qu-charts');
  panels.analytics.append(kpis, cacheSummary, chartHeader, charts);
  const routeSummary = node('div', 'qu-summary-strip');
  const routeWrap = node('div', 'qu-table-wrap');
  const routeTable = node('table', 'qu-table qu-route-table');
  const routeHead = node('thead');
  const routeHeadRow = node('tr');
  for (const label of ['决策时间', '实际模型 / 思考强度', '判断来源 / Qwen 建议', '具体结论与分流原因', '资源 / 耗时', '详情']) {
    const th = node('th', '', label); th.scope = 'col'; routeHeadRow.append(th);
  }
  routeHead.append(routeHeadRow);
  const routeBody = node('tbody');
  routeTable.append(node('caption', 'qu-sr-only', '路由判断记录及具体分流原因'), routeHead, routeBody);
  const routeEmpty = emptyState('尚无路由记录', '新决策会自动记录；Qwen 服务离线期间的记录会在恢复后补交。');
  routeWrap.append(routeTable, routeEmpty);
  const routePagination = node('div', 'qu-pagination');
  const routePageLabel = node('span', 'qu-muted');
  const routeControls = node('div', 'qu-page-controls');
  const routePrevious = button('上一页', () => { page = Math.max(1, page - 1); void load(); });
  const routeNext = button('下一页', () => { page++; void load(); });
  routeControls.append(routePrevious, routeNext);
  routePagination.append(routePageLabel, routeControls);
  panels.routing.append(routeSummary, routeWrap, routePagination,
    node('p', 'qu-note', '每次新路由判断记录一条；同一轮工具调用复用原决策，不重复计数。保留最近 50,000 条，可导出 CSV。Qwen 建议与实际模型可能因上下文绑定而不同，原因会明确说明。'));
  scroll.append(filterBox, error, freshness, panels.requests, panels.analytics, panels.routing);
  frame.append(header, tabs, scroll);
  dialog.append(frame);
  document.body.append(dialog);

  function emptyState(heading, text) {
    const result = node('div', 'qu-empty');
    result.append(icon('chart'), node('h3', '', heading), node('p', '', text));
    return result;
  }
  function setRange(days) {
    const today = new Date();
    const start = new Date(today.getFullYear(), today.getMonth(), today.getDate() - days + 1);
    filters.start.value = localDateTime(start);
    filters.end.value = localDateTime(today);
    followNow.checked = true;
    bucket.value = days > 2 ? 'day' : 'hour';
  }
  function parameters(paginated = true) {
    const start = new Date(filters.start.value);
    const end = followNow.checked ? new Date() : new Date(filters.end.value);
    if (followNow.checked) filters.end.value = localDateTime(end);
    if (!Number.isFinite(start.getTime()) || !Number.isFinite(end.getTime()) || end <= start) throw new Error('请选择有效的起止时间，结束时间必须晚于开始时间。');
    if (activeTab === 'routing') {
      const routeParams = new URLSearchParams({ start: String(start.getTime() / 1000), end: String(end.getTime() / 1000), model: filters.model.value.trim(), tier: filters.tier.value, classifier_mode: filters.classifier_mode.value, reason: filters.reason.value.trim() });
      if (paginated) { routeParams.set('page', String(page)); routeParams.set('page_size', '25'); }
      return routeParams;
    }
    const result = new URLSearchParams({ start: String(start.getTime() / 1000), end: String(end.getTime() / 1000), model: filters.model.value.trim(), mode: filters.mode.value, status: filters.status.value, stream: filters.stream.value, bucket: bucket.value, timezone_offset_minutes: String(-new Date().getTimezoneOffset()) });
    if (paginated) { result.set('page', String(page)); result.set('page_size', pageSize.value || '25'); }
    return result;
  }
  function showError(message) { errorText.textContent = message; error.hidden = false; }
  async function responseJson(response) {
    if (!response.ok) throw new Error('本地日志服务暂时不可用（HTTP ' + response.status + '），连接恢复后可重试。');
    return response.json();
  }
  async function load() {
    if (!dialog.open) return;
    clearTimeout(pollTimer);
    controller?.abort();
    const request = ++serial;
    const requestController = new AbortController();
    controller = requestController;
    const timeout = setTimeout(() => requestController.abort(), 12000);
    refreshButton.disabled = true;
    dialog.setAttribute('aria-busy', 'true');
    try {
      const routing = activeTab === 'routing';
      const query = parameters();
      const response = await fetch((routing ? '/local/routing?' : '/local/usage?') + query, { cache: 'no-store', signal: requestController.signal });
      const result = await responseJson(response);
      if (request !== serial || !dialog.open) return;
      if (!Array.isArray(result.rows) || !result.summary || (!routing && !Array.isArray(result.series))) throw new Error('本地日志服务返回的数据格式不正确，请刷新重试。');
      const lastPage = Math.max(1, Math.ceil((result.total || 0) / (result.page_size || 25)));
      if (page > lastPage) { page = lastPage; void load(); return; }
      if (routing) routeData = result; else data = result;
      page = numeric(result.page) ? result.page : page;
      lastUpdated = new Date();
      error.hidden = true;
      if (routing) renderRoutes(); else render();
    } catch (failure) {
      if (request !== serial || !dialog.open) return;
      showError(failure.name === 'AbortError' ? '日志请求超时，稍后会自动重试。' : failure.message || '无法连接本地日志服务。');
      if (!data) { empty.querySelector('h3').textContent = '尚未读取到日志数据'; empty.querySelector('p').textContent = '日志服务恢复后会自动重试，当前对话与模型生成继续运行。'; }
      freshness.textContent = lastUpdated ? '更新失败 · 当前显示 ' + lastUpdated.toLocaleTimeString('zh-CN', { hour12: false }) + ' 的记录' : '尚未读取到日志数据 · 不影响对话与模型生成';
    } finally {
      clearTimeout(timeout);
      if (request === serial) {
        refreshButton.disabled = false;
        dialog.setAttribute('aria-busy', 'false');
        if (dialog.open) pollTimer = setTimeout(() => document.hidden ? schedule() : load(), 10000);
      }
    }
  }
  function schedule() { clearTimeout(pollTimer); if (dialog.open) pollTimer = setTimeout(() => document.hidden ? schedule() : load(), 10000); }
  function selectTab(key) {
    const wasRouting = activeTab === 'routing';
    activeTab = key;
    for (const [name, tab] of Object.entries(tabButtons)) {
      tab.setAttribute('aria-selected', String(name === key));
      tab.tabIndex = name === key ? 0 : -1;
      panels[name].hidden = name !== key;
    }
    for (const name of ['mode', 'status', 'stream']) filters[name].parentElement.hidden = key === 'routing';
    for (const name of ['tier', 'classifier_mode', 'reason']) filters[name].parentElement.hidden = key !== 'routing';
    preferencesButton.hidden = key === 'routing';
    if (wasRouting !== (key === 'routing')) { page = 1; error.hidden = true; lastUpdated = null; void load(); }
  }
  function open() {
    if (dialog.open) return;
    lastFocus = document.activeElement;
    position();
    dialog.showModal();
    entry.setAttribute('aria-expanded', 'true');
    closeButton.focus();
    void load();
  }
  dialog.addEventListener('close', () => {
    serial++;
    controller?.abort();
    clearTimeout(pollTimer);
    clearTimeout(debounceTimer);
    entry.setAttribute('aria-expanded', 'false');
    if (lastFocus?.isConnected) lastFocus.focus({ preventScroll: true });
  });
  dialog.addEventListener('keydown', event => event.stopPropagation());
  document.addEventListener('visibilitychange', () => { if (!document.hidden && dialog.open) void load(); });
  window.addEventListener('online', () => { if (dialog.open) void load(); });

  function pair(primary, secondary) {
    const result = node('div', 'qu-pair');
    result.append(node('span', '', primary), node('small', 'qu-muted', secondary));
    return result;
  }
  function currencySuffix(row) { return typeof row.cost_currency === 'string' && row.cost_currency ? ' ' + row.cost_currency : ''; }
  function costAmount(value, row) {
    return numeric(value) ? (value < 0 ? '-' + money(-value) : money(value)) + currencySuffix(row) + (row.cost_estimated === true ? '（估算）' : '') : '—';
  }
  function routeSaving(row) {
    if (!numeric(row.cost_actual)) return node('small', 'qu-muted', '未计费');
    if (!numeric(row.cost_saved)) return node('small', 'qu-muted', '实际 ' + money(row.cost_actual) + currencySuffix(row));
    if (row.cost_saved < 0) return node('small', 'qu-muted', '+' + money(-row.cost_saved) + '（升档）');
    return node('small', 'qu-muted', '节省 ' + money(row.cost_saved) + (numeric(row.cost_baseline) ? '（实际 ' + money(row.cost_actual) + ' / 基准 ' + money(row.cost_baseline) + '）' : ''));
  }
  function routeDetails(row) {
    const modal = secondaryDialog('路由详情');
    modal.append(node('p', 'qu-route-detail-conclusion', row.conclusion || '历史记录未包含具体结论。'));
    const grid = node('dl', 'qu-detail-grid');
    const fields = [['决策 ID', row.id], ['时间', dateTime(row.decided_at)], ['实际档位', tierNames[row.tier] || row.tier],
      ['实际模型 / 强度', row.model + ' / ' + row.effort], ['判断来源', classifierNames[row.classifier_mode] || row.classifier_mode],
      ['Qwen 建议', row.qwen_tier ? row.qwen_tier + ' / ' + row.qwen_effort : '未提供'], ['Qwen 类型 / 置信度', (row.classifier_kind || '—') + ' / ' + (row.classifier_confidence || '—')],
      ['决策原因', row.decision_reason], ['分类器原因', row.classifier_reason], ['资源原因', row.resource_reason],
      ['GPU 剩余 / 阈值', number(row.gpu_free_mib) + ' / ' + number(row.gpu_threshold_mib) + ' MiB'],
      ['ComfyUI 运行 / 排队', number(row.comfy_running) + ' / ' + number(row.comfy_pending)],
      ['Qwen 状态', row.qwen_state], ['上下文消息数', number(row.context_messages)],
      ['任务被截取', row.task_excerpted === true ? '是' : row.task_excerpted === false ? '否' : '—'],
      ['判断耗时', duration(row.classifier_ms)], ['路由构建', row.router_build], ['轮次哈希', row.turn_hash]];
    const group = row.cost_group || '';
    const multiplier = numeric(row.cost_group_multiplier) ? ' ×' + number(row.cost_group_multiplier) : '';
    const billed = ['cost_input_tokens', 'cost_cached_tokens', 'cost_cache_write_tokens', 'cost_output_tokens', 'cost_requests'].some(key => numeric(row[key]));
    fields.push(['计费分组', group || multiplier ? (group || '—') + multiplier : '—'],
      ['实际费用', costAmount(row.cost_actual, row)], ['基准费用', costAmount(row.cost_baseline, row)], ['节省', costAmount(row.cost_saved, row)],
      ['计费 Token', billed ? '输入 ' + number(row.cost_input_tokens) + ' / 缓存读 ' + number(row.cost_cached_tokens) + ' / 缓存写 ' + number(row.cost_cache_write_tokens) + ' / 输出 ' + number(row.cost_output_tokens) + '，共 ' + number(row.cost_requests) + ' 次请求' : '—'],
      ['计费来源', row.cost_source || '—'], ['计费说明', row.cost_note || '—']);
    for (const [name, value] of fields) grid.append(node('dt', '', name), node('dd', '', value ?? '—'));
    modal.append(grid); modal.showModal();
  }
  function renderRoutes() {
    const s = routeData.summary;
    freshness.textContent = '更新于 ' + lastUpdated.toLocaleTimeString('zh-CN', { hour12: false }) + ' · 每 10 秒刷新 · 具体结论直接展示在列表中';
    const costItems = [];
    if (numeric(s.cost_actual_total) && numeric(s.cost_baseline_total) && numeric(s.cost_saved_total)) {
      costItems.push(node('span', 'qu-muted', '累计节省 ' + money(s.cost_saved_total) + currencySuffix(s)),
        node('span', 'qu-muted', '实际 ' + money(s.cost_actual_total) + ' / 基准 ' + money(s.cost_baseline_total)));
    }
    if (numeric(s.cost_routes)) costItems.push(node('span', 'qu-muted', number(s.cost_routes) + ' 条已计费'));
    routeSummary.replaceChildren(node('strong', '', number(s.routes) + ' 次决策'),
      node('span', 'qu-muted', 'Qwen 判断 ' + number(s.qwen_routes)), node('span', 'qu-muted', '规则回退 / 保护 ' + number(s.fallback_routes)),
      node('span', 'qu-muted', 'Sol ' + number(s.coding_routes)), node('span', 'qu-muted', 'GPT-6 ' + number((s.complex_routes || 0) + (s.max_routes || 0))),
      ...costItems);
    routeBody.replaceChildren(); routeTable.hidden = !routeData.rows.length; routeEmpty.hidden = !!routeData.rows.length;
    for (const row of routeData.rows) {
      const tr = node('tr');
      const actual = pair(row.model + ' / ' + row.effort, tierNames[row.tier] || row.tier);
      const judgement = pair(classifierNames[row.classifier_mode] || row.classifier_mode,
        (row.qwen_tier ? row.qwen_tier + ' / ' + row.qwen_effort : '无 Qwen 建议') + (row.classifier_confidence ? ' · ' + row.classifier_confidence : ''));
      judgement.append(routeSaving(row));
      const why = node('div', 'qu-route-conclusion', row.conclusion || '历史记录未包含具体结论。');
      const resources = pair(numeric(row.gpu_free_mib) ? '空闲 ' + number(row.gpu_free_mib) + ' MiB' : '显存未报告',
        (row.comfy_busy ? 'ComfyUI 忙 · ' : '') + duration(row.classifier_ms));
      for (const item of [node('span', '', dateTime(row.decided_at)), actual, judgement, why, resources, button('详情', () => routeDetails(row), 'qu-button qu-button-small qu-button-link')]) {
        const td = node('td'); td.append(item); tr.append(td);
      }
      routeBody.append(tr);
    }
    const pages = Math.max(1, Math.ceil(routeData.total / (routeData.page_size || 25)));
    routePageLabel.textContent = '共 ' + number(routeData.total) + ' 条 · 第 ' + page + ' / ' + pages + ' 页';
    routePrevious.disabled = page <= 1; routeNext.disabled = page >= pages;
  }
  function render() {
    const summary = data.summary;
    freshness.textContent = '更新于 ' + lastUpdated.toLocaleTimeString('zh-CN', { hour12: false }) + ' · 打开面板时每 10 秒更新 · 记录从本功能启用后开始累计';
    models.replaceChildren(...(data.models || []).map(model => { const option = node('option'); option.value = model; return option; }));
    summaryStrip.replaceChildren(node('strong', '', number(summary.requests) + ' 次请求'), node('span', 'qu-muted', '总 Token ' + number(summary.total_tokens)), node('span', 'qu-muted', '估算费用 ' + money(summary.cost)), node('span', 'qu-muted', '本地运行默认 0 元'));
    tbody.replaceChildren();
    table.hidden = data.rows.length === 0;
    empty.hidden = data.rows.length !== 0;
    if (data.rows.length === 0) {
      empty.querySelector('h3').textContent = '当前条件下没有请求记录';
      empty.querySelector('p').textContent = '记录从启用本功能后开始累计，不补造历史数据。可以调整筛选范围，或发送一条消息后刷新。';
    }
    for (const row of data.rows) {
      const tr = node('tr');
      const model = pair(row.model || '未知模型', row.source || '—');
      model.classList.add('qu-model-cell');
      model.title = row.model || '未知模型';
      const status = node('div', 'qu-pair');
      const badge = node('span', 'qu-status', statusNames[row.status] || row.status || '—');
      badge.dataset.status = row.status;
      status.append(badge, node('small', 'qu-muted', (typeof row.stream === 'boolean' ? row.stream ? '流式' : '非流式' : '—') + (row.agent ? ' · Agent' : '')));
      const time = numeric(row.started_at) ? new Date(row.started_at * 1000) : null;
      const cells = [pair(time ? time.toLocaleTimeString('zh-CN', { hour12: false }) : '—', time ? localDate(time) : ''), model,
        node('span', 'qu-mode-tag', modeNames[row.mode] || row.mode || '—'), status,
        pair(number(row.input_tokens) + ' 输入', number(row.output_tokens) + ' 输出'),
        pair(number(row.cache_read_tokens) + ' 读取', number(row.cache_write_tokens) + ' 写入'),
        node('span', 'qu-money', money(row.cost)), pair(duration(row.ttft_ms), duration(row.duration_ms)),
        node('span', 'qu-nowrap', numeric(row.tokens_per_second) ? number(row.tokens_per_second) + ' tok/s' : '—'),
        button('详情', () => details(row), 'qu-button qu-button-small qu-button-link')];
      for (const content of cells) { const td = node('td'); td.append(content); tr.append(td); }
      tbody.append(tr);
    }
    const totalPages = Math.max(1, Math.ceil((data.total || 0) / (data.page_size || 25)));
    pageLabel.textContent = '共 ' + number(data.total) + ' 条 · 第 ' + page + ' / ' + totalPages + ' 页';
    previous.disabled = page <= 1;
    next.disabled = page >= totalPages;
    kpis.replaceChildren();
    const metrics = [
      ['请求次数', number(summary.requests), '成功 ' + number(summary.success) + ' · 失败 ' + number(summary.error)],
      ['估算费用', money(summary.cost), '本地运行默认 0 元'],
      ['输入 Token', number(summary.input_tokens), '总 Token ' + number(summary.total_tokens)],
      ['输出 Token', number(summary.output_tokens), '平均首 Token ' + duration(summary.ttft_avg_ms)],
      ['RPM', number(summary.rpm), '时间范围内平均请求 / 分钟'],
      ['TPM', number(summary.tpm), '时间范围内平均 Token / 分钟'],
    ];
    for (const [label, value, note] of metrics) {
      const card = node('article', 'qu-kpi');
      if (label === 'RPM' || label === 'TPM') card.title = (label === 'RPM' ? '总请求数' : '总 Token 数') + ' ÷ 所选起止时间的分钟数，包含没有请求的时段。';
      card.append(node('span', 'qu-kpi-label', label), node('strong', 'qu-kpi-value', value), node('small', 'qu-muted', note));
      kpis.append(card);
    }
    cacheSummary.replaceChildren(node('span', '', '缓存读取 ' + number(summary.cache_read_tokens)), node('span', '', '缓存写入 ' + number(summary.cache_write_tokens)), node('span', '', '用量未知请求 ' + number(summary.unknown_usage_requests)), node('span', '', '平均耗时 ' + duration(summary.duration_avg_ms)));
    renderChart();
  }

  function renderChart() {
    barsButton.setAttribute('aria-pressed', String(!areaChart));
    areaButton.setAttribute('aria-pressed', String(areaChart));
    if (!data) { charts.replaceChildren(emptyState('尚未读取到分析数据', '请求数、Token 和估算费用会按所选时间范围汇总。')); return; }
    const titles = { total_tokens: 'Token 消耗趋势', cost: '估算费用趋势', requests: '请求趋势' };
    charts.replaceChildren(chart(titles[metric.value], metric.value, areaChart));
  }

  function chart(titleText, metric, area) {
    const card = node('article', 'qu-chart-card');
    const chartTitle = node('div', 'qu-chart-title');
    chartTitle.append(node('h2', '', titleText), node('span', 'qu-muted', '按模型 · ' + (bucket.value === 'day' ? '每日' : '每小时')));
    card.append(chartTitle);
    const points = (data.series || []).filter(point => numeric(point.start));
    if (!points.length) { card.append(emptyState('暂无趋势数据', '所选时间范围内的请求会汇总到这里。')); return card; }
    const names = [...new Set(points.map(point => point.model || '未知模型'))];
    let times = [...new Set(points.map(point => point.start))].sort((a, b) => a - b);
    const interval = bucket.value === 'day' ? 86400 : 3600;
    const offset = -new Date().getTimezoneOffset() * 60;
    const rangeStart = data.range?.start;
    const rangeEnd = data.range?.end;
    if (numeric(rangeStart) && numeric(rangeEnd)) {
      const first = Math.floor((rangeStart + offset) / interval) * interval - offset;
      const count = Math.ceil((rangeEnd - first) / interval);
      // Empty buckets represent zero recorded requests, so inactive hours are
      // shown as gaps instead of compressing the time axis around activity.
      if (count > 0 && count <= 2000) times = [...new Set([...times, ...Array.from({ length: count }, (_, index) => first + index * interval)])].sort((a, b) => a - b);
    }
    const table = new Map(times.map(time => [time, new Map()]));
    for (const point of points) {
      const values = table.get(point.start);
      const name = point.model || '未知模型';
      values.set(name, (values.get(name) || 0) + (numeric(point[metric]) ? point[metric] : 0));
    }
    const totals = times.map(time => [...table.get(time).values()].reduce((sum, value) => sum + value, 0));
    const rawMax = Math.max(...totals) || 1;
    const unit = Math.pow(10, Math.floor(Math.log10(rawMax)));
    const maximum = Math.ceil(rawMax / unit) * unit;
    const width = 760, height = 265, left = 64, right = 18, top = 14, bottom = 42;
    const plotW = width - left - right, plotH = height - top - bottom;
    const root = svg('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': titleText + '，按模型分组', class: 'qu-chart-svg' });
    root.append(svg('title', {}, titleText));
    root.append(svg('desc', {}, '时间范围内共 ' + times.length + ' 个有记录的时间段，' + names.length + ' 个模型。'));
    const y = value => top + plotH - value / maximum * plotH;
    const timeSpan = Math.max(interval, times[times.length - 1] - times[0] + interval);
    const x = index => left + plotW * (times[index] - times[0] + interval / 2) / timeSpan;
    const metricValue = value => metric === 'cost' ? money(value) : number(value) + (metric === 'requests' ? ' 次' : ' Token');
    for (let index = 0; index <= 4; index++) {
      const value = maximum * index / 4;
      root.append(svg('line', { x1: left, x2: width - right, y1: y(value), y2: y(value), class: 'qu-gridline' }));
      root.append(svg('text', { x: left - 10, y: y(value) + 4, 'text-anchor': 'end', class: 'qu-axis' }, metric === 'cost' ? money(value) : new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 }).format(value)));
    }
    const step = Math.max(1, Math.ceil(times.length / 6));
    times.forEach((time, index) => {
      if (index % step !== 0 && index !== times.length - 1) return;
      const date = new Date(time * 1000);
      const label = bucket.value === 'day' ? (date.getMonth() + 1) + '/' + date.getDate() : String(date.getHours()).padStart(2, '0') + ':' + String(date.getMinutes()).padStart(2, '0');
      root.append(svg('text', { x: x(index), y: height - 14, 'text-anchor': 'middle', class: 'qu-axis' }, label));
    });
    const stack = times.map(() => 0);
    names.forEach((name, modelIndex) => {
      const color = colors[modelIndex % colors.length];
      const values = times.map(time => table.get(time).get(name) || 0);
      if (area && times.length > 1) {
        const upper = times.map((_, index) => `${x(index)},${y(stack[index] + values[index])}`);
        const lower = times.map((_, index) => `${x(index)},${y(stack[index])}`).reverse();
        const path = svg('path', { d: 'M' + upper.join(' L') + ' L' + lower.join(' L') + ' Z', fill: color, 'fill-opacity': 0.2, stroke: color, 'stroke-width': 1.5 });
        path.append(svg('title', {}, name + ' · ' + metricValue(values.reduce((sum, value) => sum + value, 0))));
        root.append(path);
      } else {
        const barWidth = Math.max(1, Math.min(44, plotW / times.length * 0.65));
        values.forEach((value, index) => {
          const rect = svg('rect', { x: x(index) - barWidth / 2, y: y(stack[index] + value), width: barWidth, height: value / maximum * plotH, rx: 2, fill: color });
          rect.append(svg('title', {}, dateTime(times[index]) + ' · ' + name + ' · ' + metricValue(value)));
          root.append(rect);
        });
      }
      values.forEach((value, index) => { stack[index] += value; });
    });
    const legend = node('div', 'qu-legend');
    names.forEach((name, index) => { const item = node('span', 'qu-legend-item'); const dot = node('i'); dot.style.backgroundColor = colors[index % colors.length]; item.append(dot, node('span', '', name)); legend.append(item); });
    card.append(root, legend);
    return card;
  }

  function secondaryDialog(titleText) {
    const modal = node('dialog', 'qu-secondary');
    modal.setAttribute('aria-label', titleText);
    const head = node('div', 'qu-secondary-header');
    const close = button('', () => modal.close(), 'qu-button qu-icon-button', 'close');
    close.setAttribute('aria-label', '关闭');
    head.append(node('h2', '', titleText), close);
    modal.append(head);
    document.body.append(modal);
    modal.addEventListener('close', () => { modal.remove(); if (dialog.open) closeButton.focus({ preventScroll: true }); }, { once: true });
    modal.addEventListener('keydown', event => event.stopPropagation());
    return modal;
  }
  function details(row) {
    const modal = secondaryDialog('请求详情');
    const grid = node('dl', 'qu-detail-grid');
    const fields = [
      ['请求 ID', row.id], ['模型', row.model || '未知模型'], ['开始时间', dateTime(row.started_at)], ['结束时间', dateTime(row.finished_at)],
      ['状态', statusNames[row.status] || row.status], ['HTTP 状态', row.http_status ?? '—'], ['分组', modeNames[row.mode] || row.mode],
      ['来源', row.source || '—'], ['请求端点', row.endpoint], ['类型', (row.stream ? '流式' : '非流式') + (row.agent ? ' · Agent' : '')],
      ['输入 Token', number(row.input_tokens)], ['输出 Token', number(row.output_tokens)], ['缓存读取 Token', number(row.cache_read_tokens)], ['缓存写入 Token', number(row.cache_write_tokens)],
      ['首 Token 延迟', duration(row.ttft_ms)], ['总耗时', duration(row.duration_ms)], ['生成速度', numeric(row.tokens_per_second) ? number(row.tokens_per_second) + ' tok/s' : '—'], ['估算费用', money(row.cost)],
    ];
    for (const [key, value] of fields) grid.append(node('dt', '', key), node('dd', '', value === null || value === undefined ? '—' : String(value)));
    modal.append(grid);
    if (row.error_message) { const box = node('div', 'qu-detail-error'); box.append(node('strong', '', '错误信息'), node('pre', '', String(row.error_message))); modal.append(box); }
    modal.append(node('p', 'qu-note', '— 表示服务未报告该指标。费用按请求发生时的设置估算，本地运行默认 0 元。'));
    modal.showModal();
  }
  async function preferences() {
    const modal = secondaryDialog('费用设置');
    modal.append(node('p', 'qu-note', '估算费用，本地运行默认 0 元。可按每百万 Token 设置参考单价；修改仅影响新请求，不会改写历史费用。'));
    const form = node('form', 'qu-price-form');
    const fields = {};
    for (const [key, text] of [['input_per_million', '输入'], ['output_per_million', '输出'], ['cache_read_per_million', '缓存读取'], ['cache_write_per_million', '缓存写入']]) {
      const label = node('label', 'qu-price-field');
      label.append(node('span', '', text + '（元 / 百万 Token）'));
      const input = node('input', 'qu-input');
      input.type = 'number'; input.min = '0'; input.max = '1000000000'; input.step = 'any'; input.required = true; input.disabled = true;
      label.append(input); form.append(label); fields[key] = input;
    }
    const notice = node('div', 'qu-note', '正在读取费用设置…');
    notice.setAttribute('role', 'status');
    const actions = node('div', 'qu-secondary-actions');
    const save = node('button', 'qu-button qu-button-primary', '保存设置');
    save.type = 'submit'; save.disabled = true;
    actions.append(button('取消', () => modal.close()), save);
    form.append(notice, actions);
    modal.append(form);
    modal.showModal();
    try {
      const result = await responseJson(await fetch('/local/usage/preferences', { cache: 'no-store', signal: AbortSignal.timeout(10000) }));
      if (!modal.open) return;
      for (const [key, input] of Object.entries(fields)) { input.value = numeric(result[key]) ? String(result[key]) : '0'; input.disabled = false; }
      notice.textContent = '币种：人民币 CNY'; save.disabled = false;
    } catch (failure) { notice.textContent = failure.name === 'TimeoutError' ? '读取设置超时，请关闭后重试。' : failure.message; }
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (!form.reportValidity() || save.disabled) return;
      const values = { currency: 'CNY' };
      for (const [key, input] of Object.entries(fields)) { values[key] = Number(input.value); if (!Number.isFinite(values[key]) || values[key] < 0) return; }
      save.disabled = true;
      try {
        await responseJson(await fetch('/local/usage/preferences', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(values), signal: AbortSignal.timeout(10000) }));
        modal.close();
        if (dialog.open) void load();
      } catch (failure) { notice.textContent = failure.message || '保存失败，请重试。'; save.disabled = false; }
    });
  }
  async function exportCsv() {
    if (exporting) return;
    exporting = true; exportButton.disabled = true;
    try {
      const routing = activeTab === 'routing';
      const response = await fetch((routing ? '/local/routing/export?' : '/local/usage/export?') + parameters(false), { cache: 'no-store', signal: AbortSignal.timeout(30000) });
      if (!response.ok) throw new Error('导出失败（HTTP ' + response.status + '），请重试。');
      const url = URL.createObjectURL(await response.blob());
      const anchor = node('a');
      anchor.href = url; anchor.download = (routing ? 'Qwen-路由记录-' : 'Qwen-使用日志-') + filters.start.value.replace(/:/g, '-') + '-' + filters.end.value.replace(/:/g, '-') + '.csv';
      document.body.append(anchor); anchor.click(); anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
    } catch (failure) { showError(failure.name === 'TimeoutError' ? '导出超时，请缩小时间范围后重试。' : failure.message); }
    finally { exporting = false; exportButton.disabled = false; }
  }

  function position() {
    const right = sidebar?.getBoundingClientRect().right || 56;
    dialog.style.setProperty('--qu-left', (window.innerWidth >= 900 ? Math.min(right + 16, 320) : 12) + 'px');
  }
  const resize = new ResizeObserver(position);
  function attach() {
    const candidate = document.querySelector('body > div[style="display: contents"] > .flex > aside') ||
      Array.from(document.querySelectorAll('aside')).find(item => item.querySelector('[aria-label="展开导航"], [aria-label="收起导航"], [aria-label="返回首页"], [aria-label="Expand navigation"], [aria-label="Collapse navigation"], [aria-label="Go to start"]'));
    if (!candidate) return;
    if (sidebar !== candidate) { resize.disconnect(); sidebar = candidate; resize.observe(sidebar); }
    if (footer.parentElement !== sidebar) sidebar.append(footer);
    position();
  }
  let attachPending = false;
  new MutationObserver(() => {
    if (attachPending) return;
    attachPending = true;
    requestAnimationFrame(() => { attachPending = false; attach(); });
  }).observe(document.body, { childList: true, subtree: true });
  window.addEventListener('resize', position);
  setRange(1);
  selectTab(activeTab);
  renderChart();
  attach();
})();
