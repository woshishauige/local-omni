const fs = require('node:fs');
const { chromium } = require("../web/lib/playwright.cjs");

(async () => {
  const browser = await chromium.launch({
    executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless: true, args: ['--disable-gpu'], timeout: 15000,
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
    await page.goto('http://127.0.0.1:8188', { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => !!window.LiteGraph?.registered_node_types?.H3MP4ShotEditor, null, { timeout: 45000 });
    await page.waitForLoadState('networkidle');
    await page.waitForFunction(() => window.comfyAPI.app.app.graph._nodes.length > 0);
    const graph = JSON.parse(fs.readFileSync('D:/AI/ComfyUI-BSAI_v010/ComfyUI/user/default/workflows/H3-12GB-Local/08_H3_LongVideo_768x512.json', 'utf8'));
    await page.evaluate(async ({ graph, promptId }) => {
      const { app } = await import('/scripts/app.js');
      const { api } = await import('/scripts/api.js');
      const record = (await (await fetch('/history/' + promptId)).json())[promptId];
      if (!record?.outputs?.['50']) throw new Error('Completed H3 output is unavailable');
      await app.loadGraphData(graph);
      app.canvas.ds.scale = 0.65;
      app.canvas.ds.offset = [-2150, 45];
      api.dispatchEvent(new CustomEvent('executed', { detail: { node: '50', display_node: '50', output: record.outputs['50'] } }));
      app.canvas.setDirty(true, true);
    }, { graph, promptId: process.argv[2] });
    await page.waitForFunction(() => [...document.querySelectorAll('video')].some(video => video.readyState >= 2), null, { timeout: 20000 });
    const report = await page.evaluate(async () => {
      const video = [...document.querySelectorAll('video')].find(video => video.readyState >= 2);
      video.pause();
      await new Promise(resolve => { video.addEventListener('seeked', resolve, { once: true }); video.currentTime = 4.3; });
      return { width: video.videoWidth, height: video.videoHeight, duration: video.duration, src: video.currentSrc };
    });
    if (report.width !== 768 || report.height !== 512) throw new Error(JSON.stringify(report));
    await page.screenshot({ path: 'D:/AI/deployment/logs/768x512/H3-video-preview-ui.png' });
    console.log(JSON.stringify(report));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
