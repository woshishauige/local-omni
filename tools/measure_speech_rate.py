"""Measure the actual speaking rate of each sentence the engine produces.

"Front slow, then suddenly faster" has two very different possible causes, and they are
told apart by measuring the audio rather than the delivery:

  * the model really does speak the early sentences more slowly - then the trimmed
    speech duration per character differs between sentences
  * the audio is uniform and the difference is in how it is played back - then the
    per-character durations match and the cause is elsewhere (pipeline gaps, or two
    different voices in one reply)

Each sentence is synthesised exactly as the gateway does it (same TTSConfig, same
speaker, one request per sentence), then the waveform is trimmed to its first and last
non-silent sample. Frames x 1920 / 24000 is what the code stream says the duration
should be, and the WAV header is what the file claims; both are printed so a mismatch
between the code stream and the file would show up too.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

PROJECT = Path(r"D:\AI\Qwen-Local\tools\Qwen3-TTS-GGUF")
sys.path.insert(0, str(PROJECT))

SAMPLE_RATE = 24000
SAMPLES_PER_FRAME = 1920

# Four sentences shaped like a real reply: a short opener, then longer body sentences.
SENTENCES = [
    "你好，我在。",
    "今天天气不错，适合出门走走。",
    "顺便买点水果回来，晚上做一顿好吃的。",
    "路上注意安全。",
]


def wav_header_facts(path: Path):
    import soundfile as sf
    info = sf.info(str(path))
    return info.samplerate, info.frames


def trimmed_span(audio: np.ndarray, threshold: float = 0.01):
    """First and last sample above the threshold, i.e. the audible part."""
    loud = np.where(np.abs(audio) > threshold)[0]
    if loud.size == 0:
        return 0, 0
    return int(loud[0]), int(loud[-1])


def main():
    import os
    os.chdir(PROJECT)
    from qwen3_tts_gguf.inference import TTSEngine, TTSConfig

    on_cpu = os.environ.get("QWEN_TTS_CPU", "").strip() not in ("", "0", "false")
    provider = "CPU" if on_cpu else "GPU"
    engine = TTSEngine(model_dir="model-custom", onnx_provider=provider,
                       llm_use_gpu=not on_cpu, verbose=False, local_playback=False)
    if not engine:
        print("engine not ready")
        return 1
    stream = engine.create_stream()
    config = TTSConfig(max_steps=400, temperature=0.6, sub_temperature=0.6,
                       seed=42, sub_seed=45, streaming=True)

    print("%-34s %6s %8s %8s %8s %8s %7s" % (
        "sentence", "chars", "frames", "codes_s", "wav_s", "speech_s", "char/s"))
    print("-" * 92)

    # One throwaway synthesis first, so the numbers below are the steady state. The
    # difference between this and the first real sentence is the cold-start cost that
    # currently lands on whatever the user happens to say first.
    if "--no-warm" not in sys.argv:
        started = time.perf_counter()
        warm = stream.custom(text="你好", speaker="uncle_fu", instruct=None,
                             language="chinese", config=config)
        warm_seconds = time.perf_counter() - started
        warm_audio = len(np.asarray(warm.audio)) / SAMPLE_RATE if warm is not None else 0
        print("warm-up (discarded): wall %.2fs for %.2fs of audio -> rtf %.2f"
              % (warm_seconds, warm_audio, warm_seconds / warm_audio if warm_audio else 0))
        print("-" * 92)

    rows = []
    for index, sentence in enumerate(SENTENCES):
        started = time.perf_counter()
        result = stream.custom(text=sentence, speaker="uncle_fu", instruct=None,
                               language="chinese", config=config)
        elapsed = time.perf_counter() - started
        if result is None or result.audio is None:
            print("synthesis failed for %r" % sentence)
            return 1
        audio = np.asarray(result.audio, dtype=np.float32)
        frames = len(result.codes) if result.codes is not None else 0
        first, last = trimmed_span(audio)
        speech = (last - first + 1) / SAMPLE_RATE
        duration = len(audio) / SAMPLE_RATE
        rows.append({"chars": len(sentence), "speech": speech, "duration": duration,
                     "frames": frames, "wall": elapsed})
        print("%-34s %6d %8d %8.3f %8.3f %8.3f %7.2f" % (
            sentence[:32], len(sentence), frames, frames * SAMPLES_PER_FRAME / SAMPLE_RATE,
            duration, speech, len(sentence) / speech if speech else 0))

    print("\n-- synthesis cost --")
    for sentence, row in zip(SENTENCES, rows):
        print("  %-34s wall %.2fs  rtf %.2f" % (sentence[:32], row["wall"],
                                                row["wall"] / row["duration"] if row["duration"] else 0))

    rates = [row["chars"] / row["speech"] for row in rows if row["speech"]]
    if rates:
        print("\nchars per second of actual speech: %s" % ["%.2f" % r for r in rates])
        print("slowest %.2f  fastest %.2f  ratio %.2fx" % (min(rates), max(rates), max(rates) / min(rates)))

    # The header must agree with the code stream, or playback runs at the wrong speed.
    print("\n-- header vs code stream --")
    for sentence, row in zip(SENTENCES, rows):
        expected = row["frames"] * SAMPLES_PER_FRAME / SAMPLE_RATE
        print("  %-34s expected %.3fs  actual %.3fs  delta %+.3fs"
              % (sentence[:32], expected, row["duration"], row["duration"] - expected))
    engine.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
