"""ComfyUI process control, workflow graphs, overrides and GPU telemetry.

Split out of ``image_jobs`` so the job runner stays about one generation cycle:
this module owns "is ComfyUI running, can I start it", "which graph does the
selected pipeline build and which inputs may be edited", and "how much VRAM is a
job using". Graph construction is per architecture family (Flux.2, Chroma,
SDXL/Pony) and every family reports where each composer parameter lives, so the
editor and the request parameters stay in sync across pipelines.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import subprocess
import time
# Used by restart(), whose "clean up a dead ComfyUI" path runs after every image cycle. Missing
# this import is not cosmetic: it raised NameError precisely when the service was recovering, and
# the gateway was left in its `error` state — which is what "网站没用" looked like from outside.
from contextlib import suppress
from pathlib import Path
# Used by restart() and stop(). restart()'s "clean up a dead ComfyUI" path runs after every image
# cycle, and stop() is what the page-close shutdown calls. Missing this import is not cosmetic:
# it raises NameError precisely when the service is recovering — the gateway was left in its
# `error` state once over this ("网站没用" from outside), and 2026-09-23 19:51 it was the one step
# of the page-close shutdown that silently did nothing (ComfyUI kept holding 8188).
import psutil

import httpx

from app.image import image_pipelines

LOGGER = logging.getLogger("image-jobs")

from app.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT
COMFY_ROOT = ROOT.parent / "ComfyUI-BSAI_v010"
COMFY_PYTHON = COMFY_ROOT / "python" / "python.exe"
COMFY_MAIN = COMFY_ROOT / "ComfyUI" / "main.py"
COMFY_LOG = ROOT / "logs" / "comfyui.log"
WORKFLOW_PATH = ROOT / "logs" / "image-workflow.json"
MODELS_ROOT = COMFY_ROOT / "ComfyUI" / "models"
COMFY_URL = "http://127.0.0.1:8188"
START_TIMEOUT = 300

# Flags mirror the validated H3 launcher (dynamic VRAM, latent previews, no
# pinned memory, no API nodes). The generate view starts the server in the
# background, so the browser is never launched and no Qwen process is stopped.
# The whitelist carries every node pack the shared ComfyUI has to serve: image
# generation and SeedVR2 upscaling need ComfyUI-GGUF, and the H3 video pipeline
# needs its own director/attention/cache packs. Verified together: 1382 nodes
# load with this list, and the image pipelines still resolve their nodes.
COMFY_FLAGS = [
    "--listen", "127.0.0.1", "--port", "8188", "--windows-standalone-build",
    "--disable-auto-launch", "--enable-dynamic-vram", "--fast-disk",
    "--reserve-vram", "0.8", "--preview-method", "latent2rgb",
    "--use-pytorch-cross-attention", "--disable-pinned-memory",
    "--disable-api-nodes", "--disable-all-custom-nodes",
    # argparse declares this with nargs='+', so each pack is its own argument: a
    # single space-joined string would be read as one (nonexistent) pack name and
    # every H3 node would silently vanish.
    "--whitelist-custom-nodes",
    "ComfyUI-GGUF",
    "ComfyUI-H3-Multishot",
    "ComfyUI-H3-Motion-Context",
    "ComfyUI-H3-LocalDirector",
    "ComfyUI-MiniMaxH3-FirstBlockCache",
    # The T8 pack arrived with the downloaded "4-step dual-clock" workflow: it adds the
    # H3 dual-clock sampler (low-step sigma grid, shift 12/3), the AV latent split/concat
    # and the AV decode nodes that workflow is built on.
    "minimax-h3-audio-T8",
    # Frame interpolation for the video view's post-process: RIFE VFI smooths a
    # 24 fps fight shot to 48 fps. taichi and the rife weights are already on this
    # machine, so the pack only has to be allowed to load.
    "ComfyUI-Frame-Interpolation",
    "ComfyUI-VideoHelperSuite",
    "ComfyUI-KJNodes",
    "rgthree-comfy",
    "ComfyUI_Comfyroll_CustomNodes",
    "ComfyUI-Easy-Use",
    "ComfyUI-Custom-Scripts",
]
COMFY_ENV = {
    "PYTHONUTF8": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1",
}
# CREATE_NO_WINDOW | DETACHED_PROCESS so the server outlives the gateway process
# and never flashes a console window on the desktop.
CREATE_FLAGS = 0x08000000 | 0x00000008

# Where each dropdown looks for its options, relative to the models folder.
CHOICE_FOLDERS = {
    "checkpoints": ("checkpoints",),
    "diffusion_models": ("diffusion_models", "unet"),
    "unet_gguf": ("unet", "diffusion_models"),
    "text_encoders": ("text_encoders", "clip"),
    "vae": ("vae",),
    "loras": ("loras",),
}

# -- workflow overrides -------------------------------------------------------
def linked_inputs(pipeline_key: str | None = None) -> set[tuple[str, str]]:
    """Node inputs owned by the composer parameters, not by the workflow file."""
    return image_pipelines.linked_inputs(pipeline_key or image_pipelines.DEFAULT_PIPELINE)


def overrides_stamp() -> tuple[int, int] | None:
    """Cheap fingerprint of the workflow-override file: (mtime_ns, size), or None.

    Callers that build a big description out of ``load_overrides()`` (the image panel asks for
    it on every progress poll - 4 Hz while a job runs) use this to skip both the file read and
    the JSON parse when nothing changed on disk.
    """
    try:
        stat = WORKFLOW_PATH.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def load_overrides() -> dict:
    """Saved edits per pipeline: ``{pipeline: {node_id: {input: value}}}``."""
    if not WORKFLOW_PATH.exists():
        return {}
    try:
        stored = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        LOGGER.warning("Workflow override file is unreadable; using defaults", exc_info=True)
        return {}
    if not isinstance(stored, dict):
        return {}
    # Older files stored the node map directly for the single Flux pipeline.
    if stored and all(key.isdigit() for key in stored):
        stored = {image_pipelines.DEFAULT_PIPELINE: stored}
    return {key: _without_linked(key, value) for key, value in stored.items() if isinstance(value, dict)}


def _without_linked(pipeline_key: str, overrides: dict) -> dict:
    linked = linked_inputs(pipeline_key)
    clean = {}
    for node_id, values in overrides.items():
        if not isinstance(values, dict):
            continue
        kept = {name: value for name, value in values.items() if (str(node_id), name) not in linked}
        if kept:
            clean[str(node_id)] = kept
    return clean


def pipeline_overrides(pipeline_key: str) -> dict:
    return load_overrides().get(pipeline_key, {})


def save_overrides(overrides: dict) -> None:
    WORKFLOW_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = WORKFLOW_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(WORKFLOW_PATH)


def save_pipeline_overrides(pipeline_key: str, overrides: dict) -> None:
    stored = load_overrides()
    if overrides:
        stored[pipeline_key] = overrides
    else:
        stored.pop(pipeline_key, None)
    save_overrides(stored)


# -- editable node model ------------------------------------------------------
def _coerce(field: image_pipelines.Field, value):
    if field.kind == "int":
        integer = int(float(value))
        low = int(field.minimum) if field.minimum is not None else integer
        high = int(field.maximum) if field.maximum is not None else integer
        return max(low, min(high, integer))
    if field.kind == "float":
        number = float(value)
        low = field.minimum if field.minimum is not None else number
        high = field.maximum if field.maximum is not None else number
        return max(low, min(high, number))
    if field.kind == "bool":
        return bool(value)
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field.label} 不能为空")
    return text


def validate_overrides(pipeline_key: str, payload: dict) -> dict:
    """Keep known, non-linked inputs for this pipeline and coerce their types."""
    fields = image_pipelines.editable_fields(pipeline_key)
    linked = linked_inputs(pipeline_key)
    pipeline = image_pipelines.pipeline(pipeline_key)
    titles = {node.id: node.title for node in pipeline.nodes}
    clean: dict[str, dict] = {}
    for node_id, values in (payload or {}).items():
        node_id = str(node_id)
        if not isinstance(values, dict) or node_id not in titles:
            continue
        accepted = {}
        for name, value in values.items():
            field = fields.get((node_id, name))
            if field is None or (node_id, name) in linked:
                continue
            try:
                accepted[name] = _coerce(field, value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{titles[node_id]} · {field.label}: {exc}") from exc
        if accepted:
            clean[node_id] = accepted
    return clean


def workflow_template(pipeline_key: str) -> list[dict]:
    """Editable node list for the pipeline, without resolved values."""
    return [node.describe() for node in image_pipelines.pipeline(pipeline_key).nodes]


def workflow_labels(pipeline_key: str) -> dict:
    """Composer parameter -> the node field that owns it, for UI syncing."""
    labels = {}
    for node in image_pipelines.pipeline(pipeline_key).nodes:
        for field in node.fields:
            if field.param:
                labels[field.param] = {"node": node.id, "input": field.input, "title": node.title, "label": field.label}
    return labels


# -- graph builders -----------------------------------------------------------
def _node(class_type: str, inputs: dict, title: str) -> dict:
    return {"class_type": class_type, "inputs": inputs, "title_hint": title}


def _ref(node_id: str, slot: int = 0):
    return [str(node_id), slot]


def normalize_seed(seed) -> int:
    try:
        value = int(seed)
    except (TypeError, ValueError) as exc:
        raise ValueError("seed must be an integer") from exc
    return secrets.randbits(63) if value < 0 else min(value, 2**64 - 1)


def _dimension(value, name: str) -> int:
    integer = int(value)
    if integer < 64 or integer > 8192:
        raise ValueError(f"{name} must be between 64 and 8192")
    return integer - (integer % 8)


def build_graph(
    pipeline_key: str,
    prompt: str,
    negative_prompt: str = "",
    references: list[str] | None = None,
    width: int = 1024,
    height: int = 1024,
    steps: int = 4,
    cfg: float = 1.0,
    denoise: float = 1.0,
    seed: int = -1,
    filename_prefix: str = "qwen/generated",
    overrides: dict | None = None,
    # Appended rather than inserted: every existing caller passes the arguments
    # above positionally, so a new parameter must go last or their values shift
    # onto the wrong names.
    scale: float | None = None,
    # SeedVR2's redraw ratio: 1.0 keeps its output as it is, below that the restored frames
    # are mixed back with the plain resize. Only the two SeedVR2 families read it.
    upscale_strength: float | None = None,
) -> dict:
    """Build the API graph for a pipeline and report where each parameter lives."""
    pipeline = image_pipelines.pipeline(pipeline_key)
    # An upscaler runs from an image, not from a prompt, so the prompt rule only
    # applies to the pipelines that actually sample text.
    if not pipeline.references_required and not (prompt or "").strip():
        raise ValueError("prompt must be a non-empty string")
    # Explicit edits (from the panel or a caller) win over the saved file, so the
    # builders and apply_overrides must see one merged map.
    merged_overrides = {**pipeline_overrides(pipeline_key), **(overrides or {})}
    # An upscaler takes its output size from the input image times the scale, so
    # there is no canvas to validate: the request's size is simply not its to set.
    if pipeline.references_required:
        canvas = {"width": None, "height": None}
    else:
        canvas = {"width": _dimension(width, "width"), "height": _dimension(height, "height")}
    parameters = {
        "pipeline": pipeline_key,
        **canvas,
        "steps": max(1, min(200, int(steps))),
        "cfg": float(cfg),
        # Only the upscalers declare a multiplier; the others never read it.
        **({"scale": float(scale)} if scale is not None and pipeline.references_required else {}),
        "denoise": max(0.0, min(1.0, float(denoise))),
        "seed": normalize_seed(seed),
    }
    if upscale_strength is not None and pipeline.family.startswith("seedvr2"):
        parameters["upscale_strength"] = max(0.0, min(1.0, float(upscale_strength)))
    if pipeline.family == "sdxl":
        graph = _build_sdxl(prompt, negative_prompt, references, parameters, filename_prefix, merged_overrides)
    elif pipeline.family == "sdxl_gguf":
        graph = _build_sdxl_gguf(prompt, negative_prompt, references, parameters, filename_prefix, merged_overrides)
    elif pipeline.family == "chroma":
        graph = _build_chroma(prompt, negative_prompt, references, parameters, filename_prefix, merged_overrides)
    elif pipeline.family == "seedvr2":
        graph = _build_seedvr2(prompt, negative_prompt, references, parameters, filename_prefix, merged_overrides)
    elif pipeline.family == "seedvr2_video":
        graph = _build_seedvr2_video(prompt, negative_prompt, references, parameters,
                                     filename_prefix, merged_overrides)
    elif pipeline.family == "qwen21":
        graph = _build_qwen21(prompt, negative_prompt, references, parameters, filename_prefix, merged_overrides)
    elif pipeline.family == "qwen21_turbo":
        graph = _build_qwen21_turbo(prompt, negative_prompt, references, parameters,
                                    filename_prefix, merged_overrides)
    else:
        graph = _build_flux2(prompt, references, parameters, filename_prefix, merged_overrides)
    apply_overrides(graph, merged_overrides, pipeline_key)
    return {"prompt": graph, "parameters": parameters}


def apply_overrides(graph: dict, overrides: dict, pipeline_key: str | None = None) -> dict:
    """Apply saved edits onto a built graph, skipping stale or linked inputs.

    The pipeline must be named: several pipelines share node ids (SDXL and the
    GGUF variant both sample in node 4), so guessing from the node ids would
    read the wrong editable-field map and silently drop every saved edit.
    """
    nodes = graph.get("prompt") if isinstance(graph.get("prompt"), dict) else graph
    if pipeline_key is None:
        for key, candidate in image_pipelines.PIPELINES.items():
            if set(nodes) & {node.id for node in candidate.nodes}:
                pipeline_key = key
                break
    fields = image_pipelines.editable_fields(pipeline_key) if pipeline_key else {}
    linked = image_pipelines.linked_inputs(pipeline_key) if pipeline_key else set()
    for node_id, values in (overrides or {}).items():
        node = nodes.get(str(node_id))
        if node is None:
            continue
        for name, value in values.items():
            field = fields.get((str(node_id), name))
            if field is None or (str(node_id), name) in linked:
                continue
            node["inputs"][name] = _coerce(field, value)
    return graph


def _build_flux2(prompt: str, references: list[str] | None, parameters: dict, filename_prefix: str, overrides: dict) -> dict:
    from deployment.flux2_klein_workflow import build_flux2_klein_prompt

    # Resolve every file input through the same rule as the other pipelines, so
    # the panel, the workflow preview and the graph always agree.
    graph = build_flux2_klein_prompt(
        prompt, reference_images=references or [], width=parameters["width"], height=parameters["height"],
        steps=parameters["steps"], cfg=parameters["cfg"], denoise=parameters["denoise"],
        seed=parameters["seed"], filename_prefix=filename_prefix,
    )["prompt"]
    graph["1"]["inputs"]["unet_name"] = _model_str("flux2", "unet_name", overrides)
    graph["2"]["inputs"]["clip_name"] = _model_str("flux2", "clip_name", overrides)
    graph["3"]["inputs"]["vae_name"] = _model_str("flux2", "vae_name", overrides)
    return graph


def _build_qwen21(prompt: str, negative_prompt: str, references: list[str] | None,
                  parameters: dict, filename_prefix: str, overrides: dict) -> dict:
    """Qwen-Image 2.1: the official text-to-image graph (int8 DiT, CFG 1, 25 steps).

    TextEncodeQwenImage21 both encodes the prompt (Qwen3-VL) and exposes the latent
    it was trained at, but the canvas still comes from EmptyLatentImage so the
    panel's width/height keep working. CFG stays at 1: the model is CFG-free, and
    raising it doubles the cost per step without the official recipe asking for it.
    """
    graph = {
        "1": _node("UNETLoader", {
            "unet_name": _model_str("qwen21", "unet_name", overrides),
            "weight_dtype": "default",
        }, "采样模型"),
        "2": _node("CLIPLoader", {
            "clip_name": _model_str("qwen21", "clip_name", overrides),
            "type": "qwen_image",
        }, "文本编码器"),
        "3": _node("VAELoader", {
            "vae_name": _model_str("qwen21", "vae_name", overrides),
        }, "VAE"),
        "4": _node("TextEncodeQwenImage21", {
            "clip": _ref("2"),
            "prompt": prompt,
            "negative_prompt": negative_prompt or "",
            "resolution": 2048 if max(parameters["width"] or 1024, parameters["height"] or 1024) > 1024 else 1024,
            "images": [_ref("20")] if references else [],
            "vae": _ref("3"),
        }, "提示词"),
        "5": _node("EmptyLatentImage", {
            "width": parameters["width"], "height": parameters["height"], "batch_size": 1,
        }, "画布尺寸"),
        "6": _node("KSampler", {
            "model": _ref("1"),
            "positive": ["4", 0],
            "negative": ["4", 1],
            "latent_image": _ref("5"),
            "seed": parameters["seed"],
            "steps": parameters["steps"],
            "cfg": parameters["cfg"],
            "sampler_name": parameters.get("sampler") or "euler",
            "scheduler": parameters.get("scheduler") or "simple",
            "denoise": parameters.get("denoise", 1.0),
        }, "采样"),
        "7": _node("VAEDecode", {"samples": _ref("6"), "vae": _ref("3")}, "解码"),
        "8": _node("SaveImage", {"images": _ref("7"), "filename_prefix": filename_prefix}, "保存图片"),
    }
    if references:
        # Reference in: encode it and sample from its latent, so the prompt only
        # redraws what the denoise level allows. A full-strength denoise would
        # throw the reference away, so it is clamped to a sane edit level.
        denoise = parameters.get("denoise", 1.0)
        graph["20"] = _node("LoadImage", {"image": references[0]}, "参考图")
        graph["21"] = _node("VAEEncode", {"pixels": _ref("20"), "vae": _ref("3")}, "编码参考图")
        graph["6"]["inputs"]["latent_image"] = _ref("21")
        graph["6"]["inputs"]["denoise"] = denoise if denoise < 0.999 else 0.65
    return graph


def _qwen_turbo_sigmas(denoise: float) -> str:
    """The official 8-step ladder, scaled down when starting from a reference."""
    ladder = [1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0.0]
    if denoise >= 0.999:
        return "1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0"
    scaled = [round(value * denoise, 6) for value in ladder]
    return ", ".join(f"{value:g}" for value in scaled)


def _build_qwen21_turbo(prompt: str, negative_prompt: str, references: list[str] | None,
                        parameters: dict, filename_prefix: str, overrides: dict) -> dict:
    """Qwen-Image 2.1 Turbo: the 8-step distilled graph from the official template.

    The checkpoint carries its own 8-step sigma ladder, so the sampler is
    SamplerCustomAdvanced fed by ManualSigmas rather than a plain KSampler; the
    panel's step count is informational. With a reference image the graph encodes
    it (VAEEncode) and scales the ladder down by the requested denoise, which is
    how img2img strength is expressed with a custom schedule. CFG stays at 1.
    """
    denoise = parameters.get("denoise", 1.0) if references else 1.0
    sigmas = (overrides.get("7", {}) or {}).get("sigmas") or _qwen_turbo_sigmas(denoise)
    graph = {
        "1": _node("UnetLoaderGGUF", {
            "unet_name": _model_str("qwen21_turbo", "unet_name", overrides),
        }, "采样模型（GGUF）"),
        "2": _node("CLIPLoader", {
            "clip_name": _model_str("qwen21_turbo", "clip_name", overrides),
            "type": "qwen_image",
        }, "文本编码器"),
        "3": _node("VAELoader", {
            "vae_name": _model_str("qwen21_turbo", "vae_name", overrides),
        }, "VAE"),
        "4": _node("TextEncodeQwenImage21", {
            "clip": _ref("2"),
            "prompt": prompt,
            "negative_prompt": negative_prompt or "",
            "resolution": 2048 if max(parameters["width"] or 1024, parameters["height"] or 1024) > 1024 else 1024,
            "images": [_ref("20")] if references else [],
            "vae": _ref("3"),
        }, "提示词"),
        "5": _node("EmptyLatentImage", {
            "width": parameters["width"], "height": parameters["height"], "batch_size": 1,
        }, "画布尺寸"),
        "6": _node("KSamplerSelect", {"sampler_name": parameters.get("sampler") or "euler"}, "采样器"),
        "7": _node("ManualSigmas", {"sigmas": sigmas}, "8 步 sigma 阶梯（官方）"),
        "8": _node("CFGGuider", {
            "model": _ref("1"),
            "positive": ["4", 0],
            "negative": ["4", 1],
            "cfg": parameters["cfg"],
        }, "提示词引导"),
        "9": _node("RandomNoise", {"noise_seed": parameters["seed"]}, "种子"),
        "10": _node("SamplerCustomAdvanced", {
            "noise": _ref("9"), "guider": _ref("8"), "sampler": _ref("6"),
            "sigmas": _ref("7"), "latent_image": _ref("5"),
        }, "采样"),
        "11": _node("VAEDecode", {"samples": _ref("10"), "vae": _ref("3")}, "解码"),
        "12": _node("SaveImage", {"images": _ref("11"), "filename_prefix": filename_prefix}, "保存图片"),
    }
    if references:
        graph["20"] = _node("LoadImage", {"image": references[0]}, "参考图")
        graph["21"] = _node("VAEEncode", {"pixels": _ref("20"), "vae": _ref("3")}, "编码参考图")
        graph["10"]["inputs"]["latent_image"] = _ref("21")
    return graph


def _choose_model(pipeline_key: str, input_name: str) -> str | None:
    """A saved choice wins, otherwise the pipeline's preferred local file.

    Returns ``None`` when nothing suitable exists: the graph is still built (so
    the panel can show a placeholder and ``pipeline_ready`` can report exactly
    which folder is missing) instead of failing deep inside a builder.
    """
    saved = pipeline_overrides(pipeline_key).get(_node_for_input(pipeline_key, input_name), {}).get(input_name)
    if saved:
        return saved
    try:
        return _default_model(pipeline_key, input_name)
    except RuntimeError:
        return None


def _missing_path(pipeline_key: str, input_name: str) -> str:
    folder = _choice_folder(pipeline_key, input_name)
    return f"（缺少 {folder} 模型）"


def _comfy_model_path(value: str) -> str:
    """The spelling ComfyUI's own loader dropdowns use for a model value.

    ``list_models`` builds names with ``as_posix()``, but ComfyUI reports and
    validates subfolder models with the platform separator, so a model inside a
    subfolder is rejected as "value not in list" unless it is spelled the same
    way. Models at the folder root have no separator and are unaffected.
    """
    return value.replace("/", os.sep)


def _model_str(pipeline_key: str, input_name: str, overrides: dict) -> str:
    """A node value: the explicit edit, the resolved file, or a marked placeholder."""
    explicit = overrides.get(_node_for_input(pipeline_key, input_name), {}).get(input_name)
    value = str(explicit or _choose_model(pipeline_key, input_name) or _missing_path(pipeline_key, input_name))
    return _comfy_model_path(value)


def _model_ref(pipeline_key: str, input_name: str, overrides: dict) -> tuple[str, bool]:
    """The value plus whether it is a real file (so builders can skip nodes)."""
    chosen = _choose_model(pipeline_key, input_name)
    explicit = overrides.get(_node_for_input(pipeline_key, input_name), {}).get(input_name)
    value = str(explicit or chosen or _missing_path(pipeline_key, input_name))
    return _comfy_model_path(value), bool(explicit or chosen)


def _build_chroma(prompt: str, negative_prompt: str, references: list[str] | None, parameters: dict, filename_prefix: str, overrides: dict) -> dict:
    graph: dict[str, dict] = {}
    graph["1"] = _node("UNETLoader", {"unet_name": _model_str("chroma", "unet_name", overrides), "weight_dtype": "default"}, "Chroma1-HD")
    graph["2"] = _node("CLIPLoader", {"clip_name": _model_str("chroma", "clip_name", overrides), "type": "chroma"}, "T5 text encoder")
    graph["3"] = _node("VAELoader", {"vae_name": _model_str("chroma", "vae_name", overrides)}, "FLUX VAE")
    graph["4"] = _node("CLIPTextEncode", {"clip": _ref("2"), "text": prompt}, "Positive prompt")
    graph["5"] = _node("CLIPTextEncode", {"clip": _ref("2"), "text": negative_prompt or ""}, "Negative prompt")
    graph["6"] = _node("EmptySD3LatentImage", {"width": parameters["width"], "height": parameters["height"], "batch_size": 1}, "Canvas")
    graph["7"] = _node("ModelSamplingAuraFlow", {"model": _ref("1"), "shift": 1.0}, "Sampling shift")
    graph["8"] = _node("KSampler", {
        "model": _ref("7"), "positive": _ref("4"), "negative": _ref("5"), "latent_image": _ref("6"),
        "seed": parameters["seed"], "steps": parameters["steps"], "cfg": parameters["cfg"],
        "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
    }, "Chroma sampling")
    decode_source = "8"
    if references:
        graph["20"] = _node("LoadImage", {"image": references[0]}, "Reference image")
        graph["21"] = _node("VAEEncode", {"pixels": _ref("20"), "vae": _ref("3")}, "Encode reference")
        graph["8"]["inputs"]["latent_image"] = _ref("21")
        graph["8"]["inputs"]["denoise"] = parameters["denoise"]
    graph["9"] = _node("VAEDecode", {"samples": _ref(decode_source), "vae": _ref("3")}, "Decode")
    graph["10"] = _node("SaveImage", {"images": _ref("9"), "filename_prefix": filename_prefix}, "Generated image")
    return graph


def _apply_sdxl_loras(graph: dict, overrides: dict, model_source: list, clip_source: list) -> tuple[list, list]:
    """Chain the SDXL LoRA slots (11 = accelerator, 12 = quality) onto the graph.

    Slots are only emitted when a LoRA is actually selected: ComfyUI rejects
    "None" as a lora_name, and an unused loader would load nothing. Returns the
    model/clip outputs of the last loader so the callers keep wiring the sampler
    and the text encoders to whatever comes last.
    """
    for node_id, title in (("11", "LoRA 1"), ("12", "LoRA 2")):
        values = overrides.get(node_id, {})
        lora = str(values.get("lora_name") or "").strip()
        if not lora or lora.lower() == "none":
            continue
        graph[node_id] = _node("LoraLoader", {
            "model": model_source, "clip": clip_source, "lora_name": lora,
            "strength_model": float(values.get("strength_model", 1.0)),
            "strength_clip": float(values.get("strength_clip", 1.0)),
        }, title)
        model_source, clip_source = _ref(node_id, 0), _ref(node_id, 1)
    return model_source, clip_source


def _build_sdxl(prompt: str, negative_prompt: str, references: list[str] | None, parameters: dict, filename_prefix: str, overrides: dict) -> dict:
    graph: dict[str, dict] = {}
    ckpt, has_ckpt = _model_ref("sdxl", "ckpt_name", overrides)
    graph["10"] = _node("CheckpointLoaderSimple", {"ckpt_name": ckpt}, "SDXL checkpoint")
    model_source, clip_source = _ref("10", 0), _ref("10", 1)
    model_source, clip_source = _apply_sdxl_loras(graph, overrides, model_source, clip_source)
    graph["1"] = _node("CLIPTextEncode", {"clip": clip_source, "text": prompt}, "Positive prompt")
    graph["2"] = _node("CLIPTextEncode", {"clip": clip_source, "text": negative_prompt or _default_negative()}, "Negative prompt")
    graph["3"] = _node("EmptyLatentImage", {
        "width": parameters["width"], "height": parameters["height"], "batch_size": 1,
    }, "Canvas")
    latent_source = _ref("3")
    if references and has_ckpt:
        graph["20"] = _node("LoadImage", {"image": references[0]}, "Reference image")
        graph["21"] = _node("VAEEncode", {"pixels": _ref("20"), "vae": _ref("10", 2)}, "Encode reference")
        latent_source = _ref("21")
    graph["4"] = _node("KSampler", {
        "model": model_source, "positive": _ref("1"), "negative": _ref("2"), "latent_image": latent_source,
        "seed": parameters["seed"], "steps": parameters["steps"], "cfg": parameters["cfg"],
        "sampler_name": "dpmpp_2m", "scheduler": "karras",
        "denoise": parameters["denoise"] if references and has_ckpt else 1.0,
    }, "SDXL sampling")
    graph["5"] = _node("VAEDecode", {"samples": _ref("4"), "vae": _ref("10", 2)}, "Decode")
    graph["6"] = _node("SaveImage", {"images": _ref("5"), "filename_prefix": filename_prefix}, "Generated image")
    return graph


def _default_negative() -> str:
    return ("lowres, bad anatomy, bad hands, extra digits, fewer digits, cropped, worst quality, "
            "low quality, jpeg artifacts, watermark, signature, text")


def _build_seedvr2(prompt: str, negative_prompt: str, references: list[str] | None, parameters: dict, filename_prefix: str, overrides: dict) -> dict:
    """SeedVR2 restore/upscale. The input image is the conditioning, not a prompt.

    The sampler and the conditioning node must read the *same* latent object.
    ComfyUI runs a node once per element of its longest list input and repeats the
    last element of every shorter list, so a conditioning built from a different
    latent gets paired with the first chunk and then reused, and the model rejects
    it with "conditioning shape must match latent batch/temporal/spatial
    dimensions". Feeding both from node 6 keeps them aligned.

    An empty reference list is allowed so the panel can render this pipeline's
    graph before an image is chosen; running it without one is refused in
    ``image_jobs``, which is the layer that owns the request contract.
    """
    graph: dict[str, dict] = {}
    unet, has_unet = _model_ref("seedvr2", "unet_name", overrides)
    vae, has_vae = _model_ref("seedvr2", "vae_name", overrides)
    if not has_unet or not has_vae:
        raise ValueError("SeedVR2 放大缺少模型文件：需要 SeedVR2 3B int8 与配套 VAE。")

    scale = _seedvr2_scale(overrides, "seedvr2", parameters)
    graph["7"] = _node("UNETLoader", {"unet_name": unet, "weight_dtype": "default"}, "SeedVR2 3B int8")
    graph["5"] = _node("VAELoader", {"vae_name": vae}, "SeedVR2 VAE")
    graph["1"] = _node("LoadImage", {"image": references[0] if references else ""}, "Reference image")
    # ResizeImageMaskNode is a dynamic-combo node: the child widget's API key is
    # the dotted path, not the bare child name.
    graph["3"] = _node("ResizeImageMaskNode", {
        "input": _ref("1"), "resize_type": "scale by multiplier",
        "resize_type.multiplier": scale, "scale_method": "lanczos",
    }, f"Resize x{scale:g}")
    graph["4"] = _node("SeedVR2Preprocess", {"resized_images": _ref("3")}, "Pre-process")
    graph["6"] = _node("VAEEncodeTiled", {
        "pixels": _ref("4"), "vae": _ref("5"),
        **_seedvr2_tiling(overrides, "6", tile=512, overlap=128),
    }, "VAE encode")
    graph["8"] = _node("SeedVR2Conditioning", {
        "model": _ref("7"), "vae_conditioning": _ref("6"),
    }, "Conditioning")
    graph["10"] = _node("KSampler", {
        "model": _ref("7"), "positive": _ref("8", 0), "negative": _ref("8", 1),
        "latent_image": _ref("6"), "seed": parameters["seed"], "steps": parameters["steps"],
        "cfg": parameters["cfg"], "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
    }, "SeedVR2 sample")
    graph["12"] = _node("VAEDecodeTiled", {
        "samples": _ref("10"), "vae": _ref("5"),
        **_seedvr2_tiling(overrides, "12", tile=512, overlap=128),
    }, "VAE decode")
    graph["13"] = _node("SeedVR2PostProcessing", {
        "images": _ref("12"), "original_resized_images": _ref("3"),
        "color_correction_method": str(overrides.get("13", {}).get("color_correction_method") or "wavelet"),
    }, "Colour match")
    graph["15"] = _node("SaveImage", {"images": _seedvr2_redraw(graph, parameters),
                                      "filename_prefix": filename_prefix}, "Restored image")
    return graph


def _seedvr2_redraw(graph: dict, parameters: dict) -> list:
    """Where the restored frames go after the redraw-ratio mix, if one was asked for.

    SeedVR2 re-synthesises texture frame by frame, which is what makes hair, eyes and fabric
    "boil" in motion even when the geometry is stable (measured: texture-band flicker 5.33
    against 4.70 for a plain lanczos resize of the same clip). Mixing its output back with the
    resize the graph already computed is the only strength control the model offers, so
    `upscale_strength` below 1 blends the two.
    """
    strength = float((parameters or {}).get("upscale_strength") or 1.0)
    if strength >= 1.0:
        return _ref("13")
    graph["16"] = _node("ImageBlend", {
        "image1": _ref("13"), "image2": _ref("3"),
        "blend_factor": max(0.0, min(1.0, strength)), "blend_mode": "normal",
    }, f"Redraw {strength:g}")
    return _ref("16")


def _seedvr2_scale_default(pipeline_key: str = "seedvr2") -> float:
    return float(image_pipelines.pipeline(pipeline_key).defaults.get("scale", 1.0))


def _seedvr2_scale(overrides: dict, pipeline_key: str, parameters: dict | None = None) -> float:
    """The multiplier in force: the request wins, then the saved file, then the default.

    The composer owns it like steps/CFG, so a "1.25×" recipe really runs at 1.25×
    instead of inheriting whatever the workflow file was left with.
    """
    requested = (parameters or {}).get("scale")
    if isinstance(requested, (int, float)) and not isinstance(requested, bool) and 1.0 <= float(requested) <= 4.0:
        return float(requested)
    return float(overrides.get("3", {}).get("resize_type.multiplier", _seedvr2_scale_default(pipeline_key)))


def _build_seedvr2_video(prompt: str, negative_prompt: str, references: list[str] | None,
                         parameters: dict, filename_prefix: str, overrides: dict) -> dict:
    """SeedVR2 over a whole clip.

    Reads the clip the panel selected through the video source route rather than a
    reference image name, and routes the encoded latent through the temporal
    chunk/merge pair the way the model requires: both the conditioning node and
    the sampler must read the *same* chunk list, because ComfyUI runs a node once
    per list element and repeats the last element of any shorter list.
    """
    unet, has_unet = _model_ref("seedvr2_video", "unet_name", overrides)
    vae, has_vae = _model_ref("seedvr2_video", "vae_name", overrides)
    if not has_unet or not has_vae:
        raise ValueError("SeedVR2 视频放大缺少模型文件：需要 SeedVR2 3B int8 与配套 VAE。")

    from app.video import video_routes
    # An empty source is allowed so the panel can render this pipeline's graph
    # before a clip is chosen; starting a run without one is refused by the job
    # layer, which owns the request contract.
    source = references[0] if references else (video_routes._SOURCE.get("path") or "")

    scale = _seedvr2_scale(overrides, "seedvr2_video", parameters)
    graph: dict[str, dict] = {}
    graph["7"] = _node("UNETLoader", {"unet_name": unet, "weight_dtype": "default"}, "SeedVR2 3B int8")
    graph["5"] = _node("VAELoader", {"vae_name": vae}, "SeedVR2 VAE")
    graph["1"] = _node("LoadVideo", {"file": source}, "Source clip")
    graph["2"] = _node("GetVideoComponents", {"video": _ref("1")}, "Split frames / audio")
    graph["3"] = _node("ResizeImageMaskNode", {
        "input": _ref("2", 0), "resize_type": "scale by multiplier",
        "resize_type.multiplier": scale, "scale_method": "lanczos",
    }, f"Resize x{scale:g}")
    graph["4"] = _node("SeedVR2Preprocess", {"resized_images": _ref("3")}, "Pre-process")
    graph["6"] = _node("VAEEncodeTiled", {
        "pixels": _ref("4"), "vae": _ref("5"),
        **_seedvr2_tiling(overrides, "6", tile=512, overlap=128),
    }, "VAE encode")
    # auto keeps the whole clip in one chunk while it fits, which is what keeps the
    # conditioning shape aligned with the sampler's latent.
    graph["9"] = _node("SeedVR2TemporalChunk", {
        "latent": _ref("6"), "chunking_mode": "auto",
        "temporal_overlap": int(overrides.get("9", {}).get("temporal_overlap") or 0),
    }, "Split latent")
    graph["8"] = _node("SeedVR2Conditioning", {
        "model": _ref("7"), "vae_conditioning": _ref("9", 0),
    }, "Conditioning on the same chunks")
    graph["10"] = _node("KSampler", {
        "model": _ref("7"), "positive": _ref("8", 0), "negative": _ref("8", 1),
        "latent_image": _ref("9", 0), "seed": parameters["seed"], "steps": parameters["steps"],
        "cfg": parameters["cfg"], "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
    }, "SeedVR2 sample")
    graph["11"] = _node("SeedVR2TemporalMerge", {
        "latents": _ref("10"), "temporal_overlap": _ref("9", 1),
    }, "Merge chunks")
    graph["12"] = _node("VAEDecodeTiled", {
        "samples": _ref("11"), "vae": _ref("5"),
        **_seedvr2_tiling(overrides, "12", tile=512, overlap=128),
    }, "VAE decode")
    graph["13"] = _node("SeedVR2PostProcessing", {
        "images": _ref("12"), "original_resized_images": _ref("3"),
        "color_correction_method": str(overrides.get("13", {}).get("color_correction_method") or "wavelet"),
    }, "Colour match")
    graph["14"] = _node("CreateVideo", {
        "images": _seedvr2_redraw(graph, parameters), "audio": _ref("2", 1),
        "fps": _ref("2", 2), "bit_depth": _ref("2", 3),
    }, "Assemble")
    graph["15"] = _node("SaveVideo", {
        "video": _ref("14"), "filename_prefix": filename_prefix, "format": "auto", "codec": "auto",
    }, "Restored clip")
    return graph


def _seedvr2_tiling(overrides: dict, node_id: str, tile: int, overlap: int) -> dict:
    """Tiling knobs for one VAE node, clamped so a typo cannot ask for nonsense."""
    values = overrides.get(node_id, {})
    return {
        "tile_size": max(256, min(2048, int(values.get("tile_size") or tile))),
        "overlap": max(0, min(512, int(values.get("overlap") or overlap))),
        "temporal_size": max(8, min(256, int(values.get("temporal_size") or 64))),
        "temporal_overlap": max(0, min(64, int(values.get("temporal_overlap") or 8))),
    }


def _build_sdxl_gguf(prompt: str, negative_prompt: str, references: list[str] | None, parameters: dict, filename_prefix: str, overrides: dict) -> dict:
    """SDXL family via ComfyUI-GGUF: quantized UNet plus the two SDXL CLIPs."""
    graph: dict[str, dict] = {}
    unet, has_unet = _model_ref("sdxl_gguf", "unet_name", overrides)
    clip_l, has_clip_l = _model_ref("sdxl_gguf", "clip_name1", overrides)
    clip_g, has_clip_g = _model_ref("sdxl_gguf", "clip_name2", overrides)
    vae, has_vae = _model_ref("sdxl_gguf", "vae_name", overrides)
    graph["10"] = _node("UnetLoaderGGUF", {"unet_name": unet}, "SDXL UNet (GGUF)")
    graph["13"] = _node("DualCLIPLoader", {
        "clip_name1": clip_l, "clip_name2": clip_g, "type": "sdxl",
    }, "SDXL CLIP-L + CLIP-G")
    clip_source = _ref("13", 0)
    # The GGUF UNet has no bundled VAE, so one is always loaded.
    graph["14"] = _node("VAELoader", {"vae_name": vae}, "VAE")
    model_source = _ref("10", 0)
    model_source, clip_source = _apply_sdxl_loras(graph, overrides, model_source, clip_source)
    graph["1"] = _node("CLIPTextEncode", {"clip": clip_source, "text": prompt}, "Positive prompt")
    graph["2"] = _node("CLIPTextEncode", {"clip": clip_source, "text": negative_prompt or _default_negative()}, "Negative prompt")
    graph["3"] = _node("EmptyLatentImage", {
        "width": parameters["width"], "height": parameters["height"], "batch_size": 1,
    }, "Canvas")
    latent_source = _ref("3")
    if references and has_unet and has_vae:
        graph["20"] = _node("LoadImage", {"image": references[0]}, "Reference image")
        graph["21"] = _node("VAEEncode", {"pixels": _ref("20"), "vae": _ref("14")}, "Encode reference")
        latent_source = _ref("21")
    graph["4"] = _node("KSampler", {
        "model": model_source, "positive": _ref("1"), "negative": _ref("2"), "latent_image": latent_source,
        "seed": parameters["seed"], "steps": parameters["steps"], "cfg": parameters["cfg"],
        "sampler_name": "dpmpp_2m", "scheduler": "karras",
        "denoise": parameters["denoise"] if references and has_unet and has_vae else 1.0,
    }, "SDXL sampling")
    graph["5"] = _node("VAEDecode", {"samples": _ref("4"), "vae": _ref("14")}, "Decode")
    graph["6"] = _node("SaveImage", {"images": _ref("5"), "filename_prefix": filename_prefix}, "Generated image")
    return graph


def list_models(kind: str) -> list[str]:
    """Model files ComfyUI can see for a dropdown, from the models folders."""
    names: list[str] = []
    for folder in CHOICE_FOLDERS.get(kind, ()):
        base = MODELS_ROOT / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.safetensors")) + sorted(base.rglob("*.ckpt")) + sorted(base.rglob("*.gguf")):
            if path.name.startswith("put_"):
                continue
            relative = path.relative_to(base).as_posix()
            if relative not in names:
                names.append(relative)
    return names


# What ComfyUI itself offers per kind, cached briefly. A value that is absent
# here is rejected by /prompt validation, so this is the authoritative list.
_COMFY_CHOICES: dict[str, list[str]] = {}
_COMFY_CHOICES_AT: dict[str, float] = {}
CHOICE_SOURCES = {
    "checkpoints": ("CheckpointLoaderSimple", "ckpt_name"),
    "diffusion_models": ("UNETLoader", "unet_name"),
    "unet_gguf": ("UnetLoaderGGUF", "unet_name"),
    "text_encoders": ("CLIPLoader", "clip_name"),
    "vae": ("VAELoader", "vae_name"),
    "loras": ("LoraLoader", "lora_name"),
}


async def comfy_choices(client: httpx.AsyncClient, kind: str, ttl: float = 60.0) -> list[str]:
    """Dropdown values exactly as ComfyUI spells them, falling back to disk."""
    source = CHOICE_SOURCES.get(kind)
    if source is None:
        return []
    cls, input_name = source
    now = time.time()
    if kind in _COMFY_CHOICES and now - _COMFY_CHOICES_AT.get(kind, 0) < ttl:
        return _COMFY_CHOICES[kind]
    try:
        response = await client.get(f"{COMFY_URL}/object_info/{cls}", timeout=5)
        response.raise_for_status()
        required = response.json()[cls]["input"]["required"]
        options = required.get(input_name) or [[]]
        values = [str(value) for value in (options[0] if options else []) if isinstance(value, str)]
    except (httpx.HTTPError, OSError, ValueError, KeyError, IndexError, TypeError):
        values = []
    if not values:
        values = list_models(kind)
    _COMFY_CHOICES[kind] = values
    _COMFY_CHOICES_AT[kind] = now
    return values


def _default_model(pipeline_key: str, input_name: str) -> str:
    """Prefer a saved choice, then the declared default, then anything present."""
    saved = pipeline_overrides(pipeline_key).get(_node_for_input(pipeline_key, input_name), {}).get(input_name)
    if saved:
        return saved
    available = list_models(_choice_folder(pipeline_key, input_name))
    candidates = _preferred_names(pipeline_key, input_name)
    for needle in candidates:
        for name in available:
            if needle in name.lower():
                return name
    if not available:
        raise RuntimeError(
            f"{image_pipelines.pipeline(pipeline_key).name} 缺少 {input_name} 对应的模型文件，"
            f"请先下载到 {_model_folder(input_name)}"
        )
    # A folder can hold unrelated files (segmentation, audio, video). Guessing one
    # of those would fail deep inside ComfyUI, so refuse instead.
    raise RuntimeError(
        f"{image_pipelines.pipeline(pipeline_key).name} 在 {_model_folder(input_name)} 里"
        f"没有可用的 {input_name}（现有：{', '.join(available[:4])}）。请在“工作流”卡片里选择，"
        f"或把模型放到 {_model_folder(input_name)}"
    )


def file_integrity(path: Path) -> str | None:
    """Why a downloaded model file cannot be used, or ``None`` when it looks fine.

    A safetensors header states the exact byte length its tensors need, so a
    truncated download is detectable without hashing the whole file.
    """
    if not path.exists():
        return "文件不存在"
    if path.suffix != ".safetensors":
        return "文件为空" if path.stat().st_size == 0 else None
    try:
        with path.open("rb") as handle:
            header_len = int.from_bytes(handle.read(8), "little")
            if header_len <= 0 or header_len > 512 * 1024 * 1024:
                return "文件头损坏（可能下载中断）"
            header = json.loads(handle.read(header_len).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return "文件无法读取"
    largest = 0
    for key, value in header.items():
        if key == "__metadata__" or not isinstance(value, dict):
            continue
        offsets = value.get("data_offsets")
        if isinstance(offsets, list) and len(offsets) == 2:
            largest = max(largest, int(offsets[1]))
    expected = 8 + header_len + largest
    actual = path.stat().st_size
    if actual < expected:
        return f"文件不完整（缺 {(expected - actual) / 1024 / 1024:.1f} MB）"
    if actual > expected:
        return f"文件比预期大 {(actual - expected) / 1024 / 1024:.1f} MB"
    return None


def model_file(pipeline_key: str, input_name: str) -> Path | None:
    """The path behind a resolved dropdown value, if it exists on disk."""
    chosen = _choose_model(pipeline_key, input_name)
    if not chosen:
        return None
    folder = _choice_folder(pipeline_key, input_name)
    for base in CHOICE_FOLDERS.get(folder, (folder,)):
        candidate = MODELS_ROOT / base / chosen.replace("\\", "/")
        if candidate.exists():
            return candidate
    return None


def integrity_warnings(pipeline_key: str) -> list[dict]:
    """Damaged files among the pipeline's resolved models."""
    findings: list[dict] = []
    for node in image_pipelines.pipeline(pipeline_key).nodes:
        for field in node.fields:
            if field.choices not in {"checkpoints", "diffusion_models", "unet_gguf", "text_encoders", "vae"}:
                continue
            candidate = model_file(pipeline_key, field.input)
            if candidate is None:
                continue
            problem = file_integrity(candidate)
            if problem:
                findings.append({"field": field.input, "file": candidate.name, "problem": problem})
    return findings


def _model_folder(input_name: str) -> Path:
    """The real folder behind a dropdown kind, for actionable error messages."""
    for kind, (class_type, name) in CHOICE_SOURCES.items():
        if name == input_name:
            folders = CHOICE_FOLDERS.get(kind) or (kind,)
            return MODELS_ROOT / folders[0]
    return MODELS_ROOT


def _preferred_names(pipeline_key: str, input_name: str) -> tuple[str, ...]:
    return {
        ("sdxl", "ckpt_name"): ("cyberrealisticpony", "cyberrealistic", "lustify", "pony", "illustrious",
                                "noobai", "epicrealism", "realvis", "juggernaut", "dreamshaper", "sdxl"),
        ("sdxl_gguf", "unet_name"): ("cyberrealistic", "pony", "illustrious", "noobai", "lustify", "sdxl"),
        ("sdxl_gguf", "clip_name1"): ("clip_l", "clip-l"),
        ("sdxl_gguf", "clip_name2"): ("clip_g", "clip-g"),
        ("sdxl_gguf", "vae_name"): ("sdxl_vae", "sdxl-vae", "vae-ft", "sdxl"),
        ("chroma", "unet_name"): ("chroma",),
        ("chroma", "clip_name"): ("t5xxl", "t5"),
        ("chroma", "vae_name"): ("chroma_ae", "ae.safetensors", "flux"),
        ("flux2", "unet_name"): ("flux-2-klein", "flux2", "flux"),
        ("flux2", "clip_name"): ("qwen_3_8b", "qwen3_8b"),
        ("flux2", "vae_name"): ("full_encoder_small_decoder",),
        ("qwen21", "unet_name"): ("qwen_image_2.1_int8_convrot", "qwen_image_2.1", "qwen_image"),
        ("qwen21", "clip_name"): ("qwen3vl_8b_w4a8", "qwen3vl_8b_int8", "qwen3vl_8b"),
        ("qwen21", "vae_name"): ("qwen_image_2.1_vae_bf16", "qwen_image_2.1_vae", "qwen_image_vae"),
        ("qwen21_turbo", "unet_name"): ("qwen-image-2.1-turbo-q8_0", "qwen_image_2.1_turbo", "2.1-turbo", "turbo"),
        ("qwen21_turbo", "clip_name"): ("qwen3vl_8b_w4a8", "qwen3vl_8b_int8", "qwen3vl_8b"),
        ("qwen21_turbo", "vae_name"): ("qwen_image_2.1_vae_bf16", "qwen_image_2.1_vae", "qwen_image_vae"),
        ("seedvr2", "unet_name"): ("seedvr2_3b_int8", "seedvr2_3b", "seedvr2"),
        ("seedvr2", "vae_name"): ("seedvr2_ema_vae", "seedvr2"),
        # The video pipeline loads the same two files; the key differs only because
        # _preferred_names is looked up per pipeline.
        ("seedvr2_video", "unet_name"): ("seedvr2_3b_int8", "seedvr2_3b", "seedvr2"),
        ("seedvr2_video", "vae_name"): ("seedvr2_ema_vae", "seedvr2"),
    }.get((pipeline_key, input_name), ())


def _choice_folder(pipeline_key: str, input_name: str) -> str:
    for node in image_pipelines.pipeline(pipeline_key).nodes:
        for field in node.fields:
            if field.input == input_name and field.choices:
                return field.choices
    return "checkpoints"


def _node_for_input(pipeline_key: str, input_name: str) -> str:
    for node in image_pipelines.pipeline(pipeline_key).nodes:
        for field in node.fields:
            if field.input == input_name:
                return node.id
    return ""


def describe_graph(graph: dict, pipeline_key: str) -> list[dict]:
    """Node list for display: execution order with the inputs that will run."""
    pipeline = image_pipelines.pipeline(pipeline_key)
    order = {node.id: index for index, node in enumerate(pipeline.nodes)}
    titles = {node.id: node.title for node in pipeline.nodes}
    described = []
    for node_id, node in (graph.get("prompt") if "prompt" in graph else graph).items():
        described.append({
            "id": str(node_id),
            "class_type": node.get("class_type"),
            "title": titles.get(str(node_id)) or node.get("class_type"),
            "inputs": {name: value for name, value in node.get("inputs", {}).items() if not isinstance(value, list)},
        })
    described.sort(key=lambda item: order.get(item["id"], len(order)))
    return described


# -- ComfyUI process ----------------------------------------------------------
class ComfySupervisor:
    """Starts ComfyUI on demand and reports whether it is already serving."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.process: subprocess.Popen | None = None
        self.started_at: float | None = None
        self.last_error: str | None = None
        # settings.json controls both switches; the defaults match the shipped
        # behaviour (start ComfyUI on demand, wait up to five minutes).
        self.autostart = True
        self.start_timeout = float(START_TIMEOUT)

    async def status(self) -> dict:
        try:
            response = await self.client.get(f"{COMFY_URL}/system_stats", timeout=3)
            response.raise_for_status()
            stats = response.json()
        except (httpx.HTTPError, OSError, ValueError):
            return {"reachable": False, "managed": self.owns_process(), "error": self.last_error}
        system = (stats.get("system") or {}) if isinstance(stats, dict) else {}
        devices = stats.get("devices") or []
        device = devices[0] if devices else {}
        return {
            "reachable": True,
            "managed": self.owns_process(),
            "argv": system.get("argv"),
            "version": system.get("comfyui_version"),
            "device": device.get("name"),
            "vram_total_mib": round((device.get("vram_total") or 0) / 1024 / 1024, 1) or None,
            "vram_free_mib": round((device.get("vram_free") or 0) / 1024 / 1024, 1) or None,
            "error": self.last_error,
        }

    def owns_process(self) -> bool:
        return bool(self.process and self.process.poll() is None)

    def command(self) -> list[str]:
        return [str(COMFY_PYTHON), "-s", "-u", str(COMFY_MAIN), *COMFY_FLAGS]

    def available(self) -> str | None:
        for path in (COMFY_PYTHON, COMFY_MAIN):
            if not path.exists():
                return f"未找到 {path}"
        return None

    def spawn(self) -> None:
        missing = self.available()
        if missing:
            raise RuntimeError(f"无法自动启动 ComfyUI：{missing}")
        if self.owns_process():
            return
        COMFY_LOG.parent.mkdir(parents=True, exist_ok=True)
        handle = open(COMFY_LOG, "ab", buffering=0)
        environment = os.environ.copy()
        environment.update(COMFY_ENV)
        self.process = subprocess.Popen(
            self.command(), cwd=str(COMFY_ROOT), stdout=handle, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=environment, creationflags=CREATE_FLAGS,
        )
        self.started_at = time.time()
        self.last_error = None
        LOGGER.info("Started ComfyUI pid=%s", self.process.pid)

    def restart(self) -> bool:
        """Terminate a wedged ComfyUI so it can be started again on demand.

        Only a process that runs this checkout's ``ComfyUI/main.py`` is touched,
        so an unrelated local server is never killed. Returns whether something
        was actually stopped.
        """
        stopped = False
        owned = self.process
        if owned is not None and owned.poll() is None:
            with suppress(OSError):
                owned.terminate()
                owned.wait(timeout=15)
            stopped = True
        self.process = None
        self.started_at = None
        expected_main = str(COMFY_MAIN)
        for candidate in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                command = candidate.info.get("cmdline") or []
                if candidate.info["pid"] == os.getpid() or expected_main not in command:
                    continue
                candidate.terminate()
                candidate.wait(timeout=15)
                stopped = True
                LOGGER.warning("Stopped a ComfyUI from this checkout pid=%s",
                               candidate.info["pid"])
            except (psutil.Error, OSError):
                continue
        if stopped:
            self.last_error = "ComfyUI 已重启（上一次执行卡死）"
        return stopped

    def stop(self, grace: float = 15.0) -> bool:
        """Shut ComfyUI down for good (页面关闭后的自动收尾用)，返回是否真的停了什么。

        与 `restart()` 共用同一套"只碰本 checkout 的进程"的判定，区别只有一处：这里不留下
        "已重启"的提示——这不是重启，是收工；下次要出图时 `ensure_running()` 会照常把它拉起来。
        """
        stopped = self.restart()
        if stopped:
            self.last_error = None
            LOGGER.warning("ComfyUI stopped for the page-close shutdown")
        return stopped

    async def ensure_running(self, timeout: float | None = None, on_note=None) -> dict:
        """Return status once ComfyUI answers, starting it when it is not running."""
        state = await self.status()
        if state["reachable"]:
            return state
        if not self.autostart:
            raise RuntimeError(f"ComfyUI 未运行（{COMFY_URL}），自动启动已在 settings.json 中关闭。")
        if self.available():
            raise RuntimeError(f"ComfyUI 未运行且无法自动启动：{self.available()}")
        if on_note:
            on_note("starting_comfy")
        self.spawn()
        deadline = time.monotonic() + (timeout or self.start_timeout)
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            state = await self.status()
            if state["reachable"]:
                LOGGER.info("ComfyUI is ready after %.1f s", time.time() - (self.started_at or time.time()))
                return state
            if self.process and self.process.poll() is not None:
                self.last_error = f"ComfyUI 进程已退出（代码 {self.process.returncode}），日志：{COMFY_LOG}"
                raise RuntimeError(self.last_error)
        self.last_error = f"ComfyUI 启动超时（{int(timeout or self.start_timeout)} 秒），日志：{COMFY_LOG}"
        raise RuntimeError(self.last_error)


# -- GPU telemetry ------------------------------------------------------------
class GpuMonitor:
    """VRAM totals and per-process attribution from nvidia-smi.

    Sampling happens alongside the job's queue polling, so nothing here owns a
    task: one nvidia-smi call per queue tick is enough for a per-second graph.
    """

    # 生图面板在作业期间每 250 ms 轮询一次进度，而每次采样都要起一个 nvidia-smi（本机实测
    # 52 ms）。一秒钟一个样本就够画那张"每秒"的图，所以两次采样之间复用上一次读数。
    SAMPLE_TTL_SECONDS = 1.0

    def __init__(self):
        self.baseline: float | None = None
        self.release_baseline: float | None = None
        self.release_peak: float = 0.0
        self.peak_mib: float = 0.0
        self.latest: dict | None = None
        self._timeline: list[dict] = []
        self._sample: dict | None = None
        self._sample_at: float = 0.0

    @staticmethod
    async def _run(args: list[str]) -> str:
        process = await asyncio.create_subprocess_exec(
            "nvidia-smi", *args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        output, _ = await process.communicate()
        return output.decode("utf-8", "replace")

    async def sample(self) -> dict | None:
        now = time.monotonic()
        if self._sample is not None and (now - self._sample_at) < self.SAMPLE_TTL_SECONDS:
            return self._sample
        try:
            raw = await self._run(["--query-gpu=memory.total,memory.used,memory.free,utilization.gpu",
                                   "--format=csv,noheader,nounits"])
        except (OSError, asyncio.SubprocessError):
            return None
        first = raw.strip().splitlines()[0] if raw.strip() else ""
        parts = [part.strip() for part in first.split(",")]
        if len(parts) < 4:
            return None
        try:
            total, used, free, utilization = (float(value) for value in parts[:4])
        except ValueError:
            return None
        self._sample = {"total_mib": total, "used_mib": used, "free_mib": free,
                        "utilization": utilization}
        self._sample_at = now
        return self._sample

    async def processes(self) -> list[dict]:
        try:
            raw = await self._run(["--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader"])
        except (OSError, asyncio.SubprocessError):
            return []
        entries = []
        for line in raw.strip().splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 3:
                continue
            memory = parts[2].split()[0]
            entries.append({
                "pid": int(parts[0]) if parts[0].isdigit() else None,
                "name": Path(parts[1]).name,
                "used_mib": float(memory) if memory.isdigit() else 0.0,
            })
        return entries

    def begin(self, statistics: dict | None) -> None:
        """Record the pre-job baseline, while the language model is still loaded."""
        self._timeline = []
        self.peak_mib = statistics["used_mib"] if statistics else 0.0
        self.baseline = statistics["used_mib"] if statistics else None
        self.release_baseline = None
        self.release_peak = 0.0
        self.latest = statistics

    def release(self, statistics: dict | None) -> None:
        """Re-baseline after the language model is unloaded.

        At that point the remaining VRAM is what the image job itself allocates,
        which is the number worth showing on a 12 GB card. The peak since the
        pre-job baseline stays available as the whole-job impact.
        """
        if statistics is None:
            return
        self.release_baseline = statistics["used_mib"]
        self.release_peak = statistics["used_mib"]
        self.latest = statistics

    def observe(self, statistics: dict | None) -> dict | None:
        if statistics is None:
            return self.latest
        self.peak_mib = max(self.peak_mib, statistics["used_mib"])
        if self.release_baseline is not None:
            self.release_peak = max(self.release_peak, statistics["used_mib"])
        self.latest = statistics
        self._timeline.append({"at": round(time.time(), 1), "used_mib": statistics["used_mib"]})
        del self._timeline[:-600]
        return statistics

    def freeze(self, statistics: dict | None) -> None:
        """Stop tracking the image footprint once ComfyUI frees its models.

        Sampling the post-free VRAM would report a footprint of zero right when
        the user looks at the finished result, so the measured peak is kept.
        """
        if statistics is not None:
            self.latest = statistics
        self.release_baseline = None

    def snapshot(self) -> dict | None:
        if self.latest is None:
            return None
        used = self.latest["used_mib"]
        job_mib = round(max(0.0, used - self.baseline), 1) if self.baseline is not None else 0.0
        peak_job = round(max(0.0, self.peak_mib - self.baseline), 1) if self.baseline is not None else 0.0
        snapshot = {
            **self.latest,
            "job_mib": job_mib,
            "job_peak_mib": peak_job,
            "peak_mib": round(self.peak_mib, 1),
            "baseline_mib": self.baseline,
            "samples": len(self._timeline),
        }
        if self.release_baseline is not None or self.release_peak:
            baseline = self.release_baseline if self.release_baseline is not None else self.latest["used_mib"]
            snapshot["image_peak_mib"] = round(max(0.0, self.release_peak - baseline), 1)
            snapshot["image_mib"] = round(max(0.0, used - baseline), 1) if self.release_baseline is not None else snapshot["image_peak_mib"]
            snapshot["released_baseline_mib"] = baseline
        return snapshot

    def timeline(self) -> list[dict]:
        return list(self._timeline)
