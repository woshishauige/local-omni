/* The toolbar carries the modes, and no leftover module of its own.
 *
 * The 视频识别 panel used to append a launcher («视频» with the small «识图» hint) into the mode row,
 * which read as a second, separate way to read a video even though 识图 already does that (the
 * packaged app's attachment area sends `input_video` itself). It was removed; this check is what
 * keeps it removed, and it writes a screenshot of the toolbar so the row can be looked at.
 *
 * Usage: node check_toolbar_modules.cjs [outDir]
 */
const { spawn } = require('node:child_process');
const { results, check, cdp, evaluate, sleep, launchAndOpen } = require('./lib/cdp.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const OUT = process.argv[2] || path.join(__dirname, 'shots');
const PORT = Number(process.env.TOOLBAR_PORT || 9413);
const ROOT = 'D:\\AI\\Qwen-Local';
const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(candidate => fs.existsSync(candidate));


(async () => {
  if (!EDGE) { console.log('SKIP: Edge not found'); return; }
  fs.mkdirSync(OUT, { recursive: true });
  const link = fs.readFileSync(path.join(ROOT, 'logs', 'admin-link.txt'), 'ascii').trim();
  // 浏览器、调试端口、登录与导航都在 web/lib/cdp.cjs 里（原来每个脚本各复制一遍）。
  const { profile, child, ws } = await launchAndOpen({ port: 9499 });
  try {

    const toolbar = JSON.parse(await evaluate(ws, `(() => {
      const row = document.querySelector('#local-mode-toolbar');
      const buttons = [...document.querySelectorAll('.local-mode-button')];
      return JSON.stringify({
        modes: buttons.map(button => button.dataset.mode),
        labels: buttons.map(button => button.textContent.replace(/\\s+/g, ' ').trim()),
        // The removed module announced itself three ways: its launcher class, its panel and its
        // accessible name.
        leftoverLaunchers: document.querySelectorAll('.local-video-pick, .local-vision-launch').length,
        leftoverPanels: document.querySelectorAll('[aria-label="视频分析"], #local-vision-panel').length,
        toolbarHeight: row ? Math.round(row.getBoundingClientRect().height) : null,
        voicePanel: Boolean(document.querySelector('#local-voice-panel')),
      });
    })()`));
    check('工具栏只剩模式按钮（没有额外的识别模块）',
      toolbar.leftoverLaunchers === 0 && toolbar.leftoverPanels === 0,
      `启动器=${toolbar.leftoverLaunchers} 面板=${toolbar.leftoverPanels}`);
    check('五个模式都在', JSON.stringify(toolbar.modes) ===
      JSON.stringify(['text', 'vision', 'voice', 'generate', 'video']), toolbar.modes.join('/'));
    check('识图仍然是模式之一（视频走它自己的附件区）', toolbar.modes.includes('vision'));
    check('语音页仍然挂载', toolbar.voicePanel === true);
    console.log(`      工具栏：${toolbar.labels.join(' | ')}`);

    if (toolbar.toolbarHeight) {
      const shot = await cdp(ws, 'Page.captureScreenshot',
        { format: 'png', clip: { x: 0, y: 0, width: 1440, height: toolbar.toolbarHeight + 40, scale: 1 } });
      const file = path.join(OUT, 'toolbar.png');
      fs.writeFileSync(file, Buffer.from(shot.data, 'base64'));
      console.log('  ' + path.basename(file));
    }
    ws.close();
  } finally {
    child.kill();
    await sleep(700);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (failure) { /* Edge may hold a handle */ }
  }
  console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
  process.exit(results.every(Boolean) ? 0 : 1);
})().catch(error => { console.error('ERROR', error.message); process.exit(1); });
