// Load each generated workflow in ComfyUI's own frontend and run it once.
//
// A workflow JSON that ComfyUI refuses to load (wrong widget order, dangling
// links, unknown node) would look fine on disk but fail on open, so this drives
// the real frontend: loadGraphData -> queuePrompt -> wait for the output image.
//
//   node verify_comfy_workflows.cjs [workflow.json ...]
const path = require("node:path");
const fs = require("node:fs");
const { chromium } = require("../web/lib/playwright.cjs");

const COMFY = "http://127.0.0.1:8188";
const WORKFLOWS = path.join("D:", "AI", "ComfyUI-BSAI_v010", "ComfyUI", "user", "default", "workflows");

async function historyNewest(http) {
  const response = await fetch(`${COMFY}/history?max_items=20`);
  const data = await response.json();
  const entries = Object.entries(data || {}).map(([id, item]) => ({ id, item }));
  entries.sort((a, b) => (b.item?.status?.messages?.[0]?.[1]?.timestamp || 0) - (a.item?.status?.messages?.[0]?.[1]?.timestamp || 0));
  return entries;
}

(async () => {
  const files = process.argv.slice(2).length
    ? process.argv.slice(2)
    : fs.readdirSync(WORKFLOWS).filter(name => name.startsWith("sdxl-pony-") && name.endsWith(".json"));
  const browser = await chromium.launch({ headless: true, channel: "msedge" });
  const context = await browser.newContext({ viewport: { width: 1600, height: 900 } });
  const page = await context.newPage();
  const problems = [];
  page.on("pageerror", error => problems.push(`pageerror: ${error.message}`));
  let failures = 0;
  try {
    await page.goto(COMFY, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => Boolean(window.app && window.app.graph), null, { timeout: 60000 });
    await page.waitForTimeout(1500);
    for (const file of files) {
      const before = await historyNewest();
      const workflow = JSON.parse(fs.readFileSync(path.join(WORKFLOWS, file), "utf8"));
      const loaded = await page.evaluate(async data => {
        const app = window.app;
        // Loading is asynchronous in this frontend version: awaiting it is what
        // makes the node count meaningful.
        await app.loadGraphData(data, true, true);
        await new Promise(resolve => setTimeout(resolve, 500));
        return {
          nodes: app.graph._nodes.length,
          types: app.graph._nodes.map(node => node.type),
          missing: app.graph._nodes.filter(node => node.has_errors).map(node => node.type),
        };
      }, workflow);
      process.stdout.write(`${file}\n  nodes=${loaded.nodes} missing=${JSON.stringify(loaded.missing)}\n`);
      if (loaded.nodes !== workflow.nodes.length) {
        problems.push(`${file}: graph has ${loaded.nodes} nodes, file has ${workflow.nodes.length}`);
        failures += 1;
        continue;
      }
      await page.evaluate(() => window.app.queuePrompt(0, 1));
      const deadline = Date.now() + 300000;
      let done = null;
      while (Date.now() < deadline) {
        await page.waitForTimeout(3000);
        const entries = await historyNewest();
        const fresh = entries.find(entry => !before.some(seen => seen.id === entry.id) && entry.item?.status?.completed);
        if (fresh) { done = fresh; break; }
      }
      if (!done) {
        problems.push(`${file}: no completed prompt within 5 minutes`);
        failures += 1;
        continue;
      }
      const images = Object.values(done.item.outputs || {}).flatMap(output => output.images || []);
      process.stdout.write(`  ran prompt ${done.id.slice(0, 8)} -> ${images.length} image(s) `
        + `${images.map(image => `${image.subfolder || ""}/${image.filename}`).join(", ")}\n`);
      if (!images.length) { problems.push(`${file}: completed without an image`); failures += 1; }
    }
    console.log("problems:", JSON.stringify(problems));
    console.log("page errors:", JSON.stringify([...new Set(problems.filter(item => item.startsWith("pageerror")))]));
  } finally {
    await browser.close();
  }
  process.exitCode = failures ? 1 : 0;
})().catch(error => { console.error(error); process.exitCode = 1; });
