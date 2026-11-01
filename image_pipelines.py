"""Image pipelines: which architecture runs, with which files and parameters.

The gateway started as Flux.2 Klein only, with node ids hard-coded into the
workflow editor and the monitoring panels. This module turns that into a small
registry so several architectures can coexist and be switched from the panel:

* ``sdxl``   — SDXL / Pony / Illustrious fp16 checkpoints, the largest NSFW and
  LoRA ecosystem. This is the machine's default pipeline: CyberRealisticPony V18
  fp16 plus the LCM accelerator (8 steps) and the photorealism slider.
* ``flux2``  — Flux.2 Klein (the original pipeline, kept as a comparison).
* ``chroma`` — Chroma1-HD, an uncensored Flux-architecture model (optional, its
  17.8 GB weights are not on this machine yet).

Each entry declares what it needs, how it samples by default, which node classes
ComfyUI must provide, and which node inputs stay editable in the UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Field:
    """One editable node input."""

    input: str
    label: str
    kind: str = "text"          # text | int | float | bool
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    param: str | None = None    # composer parameter this input mirrors
    choices: str | None = None  # folder to build a dropdown from

    def describe(self) -> dict:
        described = {"input": self.input, "label": self.label, "type": self.kind}
        if self.minimum is not None:
            described["min"] = self.minimum
        if self.maximum is not None:
            described["max"] = self.maximum
        if self.step is not None:
            described["step"] = self.step
        if self.param:
            described["param"] = self.param
        if self.choices:
            described["choices"] = self.choices
        return described


@dataclass(frozen=True)
class LoraPreset:
    """A LoRA the panel can apply with one click.

    ``role`` is either ``speed`` (an accelerator: it also proposes the step
    count, CFG and sampler that make it work) or ``quality`` (a style/detail
    add-on that stacks next to the accelerator).
    """

    key: str
    label: str
    lora: str
    role: str
    note: str
    families: tuple[str, ...]
    strength: float = 1.0
    params: dict = field(default_factory=dict)

    def describe(self, available: bool | None = None, active: bool = False) -> dict:
        described = {
            "key": self.key,
            "label": self.label,
            "lora": self.lora,
            "role": self.role,
            "note": self.note,
            "strength": self.strength,
            "params": dict(self.params),
            "active": active,
        }
        if available is not None:
            described["available"] = available
        return described


@dataclass(frozen=True)
class Node:
    """One node of the pipeline's graph, in execution order."""

    id: str
    class_type: str
    title: str
    fields: tuple[Field, ...] = ()

    def describe(self) -> dict:
        return {
            "id": self.id,
            "class_type": self.class_type,
            "title": self.title,
            "fields": [field.describe() for field in self.fields],
        }


@dataclass(frozen=True)
class Pipeline:
    key: str
    name: str
    family: str                       # graph builder used for this pipeline
    summary: str
    nsfw: str
    base_models: tuple[str, ...]      # names shown in the model picker
    defaults: dict
    nodes: tuple[Node, ...]
    requires: tuple[str, ...] = ()    # ComfyUI class_type names that must exist
    notes: str = ""
    sample_defaults: dict = field(default_factory=dict)
    lora_slots: tuple[str, ...] = ()  # LoRA node ids, in chaining order
    references_required: bool = False  # runs from an image, not from a prompt

    def describe(self, ready: bool | None = None, missing: list[str] | None = None) -> dict:
        described = {
            "key": self.key,
            "name": self.name,
            "family": self.family,
            "summary": self.summary,
            "nsfw": self.nsfw,
            "base_models": list(self.base_models),
            "defaults": dict(self.defaults),
            "nodes": [node.describe() for node in self.nodes],
            "requires": list(self.requires),
            "notes": self.notes,
            "lora_slots": list(self.lora_slots),
            "references_required": self.references_required,
        }
        if ready is not None:
            described["ready"] = ready
            described["missing"] = missing or []
        return described


FLUX2 = Pipeline(
    key="flux2",
    name="Flux.2 Klein 9B",
    family="flux2",
    summary="原管线：Flux.2 Klein fp8 + Qwen3-8B 编码器，4 步出图。",
    nsfw="受模型自身安全限制，NSFW 效果差",
    base_models=("Flux.2 Klein",),
    defaults={"steps": 4, "cfg": 1.0, "width": 1024, "height": 1024, "sampler": "euler"},
    nodes=(
        Node("1", "UNETLoader", "采样模型", (Field("unet_name", "模型文件", choices="diffusion_models"),)),
        Node("2", "CLIPLoader", "文本编码器", (Field("clip_name", "编码器文件", choices="text_encoders"),)),
        Node("3", "VAELoader", "VAE", (Field("vae_name", "VAE 文件", choices="vae"),)),
        Node("6", "EmptyFlux2LatentImage", "画布尺寸", (
            Field("width", "宽度", "int", 64, 4096, 64, param="width"),
            Field("height", "高度", "int", 64, 4096, 64, param="height"),
        )),
        Node("7", "RandomNoise", "种子", (Field("noise_seed", "种子", "int", 0, 2**64 - 1, 1, param="seed"),)),
        Node("8", "KSamplerSelect", "采样器", (Field("sampler_name", "采样器", choices="samplers"),)),
        Node("9", "Flux2Scheduler", "步数", (Field("steps", "步数", "int", 1, 200, 1, param="steps"),)),
        Node("10", "CFGGuider", "提示词引导", (Field("cfg", "CFG", "float", 0, 100, 0.1, param="cfg"),)),
        Node("4", "CLIPTextEncode", "正向提示词"),
        Node("5", "ConditioningZeroOut", "空负面条件"),
        Node("11", "SamplerCustomAdvanced", "采样"),
        Node("12", "VAEDecode", "解码"),
        Node("13", "SaveImage", "保存图片", (Field("filename_prefix", "文件名前缀"),)),
    ),
    requires=("UNETLoader", "CLIPLoader", "VAELoader", "EmptyFlux2LatentImage", "RandomNoise",
              "KSamplerSelect", "Flux2Scheduler", "CFGGuider", "SamplerCustomAdvanced"),
    notes="负面提示词不参与采样（ConditioningZeroOut），参考强度用于图生图。",
)

CHROMA = Pipeline(
    key="chroma",
    name="Chroma1-HD（不审查·Flux 架构）",
    family="chroma",
    summary="官方 uncensored 的 Flux 架构模型，26 步左右，写实与人体质量优于 Klein。",
    nsfw="设计为不审查（unlocked），社区用于 NSFW 写实",
    base_models=("Chroma",),
    defaults={"steps": 26, "cfg": 4.0, "width": 1024, "height": 1024, "sampler": "euler", "shift": 1.0},
    nodes=(
        Node("1", "UNETLoader", "采样模型", (Field("unet_name", "模型文件", choices="diffusion_models"),)),
        Node("2", "CLIPLoader", "T5 文本编码器", (Field("clip_name", "T5 编码器", choices="text_encoders"),
                                              Field("type", "类型", "text"))),
        Node("3", "VAELoader", "FLUX VAE", (Field("vae_name", "VAE 文件", choices="vae"),)),
        Node("4", "CLIPTextEncode", "正向提示词", (Field("text", "正向提示词"),)),
        Node("5", "CLIPTextEncode", "负面提示词", (Field("text", "负面提示词"),)),
        Node("6", "EmptySD3LatentImage", "画布尺寸", (
            Field("width", "宽度", "int", 64, 4096, 64, param="width"),
            Field("height", "高度", "int", 64, 4096, 64, param="height"),
        )),
        Node("7", "ModelSamplingAuraFlow", "采样偏移", (Field("shift", "shift", "float", 0, 10, 0.05),)),
        Node("8", "KSampler", "采样", (
            Field("seed", "种子", "int", 0, 2**64 - 1, 1, param="seed"),
            Field("steps", "步数", "int", 1, 200, 1, param="steps"),
            Field("cfg", "CFG", "float", 0, 100, 0.1, param="cfg"),
            Field("sampler_name", "采样器", choices="samplers"),
            Field("scheduler", "调度器", choices="schedulers"),
            Field("denoise", "重绘强度", "float", 0, 1, 0.05, param="denoise"),
        )),
        Node("9", "VAEDecode", "解码"),
        Node("10", "SaveImage", "保存图片", (Field("filename_prefix", "文件名前缀"),)),
    ),
    requires=("UNETLoader", "CLIPLoader", "VAELoader", "EmptySD3LatentImage", "ModelSamplingAuraFlow", "KSampler"),
    notes="需要 T5-XXL（fp8 约 4.9 GB）与 FLUX VAE；负面提示词真正参与采样。",
)

QWEN21 = Pipeline(
    key="qwen21",
    name="Qwen-Image 2.1（7B·中文排版/透明/改图）",
    family="qwen21",
    summary="Qwen-Image 2.1（7B）。强项：中文/文字排版、透明背景、参考图改图。1024² 约 30 秒。",
    nsfw="官方对内容有安全微调（不审查版只有编码器路线，且对出图无实测影响）",
    base_models=("Qwen-Image 2.1 int8",),
    defaults={"steps": 25, "cfg": 1.0, "width": 1024, "height": 1024, "sampler": "euler"},
    nodes=(
        Node("1", "UNETLoader", "采样模型", (Field("unet_name", "模型文件", choices="diffusion_models"),)),
        Node("2", "CLIPLoader", "文本编码器", (
            Field("clip_name", "编码器文件", choices="text_encoders"),
            Field("type", "编码器类型"),
        )),
        Node("3", "VAELoader", "VAE", (Field("vae_name", "VAE 文件", choices="vae"),)),
        Node("4", "TextEncodeQwenImage21", "提示词", (
            Field("prompt", "正向提示词"),
            Field("negative_prompt", "负面提示词"),
            Field("resolution", "训练分辨率", "int", 256, 2048, 64),
        )),
        Node("5", "EmptyLatentImage", "画布尺寸", (
            Field("width", "宽度", "int", 64, 4096, 64, param="width"),
            Field("height", "高度", "int", 64, 4096, 64, param="height"),
        )),
        Node("6", "KSampler", "采样", (
            Field("seed", "种子", "int", 0, 2**64 - 1, 1, param="seed"),
            Field("steps", "步数", "int", 1, 200, 1, param="steps"),
            Field("cfg", "CFG", "float", 0, 100, 0.1, param="cfg"),
            Field("sampler_name", "采样器", choices="samplers"),
            Field("scheduler", "调度器", choices="schedulers"),
        )),
        Node("7", "VAEDecode", "解码"),
        Node("8", "SaveImage", "保存图片", (Field("filename_prefix", "文件名前缀"),)),
    ),
    requires=("UNETLoader", "CLIPLoader", "VAELoader", "TextEncodeQwenImage21",
              "EmptyLatentImage", "KSampler"),
    notes="25 步 / CFG 1。调高 CFG 会让速度翻倍，不建议。",
)

QWEN21_TURBO = Pipeline(
    key="qwen21_turbo",
    name="Qwen-Image 2.1 Turbo（8 步快出图）",
    family="qwen21_turbo",
    summary="2.1 的 8 步加速版（GGUF Q8）。1024² 约 22 秒，不审查，可出 NSFW。",
    nsfw="与 2.1 同源；官方对内容有安全微调",
    base_models=("Qwen-Image 2.1 Turbo Q8",),
    defaults={"steps": 8, "cfg": 1.0, "width": 1024, "height": 1024, "sampler": "euler"},
    nodes=(
        Node("1", "UnetLoaderGGUF", "采样模型（GGUF）", (Field("unet_name", "模型文件", choices="diffusion_models"),)),
        Node("2", "CLIPLoader", "文本编码器", (
            Field("clip_name", "编码器文件", choices="text_encoders"),
            Field("type", "编码器类型"),
        )),
        Node("3", "VAELoader", "VAE", (Field("vae_name", "VAE 文件", choices="vae"),)),
        Node("4", "TextEncodeQwenImage21", "提示词", (
            Field("prompt", "正向提示词"),
            Field("negative_prompt", "负面提示词"),
            Field("resolution", "训练分辨率", "int", 256, 2048, 64),
        )),
        Node("5", "EmptyLatentImage", "画布尺寸", (
            Field("width", "宽度", "int", 64, 4096, 64, param="width"),
            Field("height", "高度", "int", 64, 4096, 64, param="height"),
        )),
        Node("6", "KSamplerSelect", "采样器", (Field("sampler_name", "采样器", choices="samplers"),)),
        Node("7", "ManualSigmas", "8 步 sigma 阶梯（官方）", (Field("sigmas", "sigma 序列"),)),
        Node("8", "CFGGuider", "提示词引导", (Field("cfg", "CFG", "float", 0, 100, 0.1, param="cfg"),)),
        Node("9", "RandomNoise", "种子", (Field("noise_seed", "种子", "int", 0, 2**64 - 1, 1, param="seed"),)),
        Node("10", "SamplerCustomAdvanced", "采样"),
        Node("11", "VAEDecode", "解码"),
        Node("12", "SaveImage", "保存图片", (Field("filename_prefix", "文件名前缀"),)),
    ),
    requires=("UnetLoaderGGUF", "CLIPLoader", "VAELoader", "TextEncodeQwenImage21", "EmptyLatentImage",
              "KSamplerSelect", "ManualSigmas", "CFGGuider", "RandomNoise", "SamplerCustomAdvanced"),
    notes="8 步固定，改步数无效；CFG 必须为 1。",
)

SEEDVR2 = Pipeline(
    key="seedvr2",
    name="SeedVR2 放大 / 修复（从参考图开始）",
    family="seedvr2",
    summary="SeedVR2 3B int8 单步恢复：把参考图放大并补回细节，不需要提示词。",
    nsfw="纯放大修复，不改内容，尺度跟随参考图",
    base_models=("SeedVR2 3B int8",),
    # The composer's width/height are canvas size, which an upscaler has no use
    # for: output size is the reference image times scale. They stay declared so
    # the panel keeps its layout, but the builder ignores them.
    defaults={"steps": 2, "cfg": 1.0, "width": 0, "height": 0, "sampler": "euler", "scale": 1.5},
    nodes=(
        Node("7", "UNETLoader", "SeedVR2 模型", (Field("unet_name", "模型文件", choices="diffusion_models"),)),
        Node("5", "VAELoader", "SeedVR2 VAE", (Field("vae_name", "VAE 文件", choices="vae"),)),
        Node("1", "LoadImage", "参考图"),
        Node("3", "ResizeImageMaskNode", "放大倍率", (
            Field("resize_type.multiplier", "放大倍率", "float", 1.0, 4.0, 0.05, param="scale"),
        )),
        Node("4", "SeedVR2Preprocess", "输入预处理"),
        Node("6", "VAEEncodeTiled", "VAE 编码", (
            Field("tile_size", "编码瓦片", "int", 256, 2048, 64),
            Field("overlap", "编码重叠", "int", 0, 512, 16),
            Field("temporal_size", "编码时序块", "int", 8, 256, 8),
            Field("temporal_overlap", "编码时序重叠", "int", 0, 64, 4),
        )),
        Node("8", "SeedVR2Conditioning", "条件（与采样同一 latent）"),
        Node("10", "KSampler", "采样", (
            Field("seed", "种子", "int", 0, 2**64 - 1, 1, param="seed"),
            Field("steps", "采样步数", "int", 1, 8, 1, param="steps"),
            Field("cfg", "CFG", "float", 0, 100, 0.1, param="cfg"),
            Field("sampler_name", "采样器", choices="samplers"),
            Field("scheduler", "调度器", choices="schedulers"),
            Field("denoise", "重绘强度", "float", 0, 1, 0.05),
        )),
        Node("12", "VAEDecodeTiled", "VAE 解码", (
            Field("tile_size", "解码瓦片", "int", 256, 2048, 64),
            Field("overlap", "解码重叠", "int", 0, 512, 16),
            Field("temporal_size", "解码时序块", "int", 8, 256, 8),
            Field("temporal_overlap", "解码时序重叠", "int", 0, 64, 4),
        )),
        Node("13", "SeedVR2PostProcessing", "色彩对齐", (
            Field("color_correction_method", "色彩校正", choices="color_correction"),
        )),
        Node("15", "SaveImage", "保存图片", (Field("filename_prefix", "文件名前缀"),)),
    ),
    requires=("UNETLoader", "VAELoader", "LoadImage", "ResizeImageMaskNode", "SeedVR2Preprocess",
              "VAEEncodeTiled", "SeedVR2Conditioning", "KSampler", "VAEDecodeTiled",
              "SeedVR2PostProcessing", "SaveImage"),
    notes=("放大不需要提示词，只要一张参考图。条件节点必须和采样器读同一个 latent："
           "分块输出的列表会被 comfy_execution 按最长列表补齐，条件若另接一路就会错配。"
           "倍率 1.0× 只提质不放大；1.25×/3 步清晰度最高；1.5×/2 步是折中。"),
    references_required=True,
)

# The video twin of SEEDVR2: same models and the same one-step restoration, but it
# reads a clip instead of a still. It exists as its own pipeline because the loader
# and the temporal chunk/merge pair differ, and because the panel offers it under
# the video mode rather than the image one.
SEEDVR2_VIDEO = Pipeline(
    key="seedvr2_video",
    name="SeedVR2 视频放大 / 修复（从已上传的视频开始）",
    family="seedvr2_video",
    summary="SeedVR2 3B int8 逐帧恢复整段视频：放大并补回细节，不需要提示词。",
    nsfw="纯放大修复，不改内容；画面尺度跟随源视频",
    base_models=("SeedVR2 3B int8",),
    defaults={"steps": 2, "cfg": 1.0, "width": 0, "height": 0, "sampler": "euler", "scale": 1.25},
    nodes=(
        Node("7", "UNETLoader", "SeedVR2 模型", (Field("unet_name", "模型文件", choices="diffusion_models"),)),
        Node("5", "VAELoader", "SeedVR2 VAE", (Field("vae_name", "VAE 文件", choices="vae"),)),
        Node("1", "LoadVideo", "源视频"),
        Node("3", "ResizeImageMaskNode", "放大倍率", (
            Field("resize_type.multiplier", "放大倍率", "float", 1.0, 2.0, 0.05, param="scale"),
        )),
        Node("4", "SeedVR2Preprocess", "输入预处理"),
        Node("6", "VAEEncodeTiled", "VAE 编码", (
            Field("tile_size", "编码瓦片", "int", 256, 2048, 64),
            Field("overlap", "编码重叠", "int", 0, 512, 16),
            Field("temporal_size", "编码时序块", "int", 8, 256, 8),
            Field("temporal_overlap", "编码时序重叠", "int", 0, 64, 4),
        )),
        Node("9", "SeedVR2TemporalChunk", "时序分块", (
            Field("temporal_overlap", "分块重叠", "int", 0, 64, 1),
        )),
        Node("8", "SeedVR2Conditioning", "条件（与采样同一分块）"),
        Node("10", "KSampler", "采样", (
            Field("seed", "种子", "int", 0, 2**64 - 1, 1, param="seed"),
            Field("steps", "采样步数", "int", 1, 8, 1, param="steps"),
            Field("cfg", "CFG", "float", 0, 100, 0.1, param="cfg"),
            Field("sampler_name", "采样器", choices="samplers"),
            Field("scheduler", "调度器", choices="schedulers"),
            Field("denoise", "重绘强度", "float", 0, 1, 0.05),
        )),
        Node("11", "SeedVR2TemporalMerge", "合并分块"),
        Node("12", "VAEDecodeTiled", "VAE 解码", (
            Field("tile_size", "解码瓦片", "int", 256, 2048, 64),
            Field("overlap", "解码重叠", "int", 0, 512, 16),
            Field("temporal_size", "解码时序块", "int", 8, 256, 8),
            Field("temporal_overlap", "解码时序重叠", "int", 0, 64, 4),
        )),
        Node("13", "SeedVR2PostProcessing", "色彩对齐", (
            Field("color_correction_method", "色彩校正", choices="color_correction"),
        )),
        Node("14", "CreateVideo", "合成视频"),
        Node("15", "SaveVideo", "保存视频", (Field("filename_prefix", "文件名前缀"),)),
    ),
    requires=("UNETLoader", "VAELoader", "LoadVideo", "GetVideoComponents", "ResizeImageMaskNode",
              "SeedVR2Preprocess", "VAEEncodeTiled", "SeedVR2TemporalChunk", "SeedVR2Conditioning",
              "KSampler", "SeedVR2TemporalMerge", "VAEDecodeTiled", "SeedVR2PostProcessing",
              "CreateVideo", "SaveVideo"),
    notes=("视频放大同样不需要提示词。条件节点必须和采样器读同一个分块列表："
           "comfy_execution 按最长列表补齐，条件若另接一路就会错配并报维度不符。"
           "12 GB 卡上 1.5× 的显存余量很薄，长片段建议用 1.0× 或 1.25×。"),
    references_required=True,
)

SDXL = Pipeline(
    key="sdxl",
    name="SDXL / Pony / Illustrious（NSFW 生态最大·默认）",
    family="sdxl",
    summary="一体化 checkpoint，自带 CLIP 与 VAE；默认配 LCM-LoRA 8 步 + 照片级写实滑杆 1.5。",
    nsfw="最成熟：Pony/Illustrious 系专为 NSFW 训练，LoRA 生态最大",
    base_models=("SDXL 1.0", "Pony", "Illustrious", "NoobAI"),
    # The accelerated recipe is the default: the LCM preset writes sampler/scheduler
    # into the workflow file, so the step count and CFG must match it out of the box
    # or every generation would run LCM at 28 steps / CFG 6 and produce garbage.
    defaults={"steps": 8, "cfg": 1.5, "width": 832, "height": 1216, "sampler": "lcm", "scheduler": "sgm_uniform", "denoise": 1.0},
    nodes=(
        Node("10", "CheckpointLoaderSimple", "主模型", (Field("ckpt_name", "模型文件", choices="checkpoints"),)),
        Node("11", "LoraLoader", "LoRA 1", (
            Field("lora_name", "LoRA 文件", choices="loras"),
            Field("strength_model", "模型权重", "float", -2, 2, 0.05),
            Field("strength_clip", "CLIP 权重", "float", -2, 2, 0.05),
        )),
        Node("12", "LoraLoader", "LoRA 2", (
            Field("lora_name", "LoRA 文件", choices="loras"),
            Field("strength_model", "模型权重", "float", -2, 2, 0.05),
            Field("strength_clip", "CLIP 权重", "float", -2, 2, 0.05),
        )),
        Node("1", "CLIPTextEncode", "正向提示词", (Field("text", "正向提示词"),)),
        Node("2", "CLIPTextEncode", "负面提示词", (Field("text", "负面提示词"),)),
        Node("3", "EmptyLatentImage", "画布尺寸", (
            Field("width", "宽度", "int", 64, 4096, 64, param="width"),
            Field("height", "高度", "int", 64, 4096, 64, param="height"),
            Field("batch_size", "每批张数", "int", 1, 8, 1),
        )),
        Node("4", "KSampler", "采样", (
            Field("seed", "种子", "int", 0, 2**64 - 1, 1, param="seed"),
            Field("steps", "步数", "int", 1, 200, 1, param="steps"),
            Field("cfg", "CFG", "float", 0, 100, 0.1, param="cfg"),
            Field("sampler_name", "采样器", choices="samplers"),
            Field("scheduler", "调度器", choices="schedulers"),
            Field("denoise", "重绘强度", "float", 0, 1, 0.05, param="denoise"),
        )),
        Node("5", "VAEDecode", "解码"),
        Node("6", "SaveImage", "保存图片", (Field("filename_prefix", "文件名前缀"),)),
    ),
    requires=("CheckpointLoaderSimple", "LoraLoader", "CLIPTextEncode", "EmptyLatentImage", "KSampler"),
    notes=("负面提示词真正参与采样；LoRA 可叠加。当前默认配方：LCM-LoRA 8 步（CFG 1.5、lcm+sgm_uniform）"
           "+ 照片级写实滑杆 1.5。想跑全量 28 步时把步数/CFG 改回 28/6 并点“关闭全部”关掉加速 LoRA。"),
    lora_slots=("11", "12"),
)

# GGUF 变体保留定义但不注册：本机已删除 GGUF 权重（cyberrealisticXL Q8 + 双 CLIP），
# 面板不再提供该管线。如需恢复：把下面的 SDXL_GGUF 加回 PIPELINES 并重新下载
# deployment/fetch_image_models.py 的 4/5/6 号条目。
SDXL_GGUF = Pipeline(
    key="sdxl_gguf",
    name="SDXL/Pony GGUF（省显存·下载小）",
    family="sdxl_gguf",
    summary="同一个 SDXL 写实家族，但用 GGUF 量化权重 + 双 CLIP，占用更小、下载更快。",
    nsfw="最成熟：Pony/Illustrious 系专为 NSFW 训练，LoRA 生态最大",
    base_models=("SDXL 1.0", "Pony", "Illustrious"),
    defaults={"steps": 28, "cfg": 6.0, "width": 832, "height": 1216, "sampler": "dpmpp_2m", "scheduler": "karras", "denoise": 1.0},
    nodes=(
        Node("10", "UnetLoaderGGUF", "主模型（GGUF）", (Field("unet_name", "GGUF 模型", choices="unet_gguf"),)),
        Node("13", "DualCLIPLoader", "双 CLIP 编码器", (
            Field("clip_name1", "CLIP-L", choices="text_encoders"),
            Field("clip_name2", "CLIP-G", choices="text_encoders"),
            Field("type", "类型"),
        )),
        Node("14", "VAELoader", "VAE（留空用内置）", (Field("vae_name", "VAE 文件", choices="vae"),)),
        Node("11", "LoraLoader", "LoRA 1", (
            Field("lora_name", "LoRA 文件", choices="loras"),
            Field("strength_model", "模型权重", "float", -2, 2, 0.05),
            Field("strength_clip", "CLIP 权重", "float", -2, 2, 0.05),
        )),
        Node("12", "LoraLoader", "LoRA 2", (
            Field("lora_name", "LoRA 文件", choices="loras"),
            Field("strength_model", "模型权重", "float", -2, 2, 0.05),
            Field("strength_clip", "CLIP 权重", "float", -2, 2, 0.05),
        )),
        Node("1", "CLIPTextEncode", "正向提示词", (Field("text", "正向提示词"),)),
        Node("2", "CLIPTextEncode", "负面提示词", (Field("text", "负面提示词"),)),
        Node("3", "EmptyLatentImage", "画布尺寸", (
            Field("width", "宽度", "int", 64, 4096, 64, param="width"),
            Field("height", "高度", "int", 64, 4096, 64, param="height"),
            Field("batch_size", "每批张数", "int", 1, 8, 1),
        )),
        Node("4", "KSampler", "采样", (
            Field("seed", "种子", "int", 0, 2**64 - 1, 1, param="seed"),
            Field("steps", "步数", "int", 1, 200, 1, param="steps"),
            Field("cfg", "CFG", "float", 0, 100, 0.1, param="cfg"),
            Field("sampler_name", "采样器", choices="samplers"),
            Field("scheduler", "调度器", choices="schedulers"),
            Field("denoise", "重绘强度", "float", 0, 1, 0.05, param="denoise"),
        )),
        Node("5", "VAEDecode", "解码"),
        Node("6", "SaveImage", "保存图片", (Field("filename_prefix", "文件名前缀"),)),
    ),
    requires=("UnetLoaderGGUF", "DualCLIPLoader", "VAELoader", "LoraLoader", "CLIPTextEncode", "EmptyLatentImage", "KSampler"),
    notes="需要 ComfyUI-GGUF（本机启动参数已加白名单）。GGUF 权重与 fp16 checkpoint 是同一模型家族，可用同一批 LoRA。",
    lora_slots=("11", "12"),
)

# One-click LoRAs. The speed entries are the mature distillation ecosystem for
# SDXL/Pony (LCM, DMD2, Hyper-SD) and each carries the sampling settings that
# make it work; the quality entries stack into the second slot.
LORA_PRESETS: tuple[LoraPreset, ...] = (
    LoraPreset(
        key="lcm",
        label="LCM-LoRA · 8 步",
        lora="lcm-lora-sdxl.safetensors",
        role="speed",
        note="Latent Consistency：4–8 步、CFG 1–2，社区最成熟的 SDXL 提速 LoRA。",
        families=("sdxl", "sdxl_gguf"),
        params={"steps": 8, "cfg": 1.5, "sampler": "lcm", "scheduler": "sgm_uniform", "denoise": 1.0},
    ),
    LoraPreset(
        key="dmd2",
        label="DMD2 · 4 步",
        lora="dmd2_sdxl_4step_lora_fp16.safetensors",
        role="speed",
        note="DMD2 分布匹配蒸馏：4 步、CFG 1，速度最快，适合批量试稿。",
        families=("sdxl", "sdxl_gguf"),
        params={"steps": 4, "cfg": 1.0, "sampler": "lcm", "scheduler": "sgm_uniform", "denoise": 1.0},
    ),
    LoraPreset(
        key="hyper8",
        label="Hyper-SDXL · 8 步",
        lora="Hyper-SDXL-8steps-CFG-lora.safetensors",
        role="speed",
        note="Hyper-SD 8 步 CFG 版：保留 CFG 引导，提示词跟随最好；本机实测 dpmpp_sde 反而更慢，故用 dpmpp_2m。",
        families=("sdxl", "sdxl_gguf"),
        params={"steps": 8, "cfg": 5.0, "sampler": "dpmpp_2m", "scheduler": "karras", "denoise": 1.0},
    ),
    LoraPreset(
        key="photoreal",
        label="照片级写实滑杆",
        lora="sdxl_photorealistic_slider_v1-0.safetensors",
        role="quality",
        note="Ostris 写实滑杆，正向权重越高越偏真照片质感；与加速 LoRA 叠加使用。",
        families=("sdxl", "sdxl_gguf"),
        strength=1.5,
    ),
    LoraPreset(
        key="face",
        label="面部结构修复",
        lora="face_xl_v0_1.safetensors",
        role="quality",
        note="Ostris face helper：改善五官结构、皮肤质感，减少糊脸。",
        families=("sdxl", "sdxl_gguf"),
    ),
    LoraPreset(
        key="detail",
        label="极致细节滑杆",
        lora="extremely-detailed.safetensors",
        role="quality",
        note="细节滑杆：增强纹理与锐度，写实人像加 0.8–1.2 权重。",
        families=("sdxl", "sdxl_gguf"),
    ),
)
PRESET_INDEX: dict[str, LoraPreset] = {preset.key: preset for preset in LORA_PRESETS}
PRESET_FILES: frozenset[str] = frozenset(preset.lora for preset in LORA_PRESETS)
PRESET_ROLES: tuple[str, ...] = ("speed", "quality")

# Registered pipelines, in the order the panel offers them. `sdxl` (fp16 Pony +
# LCM + photorealism slider) is the machine's default recipe; the GGUF variant is
# defined above but not registered, because its weights were removed.
@dataclass(frozen=True)
class Workflow:
    """A one-click recipe: pipeline + LoRA presets + the parameters to run with.

    The panel shows these as buttons with a plain-language description, so a user
    does not have to know which LoRA or step count a look needs.
    """

    key: str
    label: str
    pipeline: str
    presets: tuple[str, ...]
    params: dict
    summary: str
    detail: str = ""
    references: bool = True      # can start from an uploaded reference image
    mode: str = "image"          # which local mode offers this recipe
    kind: str = "generate"       # generate | upscale | denoise: the sub-module that owns it

    def describe(self, available: bool | None = None, active: bool = False) -> dict:
        described = {
            "key": self.key,
            "label": self.label,
            "pipeline": self.pipeline,
            "pipeline_name": pipeline(self.pipeline).name,
            "presets": list(self.presets),
            "params": dict(self.params),
            "summary": self.summary,
            "detail": self.detail,
            "references": self.references,
            "mode": self.mode,
            "kind": self.kind,
            "active": active,
        }
        if available is not None:
            described["available"] = available
        return described


# The order is the order the panel offers them: the Qwen recipes first (they take
# a reference image and are what the panel is set up for now), then the upscalers
# which live in their own sub-module.
WORKFLOWS: tuple[Workflow, ...] = (
    Workflow(
        key="qwen21_turbo8",
        label="Qwen Turbo · 8 步 · NSFW",
        pipeline="qwen21_turbo",
        presets=(),
        params={"steps": 8, "cfg": 1.0, "denoise": 0.65, "width": 1024, "height": 1024},
        summary="最快的 Qwen：1024² 约 22 秒，不审查，可出 NSFW；也能放参考图重绘。",
        detail="步数由模型自带，面板上的步数只是记录。1024² 约 22 秒、2048² 约 95 秒。",
        references=True,
    ),
    Workflow(
        key="qwen21_turbo8_2k",
        label="Qwen Turbo · 8 步 · 2048² 高清",
        pipeline="qwen21_turbo",
        presets=(),
        params={"steps": 8, "cfg": 1.0, "denoise": 0.65, "width": 2048, "height": 2048},
        summary="2048×2048：约 95 秒，伞、手、文字等结构明显更清楚。",
        detail="显存约 11.3 GB。要细节又不想等 4 分钟，用这一档。",
        references=True,
    ),
    Workflow(
        key="qwen21_turbo8_4k",
        label="Qwen Turbo · 8 步 · 4096 宽幅",
        pipeline="qwen21_turbo",
        presets=(),
        params={"steps": 8, "cfg": 1.0, "denoise": 0.65, "width": 4096, "height": 2304},
        summary="4096×2304（16:9）：约 4–5 分钟，适合大屏/打印。",
        detail="显存约 11.6 GB。正方形 4096² 要更久，不推荐。",
        references=True,
    ),
    Workflow(
        key="qwen21_real",
        label="Qwen 2.1 · 通用（25 步）",
        pipeline="qwen21",
        presets=(),
        params={"steps": 25, "cfg": 1.0, "denoise": 0.65, "width": 1024, "height": 1024},
        summary="画质档：1024² 约 30 秒，质感比 Turbo 更自然。",
        detail="文字排版、透明背景、参考图改图都能用；负面提示词在 CFG 1 下无效。",
        references=True,
    ),
    Workflow(
        key="upscale_sharp",
        label="放大 · 清晰度最佳 1.25×（噪声持平）",
        pipeline="seedvr2",
        presets=(),
        params={"scale": 1.25, "steps": 3, "cfg": 1.0},
        summary="1.25× 三 步：细节最锐，皮肤噪点比 1.5× 少两成。832×1216 约 25 秒。",
        detail="三项实测里清晰度最高（Laplacian 方差 74.9，是同倍率 lanczos 的 4.9 倍）。"
               "想要更多细节又不想脸上发脏，选这一档；要抹颗粒到「降噪」模块再走一遍。",
        kind="upscale",
    ),
    Workflow(
        key="upscale_clean",
        label="放大 · 细节最多 1.0×",
        pipeline="seedvr2",
        presets=(),
        params={"scale": 1.0, "steps": 3, "cfg": 1.0},
        summary="不放大，只把原有画面修实：皮肤噪点最低、速度最快（约 18 秒），尺寸不变。",
        detail="适合原图分辨率已经够、只是嫌糊或有压缩痕迹的情况。"
               "它的噪点地板是三项里最低的。",
        kind="upscale",
    ),
    Workflow(
        key="upscale_balanced",
        label="放大 · 最干净 1.5×",
        pipeline="seedvr2",
        presets=(),
        params={"scale": 1.5, "steps": 2, "cfg": 1.0},
        summary="1.5× 两 步：分辨率提得最多，质量与噪点折中。832×1216 出 1248×1824。",
        detail="1.5× 档内噪点最低的配方。要出大图或印刷时用这一档。",
        kind="upscale",
    ),
    Workflow(
        key="video_upscale_clean",
        label="视频放大 · 细节最多 1.0×",
        pipeline="seedvr2_video",
        presets=(),
        params={"scale": 1.0, "steps": 3, "cfg": 1.0},
        summary="视频不放大，只修实：皮肤噪点最低，显存最省，适合 768x512 的 H3 成片。",
        detail="实测 1.0× 的噪点地板最低（crawl 0.27）。180 帧 768x512 约 150 秒。"
               "要顺带抹颗粒，放大完到「降噪」模块再走一遍。",
        mode="video",
        kind="upscale",
    ),
    Workflow(
        key="video_upscale_sharp",
        label="视频放大 · 清晰度最佳 1.25×（噪声持平）",
        pipeline="seedvr2_video",
        presets=(),
        params={"scale": 1.25, "steps": 3, "cfg": 1.0},
        summary="视频 1.25× 三 步：清晰度最高，噪点比 1.5× 少两成。",
        detail="实测清晰度 74.9（同倍率 lanczos 的 4.9 倍）。显存余量适中。"
               "SeedVR2 是细节重建器，单独用噪声会比原片高 7%～15%，所以配「降噪」模块一起用。",
        mode="video",
        kind="upscale",
    ),
    Workflow(
        key="video_upscale_balanced",
        label="视频放大 · 最干净 1.5×",
        pipeline="seedvr2_video",
        presets=(),
        params={"scale": 1.5, "steps": 2, "cfg": 1.0},
        summary="视频 1.5× 两 步：分辨率提得最多，但 12 GB 卡上显存余量很薄。",
        detail="180 帧实测峰值 11771 MiB，距上限仅约 450 MiB；片段较长时可能溢出。"
               "1.5× 实测细节比原片少 11%～18%（偏软），要细节优先选 1.25×。",
        mode="video",
        kind="upscale",
    ),
    Workflow(
        key="video_denoise",
        label="降噪 · nlmeans（默认 2.0）",
        pipeline="seedvr2_video",
        presets=(),
        # The denoise module never builds a graph: the panel posts the clip to
        # /local/video/denoise-source, and skip_upscale is what makes the job layer refuse
        # this recipe if a caller aims /local/image/generate at it by mistake.
        params={"scale": 1.0, "steps": 3, "cfg": 1.0, "video_denoise": 2.0,
                "skip_upscale": True},
        summary="只做降噪、不放大：ffmpeg 在 CPU 上跑 nlmeans，实测噪声 −26%、细节 −5%，不占显存。",
        detail="这是「降噪」模块自己的工序，和放大分开：整条链路实测顺序必须是超分在前、降噪在后"
               "（反过来噪声反而 +11%）。强度可调 1.0–3.5，默认 2.0（3.0 时 −34% / −10%）；"
               "单张图片也走同一条路。",
        mode="video",
        kind="denoise",
    ),
)
WORKFLOW_INDEX: dict[str, Workflow] = {workflow.key: workflow for workflow in WORKFLOWS}


def workflow(key: str | None) -> Workflow | None:
    return WORKFLOW_INDEX.get(key or "")


def workflows_for_all() -> tuple[Workflow, ...]:
    return WORKFLOWS


# SDXL/Pony was removed at the owner's request together with its LoRAs. The
# Pipeline objects stay in this module as dead code so old graphs remain readable,
# but neither is registered: the panel never offers them and nothing loads them.
PIPELINES: dict[str, Pipeline] = {
    pipeline.key: pipeline
    for pipeline in (QWEN21, QWEN21_TURBO, SEEDVR2, SEEDVR2_VIDEO)
}
DEFAULT_PIPELINE = QWEN21_TURBO.key


def pipeline(key: str | None) -> Pipeline:
    """Registered pipelines only; older keys fall back to the default."""
    return PIPELINES.get(key or DEFAULT_PIPELINE, PIPELINES[DEFAULT_PIPELINE])


def editable_fields(pipeline_key: str) -> dict[tuple[str, str], Field]:
    return {(node.id, field.input): field for node in pipeline(pipeline_key).nodes for field in node.fields}


def linked_inputs(pipeline_key: str) -> set[tuple[str, str]]:
    return {key for key, field in editable_fields(pipeline_key).items() if field.param}


def describe_all() -> list[dict]:
    return [pipeline.describe() for pipeline in PIPELINES.values()]


def lora_preset(key: str | None) -> LoraPreset | None:
    return PRESET_INDEX.get(key or "")


def presets_for(pipeline_key: str, role: str | None = None) -> list[LoraPreset]:
    """The one-click LoRAs that make sense for this pipeline (and role)."""
    family = pipeline(pipeline_key).family
    return [preset for preset in LORA_PRESETS
            if family in preset.families and (role is None or preset.role == role)]


def preset_slots(pipeline_key: str) -> dict[str, str]:
    """Which LoRA node each role owns: the accelerator first, quality after it."""
    slots = pipeline(pipeline_key).lora_slots
    return {role: slots[index] for index, role in enumerate(PRESET_ROLES) if index < len(slots)}


def preset_role_of(lora_name: str | None) -> str | None:
    """The role of a preset by its file name, used to spot an active preset."""
    preset = next((item for item in LORA_PRESETS if item.lora == lora_name), None)
    return preset.role if preset else None


def param_node(pipeline_key: str, input_name: str) -> tuple[str, str] | None:
    """The node that owns a sampler/scheduler-style input, if the pipeline has one.

    Presets name the sampler ``sampler`` (the composer's wording) while the graph
    input is ``sampler_name``; the alias map keeps both spellings working.
    """
    wanted = INPUT_ALIASES.get(input_name, input_name)
    for node in pipeline(pipeline_key).nodes:
        for field in node.fields:
            if field.input == wanted:
                return node.id, field.input
    return None


INPUT_ALIASES: dict[str, str] = {"sampler": "sampler_name"}
SAMPLER_INPUTS: tuple[str, ...] = ("sampler", "scheduler")


def preset_sampler_values(pipeline_key: str) -> dict[str, set[str]]:
    """Sampler/scheduler values proposed by this pipeline's accelerators."""
    values: dict[str, set[str]] = {name: set() for name in SAMPLER_INPUTS}
    for preset in presets_for(pipeline_key, role="speed"):
        for name in SAMPLER_INPUTS:
            if preset.params.get(name):
                values[name].add(str(preset.params[name]))
    return values
