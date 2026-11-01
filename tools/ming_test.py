"""Measure Ming-omni-tts-0.5B on this machine, against the questions that decide adoption.

The four numbers that matter are the same four that decide every voice decision here:
time to audio (is it faster than the current Qwen3-TTS at RTF 0.6-0.78?), peak VRAM (does it fit a
12 GB card beside nothing else, let alone beside the 9B?), whether it can produce the non-verbal
sounds the current codec cannot carry, and whether the speech is intelligible.

Everything here is measured, not quoted from the model card. The card's claims are evaluated on
H800/H20; this is a laptop 5070 Ti with 12 GB.

Usage:
    python tools/ming_test.py [--cases tts,voice,tta] [--steps 200]
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

# The cookbook opens `sentence_manager/default_config.yaml` relative to the working directory and
# imports `spkemb_extractor` from the repository root, so both have to be true before it loads.
os.chdir(REPO)
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "cookbooks"))

TTS_PROMPT = "Please generate speech based on the following description.\n"
TTA_PROMPT = "Please generate audio events based on given text.\n"

CASES = {
    # name: (prompt, text, instruction, extra decode args, what question it answers)
    "tts": (TTS_PROMPT, "今天天气不错，适合出门透透气或者去公园散散步。", None, {},
            "plain Chinese speech: first-audio latency, RTF, intelligibility"),
    "voice": (TTS_PROMPT, "你想跟我聊点什么？", {"IP": "灵小安", "情感": "高兴"}, {},
              "does a built-in voice id plus an emotion instruction actually change the delivery?"),
    "clone": (TTS_PROMPT, "这句话是用你自己的声音说的。", None, {},
              "skipped unless a reference wav is given; zero-shot needs prompt_wav_path"),
    "tta": (TTA_PROMPT, "slow heavy breathing, soft breathing close to the microphone", None,
            {"cfg": 4.5, "sigma": 0.3, "temperature": 2.5},
            "the non-verbal case: the current codec cannot carry this at all (measured -2 dB SNR)"),
}


def load_cookbook():
    """Import cookbooks/test.py under its own name.

    It cannot be imported as `test`: that shadows the stdlib package of the same name, which some of
    the model's dependencies import.
    """
    spec = importlib.util.spec_from_file_location("ming_cookbook", REPO / "cookbooks" / "test.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MingAudio


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default="tts,voice,tta")
    parser.add_argument("--steps", type=int, default=200)
    args = parser.parse_args()

    import torch
    import soundfile as sf

    OUT.mkdir(parents=True, exist_ok=True)
    print(f"  model dir : {MODEL}  ({sum(f.stat().st_size for f in MODEL.rglob('*') if f.is_file())/2**30:.2f} GiB on disk)")
    print(f"  cuda      : {torch.cuda.get_device_name(0)}  "
          f"{torch.cuda.get_device_properties(0).total_memory/2**30:.1f} GiB")

    MingAudio = load_cookbook()
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()
    started = time.monotonic()
    model = MingAudio(str(MODEL))
    load_seconds = time.monotonic() - started
    load_peak = torch.cuda.max_memory_allocated() / 2**30
    print(f"\n=== load ===\n  {load_seconds:.1f}s   peak VRAM {load_peak:.2f} GiB   "
          f"sample_rate {model.sample_rate} Hz   patch {model.patch_size}")

    results = {}
    for name in [c for c in args.cases.split(",") if c.strip()]:
        case = CASES.get(name)
        if not case:
            print(f"  (unknown case {name})")
            continue
        prompt, text, instruction, extra, question = case
        if name == "clone":
            print("  clone: skipped (needs a reference recording to test zero-shot)")
            continue
        out = OUT / f"{name}.wav"
        torch.cuda.reset_peak_memory_stats()
        started = time.monotonic()
        try:
            # `output_wav_path=None` keeps the cookbook from saving: torchaudio 2.11's save() now
            # requires torchcodec (plus a matching FFmpeg build), which this machine does not have.
            # The waveform comes back as a tensor instead and is written here with soundfile, which
            # is already present and is what the rest of this project uses.
            waveform = model.speech_generation(
                prompt=prompt, text=text, instruction=instruction,
                max_decode_steps=args.steps, output_wav_path=None, **extra)
            samples = waveform.detach().float().cpu().numpy()
            while samples.ndim > 1:
                samples = samples[0] if samples.shape[0] == 1 else samples.T
            sf.write(str(out), samples, model.sample_rate)
        except Exception as exc:                        # noqa: BLE001 - a failed case is a result
            print(f"\n=== {name} ===\n  FAILED: {type(exc).__name__}: {exc}")
            results[name] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        wall = time.monotonic() - started
        peak = torch.cuda.max_memory_allocated() / 2**30
        data, rate = sf.read(str(out))
        duration = len(data) / rate
        results[name] = {
            "wall_seconds": round(wall, 2), "audio_seconds": round(duration, 2),
            "rtf": round(wall / duration, 3) if duration else None,
            "sample_rate": rate, "peak_vram_gib": round(peak, 2),
            "rms": round(float(abs(data).mean()), 5), "bytes": out.stat().st_size,
            "question": question,
        }
        print(f"\n=== {name} ===\n  {question}")
        print(f"  wall {wall:.2f}s   audio {duration:.2f}s @ {rate} Hz   RTF {wall/duration:.3f}   "
              f"peak VRAM {peak:.2f} GiB")
        print(f"  wrote {out.name} ({out.stat().st_size:,} bytes, mean|x| {float(abs(data).mean()):.4f})")

    summary = OUT / "results.json"
    summary.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  summary written to {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
