"""Compare the Turbo run against the local baselines on the same prompt and seed.

Produces image statistics, a labelled contact sheet and a markdown table for the
same seed/prompt at 1024x1024 across every model run so far.

    python compare_turbo.py --seeds 20250912,777
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw

import compare_flux_qwen

BASE = Path(r"D:\AI\Qwen-Local\logs\model-comparison")
TURBO = Path(r"D:\AI\Qwen-Local\logs\turbo-test")
OUT = Path(r"D:\AI\Qwen-Local\logs\turbo-test")

# (label, path builder)
RUNS = [
    ("Qwen-2.1 25step", lambda seed: TURBO / f"qwen21-official-seed{seed}.png"),
    ("Pony LCM 8", lambda seed: BASE / f"sdxl-seed{seed}-baseline.png"),
    ("Flux2 Klein 4", lambda seed: BASE / f"flux2-seed{seed}.png"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", default="20250912,777")
    parser.add_argument("--tile", type=int, default=420)
    args = parser.parse_args()
    seeds = [int(value) for value in args.seeds.split(",") if value.strip()]
    OUT.mkdir(parents=True, exist_ok=True)

    report: dict = {"seeds": seeds, "runs": []}
    rows: dict[str, list[Image.Image]] = {}
    for label, builder in RUNS:
        images = []
        for seed in seeds:
            path = builder(seed)
            if not path.is_file():
                print(f"missing: {label} seed={seed} -> {path}")
                continue
            entry = {"label": label, "seed": seed, "image": str(path)}
            entry.update(compare_flux_qwen.image_stats(path))
            report["runs"].append(entry)
            images.append(Image.open(path).convert("RGB"))
            print(f"{label:14} seed={seed} {entry['size']} sharp={entry['sharpness_laplacian']:7.1f} "
                  f"contrast={entry['contrast']:6.2f} bright={entry['brightness']:6.2f}")
        if images:
            rows[label] = images

    if rows:
        tile = args.tile
        labels = list(rows)
        width = tile * len(seeds) + 12 * (len(seeds) + 1)
        height = (tile + 26) * len(labels) + 12 * (len(labels) + 1)
        sheet = Image.new("RGB", (width, height), (24, 24, 26))
        draw = ImageDraw.Draw(sheet)
        y = 12
        for label in labels:
            draw.text((14, y + 6), label, fill=(235, 235, 235))
            x = 12
            for image in rows[label]:
                sheet.paste(image.resize((tile, tile), Image.LANCZOS), (x, y + 26))
                x += tile + 12
            y += tile + 26 + 12
        sheet_path = OUT / "sheet-turbo-vs-baseline.png"
        sheet.save(sheet_path)
        print("contact sheet:", sheet_path)

    path = OUT / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("report:", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
