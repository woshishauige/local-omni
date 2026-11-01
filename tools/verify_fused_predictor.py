"""Two gates for the fused predictor.

Gate A - is the module a faithful re-implementation?
    The exported checkpoint is a plain Qwen3 stack, so transformers can run it. Both
    implementations are fed the identical 17-slot embedding sequence and every
    position's logits are compared. This checks attention, RoPE, both norms, the MLP
    and the concatenated lm_head at once; a mismatch here is a bug, not precision.

Gate B - does it agree with the shipped GGUF predictor?
    The real engine is driven with greedy sub-sampling and every
    (talker hidden, code 0, codes) triple is captured. Replaying those triples here
    compares the two predictors on identical inputs. They hold different weight
    precisions - q8_0 in the GGUF, bf16 in the checkpoint - so a small number of
    near-tie codes may legitimately differ, and the agreement rate is the number to
    judge rather than bit equality.

Gate B is skipped with --gate-a-only, since it needs the GPU and the engine.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).parent))
from fused_predictor import (  # noqa: E402
    CODEBOOK_SIZE, HIDDEN, N_CODEBOOKS, SEQ_LEN, FusedCodePredictor,
    load_codec_assets, load_safetensors, uniforms_for,
)

MODEL_DIR = Path(r"D:\AI\Qwen-Local\tools\Qwen3-TTS-GGUF\model-custom")


def gate_a(seed: int = 3) -> bool:
    from transformers import AutoConfig, AutoModelForCausalLM

    torch.manual_seed(seed)
    print("--- gate A: fused module vs transformers on the same weights ---")
    model = FusedCodePredictor(MODEL_DIR).eval()

    # Built from config and loaded by hand rather than through from_pretrained: the
    # folder is a bare safetensors dump, and this newer transformers rejects it as
    # "corrupted" while the keys themselves load perfectly.
    config = AutoConfig.from_pretrained(str(MODEL_DIR / "predictor_hf"))
    reference = AutoModelForCausalLM.from_config(config).eval().float()
    state = load_safetensors(MODEL_DIR / "predictor_hf" / "model.safetensors")
    # The dump names the body without the "model." prefix a CausalLM wrapper adds,
    # so the same weights have to be re-labelled before they will load.
    state = {(k if k.startswith("lm_head.") else "model." + k): v for k, v in state.items()}
    missing, unexpected = reference.load_state_dict(state, strict=False)
    print("missing keys:", [k for k in missing])
    print("unexpected keys:", [k for k in unexpected])

    rng = np.random.default_rng(seed)
    # The talker emits 2048-wide hidden states; the predictor's own width is 1024.
    talker_hidden = torch.from_numpy(rng.standard_normal(2048).astype(np.float32)).view(1, -1)
    code0 = torch.tensor([123], dtype=torch.long)

    codes = model(talker_hidden, code0, uniforms_for(rng), greedy=True)
    print("greedy codes from the fused module:", codes.view(-1).tolist())

    # Rebuild the exact 17 slot sequence the fused module saw and run it through
    # transformers in one pass; every position's logits must match.
    with torch.no_grad():
        h0 = (talker_hidden @ model.proj_w.t() + model.proj_b).view(1, 1, HIDDEN)
        seq = [h0, model.codec[0][code0].view(1, 1, HIDDEN)]
        for step in range(1, N_CODEBOOKS):
            seq.append(model.codec[step][codes[0, step - 1]].view(1, 1, HIDDEN))
        while len(seq) < SEQ_LEN:
            seq.append(torch.zeros_like(h0))
        embeds = torch.cat(seq, dim=1)

        reference_logits = reference(inputs_embeds=embeds, use_cache=False).logits[0]

        worst = 0.0
        per_position = []
        # Positions 1..15 are the ones a code is actually read from.
        for position in range(1, SEQ_LEN):
            probe = [embeds[:, i:i + 1] for i in range(SEQ_LEN)]
            fused = model.hidden_at(probe, position)
            fused_logits = model.lm_head(fused).view(-1)
            diff = float((fused_logits - reference_logits[position]).abs().max())
            per_position.append(diff)
            worst = max(worst, diff)

    print("max |logit difference| per position:", ["%.2e" % d for d in per_position])
    print("worst position difference: %.3e" % worst)
    ok = worst < 1e-3
    print("gate A:", "PASS" if ok else "FAIL")
    return ok


def capture_engine_triples(runs: int, llm_device: str, text: str):
    """Run the shipped engine greedily and record what the predictor was asked."""
    project = Path(r"D:\AI\Qwen-Local\tools\Qwen3-TTS-GGUF")
    sys.path.insert(0, str(project))
    import os
    os.chdir(project)

    from qwen3_tts_gguf.inference import TTSEngine, TTSConfig
    from qwen3_tts_gguf.inference.predictor import Predictor

    captured = []
    original = Predictor.predict_frame

    def spy(self, master_hidden, code_0, sampler=None, do_sample=True,
            temperature=0.9, top_p=1.0, top_k=50):
        codes, _ = original(self, master_hidden, code_0, sampler=sampler, do_sample=do_sample,
                            temperature=temperature, top_p=top_p, top_k=top_k)
        captured.append((np.array(master_hidden).copy(), int(code_0), list(codes)))
        return codes, _

    Predictor.predict_frame = spy
    try:
        engine = TTSEngine(model_dir="model-custom", onnx_provider="GPU",
                           llm_use_gpu=(llm_device == "gpu"), verbose=False)
        if not engine:
            raise RuntimeError("engine not ready")
        stream = engine.create_stream()
        config = TTSConfig(max_steps=400, sub_do_sample=False, streaming=True)
        for index in range(runs):
            stream.custom(text=text, speaker="uncle_fu", instruct=None,
                          language="chinese", config=config)
            print("  captured after run %d: %d frames" % (index + 1, len(captured)))
        engine.shutdown()
    finally:
        Predictor.predict_frame = original
    return captured


def gate_b(runs: int, llm_device: str) -> bool:
    print("\n--- gate B: greedy agreement with the GGUF predictor ---")
    text = "今天天气不错，我们出门走走吧。"
    triples = capture_engine_triples(runs, llm_device, text)
    if not triples:
        print("no frames captured; skipping")
        return False

    model = FusedCodePredictor(MODEL_DIR).eval()
    per_book = defaultdict(lambda: [0, 0])
    exact_frames = 0
    total_positions = 0
    matching_positions = 0

    with torch.no_grad():
        for hidden, code0, gguf_codes in triples:
            hidden_t = torch.from_numpy(np.asarray(hidden, dtype=np.float32)).view(1, -1)
            fused = model(hidden_t, torch.tensor([code0]), uniforms_for(np.random.default_rng(0)),
                          greedy=True).view(-1).tolist()
            total_positions += len(gguf_codes)
            frame_ok = True
            for index, (a, b) in enumerate(zip(gguf_codes, fused)):
                bucket = per_book[index]
                bucket[1] += 1
                if a == b:
                    bucket[0] += 1
                    matching_positions += 1
                else:
                    frame_ok = False
            if frame_ok:
                exact_frames += 1

    print("frames compared: %d, positions: %d" % (len(triples), total_positions))
    print("position agreement: %d/%d = %.2f%%"
          % (matching_positions, total_positions, 100.0 * matching_positions / total_positions))
    print("frames whose 15 codes all match: %d/%d = %.2f%%"
          % (exact_frames, len(triples), 100.0 * exact_frames / len(triples)))
    print("per codebook:")
    for index in sorted(per_book):
        good, total = per_book[index]
        print("   codebook %2d: %5.1f%% (%d/%d)" % (index, 100.0 * good / total, good, total))

    rate = matching_positions / total_positions
    ok = rate > 0.90
    print("gate B:", "PASS" if ok else "REVIEW", "(threshold 90%)")
    return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate-a-only", action="store_true")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--llm", default="gpu")
    args = parser.parse_args()

    a_ok = gate_a()
    if args.gate_a_only:
        return 0 if a_ok else 1
    b_ok = gate_b(args.runs, args.llm)
    return 0 if (a_ok and b_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
