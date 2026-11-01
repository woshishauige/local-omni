"""Cheap facts about the predictor's side tables.

The fused predictor assumes file 0 of the codec tables holds the talker's larger
codebook-0 table and files 1..15 hold the predictor's fifteen 2048-row tables. This
prints what is actually on disk so that assumption can be confirmed or dropped
without spending a GPU run on it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

MODEL_DIR = Path(r"D:\AI\Qwen-Local\tools\Qwen3-TTS-GGUF\model-custom")

for name in ["proj_weight.npy", "proj_bias.npy", "text_embedding_projected.npy"]:
    path = MODEL_DIR / "embeddings" / name
    if path.exists():
        array = np.load(path, mmap_mode="r")
        print("%-32s %-10s %s" % (name, array.dtype, array.shape))

print()
for index in range(16):
    path = MODEL_DIR / "embeddings" / ("codec_embedding_%d.npy" % index)
    if not path.exists():
        print("codec_embedding_%-2d missing" % index)
        continue
    array = np.load(path, mmap_mode="r")
    print("codec_embedding_%-2d %-10s %s" % (index, array.dtype, array.shape))

print("\ngguf package:", end=" ")
try:
    import gguf
    print("available", getattr(gguf, "__version__", "?"))
except Exception as exc:  # noqa: BLE001
    print("missing (%s)" % type(exc).__name__)
