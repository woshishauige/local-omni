/* 公网访客的稳定性压测：连续几分钟的混合请求，一次失败都不允许。
 *
 * 用法：node check_guest_soak_live.cjs --url https://xxx.trycloudflare.com [--seconds 150]
 */
const ROOT = 'D:\\AI\\Qwen-Local';
const fs = require('node:fs');
const path = require('node:path');
const argv = process.argv.slice(2);
const arg = (name, fallback) => {
  const at = argv.indexOf('--' + name);
  return at >= 0 && argv[at + 1] ? argv[at + 1] : fallback;
};
const BASE = String(arg('url', '')).replace(/\/+$/, '');
const SECONDS = Number(arg('seconds', 150));
const PIN = JSON.parse(fs.readFileSync(path.join(ROOT, 'share.config.json'), 'utf8')).access_token;
const results = [];
const check = (label, ok, detail = '') => {
  results.push(Boolean(ok));
  console.log(`${ok ? 'PASS' : 'FAIL'} ${label.padEnd(40)} ${detail}`);
};
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

(async () => {
  if (!BASE) { console.log('SKIP: pass --url'); process.exitCode = 1; return; }
  // 1) 口令登录拿会话
  const login = await fetch(`${BASE}/__login`, {
    method: 'POST', headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ token: PIN }), redirect: 'manual',
  });
  const cookie = (login.headers.getSetCookie ? login.headers.getSetCookie() : [])
    .map(entry => entry.split(';')[0]).join('; ');
  check('访客口令登录', login.status === 200 || login.status === 303, `HTTP ${login.status}`);
  check('拿到访客会话', cookie.includes('qwen_access'), cookie.slice(0, 24) + '…');

  const stats = { calls: 0, failures: [], latencies: [] };
  const get = async (where) => {
    const started = Date.now();
    try {
      const response = await fetch(BASE + where, { headers: { cookie }, cache: 'no-store' });
      const body = await response.text();
      stats.calls += 1;
      stats.latencies.push(Date.now() - started);
      if (response.status >= 500 || response.status === 0) {
        stats.failures.push(`${response.status} ${where}`);
      }
      return { status: response.status, body };
    } catch (failure) {
      stats.calls += 1;
      stats.latencies.push(Date.now() - started);
      stats.failures.push(`ERR ${where} ${failure.message}`);
      return { status: 0, body: '' };
    }
  };

  const deadline = Date.now() + SECONDS * 1000;
  let round = 0;
  let chat = null;
  while (Date.now() < deadline) {
    round += 1;
    await get('/local/status');
    await get('/tools');
    if (round % 5 === 0) {
      await get('/local/image/progress');
      await get('/local/video/progress');
      await get('/local/voice/status');
    }
    if (!chat && round === 4) {
      // 中途真发一轮对话：稳定不只是"能轮询"，还要生成不失败。
      try {
        const response = await fetch(BASE + '/v1/chat/completions', {
          method: 'POST', headers: { cookie, 'content-type': 'application/json' },
          body: JSON.stringify({ model: 'local', stream: false, max_tokens: 24, temperature: 0,
                                 messages: [{ role: 'user', content: '只回答两个字：收到' }] }),
        });
        const body = await response.json().catch(() => ({}));
        chat = { status: response.status, text: ((body.choices || [{}])[0].message || {}).content || '' };
      } catch (failure) {
        chat = { status: 0, text: String(failure.message) };
      }
    }
    await sleep(1500);
  }

  const sorted = stats.latencies.slice().sort((a, b) => a - b);
  const p50 = sorted[Math.floor(sorted.length * 0.5)] || 0;
  const p95 = sorted[Math.floor(sorted.length * 0.95)] || 0;
  const worst = sorted[sorted.length - 1] || 0;
  console.log(`\n压测 ${SECONDS}s · 请求 ${stats.calls} 次 · 失败 ${stats.failures.length} 次 · ` +
              `延迟 p50 ${p50}ms / p95 ${p95}ms / 最慢 ${worst}ms`);
  check('全程没有 5xx / 断链', stats.failures.length === 0, stats.failures.slice(0, 4).join(' | '));
  check('中途那一轮真实对话成功', Boolean(chat) && chat.status === 200 && (chat.text || '').length > 0,
    chat ? `HTTP ${chat.status} · ${JSON.stringify((chat.text || '').slice(0, 20))}` : '未执行');
  check('延迟可控（p95 < 3s）', p95 > 0 && p95 < 3000, `p95 ${p95}ms`);
  console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
  process.exitCode = results.every(Boolean) ? 0 : 1;
})();
