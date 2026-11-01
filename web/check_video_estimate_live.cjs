/* 面板里的预计时间是否随尺寸变化（无头 DOM，零显存）。
 *
 * 所有者要求：视频生成的预计时间要动态估算，且**时长、步数、尺寸一起参与**。
 * 这一项只证明"尺寸"这一维：点三个尺寸档位，读预计时间格子与提示文字，必须逐档变大。
 * 注意：不引用面板 IIFE 内部的 state，只看 DOM。
 */
const { spawn } = require('node:child_process');
const { results, check, cdp, evaluate, sleep, launchAndOpen } = require('./lib/cdp.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = Number(process.argv[2] || 9587);
const ROOT = 'D:\\AI\\Qwen-Local';
const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(candidate => fs.existsSync(candidate));


(async () => {
  if (!EDGE) { console.log('SKIP: Edge not found'); return; }
  const link = fs.readFileSync(path.join(ROOT, 'logs', 'admin-link.txt'), 'ascii').trim();
  // 浏览器、调试端口、登录与导航都在 web/lib/cdp.cjs 里（原来每个脚本各复制一遍）。
  const { profile, child, ws } = await launchAndOpen({ port: 9499 });
  try {
    await evaluate(ws, `window.VideoPanel.setActive(true), true`);
    await sleep(2500);

    const probe = await evaluate(ws, `(() => {
      const row = document.querySelector('.qv-size-presets');
      if (!row) return JSON.stringify({ error: '没有尺寸档位' });
      const estimateText = () => {
        const tiles = [...document.querySelectorAll('.qv-metrics .qv-metric, .qv-metrics .qv-stat, .qv-metric')];
        const tile = tiles.find(t => /预计|预估/.test(t.textContent) || /预计|预估/.test(t.getAttribute('title') || ''));
        return (tile || tiles[0] || {}).textContent?.trim() || '';
      };
      const click = size => {
        const button = [...row.querySelectorAll('button')].find(b => b.dataset.size === size);
        if (!button) return null;
        button.click();
        return estimateText();
      };
      const base = click('768x512');
      const mid = click('960x640');
      const high = click('1024x672');
      const hint = (document.querySelector('.qv-metrics') || {}).title
        || [...document.querySelectorAll('.qv-metric')].map(t => t.title).filter(Boolean).join(' ');
      return JSON.stringify({ base, mid, high, hint: String(hint).slice(0, 200) });
    })()`);
    const data = JSON.parse(probe);
    if (data.error) { check('尺寸档位存在', false, data.error); }
    else {
      const minutes = text => Number((String(text).match(/(\d+)/) || [])[1] || NaN);
      const base = minutes(data.base), mid = minutes(data.mid), high = minutes(data.high);
      check('768×512 档有预计时间', Number.isFinite(base), data.base);
      check('尺寸变大 → 预计时间变长（960×640）', mid > base, `${data.base} → ${data.mid}`);
      check('尺寸变大 → 预计时间变长（1024×672）', high > mid, `${data.mid} → ${data.high}`);
      check('提示里说明了尺寸的影响', /尺寸|×/.test(data.hint), data.hint.slice(0, 90));
      check('未标定尺寸时如实说明是外推', /外推|还没跑过/.test(data.hint), '');
    }
    await evaluate(ws, `window.VideoPanel.setActive(false), true`);
  } finally {
    try { child.kill(); } catch { /* ignore */ }
    console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
    process.exitCode = results.every(Boolean) ? 0 : 1;
  }
})();
