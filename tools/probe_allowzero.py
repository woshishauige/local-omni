"""Confirm the DirectML Reshape failure is caused by allowzero=1, and count how
many nodes in the real decoder carry the pattern.

The control is the same graph with allowzero left at its default of 0. The shape
tensor the decoder uses is [1, -1, 16, 64], which contains no zero, so the two
settings are semantically identical - if only the allowzero=1 build fails, the
decoder can be patched without changing a single output value.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import onnx
import onnxruntime as ort
from onnx import TensorProto, helper, numpy_helper

MODEL = Path(r"D:\AI\Qwen-Local\tools\Qwen3-TTS-GGUF\model-custom\qwen3_tts_decoder.fp16.onnx")
TEMP = Path(os.environ.get("TEMP", "."))


def salvage(exc: BaseException) -> str:
    if isinstance(exc, UnicodeDecodeError):
        return "<gbk> " + exc.object.decode("gbk", errors="replace").replace("\r", " ").replace("\n", " ")[:160]
    return ("%s: %s" % (type(exc).__name__, exc)).replace("\r", " ").replace("\n", " ")[:160]


def toy(allowzero):
    nodes = [helper.make_node("Reshape", ["x", "s"], ["y"], allowzero=allowzero)]
    graph = helper.make_graph(
        nodes, "toy",
        [helper.make_tensor_value_info("x", TensorProto.FLOAT16, [1, "n", 1024])],
        [helper.make_tensor_value_info("y", TensorProto.FLOAT16, [1, "n", 16, 64])],
        initializer=[numpy_helper.from_array(np.array([1, -1, 16, 64], dtype=np.int64), "s")],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 10
    path = TEMP / ("toy_allowzero_%d.onnx" % allowzero)
    onnx.save(model, str(path))
    return path


def control():
    feed = {"x": np.random.rand(1, 5, 1024).astype(np.float16)}
    for allowzero in (0, 1):
        path = toy(allowzero)
        for provider in ("CPUExecutionProvider", "DmlExecutionProvider"):
            opts = ort.SessionOptions()
            opts.log_severity_level = 4
            try:
                sess = ort.InferenceSession(str(path), sess_options=opts, providers=[provider])
                out = sess.run(None, feed)[0]
                print("allowzero=%d %-24s OK   %s   providers=%s"
                      % (allowzero, provider, out.shape, sess.get_providers()))
            except Exception as exc:  # noqa: BLE001
                print("allowzero=%d %-24s FAIL %s" % (allowzero, provider, salvage(exc)))


def scan():
    graph = onnx.load(str(MODEL), load_external_data=False).graph
    init = {t.name: numpy_helper.to_array(t) for t in graph.initializer if t.data_type == TensorProto.INT64}
    allowzero_nodes = [n for n in graph.node if n.op_type == "Reshape"
                       and any(a.name == "allowzero" and a.i == 1 for a in n.attribute)]
    print("\ndecoder Reshape nodes: %d, with allowzero=1: %d"
          % (sum(1 for n in graph.node if n.op_type == "Reshape"), len(allowzero_nodes)))
    zero_in_shape = 0
    for node in allowzero_nodes:
        shape = init.get(node.input[1]) if len(node.input) > 1 else None
        has_zero = shape is not None and bool((shape == 0).any())
        if has_zero:
            zero_in_shape += 1
            print("   %s shape=%s  <-- contains a literal 0, allowzero matters"
                  % (node.name, shape.tolist()))
    print("   allowzero=1 nodes whose shape has a literal 0 (unsafe to rewrite): %d" % zero_in_shape)


if __name__ == "__main__":
    control()
    scan()
