"""Run Qwen-Image 2.1 (base or Turbo) through ComfyUI and report speed/VRAM.

Two graph shapes, following the two official templates:

  base   Qwen-Image-2.1: KSampler(25 steps, cfg 1, euler, simple)
  turbo  Qwen-Image-2.1-Turbo: SamplerCustomAdvanced + ManualSigmas(8-step ladder),
         CFGGuider(cfg 1), KSamplerSelect(euler)

Both use TextEncodeQwenImage21 (which also builds the latent) + CLIPLoader type
`qwen_image`. The encoder can be a safetensors file (official int8/w4a8) or a GGUF
(the abliterated Qwen3-VL build) through CLIPLoaderGGUF.

    python run_qwen.py --mode base --encoder qwen3vl_8b_w4a8.safetensors --seed 20250912
    python run_qwen.py --mode turbo --encoder-gguf Qwen-Image-2.1-Turbo-Uncensored-Q8_0.gguf --seed 20250912
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
VAE = "qwen_image_2.1_vae_bf16.safetensors"
BASE_UNET = "qwen_image_2.1_int8_convrot.safetensors"
TURBO_SIGMAS = "1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0"
PROMPT = ("photo of a handsome young asian man, athletic build, natural sunlight, "
          "detailed skin texture, looking at camera, 50mm portrait")


def vram_mib() -> int:
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=30)
    return int(result.stdout.strip().splitlines()[0])


def build_graph(args) -> dict:
    graph: dict = {
        "1": ({"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": args.unet}}
               if args.unet_loader == "UnetLoaderGGUF" else
               {"class_type": "UNETLoader",
                "inputs": {"unet_name": args.unet, "weight_dtype": "default"}}),
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "4": {"class_type": "TextEncodeQwenImage21",
              "inputs": {"clip": ["2", 0], "prompt": PROMPT, "negative_prompt": "",
                         "resolution": args.size, "images": [], "vae": ["3", 0]}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["3", 0]}},
        "11": {"class_type": "SaveImage",
               "inputs": {"images": ["10", 0], "filename_prefix": "qwen/test"}},
    }
    if args.encoder_gguf:
        graph["2"] = {"class_type": "CLIPLoaderGGUF",
                      "inputs": {"clip_name": args.encoder_gguf, "type": "qwen_image"}}
    else:
        graph["2"] = {"class_type": "CLIPLoader",
                      "inputs": {"clip_name": args.encoder, "type": "qwen_image"}}
    if args.mode == "turbo":
        graph.update({
            "5": {"class_type": "CFGGuider",
                  "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["4", 1],
                             "cfg": 1.0}},
            "6": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
            "7": {"class_type": "ManualSigmas", "inputs": {"sigmas": TURBO_SIGMAS}},
            "8": {"class_type": "RandomNoise", "inputs": {"noise_seed": args.seed}},
            "9": {"class_type": "SamplerCustomAdvanced",
                  "inputs": {"noise": ["8", 0], "guider": ["5", 0], "sampler": ["6", 0],
                             "sigmas": ["7", 0], "latent_image": ["4", 2]}},
        })
    else:
        graph["9"] = {"class_type": "KSampler",
                      "inputs": {"model": ["1", 0], "positive": ["4", 0],
                                 "negative": ["4", 1], "latent_image": ["4", 2],
                                 "seed": args.seed, "steps": args.steps, "cfg": args.cfg,
                                 "sampler_name": "euler", "scheduler": "simple",
                                 "denoise": 1.0}}
    return graph


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["base", "turbo"], default="base")
    parser.add_argument("--unet", default=BASE_UNET)
    parser.add_argument("--encoder", default="qwen3vl_8b_w4a8.safetensors")
    parser.add_argument("--encoder-gguf", default="")
    parser.add_argument("--steps", type=int, default=25)
    parser.add_argument("--cfg", type=float, default=1.0)
    parser.add_argument("--size", type=int, default=1024)
    parser.add_argument("--seed", type=int, default=20250912)
    parser.add_argument("--tag", default="")
    parser.add_argument("--unet-loader", choices=["UNETLoader", "UnetLoaderGGUF"], default="UNETLoader")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    graph = build_graph(args)
    baseline = vram_mib()
    peak = baseline
    started = time.time()
    with httpx.Client(timeout=120) as client:
        response = client.post(f"{COMFY}/prompt",
                               json={"prompt": graph, "client_id": uuid.uuid4().hex})
        if response.status_code != 200:
            print("submit failed:", response.status_code, response.text[:800])
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
        error = None
        for kind, payload in status.get("messages", []):
            if kind == "execution_error":
                error = f"{payload.get('node_type')}: {payload.get('exception_message')}"[:400]
        images = [image for output in (history.get("outputs") or {}).values()
                  for image in (output.get("images") or [])]
        saved = None
        if images:
            name = images[0]
            blob = client.get(f"{COMFY}/view", params={"filename": name["filename"],
                                                       "subfolder": name.get("subfolder", ""),
                                                       "type": name.get("type", "output")}).content
            label = args.tag or args.mode
            saved = OUT / f"{label}-seed{args.seed}.png"
            saved.write_bytes(blob)

    execution = None
    last = None
    for kind, payload in (status.get("messages") or []):
        if kind == "execution_start":
            execution = payload.get("timestamp")
    if status.get("messages"):
        last = status["messages"][-1][1].get("timestamp")
    print(json.dumps({
        "mode": args.mode, "unet": args.unet,
        "encoder": args.encoder_gguf or args.encoder,
        "seed": args.seed, "size": args.size, "steps": args.steps, "cfg": args.cfg,
        "status": status.get("status_str"), "error": error,
        "wall_seconds": wall,
        "execution_ms": (last - execution) if execution and last else None,
        "vram_baseline_mib": baseline, "vram_peak_mib": peak, "vram_delta_mib": peak - baseline,
        "image": str(saved) if saved else None,
    }, ensure_ascii=False, indent=2))
    return 0 if status.get("status_str") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
