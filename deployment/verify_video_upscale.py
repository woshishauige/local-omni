"""Verify the video-clip 超分 path that the panel now drives.

Reproduces exactly what the panel does: apply the recipe, upload + select the
clip, then submit a SeedVR2 job for the video pipeline and follow it to the end.

    python verify_video_upscale.py [--recipe video_upscale_clean] [--clip name.mp4]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import requests

from verify_common import GATEWAY, poll_image_job

INPUT = Path(r"D:\AI\ComfyUI-BSAI_v010\ComfyUI\input")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recipe", default="video_upscale_clean")
    parser.add_argument("--clip", default="upscale-test-1s.mp4")
    args = parser.parse_args()

    listed = requests.get(f"{GATEWAY}/local/image/workflows", timeout=30).json()["workflows"]
    recipe = next(entry for entry in listed if entry["key"] == args.recipe)
    print("recipe:", recipe["key"], recipe["params"], "pipeline:", recipe["pipeline"])

    applied = requests.post(f"{GATEWAY}/local/image/workflows", json={"key": args.recipe}, timeout=60)
    applied.raise_for_status()
    print("applied pipeline:", applied.json()["pipeline"])

    clip = INPUT / args.clip
    assert clip.is_file(), clip
    with clip.open("rb") as handle:
        uploaded = requests.post(f"{GATEWAY}/local/video/upload",
                                 params={"name": clip.name},
                                 data=handle.read(),
                                 headers={"Content-Type": "application/octet-stream"},
                                 timeout=300)
    uploaded.raise_for_status()
    name = uploaded.json().get("name") or clip.name
    print("uploaded:", name)
    selected = requests.post(f"{GATEWAY}/local/video/source", json={"path": name}, timeout=60)
    selected.raise_for_status()

    body = {"prompt": "", "pipeline": recipe["pipeline"], "enhance_prompt": False}
    for key in ("scale", "steps", "cfg"):
        if recipe["params"].get(key) is not None:
            body[key] = recipe["params"][key]
    submitted = requests.post(f"{GATEWAY}/local/image/generate", json=body, timeout=120)
    print("submit:", submitted.status_code, submitted.text[:160])
    submitted.raise_for_status()
    job = poll_image_job(
        "/local/image/progress",
        submitted.json()["job"]["id"],
        interval=3,
        on_phase=lambda job: print(
            f"  phase={job.get('phase')} step={job.get('step')}/{job.get('max_step')}", flush=True),
    )
    print("state:", job["state"], "params:", job["params"], "timings:", job["timings"])
    print("images:", job["images"], "video:", job.get("video"))
    print("error:", job.get("error"))
    return 0 if job["state"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
