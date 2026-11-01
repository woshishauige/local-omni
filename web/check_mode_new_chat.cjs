/* 纯文本 ↔ 识图 切换后**立刻新开一个对话**：屏幕上不能还留着上一个模式的那串记录。
 *
 * The chat window belongs to the packaged app (tabs in `localStorage["LlamaUi.conversationTabs"]`,
 * messages in IndexedDB), so this drives the real thing: one real turn in 纯文本, then a click on
 * 识图, then it asserts (a) the app's tab is a new one, (b) the previous turn is off the screen, and
 * (c) the gateway filed the new thread under its own id — which is the same rule the ledger and the
 * prompt filter enforce on the server side.
 *
 * Usage: node check_mode_new_chat.cjs [--keep]
 */
const { spawn } = require('node:child_process');
const { results, check, cdp, evaluate, sleep, launchAndOpen } = require('./lib/cdp.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = Number(process.env.NEWCHAT_PORT || 9415);
const ROOT = 'D:\\AI\\Qwen-Local';
const BASE = 'http://127.0.0.1:8081';
const QUESTION = '只回答两个字：收到';
const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(candidate => fs.existsSync(candidate));


/* The gateway's own view of the threads, so the check can name the app's conversation id. */
async function ledgerIds() {
  const link = fs.readFileSync(path.join(ROOT, 'logs', 'admin-link.txt'), 'ascii').trim();
  const response = await fetch(`${BASE}/local/conversation`, { headers: { cookie: await ownerCookie(link) } });
  const body = await response.json().catch(() => ({}));
  return (body.conversations || []).map(item => item.id);
}

let SESSION = '';
async function ownerCookie(link) {
  if (SESSION) return SESSION;
  const login = await fetch(link, { redirect: 'manual' });
  SESSION = login.headers.getSetCookie().map(entry => entry.split(';')[0]).join('; ');
  return SESSION;
}

/* "一条新对话"在应用的标签模型里是最后一项 = `new-chat`：它把每个会话列在
 * `LlamaUi.conversationTabs` 里，新开的那条就追加在末尾。 */
function freshTab(tabs) {
  try {
    const list = JSON.parse(tabs || '[]');
    return Array.isArray(list) && list.length > 0 && list[list.length - 1] === 'new-chat';
  } catch (failure) {
    return false;
  }
}

/* 点模式标签，像人一样：按钮在后端还在收尾时是禁用的（禁用按钮不发 click），所以等它可用再点，
 * 点完没动静就再点一次。 */
async function clickModeTab(ws, mode) {
  for (let i = 0; i < 40; i += 1) {
    const state = JSON.parse(await evaluate(ws, `(() => {
      const button = document.querySelector('.local-mode-button[data-mode=${JSON.stringify(mode)}]');
      return JSON.stringify({
        found: Boolean(button),
        disabled: button ? Boolean(button.disabled) : null,
        pressed: button ? button.getAttribute('aria-pressed') === 'true' : null,
      });
    })()`));
    if (state.found && state.pressed) return true;
    if (state.found && !state.disabled) {
      await evaluate(ws, `(() => {
        document.querySelector('.local-mode-button[data-mode=${JSON.stringify(mode)}]').click();
        return true;
      })()`);
      await sleep(3000);
      const now = await evaluate(ws, `(() => {
        const button = document.querySelector('.local-mode-button[data-mode=${JSON.stringify(mode)}]');
        return button ? button.getAttribute('aria-pressed') === 'true' : false;
      })()`);
      if (now) return true;
      continue;
    }
    await sleep(700);
  }
  return false;
}

async function sendTurn(ws) {
  await evaluate(ws, `(() => {
    const box = document.querySelector('textarea, [contenteditable="true"]');
    if (!box) return false;
    if (box.tagName === 'TEXTAREA') {
      const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
      setter.call(box, ${JSON.stringify(QUESTION)});
      box.dispatchEvent(new Event('input', { bubbles: true }));
    } else {
      box.textContent = ${JSON.stringify(QUESTION)};
      box.dispatchEvent(new Event('input', { bubbles: true }));
    }
    return true;
  })()`);
  await sleep(600);
  await evaluate(ws, `(() => {
    const send = [...document.querySelectorAll('button')]
      .find(node => (node.textContent || '').trim() === '发送' && node.offsetParent !== null);
    if (send && !send.disabled) { send.click(); return true; }
    const box = document.querySelector('textarea, [contenteditable="true"]');
    if (box) box.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    return false;
  })()`);
}

/* What the owner sees, and which app tab it belongs to. */
async function snapshot(ws) {
  return JSON.parse(await evaluate(ws, `(() => {
    const main = document.querySelector('main') || document.body;
    const form = document.querySelector('.conversation-chat-form');
    // The conversation itself, without the composer: a leftover draft in the input box is the
    // app's own business and must not be mistaken for "the previous turn is still on screen".
    const area = (main.innerText || '').replace(form ? form.innerText : '', '');
    const buttons = [...document.querySelectorAll("button, [role='button']")];
    const newChat = buttons.find(node => {
      const name = (node.getAttribute('aria-label') || node.title || node.textContent || '').trim();
      return name === '新对话' && !node.closest('#local-voice-panel, #local-mode-toolbar, .local-voice-panel')
        && !String(node.className || '').startsWith('local-') && !String(node.className || '').startsWith('qv-');
    }) || null;
    return JSON.stringify({
      text: area.replace(/\\s+/g, ' '),
      full: (main.innerText || '').replace(/\\s+/g, ' '),
      tabs: localStorage.getItem('LlamaUi.conversationTabs'),
      mode: (document.querySelector('.local-mode-button[aria-pressed="true"]') || {}).dataset?.mode || null,
      hasNewChat: Boolean(window.LocalModes && window.LocalModes.hasNewChatControl
        && window.LocalModes.hasNewChatControl()),
      freshIntent: localStorage.getItem('qwen.local.freshChatPending'),
      newChatDisabled: newChat ? Boolean(newChat.disabled) : null,
      stopped: /\b停止\b|Processing/.test((main.innerText || '')),
    });
  })()`));
}

(async () => {
  if (!EDGE) { console.log('SKIP: Edge not found'); return; }
  const link = fs.readFileSync(path.join(ROOT, 'logs', 'admin-link.txt'), 'ascii').trim();
  // 浏览器、调试端口、登录与导航都在 web/lib/cdp.cjs 里（原来每个脚本各复制一遍）。
  const { profile, child, ws } = await launchAndOpen({ port: 9499 });
  try {

    // 服务刚重启时工具栏会等后端加载完（模式按钮是禁用的），这时候点标签什么都不会发生——
    // 这是验收脚本自己的等待，不是产品行为。
    let ready = false;
    for (let i = 0; i < 120; i += 1) {
      ready = await evaluate(ws, `(() => {
        const button = document.querySelector('.local-mode-button[data-mode="vision"]');
        return Boolean(button) && button.disabled === false;
      })()`);
      if (ready) break;
      await sleep(1000);
    }
    check('工具栏已经可用（后端加载完成）', ready === true);

    const start = await snapshot(ws);
    check('工具暴露了"新开对话"并能找到应用的按钮', start.hasNewChat === true,
      `hasNewChat=${start.hasNewChat} tab=${start.tabs}`);

    // 1) 纯文本里问一句（真调用模型），屏幕上应当留下这一轮。
    check('起手在纯文本模式', start.mode === 'text', String(start.mode));
    await sendTurn(ws);
    for (let i = 0; i < 40 && !(await snapshot(ws)).text.includes('收到'); i += 1) await sleep(700);
    const answered = await snapshot(ws);
    const textTab = answered.text.includes(QUESTION) || answered.text.includes('收到');
    check('纯文本里的一轮已经出现在屏幕上', textTab, `tab=${answered.tabs}`);
    const typedIds = (await ledgerIds()).filter(id => !before.includes(id));
    check('网关把它记成文字那条线', typedIds.every(id => id.startsWith('text·')),
      JSON.stringify(typedIds));

    // 2) 切到识图：屏幕上应当立刻是一条新对话（应用是"标签"模型，旧的仍挂在标签栏里）。
    await clickModeTab(ws, 'vision');
    if (process.argv.includes('--timeline')) {
      // 诊断：把切换后的标签列表/页面代次/待办按时间打出来，看清"新对话什么时候被还原"。
      console.log('      时间   标签列表                                    代次 待办 模式');
      for (let i = 0; i < 40; i += 1) {
        const row = await evaluate(ws, `(() => JSON.stringify({
          tabs: localStorage.getItem('LlamaUi.conversationTabs'),
          origin: Math.round(performance.timeOrigin % 100000),
          intent: localStorage.getItem('qwen.local.freshChatPending'),
          mode: (document.querySelector('.local-mode-button[aria-pressed="true"]') || {}).dataset?.mode || null,
        }))()`);
        const value = JSON.parse(row);
        console.log(`      ${String(i * 0.5).padStart(4)}s  ${String(value.tabs).slice(0, 44).padEnd(44)}`
          + ` ${value.origin} ${value.intent ? '有' : '无'}  ${value.mode}`);
        await sleep(500);
      }
    }
    // Wait for the goal state, not for a fixed delay: the switch reloads the page, and the new
    // chat is chased across that reload.
    let switched = null;
    for (let i = 0; i < 30; i += 1) {
      await sleep(700);
      switched = await snapshot(ws);
      // 等它把新对话画出来：切模式会重载页面，标签先变、空态后渲染，采样太早会看到一块空白。
      if (switched.mode === 'vision' && freshTab(switched.tabs)
          && switched.text.includes('输入消息')) break;
    }
    check('已经切到识图', switched.mode === 'vision', String(switched.mode));
    check('应用里已经是一条新对话', freshTab(switched.tabs),
      `${answered.tabs} → ${switched.tabs}｜按钮禁用=${switched.newChatDisabled}`
        + `｜待办=${switched.freshIntent}｜还在生成=${switched.stopped}`);
    // 空态文案只会在一条没有消息的会话上出现；旧会话仍挂在应用的标签栏里（那是它自己保留的
    // 入口，不是"数据传过来了"），所以判据是"当前这条是空的"，而不是"页面上找不到旧文字"。
    check('屏幕上当前是空的新对话（旧对话不在正文里）',
      switched.text.includes('输入消息'), switched.text.slice(-90));

    // 3) 切回纯文本：同样立刻新开，而不是回到刚才那条线。
    await clickModeTab(ws, 'text');
    let back = null;
    for (let i = 0; i < 30; i += 1) {
      await sleep(700);
      back = await snapshot(ws);
      if (back.mode === 'text' && freshTab(back.tabs)) break;
    }
    check('切回纯文本也是新对话', freshTab(back.tabs) && back.text.includes('输入消息'),
      `${switched.tabs} → ${back.tabs}｜正文=${back.text.slice(-60)}`);

    // 4) 语音页不属于这条规则（它本来就新开自己那条线），但它也不该被这次改动弄坏。
    await evaluate(ws, `(() => {
      const tab = document.querySelector('.local-mode-button[data-mode="voice"]');
      if (tab) tab.click();
      return true;
    })()`);
    await sleep(2500);
    const voice = JSON.parse(await evaluate(ws, `(() => JSON.stringify({
      panel: Boolean(document.querySelector('#local-voice-panel')),
      hidden: (document.querySelector('#local-voice-panel') || {}).hidden === true,
    }))()`));
    check('语音页仍能打开（没被这条规则连带）', voice.panel === true && voice.hidden === false,
      JSON.stringify(voice));
    ws.close();
  } finally {
    child.kill();
    await sleep(700);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (failure) { /* ignore */ }
  }
  console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
  process.exit(results.every(Boolean) ? 0 : 1);
})().catch(error => { console.error('ERROR', error.message); process.exit(1); });
