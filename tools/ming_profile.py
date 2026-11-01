"""Where does Ming-omni-tts actually spend its time on this machine?

The question this answers: is the measured RTF ~3.0 the autoregressive LLM's fault (which a 4-bit
quantization could help) or the decoder's (which it cannot)? The two are timed separately by
wrapping the audio tokenizer's `decode`, so the split is measured rather than argued.

It also runs the same text twice, once with the default classifier-free guidance `cfg=2.0` (two
forward passes per decode step) and once with `cfg=1.0` (one), which is the other lever whose size
can be measured instead of guessed.

Usage:
    python tools/ming_profile.py [--steps 200]
"""

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(r"D:\AI\Qwen-Local\tools\Ming-omni-tts")
MODEL = Path(r"D:\AI\Qwen-Local\models\Ming-omni-tts-0.5B")
OUT = Path(r"D:\AI\Qwen-Local\tools\bench-out\ming")

os.chdir(REPO)
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "cookbooks"))

TTS_PROMPT = "Please generate speech based on the following description.\n"
TEXT = "今天天气不错，适合出门透透气或者去公园散散步。"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--steps", type=int, default=200)
    args = parser.parse_args()

    import soundfile as sf
    import torch

    spec = importlib.util.spec_from_file_location("ming_cookbook", REPO / "cookbooks" / "test.py")
    book = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(book)

    model = book.MingAudio(str(MODEL))
    # `self.audio = AudioVAE(...)` on the model, and `sample()` is the autoregressive loop; timing
    # them separately is the whole point, so the decoder cannot be blamed for the LLM's cost.
    target = model.model
    tokenizer = target.audio

    # Wrap the decoder once so every call is attributed, including the ones inside generate().
    spent = {"decode": 0.0, "decode_calls": 0, "sample": 0.0, "sample_calls": 0}
    original = tokenizer.decode

    def timed_decode(*a, **kw):
        started = time.monotonic()
        try:
            return original(*a, **kw)
        finally:
            spent["decode"] += time.monotonic() - started
            spent["decode_calls"] += 1

    tokenizer.decode = timed_decode

    original_sample = target.sample

    def timed_sample(*a, **kw):
        started = time.monotonic()
        try:
            return original_sample(*a, **kw)
        finally:
            spent["sample"] += time.monotonic() - started
            spent["sample_calls"] += 1

    target.sample = timed_sample

    results = {}
    for label, cfg in (("cfg2.0 (default)", 2.0), ("cfg1.0 (no guidance)", 1.0)):
        spent["decode"] = 0.0
        spent["decode_calls"] = 0
        spent["sample"] = 0.0
        spent["sample_calls"] = 0
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.synchronize()
        started = time.monotonic()
        waveform = model.speech_generation(prompt=TTS_PROMPT, text=TEXT,
                                           max_decode_steps=args.steps, cfg=cfg,
                                           output_wav_path=None)
        torch.cuda.synchronize()
        total = time.monotonic() - started
        decode = spent["decode"]
        sample = spent["sample"]
        other = max(total - decode - sample, 0.0)
        samples = waveform.detach().float().cpu().numpy()
        while samples.ndim > 1:
            samples = samples[0] if samples.shape[0] == 1 else samples.T
        path = OUT / f"profile-cfg{str(cfg).replace('.', '_')}.wav"
        sf.write(str(path), samples, model.sample_rate)
        audio = len(samples) / model.sample_rate
        results[label] = {
            "wall_seconds": round(total, 2),
            "audio_seconds": round(audio, 2),
            "rtf": round(total / audio, 3) if audio else None,
            "sample_seconds": round(sample, 2),
            "sample_share": round(sample / total, 3) if total else None,
            "decode_seconds": round(decode, 2),
            "decode_share": round(decode / total, 3) if total else None,
            "other_seconds": round(other, 2),
            "peak_vram_gib": round(torch.cuda.max_memory_allocated() / 2**30, 2),
        }
        r = results[label]
        print(f"\n=== {label} ===")
        print(f"  wall {r['wall_seconds']}s  for {r['audio_seconds']}s audio   RTF {r['rtf']}")
        print(f"  LLM sample loop : {r['sample_seconds']}s  ({r['sample_share']*100:.0f}%)  "
              f"{spent['sample_calls']} call(s)")
        print(f"  VAE decoder     : {r['decode_seconds']}s  ({r['decode_share']*100:.0f}%)  "
              f"{spent['decode_calls']} call(s)")
        print(f"  prefill + rest  : {r['other_seconds']}s")
        print(f"  peak VRAM {r['peak_vram_gib']} GiB")

    summary = OUT / "profile.json"
    summary.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  written to {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
