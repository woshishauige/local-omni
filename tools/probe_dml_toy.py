"""Can DirectML run anything at all on this machine?

Five tiny models, each a single op class from the TTS decoder, loaded and run on
CPU and DirectML. If the trivial matmul/softmax pair passes on DML, DirectML is
healthy and only the decoder graph needs surgery; if even that fails, the bundled
DirectML build cannot drive this GPU and the GPU decoder path is a dead end.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import onnx
import onnxruntime as ort
from onnx import TensorProto, helper, numpy_helper

TEMP = Path(os.environ.get("TEMP", "."))


def salvage(exc: BaseException) -> str:
    if isinstance(exc, UnicodeDecodeError):
        return "<gbk> " + exc.object.decode("gbk", errors="replace").replace("\r", " ").replace("\n", " ")[:200]
    return ("%s: %s" % (type(exc).__name__, exc)).replace("\r", " ").replace("\n", " ")[:200]


def save(nodes, inputs, outputs, initializer, name):
    graph = helper.make_graph(nodes, name, inputs, outputs, initializer=initializer)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 10
    path = TEMP / ("dml_%s.onnx" % name)
    onnx.save(model, str(path))
    return path


def case_matmul_softmax():
    nodes = [
        helper.make_node("MatMul", ["x", "w"], ["m"]),
        helper.make_node("Softmax", ["m"], ["y"], axis=-1),
    ]
    return save(
        nodes,
        [helper.make_tensor_value_info("x", TensorProto.FLOAT16, [1, 4, 8])],
        [helper.make_tensor_value_info("y", TensorProto.FLOAT16, [1, 4, 8])],
        [numpy_helper.from_array(np.ones((8, 8), dtype=np.float16), "w")],
        "matmul_softmax",
    ), {"x": np.random.rand(1, 4, 8).astype(np.float16)}


def case_reshape_allowzero():
    nodes = [helper.make_node("Reshape", ["x", "s"], ["y"], allowzero=1)]
    return save(
        nodes,
        [helper.make_tensor_value_info("x", TensorProto.FLOAT16, [1, "n", 1024])],
        [helper.make_tensor_value_info("y", TensorProto.FLOAT16, [1, "n", 16, 64])],
        [numpy_helper.from_array(np.array([1, -1, 16, 64], dtype=np.int64), "s")],
        "reshape_allowzero",
    ), {"x": np.random.rand(1, 5, 1024).astype(np.float16)}


def case_reshape_dynamic():
    """Same reshape, but shaped from the runtime Shape of the input instead of a
    constant - the pattern the decoder actually uses upstream of the failure."""
    nodes = [
        helper.make_node("Shape", ["x"], ["sh"], start=0, end=2),
        helper.make_node("Concat", ["sh", "tail"], ["s"], axis=0),
        helper.make_node("Reshape", ["x", "s"], ["y"], allowzero=1),
    ]
    return save(
        nodes,
        [helper.make_tensor_value_info("x", TensorProto.FLOAT16, [1, "n", 1024])],
        [helper.make_tensor_value_info("y", TensorProto.FLOAT16, [1, "n", 16, 64])],
        [numpy_helper.from_array(np.array([16, 64], dtype=np.int64), "tail")],
        "reshape_dynamic",
    ), {"x": np.random.rand(1, 5, 1024).astype(np.float16)}


def case_conv():
    nodes = [helper.make_node("Conv", ["x", "w"], ["y"], pads=[1, 1])]
    return save(
        nodes,
        [helper.make_tensor_value_info("x", TensorProto.FLOAT16, [1, 8, 16])],
        [helper.make_tensor_value_info("y", TensorProto.FLOAT16, [1, 4, 16])],
        [numpy_helper.from_array(np.ones((4, 8, 3), dtype=np.float16) * 0.1, "w")],
        "conv",
    ), {"x": np.random.rand(1, 8, 16).astype(np.float16)}


CASES = {
    "matmul_softmax": case_matmul_softmax,
    "reshape_allowzero": case_reshape_allowzero,
    "reshape_dynamic": case_reshape_dynamic,
    "conv": case_conv,
}


def main():
    print("onnxruntime", ort.__version__, "| available:", ort.get_available_providers())
    only = sys.argv[1:] or list(CASES)
    for name in only:
        path, feed = CASES[name]()
        for provider in ("CPUExecutionProvider", "DmlExecutionProvider"):
            opts = ort.SessionOptions()
            opts.log_severity_level = 4
            try:
                sess = ort.InferenceSession(str(path), sess_options=opts, providers=[provider])
                out = sess.run(None, feed)[0]
                print("%-20s %-24s OK   %s" % (name, provider, out.shape))
            except Exception as exc:  # noqa: BLE001
                print("%-20s %-24s FAIL %s" % (name, provider, salvage(exc)))


if __name__ == "__main__":
    main()
