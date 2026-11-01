"""Full style matrix for Qwen-Image 2.1 at 1024²: nine styles x base/turbo.

The earlier sweep ran the nine styles on Turbo at 768² (too small for Qwen) and only
three on the base model. This fills in the whole grid at the model's comfortable
native size, same seed, so style drift and step-count effects can be compared.

    python styles_full.py
"""

from __future__ import annotations

import json
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
SIZE = 1024
SUBJECT = "a young woman with a red umbrella standing on a rainy city street at night"

STYLES = {
    "01-photoreal": f"photorealistic photograph of {SUBJECT}, 85mm lens, shallow depth of field, "
                    "natural skin texture, subtle film grain",
    "02-anime": f"japanese anime illustration of {SUBJECT}, cel shading, clean line art, "
                "vibrant colors, detailed background",
    "03-oil": f"oil painting of {SUBJECT}, thick impasto brush strokes, rich warm colors, "
              "canvas texture, classical portrait style",
    "04-watercolor": f"watercolor painting of {SUBJECT}, soft washes, wet-on-wet blending, "
                     "visible paper texture, loose brushwork",
    "05-ink": f"chinese ink wash painting of {SUBJECT}, sumi ink, minimalist composition, "
              "rice paper, calligraphic brush strokes, large negative space",
    "06-3d": f"3D rendered character of {SUBJECT}, stylized feature animation look, soft global "
             "illumination, subsurface scattering, clean 4k render",
    "07-cyberpunk": f"cyberpunk scene of {SUBJECT}, neon signs and holograms, rain reflections, "
                    "teal and magenta lighting, cinematic wide shot",
    "08-pixel": f"pixel art of {SUBJECT}, 32 bit era sprite style, limited palette, crisp pixels, "
                "retro game art",
    "09-poster": "bold movie poster, the title text 'NEON RAIN' in large white sans-serif letters "
                 "across the top, a woman with a red umbrella in a rainy neon street below, "
                 "minimalist typography, high contrast",
}

PIPELINES = ("qwen21_turbo", "qwen21")


def run(pipeline: str, tag: str, prompt: str) -> dict:
    steps = 8 if pipeline == "qwen21_turbo" else 25
    graph = image_comfy.build_graph(pipeline, prompt, width=SIZE, height=SIZE, steps=steps,
                                    cfg=1.0, seed=SEED,
                                    filename_prefix=f"styles2/{pipeline}-{tag}")["prompt"]
    started = time.time()
    with httpx.Client(base_url=COMFY, timeout=300) as client:
        response = client.post("/prompt", json={"prompt": graph, "client_id": uuid.uuid4().hex})
        if response.status_code != 200:
            return {"ok": False, "error": response.text[:300]}
        prompt_id = response.json()["prompt_id"]
        item = None
        while time.time() - started < 1200:
            time.sleep(2)
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
    path = OUT / f"style1024-{tag}-{pipeline}.png"
    path.write_bytes(blob)
    return {"ok": True, "path": str(path), "seconds": round(time.time() - started, 1)}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    results = []
    for pipeline in PIPELINES:
        for tag, prompt in STYLES.items():
            result = run(pipeline, tag, prompt)
            result.update({"pipeline": pipeline, "tag": tag})
            results.append(result)
            label = f"{pipeline:14} {tag:14}"
            print(("  ok   " if result["ok"] else "  FAIL ") + label +
                  (f"{result['seconds']:6.1f} s" if result["ok"] else str(result.get("error"))[:90]),
                  flush=True)

    ok = [row for row in results if row["ok"]]
    if ok:
        tile = 400
        columns = len(STYLES)
        sheet = Image.new("RGB", (columns * tile + 10 * (columns + 1),
                                   len(PIPELINES) * (tile + 28) + 12), (24, 24, 26))
        draw = ImageDraw.Draw(sheet)
        for r, pipeline in enumerate(PIPELINES):
            for c, tag in enumerate(STYLES):
                match = next((row for row in ok if row["pipeline"] == pipeline and row["tag"] == tag), None)
                if not match:
                    continue
                x = 10 + c * (tile + 10)
                y = 12 + r * (tile + 28)
                draw.text((x, y), f"{tag} · {pipeline}", fill=(235, 235, 235))
                sheet.paste(Image.open(match["path"]).convert("RGB").resize((tile, tile), Image.LANCZOS),
                            (x, y + 16))
        sheet.save(OUT / "sheet-styles-1024.png")
        print("sheet:", OUT / "sheet-styles-1024.png")
    (OUT / "styles1024-report.json").write_text(json.dumps(results, ensure_ascii=False, indent=2),
                                                encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
