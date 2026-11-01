"""Verify the DirectML decoder reproduction against the CPU decoder.

Two things have to be true before the GPU decoder can be trusted:

  1. it runs at all (DirectML refused the original graph), and
  2. it produces the same waveform as the CPU build for the same codes, chunk for
     chunk, including the streaming state handoff.

Both decoders are driven through the project's own StatefulDecoder so the real
class is under test, not a re-implementation. Codes are drawn from the same
distribution the engine produces: codebook 0 has 3072 entries, the rest 2048.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

PROJECT = Path(r"D:\AI\Qwen-Local\tools\Qwen3-TTS-GGUF")
sys.path.insert(0, str(PROJECT))

MODEL_DIR = PROJECT / "model-custom"
CPU_MODEL = MODEL_DIR / "qwen3_tts_decoder.fp16.onnx"
DML_MODEL = MODEL_DIR / "qwen3_tts_decoder.fp16.dml.onnx"

FRAMES = 60
CHUNK = 12


def make_codes(frames: int, seed: int = 7) -> np.ndarray:
    # The decoder's own codebook table has 2048 entries for every group; the talker
    # sampler is range-limited to 0..2047 as well, so all 16 columns stay in range.
    rng = np.random.default_rng(seed)
    return rng.integers(0, 2048, size=(frames, 16)).astype(np.int64)


def run_stream(decoder, codes: np.ndarray):
    """Feed the codes the way the engine does and return the joined waveform."""
    import qwen3_tts_gguf.inference.schema.protocol as protocol

    state = None
    pieces = []
    times = []
    for start in range(0, len(codes), CHUNK):
        chunk = codes[start:start + CHUNK]
        is_last = start + CHUNK >= len(codes)
        t0 = time.perf_counter()
        audio, state = decoder.decode(chunk, state=state, is_final=False)
        times.append(time.perf_counter() - t0)
        pieces.append(np.asarray(audio))
    # The engine's final call carries an empty chunk and flushes the tail.
    t0 = time.perf_counter()
    audio, state = decoder.decode(np.zeros((0, 16), dtype=np.int64), state=state, is_final=True)
    times.append(time.perf_counter() - t0)
    pieces.append(np.asarray(audio))
    return np.concatenate([p for p in pieces if p.size]), times


def main():
    from qwen3_tts_gguf.inference.decoder import StatefulDecoder

    codes = make_codes(FRAMES)
    audio_seconds = FRAMES * 1920 / 24000.0
    print("codes %s, %.2fs of audio, chunk=%d" % (codes.shape, audio_seconds, CHUNK))

    print("\n--- CPU (original graph) ---")
    cpu = StatefulDecoder(str(CPU_MODEL), onnx_provider="CPU", chunk_size=CHUNK)
    print("provider:", cpu.active_provider)
    cpu_audio, cpu_times = run_stream(cpu, codes)
    cpu_total = sum(cpu_times)
    print("samples %d  total %.3fs  rtf %.3f  max chunk %.0f ms"
          % (cpu_audio.size, cpu_total, cpu_total / audio_seconds, max(cpu_times) * 1000))

    print("\n--- DirectML (patched graph) ---")
    dml = StatefulDecoder(str(DML_MODEL), onnx_provider="DML", chunk_size=CHUNK)
    print("provider:", dml.active_provider)
    dml_audio, dml_times = run_stream(dml, codes)
    dml_total = sum(dml_times)
    print("samples %d  total %.3fs  rtf %.3f  max chunk %.0f ms"
          % (dml_audio.size, dml_total, dml_total / audio_seconds, max(dml_times) * 1000))

    print("\n--- agreement ---")
    print("sample counts: cpu=%d dml=%d" % (cpu_audio.size, dml_audio.size))
    n = min(cpu_audio.size, dml_audio.size)
    if n:
        a = cpu_audio[:n].astype(np.float64)
        b = dml_audio[:n].astype(np.float64)
        diff = np.abs(a - b)
        peak = float(np.max(np.abs(a)))
        noise = float(np.mean(diff ** 2)) ** 0.5
        signal = float(np.mean(a ** 2)) ** 0.5
        print("peak amplitude %.5f" % peak)
        print("max abs diff   %.3e" % float(diff.max()))
        print("mean abs diff  %.3e" % float(diff.mean()))
        print("rms error      %.3e  -> SNR %.1f dB" % (noise, 20 * np.log10(signal / noise) if noise else float("inf")))
        print("correlation    %.6f" % float(np.corrcoef(a, b)[0, 1]))

    # A single non-recurrent pass separates op precision from drift: the streaming
    # path feeds its own fp16 state back in, so per-chunk rounding compounds.
    print("\n--- single pass, no recurrence ---")
    whole = make_codes(FRAMES)
    cpu_audio_1, _ = cpu.decode(whole, state=None, is_final=True)
    dml_audio_1, _ = dml.decode(whole, state=None, is_final=True)
    m = min(len(cpu_audio_1), len(dml_audio_1))
    a1 = cpu_audio_1[:m].astype(np.float64)
    b1 = dml_audio_1[:m].astype(np.float64)
    d1 = np.abs(a1 - b1)
    s1 = float(np.mean(a1 ** 2)) ** 0.5
    n1 = float(np.mean(d1 ** 2)) ** 0.5
    print("samples cpu=%d dml=%d  max abs diff %.3e  SNR %.1f dB"
          % (len(cpu_audio_1), len(dml_audio_1), float(d1.max()),
             20 * np.log10(s1 / n1) if n1 else float("inf")))

    print("\nspeedup: %.2fx" % (cpu_total / dml_total if dml_total else float("nan")))


if __name__ == "__main__":
    main()
