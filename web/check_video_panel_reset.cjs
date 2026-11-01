/* 视频生成页的两项轻量验收（不起任何渲染，不占显卡）
 *
 * 1. 尺寸档位：基准 768×512、中档 960×640、高档 1024×672（H3 要求宽高能被 32 整除，
 *    所有者要的 1024×682 会被节点直接拒绝，所以取最近的合法高度）。
 * 2. 退出页面要清空"选好的素材"：所有者观察到第一段的参考图退出后还在，下一轮会被再次上传并使用。
 *
 * 做法：无头浏览器打开机主页面 → 手动显示面板 → 点尺寸档位读全局参数框 → 用
 * CDP 往文件框里挂一个真实文件 → 断言标签变化 → 调 setActive(false)（＝离开页面）→
 * 断言标签回到"未选择"且文件框为空。全程十几秒、零显存。
 *
 * 用法：node check_video_panel_reset.cjs [port]
 */
const { spawn } = require('node:child_process');
const { results, check, cdp, evaluate, sleep, launchAndOpen } = require('./lib/cdp.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = Number(process.argv[2] || 9585);
const ROOT = 'D:\\AI\\Qwen-Local';
const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(candidate => fs.existsSync(candidate));


/** 往选择器命中的文件框里挂一个真实文件（CDP 官方做法，等价于用户点选文件）。 */
async function pickFile(ws, selector, filePath) {
  const { root } = await cdp(ws, 'DOM.getDocument', { depth: -1, pierce: true });
  const { nodeId } = await cdp(ws, 'DOM.querySelector', { nodeId: root.nodeId, selector });
  if (!nodeId) throw new Error('找不到文件框：' + selector);
  await cdp(ws, 'DOM.setFileInputFiles', { nodeId, files: [filePath] });
  return nodeId;
}

(async () => {
  if (!EDGE) { console.log('SKIP: Edge not found'); return; }
  const link = fs.readFileSync(path.join(ROOT, 'logs', 'admin-link.txt'), 'ascii').trim();
  // 浏览器、调试端口、登录与导航都在 web/lib/cdp.cjs 里（原来每个脚本各复制一遍）。
  const { profile, child, ws } = await launchAndOpen({ port: 9499 });
  try {

    check('视频面板已挂载', await evaluate(ws, `Boolean(window.VideoPanel)`));

    // 手动显示面板：这一步只是本地显隐，不切服务端模式、不装载任何模型。
    await evaluate(ws, `window.VideoPanel.setActive(true), true`);
    await sleep(1200);

    // ---- 1. 尺寸档位 ----
    const presets = await evaluate(ws, `(() => {
      const row = document.querySelector('.qv-size-presets');
      const labels = [...row.querySelectorAll('button')].map(b => ({
        text: b.textContent.trim(), size: b.dataset.size, title: b.title || '',
      }));
      const click = size => {
        const button = [...row.querySelectorAll('button')].find(b => b.dataset.size === size);
        button.click();
        const inputs = [...document.querySelectorAll('.qv-params input[type=number]')];
        const values = inputs.map(i => Number(i.value));
        return values.slice(0, 2);
      };
      return { labels, mid: click('960x640'), high: click('1024x672'), base: click('768x512') };
    })()`);
    check('三档尺寸都在', presets.labels.length === 3,
      presets.labels.map(l => l.size).join(' / '));
    check('中档 = 960×640', presets.mid[0] === 960 && presets.mid[1] === 640,
      JSON.stringify(presets.mid));
    check('高档 = 1024×672（682 会被 H3 拒绝）',
      presets.high[0] === 1024 && presets.high[1] === 672, JSON.stringify(presets.high));
    check('高档说明了为什么不是 682',
      presets.labels.some(l => l.size === '1024x672' && l.title.includes('32')), '按钮 title');
    check('基准档仍是 768×512', presets.base[0] === 768 && presets.base[1] === 512,
      JSON.stringify(presets.base));
    check('所有档位都能被 32 整除',
      presets.labels.every(l => {
        const [w, h] = l.size.split('x').map(Number);
        return w % 32 === 0 && h % 32 === 0;
      }));

    // ---- 2. 退出页面清空"选好的素材" ----
    // 用页内 DataTransfer 赋值再派发 change：与用户点选文件等价，且不会被 accept 过滤挡掉
    // （CDP 的 setFileInputFiles 在 `accept="video/mp4,…"` 的框上实测不生效）。
    const pick = (selector, name, type) => `(() => {
      const input = document.querySelector(${JSON.stringify(selector)});
      const transfer = new DataTransfer();
      transfer.items.add(new File([new Uint8Array([137, 80, 78, 71])], ${JSON.stringify(name)},
        { type: ${JSON.stringify(type)} }));
      input.files = transfer.files;
      input.dispatchEvent(new Event('change', { bubbles: true }));
      return input.files.length;
    })()`;

    const refSelector = '.qv-shots .qv-shot .qv-shot-refs input[type=file]';
    check('参考图框挂上了文件',
      await evaluate(ws, pick(refSelector, 'reference-probe.png', 'image/png')) === 1);
    await sleep(300);
    const picked = await evaluate(ws, `(() => {
      const card = document.querySelector('.qv-shots .qv-shot');
      return card.querySelector('.qv-file-name').textContent.trim();
    })()`);
    check('挂上参考图后标签显示文件名', picked.includes('reference-probe'),
      JSON.stringify(picked));

    // 离开这个页面，再回来：这正是所有者说的"退出后台后"。
    await evaluate(ws, `window.VideoPanel.setActive(false), true`);
    await sleep(500);
    const afterLeave = await evaluate(ws, `(() => {
      const card = document.querySelector('.qv-shots .qv-shot');
      const inputs = [...document.querySelectorAll('.qv-shot-refs input[type=file]')];
      return {
        label: card.querySelector('.qv-file-name').textContent.trim(),
        files: inputs.reduce((sum, input) => sum + (input.files ? input.files.length : 0), 0),
      };
    })()`);
    await evaluate(ws, `window.VideoPanel.setActive(true), true`);
    await sleep(500);
    const afterReturn = await evaluate(ws, `(() => {
      const card = document.querySelector('.qv-shots .qv-shot');
      const inputs = [...document.querySelectorAll('.qv-shot-refs input[type=file]')];
      return {
        label: card.querySelector('.qv-file-name').textContent.trim(),
        files: inputs.reduce((sum, input) => sum + (input.files ? input.files.length : 0), 0),
      };
    })()`);
    check('退出后参考图标签回到"未选择"', afterLeave.label === '未选择', afterLeave.label);
    check('退出后文件框是空的', afterLeave.files === 0, `${afterLeave.files} 个文件`);
    check('再进来也没有残留', afterReturn.label === '未选择' && afterReturn.files === 0,
      `${afterReturn.label} / ${afterReturn.files} 个文件`);

    // 第一段特有的"前情片段"（视频）同样要被清掉。
    const clipSelector = '.qv-shot-context input[type=file]';
    check('前情片段框挂上了文件',
      await evaluate(ws, pick(clipSelector, 'context-probe.mp4', 'video/mp4')) === 1);
    await sleep(300);
    const contextPicked = await evaluate(ws,
      `document.querySelector('.qv-shot-context .qv-file-name').textContent.trim()`);
    await evaluate(ws, `window.VideoPanel.setActive(false), true`);
    await sleep(400);
    const contextAfter = await evaluate(ws,
      `document.querySelector('.qv-shot-context .qv-file-name').textContent.trim()`);
    check('前情片段也一起清空',
      contextPicked.includes('context-probe') && contextAfter !== contextPicked,
      `${contextPicked} → ${contextAfter}`);

    await evaluate(ws, `window.VideoPanel.setActive(false), true`);
  } finally {
    try { child.kill(); } catch { /* ignore */ }
    console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
    process.exitCode = results.every(Boolean) ? 0 : 1;
  }
})();
