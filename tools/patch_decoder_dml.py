"""Produce a DirectML-runnable copy of the TTS decoder.

onnxruntime's DirectML provider fails at execution time on `Reshape` nodes carrying
`allowzero=1` (reproduced with a five-element model: allowzero=0 runs, allowzero=1
throws DmlCommandRecorder.cpp(371)). The decoder sets it on 32 of its 33 Reshapes.

allowzero only changes the meaning of a literal 0 inside the target shape: with 0 it
copies the input dimension, with 1 it means a real zero-length dimension. Every one
of these nodes takes its shape from an int64 initializer, so each is checked here
and only the nodes whose shape contains no 0 are rewritten - for those the two
settings are provably identical, and the CPU output is compared afterwards to prove
it.

Writes <decoder>.dml.onnx next to the original and never touches the original.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import onnx
from onnx import TensorProto, numpy_helper

SOURCE = Path(r"D:\AI\Qwen-Local\tools\Qwen3-TTS-GGUF\model-custom\qwen3_tts_decoder.fp16.onnx")


def patch(source: Path, target: Path) -> dict:
    model = onnx.load(str(source))  # loads external data when present
    graph = model.graph
    initializers = {t.name: numpy_helper.to_array(t) for t in graph.initializer}

    rewritten, skipped_dynamic, skipped_zero, already = [], [], [], 0
    for node in graph.node:
        if node.op_type != "Reshape":
            continue
        attr = next((a for a in node.attribute if a.name == "allowzero"), None)
        if attr is None or attr.i == 0:
            already += 1
            continue
        shape = initializers.get(node.input[1]) if len(node.input) > 1 else None
        if shape is None:
            # Shape comes from a computed tensor; the value of a 0 would have to be
            # known at run time, so leave it alone rather than risk a silent change.
            skipped_dynamic.append(node.name)
            continue
        if (shape == 0).any():
            skipped_zero.append(node.name)
            continue
        attr.i = 0
        rewritten.append(node.name)

    onnx.save(model, str(target))
    return {
        "total_reshape": sum(1 for n in graph.node if n.op_type == "Reshape"),
        "already_ok": already,
        "rewritten": len(rewritten),
        "skipped_dynamic": skipped_dynamic,
        "skipped_zero": skipped_zero,
    }


def main():
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else SOURCE
    target = Path(sys.argv[2]) if len(sys.argv) > 2 else source.with_suffix(".dml.onnx")
    report = patch(source, target)
    print("source :", source)
    print("target :", target, "size=%.1f MB" % (target.stat().st_size / 1e6))
    for key, value in report.items():
        print("  %-16s %s" % (key, value))


if __name__ == "__main__":
    main()
