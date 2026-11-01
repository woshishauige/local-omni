"""Gate B, split across two processes on purpose.

torch ships its own OpenMP runtime and the llama.cpp DLLs ship another. Loading both
in one process makes the second one abort with "OMP: Error #15 ... libiomp5md.dll
already initialized", and the unsupported-workaround path can silently produce wrong
numbers - an earlier single-process version of this comparison reported 0.16%
agreement purely because of that clash. The engine run therefore happens in a process
that never imports torch, writes its frames to an npz, and the comparison happens in
a process that never loads llama.cpp.

Usage:
    python verify_gate_b.py capture  frames.npz [--llm gpu] [--runs 1]
    python verify_gate_b.py compare  frames.npz [--top-k 50] [--temperature 0.6]
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

PROJECT = Path(r"D:\AI\Qwen-Local\tools\Qwen3-TTS-GGUF")
MODEL_DIR = PROJECT / "model-custom"
TEXT = "今天天气不错，我们出门走走吧。"


def capture(args) -> int:
    """Engine side. Deliberately no torch import anywhere in this process."""
    sys.path.insert(0, str(PROJECT))
    os.chdir(PROJECT)
    from qwen3_tts_gguf.inference import TTSEngine, TTSConfig
    from qwen3_tts_gguf.inference.predictor import Predictor

    frames = []
    original = Predictor.predict_frame

    def spy(self, master_hidden, code_0, sampler=None, do_sample=True,
            temperature=0.9, top_p=1.0, top_k=50):
        codes, embeds = original(self, master_hidden, code_0, sampler=sampler,
                                 do_sample=do_sample, temperature=temperature,
                                 top_p=top_p, top_k=top_k)
        frames.append((np.asarray(master_hidden, dtype=np.float32).copy(), int(code_0),
                       np.asarray(codes, dtype=np.int64)))
        return codes, embeds

    Predictor.predict_frame = spy
    try:
        engine = TTSEngine(model_dir="model-custom", onnx_provider="GPU",
                           llm_use_gpu=(args.llm == "gpu"), verbose=False)
        if not engine:
            print("engine not ready", flush=True)
            return 1
        stream = engine.create_stream()
        # Greedy on the predictor side: the two predictors hold different weight
        # precisions, so a stochastic draw would compare noise against noise.
        config = TTSConfig(max_steps=400, sub_do_sample=False, streaming=True)
        for index in range(args.runs):
            stream.custom(text=TEXT, speaker="uncle_fu", instruct=None,
                          language="chinese", config=config)
            print("run %d captured %d frames" % (index + 1, len(frames)), flush=True)
        engine.shutdown()
    finally:
        Predictor.predict_frame = original

    hidden = np.stack([f[0] for f in frames])
    code0 = np.array([f[1] for f in frames], dtype=np.int64)
    codes = np.stack([f[2] for f in frames])
    np.savez_compressed(args.path, hidden=hidden, code0=code0, codes=codes)
    print("wrote %s  hidden=%s code0=%s codes=%s"
          % (args.path, hidden.shape, code0.shape, codes.shape), flush=True)
    return 0


def teacher_forced(model, hidden, code0, engine_codes) -> int:
    """Replay the engine's own codes through the fused module.

    Both predictors then see byte-identical inputs at every step, so any remaining
    disagreement is an implementation error rather than a consequence of the two
    weight precisions. Agreement here plus the free-running number is what shows the
    divergence is a cascade, not a bug.
    """
    import torch

    per_book = defaultdict(lambda: [0, 0])
    match = 0
    total = 0
    with torch.no_grad():
        for index in range(len(hidden)):
            h = torch.from_numpy(hidden[index]).view(1, -1)
            c0 = torch.tensor([int(code0[index])], dtype=torch.long)
            want = engine_codes[index].tolist()[1:]
            mine = model(h, c0, torch.zeros(15), greedy=True, forced=want).view(-1).tolist()
            total += len(want)
            for position, (a, b) in enumerate(zip(want, mine)):
                bucket = per_book[position]
                bucket[1] += 1
                if a == b:
                    bucket[0] += 1
                    match += 1
    print("teacher-forced position agreement: %d/%d = %.2f%%"
          % (match, total, 100.0 * match / total))
    for position in sorted(per_book):
        good, count = per_book[position]
        print("   codebook %2d: %6.2f%% (%d/%d)" % (position, 100.0 * good / count, good, count))
    return 0


def compare(args) -> int:
    """Torch side. Never loads llama.cpp."""
    sys.path.insert(0, str(Path(__file__).parent))
    import torch
    from fused_predictor import FusedCodePredictor

    data = np.load(args.path)
    hidden, code0, engine_codes = data["hidden"], data["code0"], data["codes"]
    print("frames %d, codes per frame %d" % (len(hidden), engine_codes.shape[1]), flush=True)

    model = FusedCodePredictor(MODEL_DIR, top_k=args.top_k,
                               temperature=args.temperature).eval()

    if args.teacher:
        return teacher_forced(model, hidden, code0, engine_codes)

    per_book = defaultdict(lambda: [0, 0])
    exact = 0
    match = 0
    total = 0

    with torch.no_grad():
        for index in range(len(hidden)):
            h = torch.from_numpy(hidden[index]).view(1, -1)
            c0 = torch.tensor([int(code0[index])], dtype=torch.long)
            mine = model(h, c0, torch.zeros(15), greedy=True).view(-1).tolist()
            # The engine's frame is [code_0, code_1 ... code_15]; code_0 is its input,
            # and the fused module returns the fifteen codes it actually predicts.
            want_all = engine_codes[index].tolist()[1:]
            total += len(mine)
            frame_ok = True
            for position, (want, got) in enumerate(zip(want_all, mine)):
                bucket = per_book[position]
                bucket[1] += 1
                if want == got:
                    bucket[0] += 1
                    match += 1
                else:
                    frame_ok = False
            if frame_ok:
                exact += 1
            if index < 3:
                print("  frame %d engine=%s" % (index, want_all))
                print("           fused =%s" % (mine,))

    print("\nposition agreement: %d/%d = %.2f%%" % (match, total, 100.0 * match / total))
    print("frames agreeing on all 15 codes: %d/%d = %.2f%%"
          % (exact, len(hidden), 100.0 * exact / len(hidden)))
    for position in sorted(per_book):
        good, count = per_book[position]
        print("   codebook %2d: %5.1f%% (%d/%d)" % (position, 100.0 * good / count, good, count))
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["capture", "compare"])
    parser.add_argument("path")
    parser.add_argument("--llm", default="gpu")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--top-k", type=int, default=50)
    parser.add_argument("--temperature", type=float, default=0.6)
    parser.add_argument("--teacher", action="store_true",
                        help="feed the engine's own codes forward and check every argmax")
    args = parser.parse_args()
    return capture(args) if args.action == "capture" else compare(args)


if __name__ == "__main__":
    raise SystemExit(main())
