"""Build ComfyUI UI-format workflow JSON files for the recommended SDXL recipe.

The gateway keeps its *own* configuration in ``Qwen-Local/logs/image-workflow.json``
(node overrides in API format), which is why nothing showed up in ComfyUI's
``user/default/workflows`` folder. These files are the converse: complete
ComfyUI-frontend workflows (nodes, links, positions, widgets) for the recipe
"CyberRealisticPony V18 fp16 + LCM-LoRA 8 steps + photorealism slider 1.5", in a
text-to-image and an image-to-image flavour.

Widget order is taken from ComfyUI's own ``/object_info`` so the files load and
run without hand-tuning.

    python build_sdxl_workflows.py [--comfy http://127.0.0.1:8188] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import requests

COMFY = "http://127.0.0.1:8188"
OUT = Path(r"D:\AI\ComfyUI-BSAI_v010\ComfyUI\user\default\workflows")

CHECKPOINT = "CyberRealisticPony_V18.0_F16.safetensors"
LCM_LORA = "lcm-lora-sdxl.safetensors"
SLIDER_LORA = "sdxl_photorealistic_slider_v1-0.safetensors"
POSITIVE = ("photo of a handsome young asian man, athletic build, natural sunlight, "
            "detailed skin texture, looking at camera, 50mm portrait")
NEGATIVE = "lowres, bad anatomy, bad hands, watermark, text"
RECIPE = "8 步 / CFG 1.5 / lcm + sgm_uniform"

# name -> (class_type, widgets, [(link input name, source node key, source slot)])
# Link inputs are filled in from the source node's output slot index.
TXT2IMG = [
    ("ckpt", "CheckpointLoaderSimple", [CHECKPOINT], []),
    ("lcm", "LoraLoader", [LCM_LORA, 1.0, 1.0], [("model", "ckpt", 0), ("clip", "ckpt", 1)]),
    ("slider", "LoraLoader", [SLIDER_LORA, 1.5, 1.5], [("model", "lcm", 0), ("clip", "lcm", 1)]),
    ("positive", "CLIPTextEncode", [POSITIVE], [("clip", "slider", 1)]),
    ("negative", "CLIPTextEncode", [NEGATIVE], [("clip", "slider", 1)]),
    ("latent", "EmptyLatentImage", [832, 1216, 1], []),
    ("sampler", "KSampler", [0, "randomize", 8, 1.5, "lcm", "sgm_uniform", 1.0],
     [("model", "slider", 0), ("positive", "positive", 0), ("negative", "negative", 0),
      ("latent_image", "latent", 0)]),
    ("decode", "VAEDecode", [], [("samples", "sampler", 0), ("vae", "ckpt", 2)]),
    ("save", "SaveImage", ["sdxl-pony/写实-LCM8"], [("images", "decode", 0)]),
]

IMG2IMG = [
    ("ckpt", "CheckpointLoaderSimple", [CHECKPOINT], []),
    ("lcm", "LoraLoader", [LCM_LORA, 1.0, 1.0], [("model", "ckpt", 0), ("clip", "ckpt", 1)]),
    ("slider", "LoraLoader", [SLIDER_LORA, 1.5, 1.5], [("model", "lcm", 0), ("clip", "lcm", 1)]),
    ("positive", "CLIPTextEncode", [POSITIVE], [("clip", "slider", 1)]),
    ("negative", "CLIPTextEncode", [NEGATIVE], [("clip", "slider", 1)]),
    # LoadImage declares a single widget (the upload button is frontend-only).
    ("image", "LoadImage", ["example.png"], []),
    ("encode", "VAEEncode", [], [("pixels", "image", 0), ("vae", "ckpt", 2)]),
    # denoise 0.55 keeps the pose and framing while redrawing skin and light.
    ("sampler", "KSampler", [0, "randomize", 8, 1.5, "lcm", "sgm_uniform", 0.55],
     [("model", "slider", 0), ("positive", "positive", 0), ("negative", "negative", 0),
      ("latent_image", "encode", 0)]),
    ("decode", "VAEDecode", [], [("samples", "sampler", 0), ("vae", "ckpt", 2)]),
    ("save", "SaveImage", ["sdxl-pony/写实-LCM8-图生图"], [("images", "decode", 0)]),
]

PAGE_X, PAGE_Y, COLUMN, ROW = 40, 60, 320, 190


def object_info(comfy: str) -> dict:
    response = requests.get(f"{comfy}/object_info", timeout=60)
    response.raise_for_status()
    return response.json()


def widget_values(class_type: str, info: dict, wanted: list) -> list:
    """Pad/trim the wanted widget values to ComfyUI's declared widget order.

    The frontend stores widgets in ``input.required`` order, with an extra
    ``control_after_generate`` entry after any seed widget. Values that are not
    provided fall back to the node's own default, so a recipe only has to name the
    widgets it actually changes.
    """
    spec = info[class_type]["input"]
    order: list[str] = []
    for name, definition in list(spec.get("required", {}).items()) + list(spec.get("optional", {}).items()):
        kind = definition[0] if isinstance(definition, list) and definition else definition
        if isinstance(kind, str) and kind.isupper() and kind not in {"STRING", "INT", "FLOAT", "BOOLEAN", "COMBO"}:
            continue                      # a link input, not a widget
        if isinstance(kind, list):
            order.append(name)            # a combo widget
            continue
        order.append(name)
        if name in {"seed", "noise_seed"}:
            order.append("control_after_generate")
    if len(wanted) > len(order):
        raise SystemExit(f"{class_type}: {len(wanted)} values for {len(order)} widgets ({order})")
    values = list(wanted) + [None] * (len(order) - len(wanted))
    for index, value in enumerate(values):
        if value is not None:
            continue
        name = order[index]
        if name == "control_after_generate":
            values[index] = "randomize"
            continue
        definition = spec.get("required", {}).get(name) or spec.get("optional", {}).get(name)
        kind = definition[0] if isinstance(definition, list) and definition else definition
        if isinstance(kind, list) and kind:
            values[index] = kind[0]        # first combo option
        elif isinstance(kind, str) and kind in {"INT", "FLOAT"}:
            options = definition[1] if len(definition) > 1 else {}
            values[index] = options.get("default", 0)
        else:
            values[index] = ""
    return values


def build(name: str, title: str, spec: list, info: dict, note: str) -> dict:
    """Turn a compact spec into a ComfyUI frontend workflow."""
    ids = {key: index + 1 for index, (key, *_rest) in enumerate(spec)}
    link_id = 0
    links: list[list] = []
    nodes: list[dict] = []
    for index, (key, class_type, wanted, inputs) in enumerate(spec):
        node_id = ids[key]
        node_inputs = []
        for slot, (input_name, source, source_slot) in enumerate(inputs):
            link_id += 1
            source_id = ids[source]
            type_spec = info[class_type]["input"]["required"].get(input_name) \
                or info[class_type]["input"]["optional"][input_name]
            link_type = type_spec[0] if isinstance(type_spec, list) else type_spec
            node_inputs.append({"name": input_name, "type": link_type, "link": link_id})
            links.append([link_id, source_id, source_slot, node_id, slot, link_type])
        output_types = info[class_type].get("output") or []
        output_names = info[class_type].get("output_name") or output_types
        nodes.append({
            "id": node_id,
            "type": class_type,
            "pos": [PAGE_X + (index % 3) * COLUMN, PAGE_Y + (index // 3) * ROW],
            "size": [280, 110],
            "flags": {},
            "order": index,
            "mode": 0,
            "inputs": node_inputs or None,
            "outputs": [
                {"name": output_names[slot] if slot < len(output_names) else str(slot),
                 "type": output_type, "links": sorted(
                     link[0] for link in links if link[1] == node_id and link[2] == slot) or None,
                 "slot_index": slot}
                for slot, output_type in enumerate(output_types)
            ],
            "properties": {"Node name for S&R": class_type},
            "widgets_values": widget_values(class_type, info, wanted),
        })
    note_id = len(nodes) + 1
    nodes.insert(0, {
        "id": note_id,
        "type": "MarkdownNote",
        "pos": [PAGE_X, PAGE_Y - 150],
        "size": [700, 130],
        "flags": {},
        "order": 0,
        "mode": 0,
        "inputs": None,
        "outputs": None,
        "title": "配方说明",
        "properties": {},
        "widgets_values": [note],
        "color": "#432",
        "bgcolor": "#653",
    })
    for link in links:
        for node in nodes:
            if node["id"] == link[1] and node["outputs"]:
                output = node["outputs"][link[2]]
                output["links"] = sorted(set((output.get("links") or []) + [link[0]]))
    return {
        "id": name,
        "revision": 0,
        "last_node_id": max(node["id"] for node in nodes),
        "last_link_id": link_id,
        "nodes": nodes,
        "links": links,
        "groups": [{
            "id": 1, "title": title, "bounding": [PAGE_X - 20, PAGE_Y - 20, 3 * COLUMN, 4 * ROW],
            "color": "#3f789e", "font_size": 24, "flags": {},
        }],
        "config": {},
        "extra": {"ds": {"scale": 0.75, "offset": [0, 0]}},
        "version": 0.4,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--comfy", default=COMFY)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    info = object_info(args.comfy)
    missing = [cls for _key, cls, _w, _i in TXT2IMG + IMG2IMG if cls not in info]
    if missing:
        raise SystemExit(f"ComfyUI does not know these nodes: {sorted(set(missing))}")

    note = (f"**{CHECKPOINT}** + `{LCM_LORA}`(1.0) + `{SLIDER_LORA}`(1.5)\\n\\n"
            f"{RECIPE}，负面提示词真正参与采样。LCM 只能配它的步数与 CFG："
            f"改成 28 步 / CFG 6 会出高对比度色块。\\n\\n"
            f"图生图变体把 EmptyLatentImage 换成 LoadImage + VAEEncode，"
            f"denoise 0.55 保持构图只重绘细节；要更像原图就调低 denoise。")
    for name, spec, title, filename in (
        ("sdxl-pony-realistic-txt2img", TXT2IMG, "SDXL/Pony 写实 · LCM 8 步 + 写实滑杆（文生图）",
         "sdxl-pony-写实-LCM8-文生图.json"),
        ("sdxl-pony-realistic-img2img", IMG2IMG, "SDXL/Pony 写实 · LCM 8 步 + 写实滑杆（图生图 denoise 0.55）",
         "sdxl-pony-写实-LCM8-图生图.json"),
    ):
        workflow = build(name, title, spec, info, note)
        target = OUT / filename
        print(f"{filename}: {len(workflow['nodes'])} nodes, {len(workflow['links'])} links"
              + (" (dry run)" if args.dry_run else ""))
        if not args.dry_run:
            OUT.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(workflow, ensure_ascii=False, indent=2), encoding="utf-8")
            print("  ->", target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
