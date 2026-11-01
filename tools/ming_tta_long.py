"""Generate a long text-to-audio clip with Ming-omni-tts (TTA path).

Measured constraints this is built around:
  * the LLM runs at 3.1 frames per second, so TARGET seconds needs about TARGET * 3.1 decode steps;
  * RTF on this laptop is ~3.0, so a two-minute clip is a multi-minute job (run it in the background);
  * a single call often ends early on its own EOS, so the clip is assembled from segments until the
    target length is reached. Segments are cross-faded by a few milliseconds because raw concatenation
    of room-tone-heavy audio clicks at the join.

Usage:
    python tools/ming_tta_long.py --seconds 120 --out D:\...\oral-2min.wav \
        --text "soft wet licking and gentle kissing mouth sounds, close to the microphone, ASMR"
"""

import argparse
import importlib.util
import os
import sys
import time
from pathlib import Path

REPO = Path(r"D:\AI\Qwen-Local\tools\Ming-omni-tts")
MODEL = Path(r"D:\AI\Qwen-Local\models\Ming-omni-tts-0.5B")

os.chdir(REPO)
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "cookbooks"))

TTA_PROMPT = "Please generate audio events based on given text.\n"
FRAMES_PER_SECOND = 3.125          # 12.5 Hz codec / patch 4, as reported by the model config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument("--text", default="soft wet licking and gentle kissing mouth sounds, "
                                          "close to the microphone, continuous ASMR")
    parser.add_argument("--out", required=True)
    parser.add_argument("--cfg", type=float, default=4.5)
    parser.add_argument("--sigma", type=float, default=0.3)
    parser.add_argument("--temperature", type=float, default=2.5)
    parser.add_argument("--max-attempts", type=int, default=14)
    # Each call ends on its own EOS after ~10.5 s, so a two-minute clip is ~12 segments.
    parser.add_argument("--target-rms-db", type=float, default=-24.0,
                        help="loudness the clip is lifted to, in dBFS; the gain is capped so the peak "
                             "stays under 0.98 instead of hard-clipping")
    args = parser.parse_args()

    import numpy as np
    import soundfile as sf
    import torch

    spec = importlib.util.spec_from_file_location("ming_cookbook", REPO / "cookbooks" / "test.py")
    book = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(book)

    started = time.monotonic()
    model = book.MingAudio(str(MODEL))
    rate = model.sample_rate
    print(f"  loaded in {time.monotonic() - started:.1f}s   sample_rate {rate} Hz")
    print(f"  target {args.seconds:.0f}s of audio   text={args.text!r}")

    pieces = []
    produced = 0.0
    for attempt in range(1, args.max_attempts + 1):
        remaining = max(args.seconds - produced, 0.0)
        if remaining < 0.5:
            break
        # Ask for just what is missing, plus a small margin; the model stops at its own EOS anyway.
        steps = min(460, int(remaining * FRAMES_PER_SECOND) + 12)
        t0 = time.monotonic()
        waveform = model.speech_generation(prompt=TTA_PROMPT, text=args.text,
                                           max_decode_steps=steps, cfg=args.cfg, sigma=args.sigma,
                                           temperature=args.temperature, output_wav_path=None)
        samples = waveform.detach().float().cpu().numpy()
        while samples.ndim > 1:
            samples = samples[0] if samples.shape[0] == 1 else samples.T
        seconds = len(samples) / rate
        produced += seconds
        pieces.append(samples)
        print(f"  segment {attempt}: {steps} steps -> {seconds:.2f}s in {time.monotonic() - t0:.1f}s "
              f"(total {produced:.2f}s)")

    if not pieces:
        print("  nothing generated")
        return 1

    fade = int(rate * 0.02)                       # 20 ms cross-fade at each join
    combined = pieces[0]
    for piece in pieces[1:]:
        n = min(fade, len(combined), len(piece))
        if n > 1:
            ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
            combined = np.concatenate([
                combined[:-n],
                combined[-n:] * (1.0 - ramp) + piece[:n] * ramp,
                piece[n:],
            ])
        else:
            combined = np.concatenate([combined, piece])

    combined = combined[: int(args.seconds * rate)]

    # The raw decode is very quiet (measured rms ~0.008, -42 dBFS: near-field mouth sounds) and some
    # segments overshoot full scale. One gain fixes both: lift towards the requested loudness, but
    # never past the point where the loudest sample clips.
    raw_rms = float(np.sqrt(np.mean(combined.astype(np.float64) ** 2)))
    raw_peak = float(np.abs(combined).max())
    wanted = 10 ** (args.target_rms_db / 20.0)
    gain = min(wanted / max(raw_rms, 1e-9), 0.98 / max(raw_peak, 1e-9))
    gain = max(gain, 1e-6)
    combined = (combined * gain).astype(np.float32)
    print(f"  gain x{gain:.2f} ({20 * np.log10(max(gain, 1e-9)):+.1f} dB): "
          f"raw rms {raw_rms:.5f} / peak {raw_peak:.3f} -> rms {np.sqrt(np.mean(combined.astype(np.float64) ** 2)):.4f}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out), combined, rate)

    duration = len(combined) / rate
    peak = float(np.abs(combined).max())
    rms = float(np.sqrt(np.mean(combined.astype(np.float64) ** 2)))
    print(f"\n  wrote {out}")
    print(f"  {duration:.2f}s @ {rate} Hz   {out.stat().st_size:,} bytes")
    print(f"  peak {peak:.3f}  rms {rms:.4f}  clipped samples {int((np.abs(combined) >= 0.999).sum())}")
    print(f"  wall clock {time.monotonic() - started:.1f}s for {duration:.1f}s of audio "
          f"(RTF {(time.monotonic() - started) / duration:.2f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
