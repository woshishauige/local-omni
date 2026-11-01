/* 规程到底有没有让模型走上正确的工具路径：走一遍真实界面（真工具、真流式）。
 *
 * 真机问题（2026-10-28 那个五小时的回合）：模型拿到 get_info 的
 * `{"os":"Microsoft Windows ...","cwd":"D:/AI/Qwen-Local"}` 之后照样跑 ls / head / which，
 * 再把 14 KB 的 HTML 读进上下文，最后 400、界面像卡死。网关现在在带工具的回合里注入一份
 * 环境/工具规程（见 agent_briefing.py）；这个脚本从真实输入框发一条需要动手的问题，
 * 看它是不是用对了工具、并且真的给出结论（不再"停在半空"）。
 *
 * 跑法：node web\check_agent_briefing.cjs
 */
const { spawn } = require('node:child_process');
const { results, check, cdp, evaluate, sleep, launchAndOpen } = require('./lib/cdp.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = 9441;
const ROOT = 'D:\\AI\\Qwen-Local';
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const QUESTION = '当前目录下有多少个 .py 文件？只回一个数字，不要解释。';

(async () => {
  // 浏览器、调试端口、登录与导航都在 web/lib/cdp.cjs 里（原来每个脚本各复制一遍）。
  const { profile, child, ws } = await launchAndOpen({ port: 9441 });
  try {
    // 新 profile 的默认权限是"逐次询问"：工具一被调用就弹确认框，没人点就停在"停止"。
    // 面板所在的真实 profile 里早就把这几项加进 alwaysAllowedTools 了，这里照着配一次。
    await evaluate(ws, `(() => {
      const allowed = ['server:read_file', 'server:glob_search', 'server:grep',
        'server:exec_shell_command', 'server:write_file', 'server:edit_file', 'server:get_info',
        'browser:run_javascript', 'browser:datetime', 'browser:media'];
      localStorage.setItem('LlamaUi.alwaysAllowedTools', JSON.stringify(allowed));
      localStorage.setItem('LlamaUi.disabledToolCategories', JSON.stringify([]));
      return localStorage.getItem('LlamaUi.alwaysAllowedTools').length;
    })()`);
    await cdp(ws, 'Page.reload', {});
    await sleep(9000);

    const typed = await evaluate(ws, `(() => {
      const area = document.querySelector('textarea[placeholder], [contenteditable="true"]');
      if (!area) return 'no composer';
      area.focus();
      if (area.tagName === 'TEXTAREA') {
        area.value = ${JSON.stringify(QUESTION)};
        area.dispatchEvent(new Event('input', { bubbles: true }));
      } else {
        document.execCommand('insertText', false, ${JSON.stringify(QUESTION)});
        area.dispatchEvent(new Event('input', { bubbles: true }));
      }
      return area.tagName;
    })()`);
    console.log('composer:', typed);

    await evaluate(ws, `(() => {
      const send = [...document.querySelectorAll('button')].find(b => /send|发送/i.test(b.getAttribute('aria-label') || b.title || ''));
      if (send) { send.click(); return 'clicked'; }
      const area = document.querySelector('textarea[placeholder], [contenteditable="true"]');
      area.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
      return 'enter';
    })()`);

    let body = '';
    let idle = 0;
    let approvals = 0;
    for (let step = 0; step < 180; step++) {
      await sleep(1000);
      // 默认权限是"逐次询问"：每次工具调用都会弹一个确认框，没人点就一直停在"停止"。
      // 这里替所有者点"允许一次"，只点授权、不点拒绝。
      const clicked = await evaluate(ws, `(() => {
        const button = [...document.querySelectorAll('button')]
          .find(b => /^允许/.test((b.innerText || '').trim()));
        if (button) { button.click(); return button.innerText.trim(); }
        return '';
      })()`);
      if (clicked) { approvals += 1; console.log('approved:', clicked); }
      // "停止"按钮在生成时出现、收尾后消失：它连续两秒不在，这一轮才算真的结束。
      const generating = await evaluate(ws, `!![...document.querySelectorAll('button')]
        .find(b => (b.innerText || '').trim() === '停止')`);
      body = await evaluate(ws, 'document.body.innerText');
      idle = generating ? 0 : idle + 1;
      if (idle >= 2 && step > 6) break;
    }
    console.log('approvals:', approvals, 'idle:', idle);

    // 界面文本里挤着侧边栏和设置项，最后的结论从这次会话的消息节点里读，最准。
    const dump = JSON.parse(await evaluate(ws, `(async () => {
      const out = { conversations: [], messages: [] };
      const list = await indexedDB.databases();
      for (const info of list) {
        const db = await new Promise(res => { const r = indexedDB.open(info.name); r.onsuccess = () => res(r.result); });
        for (const store of Array.from(db.objectStoreNames)) {
          if (!['conversations', 'messages'].includes(store)) continue;
          const rows = await new Promise(res => {
            const tx = db.transaction(store, 'readonly');
            const req = tx.objectStore(store).getAll();
            req.onsuccess = () => res(req.result);
          });
          if (store === 'conversations') out.conversations = rows.map(r => ({ id: r.id, name: r.name }));
          if (store === 'messages') out.messages = rows.map(r => ({
            role: r.role, type: r.type, id: r.id, parent: r.parent,
            content: typeof r.content === 'string' ? r.content : JSON.stringify(r.content),
            calls: (r.toolCalls || []).map ? undefined : undefined,
          }));
        }
        db.close();
      }
      return JSON.stringify(out);
    })()`));

    const assistant = dump.messages.filter(m => m.role === 'assistant' && m.content).pop();
    const tools = dump.messages.filter(m => m.role === 'tool').map(m => String(m.content).slice(0, 80));
    const answer = assistant ? String(assistant.content).trim() : '';
    console.log('conversations:', dump.conversations.length, 'messages:', dump.messages.length);
    console.log('tool results:', tools.map(t => t.replace(/\s+/g, ' ')).join(' / ').slice(0, 300));
    console.log('answer:', answer.replace(/\s+/g, ' ').slice(0, 300));
    fs.writeFileSync(path.join(__dirname, 'agent_briefing_run.json'), JSON.stringify(dump, null, 2), 'utf8');
    const numbered = /(^|\D)\d+(\D|$)/.test(answer) && answer.length < 60;
    console.log('RESULT:', numbered && tools.length > 0 ? 'PASS' : 'CHECK');
  } finally {
    child.kill();
    await sleep(500);
    fs.rmSync(profile, { recursive: true, force: true });
  }
})().catch(error => { console.error('ERROR', error.message); process.exit(1); });
