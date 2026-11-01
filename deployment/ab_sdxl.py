"""A/B: Qwen-Image 2.1 Turbo (8 steps) vs Flux.2 Klein (4 steps) on the same prompts.

Same seed and 1024x1024 for both, four prompts chosen to separate the models:
portrait anatomy, prompt-following, in-image text, and a product/scene shot.

    python ab_qwen_vs_flux.py
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

OUT = Path(r"D:\AI\Qwen-Local\logs\turbo-test")
COMFY = "http://127.0.0.1:8188"
SEED = 20250912

PROMPTS = {
    "portrait": ("photo of a handsome young asian man, athletic build, natural sunlight, "
                 "detailed skin texture, looking at camera, 50mm portrait"),
    "prompt_follow": ("a woman reading a book in a sunlit cafe, wooden table, croissant and "
                      "cup of coffee, shallow depth of field, candid"),
    "text": ("a minimalist coffee shop poster, large bold sans-serif text 'SALE 50%' centered, "
             "cream background, small line 'this weekend only' underneath, flat vector style"),
    "scene": ("a brutalist concrete library at dusk, warm interior lights, wet pavement "
              "reflections, wide angle architecture photography"),
}


SIZE = 1024


def run(pipeline: str, prompt: str, tag: str) -> dict:
    steps, cfg = (8, 1.0) if pipeline == "qwen21_turbo" else (4, 1.0)
    graph = image_comfy.build_graph(pipeline, prompt, width=SIZE, height=SIZE, steps=steps,
                                    cfg=cfg, seed=SEED, filename_prefix=f"ab/{pipeline}-{tag}")["prompt"]
    started = time.time()
    with httpx.Client(base_url=COMFY, timeout=120) as client:
        response = client.post("/prompt", json={"prompt": graph, "client_id": uuid.uuid4().hex})
        if response.status_code != 200:
            return {"ok": False, "error": response.text[:200]}
        prompt_id = response.json()["prompt_id"]
        item = None
        while time.time() - started < 900:
            time.sleep(1.5)
            item = client.get(f"/history/{prompt_id}").json().get(prompt_id)
            if item:
                break
        if not item:
            return {"ok": False, "error": "timeout"}
        status = item.get("status", {})
        images = [image for output in (item.get("outputs") or {}).values()
                  for image in (output.get("images") or [])]
        if status.get("status_str") != "success" or not images:
            return {"ok": False, "error": str([p for k, p in status.get("messages", [])
                                               if k == "execution_error"])[:300]}
        name = images[0]
        blob = client.get("/view", params={"filename": name["filename"],
                                           "subfolder": name.get("subfolder", ""),
                                           "type": name.get("type", "output")}).content
    path = OUT / f"ab-{pipeline}-{tag}.png"
    path.write_bytes(blob)
    return {"ok": True, "path": str(path), "seconds": round(time.time() - started, 1)}


def main() -> int:
    import os
    global SIZE
    SIZE = int(os.environ.get("AB_SIZE", "1024"))
    only = [t.strip() for t in os.environ.get("AB_TAGS", "").split(",") if t.strip()]
    results = []
    prompts = {k: v for k, v in PROMPTS.items() if not only or k in only}
    for tag, prompt in prompts.items():
        for pipeline in ("sdxl",):
            result = run(pipeline, prompt, tag)
            result.update({"tag": tag, "pipeline": pipeline})
            results.append(result)
            print(("  ok  " if result["ok"] else "  FAIL ") + f"{pipeline:14} {tag:14} " +
                  (f"{result.get('seconds')} s" if result["ok"] else str(result.get("error"))[:80]), flush=True)

    ok = [row for row in results if row["ok"]]
    if not ok:
        print("nothing generated")
        return 1
    tile = 512
    tags = list(prompts)
    columns = ["sdxl"]
    sheet = Image.new("RGB", (tile * 2 + 36, (tile + 26) * len(tags) + 24), (24, 24, 26))
    draw = ImageDraw.Draw(sheet)
    y = 12
    for tag in tags:
        draw.text((14, y + 4), tag, fill=(235, 235, 235))
        x = 12
        for column in columns:
            match = next((row for row in ok if row["tag"] == tag and row["pipeline"] == column), None)
            if match:
                sheet.paste(Image.open(match["path"]).convert("RGB").resize((tile, tile), Image.LANCZOS),
                            (x, y + 22))
            x += tile + 12
        y += tile + 26
    out = OUT / "sheet-ab-qwen-vs-flux.png"
    sheet.save(out)
    print("sheet:", out)
    (OUT / "ab-report.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
