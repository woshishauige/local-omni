"""Style sweep for Qwen-Image 2.1: one subject, nine styles.

Turbo (8 steps) renders every style; the base model (25 steps) repeats two of them
so any style drift from distillation shows up. Same seed throughout, so the only
variable is the style words.

    python styles_qwen.py
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
SIZE = 768

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

BASE_STYLES = ("01-photoreal", "02-anime", "09-poster")


def vram() -> int:
    import subprocess
    result = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                            capture_output=True, text=True, timeout=20)
    return int(result.stdout.strip().splitlines()[0])


def run(pipeline: str, tag: str, prompt: str) -> dict:
    steps = 8 if pipeline == "qwen21_turbo" else 25
    graph = image_comfy.build_graph(pipeline, prompt, width=SIZE, height=SIZE, steps=steps,
                                    cfg=1.0, seed=SEED, filename_prefix=f"styles/{pipeline}-{tag}")["prompt"]
    started = time.time()
    with httpx.Client(base_url=COMFY, timeout=180) as client:
        response = client.post("/prompt", json={"prompt": graph, "client_id": uuid.uuid4().hex})
        if response.status_code != 200:
            return {"ok": False, "error": response.text[:300]}
        prompt_id = response.json()["prompt_id"]
        item = None
        while time.time() - started < 900:
            time.sleep(1.5)
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
    path = OUT / f"style-{tag}-{pipeline}.png"
    path.write_bytes(blob)
    return {"ok": True, "path": str(path), "seconds": round(time.time() - started, 1)}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    results = []
    for tag, prompt in STYLES.items():
        result = run("qwen21_turbo", tag, prompt)
        result.update({"tag": tag, "pipeline": "qwen21_turbo"})
        results.append(result)
        print(("  ok   " if result["ok"] else "  FAIL ") + f"turbo {tag:14} " +
              (f"{result.get('seconds')} s" if result["ok"] else str(result.get("error"))[:90]), flush=True)
    for tag in BASE_STYLES:
        result = run("qwen21", tag, STYLES[tag])
        result.update({"tag": tag, "pipeline": "qwen21"})
        results.append(result)
        print(("  ok   " if result["ok"] else "  FAIL ") + f"base  {tag:14} " +
              (f"{result.get('seconds')} s" if result["ok"] else str(result.get("error"))[:90]), flush=True)

    ok = [row for row in results if row["ok"]]
    tiles = [(row["tag"], row["path"]) for row in ok]
    if tiles:
        columns = 3
        rows = (len(tiles) + columns - 1) // columns
        tile = 420
        sheet = Image.new("RGB", (columns * tile + 12 * (columns + 1), rows * (tile + 26) + 12),
                          (24, 24, 26))
        draw = ImageDraw.Draw(sheet)
        for index, (tag, path) in enumerate(tiles):
            column, row = index % columns, index // columns
            x = 12 + column * (tile + 12)
            y = 12 + row * (tile + 26)
            draw.text((x, y), tag, fill=(235, 235, 235))
            sheet.paste(Image.open(path).convert("RGB").resize((tile, tile), Image.LANCZOS), (x, y + 18))
        sheet.save(OUT / "sheet-styles.png")
        print("sheet:", OUT / "sheet-styles.png")
    (OUT / "styles-report.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
