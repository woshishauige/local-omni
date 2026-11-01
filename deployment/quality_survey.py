"""Uniform quality survey across the image pipelines.

Every configuration is generated with the *same* prompt, negative prompt, size
(832x1216), seed set and prompt-enhancement setting, so the only variables are
the model and the sampling recipe. Each image is then measured with objective
pixel metrics and scored by the local vision model with one identical rubric.

    python quality_survey.py [--seeds 12345,777,42] [--skip-scoring]

Reports and images land in logs/quality-survey/.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import statistics
import time
from pathlib import Path

import numpy as np
import requests
from PIL import Image

from compare_pipelines import GATEWAY, apply_presets, follow, select, submit, wait_ready

OUT = Path(r"D:\AI\Qwen-Local\logs\quality-survey")
PROMPT = ("photo of a handsome young asian man, athletic build, natural sunlight, "
          "detailed skin texture, looking at camera, 50mm portrait")
NEGATIVE = "lowres, bad anatomy, bad hands, watermark, text"
SIZE = (832, 1216)

# name, pipeline, presets, explicit parameters
CONFIGS: list[tuple[str, str, str | None, dict]] = [
    ("flux2-4step", "flux2", None, {"steps": 4, "cfg": 1.0}),
    ("fp16-28step", "sdxl", None, {"steps": 28, "cfg": 6.0}),
    ("fp16-lcm8", "sdxl", "lcm", {}),
    ("fp16-lcm8-photoreal", "sdxl", "lcm,photoreal", {}),
]

RUBRIC = """你是严格的图像质量评审，用同一把尺子给每张图打分。
只输出一个 JSON 对象，不要任何解释文字，格式：
{"解剖正确性":1-10,"面部五官":1-10,"皮肤真实感":1-10,"细节清晰度":1-10,"瑕疵程度":1-10,"综合":1-10,"主要瑕疵":["..."]}
评分口径（所有图片一致）：
- 解剖正确性：四肢/躯干/五官结构是否有畸变、多指、错位。
- 面部五官：眼睛是否对称、视线是否正常、五官比例是否自然。
- 皮肤真实感：是否像真实照片的皮肤（毛孔、质感），还是蜡像/塑料感。
- 细节清晰度：细节是否干净，有无糊、噪点、涂抹。
- 瑕疵程度：10 = 完全看不出瑕疵；每发现一处明显瑕疵扣 2 分以上（例如额头异常褶皱、色块、湿斑、耳朵发红、水印式纹理）。
- 综合：整体可用程度，10 = 可以直接当作成品照片。
只根据画面本身评分，不要因为风格差异（构图、服装、光线不同）扣分。"""


def metrics(path: Path) -> dict:
    """Objective pixel statistics, identical for every image."""
    image = Image.open(path).convert("RGB")
    array = np.asarray(image, dtype=np.float32)
    grey = array.mean(axis=2)
    # Sharpness: variance of the Laplacian (higher = crisper edges).
    padded = np.pad(grey, 1, mode="edge")
    window = padded[0:grey.shape[0] + 2, 0:grey.shape[1] + 2]
    laplacian = np.zeros_like(grey)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            weight = 4.0 if (dy == 0 and dx == 0) else (-1.0 if abs(dy) + abs(dx) == 1 else 0.0)
            if weight:
                laplacian += weight * padded[1 + dy:1 + dy + grey.shape[0],
                                             1 + dx:1 + dx + grey.shape[1]]
    del window
    height, width = grey.shape
    centre = grey[int(height * 0.15):int(height * 0.75), int(width * 0.2):int(width * 0.8)]
    # Patchiness: spread of 16x16 block means in the centre (blotchy skin shows up here).
    blocks = centre[:centre.shape[0] // 16 * 16, :centre.shape[1] // 16 * 16].reshape(-1, 16, 16)
    block_means = blocks.mean(axis=(1, 2))
    # Fine grain: residual against a 3x3 box blur, measured in the centre.
    blurred = np.asarray(Image.fromarray(centre.astype(np.uint8)).filter(
        __import__("PIL.ImageFilter", fromlist=["ImageFilter"]).BoxBlur(1)), dtype=np.float32)
    return {
        "sharpness_laplacian": round(float(laplacian.var()), 1),
        "patchiness_center": round(float(block_means.std()), 2),
        "grain_center": round(float(np.abs(centre - blurred).mean()), 2),
        "clipped_highlights_pct": round(float((grey > 250).mean() * 100), 2),
        "clipped_shadows_pct": round(float((grey < 5).mean() * 100), 2),
    }


def switch_mode(mode: str, timeout: float = 300) -> dict:
    response = requests.post(f"{GATEWAY}/local/mode", json={"mode": mode}, timeout=30)
    response.raise_for_status()
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = requests.get(f"{GATEWAY}/local/status", timeout=15).json()
        if status.get("state") == "ready" and status.get("mode") == mode:
            return status
        time.sleep(4)
    raise RuntimeError(f"gateway never reached {mode}: {status}")


def score_image(path: Path, timeout: float = 300) -> dict | None:
    """Ask the local vision model for the rubric score of one image."""
    image = Image.open(path).convert("RGB")
    image.thumbnail((896, 896))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    body = {
        "messages": [
            {"role": "system", "content": RUBRIC},
            {"role": "user", "content": [
                {"type": "text", "text": "评价这张图，只输出 JSON。"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}},
            ]},
        ],
        "temperature": 0.0,
        "max_tokens": 320,
    }
    try:
        response = requests.post(f"{GATEWAY}/v1/chat/completions", json=body, timeout=timeout)
        response.raise_for_status()
        text = response.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001 - the survey continues without scores
        return {"error": str(exc)[:200]}
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {"error": f"no JSON in reply: {text[:120]}"}
    try:
        return json.loads(text[start:end + 1])
    except ValueError as exc:
        return {"error": f"bad JSON: {exc}; raw={text[start:end + 1][:160]}"}


def follow_job(job_id: str, timeout: float = 1800, outage: float = 300) -> dict:
    """Follow a job, surviving a gateway restart in the middle of the batch."""
    deadline = time.time() + timeout
    outage_left = outage
    while time.time() < deadline:
        try:
            payload = requests.get(f"{GATEWAY}/local/image/progress", timeout=15).json()
        except requests.RequestException as exc:
            outage_left -= 5
            if outage_left <= 0:
                raise RuntimeError(f"gateway stayed unreachable: {exc}") from exc
            print(f"    gateway unreachable ({exc.__class__.__name__}); waiting", flush=True)
            time.sleep(5)
            continue
        job = payload.get("job") or {}
        if job.get("id") == job_id and not payload.get("busy") and job.get("state") != "running":
            return job
        time.sleep(2)
    raise RuntimeError("timeout waiting for the job")


def load_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def summarise(records: list[dict]) -> dict:
    buckets: dict[str, dict] = {}
    for record in records:
        bucket = buckets.setdefault(record["config"], {"n": 0, "total": [], "sampling": [], "vram": [],
                                                        "sharpness": [], "patchiness": [], "grain": [],
                                                        "overall": [], "artifacts": []})
        bucket["n"] += 1
        for key, source in (("total", "total_seconds"), ("sampling", "sampling_seconds"),
                            ("vram", "vram_image_peak_mib"), ("sharpness", "sharpness_laplacian"),
                            ("patchiness", "patchiness_center"), ("grain", "grain_center")):
            value = record.get(source)
            if isinstance(value, (int, float)):
                bucket[key].append(float(value))
        score = record.get("score") or {}
        overall = score.get("综合")
        if isinstance(overall, (int, float)):
            bucket["overall"].append(float(overall))
        for item in score.get("主要瑕疵") or []:
            bucket["artifacts"].append(str(item))
    return {
        name: {
            "runs": bucket["n"],
            **{key: round(statistics.fmean(values), 2) if values else None
               for key, values in bucket.items() if key not in {"n", "artifacts"}},
            "artifacts": bucket["artifacts"][:12],
        }
        for name, bucket in buckets.items()
    }


def write_report(records: list[dict], seeds: list[int]) -> dict:
    report = {"prompt": PROMPT, "negative": NEGATIVE, "size": SIZE, "seeds": seeds,
              "at": time.strftime("%Y-%m-%d %H:%M:%S"), "records": records,
              "summary": summarise(records)}
    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", default="12345,777,42")
    parser.add_argument("--configs", default="", help="comma-separated subset of config names")
    parser.add_argument("--skip-scoring", action="store_true")
    args = parser.parse_args()
    seeds = [int(value) for value in args.seeds.split(",") if value.strip()]
    wanted = {name.strip() for name in args.configs.split(",") if name.strip()}
    configs = [entry for entry in CONFIGS if not wanted or entry[0] in wanted]

    images_dir = OUT / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    records_path = OUT / "records.jsonl"
    records = load_records(records_path)
    done = {(record["config"], record["seed"]) for record in records}
    print(f"resuming with {len(records)} existing records", flush=True)

    for name, pipeline, presets, params in configs:
        for seed in seeds:
            if (name, seed) in done:
                print(f"[{name} seed={seed}] already recorded", flush=True)
                continue
            try:
                wait_ready()
                info = select(pipeline)
                model = info.get("model", {})
                if model.get("ready") is False:
                    print(f"[{name}] skipped: missing {model.get('missing')}", flush=True)
                    break
                steps = params.get("steps")
                cfg = params.get("cfg")
                if presets is not None:
                    payload = apply_presets(pipeline, [key for key in presets.split(",") if key])
                    active = [preset["key"] for preset in payload["presets"] if preset["active"]]
                    steps = steps or payload["params"].get("steps")
                    cfg = cfg if cfg is not None else payload["params"].get("cfg")
                    preset_label = "+".join(active)
                else:
                    apply_presets(pipeline, [])
                    preset_label = ""
                job_id = submit(PROMPT, NEGATIVE, False, steps, SIZE, cfg, seed)
                job = follow_job(job_id)
                if job.get("state") != "completed" or not job.get("images"):
                    print(f"[{name} seed={seed}] FAILED: {job.get('state')} {job.get('error')}", flush=True)
                    continue
                url = job["images"][0]
                blob = requests.get(url, timeout=60).content
                path = images_dir / f"{name}-seed{seed}.png"
                path.write_bytes(blob)
                record = {
                    "config": name, "pipeline": pipeline, "presets": preset_label, "seed": seed,
                    "steps": (job.get("params") or {}).get("steps"), "cfg": (job.get("params") or {}).get("cfg"),
                    "total_seconds": job.get("timings", {}).get("total"),
                    "sampling_seconds": job.get("timings", {}).get("sampling"),
                    "vram_image_peak_mib": (job.get("gpu") or {}).get("image_peak_mib"),
                    "image": str(path), **metrics(path),
                }
                records.append(record)
                with records_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                write_report(records, seeds)
                print(f"[{name} seed={seed}] ok total={record['total_seconds']:.1f}s "
                      f"sampling={record['sampling_seconds']:.1f}s vram={record['vram_image_peak_mib']}MB "
                      f"sharp={record['sharpness_laplacian']}", flush=True)
            except Exception as exc:  # noqa: BLE001 - keep the batch going
                print(f"[{name} seed={seed}] ERROR {exc}", flush=True)
                time.sleep(5)

    if not args.skip_scoring and records:
        pending = [record for record in records if not (record.get("score") or {}).get("综合")]
        if pending:
            try:
                print(f"switching to vision mode to score {len(pending)} images", flush=True)
                switch_mode("vision")
                for record in pending:
                    score = score_image(Path(record["image"]))
                    record["score"] = score
                    print(f"[score {record['config']} seed={record['seed']}] "
                          f"{json.dumps(score, ensure_ascii=False)[:160]}", flush=True)
                    with records_path.open("w", encoding="utf-8") as handle:
                        handle.write("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records))
                    write_report(records, seeds)
            except Exception as exc:  # noqa: BLE001
                print(f"scoring unavailable: {exc}", flush=True)
            finally:
                try:
                    switch_mode("text")
                except Exception as exc:  # noqa: BLE001
                    print(f"could not switch back to text mode: {exc}", flush=True)

    report = write_report(records, seeds)
    print("\n=== summary (mean per configuration) ===")
    header = (f"{'config':22} {'runs':>4} {'total':>7} {'sampling':>8} {'vram':>7} "
              f"{'sharp':>8} {'patch':>6} {'grain':>6} {'score':>6}")
    print(header)
    for name, row in report["summary"].items():
        print(f"{name:22} {row['runs']:>4} {row['total'] or 0:>7.1f} {row['sampling'] or 0:>8.1f} "
              f"{row['vram'] or 0:>7.0f} {row['sharpness'] or 0:>8.1f} {row['patchiness'] or 0:>6.2f} "
              f"{row['grain'] or 0:>6.2f} {row['overall'] if row['overall'] is not None else '-':>6}")
    print(f"\nreport: {OUT / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
