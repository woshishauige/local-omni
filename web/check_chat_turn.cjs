/* The chat path through the modified wrapper: a real turn sent from the page.
 *
 * context-control.js now hands every 409 to the voice module's GPU gate. The risk of that
 * change is breaking ordinary turns, so this types a message into the real composer and
 * waits for an answer.
 */
const { spawn } = require('node:child_process');
const { results, check, cdp, evaluate, sleep, launchAndOpen } = require('./lib/cdp.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = 9433;
const ROOT = 'D:\\AI\\Qwen-Local';
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';

(async () => {
  // 浏览器、调试端口、登录与导航都在 web/lib/cdp.cjs 里（原来每个脚本各复制一遍）。
  const { profile, child, ws } = await launchAndOpen({ port: 9433 });
  try {

    // The composer is a contenteditable region in this app; find whatever accepts text.
    const typed = await evaluate(ws, `(() => {
      const area = document.querySelector('textarea[placeholder], [contenteditable="true"]');
      if (!area) return 'no composer';
      area.focus();
      const text = '只回复两个字：收到';
      if (area.tagName === 'TEXTAREA') {
        area.value = text;
        area.dispatchEvent(new Event('input', { bubbles: true }));
      } else {
        document.execCommand('insertText', false, text);
        area.dispatchEvent(new Event('input', { bubbles: true }));
      }
      return area.tagName + ':' + (area.value || area.textContent || '').slice(0, 20);
    })()`);
    console.log('composer:', typed);

    await evaluate(ws, `(() => {
      const form = document.querySelector('form');
      const send = [...document.querySelectorAll('button')].find(b => /send|发送/i.test(b.getAttribute('aria-label') || b.title || ''));
      if (send) { send.click(); return 'clicked send'; }
      if (form) { form.requestSubmit ? form.requestSubmit() : form.submit(); return 'submitted form'; }
      const area = document.querySelector('textarea[placeholder], [contenteditable="true"]');
      area.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
      return 'pressed enter';
    })()`);

    let answer = '';
    for (let step = 0; step < 40; step++) {
      await sleep(1000);
      const body = await evaluate(ws, `document.body.innerText`);
      const hit = /收到/.test(body.replace('只回复两个字：收到', ''));
      if (hit) { answer = body.slice(0, 200); break; }
    }
    console.log('page shows an answer:', /收到/.test(answer));
    console.log('exceptions during the run:', logs.length ? logs.slice(0, 3) : 'none');
    console.log('sample:', JSON.stringify(answer.slice(-120)));
    console.log('RESULT:', /收到/.test(answer) && logs.length === 0 ? 'PASS' : 'CHECK');
  } finally {
    child.kill();
    await sleep(500);
    fs.rmSync(profile, { recursive: true, force: true });
  }
})().catch(error => { console.error('ERROR', error.message); process.exit(1); });
