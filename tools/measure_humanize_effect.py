"""Did the human-likeness changes move the audio in the intended direction?

The checks that ran earlier proved the mechanisms are wired: the instruction reaches the engine,
the seed is no longer pinned, the pause is present in the clip. None of that says the result is
better, and "better" is a judgement only an ear can make. What can be measured is whether each
change moves the audio the way it was supposed to:

  * `instruct` - does 温柔 actually come out slower, 耳语 quieter? If every delivery merely differs
    without moving in its stated direction, the feature is a randomiser, not a control.
  * `min_p` - the engine's own note says 0.05 "过滤低概率噪声，减少电音". Spectral flatness is the
    usual proxy: noise-like content sits near 1.0, a clear tone near 0.0. If the new setting does
    not lower it, that claim does not hold here and should be said so.
  * the seed - already shown to make renders differ; what is measured here is how much the
    delivery varies, which is the thing a fixed seed was flattening.

The A/B runs on the processor (QWEN_TTS_CPU=1) so it cannot disturb whatever holds the card, and
both arms use the same device, so the comparison stays fair.

Usage: tools\\tts-venv\\Scripts\\python.exe tools\\measure_humanize_effect.py
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

HUMANIZE = Path(r"D:\AI\Qwen-Local\tools\bench-out\humanize")
AB = HUMANIZE / "ab"
PY = r"D:\AI\Qwen-Local\tools\tts-venv\Scripts\python.exe"
WORKER = r"D:\AI\Qwen-Local\tools\tts_server.py"
CWD = r"D:\AI\Qwen-Local\tools\Qwen3-TTS-GGUF"
LINE = "今天天气不错，我们出去走走吧。"

# The styles the panel offers, in the order it offers them.
STYLE_FILES = [("默认", "style-默认.wav"), ("温柔", "style-温柔.wav"),
               ("轻快", "style-轻快.wav"), ("耳语", "style-耳语.wav")]

# Each arm states what it is: the first restores the pinned configuration this project used
# before, the second is what ships now.
ARMS = [
    ("pinned seed, min_p 0 (the old settings)",
     {"QWEN_TTS_SEED": "42", "QWEN_TTS_SUB_SEED": "45", "QWEN_TTS_MIN_P": "0",
      "QWEN_TTS_TEMPERATURE": "0.6"}),
    ("released seed, min_p 0.05 (what ships now)", {}),
]


def analyse(path):
    audio, rate = sf.read(str(path), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    length = len(audio) / rate
    rms = float(np.sqrt(np.mean(audio ** 2)))

    window = int(0.04 * rate)
    hop = int(0.02 * rate)
    f0s, flats = [], []
    for start in range(0, max(0, len(audio) - window), hop):
        frame = audio[start:start + window]
        if float(np.sqrt(np.mean(frame ** 2))) < 1e-4:
            continue
        spectrum = np.abs(np.fft.rfft(frame * np.hanning(window))) + 1e-12
        flats.append(float(np.exp(np.mean(np.log(spectrum))) / np.mean(spectrum)))
        centred = frame - frame.mean()
        auto = np.correlate(centred, centred, mode="full")[len(centred) - 1:]
        low, high = int(rate / 400), int(rate / 60)
        if auto[0] <= 0 or high >= len(auto) or high <= low:
            continue
        segment = auto[low:high]
        peak = int(np.argmax(segment)) + low
        if auto[peak] / auto[0] > 0.3:
            f0s.append(rate / peak)

    return {
        "seconds": round(length, 2),
        "rms": round(rms, 4),
        "f0": round(float(np.median(f0s)), 1) if f0s else 0.0,
        "f0_sd": round(float(np.std(f0s)), 1) if f0s else 0.0,
        "flat": round(float(np.mean(flats)), 4) if flats else 0.0,
    }


def run_worker(env_extra, tag, lines):
    """One worker process, several sentences, its own engine - used to change sampling settings,
    which the gateway fixes for the whole worker and cannot vary per request."""
    AB.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(env_extra)
    env["PYTHONUNBUFFERED"] = "1"
    env["QWEN_TTS_CPU"] = "1"
    worker = subprocess.Popen(
        [PY, "-X", "utf8", WORKER],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", bufsize=1, cwd=CWD, env=env,
    )
    made = []
    for index, text in enumerate(lines):
        worker.stdin.write(json.dumps({"text": text, "speaker": "uncle_fu", "language": "chinese"},
                                      ensure_ascii=False) + "\n")
        worker.stdin.flush()
        while True:
            line = worker.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            break
        else:
            payload = {"error": "worker stopped answering"}
        if payload.get("error"):
            print(f"   {tag}[{index}] failed: {payload['error']}")
            continue
        target = AB / f"{tag}-{index}.wav"
        target.write_bytes(Path(payload["path"]).read_bytes())
        made.append(target)
    worker.stdin.close()
    try:
        worker.wait(timeout=30)
    except subprocess.TimeoutExpired:
        worker.kill()
    return made


def main():
    print("=== 1. does each style move in the direction it claims? ===")
    print(f"{'style':<6} {'seconds':>8} {'rms':>8} {'F0 Hz':>7} {'F0 sd':>7} {'flatness':>9}")
    base = None
    rows = {}
    for label, name in STYLE_FILES:
        path = HUMANIZE / name
        if not path.is_file():
            print(f"{label:<6} (missing {name})")
            continue
        stats = analyse(path)
        rows[label] = stats
        if label == "默认":
            base = stats
        print(f"{label:<6} {stats['seconds']:>8} {stats['rms']:>8} {stats['f0']:>7} "
              f"{stats['f0_sd']:>7} {stats['flat']:>9}")
    if base:
        print("\nrelative to 默认:")
        for label, stats in rows.items():
            if label == "默认":
                continue
            print(f"  {label}: 时长 {stats['seconds'] / base['seconds']:.2f}x"
                  f"  音量 {stats['rms'] / base['rms']:.2f}x"
                  f"  音高 {stats['f0'] - base['f0']:+.1f} Hz"
                  f"  起伏 {stats['f0_sd'] / base['f0_sd']:.2f}x")
        print("  读法：温柔/平静 应更慢（时长偏大），耳语 应更轻（音量偏小），")
        print("        轻快 应更快且音高起伏更大。若都在 1.0 附近或方向相反，说明指令只是让它不同。")

    print("\n=== 2. pinned vs released sampling, same sentence three times ===")
    for title, env_extra in ARMS:
        print(f"\n-- {title}")
        made = run_worker(env_extra, "old" if env_extra else "new", [LINE] * 3)
        if not made:
            print("   nothing synthesised")
            continue
        stats = [analyse(path) for path in made]
        for index, row in enumerate(stats):
            print(f"   take {index + 1}: {row['seconds']:>6}s  F0 {row['f0']:>6} Hz"
                  f"  flatness {row['flat']}")
        seconds = [row["seconds"] for row in stats]
        f0s = [row["f0"] for row in stats]
        flats = [row["flat"] for row in stats]
        print(f"   spread: duration {max(seconds) - min(seconds):.2f}s, "
              f"F0 {max(f0s) - min(f0s):.1f} Hz, "
              f"flatness {max(flats) - min(flats):.4f} "
              f"(mean {np.mean(flats):.4f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
