"""Run every one-click workflow once through the gateway and report the result.

This is the "does the switcher actually work" check: for each recipe it switches
the workflow, submits a real generation with the same prompt and seed, follows the
job to completion and saves the image plus the measured timings. Image-to-image
gets a reference image so its denoise path is exercised too.

    python verify_workflows_live.py [--seed 20250912] [--only portrait,draft]
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import time
from pathlib import Path

import requests

from verify_common import GATEWAY, poll_image_job, wait_ready

OUT = Path(r"D:\AI\Qwen-Local\logs\workflow-comparison")
REFERENCE = Path(r"D:\AI\Qwen-Local\logs\pipeline-comparison\fp16-lcm-8step.png")
PROMPT = ("photo of a handsome young asian man, athletic build, natural sunlight, "
          "detailed skin texture, looking at camera, 50mm portrait")
NEGATIVE = "lowres, bad anatomy, bad hands, watermark, text"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=20250912)
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    wanted = {name.strip() for name in args.only.split(",") if name.strip()}

    OUT.mkdir(parents=True, exist_ok=True)
    listed = requests.get(f"{GATEWAY}/local/image/workflows", timeout=30).json()["workflows"]
    report: dict = {"prompt": PROMPT, "seed": args.seed, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "runs": []}

    for entry in listed:
        if wanted and entry["key"] not in wanted:
            continue
        if (entry.get("mode") or "image") != "image":
            print(f"[{entry['key']}] skipped: 属于视频模式（生图界面不提供）", flush=True)
            report["runs"].append({"workflow": entry["key"], "skipped": "视频模式"})
            continue
        if entry["available"] is False:
            print(f"[{entry['key']}] skipped: {entry.get('missing')}", flush=True)
            report["runs"].append({"workflow": entry["key"], "skipped": "模型文件未就绪"})
            continue
        wait_ready()
        applied = requests.post(f"{GATEWAY}/local/image/workflows", json={"key": entry["key"]}, timeout=60)
        if applied.status_code != 200:
            print(f"[{entry['key']}] switch failed {applied.status_code}: {applied.text[:200]}", flush=True)
            report["runs"].append({"workflow": entry["key"], "error": applied.text[:200]})
            continue
        payload = applied.json()
        params = payload["params"]
        # An upscaler samples from an image instead of a prompt, so the reference
        # goes in for every recipe whose pipeline declares it — that includes the
        # "放大" recipes, which used to be impossible to drive from the panel.
        needs_reference = requests.get(
            f"{GATEWAY}/local/image/workflow", params={"pipeline": payload["pipeline"]}, timeout=60,
        ).json().get("references_required", False)
        body = {
            "prompt": PROMPT, "negative_prompt": NEGATIVE, "enhance_prompt": False,
            "pipeline": payload["pipeline"], "width": 832, "height": 1216,
            "steps": params.get("steps"), "cfg": params.get("cfg"),
            "seed": args.seed,
        }
        if params.get("denoise") is not None:
            body["denoise"] = params["denoise"]
        if params.get("scale") is not None:
            body["scale"] = params["scale"]
        if needs_reference or entry["key"] == "img2img":
            encoded = base64.b64encode(REFERENCE.read_bytes()).decode("ascii")
            body["image_1"] = f"data:image/png;base64,{encoded}"
        submitted = requests.post(f"{GATEWAY}/local/image/generate", json=body, timeout=120)
        if submitted.status_code != 202:
            print(f"[{entry['key']}] submit failed {submitted.status_code}: {submitted.text[:200]}", flush=True)
            report["runs"].append({"workflow": entry["key"], "error": submitted.text[:200]})
            continue
        job = poll_image_job("/local/image/progress", submitted.json()["job"]["id"])
        images = job.get("images") or []
        saved = None
        if images:
            blob = requests.get(images[0], timeout=60).content
            saved = OUT / f"{entry['key']}.png"
            saved.write_bytes(blob)
        size = None
        if saved is not None:
            from PIL import Image

            with Image.open(saved) as opened:
                size = list(opened.size)
        run = {
            "workflow": entry["key"], "label": entry["label"], "summary": entry["summary"],
            "output_size": size,
            "pipeline": payload["pipeline"], "presets": payload["presets"],
            "state": job.get("state"), "error": job.get("error"),
            "params": job.get("params"), "timings": job.get("timings"),
            "vram_image_peak_mib": (job.get("gpu") or {}).get("image_peak_mib"),
            "image": str(saved) if saved else None,
        }
        report["runs"].append(run)
        print(f"[{entry['key']:9}] {run['state']:9} presets={payload['presets']} "
              f"scale={run['params'].get('scale')} size={size} "
              f"steps={run['params'].get('steps')} denoise={run['params'].get('denoise')} "
              f"queue={run['timings'].get('queue')} load={run['timings'].get('load')} "
              f"sampling={run['timings'].get('sampling')} total={run['timings'].get('total')} "
              f"vram={run['vram_image_peak_mib']}", flush=True)

    path = OUT / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    failures = [run for run in report["runs"] if run.get("state") != "completed" and not run.get("skipped")]
    print(f"\n{len(report['runs']) - len(failures)}/{len(report['runs'])} workflows produced an image; report: {path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
