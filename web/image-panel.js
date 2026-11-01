/**
 * Generate view for the local Qwen gateway.
 *
 * Owns everything under the toolbar in generate mode: the prompt and parameter
 * form, live progress with sampler previews and timings, queue/model monitoring,
 * and the result history. mode-control.js delegates the mode panel to this
 * object (`window.ImagePanel.setActive`) and passes its status renderer in.
 */
(() => {
  "use strict";

  if (window.ImagePanel) return;

  const words = {
    viewTitle: "图像生成",
    viewSubtitle: "Flux.2 Klein · 本地 ComfyUI",
    model: "当前模型",
    pipeline: "生图模型 / 管线",
    pipelineSwitched: "已切换管线",
    pipelineBusy: "生图任务进行中，完成后才能切换管线",
    modelRole: "采样模型",
    encoderRole: "文本编码器",
    vaeRole: "VAE",
    enhancerRole: "提示词优化",
    backendRole: "推理后端",
    comfyOnline: "ComfyUI 已连接",
    comfyOffline: "ComfyUI 未启动",
    queue: "队列",
    queueIdle: "空闲",
    queueBusy: "运行 {running} · 排队 {pending}",
    monitoring: "监控",
    refreshMeta: "点击刷新模型与队列状态",
    lastSpeed: "上次速度",
    averageSpeed: "平均速度",
    averageTime: "平均用时",
    successRate: "成功率",
    running: "进行中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
    promptLabel: "提示词",
    promptPlaceholder: "描述你想生成的画面，越具体越好",
    negativePlaceholder: "负面提示词（可选，描述不希望出现的内容）",
    params: "生成参数",
    width: "宽度",
    height: "高度",
    steps: "步数",
    guidance: "提示词引导",
    strength: "参考强度",
    scale: "放大倍率",
    upscaleHint: "放大不需要提示词：在“参考图 1”里选一张图片，倍率用上面的“放大倍率”。",
    seed: "种子",
    random: "随机",
    randomTitle: "随机生成种子（种子框内 Ctrl+R 也可以）",
    randomDone: "已随机种子",
    resetParams: "恢复默认参数",
    resetDone: "已恢复默认参数",
    references: "参考图",
    referenceOne: "参考图 1",
    referenceTwo: "参考图 2",
    chooseFile: "选择文件",
    noFile: "未选择",
    clearReferences: "移除参考图",
    enhance: "用 Qwen 优化提示词",
    enhanceHint: "关闭后直接把提示词交给 Klein",
    generate: "生成图片",
    subGenerate: "生成图片",
    subUpscale: "超分",
    subDenoise: "降噪",
    subGenerateHint: "写提示词生成新图；也可以放一张参考图做图生图。",
    subUpscaleHint: "只放大补细节，不管颗粒：SeedVR2 是细节重建器，抹颗粒留给「降噪」那一档。",
    subDenoiseHint: "单独的工序，不放大、不重建、不占显存：ffmpeg 在 CPU 上跑非局部均值。",
    upscaleGenerate: "开始放大",
    denoiseTitle: "降噪（nlmeans · CPU 后处理）",
    denoiseHint: "把选中的图片交给 ffmpeg 跑一遍 nlmeans：实测噪声 −26%、细节 −5%。顺序不能反——"
      + "先「超分」补细节，再来这里降噪（反过来实测噪声反而 +11%）。",
    denoiseSource: "降噪素材",
    denoiseStrength: "降噪强度（nlmeans）",
    denoiseStrengthHint: "1.0 最轻、3.5 最重，默认 2.0（实测噪声 −26%、细节 −5%）；3.0 时 −34% / −10%。",
    denoiseRun: "开始降噪",
    denoising: "降噪中（CPU，几秒）…",
    denoiseNeedsImage: "请先选择要降噪的图片。",
    denoiseNote: "降噪只动颗粒、不重建画面，所以它既不会更清晰也不会更模糊；跑完的图片另存为 -denoised。",
    upscaleNeedsImage: "超分需要一张参考图：请先在“参考图 1”里选一张图片。",
    referenceNotImage: "参考图只支持 PNG / JPEG / WebP 图片（视频请用视频界面的“超分重建”）。",
    generating: "生成中",
    waiting: "已排队",
    cancel: "取消生成",
    cancelRequested: "正在取消…",
    progress: "生成进度",
    idle: "待机",
    idleHint: "填写提示词后点击“生成图片”，这里会显示实时进度、速度和预览。",
    phaseQueued: "已加入队列",
    phaseQueuedBehind: "已排队，等前一张收尾",
    phaseQueuedBehindHint: "前面的图画完就自动开工（大约 1 分钟），不用再点",
    phaseStartingComfy: "启动 ComfyUI",
    phaseEnhancing: "Qwen 优化提示词",
    phaseLoading: "装载 Klein 模型",
    phaseSampling: "采样中",
    phaseDecoding: "解码中",
    phaseRestoring: "图片已出 · 正在换回对话模型（约 40 秒）",
    phaseCompleted: "生成完成",
    phaseError: "生成失败",
    phaseCancelled: "已取消",
    stepOf: "第 {step} / {max} 步",
    stepPending: "等待采样",
    elapsed: "已用时间",
    eta: "预计剩余",
    speed: "速度",
    totalTime: "总用时",
    stepTime: "单步用时",
    samplingTime: "采样用时",
    promptTime: "提示词优化",
    loadTime: "模型装载",
    queueTime: "排队等待",
    restoreTime: "模型恢复",
    etaUncalibrated: "估算中",
    toastCompleted: "生成完成，已存入记录",
    toastFailed: "生成失败",
    toastCancelled: "已取消生成",
    retry: "重试",
    dismiss: "知道了",
    failedHint: "本次没有生成图片，失败原因已保存在生成记录里。",
    comfyDownTitle: "ComfyUI 未启动，无法生成",
    comfyDownBody: "图片由本机 ComfyUI 出图。请先启动 ComfyUI（127.0.0.1:8188），再点“重试”或刷新状态。",
    checkAgain: "重新检测",
    stalled: "任务长时间没有进度，已停止等待",
    vram: "显存",
    vramTotal: "总显存",
    vramUsed: "已用",
    vramJob: "生图占用",
    vramPeak: "本次峰值",
    vramFree: "剩余",
    vramUtil: "GPU 利用率",
    vramUnavailable: "读取不到显存信息（nvidia-smi 不可用）",
    workflowTitle: "运行的工作流",
    workflowHint: "Flux.2 Klein API 工作流；改动会保存到本机并用于之后每次生成",
    workflowSave: "保存工作流",
    workflowReset: "恢复默认",
    workflowSaved: "工作流已保存",
    workflowResetDone: "已恢复默认工作流",
    workflowLive: "本次运行",
    workflowNext: "下次运行",
    workflowSync: "同步到上方参数",
    workflowSynced: "已同步到生成参数",
    workflowRunningHint: "生成中显示本次实际执行的工作流",
    workflowFile: "保存位置",
    presetTitle: "LoRA 预设",
    presetHint: "加速 LoRA 会同时套用步数与采样器；画质 LoRA 叠加在第二个槽位。",
    presetSpeed: "加速",
    presetQuality: "画质",
    presetActive: "使用中",
    presetOff: "关闭全部",
    presetApplied: "已套用",
    presetCleared: "已关闭 LoRA 预设",
    presetMissing: "未下载",
    switchTitle: "一键工作流",
    switchHint: "点一下切换整套配方：管线 + LoRA + 步数 / CFG / 重绘强度。鼠标停在按钮上看它的效果说明。",
    switchApplied: "已切换工作流",
    switchFailed: "切换工作流失败",
    switchUnavailable: "模型文件未就绪",
    switchActive: "当前使用",
    presetMismatch: "⚠ 加速 LoRA 与当前步数/CFG 不匹配，画面会被破坏",
    presetRestore: "恢复推荐值",
    presetSpeedHint: "建议 {steps} 步 / CFG {cfg}",
    timings: "阶段耗时",
    queuePosition: "队列位置",
    previewLabel: "采样预览",
    previewHint: "由采样回调输出，只用于判断构图",
    noPrompt: "请输入提示词",
    historyTitle: "生成记录",
    historyEmpty: "还没有生成记录。每次生成（成功或失败）都会自动保存在这里。",
    historyClear: "清空记录",
    historyRemove: "删除",
    historyRestore: "恢复参数",
    historyReuse: "基于此生成",
    historyOpen: "打开原图",
    historyCompare: "对比",
    historyCount: "{count} 条",
    selected: "已选 {count} 张用于对比",
    compareHint: "点击“对比”选择两张记录",
    close: "关闭",
    restored: "已恢复该记录的参数与提示词",
    reused: "已套用该记录参数，提示词保持不变",
    clearConfirm: "清空全部生成记录？此操作不可撤销。",
    historyServer: "记录保存在本机 logs/image-history.json",
    enhancedPrompt: "优化后提示词",
    showPrompt: "查看优化后的提示词",
    noReference: "无参考图",
    seconds: "秒",
    perSecond: "步/秒",
    stepsUnit: "步",
  };

  const state = {
    active: false,
    statusRender: null,
    meta: { comfy: { reachable: false, running: 0, pending: 0 }, checkpoints: [], current: {} },
    job: null,
    busy: false,
    // The sampler-preview version this view already shows. It rides along with every poll so
    // the gateway can skip re-sending an unchanged ~79 KB picture (measured: 85,745 bytes a
    // poll, one to two polls a second, ~6-8 MB per generated image).
    previewVersion: 0,
    pollTimer: null,
    pollDelay: 900,
    events: 0,
    eventLog: [],
    history: [],
    historyLoaded: false,
    compare: [],
    lastResult: null,
    lastFailed: null,
    lastSubmission: null,
    submittedJob: null,
    announcedJob: null,
    gpu: null,
    gpuTicks: 0,
    workflow: null,
    workflows: [],
    submode: "generate",
    pipelineRestored: false,
    workflowListError: null,
    workflowKey: null,
    workflowInputs: {},
  };

  const root = document.createElement("div");
  root.className = "qg-view";
  root.hidden = true;
  const defaults = { width: 1024, height: 1024, steps: 4, cfg: 1, denoise: 1, seed: -1 };

  function node(tag, className, text) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined && text !== null) result.textContent = text;
    return result;
  }

  function button(label, className, handler) {
    const result = node("button", className || "qg-button", label);
    result.type = "button";
    result.addEventListener("click", handler);
    return result;
  }

  function field(name, label, value, extra = {}) {
    const wrapper = node("label", "qg-field");
    wrapper.append(node("span", "qg-field-label", label));
    const input = node("input", "qg-field-input");
    input.name = name;
    input.type = "number";
    input.value = value;
    Object.assign(input, extra);
    wrapper.append(input);
    return { wrapper, input };
  }

  // -- static structure ------------------------------------------------------
  const layout = node("div", "qg-layout");
  const intro = node("header", "qg-intro");
  const introText = node("div", "qg-intro-text");
  introText.append(node("h2", "qg-title", words.viewTitle), node("p", "qg-subtitle", words.viewSubtitle));
  const introBadges = node("div", "qg-badges");
  intro.append(introText, introBadges);

  const grid = node("div", "qg-grid");
  // Three columns: the run's progress on the left, the work in the middle, the
  // model and monitoring on the right.
  const progressColumn = node("div", "qg-column qg-column-progress");
  const mainColumn = node("div", "qg-column qg-column-main");
  const sideColumn = node("div", "qg-column qg-column-side");

  // Model card
  const modelCard = node("section", "qg-card qg-model");
  const modelHead = node("div", "qg-card-head");
  const modelHeadText = node("div", "qg-card-head-text");
  modelHeadText.append(node("h3", "qg-card-title", words.model), node("p", "qg-card-hint", words.monitoring));
  const comfyBadge = node("span", "qg-status-badge");
  modelHead.append(modelHeadText, comfyBadge);
  const modelList = node("dl", "qg-model-list");
  const modelValues = {};
  for (const [key, role] of [["unet", words.modelRole], ["text_encoder", words.encoderRole], ["vae", words.vaeRole], ["enhancer", words.enhancerRole], ["backend", words.backendRole]]) {
    const term = node("dt", "qg-model-role", role);
    const value = node("dd", "qg-model-value", "—");
    modelValues[key] = value;
    modelList.append(term, value);
  }
  // Pipeline picker: which architecture the next generation uses.
  const pipelineRow = node("div", "qg-pipeline");
  const pipelineLabel = node("span", "qg-field-label", words.pipeline);
  const pipelineSelect = node("select", "qg-pipeline-select");
  pipelineSelect.setAttribute("aria-label", words.pipeline);
  const pipelineNote = node("p", "qg-pipeline-note", "");
  pipelineRow.append(pipelineLabel, pipelineSelect);
  pipelineSelect.addEventListener("change", () => selectPipeline(pipelineSelect.value));
  modelCard.append(modelHead, pipelineRow, pipelineNote, modelList);

  // Metrics
  const metrics = node("section", "qg-metrics");
  const metricTiles = {};
  for (const [key, label] of [["queue", words.queue], ["vram", words.vram], ["lastSpeed", words.lastSpeed], ["averageTime", words.averageTime], ["successRate", words.successRate]]) {
    const tile = node("div", "qg-metric");
    tile.dataset.metric = key;
    const value = node("span", "qg-metric-value", "—");
    metricTiles[key] = value;
    tile.append(node("span", "qg-metric-label", label), value);
    metrics.append(tile);
  }
  // Second line of the VRAM tile: what this generation added on top of the
  // pre-job baseline, which is the number a 12 GB card actually cares about.
  const gpuDelta = node("span", "qg-metric-delta", "—");
  metricTiles.vram.closest(".qg-metric").append(gpuDelta);
  // Monitoring stays honest when ComfyUI is started after the page: these
  // controls re-read the live metadata instead of waiting for the next poll.
  modelCard.addEventListener("click", () => void loadMeta());
  modelCard.title = words.refreshMeta;
  metricTiles.queue.closest(".qg-metric").addEventListener("click", () => void loadMeta());

  // Progress
  const progressCard = node("section", "qg-card qg-progress");
  const progressHead = node("div", "qg-card-head");
  const progressHeadText = node("div", "qg-card-head-text");
  const progressTitle = node("h3", "qg-card-title", words.progress);
  const progressPhase = node("p", "qg-card-hint", words.idle);
  progressHeadText.append(progressTitle, progressPhase);
  const cancelButton = button(words.cancel, "qg-button qg-button-danger", () => cancelJob());
  cancelButton.hidden = true;
  progressHead.append(progressHeadText, cancelButton);
  const track = node("div", "qg-track");
  const trackFill = node("div", "qg-track-fill");
  track.append(trackFill);
  const progressMeta = node("div", "qg-progress-meta");
  const stepLabel = node("span", "qg-progress-step", words.idleHint);
  const percentLabel = node("span", "qg-progress-percent", "0%");
  progressMeta.append(stepLabel, percentLabel);
  const statRow = node("div", "qg-stats");
  const statValues = {};
  for (const [key, label] of [["elapsed", words.elapsed], ["eta", words.eta], ["speed", words.speed], ["sampling", words.samplingTime]]) {
    const tile = node("div", "qg-stat");
    const value = node("strong", "qg-stat-value", "—");
    statValues[key] = value;
    tile.append(node("span", "qg-stat-label", label), value);
    statRow.append(tile);
  }
  const gpuRow = node("div", "qg-gpu");
  const gpuValues = {};
  for (const [key, label] of [["total", words.vramTotal], ["used", words.vramUsed], ["job", words.vramJob], ["peak", words.vramPeak]]) {
    const tile = node("div", "qg-gpu-item");
    const value = node("strong", "qg-gpu-value", "—");
    gpuValues[key] = value;
    tile.append(node("span", "qg-gpu-label", label), value);
    gpuRow.append(tile);
  }
  const gpuBar = node("div", "qg-gpu-bar");
  const gpuBarFill = node("span", "qg-gpu-bar-fill");
  const gpuBarJob = node("span", "qg-gpu-bar-job");
  gpuBar.append(gpuBarFill, gpuBarJob);
  const gpuHint = node("p", "qg-gpu-hint", "");
  gpuRow.hidden = true;
  const previewRow = node("div", "qg-preview");
  const previewImage = node("img", "qg-preview-image");
  previewImage.alt = words.previewLabel;
  const previewBox = node("figure", "qg-preview-box");
  const previewCaption = node("figcaption", "qg-preview-caption", words.previewLabel);
  previewBox.append(previewImage, previewCaption);
  const previewHint = node("p", "qg-preview-hint", words.previewHint);
  previewRow.append(previewBox, previewHint);
  previewRow.hidden = true;
  const timeline = node("div", "qg-timeline");  // Failure banner: states the outcome, the reason and the two useful actions.
  const failure = node("div", "qg-failure");
  failure.hidden = true;
  const failureText = node("div", "qg-failure-text");
  const failureActions = node("div", "qg-failure-actions");
  const retryButton = button(words.retry, "qg-button qg-button-small qg-button-primary", () => retryJob());
  const dismissButton = button(words.dismiss, "qg-button qg-button-small", () => dismissFailure());
  failureActions.append(retryButton, dismissButton);
  failure.append(failureText, failureActions);
  progressCard.append(progressHead, track, progressMeta, statRow, gpuBar, gpuRow, gpuHint, previewRow, timeline, failure);

  // Composer
  const composerCard = node("section", "qg-card qg-composer");
  const promptField = node("label", "qg-prompt");
  promptField.append(node("span", "qg-field-label", words.promptLabel));
  const promptInput = node("textarea", "qg-prompt-input");
  promptInput.rows = 3;
  promptInput.placeholder = words.promptPlaceholder;
  promptField.append(promptInput);
  const negativeInput = node("input", "qg-negative-input");
  negativeInput.type = "text";
  negativeInput.placeholder = words.negativePlaceholder;
  const paramsHead = node("div", "qg-params-head");
  paramsHead.append(node("span", "qg-field-label", words.params));
  const resetButton = button(words.resetParams, "qg-link-button", () => resetParams());
  paramsHead.append(resetButton);
  const paramsRow = node("div", "qg-params");
  const seedRandom = button(words.random, "qg-button qg-button-seed", () => rollSeed());
  seedRandom.title = words.randomTitle;
  // 参考强度：只有真的上传了参考图才有意义，没图时禁用（见 refreshReferences）。
  const denoiseField = field("denoise", words.strength, 1, { min: 0, max: 1, step: 0.05 });
  denoiseField.input.title = words.strengthHint || "上传参考图后可调；1.0 = 完全重画";
  const fields = [
    field("width", words.width, 1024, { min: 256, max: 4096, step: 64 }),
    field("height", words.height, 1024, { min: 256, max: 4096, step: 64 }),
    field("steps", words.steps, 4, { min: 1, max: 200, step: 1 }),
    field("cfg", words.guidance, 1, { min: 0, max: 20, step: 0.1 }),
    denoiseField,
    field("scale", words.scale, 1.5, { min: 1, max: 4, step: 0.05 }),
  ];
  const seedField = field("seed", words.seed, -1, { min: -1, max: 2147483647, step: 1 });
  // Wide enough to read ten digits next to the 随机 button (see .qg-field-seed).
  seedField.wrapper.classList.add("qg-field-seed");
  const seedGroup = node("span", "qg-input-group");
  seedGroup.append(seedField.input, seedRandom);
  seedField.wrapper.append(seedGroup);
  for (const entry of [...fields, seedField]) paramsRow.append(entry.wrapper);
  const upscaleHintNode = node("p", "qg-upscale-hint", words.upscaleHint);
  upscaleHintNode.hidden = true;
  const referenceRow = node("div", "qg-references");
  referenceRow.append(node("span", "qg-field-label", words.references));
  const referenceInputs = [];
  const referenceStates = [];
  for (let index = 0; index < 2; index += 1) {
    const wrapper = node("label", "qg-reference");
    const input = node("input", "qg-reference-input");
    // The generate view takes pictures only; a clip is refused before anything
    // reads it (the video view has its own 超分优化 entry for those).
    input.addEventListener("change", event => {
      const file = input.files && input.files[0];
      if (!file || file.type.startsWith("image/")) return;
      event.stopImmediatePropagation();
      input.value = "";
      refreshReferences();
      setStatus(words.referenceNotImage);
      showToast(words.referenceNotImage, "error");
    }, { capture: true });
    input.type = "file";
    input.accept = "image/png,image/jpeg,image/webp";
    const caption = node("span", "qg-reference-name", index ? words.referenceTwo : words.referenceOne);
    const clearOne = button("×", "qg-reference-clear", event => {
      event.preventDefault();
      input.value = "";
      referenceStates[index] = null;
      referenceRow.dispatchEvent(new Event("change"));
      refreshReferences();
    });
    clearOne.hidden = true;
    input.addEventListener("change", async () => {
      referenceStates[index] = input.files[0] ? await readImage(input.files[0]).catch(() => null) : null;
      refreshReferences();
    });
    wrapper.append(input, caption, clearOne);
    referenceInputs.push(input);
    referenceRow.append(wrapper);
  }
  const enhanceRow = node("label", "qg-enhance");
  const enhanceToggle = node("input", "qg-enhance-input");
  enhanceToggle.type = "checkbox";
  enhanceToggle.checked = true;
  const enhanceLabel = node("span", "", words.enhance);
  const enhanceHintNode = node("span", "qg-enhance-hint", words.enhanceHint);
  enhanceRow.append(enhanceToggle, enhanceLabel, enhanceHintNode);
  // The model doing the prompt work is whichever one is selected, so both the label and the hint
  // are filled from the gateway (it said "Qwen3.8-27B 本地" even while the 9B was selected).
  function renderEnhancer() {
    const info = (state.meta.current || {}).enhancer;
    if (!info) return;
    enhanceLabel.textContent = `${words.enhance}（${info}）`;
    enhanceHintNode.textContent = words.enhanceHint;
    enhanceRow.title = `${info}｜提示词优化会用当前模型，需要时先把它加载起来`;
  }
  const composerActions = node("div", "qg-actions");
  const clearButton = button("清空", "qg-button", () => clearComposer());
  const submitButton = button(words.generate, "qg-button qg-button-primary", () => submit());
  const statusLine = node("p", "qg-status-line", "");
  composerActions.append(clearButton, submitButton, statusLine);
  composerCard.append(upscaleHintNode, promptField, negativeInput, paramsHead, paramsRow, referenceRow,
    enhanceRow, composerActions);

  // LoRA presets get their own card: one-click LoRAs (accelerators also set the
  // steps and sampler that make them converge) plus the quality add-ons.
  const presetCard = node("section", "qg-card qg-preset-card");
  const presetCardHead = node("div", "qg-card-head");
  const presetCardHeadText = node("div", "qg-card-head-text");
  presetCardHeadText.append(
    node("h3", "qg-card-title", words.presetTitle),
    node("p", "qg-card-hint", words.presetHint),
  );
  presetCardHead.append(presetCardHeadText);
  const presetBar = node("div", "qg-presets");
  const presetHead = node("div", "qg-preset-head");
  const presetHint = node("span", "qg-preset-hint", words.presetHint);
  const presetClear = button(words.presetOff, "qg-button qg-button-small qg-preset-clear", () => clearPresets());
  presetHead.append(node("span", "qg-preset-title", words.presetTitle), presetHint, presetClear);
  const presetRows = node("div", "qg-preset-rows");
  presetBar.append(presetHead, presetRows);
  presetBar.hidden = true;
  presetCard.append(presetCardHead, presetBar);

  // History
  const historyCard = node("section", "qg-card qg-history");
  const historyHead = node("div", "qg-card-head");
  const historyHeadText = node("div", "qg-card-head-text");
  const historyTitle = node("h3", "qg-card-title", words.historyTitle);
  const historyHint = node("p", "qg-card-hint", words.historyServer);
  historyHeadText.append(historyTitle, historyHint);
  const historyActions = node("div", "qg-history-actions");
  const compareHint = node("span", "qg-compare-hint", words.compareHint);
  const clearHistory = button(words.historyClear, "qg-button", () => clearHistoryEntries());
  historyActions.append(compareHint, clearHistory);
  historyHead.append(historyHeadText, historyActions);
  const historyList = node("div", "qg-history-list");
  const historyEmpty = node("p", "qg-empty", words.historyEmpty);
  historyCard.append(historyHead, historyList, historyEmpty);

  // Sub-modules: 生成图片 writes a new picture, 超分 only restores one, 降噪 only
  // removes its grain — the last one rebuilds nothing, so it owns its own card.
  const submodeCard = node("section", "qg-card qg-submodes-card");
  const submodeRow = node("div", "qg-submodes");
  const submodeButtons = {};
  const submodeLabels = { generate: words.subGenerate, upscale: words.subUpscale,
                          denoise: words.subDenoise };
  for (const key of ["generate", "upscale", "denoise"]) {
    const chip = button(submodeLabels[key], "qg-submode", () => setSubmode(key));
    chip.dataset.submode = key;
    submodeButtons[key] = chip;
    submodeRow.append(chip);
  }
  const submodeHint = node("p", "qg-card-hint", words.subGenerateHint);
  submodeCard.append(submodeRow, submodeHint);

  // The denoise module: its own picture, its own strength, no pipeline and no graph.
  // Measured order is upscale first, denoise second, so it stays a separate step the user
  // runs on purpose rather than a pass hidden inside the upscale.
  const denoiseCard = node("section", "qg-card qg-denoise-card");
  denoiseCard.hidden = true;
  const denoiseNote = node("p", "qg-denoise-note", words.denoiseNote);
  const denoiseStrength = field("denoise-strength", words.denoiseStrength, 2,
                                { min: 1, max: 3.5, step: 0.1 });
  denoiseStrength.wrapper.title = words.denoiseStrengthHint;
  // Its own picker classes, not the composer's reference slots: two elements answering to
  // ".qg-reference-input" would hand the wrong file to whichever code asks first.
  const denoisePick = node("label", "qg-denoise-pick");
  denoisePick.append(node("span", "qg-field-label", words.denoiseSource));
  const denoiseInput = node("input", "qg-denoise-file");
  denoiseInput.type = "file";
  denoiseInput.accept = "image/png,image/jpeg,image/webp";
  const denoiseName = node("span", "qg-denoise-name", words.noFile);
  denoiseInput.addEventListener("change", () => {
    denoiseName.textContent = denoiseInput.files[0]?.name || words.noFile;
    denoisePick.dataset.filled = String(Boolean(denoiseInput.files[0]));
    denoiseNote.textContent = words.denoiseNote;
  });
  denoisePick.append(denoiseInput, denoiseName);
  const denoiseActions = node("div", "qg-actions");
  const denoiseButton = button(words.denoiseRun, "qg-button qg-button-primary", () => submitDenoise());
  denoiseActions.append(denoiseButton, denoiseNote);
  const denoisePreview = node("img", "qg-denoise-preview");
  denoisePreview.hidden = true;
  denoiseCard.append(node("h3", "qg-card-title", words.denoiseTitle),
                     node("p", "qg-card-hint", words.denoiseHint),
                     denoiseStrength.wrapper, denoisePick, denoiseActions, denoisePreview);

  // One-click workflows: the first thing to touch, because it decides the look.
  const switchCard = node("section", "qg-card qg-switch");
  const switchHead = node("div", "qg-card-head");
  const switchHeadText = node("div", "qg-card-head-text");
  const switchTitle = node("h3", "qg-card-title", words.switchTitle);
  const switchHint = node("p", "qg-card-hint", words.switchHint);
  switchHeadText.append(switchTitle, switchHint);
  const switchNote = node("p", "qg-switch-note", "");
  const switchRow = node("div", "qg-switch-row");
  switchHead.append(switchHeadText);
  switchCard.append(switchHead, switchRow, switchNote);

  progressColumn.append(progressCard);
  mainColumn.append(submodeCard, denoiseCard, switchCard, composerCard, presetCard, historyCard);
  sideColumn.append(modelCard, metrics);
  grid.append(progressColumn, mainColumn, sideColumn);
  layout.append(intro, grid);
  root.append(layout);
  // Mounted immediately and hidden by the `hidden` attribute; mode-control.js
  // only toggles visibility, so the panel keeps its state between visits.
  document.body.append(root);

  // Compare overlay
  const compareOverlay = node("div", "qg-compare");
  compareOverlay.hidden = true;
  const compareInner = node("div", "qg-compare-inner");
  const compareHead = node("div", "qg-compare-head");
  compareHead.append(node("h3", "qg-card-title", words.historyCompare));
  const compareBody = node("div", "qg-compare-body");
  compareHead.append(button(words.close, "qg-button", () => closeCompare()));
  compareInner.append(compareHead, compareBody);
  compareOverlay.append(compareInner);
  compareOverlay.addEventListener("click", event => { if (event.target === compareOverlay) closeCompare(); });
  root.append(compareOverlay);

  const toastStack = node("div", "qg-toasts");
  root.append(toastStack);

  // -- helpers ---------------------------------------------------------------
  const fieldInput = name => [...fields, seedField].find(entry => entry.input.name === name).input;

  function readImage(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error("参考图读取失败"));
      reader.readAsDataURL(file);
    });
  }

  function formatSeconds(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) return "—";
    if (value < 10) return value.toFixed(1) + " " + words.seconds;
    if (value < 60) return Math.round(value) + " " + words.seconds;
    const minutes = Math.floor(value / 60);
    return `${minutes} 分 ${String(Math.round(value % 60)).padStart(2, "0")} ` + words.seconds;
  }

  function formatNumber(value, digits = 2) {
    return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";
  }

  function refreshReferences() {
    const hasReference = referenceInputs.some(input => input.files && input.files[0]);
    denoiseField.input.disabled = !hasReference;
    denoiseField.wrapper.classList.toggle("qg-field-disabled", !hasReference);
    [...referenceRow.querySelectorAll(".qg-reference")].forEach((wrapper, index) => {
      const input = wrapper.querySelector(".qg-reference-input");
      const caption = wrapper.querySelector(".qg-reference-name");
      const clearOne = wrapper.querySelector(".qg-reference-clear");
      const file = input.files[0];
      wrapper.dataset.filled = String(Boolean(file));
      caption.textContent = file ? file.name : (index ? words.referenceTwo : words.referenceOne);
      clearOne.hidden = !file;
    });
  }

  function rollSeed() {
    const seed = fieldInput("seed");
    seed.value = String(Math.floor(Math.random() * 2147483647));
    setStatus(`${words.randomDone}: ${seed.value}`);
  }

  // Ctrl/Cmd+R inside the seed box rolls a seed instead of reloading the page,
  // which keeps an unsent prompt intact.
  seedField.input.addEventListener("keydown", event => {
    if ((event.ctrlKey || event.metaKey) && !event.shiftKey && event.key.toLowerCase() === "r") {
      event.preventDefault();
      rollSeed();
    }
  });

  function resetParams() {
    for (const [name, value] of Object.entries(defaults)) fieldInput(name).value = String(value);
    // Defaults restore the size and sampler settings, but a random placeholder
    // seed would silently reproduce the previous image, so roll a fresh one.
    rollSeed();
    setStatus(words.resetDone);
  }

  function clearComposer() {
    promptInput.value = "";
    negativeInput.value = "";
    for (const [index, input] of referenceInputs.entries()) {
      input.value = "";
      referenceStates[index] = null;
    }
    refreshReferences();
    setStatus("");
  }

  function setStatus(text) {
    statusLine.textContent = text || "";
  }

  function showToast(text, kind = "info") {
    const toast = node("div", "qg-toast", text);
    toast.dataset.kind = kind;
    const close = button("×", "qg-toast-close", () => toast.remove());
    close.setAttribute("aria-label", words.close);
    toast.append(close);
    toastStack.append(toast);
    setTimeout(() => toast.remove(), 7000);
  }

  // -- workflow --------------------------------------------------------------
  function composerParams() {
    const params = {};
    for (const { input } of [...fields, seedField]) params[input.name] = Number(input.value);
    return params;
  }

  function workflowQuery() {
    const params = composerParams();
    const query = new URLSearchParams();
    // Only the parameters this pipeline actually owns; Chroma has no CFG/denoise
    // field of its own and SDXL ignores the reference strength for txt2img.
    const owned = Object.keys(state.workflow?.sync || { width: 1, height: 1, steps: 1, cfg: 1, seed: 1 });
    for (const name of new Set([...owned, "denoise"])) {
      if (Number.isFinite(params[name])) query.set(name, String(params[name]));
    }
    if (state.meta.current?.pipeline) query.set("pipeline", state.meta.current.pipeline);
    return query.toString();
  }

  async function loadWorkflow() {
    try {
      // The panel no longer shows the graph, but it still needs the resolved
      // pipeline state: sync fields, presets, model lists and reference support.
      const payload = await api(`/local/image/workflow?${workflowQuery()}`);
      state.workflow = payload;
      state.workflowInputs = {};
      state.workflowKey = null;
      renderPresets();
      alignWithActivePreset();
      renderPresets();
      renderSubmode();
      renderWorkflowSwitch();
      renderReferenceVisibility();
      renderParamsVisibility(payload.pipeline || state.meta.current?.pipeline);
      // A page load can find an upscaler left as the active pipeline: unless the
      // user is in the upscale sub-module, go back to a generation recipe so the
      // prompt and canvas are usable again.
      if (!state.pipelineRestored && state.submode === "generate" && payload.references_required === true) {
        state.pipelineRestored = true;
        const painter = (state.workflows || []).find(entry => (entry.mode || "image") === "image"
          && (entry.kind || "generate") === "generate" && entry.available !== false);
        if (painter) void applyWorkflow(painter.key);
      }
    } catch (failure) {
      setStatus(failure.message);
    }
  }

  function syncParam(name, value) {
    const target = fieldInput(name);
    if (!target || target.value === value) return;
    target.value = value;
  }

  // -- LoRA presets ----------------------------------------------------------
  function renderPresets() {
    const presets = state.workflow?.presets || [];
    presetBar.hidden = !presets.length;
    if (!presets.length) return;
    const rows = [];
    for (const [role, label] of [["speed", words.presetSpeed], ["quality", words.presetQuality]]) {
      const rolePresets = presets.filter(preset => preset.role === role);
      if (!rolePresets.length) continue;
      const row = node("div", "qg-preset-row");
      row.append(node("span", "qg-preset-role", label));
      for (const preset of rolePresets) {
        const chip = button(preset.label, "qg-preset-chip", () => applyPreset(preset.key));
        chip.dataset.role = role;
        chip.dataset.active = String(Boolean(preset.active));
        chip.dataset.available = String(preset.available !== false);
        const advice = preset.role === "speed" && preset.params?.steps
          ? `\n${words.presetSpeedHint.replace("{steps}", preset.params.steps).replace("{cfg}", preset.params.cfg)}`
          : "";
        chip.title = preset.available === false
          ? `${preset.note}\n${words.presetMissing}：${preset.lora}${advice}`
          : `${preset.note}\nLoRA：${preset.lora}${advice}`;
        if (preset.available === false) chip.append(node("span", "qg-preset-tag", words.presetMissing));
        if (preset.active) chip.append(node("span", "qg-preset-tag", words.presetActive));
        chip.disabled = preset.available === false || Boolean(state.busy);
        row.append(chip);
      }
      rows.push(row);
    }
    presetRows.replaceChildren(...rows);
    presetClear.disabled = Boolean(state.busy) || !presets.some(preset => preset.active);
    const active = presets.filter(preset => preset.active).map(preset => preset.label);
    // An accelerator only works at the step count and CFG it was distilled for;
    // running LCM or DMD2 at 28 steps / CFG 6 produces a posterised mess, so the
    // hint turns into a warning when the composer drifts away from the preset.
    const speed = presets.find(preset => preset.active && preset.role === "speed");
    const drift = speed ? presetDrift(speed) : null;
    if (drift) {
      presetHint.dataset.warning = "true";
      presetHint.title = speed.note || "";
      const signature = `${speed.key}:${drift.want.steps}:${drift.want.cfg}:${drift.have.steps}:${drift.have.cfg}`;
      if (presetHint.dataset.signature !== signature) {
        presetHint.dataset.signature = signature;
        presetHint.replaceChildren(
          node("span", "qg-preset-warning-text",
               `${words.presetMismatch}：${speed.label} 建议 ${drift.want.steps} 步 / CFG ${drift.want.cfg}，`
               + `当前 ${drift.have.steps} / ${drift.have.cfg}`),
          button(words.presetRestore, "qg-button qg-button-small qg-preset-restore",
                 () => applyPreset(speed.key)),
        );
      }
      return;
    }
    delete presetHint.dataset.warning;
    delete presetHint.dataset.signature;
    presetHint.replaceChildren(document.createTextNode(
      active.length ? `${words.presetActive}：${active.join(" + ")}` : words.presetHint));
  }

  function presetDrift(preset) {
    const read = name => {
      const entry = [...fields, seedField].find(item => item.input.name === name);
      return entry ? Number(entry.input.value) : NaN;
    };
    const want = { steps: Number(preset.params?.steps), cfg: Number(preset.params?.cfg) };
    const have = { steps: read("steps"), cfg: read("cfg") };
    const stepsOff = Number.isFinite(want.steps) && Number.isFinite(have.steps) && have.steps !== want.steps;
    const cfgOff = Number.isFinite(want.cfg) && Number.isFinite(have.cfg) && Math.abs(have.cfg - want.cfg) > 0.05;
    return stepsOff || cfgOff ? { want, have } : null;
  }

  function applyPresetParams(params) {
    for (const [name, value] of Object.entries(params || {})) {
      const numeric = Number(value);
      // `sampler`/`scheduler` are strings and live in the workflow card instead.
      if (!Number.isFinite(numeric)) continue;
      const target = [...fields, seedField].find(entry => entry.input.name === name);
      if (!target) continue;
      target.input.value = String(numeric);
    }
  }

  async function applyPreset(key) {
    const pipeline = state.workflow?.pipeline || state.meta.current?.pipeline;
    try {
      const payload = await api("/local/image/presets", { method: "POST", body: { pipeline, key } });
      state.workflow = payload.workflow;
      state.workflowInputs = {};
      state.workflowKey = null;
      // The accelerator's steps/CFG belong to the composer, so apply them here;
      // its sampler and scheduler were persisted with the workflow file.
      applyPresetParams(payload.params);
      renderPresets();
      renderWorkflowSwitch();
      const label = payload.applied?.label || key;
      setStatus(`${words.presetApplied}：${label}`);
      showToast(`${words.presetApplied} ${label}`, "success");
    } catch (failure) {
      showToast(failure.message, "error");
    }
  }

  async function clearPresets() {
    const pipeline = state.workflow?.pipeline || state.meta.current?.pipeline;
    try {
      const payload = await api(`/local/image/presets?pipeline=${encodeURIComponent(pipeline || "")}`, { method: "DELETE" });
      state.workflow = payload.workflow;
      state.workflowInputs = {};
      state.workflowKey = null;
      applyPresetParams(payload.params);
      renderPresets();
      renderWorkflowSwitch();
      setStatus(words.presetCleared);
      showToast(words.presetCleared, "success");
    } catch (failure) {
      showToast(failure.message, "error");
    }
  }

  // -- one-click workflows ---------------------------------------------------
  async function loadWorkflows() {
    try {
      const payload = await api("/local/image/workflows");
      state.workflows = payload.workflows || [];
      state.workflowListError = null;
    } catch (failure) {
      state.workflows = [];
      state.workflowListError = failure.message;
    }
    renderWorkflowSwitch();
  }

  function activePresetKeys(pipelineKey) {
    const presets = pipelineKey && pipelineKey === (state.workflow?.pipeline || state.meta.current?.pipeline)
      ? (state.workflow?.presets || []) : [];
    return presets.filter(preset => preset.active).map(preset => preset.key).sort();
  }

  function composerNumber(name) {
    const target = [...fields, seedField].find(entry => entry.input.name === name);
    return target ? Number(target.input.value) : NaN;
  }

  function workflowIsActive(entry) {
    const pipeline = state.workflow?.pipeline || state.meta.current?.pipeline;
    if (entry.pipeline !== pipeline) return false;
    if (entry.presets.slice().sort().join() !== activePresetKeys(pipeline).join()) return false;
    // Every parameter the recipe pins has to match the composer, not just the step count:
    // the three upscales share a pipeline, steps and cfg and differ only in the multiplier,
    // so comparing a subset lit two chips up at once.
    const tolerance = { steps: 0, cfg: 0.05, denoise: 0.02, scale: 0.001 };
    for (const [name, value] of Object.entries(entry.params || {})) {
      const wanted = Number(value);
      const current = composerNumber(name);
      if (!Number.isFinite(wanted) || !Number.isFinite(current)) continue;
      if (Math.abs(current - wanted) > (tolerance[name] ?? 0.001)) return false;
    }
    return true;
  }

  function describeWorkflow(entry) {
    if (!entry) return words.switchHint;
    const state_ = entry.available === false
      ? `（${words.switchUnavailable}）` : (workflowIsActive(entry) ? `（${words.switchActive}）` : "");
    return `${entry.label}${state_}：${entry.summary}${entry.detail ? " " + entry.detail : ""}`;
  }

  function activeSpeedPreset() {
    const presets = state.workflow?.presets || [];
    return presets.find(preset => preset.active && preset.role === "speed") || null;
  }

  function alignWithActivePreset() {
    // The saved accelerator defines the steps/CFG it was distilled for. Aligning
    // here (only when the workflow state is loaded) keeps the panel consistent
    // without fighting values the user typed while working.
    const speed = activeSpeedPreset();
    if (!speed || !speed.params) return false;
    const before = [fieldInput("steps").value, fieldInput("cfg").value];
    applyPresetParams(speed.params);
    return before[0] !== fieldInput("steps").value || before[1] !== fieldInput("cfg").value;
  }

  function renderSubmode() {
    const upscale = state.submode === "upscale";
    const denoise = state.submode === "denoise";
    for (const [key, chip] of Object.entries(submodeButtons)) {
      const on = key === state.submode;
      chip.dataset.active = String(on);
      chip.setAttribute("aria-pressed", String(on));
    }
    submodeHint.textContent = denoise ? words.subDenoiseHint
      : (upscale ? words.subUpscaleHint : words.subGenerateHint);
    switchTitle.textContent = upscale ? "超分配方" : words.switchTitle;
    switchHint.textContent = upscale ? words.subUpscaleHint : words.switchHint;
    presetCard.hidden = upscale || denoise;
    composerCard.hidden = denoise;
    denoiseCard.hidden = !denoise;
    submitButton.textContent = upscale ? words.upscaleGenerate : words.generate;
    renderWorkflowSwitch();
    // renderWorkflowSwitch unhides its own card, so the denoise module hides it after.
    switchCard.hidden = denoise;
    if (!denoise) renderParamsVisibility(state.workflow?.pipeline || state.meta.current?.pipeline);
  }

  function setSubmode(key) {
    if (!["generate", "upscale", "denoise"].includes(key) || key === state.submode) return;
    state.submode = key;
    state.pipelineRestored = key === "upscale";
    renderSubmode();
    const entries = (state.workflows || []).filter(entry => (entry.mode || "image") === "image");
    const current = state.workflow?.pipeline || state.meta.current?.pipeline;
    if (key === "upscale") {
      // Entering 超分 without an upscale pipeline would leave the multiplier
      // unset, so the first upscale recipe is applied for the user.
      const recipes = entries.filter(entry => (entry.kind || "generate") === "upscale");
      if (recipes.length && !recipes.some(entry => entry.pipeline === current && workflowIsActive(entry))) {
        void applyWorkflow(recipes[0].key);
      }
    } else if (state.workflow?.references_required === true) {
      // Leaving the upscale sub-module must not leave an upscaler as the active pipeline.
      const painter = entries.find(entry => (entry.kind || "generate") === "generate" && entry.available !== false);
      if (painter) void applyWorkflow(painter.key);
    }
  }

  function activeWorkflow() {
    return (state.workflows || []).find(workflowIsActive) || null;
  }

  function renderReferenceVisibility() {
    // A recipe that cannot start from an uploaded image hides the whole row, so
    // the panel never offers an input the run would ignore.
    const active = activeWorkflow();
    const supported = !active || active.references !== false;
    referenceRow.hidden = !supported;
    if (!supported) {
      for (const input of referenceInputs) {
        input.value = "";
      }
      referenceRow.dispatchEvent(new Event("change"));
    }
  }

  function renderWorkflowSwitch() {
    // The video recipes live in the video mode; each sub-module then offers its own
    // half: 生成图片 the generation recipes, 超分放大 the three multipliers.
    const entries = (state.workflows || [])
      .filter(entry => (entry.mode || "image") === "image")
      .filter(entry => (state.submode === "upscale") === ((entry.kind || "generate") === "upscale"));
    // The denoise module has no recipes at all, and the card is re-rendered on every
    // workflow refresh, so the mode decides its visibility here rather than once.
    switchCard.hidden = state.submode === "denoise";
    if (!entries.length) {
      switchRow.replaceChildren();
      switchNote.textContent = state.workflowListError || words.switchHint;
      return;
    }
    const signature = entries.map(entry => `${entry.key}:${entry.available}:${workflowIsActive(entry)}`).join("|")
      + `|${state.busy}`;
    if (switchRow.dataset.signature === signature) {
      switchNote.textContent = describeWorkflow(entries.find(workflowIsActive) || entries[0]);
      return;
    }
    switchRow.dataset.signature = signature;
    switchRow.replaceChildren(...entries.map(entry => {
      const chip = button(entry.label, "qg-switch-chip", () => applyWorkflow(entry.key));
      chip.dataset.workflow = entry.key;
      chip.dataset.active = String(workflowIsActive(entry));
      chip.dataset.available = String(entry.available !== false);
      chip.title = describeWorkflow(entry);
      if (entry.available === false) chip.append(node("span", "qg-switch-tag", words.switchUnavailable));
      const show = () => { switchNote.textContent = describeWorkflow(entry); };
      chip.addEventListener("mouseenter", show);
      chip.addEventListener("focus", show);
      chip.disabled = entry.available === false || Boolean(state.busy);
      return chip;
    }));
    switchNote.textContent = describeWorkflow(entries.find(workflowIsActive) || entries[0]);
    renderReferenceVisibility();
  }

  async function applyWorkflow(key) {
    try {
      const payload = await api("/local/image/workflows", { method: "POST", body: { key } });
      applyPresetParams(payload.params || {});
      state.workflow = payload.workflow_payload;
      state.workflowInputs = {};
      state.workflowKey = null;
      state.meta.current = payload.model || state.meta.current;
      renderPipelines();
      renderModel();
      renderPresets();
      renderWorkflowSwitch();
      renderReferenceVisibility();
      renderParamsVisibility(payload.pipeline || state.meta.current?.pipeline);
      await loadWorkflows();
      const label = payload.workflow?.label || key;
      setStatus(`${words.switchApplied}：${label}`);
      showToast(`${words.switchApplied} ${label}`, "success");
    } catch (failure) {
      showToast(`${words.switchFailed}：${failure.message}`, "error");
    }
  }

  // -- GPU / VRAM ------------------------------------------------------------
  function formatMib(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) return "—";
    return value >= 1024 ? `${(value / 1024).toFixed(2)} GB` : `${Math.round(value)} MB`;
  }

  function renderGpu() {
    const snapshot = state.gpu || state.job?.gpu || null;
    if (!snapshot) {
      gpuRow.hidden = true;
      gpuBar.hidden = true;
      gpuHint.hidden = false;
      gpuHint.textContent = words.vramUnavailable;
      gpuDelta.textContent = "—";
      return;
    }
    gpuRow.hidden = false;
    gpuBar.hidden = false;
    gpuHint.hidden = false;
    gpuValues.total.textContent = formatMib(snapshot.total_mib);
    gpuValues.used.textContent = formatMib(snapshot.used_mib);
    gpuValues.job.textContent = formatMib(snapshot.image_peak_mib ?? snapshot.job_peak_mib);
    gpuValues.peak.textContent = formatMib(snapshot.job_peak_mib);
    const total = snapshot.total_mib || 1;
    gpuBarFill.style.width = `${Math.min(100, (snapshot.used_mib / total) * 100).toFixed(1)}%`;
    gpuBarJob.style.width = `${Math.min(100, ((snapshot.job_mib || 0) / total) * 100).toFixed(1)}%`;
    const parts = [`${words.vramFree} ${formatMib(snapshot.free_mib)}`];
    if (typeof snapshot.utilization === "number") parts.push(`${words.vramUtil} ${Math.round(snapshot.utilization)}%`);
    if (typeof snapshot.image_mib === "number") parts.push(`生图进程当前 ${formatMib(snapshot.image_mib)}`);
    if (snapshot.baseline_mib) parts.push(`任务前 ${formatMib(snapshot.baseline_mib)}`);
    gpuHint.textContent = parts.join(" · ");
    gpuDelta.textContent = snapshot.job_peak_mib ? `+${formatMib(snapshot.job_peak_mib)}` : "—";
  }

  async function loadGpu() {
    try {
      const payload = await api("/local/image/gpu");
      state.gpu = payload.gpu;
    } catch (failure) {
      state.gpu = null;
    }
    renderGpu();
  }

  // -- API -------------------------------------------------------------------
  async function api(path, options = {}) {
    const response = await fetch(path, {
      cache: "no-store",
      headers: options.body ? { "Content-Type": "application/json" } : undefined,
      ...options,
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || payload.error || `请求失败 (${response.status})`);
    return payload;
  }

  async function loadMeta() {
    try {
      state.meta = await api("/local/image/meta");
    } catch (failure) {
      state.meta = { comfy: { reachable: false, running: 0, pending: 0 }, pipelines: state.meta.pipelines || [], models: state.meta.models || {}, current: state.meta.current || {} };
    }
    renderPipelines();
    renderModel();
    renderMetrics();
    void loadWorkflows();
  }

  function renderPipelines() {
    const pipelines = state.meta.pipelines || [];
    const current = state.meta.current?.pipeline || (pipelines[0] && pipelines[0].key);
    if (!pipelines.length) return;
    // Rebuild when the offered set differs; comparing lengths would skip the
    // first build (no options, no pipelines) and any same-sized change.
    const offered = [...pipelineSelect.options].map(option => option.value);
    if (offered.join() !== pipelines.map(pipeline => pipeline.key).join()) {
      pipelineSelect.replaceChildren(...pipelines.map(pipeline => {
        const option = node("option", "", pipeline.ready === false ? `${pipeline.name}（缺 ${pipeline.missing.join("/")}）` : pipeline.name);
        option.value = pipeline.key;
        option.dataset.ready = String(pipeline.ready !== false);
        return option;
      }));
    }
    pipelineSelect.value = current;
    const active = pipelines.find(pipeline => pipeline.key === current);
    if (active) {
      const damaged = (active.damaged || []).map(item => `${item.file} ${item.problem}`).join("；");
      const missing = active.ready === false ? ` ⚠ 还缺 ${active.missing.join("、")}，下载后即可使用。` : "";
      const broken = damaged ? ` ⚠ ${damaged}` : "";
      pipelineNote.textContent = `${active.summary} NSFW：${active.nsfw}${missing}${broken}`;
      pipelineNote.dataset.nsfw = String(/不审查|最成熟/.test(active.nsfw));
      pipelineNote.dataset.missing = String(active.ready === false || Boolean(damaged));
    }
    pipelineSelect.disabled = Boolean(state.busy);
    renderParamsVisibility(current);
  }

  function renderParamsVisibility(pipelineKey) {
    // The composer mirrors the pipeline's own parameters: Chroma has a real
    // negative prompt but no separate CFG field on the canvas node, SDXL uses
    // the reference strength only when a reference image is supplied.
    const owns = new Set(Object.keys(state.workflow?.sync || {}));
    for (const { wrapper, input } of [...fields, seedField]) {
      wrapper.hidden = !owns.has(input.name);
    }
    // The 超分放大 sub-module is prompt-free and always samples from an image.
    const upscaler = state.submode === "upscale" || state.workflow?.references_required === true;
    const enhance = document.querySelector(".qg-enhance");
    if (enhance) enhance.hidden = upscaler;
    promptField.hidden = upscaler;
    negativeInput.hidden = upscaler;
    upscaleHintNode.hidden = !upscaler;
    for (const name of ["width", "height"]) {
      const target = fields.find(entry => entry.input.name === name);
      if (target && upscaler) target.wrapper.hidden = true;
    }
    if (upscaler) {
      const denoise = fields.find(entry => entry.input.name === "denoise");
      if (denoise) denoise.wrapper.hidden = true;
    }
    promptInput.placeholder = pipelineKey === "flux2"
      ? words.promptPlaceholder
      : `${words.promptPlaceholder}${pipelineKey === "sdxl" ? "（可写 danbooru 风格标签）" : ""}`;
  }

  async function selectPipeline(key) {
    if (!key || key === state.meta.current?.pipeline) return;
    if (state.busy) {
      setStatus(words.pipelineBusy);
      return;
    }
    pipelineSelect.disabled = true;
    try {
      await api("/local/image/pipeline", { method: "POST", body: { pipeline: key } });
      setStatus(`${words.pipelineSwitched}: ${key}`);
      await loadMeta();
      // Each pipeline has its own sensible sampling defaults; carrying Flux's
      // 4 steps into SDXL (or SDXL's 28 into a 4-step model) would be wrong.
      const defaults = state.meta.current?.defaults || {};
      for (const [name, value] of Object.entries(defaults)) {
        const target = [...fields, seedField].find(entry => entry.input.name === name);
        if (target && Number.isFinite(Number(value))) target.input.value = String(value);
      }
      // A pipeline default that contradicts the accelerator already in the
      // pipeline would warn immediately; the accelerator's recipe wins.
      alignWithActivePreset();
      rollSeed();
      state.workflowKey = null;
      await loadWorkflow();
      renderJob();
    } catch (failure) {
      setStatus(failure.message);
      renderPipelines();
    } finally {
      pipelineSelect.disabled = Boolean(state.busy);
    }
  }

  async function loadHistory() {
    try {
      const payload = await api("/local/image/history");
      state.history = payload.entries || [];
    } catch (failure) {
      state.history = [];
    }
    state.historyLoaded = true;
    renderHistory();
    renderMetrics();
  }

  // The denoise module's own run: read the picture, post it, show what it measured.
  async function submitDenoise() {
    const file = denoiseInput.files[0];
    if (!file) {
      denoiseNote.textContent = words.denoiseNeedsImage;
      showToast(words.denoiseNeedsImage, "error");
      return;
    }
    denoiseButton.disabled = true;
    denoiseNote.textContent = words.denoising;
    try {
      const image = await readImage(file);
      const payload = await api("/local/image/denoise", {
        method: "POST",
        body: { image, strength: Number(denoiseStrength.input.value) || 2 },
      });
      denoiseNote.textContent = `${payload.note} · 已存到 ${(payload.target || "").split(/[\\/]/).pop()}`;
      denoisePreview.src = payload.image;
      denoisePreview.hidden = false;
    } catch (failure) {
      denoiseNote.textContent = `${words.failed}：${failure.message}`;
      showToast(`${words.toastFailed}：${failure.message || ""}`, "error");
    } finally {
      denoiseButton.disabled = false;
    }
  }

  async function submit() {
    if (state.submode === "denoise") {
      await submitDenoise();
      return;
    }
    const upscaler = state.submode === "upscale" || state.workflow?.references_required === true;
    const prompt = promptInput.value.trim();
    if (upscaler && !referenceInputs.some(input => input.files[0])) {
      setStatus(words.upscaleNeedsImage);
      showToast(words.upscaleNeedsImage, "error");
      return;
    }
    if (!upscaler && !prompt) {
      setStatus(words.noPrompt);
      promptInput.focus();
      return;
    }
    submitButton.disabled = true;
    setStatus(words.generating);
    try {
      const body = {
        prompt,
        negative_prompt: negativeInput.value.trim(),
        enhance_prompt: enhanceToggle.checked,
      };
      for (const { input } of [...fields, seedField]) body[input.name] = Number(input.value);
      const references = await Promise.all(referenceInputs.map(input => input.files[0] ? readImage(input.files[0]) : null));
      body.image_1 = references[0] || null;
      body.image_2 = references[1] || null;
      state.lastSubmission = body;
      const payload = await api("/local/image/generate", { method: "POST", body });
      state.job = payload.job;
      state.busy = true;
      state.events = 0;
      state.eventLog = [];
      state.lastFailed = null;
      state.announcedJob = null;
      state.submittedJob = payload.job?.id || null;
      failure.hidden = true;
      renderJob();
      // The running workflow lives in the job snapshot the gateway returns.
      void loadWorkflow();
      schedulePoll(250);
    } catch (failure) {
      // A rejected submission (busy, ComfyUI offline, model switching) has no
      // job to watch, so report it right here instead of looking like nothing
      // happened.
      state.lastFailed = { error: failure.message || words.failed, comfy: false };
      setStatus(failure.message || words.failed);
      renderFailure();
      showToast(`${words.toastFailed}：${failure.message || ""}`, "error");
      submitButton.disabled = false;
    }
  }

  async function retryJob() {
    if (!state.lastSubmission) return;
    retryButton.disabled = true;
    try {
      await loadMeta();
      if (!state.meta.comfy?.reachable) return;
      failure.hidden = true;
      state.lastFailed = null;
      await submit();
    } finally {
      retryButton.disabled = false;
    }
  }

  function dismissFailure() {
    state.lastFailed = null;
    failure.hidden = true;
  }

  function renderFailure() {
    const failed = state.lastFailed || (state.job?.state === "error" ? { error: state.job.error, comfy: false } : null);
    if (!failed) {
      failure.hidden = true;
      return;
    }
    const offline = /ComfyUI 未启动|无法连接|connection|refused/i.test(failed.error || "");
    failure.dataset.kind = offline ? "offline" : "error";
    failureText.replaceChildren(
      node("strong", "", offline ? words.comfyDownTitle : words.toastFailed),
      node("span", "", offline ? words.comfyDownBody : `${failed.error || ""} ${words.failedHint}`),
    );
    if (offline) retryButton.textContent = words.checkAgain;
    else retryButton.textContent = words.retry;
    retryButton.disabled = !state.lastSubmission;
    failure.hidden = false;
  }

  async function cancelJob() {
    cancelButton.disabled = true;
    setStatus(words.cancelRequested);
    try {
      const payload = await api("/local/image/cancel", { method: "POST", body: {} });
      state.job = payload.job;
      renderJob();
      schedulePoll(250);
    } catch (failure) {
      setStatus(failure.message);
    } finally {
      cancelButton.disabled = false;
    }
  }

  async function removeHistory(id) {
    await api(`/local/image/history/${encodeURIComponent(id)}`, { method: "DELETE" });
    state.history = state.history.filter(entry => entry.id !== id);
    state.compare = state.compare.filter(entry => entry !== id);
    renderHistory();
    renderMetrics();
  }

  async function clearHistoryEntries() {
    if (!window.confirm(words.clearConfirm)) return;
    await api("/local/image/history", { method: "DELETE" });
    state.history = [];
    state.compare = [];
    renderHistory();
    renderMetrics();
  }

  // -- polling ---------------------------------------------------------------
  /* A hidden generate view polls slower, not never.
   *
   * Measured: with the tab hidden this view kept asking /local/image/progress every 1.5 s —
   * the response carries the job payload including its whole event log — while nothing on
   * screen could show it. A running job keeps its own cadence, because that progress is what
   * the owner comes back to read; a hidden, idle tab drops to HIDDEN_TICK_MS and catches up
   * on the first poll after the tab returns. */
  const HIDDEN_TICK_MS = 6000;
  function pollDelayFor(delay) {
    if (document.visibilityState !== "hidden") return delay;
    if (state.active && (state.busy || (state.job && state.job.state === "running"))) return delay;
    return Math.max(delay, HIDDEN_TICK_MS);
  }
  function schedulePoll(delay) {
    clearTimeout(state.pollTimer);
    // A hidden view that is not showing a job has nothing to poll for: this view only ever
    // renders while its tab is open, and setActive(true) schedules the first poll itself.
    // Without this the loop re-armed a timer for every hidden visit, and every one of them
    // woke up to return immediately.
    if (document.visibilityState === "hidden" && !state.active) { state.pollTimer = null; return; }
    state.pollTimer = setTimeout(poll, pollDelayFor(delay));
  }

  async function poll() {
    if (!state.active) return;
    try {
      const payload = await api(`/local/image/progress?since=${state.events}&preview=${state.previewVersion}`);
      const job = payload.job;
      const wasBusy = state.busy;
      const settled = job && !payload.busy && job.state !== "running" && state.announcedJob !== job.id;
      const submitted = state.submittedJob;
      state.busy = payload.busy;
      state.submittedJob = null;
      state.meta.current = payload.model || state.meta.current;
      if (job) {
        state.job = job;
        if (Array.isArray(job.events)) {
          state.eventLog = job.events;
          state.events = job.events.length;
        }
      }
      state.pollDelay = payload.busy ? 800 : 1500;
      // A job the gateway reports as stalled has stopped owning the backend, so
      // stop treating it as running here too instead of waiting forever.
      if (job?.stalled) {
        state.busy = false;
        state.lastFailed = { error: job.error || words.stalled, comfy: false };
      }
      renderJob();
      renderModel();
      // The job payload carries the VRAM samples the gateway took while polling;
      // fall back to a direct read when nothing is running.
      if (job?.gpu) {
        state.gpu = job.gpu;
        renderGpu();
      } else {
        state.gpuTicks = (state.gpuTicks || 0) + 1;
        if (state.gpuTicks % 5 === 0) void loadGpu();
      }
      // Announce the outcome once per job. A job that fails instantly can finish
      // between two polls, so this keys on the job id, not on a busy transition.
      if (settled && (wasBusy || submitted === job.id)) {
        state.announcedJob = job.id;
        if (wasBusy) await Promise.all([loadHistory(), loadMeta()]);
        void loadWorkflow();
        if (job.state === "completed") {
          state.lastResult = job.images;
          showToast(words.toastCompleted, "success");
        } else if (job.state === "error") {
          showToast(`${words.toastFailed}：${job.error || ""}`, "error");
        } else if (job.state === "cancelled") {
          showToast(words.toastCancelled, "info");
        }
      }
      if (state.statusRender) state.statusRender();
    } catch (failure) {
      state.pollDelay = 3000;
    }
    if (state.active) schedulePoll(state.pollDelay);
  }

  // -- rendering -------------------------------------------------------------
  function renderBadges() {
    introBadges.replaceChildren();
    const model = state.meta.current || {};
    if (model.unet) introBadges.append(node("span", "qg-badge", model.unet));
    introBadges.append(node("span", "qg-badge", (model.enhancer || "Qwen") + " 优化"));
    renderEnhancer();
    const badge = node("span", "qg-badge qg-badge-state");
    badge.dataset.online = String(Boolean(state.meta.comfy?.reachable));
    badge.textContent = state.meta.comfy?.reachable ? words.comfyOnline : words.comfyOffline;
    introBadges.append(badge);
  }

  function renderModel() {
    const model = state.meta.current || {};
    const resolved = state.workflow?.template || [];
    // Show what will actually load: the values resolved into the next graph.
    const resolvedValues = {};
    for (const spec of resolved) {
      for (const field of spec.fields) {
        if (field.choices && field.value !== undefined && field.value !== null) resolvedValues[field.input] = field.value;
      }
    }
    const shown = {
      unet: resolvedValues.ckpt_name || resolvedValues.unet_name || model.unet,
      text_encoder: resolvedValues.clip_name || model.text_encoder,
      vae: resolvedValues.vae_name || model.vae,
      enhancer: model.enhancer,
      backend: model.backend,
    };
    const loras = (model.loras || []).filter(Boolean);
    if (loras.length) shown.text_encoder = `${shown.text_encoder || "-"} + LoRA ${loras.map(name => name.split("/").pop()).join(", ")}`;
    for (const [key, element] of Object.entries(modelValues)) element.textContent = shown[key] || "—";
    comfyBadge.dataset.online = String(Boolean(state.meta.comfy?.reachable));
    comfyBadge.textContent = state.meta.comfy?.reachable ? words.comfyOnline : words.comfyOffline;
    renderBadges();
    renderPipelines();
  }

  function historyMetrics() {
    const done = state.history.filter(entry => entry.state === "completed");
    const speeds = done.map(entry => entry.steps_per_second).filter(value => typeof value === "number" && value > 0);
    const durations = done.map(entry => entry.duration?.total).filter(value => typeof value === "number" && value > 0);
    const average = values => values.length ? values.reduce((total, value) => total + value, 0) / values.length : null;
    return {
      count: state.history.length,
      completed: done.length,
      successRate: state.history.length ? Math.round((done.length / state.history.length) * 100) : null,
      averageSpeed: average(speeds),
      averageTime: average(durations),
    };
  }

  function renderMetrics() {
    const stats = historyMetrics();
    const queue = state.meta.comfy || {};
    metricTiles.queue.textContent = !queue.reachable ? words.comfyOffline
      : (queue.running || queue.pending) ? words.queueBusy.replace("{running}", queue.running).replace("{pending}", queue.pending)
      : words.queueIdle;
    metricTiles.queue.dataset.busy = String(Boolean(queue.running || queue.pending));
    const gpu = state.gpu || state.job?.gpu;
    metricTiles.vram.textContent = gpu ? `${formatMib(gpu.used_mib)} / ${formatMib(gpu.total_mib)}` : words.vramUnavailable;
    metricTiles.vram.dataset.busy = String(Boolean(gpu && gpu.job_mib > 0));
    metricTiles.lastSpeed.textContent = stats.averageSpeed ? `${formatNumber(stats.averageSpeed)} ${words.perSecond}` : "—";
    metricTiles.averageTime.textContent = stats.averageTime ? formatSeconds(stats.averageTime) : "—";
    metricTiles.successRate.textContent = stats.successRate === null ? "—" : `${stats.successRate}%`;
    const last = state.history.find(entry => entry.state === "completed" && entry.steps_per_second);
    metricTiles.lastSpeed.textContent = last ? `${formatNumber(last.steps_per_second)} ${words.perSecond}` : "—";
    renderGpu();
  }

  function phaseText(job) {
    if (!job) return words.idle;
    if (job.state === "queued_behind") return `${words.phaseQueuedBehind} · ${words.phaseQueuedBehindHint}`;
    if (job.state === "error") return `${words.phaseError} · ${job.error || ""}`;
    if (job.state === "cancelled") return words.phaseCancelled;
    if (job.restoring) return words.phaseRestoring;
    const map = {
      starting_comfy: words.phaseStartingComfy,
      enhancing: words.phaseEnhancing,
      loading_model: words.phaseLoading,
      queued: words.phaseQueued,
      sampling: words.phaseSampling,
      decoding: words.phaseDecoding,
      restoring: words.phaseRestoring,
      completed: words.phaseCompleted,
      error: words.phaseError,
      cancelled: words.phaseCancelled,
      idle: words.phaseCompleted,
    };
    const base = map[job.phase] || job.phase;
    if (job.phase === "sampling" && job.max_step) return `${base} · ${words.stepOf.replace("{step}", job.step).replace("{max}", job.max_step)}`;
    if (job.phase === "queued" && typeof job.queue_position === "number") {
      return job.queue_position > 0 ? `${base} · #${job.queue_position}` : base;
    }
    return base;
  }

  function renderTimeline(job) {
    const timings = job?.timings || {};
    const stages = [
      ["prompt", words.promptTime],
      ["queue", words.queueTime],
      ["load", words.loadTime],
      ["sampling", words.samplingTime],
      ["restore", words.restoreTime],
    ].filter(([key]) => typeof timings[key] === "number");
    const total = timings.total || Object.values(timings).reduce((sum, value) => sum + value, 0);
    if (!stages.length || !total) {
      timeline.replaceChildren();
      timeline.hidden = true;
      return;
    }
    timeline.hidden = false;
    const rows = stages.map(([key, label]) => {
      const value = timings[key];
      const row = node("div", "qg-timeline-row");
      const bar = node("span", "qg-timeline-bar");
      bar.style.width = `${Math.max(2, Math.round((value / total) * 100))}%`;
      row.append(node("span", "qg-timeline-label", label), bar, node("span", "qg-timeline-value", formatSeconds(value)));
      return row;
    });
    const head = node("div", "qg-timeline-head");
    head.append(node("span", "", words.timings), node("span", "", `${words.totalTime} ${formatSeconds(total)}`));
    timeline.replaceChildren(head, ...rows);
  }

  function renderJob() {
    const job = state.job;
    const active = state.busy && job;
    cancelButton.hidden = !active;
    submitButton.disabled = Boolean(active);
    submitButton.textContent = active
      ? (job?.state === "queued_behind" ? words.waiting : words.generating)
      : (state.submode === "upscale" ? words.upscaleGenerate : words.generate);
    pipelineSelect.disabled = Boolean(active);
    progressPhase.textContent = phaseText(job);
    progressCard.dataset.state = job?.state || "idle";
    const progress = job ? Math.max(0, Math.min(1, job.progress || 0)) : 0;
    trackFill.style.width = `${Math.round(progress * 100)}%`;
    percentLabel.textContent = job && job.state === "completed" ? "100%" : `${Math.round(progress * 100)}%`;
    if (!job) stepLabel.textContent = words.idleHint;
    else if (job.phase === "sampling" && job.max_step) stepLabel.textContent = words.stepOf.replace("{step}", job.step).replace("{max}", job.max_step);
    else stepLabel.textContent = phaseText(job);

    statValues.elapsed.textContent = job ? formatSeconds(job.elapsed) : "—";
    if (job && typeof job.eta_seconds === "number" && job.state === "running") statValues.eta.textContent = formatSeconds(job.eta_seconds);
    else if (job && job.step && job.max_step && job.state === "running") {
      // No completed step yet: fall back to the measured local sampling rate so
      // the estimate is still useful, and label it as an estimate.
      const fallback = (job.max_step - job.step) / 0.4;
      statValues.eta.textContent = `≈ ${formatSeconds(fallback)}`;
    } else statValues.eta.textContent = "—";
    statValues.speed.textContent = job && job.steps_per_second ? `${formatNumber(job.steps_per_second)} ${words.perSecond}` : "—";
    statValues.sampling.textContent = job?.timings?.sampling ? formatSeconds(job.timings.sampling) : "—";

    // The picture arrives only when its version moves; the gateway omits it otherwise. An
    // omitted preview therefore means "you already have this one", not "there is none", and
    // the row must not be cleared on every poll just because the payload came back light.
    const previewVersion = job ? Number(job.preview_version || 0) : 0;
    if (job && job.preview) {
      previewImage.src = job.preview;
      state.previewVersion = previewVersion;
      previewRow.hidden = false;
    } else if (!job || !previewVersion) {
      state.previewVersion = 0;
      previewRow.hidden = true;
      previewImage.removeAttribute("src");
    }
    renderTimeline(job);
    renderGpu();
    renderPresets();
    renderWorkflowSwitch();
    renderFailure();
    renderMetrics();
  }

  function historyCardNode(entry) {
    const card = node("article", "qg-history-item");
    card.dataset.state = entry.state;
    card.dataset.entryId = entry.id;
    const media = node("div", "qg-history-media");
    const image = entry.images?.[0];
    if (image) {
      const thumb = node("img", "qg-history-thumb");
      thumb.src = image;
      thumb.alt = entry.prompt;
      thumb.loading = "lazy";
      media.append(thumb);
    } else {
      media.append(node("span", "qg-history-missing", entry.state === "error" ? words.failed : words.cancelled));
    }
    const info = node("div", "qg-history-info");
    const head = node("div", "qg-history-head");
    const time = new Date(entry.time || Date.now());
    head.append(node("time", "qg-history-time", time.toLocaleString("zh-CN", { hour12: false })));
    head.append(node("span", "qg-history-state", entry.state === "completed" ? words.completed : entry.state === "error" ? words.failed : words.cancelled));
    if (entry.enhanced) head.append(node("span", "qg-history-tag", words.enhancedPrompt));
    const prompt = node("p", "qg-history-prompt", entry.prompt);
    prompt.title = entry.final_prompt || entry.prompt;
    const chips = node("div", "qg-history-chips");
    const params = entry.params || {};
    for (const text of [
      `${words.width} ${params.width}`, `${words.height} ${params.height}`,
      `${words.steps} ${params.steps}`, `${words.guidance} ${params.cfg}`,
      `${words.strength} ${params.denoise}`, `${words.seed} ${params.seed}`,
      `${words.references} ${entry.references?.filter(Boolean).length || 0}`,
    ]) chips.append(node("span", "qg-history-chip", text));
    if (typeof entry.steps_per_second === "number") chips.append(node("span", "qg-history-chip qg-history-chip-speed", `${formatNumber(entry.steps_per_second)} ${words.perSecond}`));
    if (entry.duration?.total) chips.append(node("span", "qg-history-chip", `${words.totalTime} ${formatSeconds(entry.duration.total)}`));
    const actions = node("div", "qg-history-actions");
    actions.append(
      button(words.historyRestore, "qg-button qg-button-small", () => restoreEntry(entry)),
      button(words.historyReuse, "qg-button qg-button-small", () => reuseEntry(entry)),
      button(words.historyCompare, "qg-button qg-button-small", () => toggleCompare(entry)),
      button(words.historyRemove, "qg-button qg-button-small qg-button-danger", () => removeHistory(entry.id)),
    );
    if (entry.images?.[0]) {
      const open = node("a", "qg-button qg-button-small", words.historyOpen);
      open.href = entry.images[0];
      open.target = "_blank";
      open.rel = "noopener";
      actions.append(open);
    }
    if (entry.error) info.append(node("p", "qg-history-error", entry.error));
    info.append(head, prompt, chips, actions);
    card.append(media, info);
    return card;
  }

  function renderHistory() {
    historyEmpty.hidden = state.history.length > 0;
    historyTitle.textContent = `${words.historyTitle} · ${words.historyCount.replace("{count}", state.history.length)}`;
    compareHint.textContent = state.compare.length ? words.selected.replace("{count}", state.compare.length) : words.compareHint;
    historyList.replaceChildren(...state.history.map(historyCardNode));
  }

  function restoreEntry(entry) {
    applyParams(entry, true);
    setStatus(words.restored);
  }

  function reuseEntry(entry) {
    applyParams(entry, false);
    setStatus(words.reused);
  }

  function applyParams(entry, withPrompt) {
    const params = entry.params || {};
    for (const { input } of [...fields, seedField]) {
      if (typeof params[input.name] === "number") input.value = String(params[input.name]);
    }
    if (withPrompt) {
      promptInput.value = entry.prompt || "";
      negativeInput.value = entry.negative_prompt || "";
    }
  }

  function toggleCompare(entry) {
    if (state.compare.includes(entry.id)) state.compare = state.compare.filter(id => id !== entry.id);
    else state.compare = [...state.compare.slice(-1), entry.id];
    renderHistory();
    if (state.compare.length === 2) openCompare();
  }

  function openCompare() {
    const picked = state.compare.map(id => state.history.find(entry => entry.id === id)).filter(Boolean);
    if (picked.length !== 2) return;
    compareBody.replaceChildren(...picked.map(entry => {
      const column = node("figure", "qg-compare-item");
      const image = node("img", "qg-compare-image");
      image.src = entry.images?.[0] || "";
      image.alt = entry.prompt;
      const caption = node("figcaption", "qg-compare-caption");
      caption.append(node("strong", "", entry.prompt));
      const params = entry.params || {};
      caption.append(node("span", "", `${params.width}×${params.height} · ${words.steps} ${params.steps} · ${words.seed} ${params.seed}`));
      if (entry.steps_per_second) caption.append(node("span", "", `${formatNumber(entry.steps_per_second)} ${words.perSecond} · ${formatSeconds(entry.duration?.total)}`));
      column.append(image, caption);
      return column;
    }));
    compareOverlay.hidden = false;
  }

  function closeCompare() {
    compareOverlay.hidden = true;
    state.compare = [];
    renderHistory();
  }

  // -- lifecycle -------------------------------------------------------------
  let initialized = false;

  function ensureInitialized() {
    if (initialized) return;
    initialized = true;
    renderJob();
    renderHistory();
    renderModel();
    renderMetrics();
    void loadMeta();
    void loadHistory();
    void loadGpu();
    void loadWorkflow();
  }

  function setActive(active, statusRender) {
    if (statusRender) state.statusRender = statusRender;
    if (active === state.active) {
      if (active) ensureInitialized();
      return;
    }
    state.active = active;
    root.hidden = !active;
    // The chat screen is hidden by CSS only while this view is active, so
    // switching back to text/vision restores it untouched.
    document.documentElement.classList.toggle("local-generate-mode", active);
    clearTimeout(state.pollTimer);
    if (!active) return;
    ensureInitialized();
    // Every entry into the generate view starts from a fresh seed, so repeated
    // visits cannot silently reproduce the previous picture. Switching modes
    // reloads the page, so this also covers the reload that follows the switch.
    rollSeed();
    setStatus(`${words.randomDone}: ${fieldInput("seed").value}`);
    void loadMeta();
    void loadGpu();
    // The workflow preview follows the composer values while nothing runs.
    if (!state.busy) void loadWorkflow();
    schedulePoll(400);
  }

  window.ImagePanel = {
    root,
    setActive,
    refresh: () => { void loadMeta(); void loadHistory(); void loadGpu(); void loadWorkflow(); },
    isBusy: () => state.busy,
  };
})();
