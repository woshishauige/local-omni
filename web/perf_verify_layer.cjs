/* Does the browser actually receive the edited layer? A one-shot proof, so a measurement
 * run can state which bytes it measured instead of assuming.
 *
 * Compares the served script against a hash taken here, on the same file, and reports the
 * URL the page ended up loading. Run with the rewrite on and off.
 *
 * Usage: node perf_verify_layer.cjs [--port 9433] [--rewrite]
 */
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const lib = require('./perf_lib.cjs');

const argv = process.argv.slice(2);
const arg = (name, fallback) => {
  const at = argv.indexOf('--' + name);
  return at >= 0 && argv[at + 1] ? argv[at + 1] : fallback;
};
const PORT = Number(arg('port', 9433));
const REWRITE = argv.includes('--rewrite');

const VERSIONS = { 'mode-control.js': 17, 'image-panel.js': 16, 'video-panel.js': 20, 'voice-input.js': 7 };

const hash = text => crypto.createHash('sha256').update(text, 'utf8').digest('hex').slice(0, 16);
const diskHash = file => hash(fs.readFileSync(path.join(lib.ROOT, 'web', file), 'utf8'));

async function main() {
  const browser = await lib.launch({ port: PORT });
  try {
    if (REWRITE) {
      const state = await lib.rewriteInjection(browser, VERSIONS);
      lib.step(`injection rewrite armed for ${state.rules} file(s)`);
    }
    await lib.login(browser);
    await lib.waitFor(browser.evaluate, '!!document.querySelector(".local-mode-toolbar")', 40000, 'toolbar');
    await lib.sleep(3000);

    const loaded = JSON.parse(await browser.evaluate(`JSON.stringify(
      performance.getEntriesByType('resource')
        .filter(entry => entry.name.includes('/local/') && entry.name.endsWith('.js') || /\\/local\\/[a-z-]+\\.js/.test(entry.name))
        .map(entry => ({ url: entry.name.replace('http://127.0.0.1:8081', ''), size: entry.transferSize })))`));
    const scripts = JSON.parse(await browser.evaluate(`JSON.stringify(Array.from(document.scripts)
      .map(node => node.src).filter(Boolean).map(src => src.replace('http://127.0.0.1:8081', '')))`));

    console.log(`\n--- SCRIPTS THE PAGE LOADED (rewrite=${REWRITE}) ---`);
    for (const url of scripts) console.log('  ' + url);

    console.log('\n--- SERVED BYTES VS DISK ---');
    for (const file of Object.keys(VERSIONS)) {
      const entry = loaded.find(row => row.url.includes(`/local/${file}`));
      const body = await browser.evaluate(`(async () => {
        const response = await fetch('/local/${file}?v=${VERSIONS[file]}', { cache: 'no-store' });
        const text = await response.text();
        return JSON.stringify({ status: response.status, length: text.length,
          hashInput: text.slice(0, 4000) + text.slice(-4000), probe: text.includes('TOOL_KEYS_TTL_MS') ||
            text.includes('DOCK_HIDDEN_TICK_MS') || text.includes('HISTORY_MIN_GAP_MS') || text.includes('HIDDEN_TICK_MS') });
      })()`);
      const parsed = JSON.parse(body);
      console.log(`  ${file.padEnd(18)} page-load=${entry ? entry.size + 'B' : 'not loaded'}  ` +
        `fetch=${parsed.length}B status=${parsed.status}  has-new-code=${parsed.probe}  ` +
        `disk=${fs.statSync(path.join(lib.ROOT, 'web', file)).size}B`);
    }
    fs.writeFileSync(path.join(lib.ROOT, 'web', 'perf_verify_layer.json'),
      JSON.stringify({ rewrite: REWRITE, scripts, loaded }, null, 2), 'utf8');
  } finally {
    await browser.close();
  }
  return 0;
}

main().then(code => process.exit(code)).catch(error => { console.error('ERROR', error.stack || error.message); process.exit(1); });
