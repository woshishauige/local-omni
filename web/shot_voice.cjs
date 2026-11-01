/* Screenshot the voice view, so its layout can be judged by looking at it.
 *
 * The browser checks assert numbers; numbers said the rail was 244px and the transcript 1166px
 * while the page still read as "history and voice settings are mixed into one column". This
 * tool exists for that gap: it seeds realistic conversations, opens the voice view the way the
 * owner does, and writes PNGs of the panel, of the rail, and of the settings drawer.
 *
 * Usage: node shot_voice.cjs [outDir] [--keep]
 *   --keep   leave the seeded conversations in the history (default: delete them again)
 */
const { spawn } = require('node:child_process');
const { results, check, cdp, evaluate, sleep, launchAndOpen } = require('./lib/cdp.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const OUT = process.argv[2] && !process.argv[2].startsWith('--')
  ? process.argv[2] : path.join(__dirname, 'shots');
const KEEP = process.argv.includes('--keep');
const PORT = Number(process.env.SHOT_PORT || 9412);
const ROOT = 'D:\\AI\\Qwen-Local';
const BASE = 'http://127.0.0.1:8081';
const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(candidate => fs.existsSync(candidate));


/* Realistic threads, because an empty rail hides exactly the problems worth seeing: a long
 * title, a two-line meta, one active row, and enough rows to need scrolling. */
const SEEDS = [
  ['shot-check-weekly', [['user', '帮我把这周的周报润色一下，别太官腔'], ['assistant', '好的，先给我原始版本，我按段落改。']]],
  ['shot-check-circuit', [['user', '这张图里的电路是怎么接的'], ['assistant', '电源正极经限流电阻接到 LED 阳极，阴极回地。']]],
  ['shot-check-travel', [['user', '上周的旅行计划还有吗'], ['assistant', '有的，三天两晚，第一天在市区。']]],
  ['shot-check-python', [['user', '写一段 Python 批量改文件名'], ['assistant', '用 pathlib 遍历，按序号重命名即可。']]],
];

async function seed() {
  const link = fs.readFileSync(path.join(ROOT, 'logs', 'admin-link.txt'), 'ascii').trim();
  const token = new URL(link).searchParams.get('admin');
  const login = await fetch(`${BASE}/?admin=${encodeURIComponent(token)}`, { redirect: 'manual' });
  const session = login.headers.getSetCookie().map(entry => entry.split(';')[0]).join('; ');
  if (!session) throw new Error('no owner session cookie from the admin link');
  for (const [id, turns] of SEEDS) {
    for (const [role, content] of turns) {
      const response = await fetch(`${BASE}/local/conversation/seed`, {
        method: 'POST',
        headers: { 'content-type': 'application/json', cookie: session },
        body: JSON.stringify({ conversation_id: id, role, content, mode: 'voice' }),
      });
      if (!response.ok) throw new Error('seed failed: HTTP ' + response.status);
    }
  }
  return { session, link };
}

async function cleanup(session) {
  for (const [id] of SEEDS) {
    await fetch(`${BASE}/local/conversation?conversation_id=${encodeURIComponent(id)}`,
      { method: 'DELETE', headers: { cookie: session } }).catch(() => {});
  }
}

async function shoot(ws, file, clip) {
  const params = { format: 'png' };
  if (clip) params.clip = { ...clip, scale: clip.scale || 1 };
  const shot = await cdp(ws, 'Page.captureScreenshot', params);
  fs.writeFileSync(file, Buffer.from(shot.data, 'base64'));
  console.log('  ' + path.basename(file) + (clip ? `  clip=${clip.width}x${clip.height}` : ''));
}

(async () => {
  if (!EDGE) { console.log('SKIP: Edge not found'); return; }
  fs.mkdirSync(OUT, { recursive: true });
  const { session, link } = await seed();
  // 浏览器、调试端口、登录与导航都在 web/lib/cdp.cjs 里（原来每个脚本各复制一遍）。
  const { profile, child, ws } = await launchAndOpen({ port: 9499 });
  try {

    // Enter the way the owner does.
    await evaluate(ws, `(() => {
      const tab = document.querySelector('.local-mode-button[data-mode="voice"]');
      if (!tab) throw new Error('no voice tab');
      tab.click();
      return true;
    })()`);
    await sleep(3500);

    const box = await evaluate(ws, `(() => {
      const panel = document.querySelector('#local-voice-panel');
      const side = panel && panel.querySelector('.local-voice-side');
      const r = side ? side.getBoundingClientRect() : null;
      return JSON.stringify({
        panel: panel ? panel.getBoundingClientRect().toJSON() : null,
        side: r ? { x: r.x, y: r.y, width: r.width, height: r.height } : null,
        rows: panel ? panel.querySelectorAll('.local-voice-history-item').length : 0,
      });
    })()`);
    const geo = JSON.parse(box);
    console.log(`rail=${geo.side ? Math.round(geo.side.width) + 'px' : 'hidden'} rows=${geo.rows}`);

    /* Where every rail block actually lands. The numbers are what makes "history and voice
     * settings are mixed together" a fact rather than an impression: it printed the history
     * list at zero height with the voice chips sitting on top of it. */
    const report = await evaluate(ws, `(() => {
      const panel = document.querySelector('#local-voice-panel');
      const pick = (root, selector) => {
        const node = root ? root.querySelector(selector) : null;
        if (!node) return null;
        const r = node.getBoundingClientRect();
        const style = getComputedStyle(node);
        return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
                 display: style.display, overflow: style.overflowY, flex: style.flex };
      };
      const side = panel && panel.querySelector('.local-voice-side');
      const rows = panel ? [...panel.querySelectorAll('.local-voice-history-item')] : [];
      const root = getComputedStyle(document.documentElement);
      const tokens = {};
      for (const name of ['--background', '--foreground', '--card', '--popover', '--secondary',
                          '--muted', '--muted-foreground', '--border', '--accent', '--primary',
                          '--primary-foreground', '--radius', '--local-toolbar-height']) {
        tokens[name] = root.getPropertyValue(name).trim();
      }
      return JSON.stringify({
        blocks: {
          railHead: pick(side, '.local-voice-rail-head'),
          railSearch: pick(side, '.local-voice-search'),
          historyList: pick(side, '.local-voice-history-list'),
          drawer: pick(panel, '.local-voice-settings'),
          voices: pick(panel, '.local-voice-voices'),
          toggles: pick(panel, '.local-voice-action'),
          facts: pick(panel, '.local-voice-facts'),
          dock: pick(panel, '.local-voice-dock'),
          log: pick(panel, '.local-voice-log'),
        },
        row0: rows.length ? (() => { const r = rows[0].getBoundingClientRect();
          return { y: Math.round(r.y), h: Math.round(r.height), w: Math.round(r.width) }; })() : null,
        tokens,
      });
    })()`);
    const parsed = JSON.parse(report);
    console.log('rail blocks (x,y,w,h):');
    for (const [name, value] of Object.entries(parsed.blocks)) {
      if (!value) { console.log(`  ${name}: —`); continue; }
      console.log(`  ${name.padEnd(14)} ${String(value.x).padStart(4)},${String(value.y).padStart(4)}`
        + ` ${String(value.w).padStart(4)}x${String(value.h).padStart(4)} ${value.display}/${value.overflow}`);
    }
    if (parsed.row0) console.log(`  row0           y=${parsed.row0.y} h=${parsed.row0.h} w=${parsed.row0.w}`);
    if (process.env.SHOT_TOKENS) console.log('tokens:', JSON.stringify(parsed.tokens, null, 1));

    await shoot(ws, path.join(OUT, 'voice-1-panel.png'));

    if (geo.side) {
      await shoot(ws, path.join(OUT, 'voice-2-rail.png'),
        { x: Math.round(geo.side.x), y: Math.round(geo.side.y),
          width: Math.round(geo.side.width) + 8, height: Math.min(620, Math.round(geo.side.height)) });
    }

    // The settings drawer, if this build has one.
    const toggled = await evaluate(ws, `(() => {
      const button = document.querySelector('.local-voice-settings-toggle');
      if (!button) return false;
      button.click();
      return true;
    })()`);
    if (toggled) {
      await sleep(900);
      await shoot(ws, path.join(OUT, 'voice-3-settings.png'));
      const drawer = await evaluate(ws, `(() => {
        const node = document.querySelector('.local-voice-settings');
        if (!node) return null;
        const r = node.getBoundingClientRect();
        return JSON.stringify({ x: r.x, y: r.y, width: r.width, height: r.height });
      })()`);
      if (drawer && JSON.parse(drawer).width) {
        const r = JSON.parse(drawer);
        await shoot(ws, path.join(OUT, 'voice-4-settings-crop.png'),
          { x: Math.round(r.x) - 6, y: Math.round(r.y) - 6,
            width: Math.round(r.width) + 12, height: Math.round(r.height) + 12 });
      }
    } else {
      console.log('  (no settings drawer in this build)');
    }

    // A conversation with turns on screen, which is where the bubbles and clip players live.
    // The drawer is closed first: it covers the right third of the page, and the conversation
    // shot is about the transcript, not about the drawer.
    await evaluate(ws, `(() => {
      const gear = document.querySelector('.local-voice-settings-toggle');
      if (gear && gear.getAttribute('aria-expanded') === 'true') gear.click();
      return true;
    })()`);
    await sleep(500);
    await evaluate(ws, `(async () => {
      const api = window.VoicePanel;
      if (api && api.continueConversation) await api.continueConversation('shot-check-weekly');
      return true;
    })()`);
    await sleep(2200);
    await shoot(ws, path.join(OUT, 'voice-5-conversation.png'));

    // The rail with an inline rename open, which is the only state that shows the input.
    const renaming = await evaluate(ws, `(async () => {
      const panel = document.querySelector('#local-voice-panel');
      const row = [...panel.querySelectorAll('.local-voice-history-item')]
        .find(node => node.dataset.id === 'shot-check-weekly');
      if (!row) return false;
      row.querySelector('.local-voice-history-rename').click();
      await new Promise(resolve => setTimeout(resolve, 400));
      return Boolean(panel.querySelector('.local-voice-history-rename-input'));
    })()`);
    if (renaming) {
      const side = await evaluate(ws, `(() => {
        const r = document.querySelector('#local-voice-panel .local-voice-side').getBoundingClientRect();
        return JSON.stringify({ x: r.x, y: r.y, width: r.width, height: r.height });
      })()`);
      const box = JSON.parse(side);
      await shoot(ws, path.join(OUT, 'voice-6-renaming.png'),
        { x: Math.round(box.x), y: Math.round(box.y), width: Math.round(box.width) + 8,
          height: Math.min(420, Math.round(box.height)) });
      await evaluate(ws, `(() => {
        const input = document.querySelector('.local-voice-history-rename-input');
        if (input) input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        return true;
      })()`);
      await sleep(400);
    }

    // The rail collapsed to its strip, which is the whole point of the collapse control.
    const collapsed = await evaluate(ws, `(async () => {
      const panel = document.querySelector('#local-voice-panel');
      const toggle = panel.querySelector('.local-voice-rail-toggle');
      if (!toggle) return false;
      toggle.click();
      await new Promise(resolve => setTimeout(resolve, 700));
      return true;
    })()`);
    if (collapsed) {
      await shoot(ws, path.join(OUT, 'voice-7-collapsed.png'));
      await evaluate(ws, `(() => {
        const strip = document.querySelector('#local-voice-panel .local-voice-rail-strip');
        const toggle = strip && strip.querySelector('.local-voice-rail-toggle');
        if (toggle) toggle.click();
        return true;
      })()`);
      await sleep(500);
    }
    ws.close();
  } finally {
    child.kill();
    await sleep(700);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (failure) { /* Edge may hold a handle */ }
    if (!KEEP) await cleanup(session);
  }
  console.log('写到 ' + OUT);
})().catch(error => { console.error('ERROR', error.message); process.exit(1); });
