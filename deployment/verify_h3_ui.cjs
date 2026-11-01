const fs = require('node:fs');
const { chromium } = require("../web/lib/playwright.cjs");

(async () => {
  const browser = await chromium.launch({
    executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless: true, timeout: 15000, args: ['--disable-gpu'],
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
    page.setDefaultTimeout(20000);
    await page.goto('http://127.0.0.1:8188', { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => !!window.comfyAPI?.app?.app?.graph && !!window.LiteGraph?.registered_node_types?.H3LongVideoDirector, null, { timeout: 45000 });
    await page.waitForLoadState('networkidle', { timeout: 30000 });
    await page.waitForFunction(() => window.comfyAPI.app.app.graph._nodes.length > 0);
    const reports = [];
    const workflowRoot = 'D:/AI/ComfyUI-BSAI_v010/ComfyUI/user/default/workflows/H3-12GB-Local/';
    for (const file of fs.readdirSync(workflowRoot).filter(name => /^0[1-9]_.*\.json$/.test(name))) {
      const name = file.slice(0, -5);
      const path = 'D:/AI/ComfyUI-BSAI_v010/ComfyUI/user/default/workflows/H3-12GB-Local/' + name + '.json';
      const graph = JSON.parse(fs.readFileSync(path, 'utf8'));
      const result = await page.evaluate(async ({ graph }) => {
        const { app } = await import('/scripts/app.js');
        await app.loadGraphData(graph);
        const converted = await app.graphToPrompt();
        const missing = app.graph._nodes.filter(n => n.has_errors || !n.constructor.nodeData).map(n => n.type);
        return { missing, output: converted.output, nodeCount: app.graph._nodes.length,
          nodes: app.graph._nodes.map(n => ({id:n.id,type:n.type,mode:n.mode,comfyClass:n.comfyClass})), convertedKeys:Object.keys(converted) };
      }, { graph });
      for (const target of Object.values(result.output)) {
        const checks = {
          H3LongVideoDirector: {width: 768, height: 512, fps: 24, seconds_per_shot: 8, steps: 24, seed: 20260906, resume: true},
          H3MP4ShotEditor: {width: 768, height: 512, fps: 24, seconds_per_shot: 7.5, steps: 24, seed: 2026090706, action: 'next_shot', use_first_shot_reference: true},
          MiniMaxH3ReferenceToVideo: {width: 768, height: 512, length: 192},
          BasicScheduler: {steps: 24, scheduler: 'simple'},
          CreateVideo: {fps: 24},
          MiniMaxH3MotionContextTrim: {fps: 24, match_tail: true},
          H3ModelLoaderAny: {activation_reserve_gb: /^0[589]_/.test(name) ? 3 : 2},
        }[target.class_type] || {};
        for (const [key, value] of Object.entries(checks)) {
          if (target.inputs[key] !== value) throw new Error(`${name}: ${target.class_type}.${key} = ${JSON.stringify(target.inputs[key])}, expected ${value}`);
        }
      }
      if (result.missing.length) throw new Error(name + ': missing nodes: ' + result.missing.join(', '));
      await page.evaluate(async () => {
        const { app } = await import('/scripts/app.js');
        app.canvas.ds.scale = 0.6;
        app.canvas.ds.offset = [30, 50];
        app.canvas.setDirty(true, true);
      });
      await page.screenshot({ path: 'D:/AI/deployment/logs/768x512/' + name + '-ui.png' });
      fs.writeFileSync('D:/AI/deployment/logs/768x512/' + name + '-ui-prompt.json', JSON.stringify(result.output, null, 2));
      reports.push({ name, nodes: result.nodeCount, missing: result.missing });
    }
    console.log(JSON.stringify(reports));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
