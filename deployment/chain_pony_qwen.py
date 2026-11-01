"""Chain test: Pony (SDXL) generates, then Qwen-Image 2.1 edits the result.

Proves two things at once:
  * the local SDXL/Pony pipeline renders suggestive (non-explicit) content;
  * the two architectures can be used together -- Pony for the base image and
    Qwen 2.1 for a reference-based refinement pass.

Qwen's edit path is the official 2.1 graph: TextEncodeQwenImage21 takes the
reference image through `images` and also emits the latent to sample from.

    python chain_pony_qwen.py
"""

from __future__ import annotations

import json
import shutil
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, r"D:\AI")

import image_comfy  # noqa: E402

COMFY = "http://127.0.0.1:8188"
OUT = Path(r"D:\AI\Qwen-Local\logs\turbo-test")
INPUT = Path(r"D:\AI\ComfyUI-BSAI_v010\ComfyUI\input")
SEED = 20250912

PROMPT_PONY = ("photo of a beautiful young adult woman in black lace lingerie, sitting on a bed, "
               "soft window light, elegant boudoir photography, detailed skin, 50mm lens")
PROMPT_QWEN = ("keep the same woman, pose and composition; refine skin texture, balance the lighting, "
               "add subtle film grain, keep it photorealistic")


def submit(graph: dict, tag: str) -> dict:
    started = time.time()
    with httpx.Client(base_url=COMFY, timeout=180) as client:
        response = client.post("/prompt", json={"prompt": graph, "client_id": uuid.uuid4().hex})
        if response.status_code != 200:
            return {"ok": False, "error": response.text[:400]}
        prompt_id = response.json()["prompt_id"]
        item = None
        while time.time() - started < 1200:
            time.sleep(1.5)
            item = client.get(f"/history/{prompt_id}").json().get(prompt_id)
            if item:
                break
        if not item:
            return {"ok": False, "error": "timeout"}
        status = item.get("status", {})
        images = [image for output in (item.get("outputs") or {}).values()
                  for image in (output.get("images") or [])]
        if status.get("status_str") != "success" or not images:
            errors = [p.get("exception_message", "")[:200] for k, p in status.get("messages", [])
                      if k == "execution_error"]
            return {"ok": False, "error": str(errors)[:400]}
        name = images[0]
        blob = client.get("/view", params={"filename": name["filename"],
                                           "subfolder": name.get("subfolder", ""),
                                           "type": name.get("type", "output")}).content
    path = OUT / f"chain-{tag}.png"
    path.write_bytes(blob)
    return {"ok": True, "path": str(path), "seconds": round(time.time() - started, 1),
            "filename": name["filename"], "subfolder": name.get("subfolder", "")}


def main() -> int:
    # 1) Pony
    pony = image_comfy.build_graph("sdxl", PROMPT_PONY, width=768, height=768, steps=8,
                                   cfg=1.5, seed=SEED, filename_prefix="chain/pony")["prompt"]
    result = submit(pony, "1-pony")
    print("pony:", json.dumps(result, ensure_ascii=False)[:300], flush=True)
    if not result["ok"]:
        return 1

    # 2) hand the file to ComfyUI's input folder for LoadImage
    filename = Path(result["filename"]).name
    shutil.copyfile(result["path"], INPUT / filename)

    # 3) Qwen 2.1 edit
    graph = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": "qwen_image_2.1_int8_convrot.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": "qwen3vl_8b_w4a8.safetensors", "type": "qwen_image"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
        "4": {"class_type": "LoadImage", "inputs": {"image": filename}},
        "5": {"class_type": "TextEncodeQwenImage21",
              "inputs": {"clip": ["2", 0], "prompt": PROMPT_QWEN, "negative_prompt": "",
                         "resolution": 768, "images": [["4", 0]], "vae": ["3", 0]}},
        "6": {"class_type": "VAEEncode", "inputs": {"pixels": ["4", 0], "vae": ["3", 0]}},
        "7": {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "positive": ["5", 0], "negative": ["5", 1],
                         "latent_image": ["6", 0], "seed": SEED, "steps": 25, "cfg": 1.0,
                         "sampler_name": "euler", "scheduler": "simple", "denoise": 0.55}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "chain/qwen-edit"}},
    }
    edited = submit(graph, "2-qwen-edit")
    print("qwen-edit:", json.dumps(edited, ensure_ascii=False)[:300], flush=True)
    return 0 if edited["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
