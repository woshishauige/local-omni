"""Verify image-to-image: a reference image plus a denoise value must change the picture.

Sends a locally generated PNG back through the gateway as a data URL, asks for a
partial redraw, and reports what actually ran (parameters, the graph's denoise
input, timings) plus the produced image. Usage:

    python verify_img2img.py [reference.png] [--denoise 0.55] [--steps 8] [--pipeline sdxl_gguf]
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

import requests

from verify_common import GATEWAY, poll_image_job, wait_ready

OUT = Path(r"D:\AI\Qwen-Local\logs\pipeline-comparison")
PROMPT = "photo of a handsome young asian man, athletic build, natural sunlight, detailed skin texture, 50mm portrait"
NEGATIVE = "lowres, bad anatomy, bad hands, watermark, text"


def denoise_in_graph(job: dict) -> float | None:
    for node in job.get("graph") or []:
        inputs = node.get("inputs") or {}
        if "denoise" in inputs:
            return float(inputs["denoise"])
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("reference", nargs="?", default=str(OUT / "final-sdxl-gguf.png"))
    parser.add_argument("--denoise", type=float, default=0.55)
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--cfg", type=float, default=1.5)
    parser.add_argument("--pipeline", default="sdxl")
    parser.add_argument("--preset", default="lcm")
    args = parser.parse_args()

    reference = Path(args.reference)
    if not reference.exists():
        raise SystemExit(f"reference image not found: {reference}")
    encoded = base64.b64encode(reference.read_bytes()).decode("ascii")

    wait_ready()
    if args.preset and args.preset != "none":
        response = requests.post(f"{GATEWAY}/local/image/presets",
                                 json={"pipeline": args.pipeline, "key": args.preset}, timeout=30)
        response.raise_for_status()
    response = requests.post(f"{GATEWAY}/local/image/generate", timeout=60, json={
        "prompt": PROMPT, "negative_prompt": NEGATIVE, "enhance_prompt": False,
        "pipeline": args.pipeline, "width": 832, "height": 1216,
        "steps": args.steps, "cfg": args.cfg, "denoise": args.denoise, "seed": 12345,
        "image_1": f"data:image/png;base64,{encoded}",
    })
    if response.status_code != 202:
        raise SystemExit(f"submit failed {response.status_code}: {response.text[:300]}")
    job_id = response.json()["job"]["id"]
    print(f"job {job_id}: denoise={args.denoise} steps={args.steps}")
    job = poll_image_job("/local/image/progress", job_id)
    print(json.dumps({
        "state": job.get("state"),
        "params": job.get("params"),
        "references": job.get("references"),
        "denoise_in_graph": denoise_in_graph(job),
        "timings": job.get("timings"),
        "images": job.get("images"),
        "error": job.get("error"),
    }, ensure_ascii=False, indent=2))
    return 0 if job.get("state") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
