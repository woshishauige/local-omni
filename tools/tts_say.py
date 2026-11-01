"""Synthesize Chinese speech with the exported CustomVoice engine.

Verifies the whole export end to end: the GGUF Talker and Predictor, the ONNX codec
encoder/decoder and the embedding tables all have to agree on shapes before any
audio comes out. Several built-in speakers render the same sentence so the voices
can be compared by ear; `eric` and `dylan` additionally exercise the dialect path.

Run from the project root (the engine resolves model_dir relative to it):
    python D:\\AI\\Qwen-Local\\tools\\tts_say.py
"""

import os
from pathlib import Path
import sys
import time

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

PROJECT = Path(r"D:\AI\Qwen-Local\tools\Qwen3-TTS-GGUF")
OUT_DIR = Path(r"D:\AI\Qwen-Local\logs\tts-out")
OUT_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(PROJECT))

from qwen3_tts_gguf.inference import TTSEngine, TTSConfig  # noqa: E402

SENTENCE = "你好，我是本地部署的语音助手，现在正在测试中文语音合成。"

CASES = [
    ("uncle_fu", None),
    ("serena", None),
    ("vivian", None),
    ("ryan", None),
    ("aiden", None),
    ("ono_anna", None),
    ("eric", None),          # 四川方言
    ("dylan", None),         # 北京方言
]


def main():
    engine = TTSEngine(model_dir="model-custom", onnx_provider="CUDA")
    if not engine:
        print("引擎初始化失败")
        return 1
    stream = engine.create_stream()

    config = TTSConfig(
        max_steps=400,
        temperature=0.6,
        sub_temperature=0.6,
        seed=42,
        sub_seed=45,
        streaming=True,
    )

    ok = failed = 0
    for speaker, instruct in CASES:
        print(f"\n>> 合成 {speaker}", flush=True)
        try:
            t0 = time.time()
            result = stream.custom(
                text=SENTENCE, speaker=speaker, instruct=instruct,
                language="chinese", config=config,
            )
            elapsed = time.time() - t0
            wav = OUT_DIR / f"{speaker}.wav"
            result.save(str(wav))
            size = wav.stat().st_size / 1024 if wav.is_file() else 0
            try:
                result.print_stats()
            except Exception:  # noqa: BLE001 - stats are cosmetic
                pass
            print(f"   OK {elapsed:.2f}s  {size:.1f} KB  -> {wav.name}", flush=True)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"   FAIL {type(exc).__name__}: {exc}", flush=True)
            failed += 1
        finally:
            try:
                stream.join()
            except Exception:  # noqa: BLE001
                pass

    engine.shutdown()
    print(f"\n成功 {ok} / 失败 {failed}")
    print(f"产物: {OUT_DIR}")
    for f in sorted(OUT_DIR.glob("*.wav")):
        print(f"  {f.name:16} {f.stat().st_size/1024:8.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
