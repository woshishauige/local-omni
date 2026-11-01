"""Build API-format Flux.2 Klein workflows for text and image generation.

The stock example workflow contains two permanently connected ``LoadImage``
nodes.  That is convenient in the editor, but an API request with one or zero
images leaves an invalid IMAGE socket.  This builder creates only the branches
needed for the request, so all three supported input shapes are valid:

* text-to-image (no references)
* image-to-image with one reference
* image-to-image with two references

The returned object is ready to POST to ``/prompt``.  ``prompt`` is expected to
be the final English visual prompt produced by Qwen3.8-27B; callers may use
``QWEN_PROMPT_SYSTEM`` as the system contract for that expansion step.
"""

from __future__ import annotations

import secrets
from typing import Any, Mapping, Sequence


DEFAULT_UNET = "flux-2-klein-9b-fp8.safetensors"
DEFAULT_CLIP = "qwen_3_8b_fp8mixed.safetensors"
DEFAULT_VAE = "full_encoder_small_decoder.safetensors"

# This contract is deliberately model-agnostic: Qwen writes the prompt while
# Klein receives the resulting string through CLIPTextEncode.
QWEN_PROMPT_SYSTEM = (
    "You are the prompt adapter for Flux.2 Klein image generation. "
    "Convert the user's request into one concise English visual prompt. "
    "Describe subject, action, composition, camera/viewpoint, lighting, "
    "materials and style when relevant. Preserve explicit constraints and "
    "reference-image relationships. When the user's request involves a human "
    "figure, write the prompt in a tasteful, mildly attractive and sensual "
    "manner: emphasize the appealing physique, skin texture, confident pose "
    "and soft seductive atmosphere, while keeping the person fully clothed. "
    "Output only the prompt text, without Markdown, labels, JSON, commentary, "
    "or a negative prompt."
)


def _node(class_type: str, inputs: Mapping[str, Any], title: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"class_type": class_type, "inputs": dict(inputs)}
    if title:
        entry["_meta"] = {"title": title}
    return entry


def _ref(node_id: str, slot: int = 0) -> list[Any]:
    return [str(node_id), slot]


def build_flux2_klein_prompt(
    prompt: str,
    *,
    reference_images: Sequence[str] | None = None,
    width: int = 1024,
    height: int = 1024,
    steps: int = 4,
    cfg: float = 1.0,
    denoise: float = 1.0,
    seed: int = 0,
    sampler_name: str = "euler",
    megapixels: float = 1.0,
    unet_name: str = DEFAULT_UNET,
    clip_name: str = DEFAULT_CLIP,
    vae_name: str = DEFAULT_VAE,
    filename_prefix: str = "flux2-klein/generated",
) -> dict[str, Any]:
    """Return a valid ComfyUI API prompt for 0, 1, or 2 references.

    ``reference_images`` contains ComfyUI input filenames, not local paths.
    Extra entries are rejected rather than silently ignored, while ``None``
    and an empty sequence both select text-to-image mode.
    """

    refs = list(reference_images or ())
    if len(refs) > 2:
        raise ValueError("Flux.2 Klein accepts at most two reference images")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    width = _multiple_of_8(width, "width")
    height = _multiple_of_8(height, "height")
    try:
        normalized_steps = int(steps)
    except (TypeError, ValueError) as exc:
        raise ValueError("steps must be an integer") from exc
    if not 1 <= normalized_steps <= 200:
        raise ValueError("steps must be between 1 and 200")
    try:
        normalized_cfg = float(cfg)
    except (TypeError, ValueError) as exc:
        raise ValueError("cfg must be a number") from exc
    if not 0.0 <= normalized_cfg <= 20.0:
        raise ValueError("cfg must be between 0 and 20")
    if not 0.0 < float(denoise) <= 1.0:
        raise ValueError("denoise must be greater than 0 and at most 1")
    # Flux.2 Klein's current ``Flux2Scheduler`` API has no denoise input;
    # retain the validated field for a stable UI/API contract and let future
    # scheduler revisions consume it without breaking callers.
    if not 0.01 <= float(megapixels) <= 16.0:
        raise ValueError("megapixels must be between 0.01 and 16")

    # The UI convention ``seed=-1`` means "random" while ComfyUI's
    # RandomNoise node accepts only an unsigned integer.  Resolve that
    # convention here so API callers cannot submit an invalid graph.
    try:
        normalized_seed = int(seed)
    except (TypeError, ValueError) as exc:
        raise ValueError("seed must be an integer") from exc
    if normalized_seed < 0:
        normalized_seed = secrets.randbits(63)
    if normalized_seed > 18446744073709551615:
        raise ValueError("seed must be between -1 and 18446744073709551615")

    graph: dict[str, dict[str, Any]] = {}
    graph["1"] = _node("UNETLoader", {"unet_name": unet_name, "weight_dtype": "default"}, "Flux.2 Klein 9B")
    graph["2"] = _node("CLIPLoader", {"clip_name": clip_name, "type": "flux2", "device": "default"}, "Klein Qwen text encoder")
    graph["3"] = _node("VAELoader", {"vae_name": vae_name}, "Flux.2 VAE")
    graph["4"] = _node("CLIPTextEncode", {"clip": _ref("2"), "text": prompt}, "Positive prompt")
    graph["5"] = _node("ConditioningZeroOut", {"conditioning": _ref("4")}, "Empty negative")
    graph["6"] = _node("EmptyFlux2LatentImage", {"width": width, "height": height, "batch_size": 1}, "Output canvas")
    graph["7"] = _node("RandomNoise", {"noise_seed": normalized_seed}, "Seed")
    graph["8"] = _node("KSamplerSelect", {"sampler_name": sampler_name}, "Sampler")
    graph["9"] = _node("Flux2Scheduler", {"steps": normalized_steps, "width": width, "height": height}, "Flux.2 schedule")

    # Chain each actual reference into conditioning.  No placeholder image is
    # created for absent inputs, which is what makes text-only requests valid.
    conditioning: list[Any] = _ref("4")
    next_id = 20
    for index, image_name in enumerate(refs, start=1):
        load_id, scale_id, encode_id, ref_id = str(next_id), str(next_id + 1), str(next_id + 2), str(next_id + 3)
        graph[load_id] = _node("LoadImage", {"image": image_name}, f"Reference image {index}")
        graph[scale_id] = _node(
            "ImageScaleToTotalPixels",
            {"image": _ref(load_id), "upscale_method": "lanczos", "megapixels": float(megapixels), "resolution_steps": 1},
            f"Scale reference {index}",
        )
        graph[encode_id] = _node("VAEEncode", {"pixels": _ref(scale_id), "vae": _ref("3")}, f"Encode reference {index}")
        graph[ref_id] = _node(
            "ReferenceLatent", {"conditioning": conditioning, "latent": _ref(encode_id)},
            f"Apply reference {index}",
        )
        conditioning = _ref(ref_id)
        next_id += 4

    graph["10"] = _node(
        "CFGGuider",
        {"model": _ref("1"), "positive": conditioning, "negative": _ref("5"), "cfg": normalized_cfg},
        "CFG guider",
    )
    graph["11"] = _node(
        "SamplerCustomAdvanced",
        {"noise": _ref("7"), "guider": _ref("10"), "sampler": _ref("8"), "sigmas": _ref("9"), "latent_image": _ref("6")},
        "Flux.2 sampling",
    )
    graph["12"] = _node("VAEDecode", {"samples": _ref("11"), "vae": _ref("3")}, "Decode")
    graph["13"] = _node("SaveImage", {"images": _ref("12"), "filename_prefix": filename_prefix}, "Generated image")
    return {"prompt": graph}


def _multiple_of_8(value: int, name: str) -> int:
    try:
        integer = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if integer < 64 or integer > 8192 or integer % 8:
        raise ValueError(f"{name} must be a multiple of 8 between 64 and 8192")
    return integer


def write_flux2_klein_prompt(path: str, prompt: str, **kwargs: Any) -> None:
    """Write a generated API prompt JSON file (small convenience for scripts)."""
    import json
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(build_flux2_klein_prompt(prompt, **kwargs), handle, ensure_ascii=False, indent=2)


__all__ = ["QWEN_PROMPT_SYSTEM", "build_flux2_klein_prompt", "write_flux2_klein_prompt"]
