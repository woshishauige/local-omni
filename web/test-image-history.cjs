// Offline regression for the generate view (web/image-panel.js).
//
// The app shell, the mode/status endpoints, the generation endpoints and the
// ComfyUI preview payloads are all mocked, so no model, GPU or ComfyUI process
// is used. Run from Qwen-Local with:  node web/test-image-history.cjs
//
// Note: Playwright's pipe on this machine mangles interior non-ASCII text on
// some return paths, so DOM-text comparisons happen inside page.evaluate and
// only booleans plus \u-escaped JSON come back.
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const { chromium } = require("./lib/playwright.cjs");

const origin = "http://127.0.0.1:8081";
const root = __dirname;
const injected = [
  '<link rel="stylesheet" href="/local/mode-control.css?v=3">',
  '<script defer src="/local/mode-control.js?v=3"></script>',
  '<link rel="stylesheet" href="/local/image-panel.css?v=3">',
  '<script defer src="/local/image-panel.js?v=3"></script>',
].join("");
// The fixture must declare UTF-8 exactly like the real app shell: without it the
// injected scripts (which contain raw Chinese literals) decode as windows-1252
// and every label renders as mojibake. The gateway serves them as charset=utf-8.
const fixture = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>
  * { box-sizing: border-box; } body { margin: 0; font-family: Arial; }
  body > div > .flex { display: flex; } aside { position: sticky; width: 56px; height: calc(100dvh - 18px); }
  .chat-screen { width: calc(100vw - 56px); min-height: calc(100dvh - 16px); display: flex; align-items: center; justify-content: center; }
  form { width: min(768px, calc(100% - 32px)); padding: 16px; border: 1px solid #ddd; } textarea { width: 100%; height: 80px; }
  @media(max-width:767px) { aside { position:fixed; left:8px; width:40px; height:40px; } .chat-screen { width:100%; } form { position:fixed; bottom:8px; width:calc(100% - 16px); } }
  </style></head><body><div style="display: contents"><div class="flex"><aside>Q</aside><main class="chat-screen"><form class="conversation-chat-form"><textarea></textarea><button type="submit">Send</button></form></main></div></div></body></html>`;
const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AARAAB/wD/AH8AAP8A/wD/AP8AAABJRU5ErkJggg==";
const preview = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFUlEQVR42mP8z8Dwn4GBgYGJAQoAHgQCAZ7l4xQAAAAASUVORK5CYII=";
const textStatus = { mode: "text", state: "ready", mtp_enabled: true, mtp_reason: "MTP", active_requests: 0, error: null, revision: 1, instance_id: "panel-test", sleeping: false, agent_enabled: true, image: {} };
const imageStatus = { ...textStatus, mode: "generate", mtp_enabled: false, mtp_reason: "生图模式由 ComfyUI 托管 Klein，MTP 关闭。", revision: 2 };
const meta = {
  comfy: { reachable: true, running: 0, pending: 0 },
  checkpoints: ["flux-2-klein-9b-fp8.safetensors"],
  models: {
    checkpoints: ["CyberRealisticPony_V18.0_F16.safetensors", "sam3.1_multiplex_fp16.safetensors"],
    diffusion_models: ["flux-2-klein-9b-fp8.safetensors"],
    text_encoders: ["qwen_3_8b_fp8mixed.safetensors"],
    vae: ["full_encoder_small_decoder.safetensors"],
    loras: ["detail/DetailedPerfection.safetensors", "speed/DMD2_Speed.safetensors"],
  },
  pipelines: [
    { key: "flux2", name: "Flux.2 Klein 9B", family: "flux2", summary: "原管线", nsfw: "受模型自身安全限制，NSFW 效果差", base_models: ["Flux.2 Klein"], defaults: { steps: 4, cfg: 1, width: 1024, height: 1024 }, nodes: [], requires: [], notes: "", ready: true, missing: [] },
    { key: "sdxl", name: "SDXL / Pony / Illustrious（NSFW 生态最大）", family: "sdxl", summary: "一体化 checkpoint", nsfw: "最成熟：Pony/Illustrious 系专为 NSFW 训练，LoRA 生态最大", base_models: ["Pony"], defaults: { steps: 8, cfg: 1.5, width: 832, height: 1216 }, nodes: [], requires: [], notes: "", ready: true, missing: [] },
  ],
  current: { unet: "flux-2-klein-9b-fp8.safetensors", text_encoder: "qwen_3_8b_fp8mixed.safetensors", vae: "full_encoder_small_decoder.safetensors", sampler: "euler", enhancer: "Qwen3.8-27B 本地", backend: "ComfyUI 127.0.0.1:8188", pipeline: "flux2", pipeline_name: "Flux.2 Klein 9B" },
};
const makeJob = (overrides = {}) => ({
  id: "img-1", state: "running", phase: "sampling", prompt: "一只戴帽子的猫", negative_prompt: "模糊",
  final_prompt: "an orange cat wearing a hat", enhanced: true,
  params: { width: 768, height: 512, steps: 8, cfg: 1.5, denoise: 0.8, seed: 424242 },
  step: 3, max_step: 8, progress: 0.375, queue_position: 0, queue_running: 1, queue_pending: 2,
  preview, prompt_id: "prompt-1", images: [], error: null, references: 0,
  created: Date.now() / 1000 - 20, started: Date.now() / 1000 - 18, finished: null,
  elapsed: 18, total_seconds: 20, step_seconds: 2.5, steps_per_second: 0.4, eta_seconds: 12.5,
  estimate_calibrated: false, timings: {}, events: [{ at: Date.now() / 1000, key: "sampling", detail: "" }],
  model: { unet: "flux-2-klein-9b-fp8.safetensors", text_encoder: "qwen_3_8b_fp8mixed.safetensors", vae: "full_encoder_small_decoder.safetensors", sampler: "euler" },
  ...overrides,
});
const historyEntry = (overrides = {}) => ({
  id: "img-1", time: Date.now() / 1000, state: "completed", prompt: "一只戴帽子的猫", negative_prompt: "模糊",
  final_prompt: "an orange cat wearing a hat", enhanced: true,
  params: { width: 768, height: 512, steps: 8, cfg: 1.5, denoise: 0.8, seed: 424242 },
  references: [], images: [png], prompt_id: "prompt-1", error: null,
  timings: { prompt: 4.2, load: 12.5, sampling: 20, restore: 9, total: 46 },
  duration: { total: 46, sampling: 20 }, steps_per_second: 0.4, model: meta.current,
  ...overrides,
});
// Editable workflow template mirroring the gateway's /local/image/workflow.
// Like the gateway, the graph view reflects the parameters the panel sent, and
// saved edits take precedence over them.
const sdxlTemplate = (overrides = {}, params = {}, running = []) => {
  const value = (node, input, fallback, param) => {
    const stored = overrides[node]?.[input];
    if (stored !== undefined && stored !== null && stored !== "") return String(stored);
    const fromParams = param && params[param] !== undefined ? params[param] : null;
    return fromParams !== null ? String(fromParams) : String(fallback);
  };
  return {
    pipeline: "sdxl",
    pipelines: [],
    template: [
      { id: "10", class_type: "CheckpointLoaderSimple", title: "主模型", fields: [{ input: "ckpt_name", label: "模型文件", type: "text", choices: "checkpoints", value: overrides["10"]?.ckpt_name ?? "CyberRealisticPony_V18.0_F16.safetensors" }] },
      { id: "11", class_type: "LoraLoader", title: "LoRA 1", fields: [
        { input: "lora_name", label: "LoRA 文件", type: "text", choices: "loras", value: overrides["11"]?.lora_name ?? "None" },
        { input: "strength_model", label: "模型权重", type: "float", min: -2, max: 2, step: 0.05, value: overrides["11"]?.strength_model ?? 1.0 }] },
      { id: "12", class_type: "LoraLoader", title: "LoRA 2", fields: [{ input: "lora_name", label: "LoRA 文件", type: "text", choices: "loras", value: overrides["12"]?.lora_name ?? "None" }] },
      { id: "1", class_type: "CLIPTextEncode", title: "正向提示词", fields: [{ input: "text", label: "正向提示词", type: "text", value: "preview" }] },
      { id: "2", class_type: "CLIPTextEncode", title: "负面提示词", fields: [{ input: "text", label: "负面提示词", type: "text", value: "lowres, bad anatomy" }] },
      { id: "3", class_type: "EmptyLatentImage", title: "画布尺寸", fields: [
        { input: "width", label: "宽度", type: "int", min: 64, max: 4096, step: 64, param: "width", value: value("3", "width", 832, "width") },
        { input: "height", label: "高度", type: "int", min: 64, max: 4096, step: 64, param: "height", value: value("3", "height", 1216, "height") }] },
      { id: "4", class_type: "KSampler", title: "采样", fields: [
        { input: "seed", label: "种子", type: "int", param: "seed", value: value("4", "seed", 1, "seed") },
        { input: "steps", label: "步数", type: "int", min: 1, max: 200, param: "steps", value: value("4", "steps", 28, "steps") },
        { input: "cfg", label: "CFG", type: "float", min: 0, max: 100, step: 0.1, param: "cfg", value: value("4", "cfg", 6, "cfg") },
        { input: "sampler_name", label: "采样器", type: "text", choices: "samplers", value: overrides["4"]?.sampler_name ?? "dpmpp_2m" },
        { input: "scheduler", label: "调度器", type: "text", choices: "schedulers", value: overrides["4"]?.scheduler ?? "karras" },
        { input: "denoise", label: "重绘强度", type: "float", min: 0, max: 1, step: 0.05, param: "denoise", value: 1 }] },
      { id: "5", class_type: "VAEDecode", title: "解码", fields: [] },
      { id: "6", class_type: "SaveImage", title: "保存图片", fields: [{ input: "filename_prefix", label: "文件名前缀", type: "text", value: "qwen/generated" }] },
    ],
    sync: {
      width: { node: "3", input: "width", title: "画布尺寸", label: "宽度" },
      height: { node: "3", input: "height", title: "画布尺寸", label: "高度" },
      seed: { node: "4", input: "seed", title: "采样", label: "种子" },
      steps: { node: "4", input: "steps", title: "采样", label: "步数" },
      cfg: { node: "4", input: "cfg", title: "采样", label: "CFG" },
    },
    graph: [{ id: "10", class_type: "CheckpointLoaderSimple", title: "主模型", inputs: { ckpt_name: "CyberRealisticPony_V18.0_F16.safetensors" } }],
    running,
    overrides,
    presets: presetsPayload(overrides),
    preset_slots: { speed: "11", quality: "12" },
    models: {
      checkpoints: ["CyberRealisticPony_V18.0_F16.safetensors", "sam3.1_multiplex_fp16.safetensors"],
      loras: ["detail/DetailedPerfection.safetensors", "speed/DMD2_Speed.safetensors"],
    },
    default_negative: "lowres, bad anatomy",
    workflow_file: "D:/AI/Qwen-Local/logs/image-workflow.json",
  };
};

const makeWorkflow = (overrides = {}, params = {}, running = []) => {
  const value = (node, input, fallback, param) => {
    const stored = overrides[node]?.[input];
    if (stored !== undefined && stored !== null && stored !== "") return String(stored);
    const fromParams = param && params[param] !== undefined ? params[param] : null;
    return fromParams !== null ? String(fromParams) : String(fallback);
  };
  return {
  template: [
    { id: "1", class_type: "UNETLoader", title: "采样模型", fields: [{ input: "unet_name", label: "模型文件", type: "text", value: overrides["1"]?.unet_name ?? "flux-2-klein-9b-fp8.safetensors" }] },
    { id: "2", class_type: "CLIPLoader", title: "文本编码器", fields: [{ input: "clip_name", label: "编码器文件", type: "text", value: "qwen_3_8b_fp8mixed.safetensors" }] },
    { id: "3", class_type: "VAELoader", title: "VAE", fields: [{ input: "vae_name", label: "VAE 文件", type: "text", value: "full_encoder_small_decoder.safetensors" }] },
    { id: "6", class_type: "EmptyFlux2LatentImage", title: "画布尺寸", fields: [
      { input: "width", label: "宽度", type: "int", min: 64, max: 4096, step: 64, param: "width", value: value("6", "width", 1024, "width") },
      { input: "height", label: "高度", type: "int", min: 64, max: 4096, step: 64, param: "height", value: value("6", "height", 1024, "height") }] },
    { id: "7", class_type: "RandomNoise", title: "种子", fields: [{ input: "noise_seed", label: "种子", type: "int", param: "seed", value: value("7", "noise_seed", 42, "seed") }] },
    { id: "8", class_type: "KSamplerSelect", title: "采样器", fields: [{ input: "sampler_name", label: "采样器", type: "text", value: overrides["8"]?.sampler_name ?? "euler" }] },
    { id: "9", class_type: "Flux2Scheduler", title: "步数", fields: [{ input: "steps", label: "步数", type: "int", min: 1, max: 200, param: "steps", value: value("9", "steps", 4, "steps") }] },
    { id: "10", class_type: "CFGGuider", title: "提示词引导", fields: [{ input: "cfg", label: "CFG", type: "float", min: 0, max: 100, step: 0.1, param: "cfg", value: value("10", "cfg", 1, "cfg") }] },
    { id: "11", class_type: "SamplerCustomAdvanced", title: "采样", fields: [] },
    { id: "12", class_type: "VAEDecode", title: "解码", fields: [] },
    { id: "13", class_type: "SaveImage", title: "保存图片", fields: [{ input: "filename_prefix", label: "文件名前缀", type: "text", value: "flux2-klein/generated" }] },
  ],
  sync: {
    width: { node: "6", input: "width", title: "画布尺寸", label: "宽度" },
    height: { node: "6", input: "height", title: "画布尺寸", label: "高度" },
    seed: { node: "7", input: "noise_seed", title: "种子", label: "种子" },
    steps: { node: "9", input: "steps", title: "步数", label: "步数" },
    cfg: { node: "10", input: "cfg", title: "提示词引导", label: "CFG" },
  },
  graph: [
    { id: "1", class_type: "UNETLoader", title: "Flux.2 Klein 9B", inputs: { unet_name: overrides["1"]?.unet_name || "flux-2-klein-9b-fp8.safetensors" } },
    { id: "9", class_type: "Flux2Scheduler", title: "步数", inputs: { steps: Number(value("9", "steps", 4, "steps")) } },
  ],
  running,
  overrides,
  presets: [],
  preset_slots: {},
  models: {
    checkpoints: ["flux-2-klein-9b-fp8.safetensors"],
    diffusion_models: ["flux-2-klein-9b-fp8.safetensors"],
    loras: ["detail/DetailedPerfection.safetensors"],
  },
  default_negative: "lowres, bad anatomy",
  workflow_file: "D:/AI/Qwen-Local/logs/image-workflow.json",
  };
};

// One-click LoRA presets, mirroring the gateway's /local/image/presets payload:
// availability comes from the files on disk, active state from the saved edits.
const workflowCatalogue = [
  { key: "portrait", label: "写实人像 · 推荐", pipeline: "sdxl", presets: ["lcm", "photoreal"],
    params: { steps: 8, cfg: 1.5, denoise: 1, width: 832, height: 1216 }, references: true, available: true,
    summary: "Pony fp16 + LCM 8 步 + 照片级写实滑杆 1.5：约 40 秒一张。", detail: "本机默认配方。" },
  { key: "draft", label: "极速试稿", pipeline: "sdxl", presets: ["dmd2"],
    params: { steps: 4, cfg: 1, denoise: 1, width: 832, height: 1216 }, references: true, available: true,
    summary: "DMD2 4 步：最快出草图。", detail: "细节偏软。" },
  { key: "flux2", label: "Flux.2 构图", pipeline: "flux2", presets: [],
    params: { steps: 4, cfg: 1, denoise: 1, width: 1024, height: 1024 }, references: false, available: true,
    summary: "原 Klein 管线：构图更强，负面词无效。", detail: "" },
  { key: "upscale_sharp", label: "放大 · 清晰度最佳 1.25×", pipeline: "seedvr2", presets: [],
    params: { scale: 1.25, steps: 3, cfg: 1 }, references: true, mode: "image", kind: "upscale", available: true,
    summary: "1.25× 三 步：细节最锐，皮肤噪点比 1.5× 少两成。", detail: "清晰度最高。" },
  { key: "video_upscale_clean", label: "视频放大 · 噪点最少 1.0×", pipeline: "seedvr2_video", presets: [],
    params: { scale: 1, steps: 3, cfg: 1 }, references: true, mode: "video", available: true,
    summary: "视频不放大，只修实。", detail: "" },
  { key: "chroma", label: "Chroma 不审查", pipeline: "chroma", presets: [],
    params: { steps: 26, cfg: 4, denoise: 1, width: 1024, height: 1024 }, references: true, available: false,
    summary: "Chroma1-HD，需要先下载 17.8 GB 权重。", detail: "" },
];

const presetCatalogue = [
  { key: "lcm", label: "LCM-LoRA · 8 步", lora: "lcm-lora-sdxl.safetensors", role: "speed", strength: 1,
    note: "Latent ConsistencyLatent Consistency：4–8 步、CFG 1–2。", params: { steps: 8, cfg: 1.5, sampler: "lcm", scheduler: "sgm_uniform", denoise: 1 }, available: true },
  { key: "hyper8", label: "Hyper-SDXL · 8 步", lora: "Hyper-SDXL-8steps-CFG-lora.safetensors", role: "speed", strength: 1,
    note: "Hyper-SD 8 Hyper-SD 8 步 CFG 版。", params: { steps: 8, cfg: 5, sampler: "dpmpp_sde", scheduler: "karras", denoise: 1 }, available: false },
  { key: "face", label: "面部结构修复", lora: "face_xl_v0_1.safetensors", role: "quality", strength: 1,
    note: "face helperface helper：改善五官结构。", params: {}, available: true },
  { key: "dmd2", label: "DMD2 · 4 步", lora: "dmd2_sdxl_4step_lora_fp16.safetensors", role: "speed", strength: 1,
    note: "DMD2 分布匹配蒸馏：4 步、CFG 1。", params: { steps: 4, cfg: 1, sampler: "lcm", scheduler: "sgm_uniform", denoise: 1 }, available: true },
  { key: "photoreal", label: "照片级写实滑杆", lora: "sdxl_photorealistic_slider_v1-0.safetensors", role: "quality", strength: 1.5,
    note: "Ostris 写实滑杆：推向真照片质感。", params: {}, available: true },
];
const presetLoras = stored => [stored["11"]?.lora_name, stored["12"]?.lora_name].filter(Boolean);
const presetsPayload = (stored = {}) => presetCatalogue.map(preset => ({ ...preset, active: presetLoras(stored).includes(preset.lora) }));
const slotOfRole = role => (role === "speed" ? "11" : "12");

async function run() {
  const browser = await chromium.launch({ headless: true, channel: "msedge" });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await context.newPage();
    const pageErrors = [];
    const submits = [];
    let status = textStatus;
    let job = null;
    let busy = false;
    let history = [];
    let progressPolls = 0;
    let metaRequests = 0;
    let overrides = {};
    const workflowSaves = [];
    const workflowResets = [];
    const workflowReads = [];
    const pipelineSelections = [];
    const presetCalls = [];
    const workflowSwitches = [];
    const gpu = { total_mib: 12227, used_mib: 10992, free_mib: 1235, utilization: 88, job_mib: 8600, job_peak_mib: 9120, image_mib: 4300, image_peak_mib: 8600, baseline_mib: 2392, released_baseline_mib: 500, samples: 12, peak_mib: 11512 };
    page.on("pageerror", error => pageErrors.push(error.message));
    page.on("dialog", dialog => dialog.accept());
    const serve = (route, file, contentType) => route.fulfill({ path: path.join(root, file), contentType });
    await page.route("**/local/mode-control.js*", route => serve(route, "mode-control.js", "text/javascript"));
    await page.route("**/local/mode-control.css*", route => serve(route, "mode-control.css", "text/css"));
    await page.route("**/local/image-panel.js*", route => serve(route, "image-panel.js", "text/javascript"));
    await page.route("**/local/image-panel.css*", route => serve(route, "image-panel.css", "text/css"));
    await page.route("**/sw.js", route => serve(route, "sw.js", "application/javascript"));
    await page.route("**/local/status", route => route.fulfill({ json: status }));
    await page.route("**/local/mode", route => {
      status = route.request().postDataJSON().mode === "generate" ? imageStatus : textStatus;
      return route.fulfill({ status: 202, json: status });
    });
    await page.route("**/local/image/pipeline", route => {
      const key = route.request().postDataJSON().pipeline;
      pipelineSelections.push({ pipeline: key });
      const pipeline = meta.pipelines.find(entry => entry.key === key);
      meta.current = {
        ...meta.current,
        pipeline: key,
        pipeline_name: pipeline?.name || key,
        defaults: pipeline?.defaults || {},
        unet: key === "sdxl" ? "CyberRealisticPony_V18.0_F16.safetensors" : "flux-2-klein-9b-fp8.safetensors",
      };
      return route.fulfill({ json: { pipeline: key, model: meta.current } });
    });    await page.route("**/local/image/meta", route => { metaRequests += 1; return route.fulfill({ json: meta }); });
    await page.route("**/local/image/gpu*", route => route.fulfill({ json: { gpu, available: true, processes: [] } }));
    await page.route("**/local/image/workflow*", route => {
      if ((new URL(route.request().url()).searchParams.get("pipeline") || "") === "seedvr2") {
        return route.fulfill({ json: upscalePayload() });
      }
      const url = new URL(route.request().url());
      const params = Object.fromEntries([...url.searchParams.entries()].map(([key, value]) => [key, value]));
      const pipeline = url.searchParams.get("pipeline") || meta.current.pipeline || "flux2";
      // The gateway reports the workflow that is executing while a job runs.
      const running = busy && job?.graph ? job.graph : [];
      const build = (stored, merged) => pipeline === "sdxl"
        ? sdxlTemplate(stored, merged, running)
        : makeWorkflow(stored, merged, running);
      if (route.request().method() === "PUT") {
        const body = route.request().postDataJSON();
        workflowSaves.push(body);
        overrides = body.overrides;
        // The gateway returns the persisted edits with the request parameters
        // applied, exactly like the real endpoint.
        return route.fulfill({ json: { pipeline, overrides, workflow: build(overrides, { ...params, ...body.params }) } });
      }
      if (route.request().method() === "DELETE") {
        overrides = {};
        workflowResets.push(true);
        return route.fulfill({ json: { pipeline, overrides: {}, workflow: build({}, params) } });
      }
      workflowReads.push(route.request().url());
      return route.fulfill({ json: build(overrides, params) });
    });
    const upscalePayload = () => ({
    pipeline: "seedvr2", pipeline_name: "SeedVR2 放大 / 修复", kind: "upscale",
    references_required: true, summary: "SeedVR2 3B int8 单步恢复",
    defaults: { steps: 2, cfg: 1, scale: 1.5 },
    presets: presetsPayload({}), preset_slots: { speed: "11", quality: "12" },
    sync: { scale: { node: "3", input: "resize_type.multiplier", title: "放大倍率", label: "放大倍率" },
            steps: { node: "10", input: "steps" }, cfg: { node: "10", input: "cfg" },
            seed: { node: "10", input: "seed" } },
    overrides: {}, models: {}, default_negative: "", template: [], graph: [], running: [],
  });
  await page.route("**/local/image/workflows", route => {
      const activePresets = [overrides["11"]?.lora_name, overrides["12"]?.lora_name].filter(Boolean);
      const payload = workflowCatalogue.map(workflow => ({
        ...workflow,
        pipeline_name: workflow.pipeline,
        presets_available: true,
        active: workflow.pipeline === meta.current.pipeline
          && workflow.presets.length === activePresets.length
          && workflow.presets.every((key, index) => presetCatalogue.find(item => item.key === key)?.lora === activePresets[index]),
      }));
      if (route.request().method() !== "POST") return route.fulfill({ json: { workflows: payload, current: meta.current.pipeline } });
      const key = route.request().postDataJSON().key;
      const chosen = workflowCatalogue.find(item => item.key === key);
      workflowSwitches.push(key);
      if (chosen.pipeline !== meta.current.pipeline) {
        meta.current = { ...meta.current, pipeline: chosen.pipeline, defaults: chosen.pipeline === "sdxl" ? { steps: 28, cfg: 6, width: 832, height: 1216 } : { steps: 4, cfg: 1, width: 1024, height: 1024 } };
      }
      overrides = {};
      for (const presetKey of chosen.presets) {
        const preset = presetCatalogue.find(item => item.key === presetKey);
        overrides[slotOfRole(preset.role)] = { lora_name: preset.lora, strength_model: preset.strength, strength_clip: preset.strength };
      }
      if (chosen.pipeline === "seedvr2") {
        meta.current = { ...meta.current, pipeline: "seedvr2",
                         defaults: { steps: 2, cfg: 1, width: 0, height: 0, scale: 1.5 } };
      }
      const rebuild = () => (chosen.pipeline === "seedvr2" ? upscalePayload()
        : (chosen.pipeline === "sdxl" ? sdxlTemplate : makeWorkflow)(overrides, {}, []));
      return route.fulfill({ json: {
        workflow: { ...chosen, active: true }, pipeline: chosen.pipeline, presets: chosen.presets,
        params: chosen.params, model: meta.current, workflows: payload,
        workflow_payload: rebuild(),
      } });
    });
    await page.route("**/local/image/presets*", route => {
      // Mirrors the gateway: a speed preset owns slot 11 and the sampler, a
      // quality preset owns slot 12, and clearing only drops preset files.
      const pipeline = meta.current.pipeline || "flux2";
      const rebuild = () => (pipeline === "sdxl" ? sdxlTemplate : makeWorkflow)(overrides, {}, busy && job?.graph ? job.graph : []);
      if (route.request().method() === "DELETE") {
        for (const slot of ["11", "12"]) {
          if (presetCatalogue.some(preset => preset.lora === overrides[slot]?.lora_name)) delete overrides[slot];
        }
        // An accelerator owns its sampler pair; clearing takes those back too,
        // unless the stored value is something the user chose by hand.
        for (const name of ["sampler_name", "scheduler"]) {
          const proposed = presetCatalogue.filter(preset => preset.role === "speed")
            .map(preset => preset.params[name === "sampler_name" ? "sampler" : "scheduler"]);
          if (proposed.includes(overrides["4"]?.[name])) delete overrides["4"][name];
        }
        presetCalls.push({ action: "clear", pipeline });
        return route.fulfill({ json: { pipeline, cleared: "all", overrides, params: { steps: 8, cfg: 1.5, denoise: 1 }, presets: presetsPayload(overrides), workflow: rebuild() } });
      }
      const key = route.request().postDataJSON().key;
      const preset = presetCatalogue.find(entry => entry.key === key);
      presetCalls.push({ action: "apply", key, pipeline });
      for (const slot of ["11", "12"]) {
        const current = presetCatalogue.find(entry => entry.lora === overrides[slot]?.lora_name);
        if (current && current.role === preset.role) delete overrides[slot];
      }
      const slot = slotOfRole(preset.role);
      overrides[slot] = { lora_name: preset.lora, strength_model: preset.strength, strength_clip: preset.strength };
      overrides["4"] = { ...(overrides["4"] || {}), sampler_name: preset.params.sampler, scheduler: preset.params.scheduler };
      return route.fulfill({ json: { pipeline, applied: { ...preset, active: true }, params: preset.params, overrides, presets: presetsPayload(overrides), workflow: rebuild() } });
    });
    await page.route("**/local/image/history", route => {
      if (route.request().method() === "DELETE") {
        history = [];
        return route.fulfill({ json: { removed: 1, entries: [] } });
      }
      return route.fulfill({ json: { entries: history } });
    });
    await page.route("**/local/image/history/*", route => {
      const id = route.request().url().split("/").pop();
      history = history.filter(entry => entry.id !== id);
      return route.fulfill({ json: { removed: id } });
    });
    await page.route("**/local/image/progress*", route => {
      progressPolls += 1;
      if (job && busy && progressPolls > 1) job = makeJob({ step: 6, progress: 0.75, elapsed: 26, eta_seconds: 5, steps_per_second: 0.43 });
      return route.fulfill({ json: { job, busy, since: 0, events: job?.events || [], event_count: job?.events?.length || 0, model: meta.current } });
    });
    await page.route("**/local/image/generate", route => {
      submits.push(route.request().postDataJSON());
      job = makeJob({ phase: "queued", step: 0, progress: 0, preview: null, eta_seconds: null, steps_per_second: null });
      busy = true;
      progressPolls = 0;
      return route.fulfill({ status: 202, json: { job } });
    });
    await page.route("**/local/image/cancel", route => {
      busy = false;
      job = makeJob({ state: "cancelled", phase: "cancelled", progress: 0.5, error: "已取消" });
      return route.fulfill({ json: { job } });
    });
    await page.route(origin + "/**", async route => {
      if (route.request().resourceType() !== "document") return route.fallback();
      return route.fulfill({ contentType: "text/html", body: fixture.replace("</head>", injected + "</head>") });
    });

    await page.goto(origin + "/");
    await page.locator(".conversation-chat-form textarea").waitFor();
    assert.equal(await page.locator(".qg-view").isVisible(), false, "generate view stays hidden in text mode");
    assert.equal(await page.locator(".chat-screen").isVisible(), true, "the chat screen is visible in text mode");

    // --- entering generate mode hides the chat --------------------------------
    await page.locator('.local-mode-button[data-mode="generate"]').click();
    await page.locator(".qg-view:not([hidden])").waitFor({ timeout: 15000 });
    // The model card is filled asynchronously from /local/image/meta.
    await page.locator('.qg-model-value >> text=flux-2-klein-9b-fp8.safetensors').first().waitFor({ timeout: 15000 });
    const modeState = await page.evaluate(() => ({
      chatFormHidden: getComputedStyle(document.querySelector(".conversation-chat-form")).display === "none",
      chatScreenHidden: getComputedStyle(document.querySelector(".chat-screen")).display === "none",
      panelTop: Math.round(document.querySelector(".qg-view").getBoundingClientRect().top),
      toolbarBottom: Math.round(document.querySelector("#local-mode-toolbar").getBoundingClientRect().bottom),
      modelRows: [...document.querySelectorAll(".qg-model-value")].map(node => node.textContent),
      queue: document.querySelector('.qg-metric[data-metric="queue"] .qg-metric-value').textContent,
      badgeOnline: document.querySelector(".qg-badge-state").dataset.online,
    }));
    assert.equal(modeState.chatFormHidden, true, "the chat composer is hidden in generate mode");
    assert.equal(modeState.chatScreenHidden, true, "the chat message area is hidden in generate mode");
    assert.ok(modeState.panelTop >= modeState.toolbarBottom - 1, "the panel starts below the toolbar");
    assert.equal(modeState.modelRows[0], "flux-2-klein-9b-fp8.safetensors", `the sampling model is shown (meta requests: ${metaRequests})`);
    assert.ok(modeState.modelRows.includes("Qwen3.8-27B 本地"), "the prompt enhancer is shown");
    assert.equal(modeState.badgeOnline, "true", "ComfyUI connectivity is shown");

    // --- parameters, random seed and Ctrl+R -----------------------------------
    const seed = page.locator('.qg-field-input[name="seed"]');
    // Entering the view always starts from a fresh seed, never the -1 placeholder.
    const entered = Number(await seed.inputValue());
    assert.ok(Number.isInteger(entered) && entered >= 0 && entered <= 2147483647, `entering rolls a real seed, got ${entered}`);
    assert.notEqual(entered, -1, "the placeholder -1 is never what a generation would use");
    await page.locator(".qg-button-seed").click();
    const first = Number(await seed.inputValue());
    assert.notEqual(first, entered, "the random button rolls a new seed");
    await seed.fill("7");
    await seed.press("Control+r");
    const second = Number(await seed.inputValue());
    assert.ok(first >= 0 && first <= 2147483647, `random seed in range, got ${first}`);
    assert.ok(second >= 0 && second !== 7, `Ctrl+R randomizes the seed, got ${second}`);
    await page.locator(".qg-link-button").click();
    const reset = Number(await seed.inputValue());
    assert.ok(reset >= 0 && reset <= 2147483647, `restore defaults also rolls a seed, got ${reset}`);
    assert.equal(await page.locator('.qg-field-input[name="width"]').inputValue(), "1024", "restore defaults resets the width");

    // Leaving and re-entering rolls again, so repeated visits cannot silently
    // reproduce the same picture.
    await seed.fill("123456");
    await page.locator('.local-mode-button[data-mode="text"]').click();
    await page.locator(".qg-view").waitFor({ state: "hidden", timeout: 15000 });
    await page.locator('.local-mode-button[data-mode="generate"]').click();
    await page.locator(".qg-view:not([hidden])").waitFor({ timeout: 15000 });
    const reentered = Number(await seed.inputValue());
    assert.ok(reentered >= 0 && reentered <= 2147483647, `re-entering rolls a seed, got ${reentered}`);
    assert.notEqual(reentered, 123456, "re-entering replaces the previous seed");
    assert.ok(await page.locator(".qg-status-line").first().innerText().then(text => text.length > 0), "the new seed is reported on screen");

    // --- pipeline switching (Flux.2 -> SDXL/Pony) -----------------------------
    const pipelines = await page.evaluate(() => ({
      options: [...document.querySelectorAll(".qg-pipeline-select option")].map(option => ({ key: option.value, label: option.textContent })),
      selected: document.querySelector(".qg-pipeline-select").value,
      note: document.querySelector(".qg-pipeline-note").textContent,
      nsfw: document.querySelector(".qg-pipeline-note").dataset.nsfw,
    }));
    assert.equal(pipelines.options.length, 2, "both pipelines are offered");
    assert.equal(pipelines.selected, "flux2", "the current pipeline is selected");
    assert.ok(pipelines.note.includes("NSFW"), `the note explains the NSFW situation: ${pipelines.note}`);

    await page.locator(".qg-pipeline-select").selectOption("sdxl");
    await page.waitForFunction(() => document.querySelector(".qg-pipeline-select").value === "sdxl");
    assert.equal(pipelineSelections.length, 1, "switching posts the choice once");
    assert.equal(pipelineSelections[0].pipeline, "sdxl");
    await page.waitForFunction(() => document.querySelectorAll(".qg-model-value")[0]
      && document.querySelectorAll(".qg-model-value")[0].textContent === "CyberRealisticPony_V18.0_F16.safetensors");
    const sdxlState = await page.evaluate(() => ({
      note: document.querySelector(".qg-pipeline-note").textContent,
      nsfw: document.querySelector(".qg-pipeline-note").dataset.nsfw,
      model: document.querySelectorAll(".qg-model-value")[0].textContent,
      steps: document.querySelector('.qg-field-input[name="steps"]').value,
      cfg: document.querySelector('.qg-field-input[name="cfg"]').value,
      width: document.querySelector('.qg-field-input[name="width"]').value,
      height: document.querySelector('.qg-field-input[name="height"]').value,
    }));
    assert.equal(sdxlState.nsfw, "true", "the SDXL pipeline is marked as the mature NSFW option");
    assert.ok(sdxlState.note.includes("最成熟"), `the note states the NSFW advantage: ${sdxlState.note}`);
    assert.equal(sdxlState.model, "CyberRealisticPony_V18.0_F16.safetensors", "the model card shows the resolved checkpoint");
    // The default recipe is 8 steps / CFG 1.5 on an 832x1216 canvas.
    assert.equal(sdxlState.steps, "8", "the pipeline's default recipe reaches the composer");
    assert.equal(sdxlState.cfg, "1.5");
    assert.equal(sdxlState.width, "832", "SDXL canvas defaults reach the composer");
    assert.equal(sdxlState.height, "1216");

    // --- one-click accelerator LoRAs ------------------------------------------
    await page.waitForFunction(() => document.querySelectorAll(".qg-preset-chip").length > 0);
    const chipState = await page.evaluate(() => ({
      roles: [...document.querySelectorAll(".qg-preset-role")].map(node => node.textContent),
      chips: [...document.querySelectorAll(".qg-preset-chip")].map(chip => ({
        label: chip.textContent, available: chip.dataset.available, active: chip.dataset.active, disabled: chip.disabled,
      })),
      hidden: document.querySelector(".qg-presets").hidden,
    }));
    assert.deepEqual(chipState.roles, ["加速", "画质"], "chips are grouped into speed and quality rows");
    assert.equal(chipState.hidden, false, "the preset bar is shown for SDXL");
    assert.equal(chipState.chips.length, 5, "every preset the pipeline can run is offered");
    assert.equal(chipState.chips[1].available, "false", "a preset whose LoRA is not downloaded is marked");
    assert.equal(chipState.chips[1].disabled, true, "an unavailable preset cannot be applied");

    await page.locator(".qg-preset-chip", { hasText: "LCM-LoRA" }).click();
    await page.waitForFunction(() => document.querySelector(".qg-preset-chip[data-active='true']"));
    const applied = await page.evaluate(() => ({
      steps: document.querySelector('.qg-field-input[name="steps"]').value,
      cfg: document.querySelector('.qg-field-input[name="cfg"]').value,
      active: [...document.querySelectorAll(".qg-preset-chip[data-active='true']")].map(chip => chip.textContent),
      hint: document.querySelector(".qg-preset-hint").textContent,
    }));
    assert.equal(applied.steps, "8", "the accelerator sets its own step count");
    assert.equal(applied.cfg, "1.5", "the accelerator sets the CFG it converges at");
    assert.equal(overrides["4"].sampler_name, "lcm", "the sampler is written for the accelerator");
    assert.equal(overrides["4"].scheduler, "sgm_uniform", "so is the scheduler");
    assert.equal(overrides["11"].lora_name, "lcm-lora-sdxl.safetensors", "the LoRA lands in the first slot");
    assert.equal(applied.active.length, 1, "exactly one chip reports itself active");
    assert.ok(applied.hint.includes("LCM-LoRA"), `the hint names the active preset: ${applied.hint}`);
    assert.deepEqual(presetCalls.map(call => call.action), ["apply"], "applying posts once, with no manual save");

    // A quality preset stacks into the second slot without touching the steps.
    await page.locator(".qg-preset-chip", { hasText: "面部结构修复" }).click();
    await page.waitForFunction(() => document.querySelectorAll(".qg-preset-chip[data-active='true']").length === 2);
    const stacked = await page.evaluate(() => ({
      steps: document.querySelector('.qg-field-input[name="steps"]').value,
    }));
    assert.equal(overrides["11"].lora_name, "lcm-lora-sdxl.safetensors", "the accelerator keeps the first slot");
    assert.equal(overrides["12"].lora_name, "face_xl_v0_1.safetensors", "the quality LoRA stacks into the second slot");
    assert.equal(stacked.steps, "8", "a quality LoRA leaves the step count alone");

    // A hand-edited step count would wreck an accelerated run, so the hint turns
    // into a warning until the preset's own settings are back.
    assert.equal(await page.locator(".qg-preset-hint").getAttribute("data-warning"), null,
                 "matching settings raise no warning");
    await page.locator('.qg-field-input[name="steps"]').fill("28");
    await page.waitForFunction(() => document.querySelector(".qg-preset-hint").dataset.warning === "true");
    const warned = await page.evaluate(() => ({
      text: document.querySelector(".qg-preset-hint").textContent,
      color: getComputedStyle(document.querySelector(".qg-preset-warning-text")).fontWeight,
      button: document.querySelector(".qg-preset-restore")?.textContent || null,
      title: document.querySelector(".qg-preset-hint").title,
    }));
    assert.ok(warned.text.includes("28") && warned.text.includes("8"), `the warning names both values: ${warned.text}`);
    assert.ok(Number(warned.color) >= 600, "the warning is emphasised");
    assert.equal(warned.button, "恢复推荐值", "the warning carries its own fix");
    assert.ok(warned.title.includes("CFG"), `the tooltip explains what the LoRA needs: ${warned.title}`);

    // One click on the warning restores the accelerator's own recipe.
    await page.locator(".qg-preset-restore").click();
    await page.waitForFunction(() => document.querySelector(".qg-preset-hint").dataset.warning === undefined);
    assert.equal(await page.locator('.qg-field-input[name="steps"]').inputValue(), "8",
                 "恢复推荐值 writes the accelerator's step count back");
    assert.equal(await page.locator('.qg-field-input[name="cfg"]').inputValue(), "1.5");

    // Reload alignment: a pipeline whose defaults contradict the saved accelerator
    // must not start the panel off with a warning.
    meta.pipelines.find(pipeline => pipeline.key === "sdxl").defaults = { steps: 28, cfg: 6, width: 832, height: 1216 };
    await page.locator(".qg-pipeline-select").selectOption("flux2");
    await page.waitForTimeout(400);
    await page.locator(".qg-pipeline-select").selectOption("sdxl");
    await page.waitForFunction(() => document.querySelector(".qg-pipeline-select").value === "sdxl");
    await page.waitForTimeout(600);
    const aligned = await page.evaluate(() => ({
      steps: document.querySelector('.qg-field-input[name="steps"]').value,
      cfg: document.querySelector('.qg-field-input[name="cfg"]').value,
      warning: document.querySelector(".qg-preset-hint").dataset.warning === "true",
      text: document.querySelector(".qg-preset-hint").textContent,
    }));
    meta.pipelines.find(pipeline => pipeline.key === "sdxl").defaults = { steps: 8, cfg: 1.5, width: 832, height: 1216 };
    assert.equal(aligned.warning, false,
                 `a saved accelerator outranks the pipeline defaults: ${aligned.text}`);
    assert.equal(aligned.steps, "8", "the composer keeps the accelerator's step count");
    assert.equal(aligned.cfg, "1.5");
    assert.equal(await page.locator('.qg-field-input[name="steps"]').inputValue(), "8",
                 "re-applying the preset restores its step count");

    // --- one-click workflows ---------------------------------------------------
    // Recipes are disabled while a job runs, so the scenario starts from idle.
    busy = false;
    job = makeJob({ state: "completed", phase: "completed", progress: 1, images: [] });
    await page.waitForFunction(() => document.querySelectorAll(".qg-switch-chip").length > 0
      && !document.querySelector(".qg-switch-chip").disabled, null, { timeout: 15000 });
    const switchState = await page.evaluate(() => ({
      chips: [...document.querySelectorAll(".qg-switch-chip")].map(chip => ({
        label: chip.textContent, active: chip.dataset.active, available: chip.dataset.available, disabled: chip.disabled,
      })),
      note: document.querySelector(".qg-switch-note").textContent,
      title: document.querySelector(".qg-switch .qg-card-title").textContent,
    }));
    assert.equal(switchState.chips.length, 4,
                 "生成图片 offers the generation recipes (upscalers and video recipes stay out)");
    assert.equal(switchState.title, "一键工作流", "the switcher is its own card");
    assert.equal(switchState.chips.filter(chip => chip.active === "true").length, 0,
                 "a hand-tuned LoRA mix matches no recipe, so nothing is highlighted");
    const missing = switchState.chips.find(chip => chip.label.includes("Chroma"));
    assert.equal(missing.available, "false", "a recipe whose model is missing is marked");
    assert.equal(missing.disabled, true, "and cannot be clicked");
    assert.ok(switchState.note.includes("写实人像"), `the first recipe is described as a fallback: ${switchState.note}`);
    assert.ok(switchState.note.includes("40 秒"), "the description says what the recipe gives you");

    await page.locator(".qg-switch-chip", { hasText: "极速试稿" }).hover();
    await page.waitForFunction(() => document.querySelector(".qg-switch-note").textContent.includes("草图"));
    const hovered = await page.locator(".qg-switch-note").innerText();
    assert.ok(hovered.includes("DMD2"), `hovering explains the recipe: ${hovered}`);

    await page.locator(".qg-switch-chip", { hasText: "极速试稿" }).click();
    await page.waitForFunction(() => document.querySelector(".qg-switch-chip[data-active='true']")?.textContent.includes("极速"), null, { timeout: 8000 })
      .catch(async () => {
        const dump = await page.evaluate(() => ({
          chips: [...document.querySelectorAll(".qg-switch-chip")].map(chip => chip.textContent + ":" + chip.dataset.active + ":" + chip.disabled),
          note: document.querySelector(".qg-switch-note").textContent.slice(0, 40),
          steps: document.querySelector('.qg-field-input[name="steps"]').value,
          cfg: document.querySelector('.qg-field-input[name="cfg"]').value,
          denoise: document.querySelector('.qg-field-input[name="denoise"]').value,
          width: document.querySelector('.qg-field-input[name="width"]').value,
          pipeline: document.querySelector(".qg-pipeline-select").value,
          status: document.querySelector(".qg-status-line").textContent,
        }));
        throw new Error("draft chip never became active: " + JSON.stringify(dump));
      });
    const switched = await page.evaluate(() => ({
      steps: document.querySelector('.qg-field-input[name="steps"]').value,
      cfg: document.querySelector('.qg-field-input[name="cfg"]').value,
      note: document.querySelector(".qg-switch-note").textContent,
      status: document.querySelector(".qg-status-line").textContent,
    }));
    assert.deepEqual(workflowSwitches, ["draft"], "one click posts one switch");
    assert.equal(switched.steps, "4", "the recipe sets its step count");
    assert.equal(switched.cfg, "1", "and its CFG");
    assert.equal(overrides["11"].lora_name, "dmd2_sdxl_4step_lora_fp16.safetensors", "the accelerator LoRA is applied");
    assert.equal(overrides["12"], undefined, "recipes without a quality LoRA clear that slot");
    const sized = await page.evaluate(() => ({
      width: document.querySelector('.qg-field-input[name="width"]').value,
      height: document.querySelector('.qg-field-input[name="height"]').value,
      referencesHidden: document.querySelector(".qg-references").hidden,
    }));
    assert.equal(sized.width, "832", "the recipe rewrites the width input");
    assert.equal(sized.height, "1216", "the recipe rewrites the height input");
    assert.equal(sized.referencesHidden, false, "a recipe that supports references keeps the uploader");

    // A recipe without reference support hides the whole upload row.
    await page.locator(".qg-switch-chip", { hasText: "Flux.2 构图" }).click();
    await page.waitForFunction(() => document.querySelector(".qg-references").hidden === true);
    const hidden = await page.evaluate(() => ({
      width: document.querySelector('.qg-field-input[name="width"]').value,
      height: document.querySelector('.qg-field-input[name="height"]').value,
      note: document.querySelector(".qg-switch-note").textContent,
    }));
    assert.equal(hidden.width, "1024", "the flux2 recipe restores its own canvas");
    assert.equal(hidden.height, "1024");
    assert.ok(hidden.note.includes("Flux.2"), "its description is shown");

    await page.locator(".qg-switch-chip", { hasText: "写实人像" }).click();
    await page.waitForFunction(() => document.querySelector(".qg-references").hidden === false);
    assert.equal(await page.locator('.qg-field-input[name="width"]').inputValue(), "832");

    assert.ok(switched.note.includes("极速试稿"), "the description follows the switch");
    assert.ok(switched.status.includes("已切换工作流"), `the switch is reported: ${switched.status}`);

    // --- upscalers -------------------------------------------------------------
    // The scenario starts from a settled job so the composer is interactive.
    busy = false;
    job = makeJob({ state: "completed", phase: "completed", progress: 1, images: [] });
    await page.waitForFunction(() => !document.querySelector(".qg-composer .qg-actions .qg-button-primary").disabled,
                               null, { timeout: 15000 });
    assert.equal(await page.locator(".qg-switch-chip", { hasText: "视频放大" }).count(), 0,
                 "video recipes belong to the video mode and stay out of this panel");
    assert.equal(await page.locator(".qg-switch-chip", { hasText: "放大 · 清晰度最佳" }).count(), 0,
                 "the upscale recipes belong to the 超分 sub-module");
    const painter = await page.evaluate(() => ({
      promptHidden: document.querySelector(".qg-prompt").hidden,
      scaleHidden: document.querySelector('.qg-field-input[name="scale"]').closest(".qg-field").hidden,
      presetsHidden: document.querySelector(".qg-preset-card").hidden,
      button: document.querySelector(".qg-composer .qg-actions .qg-button-primary").textContent,
    }));
    assert.equal(painter.promptHidden, false, "生成图片 keeps the prompt box");
    assert.equal(painter.scaleHidden, true, "and hides the multiplier");
    assert.equal(painter.presetsHidden, false, "the LoRA presets belong here");

    await page.locator(".qg-submode", { hasText: "超分" }).click();
    await page.waitForFunction(() => document.querySelectorAll(".qg-switch-chip").length === 1
      && document.querySelector(".qg-switch-chip").textContent.includes("放大"), null, { timeout: 15000 });
    const upscaleRow = await page.evaluate(() => ({
      chips: [...document.querySelectorAll(".qg-switch-chip")].map(chip => chip.textContent.trim()),
      title: document.querySelector(".qg-switch .qg-card-title").textContent,
      presetsHidden: document.querySelector(".qg-preset-card").hidden,
      button: document.querySelector(".qg-composer .qg-actions .qg-button-primary").textContent,
      accepts: [...document.querySelectorAll(".qg-reference-input")].map(node => node.accept),
    }));
    assert.equal(upscaleRow.title, "超分配方", "the sub-module relabels the card");
    assert.equal(upscaleRow.presetsHidden, true, "LoRAs have no place in an upscale run");
    assert.equal(upscaleRow.button, "开始放大", "the button says what it does");
    assert.ok(upscaleRow.accepts.every(value => value.includes("image/") && !value.includes("video")),
              "the reference inputs take pictures only");
    await page.locator(".qg-switch-chip", { hasText: "放大 · 清晰度最佳" }).click();
    await page.waitForFunction(() => {
      const field = document.querySelector('.qg-field-input[name="scale"]');
      return field && field.value === "1.25" && !field.closest(".qg-field").hidden;
    });
    const upscaler = await page.evaluate(() => ({
      scale: document.querySelector('.qg-field-input[name="scale"]').value,
      promptHidden: document.querySelector(".qg-prompt").hidden,
      negativeHidden: document.querySelector(".qg-negative-input").hidden,
      hintShown: !document.querySelector(".qg-upscale-hint").hidden,
      hintText: document.querySelector(".qg-upscale-hint").textContent,
      enhanceHidden: document.querySelector(".qg-enhance").hidden,
    }));
    assert.equal(upscaler.promptHidden, true, "an upscaler needs no prompt, so the box is hidden");
    assert.equal(upscaler.negativeHidden, true, "and no negative prompt either");
    assert.equal(upscaler.enhanceHidden, true, "prompt rewriting is pointless here");
    assert.equal(upscaler.hintShown, true, "a hint explains what to do instead");
    assert.ok(upscaler.hintText.includes("参考图"), `the hint points at the reference box: ${upscaler.hintText}`);

    // Without an image the run is refused; with one it carries the multiplier.
    const submitsBefore = submits.length;
    await page.locator(".qg-composer .qg-actions .qg-button-primary").click();
    await page.waitForTimeout(300);
    assert.equal(submits.length, submitsBefore, "an upscale run without an image is not submitted");
    await page.locator(".qg-reference-input").first().setInputFiles({
      name: "reference.png", mimeType: "image/png",
      buffer: Buffer.from("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489", "hex"),
    });
    await page.waitForTimeout(200);
    await page.locator(".qg-composer .qg-actions .qg-button-primary").click();
    await page.waitForFunction(count => window.__submits > count, submitsBefore, { timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(600);
    assert.equal(submits.length, submitsBefore + 1, "the run is submitted once an image is chosen");
    const upscaleSubmit = submits[submits.length - 1];
    assert.equal(upscaleSubmit.scale, 1.25, "the recipe's multiplier travels with the request");
    assert.equal(upscaleSubmit.steps, 3, "so do its steps");
    assert.equal(upscaleSubmit.pipeline, undefined, "the pipeline is left to the gateway's current one");
    assert.ok(upscaleSubmit.image_1, "the reference image is sent");

    // Settle the mocked job so the rest of the run has an idle panel again.
    busy = false;
    job = makeJob({ state: "completed", phase: "completed", progress: 1, images: [] });
    await page.waitForFunction(() => document.querySelectorAll(".qg-switch-chip").length > 0
      && !document.querySelector(".qg-switch-chip").disabled, null, { timeout: 15000 });

    // Back to 生成图片, which also restores a generation recipe.
    await page.locator(".qg-submode", { hasText: "生成图片" }).click();
    await page.waitForFunction(() => document.querySelector(".qg-prompt").hidden === false, null, { timeout: 15000 });
    await page.locator(".qg-switch-chip", { hasText: "写实人像" }).click();
    await page.waitForFunction(() => document.querySelector(".qg-switch-chip[data-active='true']").textContent.includes("写实人像"));
    assert.equal(await page.locator('.qg-field-input[name="steps"]').inputValue(), "8");

    // Clearing drops the presets but leaves a hand-picked LoRA in place.
    await page.locator(".qg-preset-clear").click();
    await page.waitForFunction(() => document.querySelectorAll(".qg-preset-chip[data-active='true']").length === 0);
    const cleared = await page.evaluate(() => ({
      steps: document.querySelector('.qg-field-input[name="steps"]').value,
      cfg: document.querySelector('.qg-field-input[name="cfg"]').value,
      active: document.querySelectorAll(".qg-preset-chip[data-active='true']").length,
      status: document.querySelector(".qg-status-line").textContent,
    }));
    assert.equal(cleared.active, 0, "no preset chip is active after clearing");
    assert.equal(cleared.steps, "8", "clearing restores the pipeline's own step count");
    assert.equal(cleared.cfg, "1.5");
    assert.deepEqual(overrides, {}, "the saved workflow file is emptied of presets");
    assert.ok(cleared.status.length > 0, "clearing is reported on screen");

    // The graph editor is off the page: recipes, presets and the composer decide
    // what runs, and the backend still keeps logs/image-workflow.json.
    const removed = await page.evaluate(() => ({
      card: document.querySelector(".qg-workflow"),
      rows: document.querySelectorAll(".qg-workflow-row").length,
      badge: document.querySelector(".qg-workflow-badge"),
      save: [...document.querySelectorAll("button")].filter(node => node.textContent.includes("保存工作流")).length,
      presets: document.querySelectorAll(".qg-preset-chip").length,
    }));
    assert.equal(removed.card, null, "the 运行的工作流 card is gone");
    assert.equal(removed.rows, 0, "no graph rows are rendered");
    assert.equal(removed.badge, null, "the 本次运行/下次运行 badge is gone");
    assert.equal(removed.save, 0, "the save-workflow button is gone");
    assert.ok(removed.presets > 0, "the LoRA preset buttons survived in their own card");

    // Switching back keeps the Flux pipeline untouched.
    await page.locator(".qg-pipeline-select").selectOption("flux2");
    await page.waitForFunction(() => document.querySelectorAll(".qg-model-value")[0].textContent === "flux-2-klein-9b-fp8.safetensors");
    const backToFlux = await page.evaluate(() => ({
      model: document.querySelectorAll(".qg-model-value")[0].textContent,
      steps: document.querySelector('.qg-field-input[name="steps"]').value,
      width: document.querySelector('.qg-field-input[name="width"]').value,
      hidden: document.querySelector(".qg-references").hidden,
    }));
    assert.equal(backToFlux.model, "flux-2-klein-9b-fp8.safetensors", "the Flux model returns");
    assert.equal(backToFlux.steps, "4", "the Flux defaults come back with it");
    assert.equal(backToFlux.width, "1024", "and its own canvas");
    // The fixture marks the Flux.2 recipe as reference-less on purpose, so the
    // uploader must be gone for it while the SDXL recipes keep it.
    assert.equal(backToFlux.hidden, true, "a recipe that cannot start from an image hides the uploader");

    // --- generation shows live progress, preview and monitoring ---------------
    const submitsBeforeLive = submits.length;
    await page.locator(".qg-prompt-input").fill("一只戴帽子的猫");
    await page.locator(".qg-negative-input").fill("模糊");
    for (const [name, value] of [["width", "768"], ["height", "512"], ["steps", "8"], ["cfg", "1.5"], ["denoise", "0.8"]]) {
      await page.locator(`.qg-field-input[name="${name}"]`).fill(value);
    }
    await seed.fill("424242");
    await page.locator(".qg-composer .qg-actions .qg-button-primary").click();
    await page.waitForFunction(() => document.querySelector(".qg-progress-percent").textContent !== "0%");
    const live = await page.evaluate(() => ({
      phase: document.querySelector(".qg-progress .qg-card-hint").textContent,
      percent: document.querySelector(".qg-progress-percent").textContent,
      speed: document.querySelectorAll(".qg-stat-value")[2].textContent,
      elapsed: document.querySelectorAll(".qg-stat-value")[0].textContent,
      eta: document.querySelectorAll(".qg-stat-value")[1].textContent,
      previewShown: !document.querySelector(".qg-preview").hidden,
      cancelVisible: !document.querySelector(".qg-progress .qg-button-danger").hidden,
      submitDisabled: document.querySelector(".qg-composer .qg-actions .qg-button-primary").disabled,
    }));
    assert.ok(live.phase.includes("采样"), `phase shows sampling: ${live.phase}`);
    assert.notEqual(live.percent, "0%", "the progress bar tracks the sampler");
    assert.ok(live.speed.includes("步/秒"), `speed is shown: ${live.speed}`);
    assert.notEqual(live.elapsed, "—", "elapsed time is shown");
    assert.notEqual(live.eta, "—", "ETA is shown");
    assert.equal(live.previewShown, true, "the sampler preview is shown");
    assert.equal(live.cancelVisible, true, "cancel is offered while running");
    assert.equal(live.submitDisabled, true, "a second submission is blocked while running");
    // The earlier upscale scenario also submitted, so this checks its own request.
    assert.equal(submits.length, submitsBeforeLive + 1, "exactly one generate request was sent here");
    const liveSubmit = submits[submits.length - 1];
    assert.equal(liveSubmit.steps, 8);
    assert.equal(liveSubmit.seed, 424242);
    assert.equal(liveSubmit.width, 768);
    assert.equal(liveSubmit.negative_prompt, "模糊");

    // --- completion writes history and refreshes monitoring -------------------
    history = [historyEntry()];
    busy = false;
    job = makeJob({ state: "completed", phase: "completed", progress: 1, images: [png], timings: { prompt: 4.2, load: 12.5, sampling: 20, restore: 9, total: 46 } });
    await page.waitForFunction(() => document.querySelectorAll(".qg-history-item").length === 1, null, { timeout: 15000 });
    const done = await page.evaluate(() => {
      const item = document.querySelector(".qg-history-item");
      const chips = [...item.querySelectorAll(".qg-history-chip")].map(node => node.textContent);
      return {
        chips,
        prompt: item.querySelector(".qg-history-prompt").textContent === "一只戴帽子的猫",
        thumb: item.querySelectorAll(".qg-history-thumb").length === 1,
        actions: [...item.querySelectorAll(".qg-history-actions .qg-button")].map(node => node.textContent),
        timelineRows: document.querySelectorAll(".qg-timeline-row").length,
        averageTime: document.querySelectorAll(".qg-metric-value")[3].textContent,
        successRate: document.querySelectorAll(".qg-metric-value")[4].textContent,
        title: document.querySelector(".qg-history .qg-card-title").textContent,
      };
    });
    for (const expected of ["宽度 768", "高度 512", "步数 8", "提示词引导 1.5", "参考强度 0.8", "种子 424242"]) {
      assert.ok(done.chips.includes(expected), `history chip ${expected} missing from ${JSON.stringify(done.chips)}`);
    }
    assert.ok(done.chips.some(chip => chip.includes("步/秒")), "history records the sampling speed");
    assert.equal(done.prompt, true, "history shows the prompt");
    assert.equal(done.thumb, true, "history shows the result thumbnail");
    assert.deepEqual(done.actions, ["恢复参数", "基于此生成", "对比", "删除", "打开原图"], "history actions are complete");
    assert.ok(done.timelineRows >= 4, `the timings breakdown is shown: ${done.timelineRows}`);
    assert.notEqual(done.averageTime, "—", "average duration is monitored");
    assert.equal(done.successRate, "100%", "success rate is monitored");
    assert.ok(done.title.includes("1"), `history title carries the count: ${done.title}`);

    // --- restore and reuse act on the composer --------------------------------
    await page.locator(".qg-prompt-input").fill("temporary");
    await page.locator(".qg-history-actions .qg-button", { hasText: "恢复参数" }).first().click();
    assert.equal(await page.locator(".qg-prompt-input").inputValue(), "一只戴帽子的猫", "restore brings the prompt back");
    assert.equal(await seed.inputValue(), "424242", "restore brings the seed back");
    await page.locator(".qg-prompt-input").fill("temporary");
    await page.locator(".qg-history-actions .qg-button", { hasText: "基于此生成" }).first().click();
    assert.equal(await page.locator(".qg-prompt-input").inputValue(), "temporary", "reuse keeps the current prompt");

    // --- cancel path ----------------------------------------------------------
    await page.locator(".qg-composer .qg-actions .qg-button-primary").click();
    await page.waitForFunction(() => !document.querySelector(".qg-progress .qg-button-danger").hidden);
    await page.locator(".qg-progress .qg-button-danger").click();
    await page.waitForFunction(() => document.querySelector(".qg-progress").dataset.state === "cancelled", null, { timeout: 10000 });
    const cancelled = await page.evaluate(() => ({
      phase: document.querySelector(".qg-progress .qg-card-hint").textContent,
      state: document.querySelector(".qg-progress").dataset.state,
    }));
    assert.equal(cancelled.state, "cancelled", "the cancelled state is rendered");
    assert.ok(cancelled.phase.includes("取消"), `cancel is reported: ${cancelled.phase}`);

    // --- VRAM monitoring (the graph editor itself is gone) --------------------
    const monitoring = await page.evaluate(() => ({
      vram: document.querySelector('.qg-metric[data-metric="vram"] .qg-metric-value').textContent,
      gpuTiles: [...document.querySelectorAll(".qg-gpu-item")].map(item => item.querySelector(".qg-gpu-value").textContent),
      badge: document.querySelector(".qg-badge-state").dataset.online,
      workflowCard: document.querySelector(".qg-workflow"),
      reads: document.querySelectorAll(".qg-workflow-row").length,
    }));
    assert.ok(monitoring.vram.length > 0, "VRAM is monitored");
    assert.ok(monitoring.gpuTiles.length >= 4, `the GPU tiles are complete: ${monitoring.gpuTiles.length}`);
    assert.equal(monitoring.badge, "true", "ComfyUI connectivity is shown");
    assert.equal(monitoring.workflowCard, null, "the removed card never comes back");
    assert.equal(monitoring.reads, 0, "no graph rows are rendered anywhere");

    // The composer keeps owning the parameters, and the backend keeps the file.
    await page.locator('.qg-field-input[name="steps"]').fill("12");
    assert.equal(await page.locator('.qg-field-input[name="steps"]').inputValue(), "12",
                 "the composer still accepts the step count");
    assert.ok(workflowReads.length >= 1, "the panel still reads the saved workflow state");
    await page.locator('.qg-field-input[name="steps"]').fill("8");

    // --- delete and clear history --------------------------------------------
    await page.locator(".qg-history-actions .qg-button", { hasText: "删除" }).first().click();
    await page.waitForFunction(() => document.querySelectorAll(".qg-history-item").length === 0);
    assert.equal(await page.locator(".qg-empty").isVisible(), true, "the empty hint returns");
    history = [historyEntry({ id: "img-9", state: "error", error: "ComfyUI 工ComfyUI 工作流执行失败", images: [], steps_per_second: null })];
    await page.locator(".qg-history .qg-button", { hasText: "清空记录" }).click();
    await page.waitForFunction(() => document.querySelectorAll(".qg-history-item").length === 0);
    assert.equal(history.length, 0, "clear removes the stored records");

    // --- VRAM monitoring during and after a generation -----------------------
    busy = true;
    job = makeJob({
      gpu,
      graph: [
        { id: "1", class_type: "UNETLoader", title: "Flux.2 Klein 9B", inputs: { unet_name: "flux-2-klein-9b-fp8.safetensors" } },
        { id: "9", class_type: "Flux2Scheduler", title: "Flux.2 schedule", inputs: { steps: 8, width: 768 } },
      ],
    });
    await page.waitForFunction(() => !document.querySelector(".qg-gpu").hidden, null, { timeout: 15000 });
    const gpuLive = await page.evaluate(() => ({
      tile: document.querySelector('.qg-metric[data-metric="vram"] .qg-metric-value').textContent,
      delta: document.querySelector(".qg-metric-delta").textContent,
      values: [...document.querySelectorAll(".qg-gpu-value")].map(node => node.textContent),
      hint: document.querySelector(".qg-gpu-hint").textContent,
      jobWidth: document.querySelector(".qg-gpu-bar-job").style.width,
      phase: document.querySelector(".qg-progress .qg-card-hint").textContent,
    }));
    assert.ok(gpuLive.tile.includes("10.73 GB") && gpuLive.tile.includes("11.94 GB"), `the VRAM tile shows used/total: ${gpuLive.tile}`);
    assert.equal(gpuLive.delta, "+8.91 GB", "the tile shows this job's peak footprint");
    assert.equal(gpuLive.values[0], "11.94 GB", "total VRAM");
    assert.equal(gpuLive.values[1], "10.73 GB", "used VRAM");
    assert.equal(gpuLive.values[2], "8.40 GB", "VRAM used by this generation");
    assert.equal(gpuLive.values[3], "8.91 GB", "peak VRAM for this generation");
    assert.ok(gpuLive.hint.includes("GPU 利用率"), `utilisation is reported: ${gpuLive.hint}`);
    assert.ok(gpuLive.jobWidth && gpuLive.jobWidth !== "0%", "the job's share is drawn on the VRAM bar");
    assert.ok(gpuLive.phase.length > 0, `the progress card names the phase: ${gpuLive.phase}`);
    busy = false;
    job = makeJob({ state: "completed", phase: "completed", progress: 1, images: [png], gpu });
    await page.waitForTimeout(1800);
    assert.equal(await page.locator('.qg-field-input[name="steps"]').inputValue(), "8",
                 "the composer keeps its values once the job settles");

    // --- a failed job stays visible with its reason and a retry --------------    history = [historyEntry({ id: "img-fail", state: "error", error: "ComfyUI 未启动或无法连接（http://127.0.0.1:8188），请先启动 ComfyUI 后再生成。", images: [], steps_per_second: null })];
    busy = false;
    job = makeJob({ state: "error", phase: "error", error: "ComfyUI 未启动或无法连接（http://127.0.0.1:8188），请先启动 ComfyUI 后再生成。", preview: null, images: [] });
    status = { ...imageStatus };
    await page.waitForFunction(() => document.querySelector(".qg-failure").hidden === false, null, { timeout: 15000 });
    const failed = await page.evaluate(() => ({
      banner: document.querySelector(".qg-failure-text").textContent,
      kind: document.querySelector(".qg-failure").dataset.kind,
      retry: document.querySelector(".qg-failure-actions .qg-button").textContent,
      panelStillOpen: !document.querySelector(".qg-view").hidden,
      chatStillHidden: getComputedStyle(document.querySelector(".chat-screen")).display === "none",
      phase: document.querySelector(".qg-progress .qg-card-hint").textContent,
    }));
    assert.equal(failed.panelStillOpen, true, "the generate view stays open when a job fails");
    assert.equal(failed.chatStillHidden, true, "the chat stays hidden after a failed job");
    assert.equal(failed.kind, "offline", "an unreachable ComfyUI is reported as such");
    assert.ok(failed.banner.includes("ComfyUI"), `the banner names the cause: ${failed.banner}`);
    assert.ok(failed.retry.length > 0, "the banner offers a retry action");
    assert.ok(failed.phase.includes("失败"), `the progress card reports the failure: ${failed.phase}`);

    // --- the generate view survives the backend restoring text mode ----------
    status = { ...textStatus, revision: 9 };
    await page.waitForTimeout(2600);
    const afterRestore = await page.evaluate(() => ({
      panelOpen: !document.querySelector(".qg-view").hidden,
      chatHidden: getComputedStyle(document.querySelector(".chat-screen")).display === "none",
      generatePressed: document.querySelector('.local-mode-button[data-mode="generate"]').getAttribute("aria-pressed"),
      textPressed: document.querySelector('.local-mode-button[data-mode="text"]').getAttribute("aria-pressed"),
    }));
    assert.equal(afterRestore.panelOpen, true, "the generate view stays open while the backend is back in text mode");
    assert.equal(afterRestore.chatHidden, true, "the chat stays hidden while the generate tab is open");
    assert.equal(afterRestore.generatePressed, "true", "the generate tab stays selected");
    assert.equal(afterRestore.textPressed, "false", "the text tab is not selected");

    // --- leaving generate mode restores the chat ------------------------------
    await page.locator('.local-mode-button[data-mode="text"]').click();
    await page.locator(".qg-view").waitFor({ state: "hidden", timeout: 15000 });    const restored = await page.evaluate(() => ({
      chatVisible: getComputedStyle(document.querySelector(".chat-screen")).display !== "none",
      formVisible: getComputedStyle(document.querySelector(".conversation-chat-form")).display !== "none",
      pageWidth: document.documentElement.scrollWidth,
      viewportWidth: innerWidth,
    }));
    assert.equal(restored.chatVisible, true, "the chat screen returns in text mode");
    assert.equal(restored.formVisible, true, "the chat composer returns in text mode");
    assert.equal(restored.pageWidth, restored.viewportWidth, "no horizontal overflow");

    // --- screenshots for visual review ---------------------------------------
    fs.mkdirSync(path.join(root, "../logs/image-panel-validation"), { recursive: true });
    await page.setViewportSize({ width: 1440, height: 1100 });
    await page.locator('.local-mode-button[data-mode="generate"]').click();
    await page.locator(".qg-view:not([hidden])").waitFor();
    history = [historyEntry({ id: "img-shot", prompt: "一只戴帽子的猫站在窗台上，暖色阳光" })];
    busy = true;
    job = makeJob({});
    await page.waitForTimeout(1800);
    await page.screenshot({ path: path.join(root, "../logs/image-panel-validation/desktop.png"), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(500);
    const mobile = await page.evaluate(() => ({
      pageWidth: document.documentElement.scrollWidth,
      viewportWidth: innerWidth,
      modelRows: document.querySelectorAll(".qg-model-list dt").length,
      paramsColumns: getComputedStyle(document.querySelector(".qg-params")).gridTemplateColumns.split(" ").length,
    }));
    assert.equal(mobile.pageWidth, mobile.viewportWidth, "no horizontal overflow at 390px");
    assert.equal(mobile.modelRows, 5, "the model card stays complete on mobile");
    assert.equal(mobile.paramsColumns, 2, "parameters reflow to two columns on mobile");
    await page.screenshot({ path: path.join(root, "../logs/image-panel-validation/mobile.png"), fullPage: true });

    assert.deepEqual(pageErrors, [], `no page errors: ${pageErrors.join(" | ")}`);
    console.log(JSON.stringify({ passed: true, submits: submits.length, progressPolls, mobile }));
    await context.close();
  } finally {
    await browser.close();
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
