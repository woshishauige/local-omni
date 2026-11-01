"""Resolution sweep for Qwen-Image 2.1: same prompt and seed at 768 / 1024 / 2048.

Answers "is the distortion the model, the size, or the step count": the same
subject is rendered at three canvas sizes with both the base (25 steps) and the
Turbo (8 steps) model, and reports time plus peak VRAM for each.

    python resolution_sweep.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, r"D:\AI")

import image_comfy  # noqa: E402

COMFY = "http://127.0.0.1:8188"
OUT = Path(r"D:\AI\Qwen-Local\logs\style-test")
SEED = 20250912
PROMPT = ("photorealistic photograph of a young woman with a red umbrella standing on a rainy "
          "city street at night, 85mm lens, shallow depth of field, natural skin texture, subtle film grain")


def vram() -> int:
    result = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                            capture_output=True, text=True, timeout=20)
    return int(result.stdout.strip().splitlines()[0])


def run(pipeline: str, size: int) -> dict:
    steps = 8 if pipeline == "qwen21_turbo" else 25
    graph = image_comfy.build_graph(pipeline, PROMPT, width=size, height=size, steps=steps,
                                    cfg=1.0, seed=SEED,
                                    filename_prefix=f"res/{pipeline}-{size}")["prompt"]
    started = time.time()
    peak = vram()
    with httpx.Client(base_url=COMFY, timeout=300) as client:
        response = client.post("/prompt", json={"prompt": graph, "client_id": uuid.uuid4().hex})
        if response.status_code != 200:
            return {"ok": False, "error": response.text[:300]}
        prompt_id = response.json()["prompt_id"]
        item = None
        while time.time() - started < 2400:
            time.sleep(3)
            peak = max(peak, vram())
            try:
                body = client.get(f"/history/{prompt_id}").text
                item = json.loads(body).get(prompt_id) if body.strip() else None
            except Exception:
                item = None
            if item:
                break
        if not item:
            return {"ok": False, "error": "timeout"}
        status = item.get("status", {})
        images = [image for output in (item.get("outputs") or {}).values()
                  for image in (output.get("images") or [])]
        if status.get("status_str") != "success" or not images:
            errors = [payload.get("exception_message", "")[:200] for kind, payload
                      in status.get("messages", []) if kind == "execution_error"]
            return {"ok": False, "error": str(errors)[:300]}
        name = images[0]
        blob = client.get("/view", params={"filename": name["filename"],
                                           "subfolder": name.get("subfolder", ""),
                                           "type": name.get("type", "output")}).content
    path = OUT / f"res-{size}-{pipeline}.png"
    path.write_bytes(blob)
    return {"ok": True, "path": str(path), "seconds": round(time.time() - started, 1), "peak_mib": peak}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    results = []
    for pipeline in ("qwen21_turbo", "qwen21"):
        for size in (768, 1024, 2048):
            result = run(pipeline, size)
            result.update({"pipeline": pipeline, "size": size})
            results.append(result)
            label = f"{pipeline:14} {size:5}"
            if result["ok"]:
                print(f"  ok   {label} {result['seconds']:7.1f} s  peak {result['peak_mib']} MiB", flush=True)
            else:
                print(f"  FAIL {label} {str(result.get('error'))[:120]}", flush=True)

    ok = [row for row in results if row["ok"]]
    if ok:
        tile = 512
        columns = len({row["size"] for row in ok})
        rows = len({row["pipeline"] for row in ok})
        sizes = sorted({row["size"] for row in ok})
        pipelines = [p for p in ("qwen21_turbo", "qwen21")
                     if any(row["pipeline"] == p for row in ok)]
        sheet = Image.new("RGB", (columns * tile + 12 * (columns + 1), rows * (tile + 30) + 12),
                          (24, 24, 26))
        draw = ImageDraw.Draw(sheet)
        for r, pipeline in enumerate(pipelines):
            for c, size in enumerate(sizes):
                match = next((row for row in ok if row["pipeline"] == pipeline and row["size"] == size), None)
                if not match:
                    continue
                x = 12 + c * (tile + 12)
                y = 12 + r * (tile + 30)
                draw.text((x, y), f"{pipeline} {size}²  {match['seconds']}s  {match['peak_mib']}MiB",
                          fill=(235, 235, 235))
                sheet.paste(Image.open(match["path"]).convert("RGB").resize((tile, tile), Image.LANCZOS),
                            (x, y + 18))
        sheet.save(OUT / "sheet-resolution.png")
        print("sheet:", OUT / "sheet-resolution.png")
    (OUT / "resolution-report.json").write_text(json.dumps(results, ensure_ascii=False, indent=2),
                                               encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
