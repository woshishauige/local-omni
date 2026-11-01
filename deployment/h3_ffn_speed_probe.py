"""Read-only H3 FFN benchmark; synthetic inputs, real checkpoint weights.

This isolates the installed GGUF dequantization and FFN math. It does not
measure a whole video or prove that a different chunk count fits that video.
"""
import importlib.util
import json
from pathlib import Path
import statistics
import time

import gguf
import numpy as np
import torch


ROOT = Path("D:/AI/ComfyUI-BSAI_v010/ComfyUI")
OUTPUT = Path("D:/AI/deployment/logs/h3-ffn-speed-probe-20260907.json")
spec = importlib.util.spec_from_file_location(
    "h3_probe_dequant", ROOT / "custom_nodes/ComfyUI-GGUF/dequant.py"
)
dequant = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dequant)


def decode(data, tensor):
    # Match GGMLLayer's default: native Q4_0 dequant, then BF16 compute.
    return dequant.dequantize(
        data.to("cuda"), tensor.tensor_type, tuple(reversed(tensor.shape.tolist()))
    ).to(torch.bfloat16)


def forward(x, chunks, data, tensors):
    out = torch.empty_like(x)
    offset = 0
    for c in torch.chunk(x, chunks, dim=0):
        weight = decode(data[0], tensors[0])
        h = torch.nn.functional.linear(c, weight)
        del weight
        gate, up = h.chunk(2, dim=-1)
        activated = torch.nn.functional.silu(gate).mul_(up)
        del gate, up, h
        weight = decode(data[1], tensors[1])
        out[offset:offset + len(c)] = torch.nn.functional.linear(activated, weight)
        del weight, activated
        offset += len(c)
    return out


def main():
    reader = gguf.GGUFReader(
        ROOT / "models/diffusion_models/minimax_h3/MiniMax-H3-ref2va-curve-Q4_0.gguf"
    )
    by_name = {t.name: t for t in reader.tensors}
    tensors = [by_name[f"blocks.0.mlp.fc{i}.weight"] for i in (1, 2)]
    cpu_data = [torch.from_numpy(np.array(t.data, copy=True)) for t in tensors]
    torch.manual_seed(20260907)
    x = torch.randn((24576, 5376), device="cuda", dtype=torch.bfloat16)
    report = {
        "gpu": torch.cuda.get_device_name(),
        "torch": torch.__version__,
        "input_shape": list(x.shape),
        "compute_dtype": str(x.dtype),
        "scope": "Single FFN, synthetic inputs; excludes attention, other layers, encoder, VAE and video quality validation.",
        "trials": [],
    }
    with torch.inference_mode():
        for placement in ("cuda", "cpu"):
            data = [d.to(placement) for d in cpu_data]
            outputs = {}
            timings = {4: [], 2: []}
            peaks = {4: [], 2: []}
            for chunks in (4, 2):
                forward(x, chunks, data, tensors)
            torch.cuda.synchronize()
            # Alternate the order to limit warm-up and temperature bias.
            for chunks in (4, 2, 2, 4, 4, 2):
                torch.cuda.reset_peak_memory_stats()
                start = time.perf_counter()
                out = forward(x, chunks, data, tensors)
                torch.cuda.synchronize()
                timings[chunks].append(time.perf_counter() - start)
                peaks[chunks].append(torch.cuda.max_memory_allocated() / 2**20)
                outputs[chunks] = out.cpu()
                del out
            diff = outputs[2].float() - outputs[4].float()
            ref_rms = outputs[4].float().square().mean().sqrt().item()
            entry = {
                "weight_storage": placement,
                "chunks_4_median_seconds": statistics.median(timings[4]),
                "chunks_2_median_seconds": statistics.median(timings[2]),
                "chunks_2_speedup": statistics.median(timings[4]) / statistics.median(timings[2]),
                "chunks_4_peak_allocated_mib": max(peaks[4]),
                "chunks_2_peak_allocated_mib": max(peaks[2]),
                "bitwise_equal": torch.equal(outputs[4], outputs[2]),
                "max_abs_difference": diff.abs().max().item(),
                "relative_rms_difference": diff.square().mean().sqrt().item() / ref_rms,
                "raw_seconds": timings,
            }
            report["trials"].append(entry)
            print(json.dumps(entry), flush=True)
            del data, outputs, diff
            torch.cuda.empty_cache()
    OUTPUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(str(OUTPUT), flush=True)


if __name__ == "__main__":
    main()
