"""Get the whole DirectML error for the fused graph, and try the variants that could
make it loadable.

The per-operator probe already cleared TopK, CumSum, Less, Gather and Slice on
DirectML, so the refusal is about the graph as a whole rather than one exotic op.
Three things are worth separating:

  * the full native error text, which the onnxruntime python binding otherwise
    destroys by decoding GBK as UTF-8
  * fp32 instead of fp16, in case the mixed-precision fp16/fp32 graph is the problem
  * graph optimisation off, since DML does its own fusion at session build time
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort

MODEL_DIR = Path(r"D:\AI\Qwen-Local\tools\Qwen3-TTS-GGUF\model-custom")
FP16 = MODEL_DIR / "qwen3_tts_predictor_fused.fp16.onnx"
FP32 = MODEL_DIR / "qwen3_tts_predictor_fused.fp32.onnx"


def full_error(exc: BaseException) -> str:
    if isinstance(exc, UnicodeDecodeError):
        for codec in ("gbk", "mbcs", "latin-1"):
            try:
                return " ".join(exc.object.decode(codec, errors="replace").split())
            except Exception:
                continue
    return " ".join(str(exc).split())


def attempt(path: Path, provider: str, disable_opt: bool = False, run: bool = True):
    if not path.exists():
        print("  %-24s missing" % path.name)
        return None
    options = ort.SessionOptions()
    options.log_severity_level = 3
    if disable_opt:
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    label = "%s%s" % (provider.replace("ExecutionProvider", ""), " no-opt" if disable_opt else "")
    started = time.perf_counter()
    try:
        session = ort.InferenceSession(str(path), sess_options=options, providers=[provider])
    except Exception as exc:  # noqa: BLE001
        print("  %-16s INIT FAILED: %s" % (label, full_error(exc)[:600]))
        return None
    build = time.perf_counter() - started
    actual = session.get_providers()[0]
    print("  %-16s init ok in %.1fs, provider=%s" % (label, build, actual))
    if not run:
        return session
    dtype = np.float16 if "fp16" in path.name else np.float32
    feed = {
        "talker_hidden": np.zeros((1, 2048), dtype=dtype),
        "code0": np.array([1], dtype=np.int64),
        "uniforms": np.full(15, 0.5, dtype=np.float32),
    }
    try:
        for _ in range(3):
            session.run(["codes"], feed)
        started = time.perf_counter()
        for _ in range(20):
            session.run(["codes"], feed)
        per_frame = (time.perf_counter() - started) / 20 * 1000
        print("  %-16s %.2f ms/frame -> %.3f s per second of audio"
              % (label, per_frame, per_frame * 12.5 / 1000))
        return session
    except Exception as exc:  # noqa: BLE001
        print("  %-16s RUN FAILED: %s" % (label, full_error(exc)[:600]))
        return None


def main():
    print("onnxruntime", ort.__version__, "|", ort.get_available_providers())

    print("\n=== fp16 graph ===")
    attempt(FP16, "CPUExecutionProvider", run=False)
    attempt(FP16, "DmlExecutionProvider")
    attempt(FP16, "DmlExecutionProvider", disable_opt=True)

    if "--export-fp32" in sys.argv:
        print("\n=== exporting fp32 ===")
        sys.path.insert(0, str(Path(__file__).parent))
        import export_fused_predictor as exporter
        exporter.export(FP32, "fp32", 17, False)

    print("\n=== fp32 graph ===")
    attempt(FP32, "CPUExecutionProvider")
    attempt(FP32, "DmlExecutionProvider")
    attempt(FP32, "DmlExecutionProvider", disable_opt=True)


if __name__ == "__main__":
    main()
