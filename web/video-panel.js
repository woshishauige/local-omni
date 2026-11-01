/**
 * Video view for the local Qwen gateway.
 *
 * A peer of the generate view: mode-control.js mounts it and toggles visibility
 * through setActive(). Two sub-modes share the screen area:
 *
 *   视频生成  H3 recipes (quality / speed) driven by /local/video/*
 *   超分优化  SeedVR2 upscaling driven by the existing /local/image/* pipeline
 *
 * The two sub-modes talk to different endpoints on purpose: video generation owns
 * a job monitor that survives a ten-minute run, while upscaling reuses the image
 * pipeline that already understands SeedVR2's node wiring.
 */
(() => {
  "use strict";

  if (window.VideoPanel) return;

  const words = {
    title: "视频生成",
    subtitle: "MiniMax-H3 逐段生成 · SeedVR2 放大修复",
    subGenerate: "视频生成",
    subStitch: "视频拼接",
    stitchTitle: "把分段的片子拼回一条",
    stitchPaneHint: "长片受显存限制只能分段放大或分段生成，处理完在这里按先后拼回一条：前段先播、后段后播。",
    subUpscale: "超分重建",
    subDenoise: "降噪",
    denoiseTitle: "降噪（nlmeans · CPU 后处理）",
    denoisePaneHint:
      "单独的工序，不放大、不重建、不占显存：把选中的片段交给 ffmpeg 在 CPU 上跑非局部均值。",
    denoiseSource: "降噪素材",
    recipes: "工作流",
    loraStrength: "蒸馏强度",
    loraStrengthHint:
      "蒸馏 LoRA 的强度：1.0 最锐最细，0.85 实测噪声低 11–18%、轮廓更稳、细节少 7–19%." +
      "只有挂着蒸馏 LoRA 的线路会显示这一项，默认取工作流文件里的值（0.85）。",
    structureGuard: "结构约束（单主体 · 中景 · 每段一个动作）",
    structureGuardHint:
      "在每段提示词后自动追加一句结构约束：画面里只留一个人、景别保持中景或更近、一段只安排一个动作、镜头稳住。" +
      "打开后同框主体不再互相遮挡，肢体与边缘更完整，画面更稳；代价是构图更保守、场面更小。" +
      "关掉就完全按你写的描述来。",
    prompt: "画面描述",
    promptHint: "描述整条片子的内容与风格；H3 会按故事板逐段生成，再把各段拼成一条成片。",
    enhance: "用当前模型优化提示词",
    enhanceHint:
      "把整片描述交给当前选中的模型扩写成一条可直接拍摄的画面描述（主体动作 + 环境光线 + 镜头运动）。" +
      "打开后会先加载那个模型、写完提示词再交还给 ComfyUI，所以每次任务多等一次模型加载；" +
      "各段自己填的提示词不会被改写。",
    generate: "开始生成",
    cancel: "取消",
    idle: "空闲",
    estimated: "预估",
    elapsed: "已用",
    result: "成片",
    noResult: "还没有成片",
    interpolate: "RIFE 补帧 ×2（24→48fps）",
    interpolateHint: "把成片的帧率翻倍：每帧位移减半、帧间不连贯降约 4 成，代价是画面略软。补好的文件与原片放在一起。",
    denoise: "降噪收尾（nlmeans）",
    denoiseHint: "ffmpeg 在 CPU 上跑非局部均值降噪（s=2.0）：本机实测噪声降约 26%、细节只掉 5%，人脸区反而更清晰、轮廓更实；不占显存、不需要模型，几秒到一分钟。降好的文件与原片放在一起。",
    history: "最近任务",
    failed: "失败",
    submitting: "正在提交…",
    clickFailed: reason => `操作失败：${reason}`,
    pleaseDescribe: "请先填写整片描述，或至少给第一段写提示词",
    playFilm: "播放成片",
    upscalePipeline: "放大工作流",
    upscaleHint: "放大不需要提示词：选一张图或一段视频，SeedVR2 会放大并补回细节。这里只管放大，颗粒交给「降噪」。",
    references: "放大素材",
    upscaleGenerate: "开始放大",
    upscaleLimit:
      "各倍率支持的最长视频：1.0× → 10 秒；1.25× → 7.5 秒；1.5× → 7.5 秒。" +
      "更长的片子请按段放大，再用「视频拼接」合回一条。",
    upscaleStitch: "拼接片段",
    upscaleStitchHint: "把分段放大（或分段生成）的片子按格号拼回一条：1 先播、4 最后；空格子会被跳过，至少要填两格。分辨率与帧率要一致（同一条片子的分段天然一致）。",
    stitchClear: "清空",
    stitchNormalize: "拼接时对齐亮度（按接缝）",
    stitchNormalizeHint:
      "让每个接缝两侧的亮度对齐（按上一段末帧与这一段首帧的差值累积平移），专治逐段变亮/变暗的漂移；" +
      "不改动画面其它内容，实际修正量会显示在结果里。注意它只修亮度，改不了模型在生成时决定的构图变化。",
    upscaleStrength: "重绘占比（超分强度）",
    upscaleStrengthHint:
      "SeedVR2 逐帧重画纹理，这就是头发/眼部动起来「跳得快」的来源（同分辨率实测：纯 SeedVR2 纹理区跳变 5.33、" +
      "纯 lanczos 放大 4.70）。1.0 全保留；调低会与普通放大按比例混合，越低调越稳、细节也越少（0.7 时细节 −27%）。",
    upscaleSteps: "超分步数（SeedVR2）",
    upscaleStepsHint:
      "SeedVR2 自己的采样步数：1 步最干净最快、伪影最少；步数越高补回的纹理越多、也越容易带噪。" +
      "默认取所选配方里的值，可随时改。",
    denoiseStrength: "降噪强度（nlmeans）",
    denoiseStrengthHint:
      "1.0 最轻、3.5 最重，默认 2.0（实测噪声 −26%、细节 −5%）；3.0 时 −34% / −10%。" +
      "顺序不能反：先「超分重建」补细节、再来这里降噪（反过来实测噪声反而 +11%）。" +
      "整条链路（1.25× 超分 + 降噪 2.0）实测噪声 −31%、细节 +28%。CPU 后处理，不占显存。",
    denoiseRun: "开始降噪",
    denoiseNote:
      "降噪只动颗粒、不重建画面，所以它既不会更清晰也不会更模糊。跑完的文件与原片放在一起（文件名带 -denoised）。",
    minutes: "分钟",
    shotsTitle: "段落规划",
    shotContinuity: "接法",
    shotContinue: "续接",
    shotCut: "切镜",
    shotContinuityHint:
      "续接：本段开头钉住上一段的最后 22 帧（同一条动作往下走时用它）；" +
      "切镜：本段从零开始，不贴上一段尾巴（分镜切换、每段各自成镜时用它，接缝不会再有硬贴的顿挫）。",
    shotsUnit: "段",
    shotsNote: (count, totalSeconds) =>
      `H3 按段依次渲染，再把各段拼成一条成片：${count} 段共约 ${totalSeconds.toFixed(1)} 秒画面。` +
      `每段的时长、步数与种子都在这里单独设置，改哪段就只影响哪段。` +
      `第 2 段起会自动引用第 1 段画面保持人物一致（工作流内置，无需上传）。` +
      `段落越长、动作越密，画面越容易出现肢体缺失与结构不稳；模型本身没有物理约束，` +
      `画面里主体越少、景别越近就越稳，代价是场面更小、人物更容易出画。` +
      `所以动作与近景适合拆到不同段，而不是挤在一段里。`,
    shotLabel: index => `第 ${index} 段`,
    shotPromptPlaceholder: "这一段发生什么（不填则用整片描述）",
    shotRef: "参考图（可选，最多 5 张）",
    clearRefs: "清除",
    shotContext: "前文视频（可选，只对第 1 段）",
    shotContextShort: "前文视频",
    contextNone: "未选择",
    shotContextHint: "第 1 段本来没有上文。选一段视频后，这一段的续接方式与「第 2 段接第 1 段」完全一样：" +
      "取它的最后 22 帧做运动上下文、最后 1 秒做音频上下文、最后一帧做人物与场景参考。" +
      "视频会被缩放到本次渲染的尺寸与 24 fps；没有音轨会自动补一段静音。至少 1 秒长。",
    shotCost: "预计",
    shotCostHint: "按本机实测推算：每段首步约 72 秒、之后每步约 33 秒（7.5 秒画面）；蒸馏线路直接用该线路的实测秒数。",
    estimateHint: "下限=模型已加载，上限=本次会话首次运行（需额外加载模型约 10 分钟）。",
    paramsTitle: "画面尺寸（全片统一）",
    resetParams: "恢复推荐值",
    globalParamsNote:
      "各段要拼成同一条成片，因此宽高必须一致，只有这里是全片统一设置；" +
      "时长、步数、种子在上面每段里单独调。" +
      "分辨率越高，细节与轮廓越完整，采样耗时与显存占用也同步上升；" +
      "它还会影响构图倾向：分辨率越高，模型越容易把人物画得小一些、远一些。" +
      "每段耗时随时长与步数近似线性增长，本会话首次运行还要额外加载模型。",
    randomSeed: "随机",
    randomSeedHint: "换一个随机种子；每段互不影响。",
    outputTitle: "保存位置",
    output: "存储路径",
    outputHint: "成片写在这个文件夹下面。默认 ComfyUI/output/video/，点「选择…」可以换到别的盘。",
    browse: "选择…",
    useDefault: "用默认",
    outputPreview: "成片将写到",
    outputExists: "已存在",
    outputWillCreate: "将新建",
    outputPathMissing: "现在还没有这个文件夹，开始生成时会创建",
    outputPathBad: "存储路径要填绝对路径，例如 D:\\AI\\ComfyUI-BSAI_v010\\ComfyUI\\output\\video",
    outputNameBad: "保存名称不能含 < > : \" / \\ | ? *，也不能以点或空格结尾",
    outputNameFallback: "不填就用工作流自带的名字",
    browseUp: "上一级",
    browseUse: "用这个文件夹",
    browseEmpty: "这个文件夹里没有子文件夹",
    browseLoading: "读取中…",
    project: "保存名称（项目文件夹）",
    projectHint: "这段片子独占的子文件夹名字，例如 pian1；留空则用工作流自带的名字。",
    paramNames: {
      width: "宽度",
      height: "高度",
      seconds: "时长（秒）",
      steps: "采样步数",
      seed: "种子",
    },
    phaseNames: {
      queued: "排队中", accepted: "已受理", starting_comfy: "启动 ComfyUI",
      building_graph: "构建图", submitting: "提交任务", generating: "生成中",
      done: "完成", cancel_requested: "正在取消", idle: "空闲",
      staging: "准备素材", interpolating: "补帧中",
      enhancing: "提示词优化", enhanced: "提示词优化",
    },
    flowTitle: "流程",
    flowStages: {
      accepted: "受理", enhancing: "提示词优化", starting_comfy: "启动",
      building_graph: "构建图",
      submitting: "提交", generating: "逐段生成", done: "拼接完成",
    },
    shotStates: { pending: "等待", running: "生成中", done: "完成", error: "失败" },
    progressTitle: "生成进度",
    idleHint: "填好画面描述与段落规划后点「开始生成」：这里会实时显示每段进度、逐步遥测、采样预览与整体流程。",
    metricElapsed: "已用时间", metricEta: "预计剩余", metricSpeed: "速度", metricSampling: "采样用时",
    metricEstimate: "整片预估",
    metricEstimateTip: "下限=模型已加载，上限=本次会话首次运行（第一次还要加载模型）。",
    previewLabel: "采样预览",
    previewHint: "由采样回调输出（最近几帧拼在一起），只用于判断构图",
    comfyPhases: {
      idle: "空闲", encoding: "编码", sampling: "采样", decoding: "解码",
      saving: "保存", completed: "完成", interrupted: "已中断", error: "失败",
    },
    stepOf: (step, steps) => `第 ${step}/${steps} 步`,
    secondsPerStep: seconds => `${seconds.toFixed(0)} 秒/步`,
    segmentLeft: seconds => `本段剩余 ≈ ${formatDuration(seconds)}`,
    used: "已用", usedUnknown: "已用 —", estimateShort: "预计",
    vramTotal: "总显存", vramUsed: "已用", vramFree: "剩余", vramPeak: "本次峰值",
    vramCard: "显存与队列",
    vramUtil: "GPU 利用率", queue: "队列",
    vramUnavailable: "读不到显存信息（nvidia-smi 不可用）",
    vramBaseline: "空闲基线", vramDelta: "本次新增",
    vramFreeTip: "此刻整卡剩余可用显存（渲染期间通常只剩几百 MB，说明卡已经被吃满）。",
    vramPeakTip: "本次生成期间见过的最高「已用显存」。",
    vramUsedTip: "此刻整卡已用显存（含其他程序）。",
    vramDeltaTip: "本次生成比「开始前的空闲占用」多了多少。视频链路渲染前会先卸载文本模型，" +
                  "所以这个增量常是 0 —— 显存是被换出来的，不是凭空多出来的；看「本次峰值」更实在。",
    lastSpeed: "上一段", averageSpeed: "平均每段", successRate: "成功率",
    enhanceResultTitle: "提示词优化",
    enhanceOriginal: "原文",
    enhanceRewritten: "改后（各段继承）",
    enhanceSkipped: "未优化",
  };

  const state = {
    active: false,
    submode: "generate",
    recipes: [],
    recipe: null,
    upscaleRecipes: [],
    upscaleRecipe: null,
    // SeedVR2's step count and redraw ratio, both re-seeded from the chosen recipe.
    upscaleSteps: null,
    // SeedVR2's redraw ratio; 1.0 means "keep its output as it is".
    upscaleStrength: 1,
    // nlmeans strength of the denoise module, which is a separate step from the upscale.
    denoiseStrength: 2.0,
    // The four stitching slots, keyed by their number: the number is the playback order.
    stitch: { 1: null, 2: null, 3: null, 4: null },
    job: null,
    busy: false,
    render: null,
    poll: null,
    shotCount: 1,
    // Per-shot settings. The director renders one shot per submission, so each
    // shot carries its own duration, step count and seed; width and height stay
    // global because the clips are concatenated into one film.
    shotPrompts: {},
    shotRefs: {},
    firstShotContext: null,
    shotSeconds: {},
    shotSteps: {},
    shotSeeds: {},
    // Per-shot join style: "continue" (default) or "cut".
    shotContinuity: {},
    // Distillation LoRA strength. null means "whatever the recipe's workflow file holds";
    // picking a recipe that ships a LoRA fills it in from that file.
    loraStrength: null,
    params: { width: 768, height: 512 },
    // Save location: what the panel shows, what the gateway calls the default, and
    // where the folder chooser currently is.
    outputDefault: "",
    browsePath: null,
    browseParent: null,
    // The last finished film and whether a RIFE interpolation is running for it.
    lastPath: null,
    interpolating: false,
    // The interpolated cut of the film currently on screen, once it exists.
    interpolated: null,
    // VRAM: the sample, when it was taken, the idle baseline and the run's peak.
    gpu: null,
    gpuAt: 0,
    gpuIdle: null,
    gpuBaseline: null,
    gpuPeak: 0,
    // Measured on this machine: the first run of a session pays a one-off model
    // initialisation of about ten minutes, later runs about seventy seconds, and a
    // shot's first sampling step costs roughly twice a steady step.
    // Per-machine timings used by the shot and film estimates. coldInit/warmInit are H3's own
    // load, firstStep/steadyStep the sampling; enhance is the language-model call (measured
    // 1.6–1.7 s warm, 12.2 s on its first call) and enhanceCold is bringing that model back up
    // (measured 25.49 s: "Backend ready: mode=text seconds=25.49" in the manager log).
    timing: { coldInit: 598, warmInit: 72, firstStep: 72, steadyStep: 33,
              enhance: 2, enhanceCold: 26 },
    // 动态代价模型（来自服务端 `/local/video/recipes` 的 `cost_model`）：预计时间由它算，
    // 时长、步数、尺寸一起参与，并随真实跑完的样本校准。
    costModel: null,
    // Mirrors the 提示词优化 switch, because the estimate and the flow are drawn before the
    // switch itself exists (the progress rail is built first).
    enhance: false,
  };

  // What the shipped workflows were measured at; the reset button and any shot the
  // user has not touched fall back to these.
  const RECOMMENDED = { width: 768, height: 512, seconds: 7.5, steps: 20 };

  function make(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text) el.textContent = text;
    return el;
  }

  function button(label, className, handler) {
    const el = make("button", className || "qv-button", label);
    el.type = "button";
    // Every click reports its own outcome. A handler that throws used to leave the
    // page looking untouched, which is indistinguishable from a dead button.
    el.addEventListener("click", event => {
      try {
        const outcome = handler(event);
        if (outcome && typeof outcome.catch === "function") outcome.catch(reportFailure);
      } catch (error) {
        reportFailure(error);
      }
    });
    return el;
  }

  function field(label, hint) {
    const wrap = make("label", "qv-field");
    wrap.append(make("span", "qv-label", label));
    if (hint) wrap.append(make("span", "qv-hint", hint));
    return wrap;
  }

  async function api(path, options) {
    // A `raw` body is sent as-is so a large clip is not encoded into JSON or a
    // base64 data URL on the way to the upload route.
    const raw = options?.raw;
    const response = await fetch(path, {
      method: options?.method || "GET",
      headers: options?.body ? { "Content-Type": "application/json" } : undefined,
      body: raw || (options?.body ? JSON.stringify(options.body) : undefined),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      // The status code travels with the message: a rejected request has to be
      // diagnosable from the panel alone.
      const detail = payload.detail || payload.error || "请求被拒绝";
      throw new Error(`${detail}（HTTP ${response.status}）`);
    }
    return payload;
  }

  // -- shell ------------------------------------------------------------------
  const root = make("div", "qv-root");
  root.id = "local-video-panel";
  root.hidden = true;

  const header = make("header", "qv-header");
  header.append(make("h2", "qv-title", words.title),
                make("p", "qv-subtitle", words.subtitle));
  const submodeRow = make("div", "qv-submodes");
  const submodeButtons = {};
  // Four sub-modes: generating a film, rebuilding resolution (SeedVR2), denoising, and
  // stitching separately processed segments back together. Denoising is not a property of
  // an upscale and stitching is not an upscale at all, so each owns its own pane instead
  // of sharing the upscale card.
  for (const [key, label] of [["generate", words.subGenerate], ["upscale", words.subUpscale],
                              ["denoise", words.subDenoise], ["stitch", words.subStitch]]) {
    const el = button(label, "qv-submode", () => setSubmode(key));
    el.dataset.submode = key;
    submodeButtons[key] = el;
    submodeRow.append(el);
  }
  header.append(submodeRow);

  const body = make("div", "qv-body");
  // Three rails: the monitor on the left, the settings in the middle, the film and
  // history on the right — a run's state and its result each get their own column
  // instead of competing with the settings for attention. The title block lives in
  // the middle rail so all three columns start on the same line and the progress
  // module can sit at the very top of the page instead of sliding up under it.
  const monitorColumn = make("div", "qv-column qv-column-monitor");
  const left = make("div", "qv-column qv-column-main");
  const right = make("div", "qv-column qv-column-side");
  left.append(header);
  body.append(monitorColumn, left, right);

  // -- shared status ----------------------------------------------------------
  // The monitor is its own rail: the same language as the generate view — a titled
  // progress card, label+value tiles, a sampler preview — plus the shot-by-shot plan
  // and the process log that only a chained video run has.
  const statusCard = make("section", "qv-card qv-monitor");
  const monitorHead = make("div", "qv-monitor-head");
  monitorHead.append(make("h3", "qv-card-title", words.progressTitle));
  const monitorBadge = make("span", "qv-monitor-badge", words.idle);
  monitorHead.append(monitorBadge);
  const monitorHint = make("p", "qv-card-hint", words.idleHint);

  const progressTrack = make("div", "qv-track");
  const progressFill = make("div", "qv-track-fill");
  progressTrack.append(progressFill);
  const progressRow = make("div", "qv-progress-row");
  const statusLine = make("p", "qv-status", "");
  const progressLabel = make("span", "qv-progress-percent", "0%");
  progressRow.append(statusLine, progressLabel);

  function tileGrid(specs) {
    const grid = make("div", "qv-metrics");
    const tiles = {};
    for (const [key, label, tip] of specs) {
      const tile = make("div", "qv-metric");
      const value = make("span", "qv-metric-value", "—");
      tile.append(make("span", "qv-metric-label", label), value);
      if (tip) {
        tile.title = tip;
        tile.classList.add("qv-metric-tipped");
      }
      tiles[key] = value;
      grid.append(tile);
    }
    return { grid, tiles };
  }

  const stats = tileGrid([["estimate", words.metricEstimate, words.metricEstimateTip],
                          ["elapsed", words.metricElapsed], ["eta", words.metricEta],
                          ["speed", words.metricSpeed], ["sampling", words.metricSampling]]);

  // Per-step telemetry from ComfyUI: the latent preview the sampler itself produces,
  // the step counter and the per-phase timings.
  const previewBox = make("figure", "qv-preview");
  const previewImage = make("img", "qv-preview-image");
  previewImage.alt = words.previewLabel;
  const previewCaption = make("figcaption", "qv-preview-caption", words.previewLabel);
  const previewHint = make("span", "qv-preview-hint", words.previewHint);
  // The rail has no room for a second line of prose, so the hint rides on the caption.
  previewCaption.title = words.previewHint;
  previewBox.append(previewImage, previewCaption, previewHint);
  previewBox.hidden = true;
  const stepLine = make("div", "qv-monitor-line", "");
  const timingLine = make("div", "qv-monitor-line", "");
  const speedLine = make("div", "qv-monitor-line", "");
  const monitorSettings = make("div", "qv-monitor-line", "");
  // What the optimiser actually wrote. The film is generated from this sentence (shots
  // without their own text inherit it), so it belongs in the rail instead of only in the
  // job record: "已优化" alone tells the owner nothing about what changed.
  const promptBox = make("div", "qv-prompt-result");
  promptBox.hidden = true;
  const promptMeta = make("div", "qv-monitor-line", "");
  const promptOriginal = make("p", "qv-prompt-text", "");
  const promptRewritten = make("p", "qv-prompt-text qv-prompt-rewritten", "");
  promptBox.append(make("div", "qv-monitor-subtitle", words.enhanceResultTitle), promptMeta,
                   make("span", "qv-prompt-label", words.enhanceOriginal), promptOriginal,
                   make("span", "qv-prompt-label", words.enhanceRewritten), promptRewritten);
  const shotTable = make("div", "qv-monitor-shots");

  // The whole flow as one bar instead of a log: preparation, the shot chain and the
  // final assembly. Each stage is a segment; the stage the run is in fills as far as
  // the run has actually got.
  const flowTitle = make("div", "qv-monitor-subtitle", words.flowTitle);
  const flowHead = make("div", "qv-flow-head");
  const flowPercent = make("span", "qv-flow-percent", "0%");
  flowHead.append(flowPercent);
  const flowTrack = make("div", "qv-flow-track");
  const flowSteps = make("div", "qv-flow-steps");
  const flowBox = make("div", "qv-flow");
  flowBox.append(flowHead, flowTrack, flowSteps);
  /* The card is a fixed frame with one scrolling body (see .qv-monitor in the stylesheet).
   * Everything below the head is live during a run — the hint, the four telemetry lines, the
   * latent preview, the shot table and the flow bar all appear as the run progresses — and when
   * the card sized itself, each of those arrivals moved the whole page under the owner's cursor
   * ("进度这一个组件仍然会自变换"). The head stays put; the rest scrolls inside a frame whose
   * size is decided by the stylesheet, not by the content. */
  const monitorBody = make("div", "qv-monitor-body");
  monitorBody.append(progressRow, progressTrack,
                     stepLine, stats.grid, previewBox, timingLine,
                     speedLine, monitorSettings, promptBox, shotTable, flowTitle, flowBox, monitorHint);
  statusCard.append(monitorHead, monitorBody);
  monitorColumn.append(statusCard);

  // -- video sub-mode ---------------------------------------------------------
  const videoPane = make("div", "qv-pane");
  const recipeCard = make("section", "qv-card");
  recipeCard.append(make("h3", "qv-card-title", words.recipes));
  const recipeRow = make("div", "qv-recipes");
  recipeCard.append(recipeRow);
  const recipeNote = make("p", "qv-note", "");
  recipeCard.append(recipeNote);

  // Distillation strength: only the recipes that carry a distill LoRA show this, and its
  // default is whatever that workflow file holds (0.85). Measured on sexpro's own shots:
  // 0.85 against 1.0 lowered flat-region noise 11-18% and raised silhouette IoU slightly,
  // for 7-19% less Laplacian detail.
  const loraRow = make("label", "qv-shot-param qv-lora-strength");
  loraRow.append(make("span", "qv-label", words.loraStrength));
  const loraInput = make("input", "qv-shot-param-input");
  loraInput.type = "number";
  loraInput.min = "0.4";
  loraInput.max = "1.2";
  loraInput.step = "0.05";
  loraInput.addEventListener("input", () => {
    const value = Number(loraInput.value);
    if (Number.isFinite(value)) state.loraStrength = Math.min(1.2, Math.max(0.4, value));
    updateEstimate();
  });
  loraRow.append(loraInput);
  loraRow.hidden = true;
  loraRow.title = words.loraStrengthHint;

  // The structure switch appends a measured-effective constraint to every shot's
  // prompt, so it is available for both recipes.
  const guardRow = make("label", "qv-toggle");
  const guardInput = make("input", "qv-toggle-input");
  guardInput.type = "checkbox";
  guardRow.append(guardInput, make("span", "qv-toggle-label", words.structureGuard));
  const guardNote = make("p", "qv-note", words.structureGuardHint);
  recipeCard.append(guardRow, guardNote);

  const promptCard = make("section", "qv-card");
  const promptField = field(words.prompt, words.promptHint);
  const promptInput = make("textarea", "qv-textarea");
  promptInput.rows = 4;
  promptInput.placeholder = "例如：夜晚雨后的城市街道，霓虹在水面反光，镜头缓缓前推……";
  promptField.append(promptInput);
  // Same prompt optimisation as the image view, and the same rule: it runs on the model the
  // owner currently has selected. Opt-in here, because it loads that model first and hands the
  // card back to ComfyUI afterwards — a real cost, unlike in the image view where the prompt is
  // already the first step of a long job.
  const enhanceRow = make("label", "qv-toggle");
  const enhanceInput = make("input", "qv-toggle-input");
  enhanceInput.type = "checkbox";
  enhanceInput.checked = false;
  // The switch owns two things besides the request field: the flow gains (or loses) its
  // 提示词优化 stage, and the film estimate gains (or loses) that step's cost.
  enhanceInput.addEventListener("change", () => {
    state.enhance = enhanceInput.checked;
    renderedFlow = "";
    updateEstimate();
  });
  const enhanceLabel = make("span", "qv-toggle-label", words.enhance);
  enhanceRow.append(enhanceInput, enhanceLabel);
  const enhanceNote = make("p", "qv-note", words.enhanceHint);
  promptCard.append(promptField, enhanceRow, enhanceNote);

  // -- shot plan ---------------------------------------------------------------
  // The gateway submits the director once per shot and stitches the clips into a
  // single master.mp4, so the shot count decides how many prompts, reference
  // images and per-shot settings the panel asks for.
  const shotCard = make("section", "qv-card");
  shotCard.append(make("h3", "qv-card-title", words.shotsTitle));
  const shotRow = make("div", "qv-shot-count");
  const shotButtons = {};
  for (const count of [1, 2, 3, 4]) {
    const el = button(`${count} ${words.shotsUnit}`, "qv-recipe", () => setShotCount(count));
    el.dataset.count = String(count);
    shotButtons[count] = el;
    shotRow.append(el);
  }
  shotCard.append(shotRow);
  const shotNote = make("p", "qv-note", "");
  shotCard.append(shotNote);
  // The distillation strength belongs with the per-shot plan: it applies to every shot of
  // the run, and its value is pre-filled from the selected workflow file (0.85).
  shotCard.append(loraRow);
  const shotList = make("div", "qv-shots");
  shotCard.append(shotList);
  let shotCostNodes = {};

  // -- advanced parameters -----------------------------------------------------
  const paramCard = make("section", "qv-card");
  const paramHead = make("div", "qv-param-head");
  paramHead.append(make("h3", "qv-card-title", words.paramsTitle));
  const resetParams = button(words.resetParams, "qv-link-button", () => resetParameters());
  paramHead.append(resetParams);
  paramCard.append(paramHead);
  const paramGrid = make("div", "qv-params");
  const globalParamInputs = {};
  paramCard.append(paramGrid);
  const paramNote = make("p", "qv-note", words.globalParamsNote);
  paramCard.append(paramNote);
  // Sizes that were actually measured on this machine, so the choice is one click
  // instead of typing two numbers.
  const sizeRow = make("div", "qv-recipes qv-size-presets");
  // Sizes that were actually measured on this machine, so the choice is one click
  // instead of typing two numbers. H3 rejects anything whose sides are not divisible by 32
  // (`H3 needs 24 fps and dimensions divisible by 32.` in the director node), so the high
  // tier is 1024×672: the owner asked for 1024×682, and 682 % 32 = 10 would fail the render.
  const sizePresets = [
    { label: "768×512 · 基准", width: 768, height: 512 },
    { label: "960×640 · 中档", width: 960, height: 640 },
    { label: "1024×672 · 高档", width: 1024, height: 672,
      note: "高档：所有者要的 1024×682 不能被 32 整除（H3 会直接报错），取最近的合法高度 672。" },
  ];
  for (const preset of sizePresets) {
    const el = button(preset.label, "qv-recipe", () => {
      state.params.width = preset.width;
      state.params.height = preset.height;
      renderGlobalParams();
      updateEstimate();
    });
    el.dataset.size = `${preset.width}x${preset.height}`;
    if (preset.note) el.title = preset.note;
    sizeRow.append(el);
  }
  paramCard.append(sizeRow);

  // -- save location -----------------------------------------------------------
  // The node writes one project folder below this path. A browser cannot see the
  // host's filesystem, so the chooser lists folders through the gateway.
  const outputCard = make("section", "qv-card");
  outputCard.append(make("h3", "qv-card-title", words.outputTitle));
  const outputField = field(words.output, words.outputHint);
  const outputRow = make("div", "qv-path-row");
  const outputInput = make("input", "qv-file");
  outputInput.type = "text";
  outputInput.spellcheck = false;
  const browseButton = button(words.browse, "qv-browse", () => toggleBrowser());
  outputRow.append(outputInput, browseButton, button(words.useDefault, "qv-link-button", () => setOutputRoot(state.outputDefault)));
  outputField.append(outputRow);

  const browser = make("div", "qv-browser");
  browser.hidden = true;
  const browserRoots = make("div", "qv-browser-roots");
  const browserHead = make("div", "qv-browser-head");
  const browserPath = make("span", "qv-browser-path", "");
  const browserUp = button(words.browseUp, "qv-browser-button", () => browseTo(state.browseParent));
  const browserUse = button(words.browseUse, "qv-browser-button qv-browser-use", () => {
    setOutputRoot(state.browsePath);
    toggleBrowser(false);
  });
  browserHead.append(browserPath, browserUp, browserUse);
  const browserList = make("div", "qv-browser-list");
  browser.append(browserRoots, browserHead, browserList);
  outputField.append(browser);
  outputCard.append(outputField);

  const projectField = field(words.project, words.projectHint);
  const projectInput = make("input", "qv-file");
  projectInput.type = "text";
  projectInput.spellcheck = false;
  projectInput.placeholder = "未填则用工作流自带的名字";
  projectField.append(projectInput);
  // The two fields only say half of it: what the node is handed is one folder. This line shows
  // that folder as it is typed, and whether it is already there — the answer used to arrive at
  // the end of a shot instead (see _prepare_project_folder).
  const outputPreview = make("p", "qv-output-preview");
  outputPreview.append(make("span", "qv-output-preview-label", words.outputPreview));
  const outputPreviewPath = make("code", "qv-output-preview-path", "—");
  const outputPreviewState = make("span", "qv-output-preview-state", "");
  outputPreview.append(outputPreviewPath, outputPreviewState);
  outputCard.append(projectField, outputPreview);
  outputInput.addEventListener("input", () => refreshOutputPreview());
  projectInput.addEventListener("input", () => refreshOutputPreview());
  let outputCheck = 0;

  // Mirrors the gateway's rule (and the director's own): a name Windows rejects is refused
  // before anything is submitted.
  function outputNameProblem(name) {
    if (!name) return "";
    if (name !== name.replace(/[. ]+$/, "") || /[<>:"/\\|?*\u0000-\u001f]/.test(name)) {
      return words.outputNameBad;
    }
    return "";
  }

  function refreshOutputPreview() {
    const root = outputInput.value.trim() || state.outputDefault;
    const name = projectInput.value.trim();
    const problem = outputNameProblem(name);
    const absolute = /^[a-zA-Z]:[\\/]/.test(root) || root.startsWith("\\\\");
    const sequence = ++outputCheck;
    if (!absolute) {
      outputPreview.dataset.state = "invalid";
      outputPreviewPath.textContent = root || "—";
      outputPreviewState.textContent = words.outputPathBad;
      return;
    }
    if (problem) {
      outputPreview.dataset.state = "invalid";
      outputPreviewPath.textContent = root.replace(/[\\/]+$/, "");
      outputPreviewState.textContent = problem;
      return;
    }
    const base = root.replace(/[\\/]+$/, "");
    const folder = name ? `${base}\\${name}\\` : `${base}\\`;
    outputPreview.dataset.state = "checking";
    outputPreviewPath.textContent = folder;
    outputPreviewState.textContent = name ? "…" : words.outputNameFallback;
    if (!name) return;
    // Whether the folder is already there, asked of the gateway (a page cannot see the disk).
    setTimeout(async () => {
      try {
        const payload = await api(`/local/video/folders?path=${encodeURIComponent(root)}`);
        if (sequence !== outputCheck) return;
        const names = (payload.folders || []).map(entry => entry.name);
        outputPreview.dataset.state = "ready";
        outputPreviewState.textContent = names.includes(name) ? words.outputExists : words.outputWillCreate;
      } catch (failure) {
        if (sequence !== outputCheck) return;
        outputPreview.dataset.state = "ready";
        outputPreviewState.textContent = words.outputPathMissing;
      }
    }, 350);
  }

  const actions = make("div", "qv-actions");
  const generateButton = button(words.generate, "qv-button qv-primary", () => submitVideo());
  const cancelButton = button(words.cancel, "qv-button qv-danger", () => cancelJob());
  cancelButton.hidden = true;
  actions.append(generateButton, cancelButton);
  // The status card sits at the top of a long page, so a run's progress is repeated
  // right here, next to the button that started it — otherwise a click looks like
  // it did nothing for the next ten minutes.
  const actionStatus = make("div", "qv-action-status");
  const actionTrack = make("div", "qv-action-track");
  const actionFill = make("div", "qv-action-fill");
  actionTrack.append(actionFill);
  actionTrack.hidden = true;
  const actionNote = make("p", "qv-action-note", "");
  const actionText = make("span", "qv-action-text", "");
  const playLink = button(words.playFilm, "qv-link-button", () => {
    video.scrollIntoView({ behavior: "smooth", block: "center" });
    void video.play().catch(() => {});
  });
  playLink.hidden = true;
  actionNote.append(actionText, playLink);
  actionStatus.append(actionTrack, actionNote);
  outputCard.append(actions, actionStatus);

  // The monitor lives in its own left rail; the settings read in the order they are
  // used: which workflow, the description it runs on, how that turns into shots,
  // and where the result is written.
  videoPane.append(recipeCard, promptCard, shotCard, paramCard, outputCard);

  // -- upscale sub-mode -------------------------------------------------------
  const upscalePane = make("div", "qv-pane");
  upscalePane.hidden = true;
  const stitchPane = make("div", "qv-pane");
  stitchPane.hidden = true;
  const upscaleCard = make("section", "qv-card");
  upscaleCard.append(make("h3", "qv-card-title", words.upscalePipeline));
  const upscaleRow = make("div", "qv-recipes");
  upscaleCard.append(upscaleRow);
  upscaleCard.append(make("p", "qv-note", words.upscaleHint));

  // The denoise dial lives in the denoise pane: this module only rebuilds texture.
  // SeedVR2's own step count, shown for the same reason the settings rail shows the video
  // routes' steps: the recipe's number is a starting point, not a law. Lower is cleaner and
  // faster, higher restores more texture.
  const upscaleStepsRow = make("label", "qv-shot-param qv-upscale-steps");
  upscaleStepsRow.append(make("span", "qv-label", words.upscaleSteps));
  const upscaleStepsInput = make("input", "qv-shot-param-input");
  upscaleStepsInput.type = "number";
  upscaleStepsInput.min = "1";
  upscaleStepsInput.max = "8";
  upscaleStepsInput.step = "1";
  upscaleStepsInput.addEventListener("input", () => {
    const value = Number(upscaleStepsInput.value);
    if (Number.isFinite(value)) state.upscaleSteps = Math.min(8, Math.max(1, Math.round(value)));
  });
  upscaleStepsRow.append(upscaleStepsInput);
  upscaleStepsRow.title = words.upscaleStepsHint;
  upscaleCard.append(upscaleStepsRow);

  // Redraw ratio: the only strength control SeedVR2 has (its own output mixed back with the
  // plain resize). Lower is steadier in motion and softer; 1.0 is untouched.
  const strengthRow = make("label", "qv-shot-param qv-upscale-strength");
  strengthRow.append(make("span", "qv-label", words.upscaleStrength));
  const strengthInput = make("input", "qv-shot-param-input");
  strengthInput.type = "number";
  strengthInput.min = "0.3";
  strengthInput.max = "1";
  strengthInput.step = "0.05";
  strengthInput.addEventListener("input", () => {
    const value = Number(strengthInput.value);
    if (Number.isFinite(value)) state.upscaleStrength = Math.min(1, Math.max(0.3, value));
  });
  strengthRow.append(strengthInput);
  strengthRow.title = words.upscaleStrengthHint;
  upscaleCard.append(strengthRow);
  // The measured length envelope sits at the top of the module: exceeding it is an OOM,
  // not a slow run, so it has to be visible before a clip is chosen.
  upscaleCard.append(make("p", "qv-note", words.upscaleLimit));

  // Stitching: upscaling is bounded by VRAM, so a long film is processed as segments and
  // rejoined here. Four numbered slots, because the slot number *is* the playback order:
  // 1 plays first, 4 last, and empty slots are skipped.
  const stitchCard = make("div", "qv-stitch");
  stitchCard.append(make("span", "qv-label", words.upscaleStitch));
  const stitchSlots = {};
  for (const index of [1, 2, 3, 4]) {
    const slot = make("label", "qv-stitch-slot");
    slot.append(make("span", "qv-stitch-index", String(index)));
    const input = make("input", "qv-file");
    input.type = "file";
    input.accept = "video/mp4,video/quicktime,video/webm";
    const name = make("span", "qv-file-name", "未选择");
    input.addEventListener("change", () => {
      const file = input.files?.[0] || null;
      state.stitch[index] = file;
      name.textContent = file ? file.name : "未选择";
      stitchNote.textContent = "";
    });
    slot.append(input, name);
    slot.title = words.upscaleStitchHint;
    stitchSlots[index] = { input, name };
    stitchCard.append(slot);
  }
  const stitchNote = make("p", "qv-note", "");
  const stitchRow = make("div", "qv-actions");
  const stitchClear = button(words.stitchClear, "qv-button", () => {
    for (const index of [1, 2, 3, 4]) {
      state.stitch[index] = null;
      stitchSlots[index].input.value = "";
      stitchSlots[index].name.textContent = "未选择";
    }
    stitchNote.textContent = "";
  });
  const stitchButton = button(words.upscaleStitch, "qv-button", () => submitStitch());
  stitchRow.append(stitchButton, stitchClear);
  // Brightness match: each segment is shifted to the first one's mean luma, which is what
  // the chain's shot-to-shot exposure drift shows up as at a join.
  const stitchNormalizeRow = make("label", "qv-toggle");
  const stitchNormalize = make("input", "qv-toggle-input");
  stitchNormalize.type = "checkbox";
  stitchNormalizeRow.append(stitchNormalize,
                            make("span", "qv-toggle-label", words.stitchNormalize));
  stitchNormalizeRow.title = words.stitchNormalizeHint;
  stitchCard.append(stitchRow, stitchNormalizeRow, stitchNote);
  const stitchCardWrap = make("section", "qv-card");
  stitchCardWrap.append(make("h3", "qv-card-title", words.stitchTitle),
                        make("p", "qv-note", words.stitchPaneHint), stitchCard);
  stitchPane.append(stitchCardWrap);

  const upscaleInputField = field(words.references, "图片走图片放大，MP4 / MOV / WebM 走视频放大；视频会先上传到 ComfyUI 的输入目录。");
  const upscaleInput = make("input", "qv-file");
  upscaleInput.type = "file";
  upscaleInput.accept = "image/png,image/jpeg,image/webp,video/mp4,video/quicktime,video/webm";
  const upscaleName = make("span", "qv-file-name", "未选择");
  upscaleInput.addEventListener("change", () => {
    const file = upscaleInput.files?.[0] || null;
    state.upscaleReference = file;
    state.upscaleIsVideo = Boolean(file && file.type.startsWith("video/"));
    upscaleName.textContent = file ? file.name : "未选择";
    void loadUpscaleRecipes();
  });
  upscaleInputField.append(upscaleInput, upscaleName);
  upscaleCard.append(upscaleInputField);

  const upscaleActions = make("div", "qv-actions");
  const upscaleButton = button(words.upscaleGenerate, "qv-button qv-primary", () => submitUpscale());
  upscaleActions.append(upscaleButton);
  upscaleCard.append(upscaleActions);
  upscalePane.append(upscaleCard);

  // -- denoise sub-mode -------------------------------------------------------
  // Denoising is a module of its own rather than a checkbox on the upscale: it rebuilds
  // nothing, costs no VRAM, and the measured order is upscale first, denoise second (the
  // reverse costs +11% noise). So it takes its own clip, its own strength and its own
  // button, and the upscale pane never denoises on the side.
  const denoisePane = make("div", "qv-pane");
  denoisePane.hidden = true;
  const denoiseCard = make("section", "qv-card");
  denoiseCard.append(make("h3", "qv-card-title", words.denoiseTitle),
                     make("p", "qv-note", words.denoisePaneHint));

  const denoiseRow = make("label", "qv-shot-param qv-denoise-strength");
  denoiseRow.append(make("span", "qv-label", words.denoiseStrength));
  const denoiseInput = make("input", "qv-shot-param-input");
  denoiseInput.type = "number";
  denoiseInput.min = "1";
  denoiseInput.max = "3.5";
  denoiseInput.step = "0.1";
  denoiseInput.value = String(state.denoiseStrength);
  denoiseInput.addEventListener("input", () => {
    const value = Number(denoiseInput.value);
    if (Number.isFinite(value)) state.denoiseStrength = Math.min(3.5, Math.max(1, value));
  });
  denoiseRow.append(denoiseInput);
  denoiseRow.title = words.denoiseStrengthHint;
  denoiseCard.append(denoiseRow);

  const denoiseNote = make("p", "qv-note", words.denoiseNote);
  const denoiseInputField = field(words.denoiseSource,
                                  "MP4 / MOV / WebM；片段会先上传到 ComfyUI 的输入目录，再交给 ffmpeg。");
  const denoiseFile = make("input", "qv-file");
  denoiseFile.type = "file";
  denoiseFile.accept = "video/mp4,video/quicktime,video/webm";
  const denoiseFileName = make("span", "qv-file-name", "未选择");
  denoiseFile.addEventListener("change", () => {
    const file = denoiseFile.files?.[0] || null;
    denoiseFileName.textContent = file ? file.name : "未选择";
    denoiseNote.textContent = words.denoiseNote;
  });
  denoiseInputField.append(denoiseFile, denoiseFileName);
  denoiseCard.append(denoiseInputField);

  const denoiseActions = make("div", "qv-actions");
  denoiseActions.append(button(words.denoiseRun, "qv-button qv-primary",
                               () => submitDenoiseModule()));
  denoiseCard.append(denoiseActions, denoiseNote);
  denoisePane.append(denoiseCard);

  left.append(videoPane, upscalePane, denoisePane, stitchPane);

  // -- result -----------------------------------------------------------------
  const resultCard = make("section", "qv-card");
  resultCard.append(make("h3", "qv-card-title", words.result));
  const video = make("video", "qv-video");
  video.controls = true;
  video.playsInline = true;
  const resultEmpty = make("p", "qv-note", words.noResult);
  // The director's raw report used to be printed under the film as a JSON blob. It is
  // diagnostics, not something to read, so it stays in the DOM (the job still carries
  // it) but never on screen.
  const reportText = make("pre", "qv-report", "");
  reportText.hidden = true;
  // RIFE doubling of the finished film: a separate job on the same file, so it has
  // its own button, progress line and result (published next to the original).
  const interpRow = make("div", "qv-actions");
  const interpButton = button(words.interpolate, "qv-button", () => submitInterpolate());
  // Denoise the finished film: ffmpeg on the CPU, a few seconds, and it reports the noise
  // and detail it measured before and after (it is the one finishing step that measurably
  // improves this machine's own output without a model).
  const denoiseButton = button(words.denoise, "qv-button", () => submitDenoise());
  interpRow.append(interpButton, denoiseButton);
  // The row alone carries the visibility: hiding the button as well would leave it
  // permanently invisible.
  interpRow.hidden = true;
  const interpNote = make("p", "qv-action-note", "");
  interpNote.hidden = true;
  resultCard.append(video, resultEmpty, interpRow, interpNote);

  // VRAM and queue live under the film, not in the progress rail: they describe the
  // machine the run is using, and the rail is about the run itself.
  const gpuCard = make("section", "qv-card qv-gpu-card");
  gpuCard.append(make("h3", "qv-card-title", words.vramCard));
  const gpuStats = tileGrid([["total", words.vramTotal],
                             ["used", words.vramUsed, words.vramUsedTip],
                             ["free", words.vramFree, words.vramFreeTip],
                             ["peak", words.vramPeak, words.vramPeakTip]]);
  const gpuFooter = make("p", "qv-card-hint", words.vramUnavailable);
  gpuCard.append(gpuStats.grid, gpuFooter);

  const historyCard = make("section", "qv-card");
  historyCard.append(make("h3", "qv-card-title", words.history));
  const historyList = make("ul", "qv-history");
  historyCard.append(historyList);
  right.append(resultCard, gpuCard, historyCard);

  root.append(body);
  document.body.append(root);

  // -- rendering --------------------------------------------------------------
  function setActionNote(text, tone) {
    actionText.textContent = text || "";
    actionNote.hidden = !text && playLink.hidden;
    actionNote.dataset.tone = tone || "";
  }

  function reportFailure(error) {
    const reason = (error && error.message) || String(error);
    console.error("[video-panel]", error);
    setActionNote(words.clickFailed(reason), "error");
  }

  function setSubmode(key) {
    state.submode = key;
    for (const [name, el] of Object.entries(submodeButtons)) {
      el.setAttribute("aria-pressed", String(name === key));
    }
    videoPane.hidden = key !== "generate";
    upscalePane.hidden = key !== "upscale";
    denoisePane.hidden = key !== "denoise";
    stitchPane.hidden = key !== "stitch";
    // The monitor is the shared rail, so it stays put for every sub-mode — the upscale,
    // denoise, stitch and interpolation jobs report there too.
    if (key === "upscale") void loadUpscaleRecipes();
  }

  function renderRecipes() {
    recipeRow.textContent = "";
    for (const entry of state.recipes) {
      const el = button(entry.label, "qv-recipe", () => selectRecipe(entry.key));
      el.dataset.recipe = entry.key;
      el.setAttribute("aria-pressed", String(entry.key === state.recipe));
      recipeRow.append(el);
    }
    const current = state.recipes.find(item => item.key === state.recipe);
    recipeNote.textContent = current ? `${current.summary}\n${current.detail}` : "";
    recipeNote.style.whiteSpace = "pre-line";
  }

  function selectRecipe(key) {
    state.recipe = key;
    applyRecipeParams();
    renderRecipes();
    updateEstimate();
  }

  // A recipe carries the settings it was tuned for — the step count above all, since a
  // 3-step distillation graph is useless at 20 — and the per-shot fields are what actually
  // reach the graph, so selecting one writes them into every shot. Called again whenever
  // the shot count changes, so a new shot is added at the recipe's step count too.
  function applyRecipeParams() {
    const entry = state.recipes.find(item => item.key === state.recipe);
    // The strength dial follows the recipe: shown only where the file wires a LoRA, and
    // reset to that file's value so switching routes never quietly carries a number over.
    state.loraStrength = entry?.lora ? Number(entry.lora.strength) : null;
    loraRow.hidden = !entry?.lora;
    if (entry?.lora) {
      loraInput.value = String(entry.lora.strength);
      loraRow.title = `${words.loraStrengthHint}（当前 ${entry.lora.name.split(/[\\/]/).pop()}）`;
    }
    const steps = Number(entry?.params?.steps);
    if (!Number.isFinite(steps) || steps < 1) return;
    let changed = false;
    for (let index = 1; index <= state.shotCount; index += 1) {
      if (shotSteps(index) !== Math.round(steps)) {
        shotSetSteps(index, steps);
        changed = true;
      }
    }
    if (changed) renderShots();
  }

  function renderUpscaleRecipes() {
    upscaleRow.textContent = "";
    for (const entry of state.upscaleRecipes) {
      const el = button(entry.label || entry.key, "qv-recipe", () => {
        state.upscaleRecipe = entry.key;
        renderUpscaleRecipes();
      });
      el.setAttribute("aria-pressed", String(entry.key === state.upscaleRecipe));
      upscaleRow.append(el);
    }
    // The dial follows the recipe the same way; the denoise recipe lives in its own pane.
    const current = state.upscaleRecipes.find(item => item.key === state.upscaleRecipe);
    // SeedVR2's step count follows the recipe the same way, and every upscale recipe has one.
    const steps = Number(current?.params?.steps);
    state.upscaleSteps = Number.isFinite(steps) ? steps : null;
    if (Number.isFinite(steps)) upscaleStepsInput.value = String(steps);
    // The redraw ratio follows the recipe too, defaulting to 1.0 (untouched SeedVR2 output).
    const strength = Number(current?.params?.upscale_strength);
    state.upscaleStrength = Number.isFinite(strength) ? strength : 1;
    strengthInput.value = String(state.upscaleStrength);
  }

  function formatDuration(seconds) {
    if (!Number.isFinite(seconds)) return "";
    if (seconds < 90) return `${Math.round(seconds)} 秒`;
    return `${Math.round(seconds / 60)} ${words.minutes}`;
  }

  function formatClock(seconds) {
    if (!Number.isFinite(seconds)) return "—";
    const total = Math.round(seconds);
    const minutes = Math.floor(total / 60);
    const rest = total % 60;
    return minutes ? `${minutes} 分 ${String(rest).padStart(2, "0")} 秒` : `${rest} 秒`;
  }

  const gigabyte = mib => `${(mib / 1024).toFixed(1)} GB`;

  // While nothing is running the rail still shows the plan: one row per shot with its
  // own settings and estimate, so the numbers are readable before the click as well.
  function renderPlannedShots() {
    const rows = Array.from({ length: state.shotCount }, (_, index) => index + 1);
    shotTable.replaceChildren(...rows.map(index => {
      const row = make("div", "qv-shot-row");
      row.dataset.state = "pending";
      row.append(make("span", "qv-shot-row-index", words.shotLabel(index)));
      row.append(make("span", "qv-shot-row-spec",
                      `${shotSeconds(index)}s · ${shotSteps(index)} 步 · 种子 ${shotSeed(index)}`));
      row.append(make("span", "qv-shot-row-time",
                      `预计 ${formatDuration(estimateShot(index, false))}`));
      row.append(make("span", "qv-shot-row-state", words.shotStates.pending));
      return row;
    }));
  }

  // One row per shot: what it was asked for, what state it is in, how long it took
  // (or is expected to take). A film is a chain of separate runs, so this is the
  // part of the process the progress bar cannot show.
  //
  // The elapsed value of a running shot is derived from the timestamp the gateway
  // sent, so it only ticks while that run is actually alive. A row left in
  // "running" by a cancelled or finished job used to keep counting for ever.
  function renderShotTable(job, live) {
    const shots = job.shots || [];
    if (!shots.length) {
      shotTable.replaceChildren();
      return;
    }
    const now = Date.now() / 1000;
    shotTable.replaceChildren(...shots.map(shot => {
      const row = make("div", "qv-shot-row");
      row.dataset.state = shot.state;
      row.append(make("span", "qv-shot-row-index", `第 ${shot.index} 段`));
      row.append(make("span", "qv-shot-row-spec",
                      `${shot.seconds}s · ${shot.steps} 步 · 种子 ${shot.seed}`));
      let timing;
      if (shot.state === "running") {
        const seconds = live && shot.started ? Math.max(0, now - shot.started)
          : (typeof shot.elapsed === "number" ? shot.elapsed : null);
        timing = seconds === null
          ? words.usedUnknown
          : `${words.used} ${formatClock(seconds)}${live ? ` / ${words.estimateShort} ${formatDuration(shot.estimate)}` : ""}`;
      } else if (typeof shot.elapsed === "number") {
        timing = `${words.used} ${formatClock(shot.elapsed)}`;
      } else {
        timing = `${words.estimateShort} ${formatDuration(shot.estimate)}`;
      }
      row.append(make("span", "qv-shot-row-time", timing));
      row.append(make("span", "qv-shot-row-state", words.shotStates[shot.state] || shot.state));
      return row;
    }));
  }

  // The flow block: one bar for the whole pipeline instead of a scrolling log. The
  // preparation stages are short, the shot chain is the bulk of it, and the last
  // stage covers assembling the master. 提示词优化 only exists when the run has one:
  // a film without it must not show a step that never happens, and a running film
  // keeps showing it even if the switch is flipped while it renders.
  const FLOW_WEIGHTS = { accepted: 0.03, enhancing: 0.03, starting_comfy: 0.02,
                         building_graph: 0.03, submitting: 0.02, generating: 0.82, done: 0.05 };
  const FLOW_BASE = ["accepted", "starting_comfy", "building_graph", "submitting",
                     "generating", "done"];
  let renderedFlow = "";
  function flowStages(job) {
    const enhancing = job ? Boolean(job.settings?.enhance_prompt) : state.enhance;
    return enhancing ? ["accepted", "enhancing", ...FLOW_BASE.slice(1)] : FLOW_BASE.slice();
  }
  function renderFlow(job, live) {
    const stages = flowStages(job);
    // "enhanced" is the same step, already finished.
    let stage = job ? (job.phase || job.state) : "accepted";
    if (stage === "enhanced") stage = "enhancing";
    const shots = job?.shots || [];
    const total = shots.length || 0;
    const finished = shots.filter(shot => shot.state === "done").length;
    const running = shots.find(shot => shot.state === "running");
    if (job && job.state === "done") stage = "done";
    const index = Math.max(0, stages.indexOf(stage));
    // How far the *stage* is: the shot chain fills by whole shots plus the running
    // shot's own progress, everything else is done-or-not.
    const shotProgress = total
      ? Math.min(1, (finished + (running ? (job.progress || 0) : 0)) / total) : 0;
    let overall = 0;
    stages.forEach((key, position) => {
      if (!job) return;
      if (position < index) overall += FLOW_WEIGHTS[key];
      else if (position === index) {
        overall += FLOW_WEIGHTS[key] * (key === "generating" ? shotProgress
          : (stage === "done" ? 1 : live ? 0.5 : 0));
      }
    });
    if (job && job.state === "done") overall = 1;
    const percent = Math.round(overall * 100);
    const key = `${stages.join()}|${stage}|${percent}|${finished}|${total}|${live}`;
    if (key === renderedFlow) return;
    renderedFlow = key;
    flowPercent.textContent = `${percent}%`;
    flowTrack.replaceChildren(...stages.map((stageKey, position) => {
      const segment = make("span", "qv-flow-segment");
      segment.dataset.state = !job ? "pending"
        : position < index ? "done"
          : position === index ? (stageKey === "done" ? "done" : "active") : "pending";
      segment.style.flexGrow = String(FLOW_WEIGHTS[stageKey]);
      segment.title = words.flowStages[stageKey];
      return segment;
    }));
    flowSteps.replaceChildren(...stages.map((stageKey, position) => {
      const step = make("span", "qv-flow-step");
      step.dataset.state = !job ? "pending"
        : position < index ? "done"
          : position === index ? (stageKey === "done" ? "done" : "active") : "pending";
      const suffix = stageKey === "generating" && total ? ` ${finished}/${total} 段` : "";
      step.textContent = `${words.flowStages[stageKey]}${suffix}`;
      return step;
    }));
  }

  function phaseName(phase) {
    return words.phaseNames[phase] || phase || words.idle;
  }

  // -- shot plan ---------------------------------------------------------------
  function setShotCount(count) {
    state.shotCount = count;
    for (const [value, el] of Object.entries(shotButtons)) {
      el.setAttribute("aria-pressed", String(Number(value) === count));
    }
    ensureShotSeeds();
    // A newly added shot follows the selected recipe's step count instead of the default.
    applyRecipeParams();
    renderShots();
    renderPlannedShots();
    updateEstimate();
  }

  function ensureShotSeeds() {
    // A fresh seed per shot, never -1: the workflow file ships one fixed seed, so
    // a new panel that passed nothing would render the same film every time.
    for (let index = 1; index <= state.shotCount; index += 1) {
      if (!Number.isFinite(Number(state.shotSeeds[index]))) {
        state.shotSeeds[index] = randomSeed();
      }
    }
  }

  function renderShots() {
    const total = Array.from({ length: state.shotCount }, (_, i) => shotSeconds(i + 1))
      .reduce((sum, value) => sum + value, 0);
    shotNote.textContent = words.shotsNote(state.shotCount, total);
    const rows = [];
    shotCostNodes = {};
    for (let index = 1; index <= state.shotCount; index += 1) {
      const card = make("div", "qv-shot");

      const head = make("div", "qv-shot-head");
      head.append(make("span", "qv-shot-label", words.shotLabel(index)));
      const cost = make("span", "qv-shot-cost", "");
      cost.title = words.shotCostHint;
      shotCostNodes[index] = cost;
      head.append(cost);
      card.append(head);

      const prompt = make("textarea", "qv-textarea qv-shot-prompt");
      prompt.rows = 2;
      prompt.placeholder = index === 1
        ? "第一段的提示词（也可只填下方的整片描述）"
        : words.shotPromptPlaceholder;
      prompt.value = state.shotPrompts[index] || "";
      prompt.addEventListener("input", () => {
        state.shotPrompts[index] = prompt.value;
      });
      card.append(prompt);

      // Every shot is rendered by its own run of the director, so duration, step
      // count and seed are all set here rather than once for the film. 13.75 s is
      // the longest shot the H3 frame grid accepts once a continuation is needed.
      const grid = make("div", "qv-shot-params");
      grid.append(
        shotNumberField(index, "seconds", { min: 5, max: 13.75, step: 0.25 }),
        shotNumberField(index, "steps", { min: 1, max: 50, step: 1 }),
        shotSeedField(index),
        shotContinuityField(index),
      );
      card.append(grid);

      const refRow = make("div", "qv-shot-refs");
      const fileInput = make("input", "qv-file");
      fileInput.type = "file";
      fileInput.multiple = true;
      fileInput.accept = "image/png,image/jpeg,image/webp";
      const label = make("span", "qv-file-name", "未选择");
      // The director takes up to five references per shot, so the picker allows a
      // batch and keeps only that many.
      const describe = files => (files.length
        ? files.map(file => file.name).join("、")
        : "未选择");
      fileInput.addEventListener("change", () => {
        const files = Array.from(fileInput.files || []).slice(0, 5);
        if (files.length) state.shotRefs[index] = files;
        else delete state.shotRefs[index];
        label.textContent = describe(files);
      });
      const dropRefs = button(words.clearRefs, "qv-link-button", () => {
        // A reference picked in an earlier round would otherwise be uploaded again
        // and used, because a file input keeps its selection.
        delete state.shotRefs[index];
        fileInput.value = "";
        label.textContent = describe([]);
      });
      if (state.shotRefs[index]) label.textContent = describe(state.shotRefs[index]);
      refRow.append(make("span", "qv-label", words.shotRef), fileInput, label, dropRefs);
      card.append(refRow);

      // Shot 1 is the only one without a shot before it. A clip picked here becomes
      // that missing predecessor: the same 22-frame motion context and last-frame
      // reference shot 2 gets from shot 1.
      if (index === 1) {
        const contextRow = make("div", "qv-shot-refs qv-shot-context");
        const contextInput = make("input", "qv-file");
        contextInput.type = "file";
        contextInput.accept = "video/mp4,video/quicktime,video/webm";
        const contextName = make("span", "qv-file-name",
                                 state.firstShotContext ? state.firstShotContext.name : words.contextNone);
        contextInput.addEventListener("change", () => {
          const file = contextInput.files?.[0] || null;
          state.firstShotContext = file;
          contextName.textContent = file ? file.name : words.contextNone;
        });
        const dropContext = button(words.clearRefs, "qv-link-button", () => {
          state.firstShotContext = null;
          contextInput.value = "";
          contextName.textContent = words.contextNone;
        });
        contextRow.append(make("span", "qv-label", words.shotContext), contextInput, contextName, dropContext);
        card.append(contextRow);
        card.append(make("p", "qv-note", words.shotContextHint));
      }
      refreshShotCost(index);
      rows.push(card);
    }
    shotList.replaceChildren(...rows);
  }

  function refreshShotCost(index) {
    const node = shotCostNodes[index];
    if (node) node.textContent = `${words.shotCost} ≈ ${(estimateShot(index, false) / 60).toFixed(1)} ${words.minutes}`;
    // The rail's plan rows carry the same estimate, so they follow the settings too.
    if (!state.busy) renderPlannedShots();
  }

  function shotNumberField(index, name, bounds) {
    const wrap = make("label", "qv-shot-param");
    wrap.append(make("span", "qv-label", words.paramNames[name]));
    const input = make("input", "qv-shot-param-input");
    input.type = "number";
    input.min = String(bounds.min);
    input.max = String(bounds.max);
    input.step = String(bounds.step);
    input.value = String(name === "seconds" ? shotSeconds(index) : shotSteps(index));
    input.addEventListener("input", () => {
      const value = Number(input.value);
      if (!Number.isFinite(value)) return;
      const clamped = Math.min(bounds.max, Math.max(bounds.min, value));
      if (name === "seconds") state.shotSeconds[index] = clamped;
      else state.shotSteps[index] = Math.round(clamped);
      // Only the cost line and the film estimate change, so the field keeps focus.
      refreshShotCost(index);
      updateEstimate();
    });
    wrap.append(input);
    return wrap;
  }

  function shotContinuityField(index) {
    const wrap = make("label", "qv-shot-param");
    wrap.append(make("span", "qv-label", words.shotContinuity));
    const row = make("div", "qv-shot-continuity");
    const options = [["continue", words.shotContinue], ["cut", words.shotCut]];
    const buttons = {};
    for (const [value, label] of options) {
      const el = button(label, "qv-link-button", () => {
        state.shotContinuity[index] = value;
        for (const [key, node] of Object.entries(buttons)) {
          node.setAttribute("aria-pressed", String(key === value));
        }
      });
      el.dataset.continuity = value;
      el.title = words.shotContinuityHint;
      el.setAttribute("aria-pressed", String(shotContinuity(index) === value));
      buttons[value] = el;
      row.append(el);
    }
    wrap.append(row);
    return wrap;
  }

  function shotSeedField(index) {
    const wrap = make("label", "qv-shot-param");
    wrap.append(make("span", "qv-label", words.paramNames.seed));
    const row = make("div", "qv-seed-row");
    const input = make("input", "qv-shot-param-input");
    input.type = "number";
    input.min = "0";
    input.max = String(2 ** 31 - 1);
    input.step = "1";
    input.value = String(shotSeed(index));
    input.addEventListener("input", () => {
      const value = Number(input.value);
      if (Number.isFinite(value) && value >= 0) state.shotSeeds[index] = Math.round(value);
    });
    const dice = button(words.randomSeed, "qv-seed-random", () => {
      const value = randomSeed();
      state.shotSeeds[index] = value;
      input.value = String(value);
    });
    dice.title = words.randomSeedHint;
    row.append(input, dice);
    wrap.append(row);
    return wrap;
  }

  // -- parameters --------------------------------------------------------------
  // Only the frame size is shared: the director concatenates the clips, so every
  // shot has to be rendered at the same width and height. Everything that can
  // differ per shot lives in the shot cards above.
  function renderGlobalParams() {
    if (!globalParamInputs.width) {
      const wants = {
        // 步长 32：H3 节点硬性要求宽高能被 32 整除，否则直接 raise ValueError。
        width: { min: 256, max: 2048, step: 32 },
        height: { min: 256, max: 2048, step: 32 },
      };
      for (const [name, bounds] of Object.entries(wants)) {
        const wrap = make("label", "qv-param");
        wrap.append(make("span", "qv-label", words.paramNames[name]));
        const input = make("input", "qv-file");
        input.type = "number";
        input.min = String(bounds.min);
        input.max = String(bounds.max);
        input.step = String(bounds.step);
        input.addEventListener("input", () => {
          const typed = Math.round(Number(input.value));
          if (!Number.isFinite(typed)) return;
          // 手打 682 这类数字会被 H3 拒绝，所以在输入处就对齐到 32 的倍数并回写，
          // 让"预计时间"和最终渲染用的是同一个数字。
          const value = Math.max(bounds.min, Math.min(bounds.max, Math.round(typed / 32) * 32));
          if (value !== typed) input.value = String(value);
          state.params[name] = value;
          updateEstimate();
        });
        wrap.append(input);
        globalParamInputs[name] = input;
        paramGrid.append(wrap);
      }
    }
    for (const name of Object.keys(globalParamInputs)) {
      globalParamInputs[name].value = String(state.params[name]);
    }
  }

  function resetParameters() {
    state.params = { width: RECOMMENDED.width, height: RECOMMENDED.height };
    state.shotSeconds = {};
    state.shotSteps = {};
    state.shotSeeds = {};
    ensureShotSeeds();
    renderGlobalParams();
    renderShots();
    updateEstimate();
  }

  // -- save location -----------------------------------------------------------
  function setOutputRoot(path) {
    if (!path) return;
    outputInput.value = path;
    state.outputChosen = path !== state.outputDefault;
    refreshOutputPreview();
  }

  async function loadOutputRoot() {
    try {
      const payload = await api("/local/video/output");
      state.outputDefault = payload.default || "";
      // The gateway remembers the last location, so reopening the panel shows the
      // folder the previous film went to instead of an empty box.
      if (!outputInput.value) setOutputRoot(payload.current || payload.default);
      else refreshOutputPreview();
    } catch (failure) {
      outputInput.placeholder = "读取默认位置失败，可直接填写绝对路径";
    }
  }

  function toggleBrowser(show) {
    const next = show === undefined ? browser.hidden : Boolean(show);
    browser.hidden = !next;
    browseButton.setAttribute("aria-pressed", String(next));
    if (next) void browseTo(outputInput.value.trim() || state.outputDefault);
  }

  async function browseTo(path) {
    if (!path) return;
    browserPath.textContent = words.browseLoading;
    browserList.textContent = "";
    try {
      const payload = await api(`/local/video/folders?path=${encodeURIComponent(path)}`);
      state.browsePath = payload.path;
      state.browseParent = payload.parent;
      browserPath.textContent = payload.path;
      browserPath.title = payload.path;
      browserUp.disabled = !payload.parent;
      browserRoots.replaceChildren(...(payload.roots || []).map(root => {
        const el = button(root.name, "qv-browser-button", () => browseTo(root.path));
        el.setAttribute("aria-pressed", String(root.path === payload.path));
        return el;
      }));
      const entries = (payload.folders || []).map(entry => {
        const el = button(`📁 ${entry.name}`, "qv-browser-folder", () => browseTo(entry.path));
        el.title = entry.path;
        return el;
      });
      if (!entries.length) entries.push(make("span", "qv-browser-empty", words.browseEmpty));
      browserList.replaceChildren(...entries);
    } catch (failure) {
      browserPath.textContent = `打不开：${failure.message}`;
      browserList.textContent = "";
    }
  }

  function renderJob() {
    const job = state.job;
    const busy = Boolean(job && ["queued", "running"].includes(job.state));
    state.busy = busy;
    generateButton.disabled = busy;
    upscaleButton.disabled = busy;
    cancelButton.hidden = !busy;
    generateButton.textContent = busy ? words.generate + "…" : words.generate;

    if (!job) {
      monitorBadge.textContent = words.idle;
      monitorBadge.dataset.state = "idle";
      monitorHint.hidden = false;
      statusLine.textContent = "";
      progressFill.style.width = "0%";
      progressLabel.textContent = "0%";
      for (const value of Object.values(stats.tiles)) value.textContent = "—";
      monitorSettings.textContent = "";
      stepLine.textContent = "";
      timingLine.textContent = "";
      renderPlannedShots();
      renderFlow(null, false);
      updateEstimate();
      interpRow.hidden = !state.lastPath;
      // No job means nothing is running: the post-process button must not stay
      // disabled from a snapshot the gateway no longer knows about.
      interpButton.disabled = state.interpolating;
      generateButton.disabled = false;
      return;
    }
    monitorHint.hidden = true;
    monitorBadge.textContent = `${phaseName(job.phase)}${job.total > 1 && job.position
      ? ` · 第 ${job.position}/${job.total} 段` : ""}`;
    monitorBadge.dataset.state = job.error ? "error" : busy ? "running" : "done";
    // The notes carry the shot the chain is on, so a long film says which part of
    // it is running instead of only "生成中".
    const notes = job.notes || [];
    const latest = notes[notes.length - 1];
    const detail = [latest?.message, job.error].filter(Boolean).join(" · ");
    const line = `${phaseName(job.phase)}${detail ? " · " + detail : ""}`;
    const percent = Math.round((job.progress || 0) * 100);
    statusLine.textContent = line;
    actionTrack.hidden = !busy;
    actionFill.style.width = `${percent}%`;
    playLink.hidden = !job.video;
    setActionNote(`${line} · ${percent}% · ${words.elapsed} ${Math.round(job.elapsed || 0)} 秒`,
                  job.error ? "error" : "");
    progressFill.style.width = `${percent}%`;
    progressLabel.textContent = `${percent}%`;
    stats.tiles.elapsed.textContent = formatClock(job.elapsed);
    stats.tiles.eta.textContent = job.eta ? formatDuration(job.eta) : "—";
    // The optimiser's own report, as soon as the job has one: which model, how long, and the
    // two texts side by side.
    const enhancer = job.enhancer;
    promptBox.hidden = !enhancer;
    if (enhancer) {
      promptMeta.textContent = [enhancer.label, enhancer.seconds ? `${enhancer.seconds} s` : "",
                                enhancer.used ? "" : (enhancer.reason || words.enhanceSkipped)]
        .filter(Boolean).join(" · ");
      promptOriginal.textContent = enhancer.original || "—";
      promptRewritten.textContent = enhancer.rewritten || enhancer.original || "—";
    }
    renderShotTable(job, busy);
    renderFlow(job, busy);
    renderComfy();
    renderSpeed();
    const settings = job.settings || {};
    const size = settings.width && settings.height
      ? `${settings.width}×${settings.height}`
      : `${state.params.width}×${state.params.height}`;
    monitorSettings.textContent = [
      size,
      `工作流 ${state.recipes.find(entry => entry.key === job.recipe)?.label || job.recipe}`,
      settings.structure_guard ? words.structureGuard : null,
      settings.first_shot_context ? `${words.shotContextShort} ${settings.first_shot_context}` : null,
      settings.project_name ? `${words.project} ${settings.project_name}` : null,
      settings.output_root || null,
    ].filter(Boolean).join(" · ");
    updateEstimate();

    if (job.video) {
      // Once a film has been interpolated, the panel keeps showing that cut while the
      // same film is on screen — a later poll must not flip it back to the original.
      const cut = state.interpolated && state.interpolated.source === job.path
        ? state.interpolated.video : job.video;
      if (video.getAttribute("src") !== cut) video.setAttribute("src", cut);
      video.hidden = false;
      resultEmpty.hidden = true;
    } else {
      video.hidden = true;
      resultEmpty.hidden = false;
    }
    // The film's absolute path outlives the job snapshot, so the post-process can
    // still run after a gateway restart cleared the job.
    if (job.path) state.lastPath = job.path;
    interpRow.hidden = !state.lastPath;
    interpButton.disabled = busy || state.interpolating;
    interpButton.title = words.interpolateHint;
    if (job.report) {
      // Kept for diagnostics only: the raw director report is never shown.
      reportText.textContent = job.report;
    }
  }

  // -- ComfyUI telemetry --------------------------------------------------------
  // The node keeps publishing its last run's state after it finishes, so only a
  // snapshot whose prompt_id matches the job on screen is shown.
  function renderComfy() {
    const comfy = state.comfy || {};
    const monitor = comfy.monitor || null;
    const queue = comfy.queue || null;
    const job = state.job;
    const mine = Boolean(monitor && job && job.prompt_id && monitor.prompt_id === job.prompt_id);

    if (mine && monitor.preview) {
      if (previewImage.getAttribute("src") !== monitor.preview) previewImage.setAttribute("src", monitor.preview);
      previewBox.hidden = false;
    } else {
      previewBox.hidden = true;
      previewImage.removeAttribute("src");
    }

    if (mine) {
      const phase = words.comfyPhases[monitor.phase] || monitor.phase || "";
      const perStep = monitor.step > 0 && monitor.elapsed_seconds
        ? monitor.elapsed_seconds / monitor.step : null;
      stepLine.textContent = [
        words.stepOf(monitor.step || 0, monitor.steps || job.shots?.[0]?.steps || "?"),
        phase,
        perStep ? words.secondsPerStep(perStep) : null,
        monitor.eta_seconds ? words.segmentLeft(monitor.eta_seconds) : null,
        monitor.elapsed_seconds ? `本段已用 ${Math.round(monitor.elapsed_seconds)} 秒` : null,
      ].filter(Boolean).join(" · ");
      // The node publishes per-phase timings as each phase closes, so this fills in
      // while the shot runs and is complete once it finishes.
      const timings = monitor.phase_timings || {};
      timingLine.textContent = Object.entries(timings)
        .map(([name, seconds]) => `${words.comfyPhases[name] || name} ${Math.round(seconds)}s`)
        .join(" · ");
      stats.tiles.speed.textContent = perStep ? `${perStep.toFixed(0)} 秒/步` : "—";
      stats.tiles.sampling.textContent = timings.sampling ? formatDuration(timings.sampling) : "—";
    } else {
      stepLine.textContent = "";
      timingLine.textContent = "";
      stats.tiles.speed.textContent = "—";
      stats.tiles.sampling.textContent = "—";
    }

    const gpu = state.gpu;
    if (gpu) {
      gpuStats.tiles.total.textContent = gigabyte(gpu.total_mib);
      gpuStats.tiles.used.textContent = gigabyte(gpu.used_mib);
      gpuStats.tiles.free.textContent = gigabyte(gpu.free_mib);
      gpuStats.tiles.peak.textContent = state.gpuPeak ? gigabyte(state.gpuPeak) : "—";
      // The run's own footprint is reported next to the tiles rather than as a tile of
      // its own: the video chain unloads the text model first, so peak-minus-idle is
      // legitimately 0 GB and would look broken sitting in a big tile.
      const baseline = state.gpuBaseline;
      const delta = baseline != null
        ? `${words.vramDelta} ${gigabyte(Math.max(0, (state.gpuPeak || gpu.used_mib) - baseline))}` : null;
      gpuFooter.textContent = [
        `${words.vramUtil} ${Math.round(gpu.utilization)}%`,
        queue ? `${words.queue} ${queue.running}/${queue.pending}` : null,
        baseline != null ? `${words.vramBaseline} ${gigabyte(baseline)}` : null,
        delta,
      ].filter(Boolean).join(" · ");
      gpuFooter.title = words.vramDeltaTip;
    } else {
      for (const value of Object.values(gpuStats.tiles)) value.textContent = "—";
      gpuFooter.textContent = words.vramUnavailable;
    }
  }

  function renderSpeed(history) {
    // Per-shot wall clock from the job's own rows, plus the session's history.
    const shots = state.job?.shots || [];
    const done = shots.filter(shot => shot.elapsed);
    const entries = history || state.history || [];
    const finished = entries.filter(entry => entry.state === "done");
    const parts = [];
    if (done.length) parts.push(`${words.lastSpeed} ${Math.round(done[done.length - 1].elapsed)} 秒`);
    if (done.length > 1) {
      parts.push(`${words.averageSpeed} ${Math.round(done.reduce((sum, shot) => sum + shot.elapsed, 0) / done.length)} 秒`);
    }
    if (entries.length) {
      parts.push(`${words.successRate} ${Math.round(finished.length / entries.length * 100)}%（${finished.length}/${entries.length}）`);
    }
    speedLine.textContent = parts.join(" · ");
  }

  async function pollComfy() {
    try {
      state.comfy = await api("/local/video/comfy");
    } catch (failure) {
      state.comfy = null;
    }
    try {
      if (!state.gpu || Date.now() - (state.gpuAt || 0) > 5000) {
        const payload = await api("/local/image/gpu");
        state.gpu = payload.gpu || null;
        state.gpuAt = Date.now();
        if (state.gpu) {
          // Track the run's own footprint the way the generate view does: the idle
          // reading just before the run is the baseline, and the peak is the highest
          // point seen since it started. A reading only counts as "idle" when nothing
          // is rendering and the card is quiet — otherwise it is some other job's
          // footprint and would make this run look like it used nothing.
          const busyQueue = (state.comfy?.queue?.running || 0) > 0;
          const quiet = !busyQueue && (state.gpu.utilization ?? 100) < 15;
          if (state.busy) {
            if (state.gpuIdle != null) state.gpuBaseline = state.gpuIdle;
            state.gpuPeak = Math.max(state.gpuPeak || 0, state.gpu.used_mib);
          } else {
            state.gpuIdle = quiet ? state.gpu.used_mib : null;
            state.gpuBaseline = null;
            state.gpuPeak = 0;
          }
        }
      }
    } catch (failure) {
      state.gpu = null;
    }
    renderComfy();
  }

  // -- RIFE post-process --------------------------------------------------------
  function setInterpNote(text, tone) {
    interpNote.textContent = text || "";
    interpNote.hidden = !text;
    interpNote.dataset.tone = tone || "";
  }

  // ffmpeg denoise of the finished film: one request, one answer, and the answer carries
  // the noise and detail it measured on both sides.
  async function submitDenoise() {
    const source = state.job?.path || state.lastPath;
    if (!source) {
      setInterpNote("还没有可降噪的成片", "error");
      return;
    }
    denoiseButton.disabled = true;
    setInterpNote("正在降噪（CPU，几秒到一分钟）…");
    try {
      const payload = await api("/local/video/denoise", { method: "POST", body: { path: source } });
      state.interpolated = { source, video: payload.video };
      video.setAttribute("src", payload.video);
      const name = (payload.target || payload.video).split(/[\\/]/).pop();
      setInterpNote(`${payload.note || "降噪完成"} · 已切到 ${name}`);
    } catch (failure) {
      setInterpNote(`${words.failed}：${failure.message}`, "error");
    } finally {
      denoiseButton.disabled = false;
    }
  }

  async function submitInterpolate() {
    const source = state.job?.path || state.lastPath;
    if (!source) {
      setInterpNote("还没有可补帧的成片", "error");
      return;
    }
    state.interpolating = true;
    interpButton.disabled = true;
    setInterpNote("正在提交补帧任务…");
    try {
      const payload = await api("/local/video/interpolate", {
        method: "POST",
        body: { path: source, multiplier: 2 },
      });
      setInterpNote(`补帧排队中：${payload.job?.id || ""}`);
      pollInterpolate();
    } catch (failure) {
      state.interpolating = false;
      interpButton.disabled = false;
      setInterpNote(`${words.failed}：${failure.message}`, "error");
    }
  }

  async function pollInterpolate() {
    try {
      const payload = await api("/local/video/interpolate/progress");
      const job = payload.job;
      if (!job) {
        state.interpolating = false;
        interpButton.disabled = false;
        return;
      }
      const percent = Math.round((job.progress || 0) * 100);
      if (job.state === "done" && job.video) {
        // Show the interpolated cut straight away and remember it for this film.
        state.interpolated = { source: job.source, video: job.video };
        video.setAttribute("src", job.video);
        state.interpolating = false;
        interpButton.disabled = false;
        const name = (job.target || job.video).split(/[\\/]/).pop();
        setInterpNote(`补帧完成，已切到 ${name}（已用 ${Math.round(job.elapsed)} 秒）`);
        return;
      }
      if (["error", "cancelled"].includes(job.state)) {
        state.interpolating = false;
        interpButton.disabled = false;
        setInterpNote(`补帧失败：${job.error || job.state}`, "error");
        return;
      }
      setInterpNote(`补帧中 · ${job.phase || ""} · ${percent}% · 已用 ${Math.round(job.elapsed)} 秒`);
      setTimeout(pollInterpolate, 2500);
    } catch (failure) {
      state.interpolating = false;
      interpButton.disabled = false;
      setInterpNote(`补帧状态读取失败：${failure.message}`, "error");
    }
  }

  function randomSeed() {
    return Math.floor(Math.random() * 2 ** 31);
  }

  function shotSeconds(index) {
    const value = Number(state.shotSeconds[index]);
    return Number.isFinite(value) && value >= 5 ? value : RECOMMENDED.seconds;
  }

  function shotSteps(index) {
    const value = Number(state.shotSteps[index]);
    return Number.isFinite(value) && value >= 1 ? Math.round(value) : RECOMMENDED.steps;
  }

  function shotSetSteps(index, steps) {
    state.shotSteps[index] = Math.max(1, Math.round(steps));
  }

  // How a shot joins the one before it: "continue" pins the previous tail as this shot's
  // opening context, "cut" starts it fresh. Shot 1's answer only matters when a 前文视频
  // was supplied, which is the only context it could continue from.
  function shotContinuity(index) {
    return state.shotContinuity[index] === "cut" ? "cut" : "continue";
  }

  function shotSeed(index) {
    const value = Number(state.shotSeeds[index]);
    return Number.isFinite(value) && value >= 0 ? Math.round(value) : randomSeed();
  }

  // 每段的预计用时来自服务端的动态代价模型（`/local/video/recipes` 的 `cost_model`），
  // 它由本机四个实测锚点拟合而来，并且**时长、步数、尺寸一起参与**：
  //   采样 = 每步秒数 × 步数 × (时长/7.5) × 尺寸因子
  //   固定 = 固定开销 × (0.5 + 0.5 × 时长/7.5)        // 编解码与保存，随时长亚线性
  //   单段 = (采样 + 固定) × 续段系数 × 全局校准      // 续段带 22 帧运动上下文
  //   冷启 = 本次会话首次运行的第一段，另加（冷载 − 热载）
  // 尺寸因子 = (宽×高 / 768×512) ** 尺寸指数；指数在没有跨尺寸样本前是 1.0，拿到真实样本后由服务端标定，
  // 面板会按 `size_calibrated` 如实说明"这个尺寸还没跑过，是外推"。
  // 以前这里只在配方写死的 measured 上按时长缩放，尺寸改动完全不反映在预计时间上。
  function costModel() {
    return state.costModel || {
      fixed_seconds: 86.4, seconds_per_step: 38.5, base_width: 768, base_height: 512,
      base_seconds: 7.5, continuation_factor: 1.45, cold_load_seconds: 598,
      warm_load_seconds: 72, pixel_exponent: 1.0, size_calibrated: false, calibration: 1,
    };
  }

  function estimateShot(index, cold) {
    const model = costModel();
    const steps = Math.max(1, shotSteps(index));
    const duration = Math.max(0.5, shotSeconds(index)) / (model.base_seconds || 7.5);
    const pixels = Math.max(1, state.params.width * state.params.height);
    const sizeFactor = Math.pow(pixels / ((model.base_width || 768) * (model.base_height || 512)),
      model.pixel_exponent ?? 1);
    const sampling = (model.seconds_per_step || 0) * steps * duration * sizeFactor;
    const overhead = (model.fixed_seconds || 0) * (0.5 + 0.5 * duration);
    const continuation = index > 1 ? (model.continuation_factor || 1) : 1;
    const coldExtra = index === 1 && cold
      ? Math.max(0, (model.cold_load_seconds || 0) - (model.warm_load_seconds || 0)) : 0;
    return (sampling + overhead) * continuation * (model.calibration || 1) + coldExtra;
  }

  /** 尺寸对预计时间的影响，用一句话说清；样本不足时如实说这是外推。 */
  function sizeNote() {
    const model = costModel();
    const pixels = Math.max(1, state.params.width * state.params.height);
    const factor = Math.pow(pixels / ((model.base_width || 768) * (model.base_height || 512)),
      model.pixel_exponent ?? 1);
    const percent = Math.round((factor - 1) * 100);
    const sign = percent >= 0 ? "+" : "";
    return `${state.params.width}×${state.params.height} 相对基准 ${sign}${percent}%`
      + (model.size_calibrated ? "" : "（该尺寸还没跑过，为线性外推）");
  }

  function updateEstimate() {
    const tile = stats.tiles.estimate;
    if (!state.recipes.length) {
      tile.textContent = "—";
      return;
    }
    // The first run of a session pays the model load, so the panel states both
    // bounds instead of one number that would be wrong half the time. 提示词优化 is
    // added once per film (it rewrites the description before the storyboard is read):
    // the call itself is ~2 s warm, and the language model has to be brought back
    // first — measured 25.5 s on this machine ("Backend ready: mode=text … seconds=25.49").
    const shots = Array.from({ length: state.shotCount }, (_, i) => i + 1);
    const cold = shots.reduce((sum, i) => sum + estimateShot(i, true), 0);
    const warm = shots.reduce((sum, i) => sum + estimateShot(i, false), 0);
    const enhancing = state.enhance;
    // A finished run reports what its own enhancement actually cost, which beats the constant.
    const measured = Number(state.job?.enhance_seconds);
    const enhanceWarm = enhancing ? (Number.isFinite(measured) && measured > 0
      ? measured : state.timing.enhance) : 0;
    const enhanceCold = enhancing ? state.timing.enhanceCold : 0;
    const warmTotal = warm + enhanceWarm;
    const coldTotal = cold + enhanceWarm + enhanceCold;
    tile.textContent = `${Math.round(warmTotal / 60)}–${Math.round(coldTotal / 60)} ${words.minutes}`;
    tile.parentElement.title = enhancing
      ? `${words.estimateHint} 已含提示词优化（约 ${Math.round(enhanceWarm)} s，本次会话首次还要把语言模型装回来约 ${Math.round(enhanceCold)} s）。${sizeNote()}`
      : `${words.estimateHint} ${sizeNote()}`;
  }

  function renderHistory(entries) {
    historyList.textContent = "";
    for (const item of entries || []) {
      const li = make("li", "qv-history-item");
      li.append(make("span", "qv-history-recipe", item.recipe || ""));
      li.append(make("span", "qv-history-state", item.error || item.state || ""));
      if (item.video) {
        const link = make("a", "qv-link", "打开");
        link.href = item.video;
        link.target = "_blank";
        li.append(link);
      }
      historyList.append(li);
    }
  }

  // -- actions ----------------------------------------------------------------
  /* Which model will do the prompt work. Read from the gateway instead of hard-coded: the
   * selection can change while this page is open, and a fixed name here is how the panel came to
   * claim the 27B had optimised a prompt the 9B wrote. */
  async function loadEnhancerName() {
    try {
      const payload = await api("/local/image/progress");
      const name = payload && payload.model && payload.model.enhancer;
      if (!name) return;
      enhanceLabel.textContent = `用 ${name} 优化提示词`;
      enhanceRow.title = `${name}｜提示词优化跟随当前模型，需要时先把它加载起来`;
    } catch (failure) { /* the generic label stays, and the switch still works */ }
  }

  async function loadRecipes() {
    try {
      const payload = await api("/local/video/recipes");
      state.recipes = payload.recipes || [];
      // 动态代价模型的常量与标定状态：预计时间由它算，尺寸也参与。
      state.costModel = payload.cost_model || state.costModel;
      state.recipe = state.recipe || payload.current || state.recipes[0]?.key || null;
      // Seeds the step count and (for a distill route) the strength dial from the file the
      // recipe points at, so the rail shows what a run would actually use.
      applyRecipeParams();
      renderRecipes();
      updateEstimate();
      void loadEnhancerName();
    } catch (failure) {
      recipeNote.textContent = `读取工作流失败：${failure.message}`;
    }
  }

  async function loadUpscaleRecipes() {
    // Image and video upscaling are separate pipelines because their loaders and
    // temporal nodes differ, so the chosen file decides which recipes apply.
    const wanted = state.upscaleIsVideo ? "seedvr2_video" : "seedvr2";
    if (state.upscalePipeline === wanted && state.upscaleRecipes.length) return;
    state.upscalePipeline = wanted;
    try {
      const payload = await api("/local/image/workflows");
      const seen = new Map();
      for (const entry of payload.workflows || []) {
        // Only the upscalers: the denoise recipe shares this pipeline but belongs to the
        // denoise module, which runs it directly through the source route.
        if (entry.pipeline === wanted && entry.kind === "upscale") seen.set(entry.key, entry);
      }
      state.upscaleRecipes = [...seen.values()];
      state.upscaleRecipe = state.upscaleRecipes[0]?.key || null;
      renderUpscaleRecipes();
    } catch (failure) {
      state.upscaleRecipes = [];
      upscaleRow.textContent = `读取放大工作流失败：${failure.message}`;
    }
  }

  function readAsDataUrl(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error("读取图片失败"));
      reader.readAsDataURL(file);
    });
  }

  async function submitVideo() {
    const globalPrompt = promptInput.value.trim();
    const shots = [];
    for (let index = 1; index <= state.shotCount; index += 1) {
      shots.push((state.shotPrompts[index] || "").trim() || globalPrompt);
    }
    if (!shots.some(text => text)) {
      statusLine.textContent = words.pleaseDescribe;
      setActionNote(words.pleaseDescribe, "error");
      promptInput.focus();
      return;
    }
    // The click answers immediately: from here on the button is busy and the note
    // says what is happening, so a slow or failed request is never silent.
    generateButton.disabled = true;
    setActionNote(words.submitting);
    try {
      // Per-shot reference images are uploaded first so the graph can name them;
      // the same route the upscale pane uses handles the transfer.
      const shotReferences = [];
      for (let index = 1; index <= state.shotCount; index += 1) {
        const files = state.shotRefs[index] || [];
        if (!files.length) {
          shotReferences.push([]);
          continue;
        }
        setActionNote(`上传第 ${index} 段参考图（${files.length} 张）…`);
        const names = [];
        for (const file of files.slice(0, 5)) {
          const uploaded = await uploadFile(file);
          names.push(uploaded.name);
        }
        shotReferences.push(names);
      }

      setActionNote(words.submitting);
      // Shot 1's predecessor clip goes up before the request too: the gateway converts
      // it to the render's size and hands the director the converted file.
      let firstShotContext = null;
      if (state.firstShotContext) {
        setActionNote(`上传前文视频（${state.firstShotContext.name}）…`);
        firstShotContext = (await uploadFile(state.firstShotContext)).name;
      }
      const payload = await api("/local/video/generate", {
        method: "POST",
        body: {
          prompt: globalPrompt,
          recipe: state.recipe,
          shots,
          shot_references: shotReferences.some(list => list.length) ? shotReferences : null,
          project_name: projectInput.value.trim() || null,
          // Where the project folder is created; empty means the gateway default.
          output_root: outputInput.value.trim() || null,
          // Appends the structure constraint to every shot's prompt on the gateway.
          structure_guard: guardInput.checked,
          // Rewrite the film description with the currently selected model before the storyboard
          // is built. The gateway loads that model on demand and reports which one it used.
          enhance_prompt: enhanceInput.checked,
          // Only routes that ship a distillation LoRA accept a strength; the gateway
          // refuses one for a plain route rather than ignoring it.
          lora_strength: state.loraStrength,
          // What shot 1 continues from, when the caller supplied a clip.
          first_shot_context: firstShotContext,
          // One entry per shot: the director renders each shot in its own run, so
          // duration, step count and seed are read per shot rather than per film.
          shot_plan: shots.map((_, i) => ({
            seconds: shotSeconds(i + 1),
            steps: shotSteps(i + 1),
            seed: shotSeed(i + 1),
            // "cut" makes the director start this shot without the previous shot's tail.
            continuity: shotContinuity(i + 1),
          })),
          // Width and height are the two values the shots must share.
          width: state.params.width,
          height: state.params.height,
        },
      });
      state.job = payload.job;
      renderJob();
      schedulePoll(800);
    } catch (failure) {
      statusLine.textContent = `${words.failed}：${failure.message}`;
      setActionNote(`${words.failed}：${failure.message}`, "error");
      generateButton.disabled = false;
    }
  }

  async function uploadFile(file) {
    const query = encodeURIComponent(file.name);
    return api(`/local/video/upload?name=${query}`, { method: "POST", raw: file });
  }

  async function submitUpscale() {
    if (!state.upscaleReference) {
      statusLine.textContent = "请先选择要放大的图片或视频";
      return;
    }
    try {
      statusLine.textContent = "提交中…";
      const applied = await api("/local/image/workflows", { method: "POST", body: { key: state.upscaleRecipe } });
      // The recipe owns its multiplier: without passing it through, every "放大"
      // button ran at whatever the saved workflow file happened to hold.
      const recipe = state.upscaleRecipes.find(entry => entry.key === state.upscaleRecipe) || {};
      const params = { ...(recipe.params || {}), ...(applied?.params || {}) };
      const body = {
        prompt: "",
        pipeline: recipe.pipeline || applied?.pipeline || "seedvr2",
        enhance_prompt: false,
      };
      for (const name of ["scale", "steps", "cfg"]) {
        if (params[name] !== undefined && params[name] !== null) body[name] = Number(params[name]);
      }
      // SeedVR2's step count is adjustable: the rail's number wins over the recipe default,
      // so a run can trade noise against restored texture without editing the workflow.
      if (Number.isFinite(Number(state.upscaleSteps)) && Number(state.upscaleSteps) > 0) {
        body.steps = Number(state.upscaleSteps);
      }
      // Redraw ratio: below 1 the restored frames are mixed back with the plain resize.
      const strength = Number(state.upscaleStrength);
      if (Number.isFinite(strength) && strength < 1) {
        body.upscale_strength = strength;
      }
      if (state.upscaleIsVideo) {
        // The clip goes up as a raw body (no base64 round trip, no 20 MB image
        // limit). The submitted job reads it from the video source route.
        statusLine.textContent = "上传视频中…";
        const query = encodeURIComponent(state.upscaleReference.name);
        const uploaded = await api(`/local/video/upload?name=${query}`, {
          method: "POST",
          raw: state.upscaleReference,
        });
        await api("/local/video/source", { method: "POST", body: { path: uploaded.name } });
        statusLine.textContent = "视频放大已提交，处理时间随片长增加";
      } else {
        // Images take the reference-image route the SeedVR2 builder reads.
        body.image_1 = await readAsDataUrl(state.upscaleReference);
      }
      const payload = await api("/local/image/generate", { method: "POST", body });
      statusLine.textContent = `放大任务已提交：${payload.job?.id || ""}（${applied?.workflow?.label || ""}）`;
      pollImageJob();
    } catch (failure) {
      statusLine.textContent = `${words.failed}：${failure.message}`;
    }
  }

  // The denoise module: upload the clip, hand it to ffmpeg on the CPU, show the result.
  // No graph, no model, no VRAM — which is exactly why it is a module of its own.
  async function submitDenoiseModule() {
    const file = denoiseFile.files?.[0] || null;
    if (!file) {
      denoiseNote.textContent = "请先选择要降噪的视频";
      return;
    }
    const strength = Number(state.denoiseStrength) || 2.0;
    try {
      denoiseNote.textContent = `上传 ${file.name}…`;
      const uploaded = await uploadFile(file);
      denoiseNote.textContent = `降噪中（nlmeans ${strength.toFixed(1)}，CPU，几秒到一分钟）…`;
      const done = await api("/local/video/denoise-source", {
        method: "POST",
        body: { name: uploaded.name, strength },
      });
      denoiseNote.textContent = `${done.note || "降噪完成"} · 已切换到降噪版`;
      state.interpolated = { source: uploaded.name, video: done.video };
      video.setAttribute("src", done.video);
    } catch (failure) {
      denoiseNote.textContent = `${words.failed}：${failure.message}`;
    }
  }

  async function submitStitch() {
    // Slot number is playback order; empty slots are simply skipped, so two clips in 1 and
    // 2 behave exactly like the old two-slot version.
    const picks = [1, 2, 3, 4].filter(index => state.stitch[index])
      .map(index => [index, state.stitch[index]]);
    if (picks.length < 2) {
      stitchNote.textContent = "至少要选两段视频（放进 1、2 号格）";
      return;
    }
    try {
      const names = [];
      for (const [index, file] of picks) {
        stitchNote.textContent = `上传第 ${index} 段：${file.name}…`;
        names.push((await uploadFile(file)).name);
      }
      stitchNote.textContent = `拼接中（${picks.length} 段${stitchNormalize.checked ? "，对齐亮度" : ""}）…`;
      const done = await api("/local/video/concat", {
        method: "POST",
        body: { clips: names, normalize: stitchNormalize.checked },
      });
      const match = stitchNormalize.checked && done.brightness
        ? ` · 亮度修正 ${done.brightness.map(value => value.toFixed(3)).join(" / ")}`
        : "";
      stitchNote.textContent = `拼接完成：${picks.map(([index, file]) => `${index}=${file.name}`)
        .join(" + ")} → ${(done.target || "").split(/[\\/]/).pop()}（${done.seconds} 秒）${match}`;
      state.interpolated = { source: done.target, video: done.video };
      video.setAttribute("src", done.video);
    } catch (failure) {
      stitchNote.textContent = `${words.failed}：${failure.message}`;
    }
  }

  async function cancelJob() {
    try {
      await api("/local/video/cancel", { method: "POST", body: {} });
    } catch (failure) {
      statusLine.textContent = `取消失败：${failure.message}`;
    }
  }

  // -- polling ----------------------------------------------------------------
  /* Only one job poll may be in flight, and only one is scheduled.
   *
   * Measured before this: selecting the video tab cost 22 requests in 2.5 s — six
   * /local/video/comfy, six /local/video/history and three /local/video/progress — because
   * setActive() starts a poll and the toolbar re-renders on every status poll, and because
   * the idle branch of a poll refreshes the history it had already refreshed. The history
   * rail is a convenience: it is throttled here and re-read the moment a job finishes. */
  const HISTORY_MIN_GAP_MS = 8000;
  const HIDDEN_TICK_MS = 6000;
  // Polling kept up while the view was closed: measured 1,278 telemetry rounds in two hours
  // with nobody on the video page. Nothing can change on a screen that is not shown.
  const IDLE_UNATTENDED_MS = 30000;
  let pollRunning = false;
  let historyAt = 0;

  function schedulePoll(delay) {
    clearTimeout(state.poll);
    state.poll = null;
    const visible = () => document.visibilityState !== "hidden";
    const busy = () => state.active && (state.busy || (state.job && ["queued", "running"].includes(state.job.state)));
    function arm() {
      clearTimeout(state.poll);
      // A hidden tab showing nothing has no reason to run at full rate; a running job keeps
      // its cadence in the background, because that progress is what the owner returns to.
      //
      // A *visible* tab with this view not on screen used to keep the full cadence too, and
      // that is what the gateway log showed: 1,278 rounds of /h3-monitor/state + /queue
      // between 17:22 and 19:40 with nobody watching the video page. Nothing on screen can
      // change while the view is closed, so that case backs off to IDLE_UNATTENDED_MS.
      const attended = visible() && state.active;
      const wait = (attended || busy()) ? delay
        : (visible() ? Math.max(delay, IDLE_UNATTENDED_MS) : Math.max(delay, HIDDEN_TICK_MS));
      state.poll = setTimeout(fire, wait);
    }
    function fire() {
      // Checked when the timer fires, not only when it was booked: the tab may have gone to
      // the background in between. Waiting again here — rather than polling anyway — is what
      // keeps a hidden tab from waking up for a result it cannot show.
      if (!visible() && !state.active) { arm(); return; }
      if (!visible() && !busy()) { arm(); return; }
      void pollVideoJob();
    }
    arm();
  }

  async function pollVideoJob() {
    if (pollRunning) return;
    pollRunning = true;
    try {
      const payload = await api("/local/video/progress");
      state.job = payload.job;
      renderJob();
      const busy = payload.busy || (state.job && ["queued", "running"].includes(state.job.state));
      // The per-step telemetry and the VRAM readout ride along with the job poll.
      await pollComfy();
      if (busy) schedulePoll(2000);
      else {
        void refreshHistory();
        schedulePoll(5000);
      }
    } catch (failure) {
      statusLine.textContent = `状态读取失败：${failure.message}`;
      schedulePoll(5000);
    } finally {
      pollRunning = false;
    }
  }

  async function pollImageJob() {
    try {
      const payload = await api("/local/image/progress?since=0");
      const job = payload.job;
      if (job) {
        statusLine.textContent = `${job.pipeline || "seedvr2"} · ${job.phase || job.state}`;
        progressFill.style.width = `${Math.round((job.progress || 0) * 100)}%`;
        if (job.state === "done") {
          const images = job.result?.images || [];
          if (images[0]?.filename) {
            video.hidden = true;
            resultEmpty.hidden = false;
            resultEmpty.textContent = `放大完成：${images[0].filename}`;
          }
          return;
        }
        if (["error", "cancelled"].includes(job.state)) {
          statusLine.textContent = `${words.failed}：${job.error || job.state}`;
          return;
        }
      }
      setTimeout(pollImageJob, 2500);
    } catch (failure) {
      statusLine.textContent = `状态读取失败：${failure.message}`;
    }
  }

  async function refreshHistory(force = false) {
    if (!force && Date.now() - historyAt < HISTORY_MIN_GAP_MS) return;
    historyAt = Date.now();
    try {
      const payload = await api("/local/video/history");
      state.history = payload.history || [];
      renderHistory(state.history);
      renderSpeed(state.history);
    } catch (failure) {
      // History is a convenience; a failure here must not disturb the job view.
    }
  }

  /** 离开这个页面时，把"选好的素材"清掉。
   *
   * 所有者观察到：退出后第一段的参考图还在。文件输入框会保留选择，而参考图在下一轮会被再次上传
   * 并真的参与渲染（`state.shotRefs` 就是提交时读的那份），所以留着不是方便而是隐患：下一次生成
   * 会悄悄用上一轮的图。进度、结果和提示词不动——进度与结果在网关那边，提示词是用户正在写的东西。
   */
  function clearPickedFiles() {
    state.shotRefs = {};
    state.firstShotContext = null;
    renderShots();          // 卡片重建：文件框清空、标签回到"未选择"
    renderPlannedShots();
  }

  // -- mounting ---------------------------------------------------------------
  window.VideoPanel = {
    setActive(active, render) {
      state.active = Boolean(active);
      state.render = render || state.render;
      root.hidden = !state.active;
      // The chat screen is hidden by CSS only while this view is active, so
      // switching back to text/vision restores it untouched. Without this the
      // fixed overlay would also sit on top of the mode toolbar.
      document.documentElement.classList.toggle("local-video-mode", state.active);
      if (!state.active) {
        clearTimeout(state.poll);
        state.poll = null;
        clearPickedFiles();
        return;
      }
      // Recipes and the running job are loaded lazily so opening the view costs
      // nothing until it is actually shown.
      if (!state.recipes.length) void loadRecipes();
      if (!state.outputDefault) void loadOutputRoot();
      // Starting the poll is what re-reads the job, and it does so on its own cadence: the
      // busy path keeps 2 s, the idle path schedules the next read itself. Calling it here
      // is therefore enough to catch up after a gateway restart, and repeating this
      // function — which the toolbar does on every status poll — costs nothing because a
      // poll already in flight is not started twice.
      void pollVideoJob();
      void refreshHistory();
    },
    isBusy() {
      return Boolean(state.busy);
    },
  };

  setSubmode("generate");
  setShotCount(1);
  renderGlobalParams();
})();
