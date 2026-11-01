/* What the page actually sees when the mode tabs are clicked.
 *
 * The owner reports the toolbar showing "本地服务连接失败" after switching views. Measured
 * from outside, /local/status answers in milliseconds even mid-switch, so the next question
 * is what the page receives: a non-2xx, a shape the toolbar rejects, or nothing at all
 * (timeout). This records every /local/ request with its status and duration, plus the
 * toolbar's own status line and any error text on screen, before and after each tab click.
 *
 * Usage: node check_mode_switch_live.cjs [port]
 */
const { spawn } = require('node:child_process');
const { results, check, cdp, evaluate, sleep, launchAndOpen } = require('./lib/cdp.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = Number(process.argv[2] || 9407);
const ROOT = 'D:\\AI\\Qwen-Local';
const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(candidate => fs.existsSync(candidate));


(async () => {
  if (!EDGE) { console.log('SKIP: Edge not found'); return; }
  // 浏览器、调试端口、登录与导航都在 web/lib/cdp.cjs 里（原来每个脚本各复制一遍）。
  const { profile, child, ws } = await launchAndOpen({ port: 9499 });
  try {

    /* The toolbar's own words, plus any error surface anywhere on the page. Reading the
     * toolbar's status line is the point: that is where the owner sees the disconnect. */
    const snapshot = async () => JSON.parse(await evaluate(ws, `(() => {
      const status = document.querySelector('.local-mode-status');
      const error = document.querySelector('.local-mode-error');
      const toolbar = document.querySelector('.local-mode-toolbar');
      return JSON.stringify({
        badge: status ? status.textContent.trim() : null,
        badgeHidden: status ? status.hidden : null,
        toolbarError: error && !error.hidden ? error.textContent.trim() : null,
        toolbarState: toolbar ? toolbar.dataset.localModelState || null : null,
        pressed: [...document.querySelectorAll('.local-mode-button')]
          .filter(b => b.getAttribute('aria-pressed') === 'true')
          .map(b => b.dataset.mode),
      });
    })()`));

    const clickTab = async mode => {
      await evaluate(ws, `(() => {
        const button = document.querySelector('.local-mode-button[data-mode="${mode}"]');
        if (!button) throw new Error('no button for ${mode}');
        button.click();
        return true;
      })()`);
    };

    const start = await snapshot();
    console.log(`初始：badge=${JSON.stringify(start.badge)} state=${start.toolbarState} pressed=${start.pressed}`);
    check('工具栏状态行存在', start.badge !== null, String(start.badge));

    const seen = [];
    for (const mode of ['vision', 'text', 'voice', 'text']) {
      const before = local.length;
      await clickTab(mode);
      await sleep(9000);
      const after = await snapshot();
      const requests = local.slice(before);
      const bad = requests.filter(r => typeof r.status !== 'number' || r.status >= 400);
      seen.push({ mode, after, requests: requests.length, bad });
      console.log(`切到 ${mode}: badge=${JSON.stringify(after.badge)} state=${after.toolbarState} ` +
        `请求 ${requests.length} 个，异常 ${bad.length} 个` +
        (bad.length ? ' -> ' + bad.slice(0, 4).map(r => `${r.status} ${r.url.replace(/^.*\/local/, '/local')} ${r.ms}ms`).join('; ') : ''));
      check(`切到 ${mode} 后没有把服务判为断联`,
        (after.badge || '').indexOf('失败') < 0 && after.toolbarState !== 'offline',
        `badge=${after.badge} state=${after.toolbarState}`);
      check(`切到 ${mode} 没有请求失败`, bad.length === 0,
        bad.slice(0, 3).map(r => `${r.status} ${r.url.replace(/^.*\/local/, '/local')}`).join('; '));
    }

    const slowest = local.slice().sort((a, b) => (b.ms || 0) - (a.ms || 0))[0];
    check('没有超过 8 秒的本地请求', !slowest || slowest.ms < 8000,
      slowest ? `${slowest.ms}ms ${slowest.url.replace(/^.*\/local/, '/local')}` : 'none');
    check('页面没有未捕获异常', problems.length === 0, problems.slice(0, 1).join(''));

    fs.writeFileSync(path.join(ROOT, 'logs', 'mode-switch-live.json'),
      JSON.stringify({ start, seen, local, problems }, null, 2));
    console.log('\n记录已写入 logs\\mode-switch-live.json');
    ws.close();
  } finally {
    child.kill();
    await sleep(700);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (failure) { /* Edge may hold a handle */ }
  }
  console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
  process.exit(results.every(Boolean) ? 0 : 1);
})().catch(error => { console.error('ERROR', error.message); process.exit(1); });
