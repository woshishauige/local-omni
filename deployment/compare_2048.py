"""1024 vs 2048 of the same render, compared at equal display scale.

Both crops cover the same subject area and are shown at the same pixel size, so the
only difference is real detail: the 2048 file contributes a native 512 px crop, the
1024 file the same region at 256 px scaled up to 512. Sharpness is measured on the
display-level crops, which is why the numbers are comparable.

    python compare_2048.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, r"D:\AI\deployment")
import compare_flux_qwen  # noqa: E402

OUT = Path(r"D:\AI\Qwen-Local\logs\style-test")

PAIRS = [
    ("photoreal · base 25 步", OUT / "res-1024-qwen21.png", OUT / "res-2048-qwen21.png"),
    ("photoreal · turbo 8 步", OUT / "res-1024-qwen21_turbo.png", OUT / "res-2048-qwen21_turbo.png"),
]

WINDOWS = [("face", 0.40, 0.22, 0.25), ("hand + handle", 0.45, 0.62, 0.25)]

CROP = 512  # display size for every crop


def region(image: Image.Image, cx: float, cy: float, fraction: float) -> tuple[int, int, int, int]:
    width = int(image.width * fraction)
    left = max(0, min(image.width - width, int(cx * image.width - width / 2)))
    top = max(0, min(image.height - width, int(cy * image.height - width / 2)))
    return left, top, left + width, top + width


def crop_pair(low: Image.Image, high: Image.Image, cx: float, cy: float, fraction: float):
    low_crop = low.crop(region(low, cx, cy, fraction))
    high_crop = high.crop(region(high, cx, cy, fraction))
    # same subject area, same display size: the 1024 contribution is upscaled 2x
    return (low_crop.resize((CROP, CROP), Image.LANCZOS),
            high_crop.resize((CROP, CROP), Image.LANCZOS))


def main() -> int:
    report = []
    rows = []
    for label, low_path, high_path in PAIRS:
        if not low_path.is_file() or not high_path.is_file():
            print("missing:", label)
            continue
        low = Image.open(low_path).convert("RGB")
        high = Image.open(high_path).convert("RGB")
        # whole image: 2048 downscaled to 1024 for pixel-parity, then compare detail of upscale path
        parity_high = high.resize((1024, 1024), Image.LANCZOS)
        tmp_a = OUT / "_a.png"
        tmp_b = OUT / "_b.png"
        low.save(tmp_a)
        parity_high.save(tmp_b)
        whole_low = compare_flux_qwen.image_stats(tmp_a)["sharpness_laplacian"]
        whole_high = compare_flux_qwen.image_stats(tmp_b)["sharpness_laplacian"]
        tmp_a.unlink(missing_ok=True)
        tmp_b.unlink(missing_ok=True)
        row = {"pair": label, "whole_1024": round(whole_low, 1), "whole_2048_downscaled": round(whole_high, 1),
               "crops": {}}
        for name, cx, cy, fraction in WINDOWS:
            low_crop, high_crop = crop_pair(low, high, cx, cy, fraction)
            a = OUT / "_ca.png"
            b = OUT / "_cb.png"
            low_crop.save(a)
            high_crop.save(b)
            sharp_low = compare_flux_qwen.image_stats(a)["sharpness_laplacian"]
            sharp_high = compare_flux_qwen.image_stats(b)["sharpness_laplacian"]
            a.unlink(missing_ok=True)
            b.unlink(missing_ok=True)
            row["crops"][name] = {"1024": round(sharp_low, 1), "2048": round(sharp_high, 1),
                                  "gain": round(sharp_high / max(1e-6, sharp_low), 2)}
        report.append(row)
        rows.append((label, low, high))
        print(f"{label}: 整图(2048缩到1024后) sharp {whole_low:.1f} vs {whole_high:.1f} | " +
              " | ".join(f"{k}: +{v['gain']}x" for k, v in row["crops"].items()), flush=True)

    if not rows:
        return 1

    tile = 440
    pad = 12
    columns = [("1024² 整图", tile), ("2048² 整图", tile)]
    for name, _cx, _cy, _f in WINDOWS:
        columns.append((f"{name} 同区域 512px", CROP))
    width = sum(size for _label, size in columns) + pad * (len(columns) + 1)
    row_height = max(tile, CROP) + 58
    sheet = Image.new("RGB", (width, row_height * len(rows) + pad), (24, 24, 26))
    draw = ImageDraw.Draw(sheet)
    for index, (label, low, high) in enumerate(rows):
        y = pad + index * row_height
        draw.text((pad, y + 4), label, fill=(240, 240, 240))
        x = pad
        images = [low.resize((tile, tile), Image.LANCZOS), high.resize((tile, tile), Image.LANCZOS)]
        for name, cx, cy, fraction in WINDOWS:
            images.extend(crop_pair(low, high, cx, cy, fraction))
        for (caption, size), image in zip(columns, images):
            draw.text((x, y + 22), caption, fill=(190, 190, 190))
            sheet.paste(image, (x, y + 40))
            x += size + pad
    out = OUT / "sheet-1024-vs-2048.png"
    sheet.save(out)
    print("sheet:", out)
    (OUT / "compare-2048.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
