"""Probe the ONNX decoder against each execution provider and report the real error.

onnxruntime decodes the native (locale-encoded) DirectML error text as UTF-8, so a
failing session surfaces as a bare UnicodeDecodeError. The raw bytes are still on
the exception object, so this script recovers them instead of guessing.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

import numpy as np
import onnxruntime as ort

MODEL = Path(r"D:\AI\Qwen-Local\tools\Qwen3-TTS-GGUF\model-custom")
NUM_LAYERS = 8
NUM_HEADS = 16
HEAD_DIM = 64


def salvage(exc: BaseException) -> str:
    """Best-effort readable text for an exception, decoding raw bytes if present."""
    if isinstance(exc, UnicodeDecodeError):
        raw = exc.object
        for codec in ("gbk", "mbcs", "latin-1"):
            try:
                return "<%s> %s" % (codec, raw.decode(codec, errors="replace"))
            except Exception:
                continue
        return repr(raw)
    return "%s: %s" % (type(exc).__name__, exc)


def report_inputs(sess):
    for item in sess.get_inputs():
        print("   in : %-22s %-16s %s" % (item.name, item.type, item.shape))
    for item in sess.get_outputs():
        print("   out: %-22s %-16s %s" % (item.name, item.type, item.shape))


def build_feed(dtype):
    feed = {
        "audio_codes": np.zeros((1, 12, 16), dtype=np.int64),
        "is_last": np.array([1.0], dtype=dtype),
        "pre_conv_history": np.zeros((1, 512, 72), dtype=dtype),
        "latent_buffer": np.zeros((1, 1024, 72), dtype=dtype),
        "conv_history": np.zeros((1, 1024, 72), dtype=dtype),
    }
    for i in range(NUM_LAYERS):
        feed["past_key_%d" % i] = np.zeros((1, NUM_HEADS, 72, HEAD_DIM), dtype=dtype)
        feed["past_value_%d" % i] = np.zeros((1, NUM_HEADS, 72, HEAD_DIM), dtype=dtype)
    return feed


def try_model(path: Path, provider: str, plain_options: bool):
    label = "%s / %s%s" % (path.name, provider, " (default opts)" if plain_options else "")
    print("\n=== %s ===" % label)
    if not path.exists():
        print("   missing")
        return
    opts = ort.SessionOptions()
    opts.log_severity_level = 3
    if not plain_options:
        opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
        opts.add_session_config_entry("session.inter_op.allow_spinning", "0")
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    try:
        sess = ort.InferenceSession(str(path), sess_options=opts, providers=[provider])
    except Exception as exc:  # noqa: BLE001
        print("   SESSION FAILED -> " + salvage(exc))
        return
    print("   providers:", sess.get_providers())
    try:
        float_input = next(i for i in sess.get_inputs() if "float" in i.type)
        dtype = np.float16 if "float16" in float_input.type else np.float32
        print("   float dtype:", dtype.__name__, "| graph inputs:", len(sess.get_inputs()))
        feed = build_feed(dtype)
        outputs = sess.run(None, feed)
        print("   RUN OK, %d outputs, out0 shape %s" % (len(outputs), np.asarray(outputs[0]).shape))
    except Exception as exc:  # noqa: BLE001
        print("   RUN FAILED -> " + salvage(exc))
        traceback.print_exc(limit=3)


def main():
    print("onnxruntime", ort.__version__)
    print("available providers:", ort.get_available_providers())
    print("default providers for this machine:", ort.get_device())
    plain = "--plain" in sys.argv
    for name in ("qwen3_tts_decoder.fp16.onnx", "qwen3_tts_decoder.fp32.onnx", "qwen3_tts_decoder.int8.onnx"):
        for provider in ("DmlExecutionProvider", "CPUExecutionProvider"):
            try_model(MODEL / name, provider, plain)


if __name__ == "__main__":
    main()
