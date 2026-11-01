/* The global tool-permission switch: does it really turn off the per-call question?
 *
 * The chat UI remembers "always allow" per tool in localStorage as
 * ["server:read_file", ...] — "allow once" is simply the absence of that entry, which is
 * why it asks again. So the switch is only correct if it leaves that list holding every
 * tool the gateway offers, and leaves it empty again when switched off. This drives the
 * real control in a real browser and reads the storage afterwards.
 *
 * Usage: node check_tool_permission.cjs [port]
 */
const { spawn } = require('node:child_process');
const { results, check, cdp, evaluate, sleep, launchAndOpen } = require('./lib/cdp.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = Number(process.argv[2] || 9444);
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

    const read = () => evaluate(ws, `(() => {
      const button = document.querySelector('.local-mode-permission');
      const dialog = document.querySelector('.local-permission-dialog');
      let allowed = null;
      try { allowed = JSON.parse(localStorage.getItem('LlamaUi.alwaysAllowedTools') || 'null'); } catch { allowed = 'unreadable'; }
      const flags = {};
      try {
        for (let i = 0; i < localStorage.length; i++) {
          const key = localStorage.key(i);
          if (key && key.startsWith('qwen.local.toolPermission.conversation.')) flags[key.split('conversation.')[1]] = localStorage.getItem(key);
        }
      } catch { /* private mode */ }
      return JSON.stringify({
        exists: !!button,
        inComposer: !!(button && button.closest('#local-composer-tools') && button.closest('.conversation-chat-form')),
        inToolbar: !!(button && button.closest('#local-mode-toolbar')),
        granted: button ? button.dataset.granted : null,
        pressed: button ? button.getAttribute('aria-pressed') : null,
        text: button ? button.textContent.trim() : null,
        title: button ? button.title : null,
        dialogOpen: dialog ? !dialog.hidden : null,
        dialogText: dialog ? dialog.textContent.slice(0, 60) : null,
        allowed,
        flags,
      });
    })()`);

    const gatewayTools = JSON.parse(await evaluate(ws,
      `fetch('/tools', { cache: 'no-store' }).then(r => r.json()).then(t => JSON.stringify(t.map(x => (x.type || 'server') + ':' + x.tool)))`));
    /* 浏览器工具不在网关的 /tools 里——应用的提问里却有 read_media。这条断言就是"已全局放行
       还问一次"的回归线：名单里没有浏览器工具，放行就是假的。 */
    const browserTools = ['browser:read_media', 'browser:get_datetime', 'browser:run_javascript'];

    const initial = JSON.parse(await read());
    check('the switch lives in the conversation box', initial.exists === true && initial.inComposer === true,
      `composer=${initial.inComposer}`);
    check('it is no longer a page-wide toolbar switch', initial.inToolbar === false);
    check('it starts in the asking state', initial.granted === 'false' && initial.pressed === 'false',
      `${initial.text} · pressed=${initial.pressed}`);
    check('it explains what it does on hover',
      typeof initial.title === 'string' && initial.title.includes('询问'), initial.title);
    check('nothing is pre-authorised', initial.allowed === null || initial.allowed.length === 0,
      JSON.stringify(initial.allowed));

    // Turning it on must ask first: this is the "are you sure" for real privileges.
    await evaluate(ws, `document.querySelector('.local-mode-permission').click()`);
    await sleep(400);
    const asking = JSON.parse(await read());
    check('clicking it opens a confirmation instead of granting', asking.dialogOpen === true,
      asking.dialogText);
    check('the confirmation names what gets granted',
      asking.dialogText.includes('本对话') || asking.dialogText.includes('工具'), asking.dialogText);
    check('still not granted while the dialog is open',
      asking.granted === 'false' && (asking.allowed === null || asking.allowed.length === 0));

    // Cancelling must change nothing.
    await evaluate(ws, `document.querySelector('.local-permission-cancel').click()`);
    await sleep(300);
    const cancelled = JSON.parse(await read());
    check('cancelling leaves it asking', cancelled.granted === 'false' && cancelled.dialogOpen === false);

    // Confirm: every tool the gateway offers — and every browser tool the app owns — ends up in
    // the list the chat UI reads before it decides to ask.
    await evaluate(ws, `document.querySelector('.local-mode-permission').click()`);
    await sleep(300);
    await evaluate(ws, `document.querySelector('.local-permission-confirm').click()`);
    await sleep(1200);
    const granted = JSON.parse(await read());
    const wanted = gatewayTools.concat(browserTools);
    const missing = wanted.filter(key => !(granted.allowed || []).includes(key));
    check('confirming turns the switch on', granted.granted === 'true' && granted.pressed === 'true',
      granted.text);
    check('every gateway tool is now always allowed', missing.filter(k => k.startsWith('server:')).length === 0,
      `${(granted.allowed || []).length} keys`);
    check('the browser tools are allowed too (this is why it kept asking)',
      browserTools.every(key => (granted.allowed || []).includes(key)),
      missing.length ? 'missing ' + missing.join(',') : browserTools.join(', '));
    check('the dangerous ones are included on purpose',
      ['server:exec_shell_command', 'server:write_file', 'server:edit_file'].every(key => (granted.allowed || []).includes(key)),
      (granted.allowed || []).filter(k => /exec|write|edit/.test(k)).join(', '));
    check('the choice is remembered for this conversation',
      (granted.flags || {})['new-chat'] === 'true', JSON.stringify(granted.flags));

    // It has to survive a reload: the point is not being asked again after a restart.
    await cdp(ws, 'Page.navigate', { url });
    await sleep(9000);
    const reloaded = JSON.parse(await read());
    check('it is still on after a reload', reloaded.granted === 'true',
      `${reloaded.text} · ${(reloaded.allowed || []).length} keys`);
    check('and the browser tools survived the reload',
      browserTools.every(key => (reloaded.allowed || []).includes(key)));

    // And switching back off must restore the questions.
    await evaluate(ws, `document.querySelector('.local-mode-permission').click()`);
    await sleep(900);
    const off = JSON.parse(await read());
    check('switching it off asks again as before',
      off.granted === 'false' && wanted.every(key => !(off.allowed || []).includes(key)),
      `${(off.allowed || []).length} keys left`);
    check('the mode toolbar is otherwise untouched',
      await evaluate(ws, `!!document.querySelector('.local-model-select') && !!document.querySelector('.local-mode-segments')`));

    ws.close();
  } finally {
    child.kill();
    await sleep(700);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (failure) { /* Edge may hold a handle */ }
  }
  console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
  process.exit(results.every(Boolean) ? 0 : 1);
})().catch(error => { console.error('ERROR', error.message); process.exit(1); });
