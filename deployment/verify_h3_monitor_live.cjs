const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require("../web/lib/playwright.cjs");

(async () => {
  const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true, args: ['--disable-gpu'] });
  const origin = process.argv[2] || 'http://127.0.0.1:8189';
  try {
    const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(origin, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => window.comfyAPI?.app?.app?.graph && document.querySelector('#h3-monitor-dock'), null, { timeout: 60000 });
    const graph = JSON.parse(fs.readFileSync('D:/AI/ComfyUI-BSAI_v010/ComfyUI/user/default/workflows/H3-12GB-Local/01_H3_LongVideo.json', 'utf8'));
    const result = await page.evaluate(async graph => {
      const { app } = await import('/scripts/app.js');
      await app.loadGraphData(graph);
      const converted = await app.graphToPrompt();
      return { prompt: converted.output, missing: app.graph._nodes.filter(n => !n.constructor.nodeData).map(n => n.type) };
    }, graph);
    assert.equal(result.missing.length, 0, result.missing.join(', '));
    assert.equal(result.prompt['50'].inputs.steps, 20);
    fs.writeFileSync('D:/AI/deployment/logs/h3-current-ui-prompt.json', JSON.stringify(result.prompt, null, 2));
    const button = page.getByRole('button', { name: 'H3 监视器', exact: true });
    await button.click();
    await page.locator('.h3-monitor:not(.h3-monitor-compact)').waitFor({ state: 'visible' });
    await page.screenshot({ path: 'D:/AI/deployment/logs/h3-monitor-native-idle.png' });
    await page.goto(origin + '/extensions/ComfyUI-H3-LocalDirector/monitor.html');
    await page.waitForSelector('.h3-monitor[data-phase="idle"]');
    assert(await page.locator('.h3-stop').isDisabled());
    const state = await (await page.request.get(origin + '/h3-monitor/state')).json();
    assert.equal(state.phase, 'idle');
    assert.equal(errors.length, 0, errors.join('\n'));
    console.log(JSON.stringify({ backend: state, nativeSidebar: true, graphNodes: graph.nodes.length, missingNodes: result.missing, pageErrors: errors, promptSaved: true }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
