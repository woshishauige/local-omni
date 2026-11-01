/* 生图与生视频的每一个提示词框，右下角都有它自己的麦克风。
 *
 * 要求来自所有者："生图生视频的所有提示词这一栏语音输入要求放在框的右下角，并标上麦克风标识"。
 * 这个脚本在真浏览器里量位置（按钮必须落在框内右下角）、量形状（纯图标 + 麦克风 SVG），并且点一下
 * 真的听写，确认文字进的是**这个框**而不是聊天输入框。
 *
 * 用法：node check_prompt_mics.cjs [port]
 */
const { spawn } = require('node:child_process');
const { results, check, cdp, evaluate, sleep, launchAndOpen } = require('./lib/cdp.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = Number(process.argv[2] || 9570);
const ROOT = 'D:\\AI\\Qwen-Local';
const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(candidate => fs.existsSync(candidate));
const PHRASE = '夜晚雨后的城市街道';

/* 每个提示词框都必须有：自己的按钮、在框内右下角、是纯图标、点得到。 */
const SURVEY = `(() => {
  const round = rect => ({ x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) });
  const fields = [...document.querySelectorAll('.qg-prompt-input, .qg-negative-input, .qv-textarea')];
  return fields.map(field => {
    const visible = field.getBoundingClientRect().width > 0;
    const button = field.dataset.boxMic
      ? document.querySelector('.qv-mic-box[data-box-mic-for="' + field.dataset.boxMic + '"]')
      : null;
    if (!button) return { cls: field.className, visible, hasMic: false };
    // Scroll first, measure second: the page moves when the button is brought into view, and a
    // rectangle read before that is stale — which is what made an 8px corner look like 86px.
    button.scrollIntoView({ block: 'center' });
    const rect = field.getBoundingClientRect();
    const box = button.getBoundingClientRect();
    const center = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
    return {
      cls: field.className, visible: rect.width > 0, hasMic: true,
      micRect: round(box), fieldRect: round(rect),
      insideRight: Math.round(rect.right - box.right), insideBottom: Math.round(rect.bottom - box.bottom),
      iconOnly: ![...button.childNodes].some(node => node.nodeType === 3 && node.textContent.trim()),
      hasSvg: !!button.querySelector('svg'),
      hit: !!(center && (center === button || button.contains(center))),
      padded: getComputedStyle(field).paddingRight,
    };
  });
})()`;

(async () => {
  if (!EDGE) { console.log('SKIP: Edge not found'); return; }
  // 浏览器、调试端口、登录与导航都在 web/lib/cdp.cjs 里（原来每个脚本各复制一遍）。
  const { profile, child, ws } = await launchAndOpen({ port: 9499 });
  try {

    for (const [mode, label] of [['generate', '生图'], ['video', '生视频']]) {
      await evaluate(ws, `(document.querySelector('.local-mode-button[data-mode="${mode}"]') || {click(){}}).click(), true`);
      await sleep(4500);
      const survey = (await evaluate(ws, SURVEY)).filter(entry => entry.visible);
      check(`${label}：至少一个提示词框有麦克风`, survey.some(entry => entry.hasMic),
        `${survey.length} 个可见框`);
      for (const entry of survey.filter(item => item.hasMic)) {
        const name = entry.cls.split(' ')[0];
        check(`${label} ${name}：按钮在框内右下角`,
          entry.insideRight >= 4 && entry.insideRight <= 18 && entry.insideBottom >= 4 && entry.insideBottom <= 18,
          `右距 ${entry.insideRight}px · 下距 ${entry.insideBottom}px`);
        check(`${label} ${name}：是麦克风图标`, entry.hasSvg === true && entry.iconOnly === true);
        check(`${label} ${name}：点得到（没被遮住）`, entry.hit === true);
        check(`${label} ${name}：给按钮留了位置`, parseInt(entry.padded, 10) >= 30, entry.padded);
      }
    }

    // 真点一次：文字要进生图提示词框，而不是聊天输入框。
    await evaluate(ws, `(document.querySelector('.local-mode-button[data-mode="generate"]') || {click(){}}).click(), true`);
    await sleep(4000);
    // 有自己麦克风的框，不该再多出一个跟着光标走的浮动按钮（所有者报的"残留上一个语音输入的圆框"）。
    const pill = await evaluate(ws, `(() => {
      const field = document.querySelector('.qg-prompt-input');
      field.focus();
      field.dispatchEvent(new FocusEvent('focusin', { bubbles: true }));
      return new Promise(resolve => setTimeout(() => {
        const primary = document.querySelector('.qv-mic-primary');
        const style = primary ? getComputedStyle(primary) : null;
        const rect = primary ? primary.getBoundingClientRect() : null;
        const visible = !!primary && !primary.hidden && style.display !== 'none'
          && style.visibility !== 'hidden' && Number(style.opacity) > 0.05 && rect.width > 0;
        resolve({ visible, cls: primary ? primary.className : null, hidden: primary ? primary.hidden : null });
      }, 600));
    })()`);
    check('有自己麦克风的框不再冒出浮动圆框', pill.visible === false, JSON.stringify(pill));
    const clicked = await evaluate(ws, `(() => {
      const field = document.querySelector('.qg-prompt-input');
      if (!field) return { ok: false };
      field.value = '';
      const button = document.querySelector('.qv-mic-box[data-box-mic-for="' + field.dataset.boxMic + '"]');
      if (!button) return { ok: false, reason: 'no mic' };
      button.click();
      return { ok: true };
    })()`);
    check('点到生图提示词的麦克风', clicked.ok === true, JSON.stringify(clicked));
    await sleep(2600);
    const dictation = await evaluate(ws, `(() => {
      const field = document.querySelector('.qg-prompt-input');
      const button = document.querySelector('.qv-mic-box[data-box-mic-for="' + field.dataset.boxMic + '"]');
      const composer = document.querySelector('.conversation-chat-form textarea');
      return { field: field.value, mode: button.dataset.mode, composer: composer ? composer.value : null,
               voice: !!(window.QwenVoice && window.QwenVoice.state && (window.QwenVoice.state.live || window.QwenVoice.state.recorder)) };
    })()`);
    check('识别出的文字进了这个提示词框', (dictation.field || '').includes(PHRASE),
      JSON.stringify(dictation.field || '').slice(0, 48));
    check('没有跑进聊天输入框', !(dictation.composer || '').includes(PHRASE),
      JSON.stringify((dictation.composer || '').slice(0, 24)));
    check('按钮自己显示在录音', dictation.mode === 'recording' && dictation.voice === true, String(dictation.mode));

    await evaluate(ws, `(() => {
      const field = document.querySelector('.qg-prompt-input');
      document.querySelector('.qv-mic-box[data-box-mic-for="' + field.dataset.boxMic + '"]').click();
      return true;
    })()`);
    await sleep(2500);
    const stopped = await evaluate(ws, `(() => {
      const field = document.querySelector('.qg-prompt-input');
      const button = field.parentElement.querySelector('.qv-mic.qv-mic-box');
      return { mode: button.dataset.mode, value: field.value };
    })()`);
    check('再点一次停下，文字还在', stopped.mode === 'idle' && stopped.value.includes(PHRASE),
      `${stopped.mode} · ${JSON.stringify(stopped.value.slice(0, 24))}`);
  } finally {
    // Hand the service back in 纯文本 mode: it was switched to a panel above, and the mode belongs
    // to the service rather than to this browser profile.
    try { await evaluate(ws, `(document.querySelector('.local-mode-button[data-mode="text"]') || {click(){}}).click(), true`); } catch { /* page may be gone */ }
    try { child.kill(); } catch { /* ignore */ }
    console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
    process.exitCode = results.every(Boolean) ? 0 : 1;
  }
})();
