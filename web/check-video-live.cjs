// Live check of the video view: does the panel render in the intended card order,
// and does 开始生成 actually reach the gateway? The submit is intercepted so the
// check never starts a real ten-minute H3 run.
//
// Run from Qwen-Local with:  node web/check-video-live.cjs
const path = require("node:path");
const { chromium } = require("./lib/playwright.cjs");

const origin = "http://127.0.0.1:8081";
const shotCount = Number(process.env.DSH_CHECK_SHOTS || 2);
// A real image to hand the shot card: the reference upload is what the panel used
// to send to the clip-only route, which answered 400 and killed the whole run.
const reference = process.env.DSH_CHECK_REF || "D:/AI/ComfyUI-BSAI_v010/ComfyUI/input/H3-demo-car-start.png";
// A real clip for shot 1's "what came before" row: short, 768x512, 24 fps, audio.
const contextClip = process.env.DSH_CHECK_CONTEXT || "D:/AI/_audit/h3-turbo/base-20step/shot_01.mp4";

(async () => {
  const browser = await chromium.launch({ headless: true, channel: "msedge" });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const errors = [];
  const seen = [];
  page.on("pageerror", error => errors.push(`pageerror: ${error.message}`));
  page.on("console", message => { if (message.type() === "error") errors.push(`console: ${message.text()}`); });
  page.on("request", request => {
    if (request.url().includes("/local/video/")) seen.push(`${request.method()} ${request.url()}`);
  });
  page.on("response", response => {
    if (response.status() >= 400) {
      const request = response.request();
      const headers = request.headers();
      console.log("HTTP", response.status(), request.method(), response.url(),
                  "content-type:", headers["content-type"] || "(none)",
                  "body:", (request.postData() || "").slice(0, 120));
    }
  });
  // The submit is intercepted throughout: nothing in this check is queued in ComfyUI.
  let submitted = null;
  await page.route("**/local/video/progress*", route => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({ busy: false, job: null }),
  }));
  try {
    await page.goto(origin + "/", { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1500);
    // The mode tabs are disabled while the text backend is loading or the GPU is busy
    // with a generation, so fall back to activating the panel directly: this check is
    // about the panel, not about the app's mode gating.
    const modeButton = page.locator('.local-mode-button[data-mode="video"]');
    if (await modeButton.isEnabled().catch(() => false)) {
      await modeButton.click();
    } else {
      console.log("模式按钮被禁用（后端未就绪/显存被占）→ 直接激活视频面板");
      await page.evaluate(() => window.VideoPanel?.setActive(true));
    }
    await page.locator("#local-video-panel:not([hidden])").waitFor({ timeout: 20000 });
    await page.waitForFunction(() => document.querySelectorAll(".qv-recipes .qv-recipe").length > 0, null, { timeout: 15000 });

    const layout = await page.evaluate(() => {
      const box = node => node.getBoundingClientRect();
      const monitor = document.querySelector(".qv-column-monitor");
      const main = document.querySelector(".qv-column-main");
      const side = document.querySelector(".qv-column-side");
      return {
        cards: [...main.querySelectorAll(".qv-pane:not([hidden]) > .qv-card")]
          .map(card => (card.querySelector(".qv-card-title") || { textContent: "(no title)" }).textContent),
        rails: {
          monitorX: Math.round(box(monitor).left), mainX: Math.round(box(main).left),
          sideX: Math.round(box(side).left),
          monitor: [...monitor.querySelectorAll(".qv-status, .qv-monitor-subtitle")].map(n => n.textContent),
          side: [...side.querySelectorAll(".qv-card-title")].map(n => n.textContent),
        },
        globalParams: [...document.querySelectorAll(".qv-params .qv-label")].map(node => node.textContent),
        shotParamLabels: [...document.querySelectorAll(".qv-shot")].map(card =>
          [...card.querySelectorAll(".qv-shot-params .qv-label")].map(node => node.textContent)),
        seeds: [...document.querySelectorAll(".qv-shot .qv-seed-row input")].map(input => input.value),
        randomButtons: document.querySelectorAll(".qv-seed-random").length,
        savePath: document.querySelector('input.qv-file[type="text"]')?.value,
      };
    });
    console.log("card order:", JSON.stringify(layout.cards));
    console.log("三栏（监控/设置/成片）:", JSON.stringify(layout.rails));
    if (!(layout.rails.monitorX < layout.rails.mainX && layout.rails.mainX < layout.rails.sideX)) {
      throw new Error(`三栏顺序不对：${JSON.stringify(layout.rails)}`);
    }
    if (!layout.rails.side.includes("成片")) throw new Error("成片不在最右栏");
    if (!layout.rails.monitor.length) throw new Error("左栏没有监控内容");
    console.log("global params:", JSON.stringify(layout.globalParams));
    console.log("per-shot seeds:", JSON.stringify(layout.seeds), "random buttons:", layout.randomButtons);
    console.log("shot param labels:", JSON.stringify(layout.shotParamLabels));
    console.log("save location:", layout.savePath);

    // Real playback of a finished film, when this gateway session has one: the file
    // route has to serve bytes a browser can decode, not merely answer 200.
    const playback = await page.evaluate(async () => {
      const payload = await (await fetch("/local/video/history", { cache: "no-store" })).json();
      const url = (payload.history || []).find(entry => entry.video)?.video;
      if (!url) return { skipped: "本次会话还没有成片" };
      const probe = document.createElement("video");
      probe.muted = true;
      probe.src = url;
      document.body.append(probe);
      const meta = await new Promise(resolve => {
        probe.onloadedmetadata = () => resolve({
          duration: Math.round(probe.duration * 100) / 100,
          size: `${probe.videoWidth}x${probe.videoHeight}`,
        });
        probe.onerror = () => resolve({ error: "浏览器无法解码这个文件" });
        setTimeout(() => resolve({ error: "读取元数据超时" }), 20000);
      });
      await probe.play().catch(() => {});
      await new Promise(resolve => setTimeout(resolve, 1200));
      const playing = !probe.paused && probe.currentTime > 0;
      probe.remove();
      return { ...meta, playing };
    });
    console.log("成片播放:", JSON.stringify(playback));

    if (shotCount !== 1) {
      await page.locator(".qv-shot-count .qv-recipe", { hasText: `${shotCount} 段` }).click();
      await page.waitForFunction(count => document.querySelectorAll(".qv-shot").length === count, shotCount, { timeout: 5000 });
    }
    await page.locator(".qv-textarea").first().fill("夜晚雨后的城市街道，霓虹在水面反光，镜头缓缓前推");
    // Reference image on shot 1: it has to upload to ComfyUI's input folder and
    // travel with the job as shot_references.
    await page.locator(".qv-shot").first()
      .locator('.qv-shot-refs:not(.qv-shot-context) input[type="file"]').setInputFiles(reference);
    await page.waitForFunction(() =>
      !/未选择/.test(document.querySelector(".qv-shot .qv-file-name")?.textContent || "未选择"),
      null, { timeout: 10000 });
    console.log("第 1 段参考图:", await page.locator(".qv-shot .qv-file-name").first().textContent());

    // Shot 1's predecessor clip: the row only exists on the first shot's card, and the
    // picked file has to be uploaded and named in the request body.
    const contextRow = await page.evaluate(() => {
      const row = document.querySelector(".qv-shot .qv-shot-context");
      const card = document.querySelector(".qv-shot");
      return {
        present: Boolean(row),
        onFirstShotOnly: Boolean(row) && card === document.querySelector(".qv-shot"),
        label: row?.querySelector(".qv-label")?.textContent,
        accept: row?.querySelector('input[type="file"]')?.getAttribute("accept"),
        hint: card?.querySelector(".qv-shot-context + .qv-note")?.textContent?.slice(0, 24),
      };
    });
    console.log("前文视频控件:", JSON.stringify(contextRow));
    if (!contextRow.present || !/前文视频/.test(contextRow.label || "")) {
      throw new Error(`第 1 段缺少前文视频控件：${JSON.stringify(contextRow)}`);
    }
    if (!/video/.test(contextRow.accept || "")) throw new Error("前文视频控件没有限制视频类型");
    if (await page.locator(".qv-shot-context").count() !== 1) {
      throw new Error("前文视频控件只应出现在第 1 段卡片上");
    }
    await page.locator(".qv-shot-context input[type=file]").setInputFiles(contextClip);
    await page.waitForFunction(() =>
      !/未选择/.test(document.querySelector(".qv-shot-context .qv-file-name")?.textContent || "未选择"),
      null, { timeout: 10000 });
    console.log("前文视频:", await page.locator(".qv-shot-context .qv-file-name").textContent());

    // A refused click has to be visible right next to the button: the status card is
    // at the top of a long page, and a silent failure reads as a dead button.
    await page.route("**/local/video/generate", route => route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ detail: "已有视频任务正在运行，请等待完成或先取消。" }),
    }));
    await page.getByRole("button", { name: /开始生成|生成视频/ }).click();
    await page.waitForTimeout(1200);
    const refused = await page.evaluate(() => {
      const note = document.querySelector(".qv-action-note");
      const button = [...document.querySelectorAll(".qv-button")].find(el => /开始生成/.test(el.textContent));
      return { hidden: note?.hidden, tone: note?.dataset.tone, text: note?.textContent, buttonDisabled: button?.disabled };
    });
    console.log("被拒绝的点击:", JSON.stringify(refused));

    // Then a stubbed accept, so the check never queues a real ten-minute run.
    let submitted = null;
    const film = "D:/AI/_audit/h3-fight-ab/fight-video_quality/master.mp4";
    const filmUrl = "/local/video/file?path=" + encodeURIComponent(film);
    const shotRows = plan => (plan || []).map((shot, index) => ({
      index: index + 1, seconds: shot.seconds, steps: shot.steps, seed: shot.seed,
      state: "running", estimate: 520, started: Date.now() / 1000 - 96, finished: null, elapsed: null,
    }));
    // Take the prompt id of whatever the node is running right now, so the per-step
    // telemetry path (preview, step counter, timings) is exercised for real. Only when
    // a render is genuinely in flight: the node keeps its last state around after it
    // finishes, and the panel rightly shows nothing for a finished run.
    const live = await (await fetch(origin + "/local/video/comfy")).json().catch(() => null);
    const liveRunning = (live?.queue?.running || 0) > 0 &&
      !["completed", "idle", "interrupted", "error"].includes(live?.monitor?.phase);
    const livePromptId = liveRunning ? (live?.monitor?.prompt_id || null) : null;
    console.log("实时遥测:", JSON.stringify({ promptId: livePromptId, running: liveRunning,
      phase: live?.monitor?.phase, step: live?.monitor?.step, queue: live?.queue }));
    const snapshot = (state, shots) => ({
      id: "vid-check", recipe: "video_quality", state,
      phase: state === "done" ? "done" : "generating",
      progress: state === "done" ? 1 : 0.18, elapsed: state === "done" ? 1120 : 96,
      video: state === "done" ? filmUrl : null, path: state === "done" ? film : null,
      report: null, shots, position: state === "done" ? null : 1, total: shots.length,
      eta: state === "done" ? 0 : 640, prompt_id: livePromptId || "abc123",
      settings: { width: 768, height: 512, output_root: "D:/AI/_audit/h3-fight-ab",
                  project_name: "check", structure_guard: false },
      notes: [
        { t: 0, phase: "accepted", message: "质量优先 · 20 步原生" },
        { t: 0.05, phase: "starting_comfy", message: "" },
        { t: 0.2, phase: "building_graph", message: "第 1/2 段" },
        { t: 0.35, phase: "submitting", message: "第 1/2 段 · 36 个节点" },
        { t: 0.4, phase: "generating", message: "第 1/2 段 · 20 步" },
      ],
    });
    await page.unroute("**/local/video/generate");
    await page.route("**/local/video/generate", async route => {
      submitted = route.request().postDataJSON();
      // Keep the run alive through the progress route as well: the panel stops
      // polling as soon as it sees nothing running.
      progressJob = snapshot("running", shotRows(submitted.shot_plan));
      await route.fulfill({ status: 202, contentType: "application/json",
        body: JSON.stringify({ job: progressJob }) });
    });
    // The progress route keeps the run alive so the monitor has something to show.
    await page.unroute("**/local/video/progress*");
    let progressJob = null;
    await page.route("**/local/video/progress*", route => route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ busy: false, job: progressJob }),
    }));
    const before = await page.evaluate(async () => (await (await fetch("/local/video/history", { cache: "no-store" })).json()).history.length);
    await page.getByRole("button", { name: /开始生成|生成视频/ }).click();
    await page.waitForTimeout(2500);
    console.log("post body:", JSON.stringify(submitted, null, 1));
    const refs = submitted?.shot_references;
    console.log("随任务提交的参考图:", JSON.stringify(refs));
    if (!(refs?.[0]?.length) || !/^qwen-image-ref-[0-9a-f]+\.(png|jpg|jpeg|webp)$/.test(refs[0][0])) {
      throw new Error(`参考图没有随任务提交：${JSON.stringify(refs)}`);
    }
    const contextName = submitted?.first_shot_context;
    console.log("随任务提交的前文视频:", JSON.stringify(contextName));
    if (!/^qwen-video-[0-9a-f]+\.(mp4|mov|webm)$/.test(contextName || "")) {
      throw new Error(`前文视频没有随任务提交：${JSON.stringify(contextName)}`);
    }
    const after = await page.evaluate(() => ({
      status: document.querySelector(".qv-status")?.textContent,
      note: document.querySelector(".qv-action-note")?.textContent,
      noteHidden: document.querySelector(".qv-action-note")?.hidden,
      history: document.querySelectorAll(".qv-history-item").length,
      disabled: document.querySelector(".qv-primary")?.disabled,
      switchText: [...document.querySelectorAll(".qv-toggle-label")].map(node => node.textContent),
    }));
    console.log("status line:", JSON.stringify(after), "history before:", before);
    if (!("structure_guard" in (submitted || {}))) {
      throw new Error(`提交体里没有结构约束开关：${JSON.stringify(Object.keys(submitted || {}))}`);
    }
    if ("motion_guard" in (submitted || {})) throw new Error("提交体里还留着保守缓存开关");
    if (!after.switchText?.some(text => /结构约束/.test(text))) {
      throw new Error(`工作流卡片没有结构约束开关：${JSON.stringify(after.switchText)}`);
    }

    // With the run still being reported, the monitor shows the shot table and the flow.
    await page.waitForFunction(() => document.querySelectorAll(".qv-shot-row").length > 0, null,
                               { timeout: 15000 });
    await page.waitForTimeout(2500);
    const monitor = await page.evaluate(() => ({
      badge: document.querySelector(".qv-monitor-badge")?.textContent,
      title: document.querySelector(".qv-monitor .qv-card-title")?.textContent,
      hint: document.querySelector(".qv-monitor .qv-card-hint")?.textContent,
      label: document.querySelector(".qv-progress-percent")?.textContent,
      tiles: [...document.querySelectorAll(".qv-monitor .qv-metric")].map(tile => ({
        label: tile.querySelector(".qv-metric-label")?.textContent,
        value: tile.querySelector(".qv-metric-value")?.textContent,
      })),
      settings: [...document.querySelectorAll(".qv-monitor-line")].pop()?.textContent,
      preview: (() => {
        const box = document.querySelector(".qv-preview");
        return { hidden: box?.hidden, src: (document.querySelector(".qv-preview-image")?.getAttribute("src") || "").slice(0, 24) };
      })(),
      lines: [...document.querySelectorAll(".qv-monitor-line")].map(n => n.textContent),
      shots: [...document.querySelectorAll(".qv-shot-row")].map(row => row.textContent),
      // The flow block replaced the scrolling log: a segmented bar plus a checklist.
      flow: {
        percent: document.querySelector(".qv-flow-percent")?.textContent,
        segments: document.querySelectorAll(".qv-flow-segment").length,
        active: document.querySelectorAll('.qv-flow-segment[data-state="active"]').length,
        steps: [...document.querySelectorAll(".qv-flow-step")].map(s => s.textContent),
      },
      logs: document.querySelectorAll(".qv-log-row").length,
      report: (() => {
        const node = document.querySelector(".qv-report");
        return { present: Boolean(node), visible: Boolean(node && node.offsetParent) };
      })(),
      sticky: getComputedStyle(document.querySelector(".qv-column-monitor")).position,
    }));
    console.log("监控卡片:", JSON.stringify(monitor, null, 1));
    if (monitor.sticky !== "sticky") throw new Error("监控栏没有固定在左侧");
    if (!monitor.shots.length) throw new Error("监控卡片缺少分段");
    if (monitor.logs) throw new Error("过程记录列表应当已经被流程进度条取代");
    if (monitor.flow.segments < 5 || !monitor.flow.steps.length) {
      throw new Error(`流程进度条不完整：${JSON.stringify(monitor.flow)}`);
    }
    if (!/^\d+%$/.test(monitor.flow.percent || "")) {
      throw new Error(`流程百分比不可读：${monitor.flow.percent}`);
    }
    // The raw director report must not be on screen any more.
    if (monitor.report.visible) throw new Error("成片下方的 {} 报告仍在显示");
    const tileText = monitor.tiles.map(tile => `${tile.label}${tile.value}`).join(" ");
    if (!/已用时间/.test(tileText)) throw new Error(`监控缺少统计瓦片：${JSON.stringify(monitor.tiles)}`);
    if (/总显存/.test(tileText)) throw new Error("显存瓦片应当已经迁到右栏");
    if (/null×null/.test(monitor.settings || "")) throw new Error(`设置行出现 null×null：${monitor.settings}`);

    // VRAM and queue now live under the film in the right rail.
    const gpuCard = await page.evaluate(() => {
      const card = document.querySelector(".qv-gpu-card");
      const side = document.querySelector(".qv-column-side");
      return {
        inSideRail: Boolean(card && side && side.contains(card)),
        afterResult: (() => {
          const cards = [...side.querySelectorAll(".qv-card-title")].map(n => n.textContent);
          return cards;
        })(),
        tiles: [...(card?.querySelectorAll(".qv-metric") || [])].map(tile => ({
          label: tile.querySelector(".qv-metric-label")?.textContent,
          value: tile.querySelector(".qv-metric-value")?.textContent,
        })),
        footer: card?.querySelector(".qv-card-hint")?.textContent,
      };
    });
    console.log("显存卡片:", JSON.stringify(gpuCard));
    if (!gpuCard.inSideRail) throw new Error("显存与队列不在右栏");
    if (!["成片", "显存与队列", "最近任务"].every(t => gpuCard.afterResult.includes(t))) {
      throw new Error(`右栏卡片顺序不对：${JSON.stringify(gpuCard.afterResult)}`);
    }
    const gpuText = gpuCard.tiles.map(tile => `${tile.label}${tile.value}`).join(" ");
    if (!/总显存\s*1[0-9]/.test(gpuText)) throw new Error(`显存瓦片没有实时读数：${gpuText}`);
    if (!/队列/.test(gpuCard.footer || "")) throw new Error(`队列没有跟着显存一起迁移：${gpuCard.footer}`);
    if (livePromptId && !/第 \d+\/\d+ 步/.test(monitor.lines.join(" "))) {
      throw new Error(`监控没有显示逐步遥测：${JSON.stringify(monitor.lines)}`);
    }
    // The live monitor with its sampler preview is the point of the rail, so keep a
    // picture of it while a real run is sampling.
    await page.locator(".qv-column-monitor").screenshot({
      path: path.join(__dirname, "../logs/image-panel-validation/video-monitor-live.png") });
    progressJob = snapshot("done", shotRows(submitted.shot_plan).map(row => ({
      ...row, state: "done", elapsed: 540, started: null })));
    await page.waitForTimeout(2500);

    // The RIFE post-process: the button appears with a film, posts its absolute path,
    // and the panel switches to the interpolated cut when the job reports done.
    await page.route("**/local/video/interpolate", async route => {
      const body = route.request().postDataJSON();
      console.log("补帧请求:", JSON.stringify(body));
      if (body?.path !== film || body?.multiplier !== 2) throw new Error(`补帧请求不对：${JSON.stringify(body)}`);
      await route.fulfill({ status: 202, contentType: "application/json",
        body: JSON.stringify({ job: { id: "itp-check", state: "running", phase: "interpolating", progress: 0.2, elapsed: 5, source: film, target: null, video: null } }) });
    });
    const interpolated = film.replace(".mp4", "-48fps.mp4");
    await page.route("**/local/video/interpolate/progress*", route => route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ busy: false, job: { id: "itp-check", state: "done", phase: "done", progress: 1, elapsed: 42,
        source: film, target: interpolated, video: "/local/video/file?path=" + encodeURIComponent(interpolated) } }),
    }));
    const interpButton = page.getByRole("button", { name: /RIFE 补帧/ });
    const interpState = await page.evaluate(() => ({
      rows: document.querySelectorAll(".qv-actions").length,
      hiddenRows: [...document.querySelectorAll(".qv-actions")].map(row => row.hidden),
      notes: [...document.querySelectorAll(".qv-action-note")].map(note => note.textContent),
      buttons: [...document.querySelectorAll("button")].map(b => b.textContent).filter(t => /补帧|RIFE/.test(t)),
    }));
    console.log("补帧前的界面:", JSON.stringify(interpState), "errors:", JSON.stringify(errors));
    await interpButton.click();
    await page.waitForTimeout(2500);
    const interp = await page.evaluate(() => ({
      note: document.querySelectorAll(".qv-action-note")[1]?.textContent,
      src: document.querySelector(".qv-video")?.getAttribute("src"),
    }));
    console.log("补帧结果:", JSON.stringify(interp), "errors:", JSON.stringify(errors));
    if (!/补帧完成/.test(interp.note || "")) throw new Error(`补帧没有走完：${JSON.stringify(interp)}`);
    if (!/48fps/.test(interp.src || "")) throw new Error(`面板没有切到补帧后的文件：${interp.src}`);
    console.log("video requests:", JSON.stringify(seen, null, 1));
    console.log("errors:", JSON.stringify(errors, null, 1));
    // The panel is its own scrolling overlay, so the card order is only visible
    // from the top of it.
    await page.locator("#local-video-panel").evaluate(node => { node.scrollTop = 0; });
    await page.waitForTimeout(300);
    // Not scrolled yet: the rail must already sit at the top of the page instead of
    // sliding up under a full-width header once the wheel moves.
    const railAtRest = await page.evaluate(() => {
      const rail = document.querySelector(".qv-column-monitor");
      const main = document.querySelector(".qv-column-main");
      return { railTop: Math.round(rail.getBoundingClientRect().top),
               mainTop: Math.round(main.getBoundingClientRect().top) };
    });
    console.log("未滚动时监控栏位置:", JSON.stringify(railAtRest));
    if (railAtRest.railTop > 140 || Math.abs(railAtRest.railTop - railAtRest.mainTop) > 30) {
      throw new Error(`监控栏没有和其它栏对齐在顶部：${JSON.stringify(railAtRest)}`);
    }
    await page.screenshot({ path: path.join(__dirname, "../logs/image-panel-validation/video-panel-top.png") });
    // The monitor is sticky, so scrolling to the bottom must keep it on screen.
    await page.locator("#local-video-panel").evaluate(node => { node.scrollTop = node.scrollHeight; });
    await page.waitForTimeout(600);
    const stickyVisible = await page.evaluate(() => {
      const rail = document.querySelector(".qv-column-monitor");
      const rect = rail.getBoundingClientRect();
      return { top: Math.round(rect.top), visible: rect.top >= 0 && rect.top < 200,
               height: Math.round(rect.height), scrollable: rail.scrollHeight > rail.clientHeight + 4,
               overflowY: getComputedStyle(rail).overflowY,
               viewport: window.innerHeight };
    });
    console.log("滚到底部后监控栏位置:", JSON.stringify(stickyVisible));
    if (!stickyVisible.visible) throw new Error(`滚到底部后监控栏跑出屏幕：${JSON.stringify(stickyVisible)}`);
    // The rail pins itself and scrolls internally, so it must fit the viewport instead
    // of growing past it (which is what pushed the top out of view before).
    if (stickyVisible.height > stickyVisible.viewport - 4) {
      throw new Error(`监控栏比视口还高，无法常驻：${JSON.stringify(stickyVisible)}`);
    }
    // And it must not scroll at all: nothing in the rail may move when the page does.
    if (stickyVisible.scrollable || stickyVisible.overflowY !== "hidden") {
      throw new Error(`监控栏出现了自己的滚动条：${JSON.stringify(stickyVisible)}`);
    }
    // Show the rail from its own top in the delivered screenshot.
    await page.locator(".qv-column-monitor").evaluate(node => { node.scrollTop = 0; });
    await page.waitForTimeout(200);
    await page.screenshot({ path: path.join(__dirname, "../logs/image-panel-validation/video-panel.png") });
    // The shot card gets its own shot: per-shot settings and the reference row with
    // its 清除 button only exist inside it.
    await page.locator(".qv-shot").first().screenshot({ path: path.join(__dirname, "../logs/image-panel-validation/video-shot-card.png") });
    // The save-location chooser gets its own shot, since it only exists while open.
    await page.getByRole("button", { name: "选择…" }).click();
    await page.locator(".qv-browser:not([hidden])").waitFor({ timeout: 10000 });
    await page.waitForFunction(() => document.querySelectorAll(".qv-browser-roots button").length > 0, null, { timeout: 10000 });
    await page.waitForTimeout(500);
    await page.screenshot({ path: path.join(__dirname, "../logs/image-panel-validation/video-panel-browse.png") });
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
