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
    await page.waitForFunction(() => window.LiteGraph?.registered_node_types?.H3OptionalReferenceImage, null, { timeout: 45000 });
    await page.waitForLoadState('networkidle');
    const root = 'D:/AI/ComfyUI-BSAI_v010/ComfyUI/user/default/workflows/H3-12GB-Local/';
    const reports = [];
    for (const name of ['05_H3_LongVideo_12GB', '09_Qwen_H3_LongVideo_768x512', '08_H3_LongVideo_768x512']) {
      const graph = JSON.parse(fs.readFileSync(root + name + '.json', 'utf8'));
      const report = await page.evaluate(async graph => {
        const { app } = await import('/scripts/app.js');
        await app.loadGraphData(graph);
        const prompt = await app.graphToPrompt();
        const uploaders = app.graph._nodes.filter(n => n.type === 'H3OptionalReferenceImage');
        return { nodes: app.graph._nodes.length, missing: app.graph._nodes.filter(n => n.has_errors).map(n => n.type),
          rectangles: app.graph._nodes.filter(n => !n.flags.collapsed).map(n => ({id:n.id,x:n.pos[0],y:n.pos[1],w:n.size[0],h:n.size[1]})),
          uploaders: uploaders.map(n => ({id:n.id, widgets:n.widgets.map(w => ({name:w.name, type:w.type, value:w.value})),
            width:n.size[0], height:n.size[1]})), output:prompt.output };
      }, graph);
      if (report.missing.length || report.uploaders.length !== 20) throw new Error(JSON.stringify(report));
      for (let i=0;i<report.rectangles.length;i++) for (let j=i+1;j<report.rectangles.length;j++) {
        const a=report.rectangles[i], b=report.rectangles[j];
        if (a.x < b.x+b.w && a.x+a.w > b.x && a.y < b.y+b.h && a.y+a.h > b.y) throw new Error(`Overlapping nodes: ${name} / ${a.id} / ${b.id}`);
      }
      for (const uploader of report.uploaders) {
        if (!uploader.widgets.some(w => w.type === 'button')) throw new Error('Upload button missing: ' + uploader.id);
        if (report.output[uploader.id].inputs.image !== '__none__') throw new Error('Expected empty reference slot');
        if (report.output[uploader.id].inputs.enabled !== true) throw new Error('Enabled default is wrong');
      }
      for (let shot=1;shot<=4;shot++) for (let slot=1;slot<=5;slot++) {
        const key = `shot_${String(shot).padStart(2,'0')}_ref_${String(slot).padStart(2,'0')}`;
        const id = String(60 + (shot - 1) * 5 + slot - 1);
        if (report.output['50'].inputs[key]?.[0] !== id) throw new Error('Wrong shot reference link: ' + key);
      }
      reports.push({name,nodes:report.nodes,optionalUploaders:report.uploaders.length,missing:report.missing});
    }
    await page.evaluate(async () => {
      const { app } = await import('/scripts/app.js');
      app.canvas.ds.scale = 0.8;
      app.canvas.ds.offset = [-400, -1150];
      app.canvas.setDirty(true,true);
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    });
    await page.screenshot({path:'D:/AI/deployment/logs/h3-references-ui.png'});
    console.log(JSON.stringify(reports));
    if (process.argv[2]) {
      const chooserPromise = page.waitForEvent('filechooser', {timeout:15000});
      await page.evaluate(async () => {
        const { app } = await import('/scripts/app.js');
        app.graph.getNodeById(64).widgets.find(w => w.type === 'button').callback();
      });
      const chooser = await chooserPromise;
      await chooser.setFiles(process.argv[2]);
      await page.waitForFunction(() => window.comfyAPI.app.app.graph.getNodeById(64).widgets.find(w => w.name === 'image').value !== '__none__');
      await page.waitForFunction(() => window.comfyAPI.app.app.graph.getNodeById(64).imgs?.[0]?.naturalWidth > 0, null, {timeout:20000});
      const uploaded = await page.evaluate(async () => {
        const { app } = await import('/scripts/app.js');
        const node = app.graph.getNodeById(64);
        const before = (await app.graphToPrompt()).output['64'].inputs;
        node.widgets.find(w => w.name === 'enabled').value = false;
        const disabled = (await app.graphToPrompt()).output['64'].inputs;
        node.widgets.find(w => w.name === 'enabled').value = true;
        return {before,disabled};
      });
      if (uploaded.disabled.enabled !== false || uploaded.before.image === '__none__') throw new Error('Upload or toggle failed');
      fs.writeFileSync('D:/AI/deployment/logs/h3-reference-upload-check.json', JSON.stringify(uploaded,null,2));
      console.log(JSON.stringify({uploaded}));
      await page.evaluate(async () => {
        const { app } = await import('/scripts/app.js');
        app.canvas.ds.offset = [-1100,-1150];
        app.canvas.setDirty(true,true);
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      });
      await page.screenshot({path:'D:/AI/deployment/logs/h3-reference-upload-ui.png'});
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode=1; });
