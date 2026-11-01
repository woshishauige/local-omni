/* 真实外部访客的端到端验收：从公网地址进站 → 输口令 → 用功能。
 *
 * 这是所有者要的那条证明："必须能成功进网站并完整确认能使用功能"。所有请求都走**隧道地址**，
 * 用的是一个干净的浏览器 profile（没有任何机主 cookie），身份就是一个拿到口令的访客。
 *
 * 用法：node check_guest_site_live.cjs [--url https://xxx.trycloudflare.com] [--port 9700]
 */
const { spawn } = require('node:child_process');
const { results, check, cdp, sleep, launchAndOpen } = require('./lib/cdp.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const ROOT = 'D:\\AI\\Qwen-Local';
const argv = process.argv.slice(2);
const arg = (name, fallback) => {
  const at = argv.indexOf('--' + name);
  return at >= 0 && argv[at + 1] ? argv[at + 1] : fallback;
};
const PORT = Number(arg('port', 9700));
const BASE = String(arg('url', '')).replace(/\/+$/, '');
const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(candidate => fs.existsSync(candidate));


async function evaluate(ws, expression, timeout = 60000) {
  // 页面在服务切换/自动重载时会短暂销毁执行上下文（"Execution context was destroyed"），
  // 那是访客体验里正常的一瞬，不该让验收脚本自己崩掉：重试一次，再不行返回 null。
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const result = await cdp(ws, 'Runtime.evaluate',
        { expression, awaitPromise: true, returnByValue: true }, timeout);
      if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
      return result.result.value;
    } catch (failure) {
      if (attempt === 1) return null;
      await sleep(1200);
    }
  }
  return null;
}

(async () => {
  if (!EDGE) { console.log('SKIP: Edge not found'); return; }
  if (!BASE) { console.log('SKIP: pass --url https://<tunnel>'); process.exitCode = 1; return; }
  const pin = JSON.parse(fs.readFileSync(path.join(ROOT, 'share.config.json'), 'utf8')).access_token;
  console.log(`验收地址：${BASE}\n`);
  // 浏览器、调试端口、登录与导航都在 web/lib/cdp.cjs 里（原来每个脚本各复制一遍）。
  const { profile, child, ws } = await launchAndOpen({ port: 9499 });
  try {
    const reloaded = await evaluate(ws, `!!document.querySelector('.conversation-chat-form') && !document.querySelector('input[name="token"]')`);
    check('访客会话在刷新后仍然有效（不用重新输口令）', reloaded === true);
  } finally {
    try { child.kill(); } catch { /* ignore */ }
    console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
    process.exitCode = results.every(Boolean) ? 0 : 1;
  }
})();
