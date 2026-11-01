"""Convert unsloth's prequant FP8 transformer into a ComfyUI-loadable checkpoint.

unsloth ships `Qwen-Image-2.1-Turbo-FP8.safetensors` with keys like
`transformer_blocks.0.attn.to_q.weight` plus per-row `...to_q.scale` tensors and a
custom metadata block. ComfyUI's UNETLoader expects `model.diffusion_model.`
prefixed keys and `.weight_scale` for fp8 scaled weights, so this script streams
the file and rewrites the header, leaving the tensor bytes untouched.

    python convert_turbo_fp8.py            # write the converted file
    python convert_turbo_fp8.py --check    # only validate the mapping
"""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path

MODELS = Path(r"D:\AI\ComfyUI-BSAI_v010\ComfyUI\models\diffusion_models")
SOURCE = MODELS / "Qwen-Image-2.1-Turbo-FP8.safetensors"
TARGET = MODELS / "Qwen-Image-2.1-Turbo-FP8-ComfyUI.safetensors"
PREFIX = "model.diffusion_model."


def read_header(path: Path) -> tuple[dict, int]:
    with path.open("rb") as handle:
        length = struct.unpack("<Q", handle.read(8))[0]
        header = json.loads(handle.read(length))
    return header, 8 + length


def rewritten_key(key: str) -> str:
    """unsloth quantised-tensor container -> ComfyUI fp8-scaled checkpoint.

    Every quantised linear ships two tensors:
      transformer_blocks.N.attn.to_q._weight_qdata   (F8_E4M3)
      transformer_blocks.N.attn.to_q._weight_scale   (F32 per-row)
    ComfyUI wants `...to_q.weight` + `...to_q.weight_scale`, prefixed with
    `model.diffusion_model.`.
    """
    if key.startswith(PREFIX):
        return key
    if key.endswith("._weight_qdata"):
        return PREFIX + key[: -len("._weight_qdata")] + ".weight"
    if key.endswith("._weight_scale"):
        return PREFIX + key[: -len("._weight_scale")] + ".weight_scale"
    mapped = PREFIX + key
    if mapped.endswith(".scale"):
        mapped = mapped[: -len(".scale")] + ".weight_scale"
    return mapped


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not SOURCE.is_file():
        print(f"missing source: {SOURCE}")
        return 1

    header, data_start = read_header(SOURCE)
    metadata = header.pop("__metadata__", None) or {}
    print(f"source tensors: {len(header)} | metadata: {list(metadata)[:3]}")

    converted: dict[str, dict] = {}
    collision = 0
    for key, spec in header.items():
        new_key = rewritten_key(key)
        if new_key in converted:
            collision += 1
        converted[new_key] = spec
    print(f"converted tensors: {len(converted)} | collisions: {collision}")
    sample = [k for k in converted if "transformer_blocks.0.attn.to_q" in k or "txt_in" in k][:4]
    print("sample keys:", sample)
    scales = sum(1 for k in converted if k.endswith(".weight_scale"))
    print(f"weight_scale keys: {scales}")
    if args.check:
        return 0

    payload = {key: value for key, value in converted.items() if key != "__metadata__"}
    blob = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    blob += b" " * ((8 - len(blob) % 8) % 8)  # keep tensor data 8-byte aligned
    with TARGET.open("wb") as out:
        out.write(struct.pack("<Q", len(blob)))
        out.write(blob)
        with SOURCE.open("rb") as src:
            src.seek(data_start)
            while True:
                block = src.read(1024 * 1024 * 64)
                if not block:
                    break
                out.write(block)
    print(f"written {TARGET} ({TARGET.stat().st_size / 1024**3:.2f} GiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
