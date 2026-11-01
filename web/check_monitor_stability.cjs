/* 视频生成页的进度栏：和「成片」那一列一样是普通版面，内部不滚动，高度包住内容。
 *
 * 所有者两次要求，这里是第二次的验收口径：
 *   ①"框架大小位置锁死" → 三栏宽度与进度栏的左边缘、宽度不许动（这条一直在验）；
 *   ②"要和成片那里一样，不能随滚轮移动并且大小包住内容即可" → 不是粘在顶部、不是视口高度、
 *     卡片里没有任何可滚动区域（滚轮永远滚页面），高度就是内容高度。
 *
 * 这个脚本不改代码、不跑真任务，只把"跑起来之后才会出现的东西"一次性塞进去再量。
 *
 * 用法：node check_monitor_stability.cjs [port]
 */
const { spawn } = require('node:child_process');
const { results, check, cdp, evaluate, sleep, launchAndOpen } = require('./lib/cdp.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = Number(process.argv[2] || 9560);
const ROOT = 'D:\\AI\\Qwen-Local';
const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(candidate => fs.existsSync(candidate));

const BOX = `(() => {
  const round = rect => ({ x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) });
  const card = document.querySelector('.qv-monitor');
  const column = document.querySelector('.qv-column-monitor');
  const film = document.querySelector('.qv-column-side');
  const body = document.querySelector('.qv-monitor-body');
  const style = node => node ? getComputedStyle(node) : null;
  // 卡片里任何一层都不该能滚：滚轮永远滚页面。
  const scrollers = card ? [card, ...card.querySelectorAll('*')].filter(node => {
    const own = getComputedStyle(node);
    return (own.overflowY === 'auto' || own.overflowY === 'scroll') && node.scrollHeight > node.clientHeight + 2;
  }).map(node => (node.className || node.tagName).toString().slice(0, 30)) : [];
  return {
    card: card ? round(card.getBoundingClientRect()) : null,
    column: column ? round(column.getBoundingClientRect()) : null,
    film: film ? round(film.getBoundingClientRect()) : null,
    columns: getComputedStyle(document.querySelector('.qv-body')).gridTemplateColumns,
    cardHeight: style(card) ? style(card).height : null,
    cardOverflow: style(card) ? style(card).overflowY : null,
    bodyOverflow: style(body) ? style(body).overflowY : null,
    columnPosition: style(column) ? style(column).position : null,
    columnTop: style(column) ? style(column).top : null,
    filmPosition: style(film) ? style(film).position : null,
    filmTop: style(film) ? style(film).top : null,
    scrollers,
  };
})()`;

const STRESS = `(() => {
  const card = document.querySelector('.qv-monitor');
  const body = document.querySelector('.qv-monitor-body');
  if (!card || !body) return { ok: false };
  const badge = card.querySelector('.qv-monitor-badge');
  if (badge) { badge.textContent = '逐段生成 3/6'; badge.dataset.state = 'running'; }
  const hint = body.querySelector('.qv-card-hint');
  if (hint) hint.textContent = '提交后这里是实时进度：每一段的受理、构建图、提交、采样与拼接都会写在这一行里，最长的一行会在这里折成两行。';
  const status = body.querySelector('.qv-status');
  if (status) status.textContent = '第 3 段采样中 · 预计剩余 6 分 12 秒 · 采样 20 步';
  const percent = body.querySelector('.qv-progress-percent');
  if (percent) percent.textContent = '47%';
  const values = ['15–23 分钟', '4 分 20 秒', '6 分 12 秒', '1.8 it/s', '860 ms'];
  [...body.querySelectorAll('.qv-metric-value')].forEach((node, index) => { node.textContent = values[index] || '—'; });
  [...body.querySelectorAll('.qv-monitor-line')].forEach((node, index) => {
    node.textContent = ['第 3/6 步 · 采样 12/20 · 种子 302626409', '构建图 4.2s · 采样 38.5s',
      '速度 1.82 it/s', '分辨率 768×512 · 参考图 2 张'][index] || '';
  });
  const preview = body.querySelector('.qv-preview');
  if (preview) {
    preview.hidden = false;
    const image = preview.querySelector('img');
    if (image && !image.src) image.src = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';
  }
  const shots = body.querySelector('.qv-monitor-shots');
  if (shots && shots.children.length < 30) {
    shots.replaceChildren();
    for (let index = 1; index <= 30; index++) {
      const row = document.createElement('div');
      row.className = 'qv-shot-row';
      row.dataset.state = index < 3 ? 'done' : (index === 3 ? 'running' : 'pending');
      for (const [cls, text] of [['qv-shot-row-index', '第 ' + index + ' 段'], ['qv-shot-row-spec', '7.5s · 20 步'],
                                 ['qv-shot-row-state', '采样中'], ['qv-shot-row-time', '38s']]) {
        const span = document.createElement('span');
        span.className = cls;
        span.textContent = text;
        row.append(span);
      }
      shots.append(row);
    }
  }
  const flowSteps = document.querySelector('.qv-flow-steps');
  if (flowSteps) {
    flowSteps.replaceChildren();
    for (const label of ['受理', '启动', '构建图', '提交', '逐段生成', '拼接完成']) {
      const step = document.createElement('span');
      step.className = 'qv-flow-step';
      step.dataset.state = 'pending';
      step.textContent = label;
      flowSteps.append(step);
    }
  }
  const flowPercent = document.querySelector('.qv-flow-percent');
  if (flowPercent) flowPercent.textContent = '62%';
  return { ok: true, lines: body.querySelectorAll('.qv-monitor-line').length,
           shots: body.querySelectorAll('.qv-shot-row').length };
})()`;

(async () => {
  if (!EDGE) { console.log('SKIP: Edge not found'); return; }
  // 浏览器、调试端口、登录与导航都在 web/lib/cdp.cjs 里（原来每个脚本各复制一遍）。
  const { profile, child, ws } = await launchAndOpen({ port: 9499 });
  try {
    await evaluate(ws, `(document.querySelector('.local-mode-button[data-mode="video"]') || {click(){}}).click(), true`);
    await sleep(4000);

    const idle = await evaluate(ws, BOX);
    check('进度栏挂上了', Boolean(idle.card && idle.card.w > 200 && idle.card.h > 100),
      JSON.stringify(idle.card));
    check('和成片那一列一样的定位（都不是粘住的）',
      idle.columnPosition === idle.filmPosition && idle.columnPosition === 'static',
      `进度 ${idle.columnPosition}/${idle.columnTop} · 成片 ${idle.filmPosition}/${idle.filmTop}`);
    // 高度没有写死：卡片不写内联高度、不限高，而且里面没有任何东西被裁掉（scrollHeight 不超出框高）。
    const wraps = await evaluate(ws, `(() => {
      const card = document.querySelector('.qv-monitor');
      const style = getComputedStyle(card);
      return { box: Math.round(card.getBoundingClientRect().height), scroll: card.scrollHeight,
               maxHeight: style.maxHeight, inlineHeight: card.style.height || '' };
    })()`);
    check('卡片高度就是内容高度（没有写死，也没有截断）',
      wraps.scroll <= wraps.box + 1 && wraps.maxHeight === 'none' && wraps.inlineHeight === '',
      `框高 ${wraps.box}px · 内容 ${wraps.scroll}px · max-height ${wraps.maxHeight}`);
    check('卡片与内部都没有滚动区（滚轮只会滚页面）',
      idle.cardOverflow === 'visible' && idle.bodyOverflow === 'visible' && idle.scrollers.length === 0,
      `card=${idle.cardOverflow} body=${idle.bodyOverflow} 内部滚动区=${JSON.stringify(idle.scrollers)}`);

    const stressed = await evaluate(ws, STRESS);
    await sleep(400);
    const busy = await evaluate(ws, BOX);
    check('压力内容真的塞进去了（30 段故事板）',
      stressed.ok === true && stressed.shots >= 30, JSON.stringify(stressed));

    check('宽度与左边缘没动（横向锁死）',
      idle.card.x === busy.card.x && idle.card.w === busy.card.w && idle.column.x === busy.column.x,
      `x ${idle.card.x}→${busy.card.x} · w ${idle.card.w}→${busy.card.w}`);
    check('三栏宽度不随内容变化', idle.columns === busy.columns, `${idle.columns} → ${busy.columns}`);
    check('高度跟着内容长（包住内容，而不是固定视口高）', busy.card.h > idle.card.h,
      `${idle.card.h}px → ${busy.card.h}px`);
    check('内容再长也不在卡片里滚（没有新的滚动区）', busy.scrollers.length === 0,
      JSON.stringify(busy.scrollers));

    // 滚轮滚的是页面（这个面板里是它自己的滚动容器）：滚动时进度栏与成片必须同步位移。
    const scrolled = await evaluate(ws, `(() => {
      const card = document.querySelector('.qv-monitor');
      const film = document.querySelector('.qv-column-side');
      let scroller = card.parentElement;
      while (scroller && scroller !== document.body) {
        const style = getComputedStyle(scroller);
        if ((style.overflowY === 'auto' || style.overflowY === 'scroll') && scroller.scrollHeight > scroller.clientHeight + 2) break;
        scroller = scroller.parentElement;
      }
      const target = scroller && scroller !== document.body ? scroller : document.scrollingElement;
      const before = { card: card.getBoundingClientRect().top, film: film.getBoundingClientRect().top, at: target.scrollTop };
      target.scrollTop = before.at + 300;
      const after = { card: card.getBoundingClientRect().top, film: film.getBoundingClientRect().top, at: target.scrollTop };
      target.scrollTop = before.at;
      return { before, after, moved: Math.round(after.at - before.at),
               who: (target.className || target.tagName).toString().slice(0, 30) };
    })()`);
    check('滚轮滚的是整页：进度栏与成片同步位移', scrolled.moved > 0 &&
      Math.round(scrolled.before.card - scrolled.after.card) === Math.round(scrolled.before.film - scrolled.after.film),
      `滚动容器 ${scrolled.who} · 位移 ${scrolled.moved}px · 进度 ${Math.round(scrolled.before.card - scrolled.after.card)}px · 成片 ${Math.round(scrolled.before.film - scrolled.after.film)}px`);
  } finally {
    // Hand the service back in 纯文本 mode: it was switched to a panel above, and the mode belongs
    // to the service rather than to this browser profile.
    try { await evaluate(ws, `(document.querySelector('.local-mode-button[data-mode="text"]') || {click(){}}).click(), true`); } catch { /* page may be gone */ }
    try { child.kill(); } catch { /* ignore */ }
    console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
    process.exitCode = results.every(Boolean) ? 0 : 1;
  }
})();
