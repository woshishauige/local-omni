const {chromium} = require("./lib/playwright.cjs");
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({headless:true, channel:'msedge'});
  try {
    const width = Number(process.env.QWEN_INSPECT_WIDTH || 1440);
    const page = await browser.newPage({viewport:{width,height:width < 768 ? 844 : 900}});
    await page.route('**/local/status', route => route.fulfill({json:{mode:'vision',state:'ready',mtp_enabled:false,active_requests:0,revision:1}}));
    for (const endpoint of ['props','models','v1/models','health']) {
      await page.route(`**/${endpoint}*`, async route => {
        const response = await route.fetch({url:'http://127.0.0.1:8093/'+endpoint});
        if (endpoint === 'props') {
          const props = await response.json();
          delete props.role;
          props.model_alias = 'qwen3.8-27b-local';
          props.model_path = 'Bonsai-2-27B-PQ2_0-CRACK.gguf';
          props.modalities = {vision:true,video:true,audio:false};
          props.default_generation_settings = {n_ctx:8192,params:{temperature:0.7,top_p:0.9,top_k:20}};
          await route.fulfill({json:props});
        } else if (endpoint.endsWith('models')) {
          await route.fulfill({json:{object:'list',data:[{id:'qwen3.8-27b-local',object:'model',owned_by:'llamacpp'}]}});
        } else await route.fulfill({response});
      });
    }
    await page.goto('http://127.0.0.1:8094/');
    await page.locator('.conversation-chat-form textarea').waitFor();
    await page.waitForTimeout(1000);
    if (process.env.QWEN_INSPECT_ATTACHMENTS === '1') {
      await page.locator('input[type="file"]:not(.qg-reference-input)').setInputFiles('D:/AI/deployment/qwen-vision-test.png');
      await page.waitForTimeout(1000);
      assert.ok(await page.locator('[data-mode="text"]').isDisabled(), 'Pending attachments must prevent a destructive reload');
      await page.getByRole('button', {name:'Remove',exact:true}).click();
      await page.waitForFunction(() => !document.querySelector('[data-mode="text"]').disabled);
    }
    if (process.env.QWEN_INSPECT_IMAGE === '1') {
      console.log(JSON.stringify({image:(await page.screenshot()).toString('base64')}));
    } else {
      console.log(JSON.stringify(await page.locator('.conversation-chat-form').evaluate(form => ({
        buttons:[...form.querySelectorAll('button')].map(b=>({text:b.textContent,aria:b.getAttribute('aria-label'),title:b.getAttribute('title')})),
        inputs:[...document.querySelectorAll('input[type="file"]')].map(input=>input.outerHTML),
        attachments:[...document.querySelectorAll('img')].map(image=>({alt:image.alt, parentClass:image.parentElement.className})),
        files:[...document.querySelector('input[type="file"]').files].map(file=>file.name),
        body:document.body.innerText.slice(-1800),
      }))));
    }
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode=1});
