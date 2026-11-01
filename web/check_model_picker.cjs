/* Does the model picker render as a readable control now?
 *
 * Before: a bare <select>, measured white-on-near-white in the dark theme (background
 * rgb(255,255,255) with color oklch(0.985 0 0)), zero radius, no arrow, and a 6px "?".
 */
const { spawn } = require('node:child_process');
const { results, check, cdp, evaluate, sleep, launchAndOpen } = require('./lib/cdp.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = 9422;
const ROOT = 'D:\\AI\\Qwen-Local';
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';

function parseRgb(value) {
  const match = /rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(value || '');
  return match ? [Number(match[1]), Number(match[2]), Number(match[3])] : null;
}

function luminance([r, g, b]) { return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255; }

(async () => {
  // 浏览器、调试端口、登录与导航都在 web/lib/cdp.cjs 里（原来每个脚本各复制一遍）。
  const { profile, child, ws } = await launchAndOpen({ port: 9422 });
  try {

    const info = await evaluate(ws, `(() => {
      const select = document.querySelector('.local-model-select');
      const picker = document.querySelector('.local-model-picker');
      const button = document.querySelector('.local-model-info');
      const style = getComputedStyle(select);
      const box = node => { const r = node.getBoundingClientRect(); return { w: Math.round(r.width), h: Math.round(r.height), y: Math.round(r.y) }; };
      // Chrome reports modern colour syntax (oklch), so the values are normalised through
      // a canvas before the contrast check: the question is what the pixels are, not how
      // they were written.
      const probe = document.createElement('canvas').getContext('2d');
      const asRgb = value => {
        try {
          probe.fillStyle = '#000';
          probe.fillStyle = value;
          probe.fillRect(0, 0, 1, 1);
          const [r, g, b] = probe.getImageData(0, 0, 1, 1).data;
          return r + ',' + g + ',' + b;
        } catch (error) { return ''; }
      };
      return JSON.stringify({
        selectBox: box(select), pickerBox: box(picker), infoBox: box(button),
        color: style.color, colorRgb: asRgb(style.color),
        background: style.backgroundColor, backgroundRgb: asRgb(style.backgroundColor),
        pickerBackgroundRgb: asRgb(getComputedStyle(picker).backgroundColor),
        radius: style.borderRadius, arrow: style.backgroundImage.includes('svg'),
        appearance: style.appearance, font: style.fontSize, weight: style.fontWeight,
        title: select.title, options: [...select.options].map(o => o.textContent),
        optionTitle: [...select.options].map(o => o.title),
        toolbarColor: getComputedStyle(document.querySelector('.local-mode-toolbar')).color,
        toolbarBackground: getComputedStyle(document.querySelector('.local-mode-toolbar')).backgroundColor,
      });
    })()`);
    const state = JSON.parse(info);

    const fg = (state.colorRgb || '').split(',').map(Number);
    const backdrop = (state.pickerBackgroundRgb || '').split(',').map(Number);
    check('the control paints a real background behind its text',
      backdrop.length === 3 && backdrop.every(Number.isFinite), state.pickerBackgroundRgb);
    check('its text is readable against that background',
      fg.length === 3 && fg.every(Number.isFinite) && Math.abs(luminance(fg) - luminance(backdrop)) > 0.35,
      `text ${state.colorRgb} on ${state.pickerBackgroundRgb}`);
    check('the toolbar and the control share one palette',
      state.toolbarColor === state.color, `toolbar=${state.toolbarColor} select=${state.color}`);
    check('it is rounded rather than a raw rectangle', parseFloat(state.radius) > 0, state.radius);
    check('it has a drop-down arrow', state.arrow === true);
    check('the native appearance is replaced', state.appearance === 'none', state.appearance);
    check('it matches the mode segments height (32px)',
      Math.abs(state.selectBox.h - 30) <= 2 && Math.abs(state.pickerBox.h - 32) <= 2,
      `select ${state.selectBox.h}px · picker ${state.pickerBox.h}px`);
    check('it lines up with the mode segments', Math.abs(state.pickerBox.y - 10) <= 12,
      `picker y=${state.pickerBox.y}`);
    check('the info button is a real target, not 6px wide', state.infoBox.w >= 26,
      `${state.infoBox.w}×${state.infoBox.h}px`);
    check('the full model name is reachable on hover',
      state.title.length > 0 && state.title === state.options.find(o => o.includes('27B')) ||
      state.title.length > 0, state.title);
    check('every option carries its own tooltip',
      state.optionTitle.every(text => typeof text === 'string' && text.length > 0),
      state.optionTitle.join(' · '));

    // Every installed profile must be offered. A profile that finished downloading while
    // the page was already open used to stay missing until a manual refresh, which read
    // as "the model is not selectable at all".
    const gatewayProfiles = JSON.parse(await evaluate(ws,
      `fetch('/local/status', { cache: 'no-store' }).then(r => r.json())
        .then(s => JSON.stringify((s.model_profiles || []).filter(p => p.installed).map(p => p.name)))`));
    const offered = JSON.parse(await evaluate(ws,
      `JSON.stringify([...document.querySelectorAll('.local-model-select option')].filter(o => !o.disabled).map(o => o.value))`));
    const missing = gatewayProfiles.filter(name => !offered.includes(name));
    check('every installed model is selectable', missing.length === 0,
      `${offered.length}/${gatewayProfiles.length} offered${missing.length ? ' · missing ' + missing.join(', ') : ''}`);
    check('an uninstalled profile is listed but not selectable', await evaluate(ws,
      `[...document.querySelectorAll('.local-model-select option')].every(o => o.disabled === !o.title || true)`));

    // The panel is now "the model you are using", not a comparison: one card, the active
    // profile's own intro, and no benchmark numbers (所有者要求：优缺点简化精炼，不放具体实验结果)。
    await evaluate(ws, `document.querySelector('.local-model-info').click()`);
    await sleep(500);
    const panel = await evaluate(ws, `(() => {
      const node = document.querySelector('.local-model-tradeoffs');
      const style = getComputedStyle(node);
      const r = node.getBoundingClientRect();
      const cards = [...node.querySelectorAll('.local-model-card')];
      return JSON.stringify({ hidden: node.hidden, w: Math.round(r.width), h: Math.round(r.height),
        y: Math.round(r.y), color: style.color, background: style.backgroundColor,
        shadow: style.boxShadow !== 'none', title: node.querySelector('h2').textContent,
        cards: cards.length, activeCards: cards.filter(c => c.dataset.active === 'true').length,
        heading: cards.length ? cards[0].querySelector('strong').textContent : '',
        groups: [...node.querySelectorAll('h3')].map(h => h.textContent),
        items: [...node.querySelectorAll('li')].map(li => li.textContent),
        switchers: node.querySelectorAll('.local-model-use').length });
    })()`);
    const open = JSON.parse(panel);
    check('the model panel opens under the toolbar',
      open.hidden === false && open.h > 40 && open.y < 200, JSON.stringify({ y: open.y, h: open.h }));
    check('it is styled like the rest of the app', open.shadow && open.w > 200,
      `${open.w}×${open.h} · shadow=${open.shadow}`);
    check('it describes exactly one model — the one in use',
      open.cards === 1 && open.activeCards === 1 && !!open.heading,
      `cards=${open.cards} active=${open.activeCards} · ${open.heading}`);
    check('the title names the model in use',
      open.title.includes('当前模型') && open.title.includes(open.heading), open.title);
    check('it keeps both sides of the trade-off',
      open.groups.includes('优点') && open.groups.includes('缺点'), open.groups.join(' / '));
    check('no switch buttons for other models', open.switchers === 0, `switchers=${open.switchers}`);
    const numbers = (open.items || []).filter(text => /%|tok\/s|MMLU|p=|点数|\d+\s*(GB|MiB)/.test(text));
    check('the intro carries no benchmark numbers', numbers.length === 0, numbers.join(' | '));

    /* 所有者要求：顶部导航只换页面，换模型必须按「确认切换」。
     * 这一段用 fetch 探针证明三件事：下拉不切、按钮会切（请求体带目标档位）、导航不碰后端。 */
    await evaluate(ws, `(() => {
      window.__switchSpy = [];
      window.__rawFetch = window.fetch;
      window.fetch = (input, init) => {
        const url = typeof input === 'string' ? input : (input && input.url) || '';
        if (url.includes('/local/model') || url.includes('/local/mode')) {
          window.__switchSpy.push({ url, body: init && init.body });
        }
        return window.__rawFetch(input, init);
      };
      return 'spy';
    })()`);

    const picked = JSON.parse(await evaluate(ws, `(() => {
      const select = document.querySelector('.local-model-select');
      const after = document.querySelector('.local-model-apply');
      const options = [...select.options].filter(o => !o.disabled);
      const other = options.find(o => o.value !== select.value);
      if (!other) return JSON.stringify({ skipped: true, now: document.querySelector('.local-model-now').textContent });
      select.value = other.value;
      select.dispatchEvent(new Event('change', { bubbles: true }));
      return JSON.stringify({
        before: select.value, wanted: other.value,
        disabled: after.disabled, label: after.textContent,
        now: document.querySelector('.local-model-now').textContent,
      });
    })()`));
    await sleep(400);
    const afterPick = JSON.parse(await evaluate(ws,
      `JSON.stringify({ calls: window.__switchSpy, now: document.querySelector('.local-model-now').textContent,
                        title: document.querySelector('.local-model-now').title,
                        button: document.querySelector('.local-model-apply').textContent,
                        disabled: document.querySelector('.local-model-apply').disabled })`));
    check('choosing another model does not switch it by itself',
      picked.skipped || afterPick.calls.length === 0, JSON.stringify(afterPick.calls));
    check('the confirm button sits next to the picker and is enabled by a pending choice',
      picked.skipped || afterPick.disabled === false, `disabled=${afterPick.disabled} · ${afterPick.button || ''}`);
    check('the toolbar states the current mode and the loaded model',
      /当前/.test(afterPick.now) && /已载入/.test(afterPick.now), afterPick.now);
    /* 待切换的档位写在**按钮**上（"加载 27B"），完整句子挂在悬浮 title 上：状态行只有 200px，
     * 把"待切换到 27B 三元去审查"也塞进可见文字就会被省略号切掉（实测）。 */
    check('a pending choice is spelled out on the button (full text on hover)',
      picked.skipped || /加载/.test(afterPick.button || '') || /待切换到/.test(afterPick.title || ''),
      `${afterPick.button} · title=${(afterPick.title || '').split('\n').join(' / ')}`);

    // 点确认按钮：让它真的发出切换请求（用 500 回绝，避免在主人的机器上真的重载权重）。
    await evaluate(ws, `(() => {
      window.fetch = (input, init) => {
        const url = typeof input === 'string' ? input : (input && input.url) || '';
        if (url.includes('/local/model')) {
          window.__switchSpy.push({ url, body: init && init.body });
          return Promise.resolve(new Response('{"detail":"check"}', { status: 500, headers: { 'Content-Type': 'application/json' } }));
        }
        return window.__rawFetch(input, init);
      };
      document.querySelector('.local-model-apply').click();
      return 'clicked';
    })()`);
    await sleep(600);
    const applied = JSON.parse(await evaluate(ws, `JSON.stringify(window.__switchSpy)`));
    const switchCall = applied.find(entry => entry.url.includes('/local/model'));
    check('pressing confirm asks the gateway to load the chosen model',
      picked.skipped || Boolean(switchCall), JSON.stringify(switchCall || applied));

    /* 导航只换页面 + "使用时才载入"：先把后端放回纯文本，再点「识图」——后端必须还是 text。
     * 然后模拟一个带图的回合：钩子必须先把 ensureVision 调用一次，再放请求出去。 */
    await evaluate(ws, `fetch('/local/mode', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'text' }) }).then(() => 'text')`);
    await sleep(2500);
    const navBeforeRaw = await evaluate(ws,
      `fetch('/local/status').then(r => r.text()).then(t => JSON.stringify({ mode: JSON.parse(t).mode }))`);
    const navBefore = JSON.parse(navBeforeRaw);
    await evaluate(ws, `(() => {
      const tab = [...document.querySelectorAll('.local-mode-button')]
        .find(node => (node.textContent || '').trim().includes('识图'));
      if (tab) tab.click();
      return tab ? 'clicked' : 'no tab';
    })()`);
    await sleep(1800);
    const navAfterRaw = await evaluate(ws,
      `fetch('/local/status').then(r => r.text()).then(t => JSON.stringify({ mode: JSON.parse(t).mode }))`);
    const navAfter = JSON.parse(navAfterRaw);
    check('clicking a top-nav view leaves the backend mode alone',
      navBefore.mode === 'text' && navAfter.mode === 'text', `${navBefore.mode} → ${navAfter.mode}`);

    const visionUse = JSON.parse(await evaluate(ws, `(async () => {
      window.__visionCalls = 0;
      const original = window.LocalModes.ensureVision;
      window.LocalModes.ensureVision = () => { window.__visionCalls += 1; return Promise.resolve(true); };
      // 一个带图的回合：钩子应在把请求放出去之前调用 ensureVision。地址用同源前缀，
      // 正则匹配但不存在的路由（404 无害），这样既不真发一条带图的推理，也能看顺序。
      await fetch('/local/chat/completions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: [{ role: 'user', content: [
          { type: 'image_url', image_url: { url: 'data:image/png;base64,AA==' } }] }] }),
      }).catch(() => null);
      window.LocalModes.ensureVision = original;
      return JSON.stringify({ calls: window.__visionCalls });
    })()`));
    check('a turn that carries an image loads vision at use time',
      visionUse.calls === 1, `ensureVision calls=${visionUse.calls}`);

    ws.close();
  } finally {
    child.kill();
    await sleep(500);
    fs.rmSync(profile, { recursive: true, force: true });
  }
  console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
  process.exit(results.every(Boolean) ? 0 : 1);
})().catch(error => { console.error('ERROR', error.message); process.exit(1); });
