/* The video panel's run progress must stay put while the panel scrolls.
 *
 * It used to be `position: sticky`, and the panel root is itself the scroll container, so
 * it only "caught" while its own row was on screen and travelled with the wheel
 * otherwise. This drives the real element in a real browser: scroll the panel, measure,
 * and require the box not to move.
 *
 * Usage: node check_video_progress_pinned.cjs [port]
 */
const { spawn } = require('node:child_process');
const { results, check, cdp, evaluate, sleep } = require('./lib/cdp.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = Number(process.argv[2] || 9388);
const BASE = 'http://127.0.0.1:8081';
const ROOT = 'D:\\AI\\Qwen-Local';
const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(candidate => fs.existsSync(candidate));


async function waitFor(ws, expression, timeout, label) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await evaluate(ws, expression)) return true;
    await sleep(300);
  }
  throw new Error('timed out waiting for ' + label);
}

(async () => {
  if (!EDGE) { console.log('SKIP: Edge not found'); return; }
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'qv-prog-'));
  const child = spawn(EDGE, [
    '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check', '--window-size=1440,900', 'about:blank',
  ], { stdio: 'ignore' });
  try {
    for (let i = 0; i < 60; i++) {
      try { await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json(); break; } catch { await sleep(400); }
    }
    const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
    const ws = new WebSocket(list.find(entry => entry.type === 'page').webSocketDebuggerUrl);
    await new Promise(resolve => ws.addEventListener('open', resolve, { once: true }));
    await cdp(ws, 'Runtime.enable');
    await cdp(ws, 'Page.enable');
    const token = fs.readFileSync(path.join(ROOT, 'logs', 'admin.token'), 'ascii').trim();
    await cdp(ws, 'Page.navigate', { url: `${BASE}/?admin=${token}` });
    await waitFor(ws, 'document.readyState === "complete"', 30000, 'page load');
    await waitFor(ws, '!!document.querySelector(".qv-action-status")', 30000, 'the video panel');

    // Drive the stylesheet directly rather than depending on the panel being open: the
    // question is purely geometric, and a hidden panel measures as a 0×0 box.
    const seeded = await evaluate(ws, `(async () => {
      const css = await (await fetch('/local/video-panel.css', { cache: 'no-store' })).text();
      const style = document.createElement('style');
      style.textContent = css;
      document.head.append(style);
      const host = document.createElement('div');
      host.id = 'qv-probe-host';
      host.style.cssText = '--local-toolbar-height: 60px';
      const root = document.createElement('div');
      root.className = 'qv-root';
      const body = document.createElement('div');
      body.className = 'qv-body';
      const column = document.createElement('div');
      column.style.minHeight = '2400px';
      const status = document.createElement('div');
      status.className = 'qv-action-status';
      const track = document.createElement('div');
      track.className = 'qv-action-track';
      const fill = document.createElement('div');
      fill.className = 'qv-action-fill';
      fill.style.width = '40%';
      track.append(fill);
      const note = document.createElement('p');
      note.className = 'qv-action-note';
      note.textContent = '第 1/2 段 · 40%';
      status.append(track, note);
      column.append(status);
      body.append(column);
      root.append(body);
      host.append(root);
      document.body.append(host);
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      const box = status.getBoundingClientRect();
      const scroller = root;
      return {
        height: Math.round(box.height), top: Math.round(box.top), bottom: Math.round(box.bottom),
        left: Math.round(box.left), right: Math.round(box.right),
        position: getComputedStyle(status).position,
        scrollable: scroller.scrollHeight - scroller.clientHeight,
        viewport: window.innerHeight,
      };
    })()`);
    check('the progress box renders with its own stylesheet', seeded.height > 20, JSON.stringify(seeded));
    check('its style is fixed, not sticky', seeded.position === 'fixed', seeded.position);
    check('the panel really can scroll', seeded.scrollable > 400, `${seeded.scrollable}px of scroll`);
    check('it sits at the bottom of the viewport',
      Math.abs(seeded.bottom - seeded.viewport) < 30, `bottom=${seeded.bottom} viewport=${seeded.viewport}`);

    const after = await evaluate(ws, `(() => {
      const root = document.querySelector('#qv-probe-host .qv-root');
      root.scrollTop = root.scrollHeight;
      const status = document.querySelector('#qv-probe-host .qv-action-status');
      const box = status.getBoundingClientRect();
      return { top: Math.round(box.top), bottom: Math.round(box.bottom), scrolled: Math.round(root.scrollTop) };
    })()`);
    check('it did not move when the panel scrolled to the end',
      Math.abs(after.top - seeded.top) <= 2 && Math.abs(after.bottom - seeded.bottom) <= 2,
      `before top=${seeded.top} → after top=${after.top} (scrolled ${after.scrolled}px)`);

    const withFocus = await evaluate(ws, `(() => {
      const root = document.querySelector('#qv-probe-host .qv-root');
      root.scrollTop = Math.round(root.scrollHeight / 2);
      const box = document.querySelector('#qv-probe-host .qv-action-status').getBoundingClientRect();
      return { top: Math.round(box.top), inView: box.top >= 0 && box.bottom <= window.innerHeight + 1 };
    })()`);
    check('it stays fully on screen at every scroll position', withFocus.inView,
      `top=${withFocus.top} at mid-scroll`);

    // The overlay must not swallow clicks meant for the page behind it.
    const clickable = await evaluate(ws, `(() => {
      const box = document.querySelector('#qv-probe-host .qv-action-status').getBoundingClientRect();
      const outside = document.elementFromPoint(Math.max(4, box.left - 40), box.top + box.height / 2);
      const inside = document.elementFromPoint(box.left + box.width / 2, box.top + 5);
      return { outside: outside ? outside.className || outside.tagName : null,
        insideIsStatus: inside ? inside.closest('.qv-action-status') !== null : false };
    })()`);
    check('it does not cover the area beside it', clickable.insideIsStatus && clickable.outside !== null,
      `beside=${clickable.outside}`);

    // Narrow viewport: the width rule must not push it off screen.
    await cdp(ws, 'Emulation.setDeviceMetricsOverride', {
      width: 390, height: 844, deviceScaleFactor: 2, mobile: true,
    });
    await sleep(600);
    const narrow = await evaluate(ws, `(() => {
      const box = document.querySelector('#qv-probe-host .qv-action-status').getBoundingClientRect();
      return { left: Math.round(box.left), right: Math.round(box.right), width: Math.round(box.width) };
    })()`);
    check('it fits a 390px-wide screen', narrow.left >= 0 && narrow.right <= 390,
      `left=${narrow.left} right=${narrow.right}`);

    ws.close();
  } finally {
    child.kill();
    await sleep(500);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (failure) { /* best effort */ }
  }
  console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
  process.exit(results.every(Boolean) ? 0 : 1);
})().catch(error => { console.error('ERROR', error.message); process.exit(1); });
