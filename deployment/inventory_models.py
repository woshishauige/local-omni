"""Inventory the model folder and say which files nothing references.

For every file above a size threshold, count how many times its name appears in the
project's Python sources and settings. A file nobody mentions is a deletion
candidate; the report lists them with sizes and keeps the machine's total in view.

    python inventory_models.py
"""

from __future__ import annotations

import json
from pathlib import Path

MODELS = Path(r"D:\AI\ComfyUI-BSAI_v010\ComfyUI\models")
ROOT = Path(__file__).resolve().parent
EXTRA_SOURCES = [Path(r"D:\AI\deployment")]
MIN_BYTES = 100 * 1024 * 1024

# Sources that could name a model file.
def sources() -> list[Path]:
    files = [path for path in ROOT.glob("*.py") if path.is_file()]
    files.append(ROOT / "settings.json")
    for folder in EXTRA_SOURCES:
        files.extend(path for path in folder.glob("*.py") if path.is_file())
    return [path for path in files if path.is_file()]


def main() -> int:
    texts = {path: path.read_text(encoding="utf-8", errors="ignore") for path in sources()}
    rows = []
    for path in sorted(MODELS.rglob("*")):
        if not path.is_file() or path.stat().st_size < MIN_BYTES:
            continue
        name = path.name
        stem = name.split(".")[0]
        hits = sum(1 for text in texts.values() if name in text or stem in text)
        rel = path.relative_to(MODELS)
        rows.append({"path": str(rel), "size": path.stat().st_size, "references": hits})
    rows.sort(key=lambda row: (-row["size"]))
    unused = [row for row in rows if row["references"] == 0]
    total = sum(row["size"] for row in rows)
    unused_total = sum(row["size"] for row in unused)
    print(f"files >= {MIN_BYTES / 1024**2:.0f} MiB: {len(rows)} | total {total / 1024**3:.2f} GiB")
    print(f"unreferenced: {len(unused)} | {unused_total / 1024**3:.2f} GiB\n")
    print("referenced (kept):")
    for row in rows:
        if row["references"]:
            print(f"  {row['size'] / 1024**3:6.2f} GiB  n={row['references']:<3} {row['path']}")
    print("\nunreferenced (deletion candidates):")
    for row in unused:
        print(f"  {row['size'] / 1024**3:6.2f} GiB  {row['path']}")
    Path(r"D:\AI\Qwen-Local\logs\turbo-test\model-inventory.json").write_text(
        json.dumps({"rows": rows, "unused_total": unused_total}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
