"""Load the exported GGUF TTS engine and report exactly where it fails.

The engine swallows its own exception into `logger.error(..., exc_info=True)` and
then returns with `ready = False`, so the caller only sees "初始化失败" and no
reason. This drives the same code path with logging wired to stderr so the real
traceback surfaces, and prints which optional asset is absent.

Usage:  python tts_probe.py          (run from the project root)
"""

import logging
from pathlib import Path
import sys
import traceback

logging.basicConfig(
    level=logging.DEBUG,
    format="%(levelname)-7s %(name)s: %(message)s",
    stream=sys.stderr,
)

PROJECT = Path(r"D:\AI\Qwen-Local\tools\Qwen3-TTS-GGUF")
sys.path.insert(0, str(PROJECT))

MODEL_DIR = PROJECT / "model-custom"

REQUIRED = [
    "qwen3_tts_talker.q5_k.gguf",
    "qwen3_tts_predictor.q8_0.gguf",
    "qwen3_tts_decoder.fp16.onnx",
    "qwen3_tts_codec_encoder.fp16.onnx",
    "tokenizer.json",
]
OPTIONAL = ["qwen3_tts_speaker_encoder.fp16.onnx"]


def main():
    print("--- files ---")
    for name in REQUIRED:
        print(f"  {'OK  ' if (MODEL_DIR / name).is_file() else 'MISS'}  {name}")
    for name in OPTIONAL:
        print(f"  {'OK  ' if (MODEL_DIR / name).is_file() else 'MISS'}  {name} (optional)")

    print("\n--- DecoderProxy (subprocess) ---")
    try:
        from qwen3_tts_gguf.inference.proxy import DecoderProxy
        proxy = DecoderProxy(str(MODEL_DIR / "qwen3_tts_decoder.fp16.onnx"),
                             onnx_provider="CUDA", chunk_size=12)
        ready = proxy.wait_until_ready(timeout=25)
        print(f"  ready = {ready}")
    except Exception:
        print("  FAILED:")
        traceback.print_exc()

    print("\n--- GGUF models (this is where llama-hparams aborted) ---")
    try:
        from qwen3_tts_gguf.inference import llama
        for name, label in (("qwen3_tts_talker.q5_k.gguf", "talker"),
                            ("qwen3_tts_predictor.q8_0.gguf", "predictor")):
            try:
                model = llama.LlamaModel(str(MODEL_DIR / name), n_gpu_layers=-1, use_gpu=1)
                print(f"  OK   {label}")
                del model
            except Exception as exc:  # noqa: BLE001
                print(f"  FAIL {label}: {type(exc).__name__}: {exc}")
    except Exception:
        traceback.print_exc()

    print("\n--- full TTSEngine ---")
    try:
        from qwen3_tts_gguf.inference import TTSEngine
        engine = TTSEngine(model_dir=str(MODEL_DIR), onnx_provider="CUDA", verbose=True)
        print(f"  ready = {bool(engine)}")
    except Exception:
        traceback.print_exc()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
