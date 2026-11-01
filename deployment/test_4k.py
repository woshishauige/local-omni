"""Can this machine make a 4K image? Two routes, measured.

A. direct 4K from Qwen-Turbo (3840x2160, 8 steps) -- one shot, whatever it costs;
B. two-stage: native 2048 render -> SeedVR2 x2 (4096) -- the usual workflow.

Both report wall time and peak VRAM, and keep their outputs for a crop comparison.

    python test_4k.py
"""

from __future__ import annotations

import json
import shutil
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
INPUT = Path(r"D:\AI\ComfyUI-BSAI_v010\ComfyUI\input")
SEED = 20250912
PROMPT = ("photorealistic photograph of a young woman with a red umbrella standing on a rainy "
          "city street at night, 85mm lens, shallow depth of field, natural skin texture, subtle film grain")

WIDE = (3840, 2160)


def vram() -> int:
    result = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                            capture_output=True, text=True, timeout=20)
    return int(result.stdout.strip().splitlines()[0])


def submit(graph: dict, tag: str) -> dict:
    started = time.time()
    peak = vram()
    with httpx.Client(base_url=COMFY, timeout=300) as client:
        response = client.post("/prompt", json={"prompt": graph, "client_id": uuid.uuid4().hex})
        if response.status_code != 200:
            return {"ok": False, "error": response.text[:300]}
        prompt_id = response.json()["prompt_id"]
        item = None
        while time.time() - started < 3600:
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
            errors = [payload.get("exception_message", "")[:250] for kind, payload
                      in status.get("messages", []) if kind == "execution_error"]
            return {"ok": False, "error": str(errors)[:400], "seconds": round(time.time() - started, 1)}
        name = images[0]
        blob = client.get("/view", params={"filename": name["filename"],
                                           "subfolder": name.get("subfolder", ""),
                                           "type": name.get("type", "output")}).content
    path = OUT / f"4k-{tag}.png"
    path.write_bytes(blob)
    size = Image.open(path).size
    return {"ok": True, "path": str(path), "seconds": round(time.time() - started, 1),
            "peak_mib": peak, "size": list(size)}


def main() -> int:
    results = []

    # A. direct 4K from the 8-step model
    graph = image_comfy.build_graph("qwen21_turbo", PROMPT, width=WIDE[0], height=WIDE[1],
                                    steps=8, cfg=1.0, seed=SEED,
                                    filename_prefix="4k/direct")["prompt"]
    direct = submit(graph, "direct-turbo")
    direct["route"] = "direct 4K (turbo, 8 steps)"
    results.append(direct)
    print("direct:", json.dumps({k: direct.get(k) for k in ("ok", "seconds", "peak_mib", "size", "error")},
                                ensure_ascii=False)[:300], flush=True)

    # B. 2048 -> SeedVR2 x2
    source = OUT / "res-2048-qwen21.png"
    if source.is_file():
        shutil.copyfile(source, INPUT / "native2048.png")
        graph = image_comfy.build_graph("seedvr2", "", references=["native2048.png"],
                                        scale=2.0, steps=3, cfg=1.0, seed=SEED,
                                        filename_prefix="4k/seedvr2")["prompt"]
        staged = submit(graph, "seedvr2-x2")
        staged["route"] = "2048 -> SeedVR2 x2"
        results.append(staged)
        print("seedvr2:", json.dumps({k: staged.get(k) for k in ("ok", "seconds", "peak_mib", "size", "error")},
                                     ensure_ascii=False)[:300], flush=True)

    ok = [row for row in results if row.get("ok")]
    if ok:
        crops = []
        for row in ok:
            image = Image.open(row["path"]).convert("RGB")
            width = int(image.width * 0.22)
            left = int(image.width * 0.30)
            top = int(image.height * 0.10)
            crops.append((row["route"], image.crop((left, top, left + width, top + width)).resize((480, 480))))
        sheet = Image.new("RGB", (len(crops) * 490 + 10, 540), (24, 24, 26))
        draw = ImageDraw.Draw(sheet)
        for index, (label, crop) in enumerate(crops):
            x = 10 + index * 490
            draw.text((x, 6), label, fill=(235, 235, 235))
            sheet.paste(crop, (x, 28))
        sheet.save(OUT / "sheet-4k.png")
        print("sheet:", OUT / "sheet-4k.png")
    (OUT / "4k-report.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
