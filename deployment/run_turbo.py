"""Run the Qwen-Image 2.1 Turbo graph through ComfyUI and report speed/VRAM.

Follows the official Turbo template: UnetLoaderGGUF (the uncensored Q8_0 build) +
CLIPLoader(type qwen_image, w4a8 encoder) + VAELoader + TextEncodeQwenImage21
(which also produces the latent) + CFGGuider(cfg 1) + KSamplerSelect(euler) +
ManualSigmas(8-step ladder) + SamplerCustomAdvanced + VAEDecode + SaveImage.

    python run_turbo.py --seed 20250912 --size 1024
    python run_turbo.py --seed 777 --size 1024 --steps-plan "1.0, 0.9581, ..." --tag shared
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import uuid
from pathlib import Path

import httpx

COMFY = "http://127.0.0.1:8188"
OUT = Path(r"D:\AI\Qwen-Local\logs\turbo-test")
UNET = "Qwen-Image-2.1-Turbo-FP8-ComfyUI.safetensors"
ENCODER = "Qwen-Image-2.1-Turbo-Uncensored-Q8_0.gguf"
VAE = "qwen_image_2.1_vae_bf16.safetensors"
SIGMAS = "1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0"
PROMPT = ("photo of a handsome young asian man, athletic build, natural sunlight, "
          "detailed skin texture, looking at camera, 50mm portrait")


def vram_mib() -> int:
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=30)
    return int(result.stdout.strip().splitlines()[0])


def build_graph(seed: int, size: int, sigmas: str) -> dict:
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": UNET, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoaderGGUF", "inputs": {"clip_name": ENCODER, "type": "qwen_image"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "4": {"class_type": "TextEncodeQwenImage21",
              "inputs": {"clip": ["2", 0], "prompt": PROMPT, "negative_prompt": "",
                         "resolution": size, "images": [], "vae": ["3", 0]}},
        "5": {"class_type": "CFGGuider",
              "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["4", 1], "cfg": 1.0}},
        "6": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "7": {"class_type": "ManualSigmas", "inputs": {"sigmas": sigmas}},
        "8": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "9": {"class_type": "SamplerCustomAdvanced",
              "inputs": {"noise": ["8", 0], "guider": ["5", 0], "sampler": ["6", 0],
                         "sigmas": ["7", 0], "latent_image": ["4", 2]}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["3", 0]}},
        "11": {"class_type": "SaveImage", "inputs": {"images": ["10", 0], "filename_prefix": "qwen/turbo-test"}},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=20250912)
    parser.add_argument("--size", type=int, default=1024)
    parser.add_argument("--sigmas", default=SIGMAS)
    parser.add_argument("--tag", default="")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    graph = build_graph(args.seed, args.size, args.sigmas)
    baseline = vram_mib()
    peak = baseline
    started = time.time()
    with httpx.Client(timeout=120) as client:
        client_id = uuid.uuid4().hex
        response = client.post(f"{COMFY}/prompt", json={"prompt": graph, "client_id": client_id})
        if response.status_code != 200:
            print("submit failed:", response.status_code, response.text[:600])
            return 1
        prompt_id = response.json()["prompt_id"]
        print("prompt_id:", prompt_id, "| baseline VRAM", baseline, "MiB", flush=True)
        history = None
        while time.time() - started < 1800:
            time.sleep(2)
            peak = max(peak, vram_mib())
            try:
                item = client.get(f"{COMFY}/history/{prompt_id}", timeout=20).json().get(prompt_id)
            except httpx.HTTPError:
                continue
            if item:
                history = item
                break
        if history is None:
            print("timeout waiting for the job")
            return 1
        wall = round(time.time() - started, 1)
        status = history.get("status", {})
        images = [image for output in (history.get("outputs") or {}).values()
                  for image in (output.get("images") or [])]
        saved = None
        if images:
            name = images[0]
            blob = client.get(f"{COMFY}/view", params={"filename": name["filename"],
                                                       "subfolder": name.get("subfolder", ""),
                                                       "type": name.get("type", "output")}).content
            saved = OUT / f"turbo-seed{args.seed}{('-' + args.tag) if args.tag else ''}.png"
            saved.write_bytes(blob)

    execution = None
    for kind, payload in (status.get("messages") or []):
        if kind == "execution_start":
            execution = payload.get("timestamp")
    last = (status.get("messages") or [])[-1][1].get("timestamp") if status.get("messages") else None
    print(json.dumps({
        "model": "Qwen-Image-2.1-Turbo-Uncensored-Q8_0", "seed": args.seed, "size": args.size,
        "sigmas": args.sigmas, "status": status.get("status_str"),
        "wall_seconds": wall, "execution_ms": (last - execution) if execution and last else None,
        "vram_baseline_mib": baseline, "vram_peak_mib": peak, "vram_delta_mib": peak - baseline,
        "image": str(saved) if saved else None,
    }, ensure_ascii=False, indent=2))
    return 0 if status.get("status_str") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
