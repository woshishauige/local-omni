/* 顶部导航的显示与行为：点「识图」应当高亮识图、按钮不移动、文字不被截断，
 * 而且**正在生成时导航照样能点**（导航只换页面，不该被作业锁住）。
 *
 * 所有者反馈：「点击识图模式时顶端导航出现一点显示问题」——实测三处：
 *   1. 高亮跟的是后端模式而不是页面（点识图后仍高亮纯文本）；
 *   2. 状态行文字变长把整排模式按钮推右 10px；
 *   3. 生图作业在跑时整排按钮 disabled，点了没反应。
 */
const { spawn } = require('node:child_process');
const { results, check, cdp, evaluate, sleep, launchAndOpen } = require('./lib/cdp.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = 9432;
const ROOT = 'D:\\AI\\Qwen-Local';
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';



const SNAP = `(() => {
  const row = document.querySelector('.local-mode-row');
  const now = document.querySelector('.local-model-now');
  const box = node => { const r = node.getBoundingClientRect(); return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) }; };
  const tabs = [...document.querySelectorAll('.local-mode-button')].map(node => ({
    label: (node.textContent || '').trim().slice(0, 10),
    pressed: node.getAttribute('aria-pressed'), disabled: node.disabled, ...box(node),
  }));
  return JSON.stringify({
    row: box(row), rowHeight: row.getBoundingClientRect().height,
    now: now ? { text: now.textContent, ...box(now), scroll: now.scrollWidth, ellipsis: now.scrollWidth > now.clientWidth + 1 } : null,
    tabs,
    statusText: (document.querySelector('.local-mode-status-text') || {}).textContent || '',
    statusH: Math.round((document.querySelector('.local-mode-status') || {getBoundingClientRect: () => ({height: 0})}).getBoundingClientRect().height),
  });
})()`;

const clickTab = (ws, label) => evaluate(ws, `(() => {
  const node = [...document.querySelectorAll('.local-mode-button')]
    .find(n => (n.textContent || '').includes(${JSON.stringify(label)}));
  if (!node) return 'no tab';
  if (node.disabled) return 'disabled';
  node.click();
  return 'clicked';
})()`);

(async () => {
  // 浏览器/端口/登录/导航统一在 web/lib/cdp.cjs。
  const { profile, child, ws } = await launchAndOpen({ port: 9432 });
  try {

    // 1. 纯文本 → 识图：高亮跟着页面走
    await clickTab(ws, '纯文本');
    await sleep(1500);
    const textPage = JSON.parse(await evaluate(ws, SNAP));
    const clicked = await clickTab(ws, '识图');
    await sleep(1800);
    const visionPage = JSON.parse(await evaluate(ws, SNAP));
    const pressed = visionPage.tabs.filter(tab => tab.pressed === 'true').map(tab => tab.label);
    check('clicking 识图 actually opens the view', clicked === 'clicked', clicked);
    check('the highlight follows the view, not the backend mode',
      pressed.length === 1 && pressed[0].includes('识图'), pressed.join(',') || '(none)');
    check('the mode buttons do not move between pages',
      textPage.tabs.every((tab, index) => visionPage.tabs[index].x === tab.x),
      textPage.tabs.map((tab, index) => `${tab.label}@${tab.x}→${visionPage.tabs[index].x}`).join(' '));
    check('the toolbar keeps one row', visionPage.rowHeight <= textPage.rowHeight + 1,
      `${textPage.rowHeight}px → ${visionPage.rowHeight}px`);
    check('the status line is not cut off', visionPage.now && !visionPage.now.ellipsis,
      visionPage.now ? `${visionPage.now.text} (${visionPage.now.w}px, scroll ${visionPage.now.scroll})` : 'missing');
    check('the status line fits on the text page too', textPage.now && !textPage.now.ellipsis,
      textPage.now ? `${textPage.now.text} (${textPage.now.w}px, scroll ${textPage.now.scroll})` : 'missing');
    check('the right-hand status stays on one line', visionPage.statusH <= 24,
      `${visionPage.statusH}px · ${visionPage.statusText}`);

    // 2. 模拟"生图作业在跑"：状态改成 switching + image running，导航必须照样可点
    await evaluate(ws, `(() => {
      const raw = window.fetch.bind(window);
      window.__rawStatusFetch = raw;
      window.fetch = (input, init) => {
        const url = typeof input === 'string' ? input : (input && input.url) || '';
        if (url.includes('/local/status')) {
          return raw(input, init).then(async response => {
            const data = await response.clone().json();
            data.state = 'switching';
            data.image = { job_id: 'check-job', state: 'running', phase: 'queued', progress: 0.1 };
            return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
          });
        }
        return raw(input, init);
      };
      return 'stubbed';
    })()`);
    await sleep(3500);
    const busyPage = JSON.parse(await evaluate(ws, SNAP));
    const busyEnabled = busyPage.tabs.every(tab => !tab.disabled);
    check('a running job no longer greys out the navigation', busyEnabled,
      busyPage.tabs.map(tab => `${tab.label}:${tab.disabled ? 'disabled' : 'on'}`).join(' '));
    const clickedWhileBusy = await clickTab(ws, '纯文本');
    await sleep(1200);
    const afterBusy = JSON.parse(await evaluate(ws, SNAP));
    const nowPressed = afterBusy.tabs.filter(tab => tab.pressed === 'true').map(tab => tab.label);
    check('the view still switches while a job is running',
      clickedWhileBusy === 'clicked' && nowPressed.length === 1 && nowPressed[0].includes('纯文本'),
      `${clickedWhileBusy} → ${nowPressed.join(',') || '(none)'} · state=${afterBusy.statusText}`);

    ws.close();
  } finally {
    child.kill();
    await sleep(600);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch { /* Edge may hold a handle */ }
  }
  console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
  process.exit(results.every(Boolean) ? 0 : 1);
})().catch(error => { console.error('ERROR', error.message); process.exit(1); });
