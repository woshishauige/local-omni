"""Find the step at which the fused predictor stops matching the GGUF engine.

Comparing finished code sequences only says "wrong"; it does not say where. This
re-runs the engine's own predict_frame with the logits kept at every step, then
computes the same logits from the checkpoint and compares them step by step.

Reading it:
  * step 1 already differs  -> the prefill embedding, the projection, or the weights
    themselves do not line up
  * step 1 matches, later steps drift -> the code-to-embedding table mapping is wrong
  * every step matches but the codes differ -> the sampling rule differs, not the model
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).parent))
from fused_predictor import (  # noqa: E402
    CODEBOOK_SIZE, HIDDEN, N_CODEBOOKS, SEQ_LEN, FusedCodePredictor, uniforms_for,
)

PROJECT = Path(r"D:\AI\Qwen-Local\tools\Qwen3-TTS-GGUF")
MODEL_DIR = PROJECT / "model-custom"
FRAMES_TO_KEEP = 3


def collect(engine_runs: int = 1, llm_device: str = "gpu"):
    sys.path.insert(0, str(PROJECT))
    os.chdir(PROJECT)

    from qwen3_tts_gguf.inference import TTSEngine, TTSConfig
    from qwen3_tts_gguf.inference import llama
    from qwen3_tts_gguf.inference.predictor import Predictor

    records = []
    original = Predictor.predict_frame

    def dumping(self, master_hidden, code_0, sampler=None, do_sample=True,
                temperature=0.9, top_p=1.0, top_k=50):
        proj = self.assets.proj
        m_h_1024 = master_hidden @ proj["weight"].T + proj["bias"] if proj is not None else master_hidden
        step_codes = [code_0]
        raw = [self.assets.get_codec_embedding(0, code_0).copy()]
        c_in = np.stack([m_h_1024, self.assets.get_codec_embedding_1024(0, code_0)], axis=0)
        self.ctx.clear_kv_cache()
        self.batch.set_embd(c_in, pos=0, seq_id=0)
        self.ctx.decode(self.batch)

        n_vocab = llama.llama_vocab_n_tokens(self.model.vocab)
        logits_per_step = [np.ctypeslib.as_array(self.ctx.get_logits_ith(-1), shape=(n_vocab,)).copy()]

        local = sampler is None
        if local:
            sampler = llama.LlamaSampler(temperature=temperature if do_sample else 0.0,
                                         top_p=top_p if do_sample else 1.0,
                                         top_k=top_k if do_sample else 0)
        try:
            for cs in range(1, 16):
                start = (cs - 1) * 2048
                token = sampler.sample(self.ctx, limit_start=start, limit_end=cs * 2048)
                c = token - start
                step_codes.append(c)
                raw.append(self.assets.get_codec_embedding(cs, c).copy())
                if cs < 15:
                    emb = self.assets.get_codec_embedding_1024(cs, c)
                    if emb.ndim == 1:
                        emb = emb.reshape(1, -1)
                    self.batch.set_embd(emb, pos=cs + 1, seq_id=0)
                    self.ctx.decode(self.batch)
                    logits_per_step.append(
                        np.ctypeslib.as_array(self.ctx.get_logits_ith(-1), shape=(n_vocab,)).copy())
        finally:
            if local:
                sampler.free()

        if len(records) < FRAMES_TO_KEEP:
            records.append({
                "master_hidden": np.array(master_hidden, dtype=np.float32).copy(),
                "code_0": int(code_0),
                "codes": list(step_codes),
                "logits": np.stack(logits_per_step),   # [15, 30720]
            })
        return step_codes, raw

    Predictor.predict_frame = dumping
    try:
        print("[collect] loading engine...", flush=True)
        engine = TTSEngine(model_dir="model-custom", onnx_provider="GPU",
                           llm_use_gpu=(llm_device == "gpu"), verbose=False)
        if not engine:
            raise RuntimeError("engine not ready")
        print("[collect] engine ready, running %d synthesis" % engine_runs, flush=True)
        stream = engine.create_stream()
        config = TTSConfig(max_steps=400, sub_do_sample=False, streaming=True)
        for run in range(engine_runs):
            stream.custom(text="今天天气不错，我们出门走走吧。", speaker="uncle_fu",
                          instruct=None, language="chinese", config=config)
            print("[collect] run %d done, %d frames recorded" % (run + 1, len(records)), flush=True)
        print("[collect] shutting engine down", flush=True)
        engine.shutdown()
        print("[collect] done", flush=True)
    finally:
        Predictor.predict_frame = original
    return records


def main():
    print("[diff] start", flush=True)
    records = collect()
    if not records:
        print("nothing captured")
        return 1
    print("captured %d frames" % len(records), flush=True)

    model = FusedCodePredictor(MODEL_DIR).eval()
    with torch.no_grad():
        for index, record in enumerate(records[:2]):
            print("\n=== frame %d (code_0=%d) ===" % (index, record["code_0"]))
            hidden = torch.from_numpy(record["master_hidden"]).view(1, -1)
            code0 = torch.tensor([record["code_0"]], dtype=torch.long)
            h0 = (hidden @ model.proj_w.t() + model.proj_b).view(1, 1, HIDDEN)
            slots = [h0, model.codec[0][code0].view(1, 1, HIDDEN)] + \
                    [torch.zeros_like(h0)] * (SEQ_LEN - 2)

            engine_codes = record["codes"]
            print("engine codes:", engine_codes[1:])
            mine = []
            for step in range(1, N_CODEBOOKS + 1):
                h = model.hidden_at(slots, step)
                block = model.lm_head(h).view(-1)[(step - 1) * CODEBOOK_SIZE: step * CODEBOOK_SIZE]
                eng = record["logits"][step - 1][(step - 1) * CODEBOOK_SIZE: step * CODEBOOK_SIZE]
                mine_block = block.detach().numpy()
                diff = np.abs(mine_block - eng)
                # Compare on the values that matter: the top of each distribution.
                top_mine = int(np.argmax(mine_block))
                top_eng = int(np.argmax(eng))
                order_mine = np.argsort(-mine_block)[:5]
                order_eng = np.argsort(-eng)[:5]
                overlap = len(set(order_mine.tolist()) & set(order_eng.tolist()))
                print("  step %2d  max|d|=%.3e  corr=%.4f  argmax mine=%4d eng=%4d %s  top5 overlap %d/5"
                      % (step, diff.max(), np.corrcoef(mine_block, eng)[0, 1],
                         top_mine, top_eng, "OK " if top_mine == top_eng else "DIFF",
                         overlap))
                mine.append(top_mine)
                if step < N_CODEBOOKS:
                    slots[step + 1] = model.codec[step][
                        torch.tensor([engine_codes[step]], dtype=torch.long)].view(1, 1, HIDDEN)
            print("  greedy-from-my-weights on the engine's own code path:", mine)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
