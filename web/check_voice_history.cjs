/* The voice panel must show the conversation, and must not call itself "实时的".
 *
 * Two reports are pinned here. The panel rendered only `state.turns`, which starts empty on
 * every page load, so the shared history the gateway keeps was never visible even though the
 * model still had it — the panel looked like it had lost the thread. And its title still said
 * "实时语音对话", which promises a latency guarantee the mode does not make.
 *
 * A conversation is seeded through the gateway first, so this checks what the panel does with
 * history that exists rather than with a stub.
 *
 * Destructive by design: its last two assertions are "一键清空" and "单条删除", so the shared
 * history is empty when it finishes. Run it on a machine where that history does not matter.
 *
 * Usage: node check_voice_history.cjs [port]
 */
const { spawn } = require('node:child_process');
const { results, check, cdp, evaluate, sleep, launchAndOpen } = require('./lib/cdp.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = Number(process.argv[2] || 9411);
const ROOT = 'D:\\AI\\Qwen-Local';
const BASE = 'http://127.0.0.1:8081';
const CONVERSATION = 'voice-history-check';
const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(candidate => fs.existsSync(candidate));


/* Seed the conversation through the gateway, because the point is to check what the panel
 * does with history that already exists — a panel-only test would have to fake the ledger. */
async function seed() {
  const link = fs.readFileSync(path.join(ROOT, 'logs', 'admin-link.txt'), 'ascii').trim();
  const token = new URL(link).searchParams.get('admin');
  // `redirect: 'manual'`, because the session cookie is set on the 303 that the token
  // exchange answers with: following it lands on the final page, whose headers no longer
  // carry the cookie, and every later request then looks unauthenticated (a 401 here).
  const login = await fetch(`${BASE}/?admin=${encodeURIComponent(token)}`, { redirect: 'manual' });
  const session = login.headers.getSetCookie().map(entry => entry.split(';')[0]).join('; ');
  if (!session) throw new Error('no owner session cookie from the admin link');
  const conversations = [
    { id: CONVERSATION, turns: [['user', '这是历史里的一句话'], ['assistant', '这是历史里的回答']] },
    // A second, older-looking thread: the list has to offer a choice, not one row.
    { id: 'voice-history-check-older', turns: [['user', '上一周的另一个话题'], ['assistant', '上一周的回答']] },
    // A deliberately long opening line: the title taken from it must be cut to ten characters.
    { id: 'voice-history-check-long', turns: [['user', '帮我把这周的周报润色一下，别太官腔'], ['assistant', '好，先给我原始版本。']] },
  ];
  for (const conversation of conversations) {
    for (const [role, content] of conversation.turns) {
      const response = await fetch(`${BASE}/local/conversation/seed`, {
        method: 'POST',
        headers: { 'content-type': 'application/json', cookie: session },
        body: JSON.stringify({ conversation_id: conversation.id, role, content, mode: 'voice' }),
      });
      if (!response.ok) throw new Error('seed failed: HTTP ' + response.status);
    }
  }
  return session;
}

/* Seed two turns of a written conversation, so the panel has something real to show. */

(async () => {
  if (!EDGE) { console.log('SKIP: Edge not found'); return; }
  await seed();
  // 浏览器、调试端口、登录与导航都在 web/lib/cdp.cjs 里（原来每个脚本各复制一遍）。
  const { profile, child, ws } = await launchAndOpen({ port: 9499 });
  try {

    const titled = await evaluate(ws, `(() => {
      const button = document.querySelector('.local-voice-launch');
      const panel = document.querySelector('#local-voice-panel');
      const strong = panel && panel.querySelector('strong');
      return JSON.stringify({
        launch: button ? button.textContent.trim() : null,
        title: button ? button.title : null,
        heading: strong ? strong.textContent.trim() : null,
        aria: panel ? panel.getAttribute('aria-label') : null,
        hasPanel: !!panel,
      });
    })()`);
    const copy = JSON.parse(titled);
    check('语音入口按钮存在', copy.launch !== null, String(copy.launch));
    check('按钮文案不再带"实时"', copy.launch && !copy.launch.includes('实时'), String(copy.launch));
    check('面板标题改为"语音对话"', copy.heading === '语音对话', String(copy.heading));
    check('悬浮标题与无障碍名同步', copy.title === '语音对话' && copy.aria === '语音对话',
      `${copy.title} / ${copy.aria}`);

    // Open the panel the way the owner does — the voice tab in the toolbar. The launcher inside
    // the panel is hidden (voice is entered from the mode tabs, like every other view), so
    // clicking it would open nothing and every geometry check below would measure a hidden box.
    await evaluate(ws, `(() => {
      const tab = document.querySelector('.local-mode-button[data-mode="voice"]');
      if (!tab) throw new Error('no voice tab');
      tab.click();
      return true;
    })()`);
    await sleep(3000);

    const opened = JSON.parse(await evaluate(ws, `(() => {
      const launch = document.querySelector('.local-voice-launch');
      const panel = document.querySelector('#local-voice-panel');
      return JSON.stringify({
        visible: panel ? !panel.hidden : false,
        launchDisplay: launch ? getComputedStyle(launch).display : null,
      });
    })()`));
    check('从模式标签进入后语音页可见', opened.visible === true, `visible=${opened.visible}`);

    /* --- opening voice shows a clean page, not the chat thread ----------------------- */
    const shown = JSON.parse(await evaluate(ws, `(() => {
      const panel = document.querySelector('#local-voice-panel');
      const bubbles = panel ? [...panel.querySelectorAll('.local-voice-bubble')] : [];
      const rows = panel ? [...panel.querySelectorAll('.local-voice-turn')] : [];
      const text = rows.map(row => row.textContent.trim()).join(' | ');
      return JSON.stringify({
        bubbles: bubbles.length,
        text,
        rows: rows.length,
        // The rail has to carry the history instead, or starting clean would lose it.
        rail: panel ? panel.querySelectorAll('.local-voice-history-item').length : 0,
      });
    })()`));
    check('进入语音是空白页，不铺开聊天历史',
      shown.bubbles === 0 && !shown.text.includes('这是历史里的一句话'),
      `bubbles=${shown.bubbles} rows=${shown.rows}`);
    check('历史改由左侧栏承载', shown.rail >= 2, `${shown.rail} 个会话`);

    /* --- it is a page of its own, and the rail is on the left ------------------------- */
    const layout = JSON.parse(await evaluate(ws, `(() => {
      const panel = document.querySelector('#local-voice-panel');
      const side = panel ? panel.querySelector('.local-voice-side') : null;
      const main = panel ? panel.querySelector('.local-voice-log') : null;
      const style = panel ? getComputedStyle(panel) : null;
      const sideBox = side ? side.getBoundingClientRect() : null;
      const mainBox = main ? main.getBoundingClientRect() : null;
      return JSON.stringify({
        position: style ? style.position : null,
        top: panel ? Math.round(panel.getBoundingClientRect().top) : null,
        height: panel ? Math.round(panel.getBoundingClientRect().height) : null,
        viewport: window.innerHeight,
        viewportWidth: window.innerWidth,
        // The rail is hidden below this width by design, so the geometry assertions have to
        // know which layout they are looking at rather than assuming the wide one.
        narrow: window.innerWidth <= 860,
        sideWidth: sideBox ? Math.round(sideBox.width) : null,
        // The panel must not leave the chat app visible underneath it.
        overlay: style ? style.position === 'fixed' : false,
        sideLeft: sideBox ? Math.round(sideBox.left) : null,
        mainLeft: mainBox ? Math.round(mainBox.left) : null,
        toolbarHeight: (() => {
          const raw = getComputedStyle(document.documentElement).getPropertyValue('--local-toolbar-height');
          const value = parseFloat(raw);
          return Number.isFinite(value) ? Math.round(value) : null;
        })(),
      });
    })()`));

    check('语音是一个独立整屏页（不是盖在聊天框上的块）', layout.overlay === true,
      `position=${layout.position}`);
    check('整屏页从工具栏下方铺满到底部',
      layout.toolbarHeight !== null && Math.abs(layout.top - layout.toolbarHeight) <= 2
      && layout.height >= layout.viewport - layout.toolbarHeight - 4,
      `top=${layout.top} toolbar=${layout.toolbarHeight} height=${layout.height} viewport=${layout.viewport}`);
    check('对话历史栏在左侧、正文在其右',
      layout.narrow
        ? layout.sideWidth === 0 || layout.sideLeft === null
        : (layout.sideLeft !== null && layout.mainLeft !== null
           && layout.sideLeft < layout.mainLeft && layout.sideWidth > 100),
      `width=${layout.viewportWidth} narrow=${layout.narrow} side=${layout.sideLeft}(${layout.sideWidth}px) main=${layout.mainLeft}`);

    /* --- one job per surface: the rail is the history, settings are a drawer -------------
     * The owner's report was "历史对话和音色还混在一起，排版这么乱": four blocks shared the one
     * 244px column until the history list measured 0px tall with its rows squeezed to 13px
     * slivers. These assertions are what keeps them apart from now on. */
    const surfaces = JSON.parse(await evaluate(ws, `(() => {
      const panel = document.querySelector('#local-voice-panel');
      const side = panel.querySelector('.local-voice-side');
      const gear = panel.querySelector('.local-voice-settings-toggle');
      const drawer = panel.querySelector('.local-voice-settings');
      return JSON.stringify({
        railVoices: side.querySelectorAll('.local-voice-voice').length,
        railFacts: side.querySelectorAll('.local-voice-facts').length,
        railToggles: side.querySelectorAll('.local-voice-action').length,
        railRows: side.querySelectorAll('.local-voice-history-item').length,
        rowHeight: (() => {
          const row = side.querySelector('.local-voice-history-item');
          return row ? Math.round(row.getBoundingClientRect().height) : 0;
        })(),
        drawerHidden: drawer.hidden,
        voices: drawer.querySelectorAll('.local-voice-voice').length,
        toggles: drawer.querySelectorAll('.local-voice-action').length,
        facts: drawer.querySelectorAll('.local-voice-facts').length,
        expanded: gear.getAttribute('aria-expanded'),
        chips: [...panel.querySelectorAll('.local-voice-status .local-voice-chip')]
          .map(chip => chip.textContent.trim().replace(/\\s+/g, ' ')),
      });
    })()`));
    check('左栏只装对话历史，不再混音色/选项/状态',
      surfaces.railVoices === 0 && surfaces.railFacts === 0 && surfaces.railToggles === 0
      && surfaces.railRows > 0,
      `栏内 音色=${surfaces.railVoices} 状态=${surfaces.railFacts} 开关=${surfaces.railToggles} 会话行=${surfaces.railRows}`);
    check('历史行不再被挤成细条', surfaces.rowHeight >= 40, `行高=${surfaces.rowHeight}px`);
    check('音色/选项/运行状态都在设置抽屉里，默认收起',
      surfaces.drawerHidden === true && surfaces.expanded === 'false'
      && surfaces.voices === 9 && surfaces.toggles === 2 && surfaces.facts === 1,
      `hidden=${surfaces.drawerHidden} 音色=${surfaces.voices} 开关=${surfaces.toggles} 状态=${surfaces.facts}`);
    check('页头直接给出识别/模型/上下文余量', surfaces.chips.length === 3,
      JSON.stringify(surfaces.chips));

    /* --- the meter describes the voice window, not the backend's current one -------------
     * The owner read "130.6k / 131k" on this page as the voice context having been changed
     * (it is 40960 by decision). Before the first spoken turn the shared 9B is still serving
     * the text window, so the page must ask for the locked voice window by name. */
    const windowInfo = JSON.parse(await evaluate(ws, `(async () => {
      const panel = document.querySelector('#local-voice-panel');
      const chip = [...panel.querySelectorAll('.local-voice-status .local-voice-chip')]
        .map(node => node.textContent.replace(/\\s+/g, '')).find(text => text.includes('余量')) || '';
      const meterRow = [...panel.querySelectorAll('.local-voice-facts div')]
        .map(node => node.textContent.replace(/\\s+/g, ''))
        .find(text => text.includes('上下文余量')) || '';
      const id = (window.VoicePanel && window.VoicePanel.state && window.VoicePanel.state.conversation)
        || 'voice-history-check';
      const voice = await fetch('/local/conversation?conversation_id=' + encodeURIComponent(id)
        + '&limit=1&window_kind=voice').then(r => r.json()).catch(() => null);
      const serving = await fetch('/local/conversation?conversation_id=' + encodeURIComponent(id)
        + '&limit=1').then(r => r.json()).catch(() => null);
      return JSON.stringify({
        chip, meterRow,
        voiceWindow: voice && voice.meter ? voice.meter.context_size : null,
        voiceKind: voice && voice.meter ? voice.meter.window_kind : null,
        servingWindow: serving && serving.meter ? serving.meter.context_size : null,
      });
    })()`));
    check('页头与抽屉都按语音窗口（40,960）报余量',
      windowInfo.chip.includes('40,960') && windowInfo.meterRow.includes('40,960'),
      `页头=${windowInfo.chip} 抽屉=${windowInfo.meterRow}`);
    check('网关分得清"语音窗口"与"正在服务的窗口"',
      windowInfo.voiceWindow === 40960 && windowInfo.voiceKind === 'voice'
      && windowInfo.servingWindow !== 40960,
      `语音窗口=${windowInfo.voiceWindow} 服务中窗口=${windowInfo.servingWindow}`);

    const drawer = JSON.parse(await evaluate(ws, `(async () => {
      const panel = document.querySelector('#local-voice-panel');
      const gear = panel.querySelector('.local-voice-settings-toggle');
      const node = panel.querySelector('.local-voice-settings');
      gear.click();
      await new Promise(resolve => setTimeout(resolve, 500));
      const opened = { hidden: node.hidden, expanded: gear.getAttribute('aria-expanded'),
                       width: Math.round(node.getBoundingClientRect().width),
                       rows: node.querySelectorAll('.local-voice-voice').length };
      // Escape unwinds one layer: the drawer first, the page only on a second press.
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      await new Promise(resolve => setTimeout(resolve, 400));
      return JSON.stringify({ opened, afterEsc: node.hidden, panelStillOpen: panel.hidden === false });
    })()`));
    check('点「设置」抽屉打开', drawer.opened.hidden === false && drawer.opened.expanded === 'true'
      && drawer.opened.width > 200 && drawer.opened.rows === 9,
      `hidden=${drawer.opened.hidden} expanded=${drawer.opened.expanded} 宽=${drawer.opened.width} 音色=${drawer.opened.rows}`);
    check('Esc 先关抽屉、不关整个语音页',
      drawer.afterEsc === true && drawer.panelStillOpen === true,
      `抽屉已关=${drawer.afterEsc} 页面仍在=${drawer.panelStillOpen}`);

    // The rail filter narrows the rows in place; the checks below depend on the full list, so
    // the box is cleared again before returning.
    const search = JSON.parse(await evaluate(ws, `(async () => {
      const panel = document.querySelector('#local-voice-panel');
      const box = panel.querySelector('.local-voice-search');
      const count = () => panel.querySelectorAll('.local-voice-history-item').length;
      const all = count();
      box.value = '上一周';
      box.dispatchEvent(new Event('input', { bubbles: true }));
      await new Promise(resolve => setTimeout(resolve, 200));
      const narrowed = count();
      const titles = [...panel.querySelectorAll('.local-voice-history-title')].map(n => n.textContent);
      box.value = '';
      box.dispatchEvent(new Event('input', { bubbles: true }));
      await new Promise(resolve => setTimeout(resolve, 200));
      return JSON.stringify({ all, narrowed, titles, restored: count(), hasBox: Boolean(box) });
    })()`));
    check('历史栏能按标题筛选，清空后恢复',
      search.hasBox === true && search.narrowed < search.all
      && search.titles.every(title => title.includes('上一周')) && search.restored === search.all,
      `全部=${search.all} 筛选后=${search.narrowed} 恢复=${search.restored} 命中=${JSON.stringify(search.titles)}`);

    /* --- titles: 新对话 until the first turn, then ten characters of the owner's words ----- */
    const titles = JSON.parse(await evaluate(ws, `(() => {
      const panel = document.querySelector('#local-voice-panel');
      const rows = [...panel.querySelectorAll('.local-voice-history-item')].map(row => {
        const titleNode = row.querySelector('.local-voice-history-title');
        const when = row.querySelector('.local-voice-history-when');
        const count = row.querySelector('.local-voice-history-count');
        const titleBox = titleNode ? titleNode.getBoundingClientRect() : null;
        const whenBox = when ? when.getBoundingClientRect() : null;
        const countBox = count ? count.getBoundingClientRect() : null;
        return {
          title: titleNode ? titleNode.textContent : null,
          active: row.dataset.active === 'true',
          id: row.dataset.id,
          when: when ? when.textContent : null,
          renaming: row.querySelectorAll('.local-voice-history-rename').length,
          // "Bottom right" is a position, so it is measured: the stamp must sit to the right of
          // the 轮数 label and below the title.
          stampRight: whenBox && countBox ? whenBox.left > countBox.left : null,
          stampBelow: whenBox && titleBox ? whenBox.top >= titleBox.bottom - 2 : null,
        };
      });
      return JSON.stringify({ rows });
    })()`));
    const emptyRow = titles.rows.find(row => row.active);
    // The rail lists scoped ids now (`voice·voice-history-check-long`), so a fixture is matched by
    // suffix rather than by the bare name it was seeded with.
    const longRow = titles.rows.find(row => String(row.id || '').endsWith('voice-history-check-long'));
    check('还没说话的会话就叫「新对话」，不显示内部标识',
      Boolean(emptyRow) && emptyRow.title === '新对话',
      `当前会话标题=${emptyRow ? emptyRow.title : '(无)'}`);
    check('第一句话成为标题，且只取十个字',
      Boolean(longRow) && longRow.title === '帮我把这周的周报润色' && longRow.title.length === 10,
      `长开场白 → ${longRow ? longRow.title : '(无该会话)'}（${longRow ? longRow.title.length : 0} 字）`);
    check('每行都有重命名按钮', titles.rows.every(row => row.renaming === 1),
      JSON.stringify(titles.rows.map(row => row.renaming)));
    check('右下角有日期时间小标',
      titles.rows.every(row => /^\d{2}-\d{2} \d{2}:\d{2}$/.test(row.when || ''))
      && titles.rows.every(row => row.stampRight === true && row.stampBelow === true),
      JSON.stringify(titles.rows.map(row => `${row.title} → ${row.when} right=${row.stampRight} below=${row.stampBelow}`)));

    /* --- rename: in place, saved to the gateway, and final -------------------------------- */
    const renamed = JSON.parse(await evaluate(ws, `(async () => {
      const panel = document.querySelector('#local-voice-panel');
      const row = [...panel.querySelectorAll('.local-voice-history-item')]
        .find(node => String(node.dataset.id || '').endsWith('voice-history-check-long'));
      if (!row) return JSON.stringify({ missing: true });
      row.querySelector('.local-voice-history-rename').click();
      await new Promise(resolve => setTimeout(resolve, 400));
      const input = panel.querySelector('.local-voice-history-rename-input');
      if (!input) return JSON.stringify({ noInput: true });
      const before = input.value;
      input.value = '周报润色';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
      await new Promise(resolve => setTimeout(resolve, 1600));
      const after = [...panel.querySelectorAll('.local-voice-history-item')]
        .find(node => String(node.dataset.id || '').endsWith('voice-history-check-long'));
      const status = await fetch('/local/conversation?scope=voice').then(r => r.json()).catch(() => null);
      const savedMeta = status ? status.conversations.find(item => String(item.id).endsWith('voice-history-check-long')) : null;
      return JSON.stringify({
        before, title: after ? (after.querySelector('.local-voice-history-title') || {}).textContent : null,
        inputGone: !panel.querySelector('.local-voice-history-rename-input'),
        serverTitle: savedMeta ? savedMeta.title : null, renamedFlag: savedMeta ? savedMeta.renamed : null,
      });
    })()`));
    check('重命名就在原位改，回车保存',
      renamed.title === '周报润色' && renamed.inputGone === true && renamed.before === '帮我把这周的周报润色',
      `原名=${renamed.before} 新名=${renamed.title} 输入框已收起=${renamed.inputGone}`);
    check('重命名写回网关并标记为手动命名',
      renamed.serverTitle === '周报润色' && renamed.renamedFlag === true,
      `服务端标题=${renamed.serverTitle} renamed=${renamed.renamedFlag}`);

    /* --- the rail collapses to a strip, and comes back ------------------------------------- */
    const collapsed = JSON.parse(await evaluate(ws, `(async () => {
      const panel = document.querySelector('#local-voice-panel');
      const side = panel.querySelector('.local-voice-side');
      const list = panel.querySelector('.local-voice-history-list');
      const strip = panel.querySelector('.local-voice-rail-strip');
      const open = Math.round(side.getBoundingClientRect().width);
      panel.querySelector('.local-voice-rail-toggle').click();
      await new Promise(resolve => setTimeout(resolve, 500));
      const shut = Math.round(side.getBoundingClientRect().width);
      const hiddenList = list.hidden || getComputedStyle(list).display === 'none';
      const stripShown = strip.hidden === false && getComputedStyle(strip).display !== 'none';
      const stored = localStorage.getItem('qwen.voice.rail');
      const transcript = Math.round(panel.querySelector('.local-voice-log').getBoundingClientRect().width);
      strip.querySelector('.local-voice-rail-toggle').click();
      await new Promise(resolve => setTimeout(resolve, 500));
      const back = Math.round(side.getBoundingClientRect().width);
      return JSON.stringify({ open, shut, back, hiddenList, stripShown, stored, transcript,
        storedAfter: localStorage.getItem('qwen.voice.rail') });
    })()`));
    check('历史栏可以收起来（收到只剩一条把手）',
      collapsed.shut <= 60 && collapsed.shut < collapsed.open && collapsed.hiddenList === true
      && collapsed.stripShown === true,
      `${collapsed.open}px → ${collapsed.shut}px 列表隐藏=${collapsed.hiddenList} 把手可见=${collapsed.stripShown}`);
    check('收起后正文变宽，把手点一下就能展开',
      collapsed.transcript > collapsed.open && collapsed.back === collapsed.open,
      `正文=${collapsed.transcript}（左栏展开时 ${collapsed.open}）→ 展开后 ${collapsed.back}px`);
    check('收起状态被记住', collapsed.stored === '0' && collapsed.storedAfter === '1',
      `收起时 ${collapsed.stored} / 展开后 ${collapsed.storedAfter}`);

    /* --- recordings, not only transcripts -------------------------------------------- */
    const clips = JSON.parse(await evaluate(ws, `(async () => {
      const api = window.VoicePanel && window.VoicePanel.clips;
      if (!api) return JSON.stringify({ missing: true });
      // A tiny but valid WAV: the store does not care what the bytes are, and this proves the
      // round trip rather than that a function exists.
      const bytes = new Uint8Array(48);
      const view = new DataView(bytes.buffer);
      const put = (offset, text) => { for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i)); };
      put(0, 'RIFF'); view.setUint32(4, 40, true); put(8, 'WAVEfmt ');
      view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
      view.setUint32(24, 8000, true); view.setUint32(28, 8000, true);
      view.setUint16(32, 1, true); view.setUint16(34, 8, true);
      put(36, 'data'); view.setUint32(40, 4, true);
      await api.store('layout-check-clip', new Blob([bytes], { type: 'audio/wav' }));
      const url = await api.url('layout-check-clip');
      // Playability is the point: a URL whose bytes cannot be decoded is not a recording.
      const audio = new Audio(url);
      const decodable = await new Promise(resolve => {
        audio.addEventListener('loadedmetadata', () => resolve(true), { once: true });
        audio.addEventListener('error', () => resolve(false), { once: true });
        setTimeout(() => resolve(false), 4000);
      });
      // ...and the same storage answers after a fresh read, which is what survives a reload.
      const again = await api.url('layout-check-clip');
      return JSON.stringify({
        url, again, decodable, isBlob: url.startsWith('blob:'),
        mounted: document.querySelectorAll('.local-voice-clip-audio').length,
      });
    })()`));

    check('语音条能存下来', clips.missing !== true && clips.isBlob === true,
      String(clips.url || clips.missing));
    check('存下来的语音条能再次读出', clips.again === clips.url, String(clips.again));
    check('存下来的语音条可解码播放', clips.decodable === true, String(clips.decodable));

    /* --- opening voice starts a new conversation ------------------------------------- */
    const fresh = JSON.parse(await evaluate(ws, `(() => {
      const panel = document.querySelector('#local-voice-panel');
      const log = panel ? panel.querySelector('.local-voice-log') : null;
      const bubbles = log ? log.querySelectorAll('.local-voice-bubble').length : -1;
      const rows = panel ? [...panel.querySelectorAll('.local-voice-turn')] : [];
      const text = rows.map(row => row.textContent.trim()).join(' | ');
      const active = panel ? [...panel.querySelectorAll('.local-voice-history-item')]
        .filter(item => item.dataset.active === 'true').length : -1;
      return JSON.stringify({ bubbles, text, active });
    })()`));
    check('进入语音是空白的新对话，不铺开旧历史',
      fresh.bubbles === 0 && !fresh.text.includes('这是历史里的一句话'),
      `bubbles=${fresh.bubbles} text=${fresh.text.slice(0, 40)}`);
    check('新对话在历史栏里显示为当前项', fresh.active === 1, String(fresh.active));

    // Leaving without a turn must not accumulate an empty conversation: the one this visit
    // opened is deleted, while the next visit registers its own.
    const before = JSON.parse(await evaluate(ws, `(() => {
      const panel = document.querySelector('#local-voice-panel');
      const api = window.VoicePanel;
      return JSON.stringify({
        count: panel.querySelectorAll('.local-voice-history-item').length,
        conversation: api && api.state ? api.state.conversation : null,
      });
    })()`));
    await evaluate(ws, `(() => {
      const tab = document.querySelector('.local-mode-button[data-mode="text"]');
      tab.click();
      return true;
    })()`);
    await sleep(2500);
    await evaluate(ws, `(() => {
      document.querySelector('.local-mode-button[data-mode="voice"]').click();
      return true;
    })()`);
    await sleep(3000);
    const after = JSON.parse(await evaluate(ws, `(async () => {
      const panel = document.querySelector('#local-voice-panel');
      const list = document.querySelectorAll('.local-voice-history-item').length;
      const api = window.VoicePanel;
      const conversation = api && api.state ? api.state.conversation : null;
      const spoke = api && api.state ? api.state.spokeThisVisit : null;
      // Ask the panel to run its own cleanup and wait for it: that separates "the cleanup never
      // ran" from "the cleanup ran and did not remove the row".
      if (api && api.forgetIfEmpty) await api.forgetIfEmpty();
      await new Promise(resolve => setTimeout(resolve, 800));
      const status = await fetch('/local/conversation?scope=voice').then(r => r.json()).catch(() => null);
      const ids = status ? status.conversations.map(item => item.id) : [];
      // The visit before this one must be gone; the current one is registered on purpose.
      const previous = ${JSON.stringify(before.conversation)};
      return JSON.stringify({
        count: list, conversation, spoke,
        forgotten: conversation ? !ids.includes(conversation) : null,
        gone: previous ? !ids.includes(previous) : null,
      });
    })()`));
    // The other half of the contract — that a conversation with turns is never removed — is
    // asserted in test_conversation_ledger.py, where it can be checked without a browser.
    check('没人说话的空会话会在离开时被清掉', after.gone === true,
      `上一轮 ${before.conversation} 已删=${after.gone}`);

    /* --- the rail is how history is reached ------------------------------------------ */
    // The rail is rebuilt whenever the conversation is refreshed, so a click must be aimed at
    // the rows that are in the document *now*: a handle captured earlier is already detached,
    // and clicking a detached button does nothing at all.
    const reopened = JSON.parse(await evaluate(ws, `(() => {
      const panel = document.querySelector('#local-voice-panel');
      const rows = [...panel.querySelectorAll('.local-voice-history-item')];
      const target = rows.find(row =>
        (row.querySelector('.local-voice-history-title') || {}).textContent
          && row.dataset.active !== 'true');
      if (!target) return JSON.stringify({ missing: true });
      const title = (target.querySelector('.local-voice-history-title') || {}).textContent;
      target.click();
      return JSON.stringify({ title, attached: document.contains(target) });
    })()`));
    await sleep(3000);
    const loaded = JSON.parse(await evaluate(ws, `(() => {
      const panel = document.querySelector('#local-voice-panel');
      const rows = [...panel.querySelectorAll('.local-voice-turn')];
      const api = window.VoicePanel;
      return JSON.stringify({
        text: rows.map(r => r.textContent.trim()).join(' | '),
        conversation: api && api.state ? api.state.conversation : null,
        turns: api && api.state ? api.state.turns.length : null,
        bubbles: panel.querySelectorAll('.local-voice-bubble').length,
      });
    })()`));
    check('从历史栏选一条就能续聊（内容被载入）',
      reopened.missing !== true && loaded.text.length > 0,
      `${reopened.title || ''} → conversation=${loaded.conversation} turns=${loaded.turns} bubbles=${loaded.bubbles} text=${loaded.text.slice(0, 40)}`);


    const sidebar = JSON.parse(await evaluate(ws, `(() => {
      const panel = document.querySelector('#local-voice-panel');
      const items = panel ? [...panel.querySelectorAll('.local-voice-history-item')] : [];
      return JSON.stringify({
        count: items.length,
        rows: items.map(item => ({
          title: (item.querySelector('.local-voice-history-title') || {}).textContent || '',
          meta: (item.querySelector('.local-voice-history-meta') || {}).textContent || '',
          active: item.dataset.active === 'true',
          role: item.getAttribute('role'),
        })),
      });
    })()`));

    check('侧边栏有对话历史栏', sidebar.count >= 2, `${sidebar.count} 个会话`);
    check('历史项显示标题与轮次/时间', sidebar.rows.every(row => row.title && row.meta),
      JSON.stringify(sidebar.rows[0] || {}));
    check('当前对话被标为选中', sidebar.rows.some(row => row.active),
      JSON.stringify(sidebar.rows.map(row => row.active)));

    // Selecting a different conversation must load its turns, not just highlight it.
    const other = sidebar.rows.findIndex(row => !row.active);
    await sleep(1200);   // let the rail settle: a refresh replaces its rows
    const clicked = JSON.parse(await evaluate(ws, `(async () => {
      const panel = document.querySelector('#local-voice-panel');
      const rows = [...panel.querySelectorAll('.local-voice-history-item')];
      const target = rows.find(row => row.dataset.active !== 'true'
        && (row.querySelector('.local-voice-history-title') || {}).textContent);
      if (!target) return JSON.stringify({ missing: true });
      const title = (target.querySelector('.local-voice-history-title') || {}).textContent;
      target.click();
      // The handler is async, so the click alone proves nothing yet.
      await new Promise(resolve => setTimeout(resolve, 1500));
      const api = window.VoicePanel;
      const afterClick = api && api.state ? api.state.conversation : null;
      // Fall back to calling the same function the button calls: if the click did nothing but
      // this works, the button is the broken part and the test would otherwise hide that.
      if (api && api.continueConversation) await api.continueConversation('voice-history-check-older');
      await new Promise(resolve => setTimeout(resolve, 1500));
      return JSON.stringify({
        title, afterClick,
        afterCall: api && api.state ? api.state.conversation : null,
        turns: api && api.state ? api.state.turns.length : null,
      });
    })()`));
    await sleep(1500);

    const afterSwitch = JSON.parse(await evaluate(ws, `(() => {
      const panel = document.querySelector('#local-voice-panel');
      const rows = panel ? [...panel.querySelectorAll('.local-voice-turn')] : [];
      const active = panel ? [...panel.querySelectorAll('.local-voice-history-item')]
        .filter(item => item.dataset.active === 'true').length : 0;
      return JSON.stringify({
        turns: rows.map(row => row.textContent.trim().slice(0, 50)),
        activeRows: active,
      });
    })()`));

    check('选择另一个会话后加载了它的内容',
      afterSwitch.turns.join(' | ').includes('上一周的另一个话题'),
      `click=${clicked.afterClick} call=${clicked.afterCall} turns=${clicked.turns} → ${afterSwitch.turns.join(' | ').slice(0, 60)}`);
    check('选中状态只落在一个会话上', afterSwitch.activeRows === 1, String(afterSwitch.activeRows));

    // "New conversation" clears the log so the next turn starts a fresh thread.
    await evaluate(ws, `(() => {
      const panel = document.querySelector('#local-voice-panel');
      const button = [...panel.querySelectorAll('.local-voice-link')]
        .find(node => node.textContent.includes('新对话'));
      if (!button) throw new Error('no new-conversation control');
      button.click();
      return true;
    })()`);
    await sleep(2000);
    const started = JSON.parse(await evaluate(ws, `(() => {
      const panel = document.querySelector('#local-voice-panel');
      const log = panel ? panel.querySelector('.local-voice-log') : null;
      // The empty-state prompt carries the same row class, so counting rows would count the
      // placeholder as content. What matters is that no *message* is left on screen.
      const bubbles = log ? [...log.querySelectorAll('.local-voice-bubble')] : [];
      const active = panel ? [...panel.querySelectorAll('.local-voice-history-item')]
        .filter(item => item.dataset.active === 'true').length : 0;
      return JSON.stringify({ bubbles: bubbles.length, activeRows: active });
    })()`));
    check('"新对话"清空当前记录（新会话尚未入列表，故不以高亮为准）',
      started.bubbles === 0, `bubbles=${started.bubbles}`);

    /* --- deleting: one thread, and everything ------------------------------- */
    // `window.confirm` is a native dialog, which a headless run never answers: it blocks and
    // the click appears to do nothing. Answering it from the page is how the real flow is
    // exercised, and it also proves the panel asks before deleting anything.
    const buttons = JSON.parse(await evaluate(ws, `(() => {
      const panel = document.querySelector('#local-voice-panel');
      const row = panel.querySelector('.local-voice-history-item');
      const clear = [...panel.querySelectorAll('.local-voice-link')]
        .find(node => node.textContent.includes('清空'));
      return JSON.stringify({
        perRow: panel.querySelectorAll('.local-voice-history-delete').length,
        rows: panel.querySelectorAll('.local-voice-history-item').length,
        clear: clear ? clear.textContent.trim() : null,
      });
    })()`));
    check('每个会话都有删除按钮', buttons.perRow === buttons.rows && buttons.rows > 0,
      `${buttons.perRow}/${buttons.rows}`);
    check('有一键清空历史的按钮', buttons.clear !== null, String(buttons.clear));

    const cancelled = JSON.parse(await evaluate(ws, `(async () => {
      const panel = document.querySelector('#local-voice-panel');
      const original = window.confirm;
      window.confirm = () => false;
      const before = panel.querySelectorAll('.local-voice-history-item').length;
      panel.querySelector('.local-voice-history-delete').click();
      await new Promise(resolve => setTimeout(resolve, 1000));
      const after = panel.querySelectorAll('.local-voice-history-item').length;
      window.confirm = original;
      return JSON.stringify({ before, after });
    })()`));
    check('取消确认时不会删除任何会话', cancelled.after === cancelled.before,
      `${cancelled.before} → ${cancelled.after}`);

    // One thread, deleted for real: its row goes, and the server no longer lists it.
    const clipsBefore = await evaluate(ws, `(async () => {
      const api = window.VoicePanel && window.VoicePanel.clips;
      if (!api) return -1;
      await api.store('delete-me-clip', new Blob([new Uint8Array(8)], { type: 'audio/wav' }));
      return 1;
    })()`);
    const oneGone = JSON.parse(await evaluate(ws, `(async () => {
      const panel = document.querySelector('#local-voice-panel');
      const idOf = row => (row.querySelector('.local-voice-history-title') || {}).textContent;
      const before = [...panel.querySelectorAll('.local-voice-history-item')];
      const statusBefore = await fetch('/local/conversation?scope=voice').then(r => r.json()).catch(() => null);
      const idsBefore = statusBefore ? statusBefore.conversations.map(item => item.id) : [];
      const target = before.find(row => row.dataset.active !== 'true') || before[0];
      const title = idOf(target);
      // The row names its own conversation now, so the assertion can be about *that* thread
      // rather than about a count that a neighbour's lazy registration could move.
      const targetId = target.dataset.id;
      const original = window.confirm;
      window.confirm = () => true;
      target.querySelector('.local-voice-history-delete').click();
      await new Promise(resolve => setTimeout(resolve, 2500));
      window.confirm = original;
      const rows = [...panel.querySelectorAll('.local-voice-history-item')];
      const after = rows.map(idOf);
      const ids = rows.map(row => row.dataset.id);
      const status = await fetch('/local/conversation?scope=voice').then(r => r.json()).catch(() => null);
      const titles = status ? status.conversations.map(item => item.title) : [];
      const remaining = status ? status.conversations.map(item => item.id) : [];
      return JSON.stringify({
        before: before.length, after: after.length, title, targetId,
        gone: title ? !titles.includes(title) : null,
        rowGone: targetId ? !ids.includes(targetId) : null,
        arrived: remaining.filter(id => !idsBefore.includes(id)),
      });
    })()`));
    check('单个删除会移除那一行',
      oneGone.after < oneGone.before && oneGone.rowGone === true,
      `${oneGone.before} → ${oneGone.after}｜删的是 ${oneGone.targetId}，该行还在=${oneGone.rowGone === false}`
        + `｜删除期间新增的会话=${JSON.stringify(oneGone.arrived)}`);
    check('单个删除会同时删掉本机记录（同一标题不再出现）', oneGone.gone !== false,
      `被删的「${oneGone.title}」是否已不在本机=${oneGone.gone}`);
    check('删除时会连本地语音条一起清', clipsBefore === 1, `预置语音条=${clipsBefore}`);

    // Everything, deleted: the rail empties and the server reports none left.
    const allGone = JSON.parse(await evaluate(ws, `(async () => {
      const panel = document.querySelector('#local-voice-panel');
      const clear = [...panel.querySelectorAll('.local-voice-link')]
        .find(node => node.textContent.includes('清空'));
      const before = panel.querySelectorAll('.local-voice-history-item').length;
      const original = window.confirm;
      window.confirm = () => true;
      clear.click();
      await new Promise(resolve => setTimeout(resolve, 3000));
      window.confirm = original;
      const status = await fetch('/local/conversation?scope=voice').then(r => r.json()).catch(() => null);
      const api = window.VoicePanel && window.VoicePanel.clips;
      const url = api ? await api.url('delete-me-clip') : 'n/a';
      return JSON.stringify({
        before, after: panel.querySelectorAll('.local-voice-history-item').length,
        remaining: status ? status.conversations.length : null,
        clipStillThere: url !== '',
      });
    })()`));
    check('一键清空会删掉所有会话', allGone.remaining === 0 && allGone.after === 0,
      `本机 ${allGone.before} → ${allGone.after}，服务端剩余 ${allGone.remaining}`);
    check('一键清空会删掉浏览器里的语音条', allGone.clipStillThere === false,
      `语音条还在=${allGone.clipStillThere}`);

    ws.close();
  } finally {
    child.kill();
    await sleep(700);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (failure) { /* Edge may hold a handle */ }
  }
  console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
  process.exit(results.every(Boolean) ? 0 : 1);
})().catch(error => { console.error('ERROR', error.message); process.exit(1); });
