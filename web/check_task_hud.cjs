/* 悬浮任务窗的轻量验收（不起任何生图/生视频任务，不占显卡）
 *
 * 所有者要求：
 *   1. 不点开时是一个可移动的小圆圈；
 *   2. 展开后能看到当前在执行什么任务，以及自己的任务排在等待队列的第几位。
 *
 * 这里刻意做成"最小确认"：一个无头浏览器 + 一次页面加载，任务状态用 `QwenTaskHud._apply`
 * 直接喂负载来看它怎么显示，不跑真实任务。位置记忆用一次刷新来验，不碰显卡。
 *
 * 用法：node check_task_hud.cjs [port]
 */
const { spawn } = require('node:child_process');
const { results, check, cdp, evaluate, sleep, launchAndOpen } = require('./lib/cdp.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = Number(process.argv[2] || 9583);
const ROOT = 'D:\\AI\\Qwen-Local';
const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(candidate => fs.existsSync(candidate));


async function drag(ws, from, to) {
  await cdp(ws, 'Input.dispatchMouseEvent',
    { type: 'mousePressed', x: from.x, y: from.y, button: 'left', clickCount: 1, buttons: 1 });
  for (let step = 1; step <= 6; step++) {
    await cdp(ws, 'Input.dispatchMouseEvent', {
      type: 'mouseMoved', button: 'left', buttons: 1,
      x: Math.round(from.x + (to.x - from.x) * step / 6),
      y: Math.round(from.y + (to.y - from.y) * step / 6),
    });
    await sleep(40);
  }
  await cdp(ws, 'Input.dispatchMouseEvent',
    { type: 'mouseReleased', x: to.x, y: to.y, button: 'left', clickCount: 1, buttons: 0 });
}

(async () => {
  if (!EDGE) { console.log('SKIP: Edge not found'); return; }
  const link = fs.readFileSync(path.join(ROOT, 'logs', 'admin-link.txt'), 'ascii').trim();
  // 浏览器、调试端口、登录与导航都在 web/lib/cdp.cjs 里（原来每个脚本各复制一遍）。
  const { profile, child, ws } = await launchAndOpen({ port: 9499 });
  try {

    check('悬浮窗已挂载', await evaluate(ws, `Boolean(document.querySelector('.qt-root'))`));

    // 真机场景：窗口说的"有任务/没任务"必须与服务端一致。所有者报的就是不一致
    // （ComfyUI 里有活、窗口说"没任务"）。这个断言在空闲和忙碌两种状态下都成立。
    const agree = await evaluate(ws, `Promise.all([
        fetch('/local/video/progress', { cache: 'no-store' }).then(r => r.json()),
        fetch('/local/image/progress?since=0&preview=-1', { cache: 'no-store' }).then(r => r.json()),
      ]).then(([video, image]) => QwenTaskHud.refresh().then(() => {
        const activePhases = ['sampling', 'loading', 'loading_model', 'decoding', 'encoding',
          'queued', 'preparing', 'warming', 'starting'];
        const serverHasWork = Boolean(
          (image.job && (image.busy || image.job.state === 'queued_behind'))
          || (video.job && video.busy)
          || (video.comfy && (video.comfy.queue_running || video.comfy.queue_pending
              || activePhases.includes(String(video.comfy.phase)))));
        return {
          serverHasWork,
          windowShowsWork: document.querySelector('.qt-root').dataset.busy === 'true',
          rows: [...document.querySelectorAll('.qt-item')]
            .map(item => item.textContent.replace(/\\s+/g, ' ').trim()),
          emptyShown: !document.querySelector('.qt-empty').hidden,
        };
      }))`);
    check('窗口显示的任务与服务端一致（真机状态）',
      agree.serverHasWork === agree.windowShowsWork
      && agree.emptyShown === !agree.windowShowsWork,
      agree.windowShowsWork
        ? `服务端有任务 → 窗口显示：${agree.rows.join(' / ').slice(0, 70)}`
        : '服务端没有任务 → 窗口显示"现在没有任务"（空闲时的正确表现）');

    // ---- 折叠态：小圆圈、固定在视口、不属于任何面板 ----
    const shape = await evaluate(ws, `(() => {
      const root = document.querySelector('.qt-root');
      const bubble = document.querySelector('.qt-bubble');
      const style = getComputedStyle(bubble);
      const box = getComputedStyle(root);
      const rect = bubble.getBoundingClientRect();
      return {
        open: root.dataset.open,
        panelHidden: document.querySelector('.qt-panel').hidden,
        w: Math.round(rect.width), h: Math.round(rect.height),
        radius: style.borderRadius, cursor: style.cursor,
        fixed: box.position,
        parentIsBody: root.parentElement === document.body,
        inPanel: Boolean(root.closest('.qg-root, .qv-root, .qm-panel, .local-panel')),
      };
    })()`);
    check('默认收起，面板不显示', shape.open === 'false' && shape.panelHidden === true);
    check('收起时是小圆圈', Math.abs(shape.w - shape.h) <= 1 && shape.w >= 40 && shape.w <= 64
      && shape.radius === '50%', `${shape.w}×${shape.h} radius=${shape.radius}`);
    check('悬浮在视口上（position: fixed）', shape.fixed === 'fixed', shape.fixed);
    check('拖动光标提示可移动', shape.cursor === 'grab', shape.cursor);
    check('挂在 body 上、不属于任何面板', shape.parentIsBody === true && shape.inPanel === false);

    // 壳级注入：所有模式共用同一份 head，所以每个模式都会加载它。
    const shell = await evaluate(ws, `fetch('/', { cache: 'no-store' }).then(r => r.text())`);
    check('壳页面注入了悬浮窗（所有模式共用）',
      shell.includes('/local/task-hud.js') && shell.includes('/local/task-hud.css'));
    const source = fs.readFileSync(path.join(ROOT, 'web', 'task-hud.js'), 'utf8');
    // 注释里会提到面板的类名（上面就写着"不依赖 .qg-* / .qv-*"），所以先把注释剥掉再找真实选择器。
    const code = source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
    check('不依赖任何面板内部结构', !/['"`]\.q[gvm]-/.test(code), '源码里没有 .qg-/.qv-/.qm- 选择器');

    // 默认位置（还没拖动过）不该压住要紧的控件。先量一次，再去做拖动实验。
    const resting = await evaluate(ws, `(() => {
      const box = node => { const r = node && node.getBoundingClientRect();
        return r ? { l: Math.round(r.left), t: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) } : null; };
      const hit = (a, b) => a && b && !(a.l + a.w <= b.l || b.l + b.w <= a.l
        || a.t + a.h <= b.t || b.t + b.h <= a.t);
      const hud = box(document.querySelector('.qt-bubble'));
      const composer = box(document.querySelector('.conversation-chat-form'));
      const entry = box(document.querySelector('.qs-entry'));
      return { hud, composer, entry, composerHit: hit(hud, composer), entryHit: hit(hud, entry) };
    })()`);
    check('默认位置不压住输入框', resting.composerHit === false,
      `窗口 ${JSON.stringify(resting.hud)} vs 输入框 ${JSON.stringify(resting.composer)}`);
    check('默认位置不压住共享入口', resting.entryHit !== true,
      resting.entry ? `共享入口 ${JSON.stringify(resting.entry)}`
        : '页面上没有共享入口（机主在回环访问时不显示入口，跳过）');

    // ---- 可移动：拖一下、记在本机、刷新后还在 ----
    const before = await evaluate(ws, `QwenTaskHud.position()`);
    await drag(ws, { x: before.x + 27, y: before.y + 27 }, { x: before.x - 160, y: before.y + 120 });
    await sleep(400);
    const after = await evaluate(ws, `QwenTaskHud.position()`);
    check('拖得动', Math.abs(after.x - before.x) > 60 && Math.abs(after.y - before.y) > 40,
      `(${before.x},${before.y}) → (${after.x},${after.y})`);
    check('拖动不会顺手展开', await evaluate(ws, `QwenTaskHud.isOpen()`) === false);
    check('位置记在本机',
      await evaluate(ws, `Boolean(localStorage.getItem('qwen.taskHud.position'))`));

    await cdp(ws, 'Page.navigate', { url: link });
    await sleep(9000);
    const restored = await evaluate(ws, `QwenTaskHud.position()`);
    check('刷新后位置还在', Math.abs(restored.x - after.x) <= 4 && Math.abs(restored.y - after.y) <= 4,
      `(${after.x},${after.y}) → (${restored.x},${restored.y})`);

    // ---- 不挡住要紧的地方：这一项已经在默认位置量过，这里只留拖完之后的复核 ----
    const afterDrag = await evaluate(ws, `(() => {
      const box = node => { const r = node && node.getBoundingClientRect();
        return r ? { l: Math.round(r.left), t: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) } : null; };
      const hit = (a, b) => a && b && !(a.l + a.w <= b.l || b.l + b.w <= a.l
        || a.t + a.h <= b.t || b.t + b.h <= a.t);
      return { composer: hit(box(document.querySelector('.qt-bubble')),
                             box(document.querySelector('.conversation-chat-form'))) };
    })()`);
    check('拖到别处也不压住输入框', afterDrag.composer === false);

    // ---- 展开：当前任务 + 自己的排队名次（喂负载，不跑真实任务）----
    await evaluate(ws, `QwenTaskHud.setOpen(true), true`);
    const shown = JSON.parse(await evaluate(ws, `(() => {
      const read = () => [...document.querySelectorAll('.qt-item')].map(item => ({
        tone: item.dataset.tone,
        text: item.textContent.replace(/\\s+/g, ' ').trim(),
      }));
      const count = () => document.querySelector('.qt-count').hidden
        ? 0 : Number(document.querySelector('.qt-count').textContent);
      QwenTaskHud._apply({ id: 'img-1', state: 'queued_behind', phase: 'queued_behind',
        queue_ahead: 1, session: 'guest-token' }, null);
      const queued = { rows: read(), count: count() };
      QwenTaskHud._apply({ id: 'img-2', state: 'running', phase: 'sampling', step: 3,
        max_step: 8, progress: 0.42 }, null);
      const running = { rows: read(), count: count() };
      QwenTaskHud._apply(null, { id: 'vid-1', state: 'running', phase: 'sampling',
        position: 2, total: 4, eta: 95, submitted_by: '' });
      const video = { rows: read(), count: count() };
      QwenTaskHud._apply(null, null, { phase: 'sampling', step: 11, steps: 20,
        project: 'tianmen-duel-30s', shot: 1, shots: 1, sampling_eta_seconds: 3802,
        queue_running: 1, queue_pending: 0 });
      const orphan = { rows: read(), count: count() };
      QwenTaskHud._apply(null, null);
      const idle = { rows: read(), count: count(),
        hint: !document.querySelector('.qt-empty').hidden };
      // H3 会把最后一次中断的状态留在监视里（真机上就是 interrupted 11/20）：不能变成幽灵任务。
      QwenTaskHud._apply(null, null, { phase: 'interrupted', step: 11, steps: 20,
        project: 'tianmen-duel-30s', queue_running: 0, queue_pending: 0 });
      const stopped = { rows: read(), count: count(),
        hint: !document.querySelector('.qt-empty').hidden };
      return JSON.stringify({ queued, running, video, orphan, idle, stopped,
        open: QwenTaskHud.isOpen() });
    })()`));

    check('展开后是一个窗口', shown.open === true);
    check('等待中的任务显示"排队第 1 位"',
      shown.queued.rows.length === 1 && shown.queued.rows[0].tone === 'queued'
      && shown.queued.rows[0].text.includes('排队第 1 位'),
      shown.queued.rows[0] ? shown.queued.rows[0].text : '(没有条目)');
    check('跑着的任务显示阶段与步数',
      shown.running.rows.length === 1 && shown.running.rows[0].text.includes('采样中 3/8')
      && shown.running.rows[0].text.includes('42%'),
      shown.running.rows[0] ? shown.running.rows[0].text : '(没有条目)');
    check('视频任务显示镜头进度',
      shown.video.rows.length === 1 && shown.video.rows[0].text.includes('第 2/4 个镜头'),
      shown.video.rows[0] ? shown.video.rows[0].text : '(没有条目)');
    check('圆圈上的计数跟着任务数', shown.running.count === 1 && shown.video.count === 1
      && shown.orphan.count === 1,
      `queued=${shown.queued.count} running=${shown.running.count} video=${shown.video.count} orphan=${shown.orphan.count}`);
    check('网关重启后仍在渲染的 ComfyUI 任务也看得见',
      shown.orphan.rows.length === 1 && shown.orphan.rows[0].text.includes('ComfyUI 正在渲染')
      && shown.orphan.rows[0].text.includes('11/20')
      && shown.orphan.rows[0].text.includes('tianmen-duel-30s'),
      shown.orphan.rows[0] ? shown.orphan.rows[0].text : '(没有条目)');
    check('没有任务时回到空提示', shown.idle.rows.length === 0 && shown.idle.hint === true
      && shown.idle.count === 0);
    check('已中断/已完成的任务不留幽灵行',
      shown.stopped.rows.length === 0 && shown.stopped.hint === true && shown.stopped.count === 0,
      shown.stopped.rows.length ? shown.stopped.rows[0].text : '（干净）');

    // 收起要靠标题栏的 ×，而不是拖动。
    await evaluate(ws, `document.querySelector('.qt-close').click(), true`);
    check('点 × 收起', await evaluate(ws, `QwenTaskHud.isOpen()`) === false);

    // ---- 轮询克制：空闲 8 秒一跳，而且从不索取那 79 KB 的预览图 ----
    await evaluate(ws, `(() => {
      window.__hudUrls = [];
      const real = window.fetch.bind(window);
      window.fetch = function (input, init) {
        const url = typeof input === 'string' ? input : (input && input.url) || '';
        if (url.includes('/local/image/progress')) window.__hudUrls.push(url);
        return real(input, init);
      };
      return true;
    })()`);
    await sleep(11000);
    const urls = await evaluate(ws, `window.__hudUrls`);
    const withPreview = urls.filter(url => url.includes('preview=-1'));
    const askingPicture = urls.filter(url => url.includes('preview=') && !url.includes('preview=-1'));
    check('轮询带 preview=-1（从不要那张预览图）',
      withPreview.length >= 1 && askingPicture.length === 0,
      `${urls.length} 次：${urls.slice(0, 2).join(' | ')}`);
    check('空闲时不超过 3 次/11 秒（不是紧循环）', urls.length <= 3, `${urls.length} 次`);
  } finally {
    try { child.kill(); } catch { /* ignore */ }
    console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
    process.exitCode = results.every(Boolean) ? 0 : 1;
  }
})();
