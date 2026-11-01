"""Decide whether DirectML is broken in general on this machine, or only on the
decoder graph's Reshape.

Three independent checks:
  1. what the failing Reshape's shape tensor actually contains
  2. whether DirectML can run a five-node toy graph at all (Reshape allowzero=1,
     MatMul, Conv, Softmax)
  3. which DirectML.dll the runtime resolves, since a bundled older DirectML does
     not know newer GPU architectures
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

import numpy as np
import onnx
import onnxruntime as ort
from onnx import TensorProto, helper, numpy_helper

MODEL = Path(r"D:\AI\Qwen-Local\tools\Qwen3-TTS-GGUF\model-custom\qwen3_tts_decoder.fp16.onnx")


def salvage(exc: BaseException) -> str:
    if isinstance(exc, UnicodeDecodeError):
        for codec in ("gbk", "mbcs", "latin-1"):
            try:
                return raw_decode(exc, codec)
            except Exception:
                continue
    return "%s: %s" % (type(exc).__name__, exc)


def raw_decode(exc, codec):
    text = exc.object.decode(codec, errors="replace")
    return "<%s> %s" % (codec, text.replace("\r", " ").replace("\n", " ")[:400])


def show_shape_tensor():
    graph = onnx.load(str(MODEL), load_external_data=False).graph
    for tensor in graph.initializer:
        if tensor.name == "val_1311":
            print("val_1311 dtype=%s dims=%s values=%s"
                  % (TensorProto.DataType.Name(tensor.data_type), list(tensor.dims),
                     numpy_helper.to_array(tensor).tolist()))
    for info in list(graph.value_info) + list(graph.output):
        if info.name in ("view_1", "linear_2"):
            dims = [d.dim_value if d.HasField("dim_value") else (d.dim_param or "?")
                    for d in info.type.tensor_type.shape.dim]
            print("%s dims=%s" % (info.name, dims))


def toy_graph():
    """Minimal graph touching the same op classes the decoder uses."""
    nodes = [
        helper.make_node("Reshape", ["x", "shape"], ["r"], allowzero=1),
        helper.make_node("MatMul", ["r", "w"], ["m"]),
        helper.make_node("Softmax", ["m"], ["s"], axis=-1),
    ]
    graph = helper.make_graph(
        nodes, "toy",
        inputs=[helper.make_tensor_value_info("x", TensorProto.FLOAT16, [1, 4, 8])],
        outputs=[helper.make_tensor_value_info("s", TensorProto.FLOAT16, [None, None])],
        initializer=[
            numpy_helper.from_array(np.array([1, 2, 4, 8], dtype=np.int64), "shape"),
            numpy_helper.from_array(np.ones((8, 8), dtype=np.float16), "w"),
        ],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 10
    return model


def run_toy():
    model = toy_graph()
    path = Path(os.environ.get("TEMP", ".")) / "dml_toy.onnx"
    onnx.save(model, str(path))
    feed = {"x": np.random.rand(1, 4, 8).astype(np.float16)}
    for provider in ("CPUExecutionProvider", "DmlExecutionProvider"):
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        try:
            sess = ort.InferenceSession(str(path), sess_options=opts, providers=[provider])
            out = sess.run(None, feed)[0]
            print("toy %-24s OK  shape=%s" % (provider, out.shape))
        except Exception as exc:  # noqa: BLE001
            print("toy %-24s FAIL %s" % (provider, salvage(exc)))


def dll_inventory():
    candidates = [
        Path(r"D:\AI\Qwen-Local\tools\tts-venv\Lib\site-packages\onnxruntime\capi\DirectML.dll"),
        Path(r"D:\AI\Qwen-Local\tools\tts-venv\Lib\site-packages\onnxruntime\capi\onnxruntime_providers_dml.dll"),
        Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "DirectML.dll",
    ]
    for path in candidates:
        if not path.exists():
            print("%-100s missing" % str(path))
            continue
        size = path.stat().st_size
        try:
            version = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
            text = ""
            if version:
                buf = ctypes.create_string_buffer(version)
                ctypes.windll.version.GetFileVersionInfoW(str(path), 0, version, buf)
                ptr = ctypes.c_void_p()
                length = ctypes.c_uint()
                if ctypes.windll.version.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)):
                    data = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint * 13)).contents
                    text = "%d.%d.%d.%d" % (data[10] >> 16, data[10] & 0xFFFF, data[11] >> 16, data[11] & 0xFFFF)
            print("%-100s %9d bytes  version=%s" % (str(path), size, text or "?"))
        except Exception as exc:  # noqa: BLE001
            print("%-100s %9d bytes  version lookup failed: %s" % (str(path), size, exc))


def main():
    print("onnxruntime", ort.__version__)
    print("available:", ort.get_available_providers())
    print("\n--- failing reshape tensor ---")
    show_shape_tensor()
    print("\n--- toy graph on each provider ---")
    run_toy()
    print("\n--- DirectML runtime files ---")
    dll_inventory()


if __name__ == "__main__":
    main()
