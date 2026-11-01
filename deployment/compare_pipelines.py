"""Compare generation pipelines on this machine: time, VRAM and output files.

Submits the same prompt to each pipeline through the running gateway, follows
/local/image/progress to completion, then reports total/sampling seconds, the
job's own VRAM footprint and the produced images for visual comparison.

Usage: python compare_pipelines.py "prompt" [--pipelines flux2,sdxl] [--negative "..."]
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import requests

GATEWAY = "http://127.0.0.1:8081"
OUT = Path(r"D:\AI\Qwen-Local\logs\pipeline-comparison")


def select(pipeline: str) -> dict:
    response = requests.post(f"{GATEWAY}/local/image/pipeline", json={"pipeline": pipeline}, timeout=15)
    response.raise_for_status()
    return response.json()


def wait_ready(seconds: float = 240) -> None:
    """The gateway reports 'switching' while it loads or unloads a model."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        status = requests.get(f"{GATEWAY}/local/status", timeout=15).json()
        if status.get("state") == "ready" and not status.get("active_requests"):
            return
        time.sleep(3)
    raise RuntimeError(f"gateway never became ready: {status}")


def apply_presets(pipeline: str, keys: list[str]) -> dict:
    """Clear the saved LoRA presets, then stack the requested ones in order."""
    response = requests.delete(f"{GATEWAY}/local/image/presets", params={"pipeline": pipeline}, timeout=30)
    response.raise_for_status()
    payload = response.json()
    merged: dict = {}
    for key in keys:
        response = requests.post(f"{GATEWAY}/local/image/presets",
                                 json={"pipeline": pipeline, "key": key}, timeout=30)
        if response.status_code != 200:
            raise RuntimeError(f"preset {key} failed {response.status_code}: {response.text[:200]}")
        payload = response.json()
        # A quality preset reports no parameters, so merging keeps the sampling
        # settings the accelerator asked for.
        merged.update({name: value for name, value in (payload.get("params") or {}).items()
                       if name in {"steps", "cfg", "denoise"}})
    payload["params"] = merged
    return payload


def submit(prompt: str, negative: str, enhance: bool, steps: int | None, size: tuple[int, int] | None,
           cfg: float | None = None, seed: int = 12345) -> str:
    wait_ready()
    body = {"prompt": prompt, "negative_prompt": negative, "enhance_prompt": enhance, "seed": seed}
    if steps:
        body["steps"] = steps
    if cfg is not None:
        body["cfg"] = cfg
    if size:
        body["width"], body["height"] = size
    response = requests.post(f"{GATEWAY}/local/image/generate", json=body, timeout=30)
    if response.status_code != 202:
        raise RuntimeError(f"submit failed {response.status_code}: {response.text[:300]}")
    return response.json()["job"]["id"]


def follow(job_id: str, timeout: float = 1800) -> dict:
    deadline = time.time() + timeout
    last_phase = ""
    while time.time() < deadline:
        payload = requests.get(f"{GATEWAY}/local/image/progress", timeout=15).json()
        job = payload.get("job") or {}
        phase = f"{job.get('state')}/{job.get('phase')}"
        if phase != last_phase:
            print(f"    {phase} step={job.get('step')}/{job.get('max_step')} vram={job.get('gpu', {}).get('job_mib') if job.get('gpu') else '-'} MB", flush=True)
            last_phase = phase
        if job.get("id") == job_id and not payload.get("busy") and job.get("state") != "running":
            return job
        time.sleep(2)
    raise RuntimeError("timeout waiting for the job")


def run_pipeline(pipeline: str, prompt: str, negative: str, enhance: bool, steps: int | None,
                 size: tuple[int, int] | None, presets: list[str] | None = None, seed: int = 12345) -> dict:
    print(f"[{pipeline}] selecting", flush=True)
    info = select(pipeline)
    model = info.get("model", {})
    print(f"[{pipeline}] model={model.get('unet')} ready={model.get('ready')}", flush=True)
    if model.get("ready") is False:
        return {"pipeline": pipeline, "skipped": f"missing {','.join(model.get('missing') or [])}"}
    chosen: list[str] = []
    cfg = None
    if presets is not None:
        payload = apply_presets(pipeline, presets)
        chosen = [preset["key"] for preset in payload["presets"] if preset["active"]]
        # An accelerator also states the step count and CFG it converges at; use
        # them unless the caller pinned the value on the command line.
        steps = steps or payload["params"].get("steps")
        cfg = payload["params"].get("cfg")
        print(f"[{pipeline}] presets={chosen} steps={steps} cfg={cfg} "
              f"loras={payload['overrides']}", flush=True)
    job_id = submit(prompt, negative, enhance, steps, size, cfg, seed)
    print(f"[{pipeline}] job {job_id}", flush=True)
    job = follow(job_id)
    gpu = job.get("gpu") or {}
    result = {
        "pipeline": pipeline,
        "presets": chosen,
        "state": job.get("state"),
        "model": job.get("model"),
        "params": job.get("params"),
        "error": job.get("error"),
        "total_seconds": job.get("timings", {}).get("total"),
        "sampling_seconds": job.get("timings", {}).get("sampling"),
        "prompt_seconds": job.get("timings", {}).get("prompt"),
        "steps_per_second": job.get("steps_per_second"),
        "vram_job_peak_mib": gpu.get("job_peak_mib"),
        "vram_image_peak_mib": gpu.get("image_peak_mib"),
        "images": job.get("images") or [],
    }
    print(f"[{pipeline}] {result['state']} total={result['total_seconds']}s sampling={result['sampling_seconds']}s "
          f"vram_image={result['vram_image_peak_mib']}MB images={len(result['images'])}", flush=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--pipelines", default="flux2,sdxl,chroma")
    parser.add_argument("--negative", default="lowres, bad anatomy, bad hands, watermark, text")
    parser.add_argument("--enhance", action="store_true", help="keep the Qwen prompt rewrite (default: off, to isolate the model)")
    parser.add_argument("--steps", type=int, default=None)
    parser.add_argument("--size", default=None, help="WIDTHxHEIGHT")
    parser.add_argument("--seed", type=int, default=12345, help="fixed seed for every run")
    parser.add_argument("--presets", default=None,
                        help="comma-separated LoRA presets to apply before each run (empty = clear)")
    args = parser.parse_args()
    size = None
    if args.size:
        width, height = args.size.lower().split("x")
        size = (int(width), int(height))
    presets = None if args.presets is None else [key.strip() for key in args.presets.split(",") if key.strip()]

    OUT.mkdir(parents=True, exist_ok=True)
    report = {"prompt": args.prompt, "negative": args.negative, "enhance": args.enhance,
              "steps": args.steps, "size": args.size, "presets": presets, "seed": args.seed,
              "at": time.strftime("%Y-%m-%d %H:%M:%S"), "runs": []}
    for pipeline in [name.strip() for name in args.pipelines.split(",") if name.strip()]:
        try:
            report["runs"].append(run_pipeline(pipeline, args.prompt, args.negative, args.enhance,
                                               args.steps, size, presets, args.seed))
        except Exception as exc:  # noqa: BLE001
            print(f"[{pipeline}] FAILED: {exc}", flush=True)
            report["runs"].append({"pipeline": pipeline, "error": str(exc)})
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = OUT / f"comparison-{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nreport: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
