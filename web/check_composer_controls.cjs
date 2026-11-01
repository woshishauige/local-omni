/* 对话框右下角的两个控件：本对话放行 + 麦克风。
 *
 * 三件事必须一起成立，缺一件这个改动就没做到：
 *   1. 控件在**对话框里**（不是工具栏），麦克风是纯图标按钮、在右下角、不被应用自己的发送键压住；
 *   2. 放行是**按对话**的：这条对话放行、换一条对话仍然逐次询问，切回来又生效；
 *   3. 放行真的能止住"允许一次 / 始终允许"的提问——所以列表里必须有浏览器工具的键
 *      （`browser:read_media` 等）。它不在网关的 `/tools` 里，这正是"已全局放行还问一次"的原因。
 *
 * 用法：node check_composer_controls.cjs [port]
 */
const { spawn } = require('node:child_process');
const { results, check, cdp, evaluate, sleep } = require('./lib/cdp.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = Number(process.argv[2] || 9466);
const ROOT = 'D:\\AI\\Qwen-Local';
const BASE = 'http://127.0.0.1:8081';
const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(candidate => fs.existsSync(candidate));
const ALLOW_KEY = 'LlamaUi.alwaysAllowedTools';
const PHRASE = '把这张图片放大两倍';


async function waitFor(ws, expression, timeout = 30000, what = 'condition') {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    try { if (await evaluate(ws, expression)) return true; } catch { /* keep waiting */ }
    await sleep(400);
  }
  throw new Error('timed out waiting for ' + what);
}

const allowedList = ws => evaluate(ws, `(() => { try { return JSON.parse(localStorage.getItem(${JSON.stringify(ALLOW_KEY)}) || '[]'); } catch { return null; } })()`);
const clickChip = ws => evaluate(ws, `document.querySelector('.local-mode-permission.local-composer-chip').click(), true`);
const clickMic = ws => evaluate(ws, `document.querySelector('.qv-mic-primary').click(), true`);

(async () => {
  if (!EDGE) { console.log('SKIP: Edge not found'); return; }
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'qv-composer-'));
  const child = spawn(EDGE, [
    '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check',
    '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream',
    '--autoplay-policy=no-user-gesture-required', '--window-size=1440,900', 'about:blank',
  ], { stdio: 'ignore' });
  try {
    for (let i = 0; i < 60; i++) {
      try { await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json(); break; } catch { await sleep(400); }
    }
    const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
    const ws = new WebSocket(list.find(t => t.type === 'page').webSocketDebuggerUrl);
    await new Promise(resolve => ws.addEventListener('open', resolve, { once: true }));
    await cdp(ws, 'Runtime.enable');
    await cdp(ws, 'Page.enable');
    // 听写走网关的 /local/voice/stream*，这里用一个固定短语替身，验收的是"文本进没进对话框"。
    await cdp(ws, 'Page.addScriptToEvaluateOnNewDocument', {
      source: `(() => {
        const real = window.fetch.bind(window);
        window.__qvStreamCalls = 0;
        window.fetch = async function (input, init) {
          const url = typeof input === 'string' ? input : (input && input.url) || '';
          if (url.includes('/local/voice/stream/finish')) {
            return new Response(JSON.stringify({ text: ${JSON.stringify(PHRASE)}, committed: ${JSON.stringify(PHRASE)},
              phrase: '', seq: 9, finished: true, duration_seconds: 4 }),
              { status: 200, headers: { 'Content-Type': 'application/json' } });
          }
          if (url.includes('/local/voice/stream')) {
            window.__qvStreamCalls += 1;
            return new Response(JSON.stringify({ committed: ${JSON.stringify(PHRASE)}, phrase: '',
              text: ${JSON.stringify(PHRASE)}, seq: 0, finished: false, duration_seconds: 1 }),
              { status: 200, headers: { 'Content-Type': 'application/json' } });
          }
          return real(input, init);
        };
      })();`,
    });
    const token = fs.readFileSync(path.join(ROOT, 'logs', 'admin.token'), 'ascii').trim();
    await cdp(ws, 'Page.navigate', { url: `${BASE}/?admin=${token}` });
    await waitFor(ws, 'document.readyState === "complete"', 30000, 'page load');
    await waitFor(ws, '!!document.querySelector(".conversation-chat-form")', 30000, 'the composer');
    await waitFor(ws, '!!document.getElementById("local-composer-tools")', 30000, 'the composer dock');
    await sleep(1500);

    const box = await evaluate(ws, `(() => {
      const round = rect => ({ x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) });
      const form = document.querySelector('.conversation-chat-form');
      const dock = document.getElementById('local-composer-tools');
      const chip = document.querySelector('.local-mode-permission.local-composer-chip');
      const mic = document.querySelector('.qv-mic-primary');
      const field = document.querySelector('.conversation-chat-form textarea');
      const formRect = round(form.getBoundingClientRect());
      const micRect = round(mic.getBoundingClientRect());
      const at = document.elementFromPoint(micRect.x + micRect.w / 2, micRect.y + micRect.h / 2);
      return {
        formRect, dockRect: round(dock.getBoundingClientRect()), micRect, fieldRect: field ? round(field.getBoundingClientRect()) : null,
        micInDock: mic.parentElement === dock,
        micIsLast: dock.lastElementChild === mic,
        // 图标按钮 = 除了给读屏用的隐藏标签和计时器，没有别的可见文字。
        micIconOnly: (() => {
          const walker = document.createTreeWalker(mic, NodeFilter.SHOW_TEXT);
          let node;
          while ((node = walker.nextNode())) {
            const text = (node.textContent || '').trim();
            if (!text) continue;
            const parent = node.parentElement;
            if (!parent) continue;
            const hidden = parent.classList.contains('qv-sr-only') || parent.classList.contains('qv-mic-timer');
            if (!hidden) return false;
          }
          return true;
        })(),
        micHasSvg: !!mic.querySelector('svg'),
        hitIsMic: !!(at && (at === mic || mic.contains(at))),
        chipInDock: chip.parentElement === dock,
        chipInToolbar: !!document.querySelector('#local-mode-toolbar .local-mode-permission'),
        rightAligned: formRect.x + formRect.w - (micRect.x + micRect.w) <= 20,
        bottomHalf: micRect.y + micRect.h / 2 > formRect.y + formRect.h / 2,
        chipText: chip.textContent.trim(),
      };
    })()`);

    check('控件挂在对话框里', box.dockRect.w > 0 && box.micInDock && box.chipInDock,
      `dock ${JSON.stringify(box.dockRect)}`);
    check('麦克风在右下角', box.rightAligned && box.bottomHalf,
      `右距 ${box.formRect.x + box.formRect.w - (box.micRect.x + box.micRect.w)}px`);
    check('麦克风是纯图标按钮', box.micIconOnly && box.micHasSvg && box.micRect.w <= 40,
      `${box.micRect.w}×${box.micRect.h}`);
    check('麦克风没有被应用自己的按钮压住', box.hitIsMic === true);
    check('麦克风是这一排最右边的控件', box.micIsLast === true);
    check('工具栏里不再有这个开关', box.chipInToolbar === false);
    check('默认是逐次询问', box.chipText.includes('逐次询问'), box.chipText);
    check('默认什么都没放行', JSON.stringify(await allowedList(ws)) === '[]');

    await clickChip(ws);
    await sleep(400);
    const dialogOpen = await evaluate(ws, `(() => { const d = document.querySelector('.local-permission-dialog'); return d ? !d.hidden : false; })()`);
    check('点它先弹确认，而不是直接放行', dialogOpen === true);
    await evaluate(ws, `document.querySelector('.local-permission-confirm').click(), true`);
    await sleep(1200);

    const granted = await allowedList(ws) || [];
    check('放行后浏览器工具也在名单里', granted.includes('browser:read_media') && granted.includes('browser:run_javascript'),
      `共 ${granted.length} 个`);
    check('服务器工具也在名单里', granted.includes('server:read_file') && granted.includes('server:exec_shell_command'));
    check('筹码显示本对话已放行',
      (await evaluate(ws, `document.querySelector('.local-mode-permission.local-composer-chip').dataset.granted`)) === 'true');

    // 应用自己每次决定都会整份重写这个列表；重写不能把我们加的键冲掉。
    await evaluate(ws, `localStorage.setItem(${JSON.stringify(ALLOW_KEY)}, JSON.stringify(['server:read_file'])), true`);
    await sleep(300);
    const afterAppWrite = await allowedList(ws) || [];
    check('应用自己重写列表也压不掉放行', afterAppWrite.includes('browser:read_media'),
      afterAppWrite.join(','));

    // 每对话一份状态：换一条对话就不该再自动放行。
    await evaluate(ws, `history.pushState({}, '', '/chat/another-conversation-xyz'), true`);
    await sleep(1600);
    const otherChat = await allowedList(ws) || [];
    check('换一条对话后不再自动放行', !otherChat.includes('browser:read_media'), otherChat.join(','));
    await evaluate(ws, `history.pushState({}, '', location.pathname.replace('/chat/another-conversation-xyz', '/')), true`);
    await sleep(1600);
    const backHome = await allowedList(ws) || [];
    check('切回原对话又生效（按对话各自记着）', backHome.includes('browser:read_media'), backHome.join(','));

    // 麦克风：点一下开始听写，识别出的文字要进对话框的输入框。
    await clickMic(ws);
    await sleep(2500);
    const listening = await evaluate(ws, `(() => {
      const voice = window.QwenVoice && window.QwenVoice.state;
      const mic = document.querySelector('.qv-mic-primary');
      const field = document.querySelector('.conversation-chat-form textarea');
      return { live: !!(voice && (voice.live || voice.recorder)), mode: mic.dataset.mode,
               value: field ? field.value : null, calls: window.__qvStreamCalls || 0 };
    })()`);
    check('点麦克风就开始听写', listening.live === true || listening.calls > 0,
      `mode=${listening.mode} calls=${listening.calls}`);
    check('识别出的文字进了对话框', (listening.value || '').includes(PHRASE), JSON.stringify(listening.value || '').slice(0, 60));

    await clickMic(ws);
    await sleep(2500);
    const stopped = await evaluate(ws, `(() => {
      const voice = window.QwenVoice && window.QwenVoice.state;
      const mic = document.querySelector('.qv-mic-primary');
      return { live: !!(voice && (voice.live || voice.recorder)), mode: mic.dataset.mode,
               field: (document.querySelector('.conversation-chat-form textarea') || {}).value || '' };
    })()`);
    check('再点一次就停下', stopped.live === false, `mode=${stopped.mode}`);
    check('停下之后文字还在对话框里', stopped.field.includes(PHRASE), JSON.stringify(stopped.field).slice(0, 60));

    await clickChip(ws);
    await sleep(900);
    const revoked = await allowedList(ws) || [];
    check('关掉后恢复逐次询问', !revoked.includes('browser:read_media') && !revoked.includes('server:read_file'),
      revoked.join(','));
  } finally {
    try { child.kill(); } catch { /* ignore */ }
    console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
    process.exitCode = results.every(Boolean) ? 0 : 1;
  }
})();
