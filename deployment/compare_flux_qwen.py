"""Compare Flux.2 Klein against a Qwen-Image build under one protocol.

The protocol is deliberately identical for both models — same prompt, same
negative, same canvas, same seeds, prompt rewriting off — and each model runs at
*its own* recommended sampling recipe (a distilled 4-step model and a 20-step
model cannot share a step count without crippling one of them). Measured per run:
gateway timings (queue / load / sampling), image VRAM peak, and image statistics
(sharpness = Laplacian variance, noise floor, colour spread).

    python compare_flux_qwen.py --models flux2 --seeds 20250912,777
    python compare_flux_qwen.py --models flux2,qwen_image --seeds 20250912,777

Results land in logs/model-comparison/{report.json, sheet-*.png}.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx

GATEWAY = "http://127.0.0.1:8081"
OUT = Path(r"D:\AI\Qwen-Local\logs\model-comparison")
PROMPT = ("photo of a handsome young asian man, athletic build, natural sunlight, "
          "detailed skin texture, looking at camera, 50mm portrait")
NEGATIVE = "lowres, bad anatomy, bad hands, watermark, text"
SIZE = (1024, 1024)

# Which one-click recipe (or pipeline) drives each model, and how to switch to it.
MODELS = {
    "flux2": {"recipe": "flux2", "label": "Flux.2 Klein 9B fp8"},
    "qwen_image": {"pipeline": "qwen_image", "label": "Qwen-Image (GGUF)"},
}


def wait_ready(client: httpx.Client, seconds: float = 300) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        status = client.get("/local/status", timeout=15).json()
        if status.get("state") == "ready" and not status.get("active_requests"):
            return
        time.sleep(4)
    raise RuntimeError(f"gateway never became ready: {status}")


def follow(client: httpx.Client, job_id: str, timeout: float = 2400) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            payload = client.get("/local/image/progress", timeout=15).json()
        except httpx.HTTPError:
            time.sleep(5)
            continue
        job = payload.get("job") or {}
        if job.get("id") == job_id and not payload.get("busy") and job.get("state") != "running":
            return job
        time.sleep(3)
    raise RuntimeError("timeout waiting for the job")


def image_stats(path: Path) -> dict:
    from PIL import Image

    with Image.open(path) as opened:
        image = opened.convert("RGB")
    grey = image.convert("L")
    width, height = grey.size
    pixels = grey.load()
    total = 0.0
    total_sq = 0.0
    lap = 0.0
    lap_sq = 0.0
    count = 0
    lap_count = 0
    for y in range(height):
        for x in range(width):
            value = pixels[x, y]
            total += value
            total_sq += value * value
            count += 1
            if 0 < x < width - 1 and 0 < y < height - 1:
                laplacian = (4 * value - pixels[x - 1, y] - pixels[x + 1, y]
                             - pixels[x, y - 1] - pixels[x, y + 1])
                lap += laplacian
                lap_sq += laplacian * laplacian
                lap_count += 1
    mean = total / count
    variance = max(0.0, total_sq / count - mean * mean)
    lap_mean = lap / lap_count
    lap_variance = max(0.0, lap_sq / lap_count - lap_mean * lap_mean)
    return {
        "size": list(image.size),
        "brightness": round(mean, 2),
        "contrast": round(variance ** 0.5, 2),
        "sharpness_laplacian": round(lap_variance, 1),
    }


def run_one(client: httpx.Client, model: str, seed: int) -> dict:
    spec = MODELS[model]
    wait_ready(client)
    if "recipe" in spec:
        applied = client.post("/local/image/workflows", json={"key": spec["recipe"]}, timeout=60)
        applied.raise_for_status()
        payload = applied.json()
        params = payload["params"]
        pipeline = payload["pipeline"]
    else:
        pipeline = spec["pipeline"]
        defaults = client.get("/local/image/workflow", params={"pipeline": pipeline}, timeout=60).json()["defaults"]
        params = {"steps": defaults.get("steps"), "cfg": defaults.get("cfg"), "denoise": defaults.get("denoise")}
    body = {
        "prompt": PROMPT, "negative_prompt": NEGATIVE, "enhance_prompt": False,
        "pipeline": pipeline, "width": SIZE[0], "height": SIZE[1], "seed": seed,
    }
    for key in ("steps", "cfg", "denoise"):
        if params.get(key) is not None:
            body[key] = params[key]
    started = time.time()
    submitted = client.post("/local/image/generate", json=body, timeout=120)
    if submitted.status_code != 202:
        return {"model": model, "seed": seed, "error": f"{submitted.status_code}: {submitted.text[:300]}"}
    job = follow(client, submitted.json()["job"]["id"])
    run = {
        "model": model, "label": spec["label"], "seed": seed, "pipeline": pipeline,
        "state": job.get("state"), "error": job.get("error"),
        "params": job.get("params"), "timings": job.get("timings"),
        "vram_image_peak_mib": (job.get("gpu") or {}).get("image_peak_mib"),
        "wall_seconds": round(time.time() - started, 1),
        "image": None, "stats": None,
    }
    images = job.get("images") or []
    if images:
        blob = client.get(images[0], timeout=120).content
        OUT.mkdir(parents=True, exist_ok=True)
        saved = OUT / f"{model}-seed{seed}.png"
        saved.write_bytes(blob)
        run["image"] = str(saved)
        run["stats"] = image_stats(saved)
    return run


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", default="flux2")
    parser.add_argument("--seeds", default="20250912,777")
    args = parser.parse_args()
    models = [name.strip() for name in args.models.split(",") if name.strip()]
    seeds = [int(value) for value in args.seeds.split(",") if value.strip()]

    OUT.mkdir(parents=True, exist_ok=True)
    report = {"prompt": PROMPT, "negative": NEGATIVE, "size": SIZE, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "runs": []}
    with httpx.Client(base_url=GATEWAY, timeout=120) as client:
        for model in models:
            for seed in seeds:
                run = run_one(client, model, seed)
                report["runs"].append(run)
                timings = run.get("timings") or {}
                stats = run.get("stats") or {}
                print(f"[{model:11}] seed={seed} {run.get('state')} "
                      f"sampling={timings.get('sampling')} total={timings.get('total')} "
                      f"wall={run.get('wall_seconds')} vram={run.get('vram_image_peak_mib')} "
                      f"sharp={stats.get('sharpness_laplacian')} contrast={stats.get('contrast')} "
                      f"{run.get('error') or ''}", flush=True)
    path = OUT / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("report:", path)
    return 0 if all(run.get("state") == "completed" for run in report["runs"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
