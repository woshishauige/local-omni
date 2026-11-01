"""Transpose every 2-D tensor of the converted encoder in place.

llama.cpp stores linear weights as [in, out]; transformers (and therefore
CLIPLoader) wants [out, in]. The first conversion missed the non-Q8 tensors, so
this pass rewrites the file with every matrix flipped. One tensor at a time, so
peak memory stays around the largest matrix (~1.2 GiB).
"""

from __future__ import annotations

import json
import shutil
import struct
from pathlib import Path

import numpy as np

TARGET = Path(r"D:\AI\ComfyUI-BSAI_v010\ComfyUI\models\text_encoders\qwen3vl_8b_heretic_fp16.safetensors")
SOURCE = TARGET.with_name("qwen3vl_8b_heretic_fp16.original.safetensors")


def main() -> int:
    if not TARGET.is_file():
        print("missing:", TARGET)
        return 1
    if not SOURCE.is_file():
        shutil.move(TARGET, SOURCE)

    with SOURCE.open("rb") as handle:
        header_len = struct.unpack("<Q", handle.read(8))[0]
        header = json.loads(handle.read(header_len))
        data_start = 8 + header_len
        entries = [(name, spec) for name, spec in header.items() if name != "__metadata__"]

        offsets = {}
        cursor = 0
        for name, spec in entries:
            shape = spec["shape"]
            count = int(np.prod(shape)) if shape else 1
            size = count * 2  # F16
            offsets[name] = (cursor, size)
            cursor += size
        total = cursor

        new_header = {"__metadata__": header.get("__metadata__", {"format": "pt"})}
        cursor = 0
        for name, spec in entries:
            shape = spec["shape"]
            size = offsets[name][1]
            new_header[name] = {"dtype": "F16", "shape": shape, "data_offsets": [cursor, cursor + size]}
            cursor += size
        blob = json.dumps(new_header, separators=(",", ":")).encode("utf-8")
        blob += b" " * ((8 - len(blob) % 8) % 8)

        temporary = TARGET.with_name(TARGET.name + ".asm")
        with temporary.open("wb") as out:
            out.write(struct.pack("<Q", len(blob)))
            out.write(blob)
            for index, (name, spec) in enumerate(entries):
                offset, size = offsets[name]
                handle.seek(data_start + offset)
                raw = handle.read(size)
                array = np.frombuffer(raw, dtype=np.float16).reshape(spec["shape"])
                if array.ndim == 2:
                    array = array.T.copy()
                out.write(np.ascontiguousarray(array, dtype=np.float16).tobytes())
                if (index + 1) % 50 == 0:
                    print(f"  {index + 1}/{len(entries)}", flush=True)
        temporary.replace(TARGET)
    print(f"rewritten {TARGET} ({TARGET.stat().st_size / 1024**3:.2f} GiB, expected {total / 1024**3:.2f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
