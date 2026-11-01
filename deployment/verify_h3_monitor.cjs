const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require("../web/lib/playwright.cjs");

const webRoot = 'D:/AI/ComfyUI-BSAI_v010/ComfyUI/custom_nodes/ComfyUI-H3-LocalDirector/web';
const prefix = '/extensions/ComfyUI-H3-LocalDirector/';
let state = { phase: 'idle', step: 0, steps: 0, elapsed_seconds: 0, updated_at: 1 };
let cancelPath;
const server = http.createServer((req, res) => {
    if (req.url === '/h3-monitor/state') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(state));
    } else if (req.method === 'POST' && req.url.startsWith('/api/jobs/')) {
        cancelPath = req.url;
        state = { ...state, phase: 'interrupted', updated_at: state.updated_at + 1 };
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end('{}');
    } else if (req.url.startsWith(prefix)) {
        const file = path.join(webRoot, req.url.slice(prefix.length).split('?')[0]);
        const mime = { '.html': 'text/html', '.mjs': 'application/javascript', '.js': 'application/javascript', '.css': 'text/css', '.woff2': 'font/woff2' };
        fs.readFile(file, (error, data) => {
            res.writeHead(error ? 404 : 200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream' });
            res.end(error ? 'Not found' : data);
        });
    } else { res.writeHead(404); res.end(); }
});

(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true, args: ['--disable-gpu'] });
    try {
        const page = await browser.newPage({ viewport: { width: 1280, height: 850 } });
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.goto(`http://127.0.0.1:${server.address().port}${prefix}monitor.html`);
        await page.waitForSelector('.h3-monitor[data-phase="idle"]');
        assert(await page.locator('.h3-stop').isDisabled());
        const preview = await page.evaluate(() => {
            const canvas = document.createElement('canvas'); canvas.width = 900; canvas.height = 200;
            const ctx = canvas.getContext('2d');
            ['#719f8d', '#ab876c', '#6d91ad'].forEach((color, index) => { ctx.fillStyle = color; ctx.fillRect(index * 300, 0, 300, 200); });
            ctx.fillStyle = '#ffffff'; ctx.font = '24px sans-serif'; ctx.fillText('UI TEST / MOCK PREVIEW', 20, 100);
            return canvas.toDataURL();
        });
        state = { phase: 'sampling', prompt_id: 'test/task-id', node_id: '50', project: 'H3-monitor-verification-long-project-name', shot: 1, shots: 4, step: 8, steps: 20, elapsed_seconds: 245, sampling_eta_seconds: 320, eta_seconds: null, preview, preview_kind: 'latent_rgb', updated_at: 2 };
        await page.waitForSelector('.h3-monitor[data-phase="sampling"]');
        assert.equal(await page.locator('[data-h3="remaining"]').textContent(), '未校准');
        assert.equal(await page.locator('[data-h3="sampling"]').textContent(), '5:20');
        assert.match(await page.locator('[data-h3="finish"]').textContent(), /采样/);
        assert.equal(await page.locator('[data-h3="preview"]').evaluate(img => img.naturalWidth), 900);
        await page.screenshot({ path: 'D:/AI/deployment/logs/h3-monitor-desktop-mock.png', fullPage: true });
        await page.setViewportSize({ width: 390, height: 844 });
        await page.screenshot({ path: 'D:/AI/deployment/logs/h3-monitor-mobile-mock.png', fullPage: true });
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
        state = { ...state, eta_seconds: 355, updated_at: 3 };
        await page.waitForFunction(() => document.querySelector('[data-h3="remaining"]').textContent === '5:55');
        assert.doesNotMatch(await page.locator('[data-h3="finish"]').textContent(), /采样/);
        await page.locator('.h3-stop').click();
        await page.waitForSelector('.h3-monitor[data-phase="interrupted"]');
        assert.equal(cancelPath, '/api/jobs/test%2Ftask-id/cancel');
        assert(await page.locator('.h3-stop').isDisabled());
        await page.evaluate(async () => {
            const { createMonitorStore, mountMonitor } = await import('./monitor_ui.mjs');
            const container = document.createElement('div'); container.id = 'h3-monitor-dock'; document.body.append(container);
            const store = createMonitorStore();
            mountMonitor(container, store, { compact: true });
            store.update({ phase: 'sampling', prompt_id: 'mock-dock', step: 3, steps: 20, elapsed_seconds: 60, sampling_eta_seconds: 240, updated_at: 999 });
            window.testStore = store;
        });
        await page.screenshot({ path: 'D:/AI/deployment/logs/h3-monitor-mobile-dock-mock.png', fullPage: true });
        assert.equal(await page.locator('#h3-monitor-dock [data-h3="sampling"]').textContent(), '4:00');
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
        assert.deepEqual(errors, []);
        console.log(JSON.stringify({ passed: true, checks: ['idle', 'sampling preview', 'null ETA', 'calibrated ETA', 'cancel exact prompt ID', 'desktop', 'mobile', 'compact dock', 'no page errors'], screenshots: 'D:/AI/deployment/logs/h3-monitor-*-mock.png' }));
    } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
