"""Run one model's graph straight through ComfyUI, bypassing the gateway.

The gateway now sits behind a one-time password, and a model-vs-model comparison
does not need it: the graph builders in image_comfy are importable, so the same
workflow the panel would submit can be posted to ComfyUI directly. The language
model is not loaded, so VRAM figures are comparable.

    python direct_image_run.py --pipeline flux2 --steps 4 --cfg 1.0 --seed 20250912
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

# The graph builders live in the Qwen-Local checkout and import the deployment
# package, so both roots have to be importable.
for root in (str(Path(__file__).resolve().parent), r"D:\AI"):
    if root not in sys.path:
        sys.path.insert(0, root)

import image_comfy

COMFY = "http://127.0.0.1:8188"
OUT = Path(r"D:\AI\Qwen-Local\logs\model-comparison")
PROMPT = ("photo of a handsome young asian man, athletic build, natural sunlight, "
          "detailed skin texture, looking at camera, 50mm portrait")
NEGATIVE = "lowres, bad anatomy, bad hands, watermark, text"


def vram_mib() -> tuple[int, int]:
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=30,
    )
    used, util = (part.strip() for part in result.stdout.strip().split(",")[:2])
    return int(used), int(util)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pipeline", default="flux2")
    parser.add_argument("--steps", type=int, default=4)
    parser.add_argument("--cfg", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=20250912)
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=1024)
    parser.add_argument("--tag", default="")
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    built = image_comfy.build_graph(
        args.pipeline, PROMPT, negative_prompt=NEGATIVE, references=[],
        width=args.width, height=args.height, steps=args.steps, cfg=args.cfg,
        denoise=1.0, seed=args.seed, filename_prefix="qwen/model-comparison",
        overrides={},
    )
    graph = built["prompt"]
    files = {}
    for node in ("1", "2", "3"):
        if node in graph:
            for name, value in graph[node]["inputs"].items():
                if name.endswith("_name") and isinstance(value, str):
                    files[f"{node}.{name}"] = value
    print("nodes:", len(graph), "| model files:", files)

    baseline, _ = vram_mib()
    peak = baseline
    started = time.time()
    with httpx.Client(timeout=60) as client:
        client_id = uuid.uuid4().hex
        response = client.post(f"{COMFY}/prompt", json={"prompt": graph, "client_id": client_id})
        response.raise_for_status()
        prompt_id = response.json()["prompt_id"]
        print("prompt_id:", prompt_id, "| baseline VRAM", baseline, "MiB")
        history = None
        while time.time() - started < 1800:
            time.sleep(2)
            used, _ = vram_mib()
            peak = max(peak, used)
            try:
                item = client.get(f"{COMFY}/history/{prompt_id}", timeout=20).json().get(prompt_id)
            except httpx.HTTPError:
                continue
            if item:
                history = item
                break
        if history is None:
            print("timeout")
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
            saved = OUT / f"{args.pipeline}-seed{args.seed}{('-' + args.tag) if args.tag else ''}.png"
            saved.write_bytes(blob)

    messages = status.get("messages") or []
    execution = None
    for kind, payload in messages:
        if kind == "execution_start":
            execution = payload.get("timestamp")
    print(json.dumps({
        "pipeline": args.pipeline, "steps": args.steps, "cfg": args.cfg, "seed": args.seed,
        "status": status.get("status_str"), "wall_seconds": wall,
        "vram_baseline_mib": baseline, "vram_peak_mib": peak,
        "vram_delta_mib": peak - baseline,
        "image": str(saved) if saved else None,
        "execution_ms": (messages[-1][1].get("timestamp") - execution) if execution and messages else None,
    }, ensure_ascii=False, indent=2))
    return 0 if status.get("status_str") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
