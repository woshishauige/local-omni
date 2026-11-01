"""Stage-level benchmark for the Qwen3-TTS GGUF engine.

Non-invasive: it monkey-patches the shipped engine at runtime instead of editing
library files, so the production path stays exactly as delivered and any
experiment can be dropped by simply not running this script.

It answers two questions the ablation needs:

  1. Where does the wall clock actually go?  (talker sample/decode, predictor
     sample/decode/embed, decoder ONNX, numpy glue)
  2. How much of the predictor step is pure Python/numpy overhead rather than
     tensor math?  That number is the ceiling for any "keep the loop on device"
     rewrite.

Usage:
    python tools/bench_tts.py --llm cpu --decoder cpu  --runs 3
    python tools/bench_tts.py --llm gpu --decoder dml  --runs 3
    python tools/bench_tts.py --llm cpu --decoder dml  --runs 3

Prints one JSON object prefixed with @@BENCH@@ for the driver to parse.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

PROJECT = Path(r"D:\AI\Qwen-Local\tools\Qwen3-TTS-GGUF")

TEXT = "今天天气不错，我们出门走走吧。顺便买点水果回来，晚上做一顿好吃的。"


def instrument(stream, stats, counts):
    """Wrap the hot functions of a live stream with perf_counter timers."""
    from qwen3_tts_gguf.inference import llama as L
    from qwen3_tts_gguf.inference.assets import AssetsManager
    from qwen3_tts_gguf.inference.predictor import Predictor
    from qwen3_tts_gguf.inference.talker import TalkerPredictor

    def timed(bucket):
        def wrap(fn):
            def inner(*a, **kw):
                t0 = time.perf_counter()
                try:
                    return fn(*a, **kw)
                finally:
                    stats[bucket] += time.perf_counter() - t0
                    counts[bucket] += 1
            return inner
        return wrap

    # llama.cpp context decode, split by which model owns the context.
    for tag, ctx in (("talker", stream.talker_ctx), ("predictor", stream.predictor_ctx)):
        ctx.decode = timed(f"ctx_decode_{tag}")(ctx.decode)
    stream.talker.ctx = stream.talker_ctx
    stream.predictor.ctx = stream.predictor_ctx

    # Sampling. The talker call passes allow_tokens, the predictor one does not,
    # which is how the two are told apart without touching call sites.
    orig_sample = L.LlamaSampler.sample

    def sample(self, ctx, idx=-1, limit_start=None, limit_end=None, allow_tokens=None):
        bucket = "sample_talker" if allow_tokens is not None else "sample_predictor"
        t0 = time.perf_counter()
        try:
            return orig_sample(self, ctx, idx=idx, limit_start=limit_start,
                               limit_end=limit_end, allow_tokens=allow_tokens)
        finally:
            stats[bucket] += time.perf_counter() - t0
            counts[bucket] += 1

    L.LlamaSampler.sample = sample

    # Embedding lookups are an array index plus, in the predictor's case, a copy.
    orig_emb = AssetsManager.get_codec_embedding
    orig_emb1024 = AssetsManager.get_codec_embedding_1024

    def emb(self, q_idx, code):
        t0 = time.perf_counter()
        try:
            return orig_emb(self, q_idx, code)
        finally:
            stats["emb_raw"] += time.perf_counter() - t0
            counts["emb_raw"] += 1

    def emb1024(self, q_idx, code):
        t0 = time.perf_counter()
        try:
            return orig_emb1024(self, q_idx, code)
        finally:
            stats["emb_proj"] += time.perf_counter() - t0
            counts["emb_proj"] += 1

    AssetsManager.get_codec_embedding = emb
    AssetsManager.get_codec_embedding_1024 = emb1024

    stream.talker.prefill = timed("talker_prefill")(stream.talker.prefill)
    stream.talker.decode_step = timed("talker_step")(stream.talker.decode_step)
    stream.predictor.predict_frame = timed("predictor_frame")(stream.predictor.predict_frame)


def run_config(llm_device, decoder_provider, runs, warmup, speaker, max_steps):
    from qwen3_tts_gguf.inference import TTSEngine, TTSConfig

    llm_use_gpu = llm_device == "gpu"
    t_load = time.perf_counter()
    engine = TTSEngine(model_dir="model-custom", onnx_provider=decoder_provider,
                       llm_use_gpu=llm_use_gpu, verbose=False)
    if not engine:
        raise RuntimeError("engine not ready")
    load_seconds = time.perf_counter() - t_load

    stream = engine.create_stream()
    if stream is None:
        raise RuntimeError("stream creation failed")

    cfg = TTSConfig(max_steps=max_steps, temperature=0.6, sub_temperature=0.6,
                    seed=42, sub_seed=45, streaming=True)

    results = []
    for i in range(warmup + runs):
        stats = defaultdict(float)
        counts = defaultdict(int)
        instrument(stream, stats, counts)

        t0 = time.perf_counter()
        res = stream.custom(text=TEXT, speaker=speaker, instruct=None,
                            language="chinese", config=cfg)
        wall = time.perf_counter() - t0
        if res is None:
            raise RuntimeError("synthesis returned None")

        audio_seconds = len(res.audio) / 24000.0
        steps = len(res.codes)
        predict_frames = counts.get("predictor_frame", 0)
        dec_times = list(getattr(res.stats, "decoder_compute_times", []) or [])
        entry = {
            "wall": wall,
            "audio_seconds": audio_seconds,
            "rtf": wall / audio_seconds if audio_seconds else None,
            "steps": steps,
            "codes_per_step": (len(res.codes[0]) if steps else None),
            "counts": dict(counts),
            "stats": {k: v for k, v in stats.items()},
            "decoder_chunks": len(dec_times),
            "decoder_seconds": sum(dec_times),
            "decoder_max_ms": (max(dec_times) * 1000) if dec_times else 0.0,
            "predictor_frame_ms": (stats["predictor_frame"] / predict_frames * 1000) if predict_frames else None,
            "talker_step_ms": (stats["talker_step"] / counts["talker_step"] * 1000) if counts.get("talker_step") else None,
        }
        if i >= warmup:
            results.append(entry)

    engine.shutdown()
    return {
        "llm_device": llm_device,
        "decoder_provider": decoder_provider,
        "engine_provider": getattr(engine, "ready", None),
        "load_seconds": load_seconds,
        "runs": results,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", choices=["cpu", "gpu"], default="cpu")
    parser.add_argument("--decoder", default="cpu")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--speaker", default="uncle_fu")
    parser.add_argument("--max-steps", type=int, default=400)
    parser.add_argument("--tag", default="")
    args = parser.parse_args()

    sys.path.insert(0, str(PROJECT))
    os.chdir(PROJECT)

    if args.llm == "cpu":
        os.environ["QWEN_TTS_CPU"] = "1"

    out = run_config(args.llm, args.decoder, args.runs, args.warmup, args.speaker, args.max_steps)
    out["tag"] = args.tag or f"{args.llm}-{args.decoder}"
    print("@@BENCH@@" + json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
