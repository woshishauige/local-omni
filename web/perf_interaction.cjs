/* What one user interaction costs.
 *
 * For each toolbar tab the script times the click, reads the page clock, and then reports
 * the requests whose *start time on that same clock* is after the click — so "the fanout of
 * this interaction" is a measurement, not an attribution by eye. Long tasks and layout
 * counts during the window say whether it also costs the main thread.
 *
 * Read-only by design: it clicks the injected toolbar's view tabs (a local view change,
 * no /local/mode POST, no model switch) and never touches the owner's conversation.
 *
 * Usage: node perf_interaction.cjs [--port 9419] [--settle 2500] [--tag before]
 */
const fs = require('node:fs');
const path = require('node:path');
const lib = require('./perf_lib.cjs');

const argv = process.argv.slice(2);
const arg = (name, fallback) => {
  const at = argv.indexOf('--' + name);
  return at >= 0 && argv[at + 1] ? argv[at + 1] : fallback;
};
const PORT = Number(arg('port', 9419));
const SETTLE = Number(arg('settle', 2500));
const TAG = String(arg('tag', 'run'));
const TABS = String(arg('tabs', 'text,vision,image,video,text')).split(',');

/* Click a toolbar tab and return the page clock reading taken immediately before the
 * click, plus the counters before it. */
const clickTab = mode => `JSON.stringify((() => {
  const button = document.querySelector('.local-mode-button[data-mode="${mode}"]');
  if (!button) return { ok: false, reason: 'no button for ${mode}',
    known: Array.from(document.querySelectorAll('.local-mode-button')).map(b => b.dataset.mode) };
  const disabled = button.disabled;
  const before = { tasks: __perf.totalTasks, taskMs: __perf.taskMs,
                   mutations: window.__qvMutations || 0, batches: window.__qvMutationBatches || 0,
                   t: performance.now() };
  button.click();
  return { ok: true, before, disabled,
           toolbarState: (document.querySelector('.local-mode-toolbar') || { dataset: {} }).dataset.state,
           pressedAfterClick: button.getAttribute('aria-pressed'),
           label: (button.textContent || '').trim() };
})())`;

const COUNTERS = `JSON.stringify({ tasks: __perf.totalTasks, taskMs: __perf.taskMs, t: performance.now(),
  mutations: window.__qvMutations || 0, panelState: window.__qvPanelState && window.__qvPanelState() })`;

/* Counts DOM mutations anywhere under body, coalesced per animation frame, plus the
 * layout reads the injected code performs. A mutation counter is how "the toolbar
 * re-renders on every change" stops being a claim and becomes a number. */
const INSTRUMENT = `(() => {
  if (window.__qvMutationObserver) window.__qvMutationObserver.disconnect();
  window.__qvMutations = 0;
  window.__qvMutationBatches = 0;
  let pending = 0;
  window.__qvMutationObserver = new MutationObserver(records => {
    window.__qvMutations += records.length;
    pending += records.length;
    if (pending && !window.__qvMutationRaf) {
      window.__qvMutationRaf = requestAnimationFrame(() => { window.__qvMutationRaf = 0;
        window.__qvMutationBatches += pending; pending = 0; });
    }
  });
  window.__qvMutationObserver.observe(document.body, { childList: true, subtree: true, attributes: true, characterData: true });
  window.__qvPanelState = () => {
    const box = el => { if (!el) return null; const r = el.getBoundingClientRect();
      return { w: Math.round(r.width), h: Math.round(r.height) }; };
    return {
      image: { active: !!(window.ImagePanel && !window.ImagePanel.root.hidden), box: box(window.ImagePanel && window.ImagePanel.root) },
      video: { active: !document.querySelector('#local-video-panel').hidden, box: box(document.querySelector('#local-video-panel')) },
      voice: { active: !document.querySelector('#local-voice-panel').hidden, box: box(document.querySelector('#local-voice-panel')) },
      chatHidden: getComputedStyle(document.querySelector('.conversation-chat-form') || document.body).display,
      pressed: Array.from(document.querySelectorAll('.local-mode-button'))
        .map(b => b.dataset.mode + '=' + b.getAttribute('aria-pressed')).join(' '),
    };
  };
  return true;
})()`;

async function main() {
  const browser = await lib.launch({ port: PORT });
  if (lib.injectionGate()) lib.step('injection rewrite: ' + JSON.stringify(await lib.rewriteInjection(browser, lib.INJECTION_VERSIONS)));
  const out = { tag: TAG, settle: SETTLE, steps: [] };
  try {
    await lib.login(browser);
    await lib.waitFor(browser.evaluate,
      '!!document.querySelector(".local-mode-button") && !!window.ImagePanel && !!window.VideoPanel',
      40000, 'toolbar and panels');
    await browser.evaluate(INSTRUMENT);
    await browser.reader.arm('interaction');
    await lib.sleep(6000);
    // Drain startup traffic so each step only sees what its own click caused.
    await browser.reader.drain();

    for (let index = 0; index < TABS.length; index++) {
      const mode = TABS[index];
      const clicked = await browser.reader.js(clickTab(mode));
      if (!clicked.ok) { lib.step(`${mode}: ${clicked.reason}`); out.steps.push({ mode, error: clicked.reason }); continue; }
      const clickAt = clicked.before.t;
      await lib.sleep(SETTLE);
      const after = await browser.reader.js(COUNTERS);
      const entries = await browser.reader.drain();
      const caused = entries.filter(entry => entry.start >= clickAt - 5);
      const summary = lib.summarise(caused);
      const step = {
        index, mode, clickedAt: +clickAt.toFixed(1), disabled: clicked.disabled, label: clicked.label,
        requests: summary.count, bytes: summary.bytes,
        paths: summary.rows.map(row => `${row.count}x ${row.path}`),
        longTasks: after.tasks - clicked.before.tasks,
        blockingMs: Math.round(after.taskMs - clicked.before.taskMs),
        mutations: after.mutations - clicked.before.mutations,
        mutationBatches: (after.mutationBatches || 0) - (clicked.before.mutationBatches || 0),
        panelState: after.panelState,
      };
      out.steps.push(step);
      lib.step(`${mode.padEnd(8)} click@${step.clickedAt}ms  requests=${step.requests} bytes=${step.bytes} ` +
        `longTasks=${step.longTasks} blocking=${step.blockingMs}ms mutations=${step.mutations} ` +
        `disabled=${clicked.disabled} state=${clicked.toolbarState} pressed=${clicked.pressedAfterClick}`);
      if (step.paths.length) console.log('        ' + step.paths.join('\n        '));
      console.log('        panels: ' + JSON.stringify(step.panelState));
    }

    // How long the generate panel's own mount actually takes, from the page's own clock:
    // the gap between the click and the last request that click caused.
    out.mountLatency = out.steps
      .filter(step => step.requests > 0)
      .map(step => ({ mode: step.mode, requests: step.requests, bytes: step.bytes,
                      longTasks: step.longTasks, blockingMs: step.blockingMs }));

    const outPath = path.join(lib.ROOT, 'web', `perf_interaction_${TAG}.json`);
    fs.writeFileSync(outPath, JSON.stringify(out, null, 2), 'utf8');
    console.log(`\nwritten ${outPath}`);
  } finally {
    await browser.close();
  }
  return 0;
}

main().then(code => process.exit(code)).catch(error => { console.error('ERROR', error.stack || error.message); process.exit(1); });
