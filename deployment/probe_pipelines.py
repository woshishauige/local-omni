"""Validate the SDXL and Chroma graph structure against the running ComfyUI.

No generation: /prompt validates node classes, required inputs and connections
before queuing, so a structurally wrong graph fails here immediately. The
placeholder checkpoint is deleted from the queue right away.

Usage: python probe_pipelines.py <pipeline> [checkpoint]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, r"D:\AI")

import image_comfy  # noqa: E402

COMFY = "http://127.0.0.1:8188"


def wait_for_comfy(seconds: float = 120) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if requests.get(f"{COMFY}/system_stats", timeout=4).status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(3)
    return False


def probe(pipeline: str, checkpoint: str | None) -> int:
    if not wait_for_comfy():
        print("ComfyUI is not answering on 8188")
        return 1
    overrides = {}
    if checkpoint:
        node = "10" if pipeline == "sdxl" else "1"
        field = "ckpt_name" if pipeline == "sdxl" else "unet_name"
        overrides = {node: {field: checkpoint}}
    try:
        built = image_comfy.build_graph(
            pipeline, "a photo of a person, natural light", negative_prompt="lowres, bad anatomy",
            references=[], width=832, height=1216, steps=8, cfg=6.0, denoise=1.0, seed=1234,
            filename_prefix="qwen/probe", overrides=overrides,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"build failed: {type(exc).__name__}: {exc}")
        return 1
    graph = built["prompt"]
    print("nodes:", ", ".join(f"{node_id}:{node['class_type']}" for node_id, node in sorted(graph.items(), key=lambda item: int(item[0]))))
    response = requests.post(f"{COMFY}/prompt", json={"prompt": graph, "client_id": "pipeline-probe"}, timeout=30)
    if response.status_code != 200:
        print(f"VALIDATION FAILED {response.status_code}: {response.text[:600]}")
        return 1
    prompt_id = response.json().get("prompt_id")
    print("accepted by ComfyUI:", prompt_id)
    time.sleep(1.5)
    requests.post(f"{COMFY}/queue", json={"delete": [prompt_id]}, timeout=15)
    requests.post(f"{COMFY}/interrupt", json={"prompt_id": prompt_id}, timeout=15)
    print("cancelled again (no image was produced)")
    return 0


if __name__ == "__main__":
    pipeline = sys.argv[1] if len(sys.argv) > 1 else "sdxl"
    checkpoint = sys.argv[2] if len(sys.argv) > 2 else None
    raise SystemExit(probe(pipeline, checkpoint))
