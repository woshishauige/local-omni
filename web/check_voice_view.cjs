/* 语音页：打开时到底还切不切后端？按钮能不能载入？
 *
 * 所有者口径：「切到语音通话的时候还在模式切换」——打开语音页只该换页面；
 * 载入语音后端应当由「确认切换」按钮做，或真正开始说话时才做。
 *
 * 这个检查用真实 Edge，只观察：
 *   1. 点「语音对话」后有没有发出 /local/voice/mode（必须是 0 个）；
 *   2. 后端 /local/status 的 mode/state 必须没变（ready + text）；
 *   3. 状态行与按钮文案是否说清"未载入语音后端 / 载入语音后端"；
 *   4. 按「确认切换」时确实调用 VoicePanel.activate（用桩替换它，不真的重载权重）。
 */
const { spawn } = require('node:child_process');
const { results, check, cdp, evaluate, sleep, launchAndOpen } = require('./lib/cdp.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = 9428;
const ROOT = 'D:\\AI\\Qwen-Local';
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';



const status = ws => evaluate(ws, `fetch('/local/status').then(r => r.json())
  .then(s => JSON.stringify({ mode: s.mode, state: s.state, loaded: s.loaded_profile }))`);

(async () => {
  // 浏览器/端口/登录/导航统一在 web/lib/cdp.cjs。
  const { profile, child, ws } = await launchAndOpen({ port: 9428 });
  try {

    // 先把后端放回纯文本档，这样"打开语音页是否切换"才有区分度。
    await evaluate(ws, `fetch('/local/mode', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'text' }) }).then(() => 'text')`);
    await sleep(2500);
    const before = JSON.parse(await status(ws));

    await evaluate(ws, `(() => {
      window.__voiceCalls = [];
      const raw = window.fetch.bind(window);
      window.__rawVoiceFetch = raw;
      window.fetch = (input, init) => {
        const url = typeof input === 'string' ? input : (input && input.url) || '';
        if (String((init && init.method) || 'GET') !== 'GET'
            && /\\/local\\/(voice\\/mode|mode|model|recover)/.test(url)) {
          window.__voiceCalls.push(url);
        }
        return raw(input, init);
      };
      return 'spy';
    })()`);

    await evaluate(ws, `(() => {
      const tab = [...document.querySelectorAll('.local-mode-button')].find(n => (n.textContent || '').includes('语音'));
      if (tab) tab.click();
      return Boolean(tab);
    })()`);
    await sleep(3000);
    const after = JSON.parse(await status(ws));
    const calls = JSON.parse(await evaluate(ws, `JSON.stringify(window.__voiceCalls)`));
    check('opening the voice view sends no backend-load request', calls.length === 0, JSON.stringify(calls));
    check('the backend stays ready and untouched', after.mode === before.mode && after.state === 'ready',
      `${before.mode}/${before.state} → ${after.mode}/${after.state}`);

    const shown = JSON.parse(await evaluate(ws, `JSON.stringify({
      now: document.querySelector('.local-model-now').textContent,
      button: document.querySelector('.local-model-apply').textContent,
      disabled: document.querySelector('.local-model-apply').disabled,
      hint: (document.querySelector('.local-voice-hint, [data-voice-hint]') || {}).textContent || '',
    })`));
    check('the status line says the voice backend is not loaded yet',
      /语音/.test(shown.now) && /未载入/.test(shown.now), shown.now);
    check('the button offers to load the voice backend', /载入语音后端/.test(shown.button),
      `${shown.button} (disabled=${shown.disabled})`);

    // 按下去：桩掉 activate，证明按钮确实把"载入语音后端"交给语音页（不真的重载权重）。
    const pressed = JSON.parse(await evaluate(ws, `(async () => {
      let called = 0;
      const original = window.VoicePanel.activate;
      window.VoicePanel.activate = () => { called += 1; return Promise.resolve(true); };
      document.querySelector('.local-model-apply').click();
      await new Promise(r => setTimeout(r, 400));
      window.VoicePanel.activate = original;
      return JSON.stringify({ called });
    })()`));
    check('pressing it asks the voice panel to load the backend', pressed.called === 1,
      `activate calls=${pressed.called}`);

    ws.close();
  } finally {
    child.kill();
    await sleep(600);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch { /* Edge may hold a handle */ }
  }
  console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
  process.exit(results.every(Boolean) ? 0 : 1);
})().catch(error => { console.error('ERROR', error.message); process.exit(1); });
