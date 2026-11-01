"""Export the fused predictor to one ONNX graph.

The whole fifteen-step loop is unrolled by the exporter because the steps are a plain
Python loop over a fixed range, so the result is a single graph with one entry point
and one exit - no per-step round trip between Python and the runtime, which is what
the shipped predictor pays 187.5 times per second of audio.

The graph takes the random draws as an input. That keeps it deterministic and
comparable against torch, and it avoids depending on ONNX random operators whose
DirectML coverage is not something to bet the voice path on.

Validation compares ONNX against the torch module on the same inputs. Both outputs
are checked: the sampled codes and the concatenated logit blocks behind them.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn

sys.path.insert(0, str(Path(__file__).parent))
from fused_predictor import (  # noqa: E402
    CODEBOOK_SIZE, HIDDEN, N_CODEBOOKS, SEQ_LEN, FusedCodePredictor,
)

MODEL_DIR = Path(r"D:\AI\Qwen-Local\tools\Qwen3-TTS-GGUF\model-custom")
DEFAULT_OUT = MODEL_DIR / "qwen3_tts_predictor_fused.fp16.onnx"


class ExportWrapper(nn.Module):
    """Fixed signature for the exporter: codes plus the logits behind them."""

    def __init__(self, model: FusedCodePredictor):
        super().__init__()
        self.model = model

    def forward(self, talker_hidden, code0, uniforms):
        codes, logits = self.model(talker_hidden, code0, uniforms, return_logits=True)
        return codes, logits


def build(dtype):
    model = FusedCodePredictor(MODEL_DIR).eval()
    if dtype == "fp16":
        model = model.half()
        # cos/sin keep fp32: at position 15 the lowest RoPE frequency has an angle
        # near 2e-5, which fp16 stores with only a handful of bits. The projection and
        # MLP matmuls, where the FLOPs are, still run in fp16.
        model.cos = model.cos.float()
        model.sin = model.sin.float()
        model.mask = model.mask.float()
    return model


def one_input(dtype, rng):
    """One frame's inputs. The graph is single-frame: a frame is a sequential chain,
    so batching would buy nothing and the runtime calls it once per frame anyway."""
    hidden = torch.from_numpy(rng.standard_normal(2048).astype(np.float32)).view(1, -1)
    code0 = torch.from_numpy(np.array([rng.integers(0, CODEBOOK_SIZE)], dtype=np.int64))
    uniforms = torch.from_numpy(rng.random(N_CODEBOOKS).astype(np.float32))
    if dtype == "fp16":
        hidden = hidden.half()
    return hidden, code0, uniforms


def export(out_path: Path, dtype: str, opset: int, dynamo: bool) -> None:
    model = build(dtype)
    wrapper = ExportWrapper(model).eval()
    hidden, code0, uniforms = one_input(dtype, np.random.default_rng(0))

    with torch.no_grad():
        torch.onnx.export(
            wrapper,
            (hidden, code0, uniforms),
            str(out_path),
            input_names=["talker_hidden", "code0", "uniforms"],
            output_names=["codes", "logits"],
            opset_version=opset,
            do_constant_folding=True,
            dynamo=dynamo,
        )
    print("wrote %s (%.1f MB)" % (out_path, out_path.stat().st_size / 1e6))


def validate(out_path: Path, dtype: str, frames: int) -> bool:
    import onnxruntime as ort

    model = build(dtype)
    options = ort.SessionOptions()
    options.log_severity_level = 3
    session = ort.InferenceSession(str(out_path), sess_options=options,
                                   providers=["CPUExecutionProvider"])

    rng = np.random.default_rng(11)
    worst = 0.0
    agree = 0
    total = 0
    first = None
    for index in range(frames):
        hidden, code0, uniforms = one_input(dtype, rng)
        onnx_codes, onnx_logits = session.run(None, {
            "talker_hidden": hidden.numpy(),
            "code0": code0.numpy(),
            "uniforms": uniforms.numpy(),
        })
        with torch.no_grad():
            torch_codes, torch_logits = model(hidden, code0, uniforms, return_logits=True)
        diff = np.abs(onnx_logits.astype(np.float64) - torch_logits.float().numpy().astype(np.float64))
        worst = max(worst, float(diff.max()))
        agree += int((onnx_codes == torch_codes.numpy()).sum())
        total += onnx_codes.size
        if first is None:
            first = (onnx_codes[0].tolist(), torch_codes.numpy()[0].tolist())

    print("\nlogits: worst |diff| over %d frames %.3e" % (frames, worst))
    print("codes : %d/%d = %.4f%% identical" % (agree, total, 100.0 * agree / total))
    print("  onnx  frame 0:", first[0])
    print("  torch frame 0:", first[1])

    ok = (agree / total) > 0.99 and worst < 0.25
    print("export validation:", "PASS" if ok else "REVIEW")
    return ok


def timing(out_path: Path, dtype: str, provider: str, frames: int = 60) -> None:
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.log_severity_level = 3
    session = ort.InferenceSession(str(out_path), sess_options=options, providers=[provider])
    hidden, code0, uniforms = one_input(dtype, np.random.default_rng(5))
    feed = {"talker_hidden": hidden.numpy(), "code0": code0.numpy(), "uniforms": uniforms.numpy()}
    for _ in range(5):
        session.run(None, feed)
    start = time.perf_counter()
    for _ in range(frames):
        session.run(None, feed)
    elapsed = time.perf_counter() - start
    per_frame = elapsed / frames * 1000
    print("%-24s %7.2f ms/frame  -> %.3f s per second of audio (12.5 frames/s)"
          % (provider, per_frame, per_frame * 12.5 / 1000))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--dtype", choices=["fp32", "fp16"], default="fp16")
    parser.add_argument("--opset", type=int, default=17)
    parser.add_argument("--dynamo", action="store_true")
    parser.add_argument("--frames", type=int, default=40)
    parser.add_argument("--skip-export", action="store_true")
    args = parser.parse_args()

    out_path = Path(args.out)
    if not args.skip_export:
        export(out_path, args.dtype, args.opset, args.dynamo)
    ok = validate(out_path, args.dtype, args.frames)

    print("\n--- per-frame cost ---")
    for provider in ("CPUExecutionProvider", "DmlExecutionProvider"):
        try:
            timing(out_path, args.dtype, provider)
        except Exception as exc:  # noqa: BLE001
            message = str(exc)[:150].replace("\n", " ")
            print("%-24s FAILED %s" % (provider, message))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
