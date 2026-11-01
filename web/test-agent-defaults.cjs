const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const vm = require('node:vm');
const { chromium } = require("./lib/playwright.cjs");

const nativeRoot = path.join(__dirname, 'native');
const migration = fs.readFileSync(path.join(nativeRoot, 'agent-defaults.js'), 'utf8');
const categoryKey = 'LlamaUi.disabledToolCategories';
const migrationKey = 'QwenLocal.agentDefaults.v2';

function checkMigrationFailures() {
  const store = new Map([[categoryKey, '{broken']]);
  const localStorage = { getItem: key => store.get(key) ?? null,
    setItem: (key, value) => store.set(key, value) };
  vm.runInNewContext(migration, { localStorage, console: { warn() {} } });
  assert.equal(store.get(categoryKey), '{broken');
  assert.equal(store.has(migrationKey), false);
  vm.runInNewContext(migration, { localStorage: { getItem() { throw new Error('Unavailable'); } },
    console: { warn() {} } });
  store.clear();
  vm.runInNewContext(migration, { localStorage, console: { warn() {} } });
  assert.equal(store.get(migrationKey), '1');
  assert.equal(store.has(categoryKey), false);
}

async function run() {
  checkMigrationFailures();
  const server = http.createServer((request, response) => {
    const url = new URL(request.url, 'http://localhost');
    const requested = path.resolve(nativeRoot, '.' + (url.pathname === '/' ? '/index.html' : url.pathname));
    if (!requested.startsWith(nativeRoot + path.sep) || !fs.existsSync(requested) || !fs.statSync(requested).isFile()) {
      response.writeHead(404).end();
      return;
    }
    const contentType = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css',
      '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon', '.webmanifest': 'application/manifest+json' };
    response.setHeader('Content-Type', contentType[path.extname(requested)] || 'application/octet-stream');
    response.setHeader('Cache-Control', 'no-store');
    if (url.searchParams.has('legacy')) {
      response.end(fs.readFileSync(requested, 'utf8').replace(/<script src="\.\/agent-defaults\.js\?v=\d+"><\/script>/, ''));
    } else fs.createReadStream(requested).pipe(response);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({ headless: true, channel: 'msedge' });
  try {
    const context = await browser.newContext({ serviceWorkers: 'block', viewport: { width: 1366, height: 900 } });
    const page = await context.newPage();
    const errors = [];
    const requests = [];
    let executions = 0;
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      if (localStorage.getItem('agent-test-seeded')) return;
      localStorage.setItem('agent-test-seeded', '1');
      localStorage.setItem('LlamaUi.config', JSON.stringify({ theme: 'dark', temperature: 0.42,
        systemMessage: 'Preserve this system message.', agenticMaxTurns: 7 }));
      localStorage.setItem('LlamaUi.userOverrides', JSON.stringify(['theme', 'temperature', 'systemMessage', 'agenticMaxTurns']));
      localStorage.setItem('LlamaUi.disabledToolCategories', JSON.stringify(['server', 'browser', 'mcp', 'custom']));
      localStorage.setItem('LlamaUi.disabledToolKeys', JSON.stringify(['server:read_file']));
      localStorage.setItem('LlamaUi.alwaysAllowedTools', JSON.stringify(['browser:get_datetime']));
      localStorage.setItem('existing-unrelated-setting', 'keep');
    });
    await page.route('**/props*', route => route.fulfill({ json: {
      default_generation_settings: { n_ctx: 16384, params: { temperature: 0.7, top_p: 0.9, top_k: 20 } },
      modalities: { vision: false, audio: false, video: false }, model_alias: 'qwen3.8-27b-local',
      model_path: 'Qwen3.8-27B.gguf', total_slots: 1,
    } }));
    await page.route('**/v1/models*', route => route.fulfill({ json: { data: [{ id: 'qwen3.8-27b-local' }] } }));
    const definition = name => ({ tool: name, uses_cwd: false,
      definition: { type: 'function', function: { name, description: 'Local test tool.',
        parameters: { type: 'object', properties: {}, required: [] } } } });
    await page.route('**/tools', async route => {
      if (route.request().method() === 'GET') {
        await route.fulfill({ json: [definition('get_info'), definition('read_file')] });
      } else {
        if (route.request().postDataJSON().tool === 'file_glob_search') {
          await route.fulfill({ json: { base: 'D:/AI', entries: [] } });
          return;
        }
        executions += 1;
        assert.equal(route.request().postDataJSON().tool, 'get_info');
        await route.fulfill({ json: { plain_text_response: 'MOCK WINDOWS INFO' } });
      }
    });
    await page.route('**/v1/chat/completions', async route => {
      const request = route.request().postDataJSON();
      requests.push(request);
      const number = requests.length;
      const delta = number === 2 ? { tool_calls: [{ index: 0, id: 'call_agent_test', type: 'function',
        function: { name: 'get_info', arguments: '{}' } }] } :
        { content: number === 1 ? 'Existing conversation survives migration.' : 'Agent tool cycle completed.' };
      const chunk = changes => ({ id: 'agent-default-test', object: 'chat.completion.chunk',
        model: 'qwen3.8-27b-local', choices: [{ index: 0, delta: changes, finish_reason: null }] });
      await route.fulfill({ contentType: 'text/event-stream', body:
        'data: ' + JSON.stringify(chunk(delta)) + '\n\n' +
        'data: ' + JSON.stringify({ ...chunk({}), choices: [{ index: 0, delta: {}, finish_reason: number === 2 ? 'tool_calls' : 'stop' }] }) + '\n\n' +
        'data: [DONE]\n\n' });
    });

    await page.goto(origin + '/?legacy=1');
    const composer = page.locator('.conversation-chat-form textarea');
    await composer.fill('Existing English message.');
    await page.locator('.conversation-chat-form button[type="submit"]').click();
    await page.getByText('Existing conversation survives migration.', { exact: true }).waitFor();
    assert.ok(!requests[0].tools?.length, 'Legacy disabled categories must stay disabled before migration');
    const priorUrl = page.url();
    await page.goto(priorUrl.replace('?legacy=1', '?agent-default=1'));
    await page.getByText('Existing conversation survives migration.', { exact: true }).waitFor();
    await page.getByLabel('用户消息及操作').getByText('Existing English message.', { exact: true }).waitFor();
    const migrated = await page.evaluate(() => Object.fromEntries(Object.entries(localStorage)));
    assert.deepEqual(JSON.parse(migrated[categoryKey]), ['browser', 'mcp', 'custom']);
    assert.equal(migrated[migrationKey], '1');
    assert.deepEqual(JSON.parse(migrated['LlamaUi.disabledToolKeys']), ['server:read_file']);
    assert.deepEqual(JSON.parse(migrated['LlamaUi.alwaysAllowedTools']), ['browser:get_datetime']);
    assert.equal(migrated['existing-unrelated-setting'], 'keep');
    const config = JSON.parse(migrated['LlamaUi.config']);
    assert.equal(config.theme, 'dark');
    assert.equal(config.temperature, 0.42);
    assert.equal(config.systemMessage, 'Preserve this system message.');
    assert.equal(config.agenticMaxTurns, 7);

    await page.getByRole('button', { name: '新对话', exact: true }).first().click();
    await composer.fill('Run the local test tool.');
    await page.locator('.conversation-chat-form button[type="submit"]').click();
    const allow = page.getByRole('button', { name: '允许一次', exact: true });
    await allow.waitFor();
    assert.equal(executions, 0, 'Tool must wait for approval');
    assert.equal(requests.length, 2);
    assert.deepEqual(requests[1].tools.map(tool => tool.function.name), ['get_info']);
    assert.equal(requests[1].temperature, 0.42);
    assert.ok(requests[1].messages.some(message => message.content === 'Preserve this system message.'));
    await allow.click();
    await page.getByText('Agent tool cycle completed.', { exact: true }).waitFor();
    assert.equal(executions, 1);
    assert.equal(requests.length, 3);
    assert.ok(requests[2].messages.some(message => message.role === 'tool' && message.content === 'MOCK WINDOWS INFO'));
    assert.deepEqual(await page.evaluate(() => JSON.parse(localStorage.getItem('LlamaUi.alwaysAllowedTools'))), ['browser:get_datetime']);
    await page.reload();
    await page.getByText('Agent tool cycle completed.', { exact: true }).waitFor();

    await page.evaluate(key => localStorage.setItem(key, JSON.stringify(['server', 'browser', 'mcp', 'custom'])), categoryKey);
    await page.reload();
    await composer.waitFor();
    assert.deepEqual(await page.evaluate(key => JSON.parse(localStorage.getItem(key)), categoryKey), ['server', 'browser', 'mcp', 'custom']);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ passed: true, migration: true, existingConversationPreserved: true,
      otherSettingsPreserved: true, permissionsPreserved: true, agenticToolCycle: true,
      manualPreferenceAfterMigrationPreserved: true, modelRequestsMocked: requests.length }));
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
