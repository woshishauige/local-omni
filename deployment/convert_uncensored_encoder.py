"""Convert the abliterated Qwen3-VL encoder GGUF into a ComfyUI-loadable safetensors.

The "Uncensored" file is a Qwen3-VL-8B-Instruct text encoder quantised to GGUF Q8_0
by a non-ComfyUI tool. ComfyUI's CLIPLoader cannot read it as a `qwen_image`
encoder (the GGUF path yields wrong conditioning dims), so this script reads the
GGUF, renames llama.cpp keys to HF/transformers names and writes one fp16
safetensors file.

The output is ~16.3 GiB, so it is written by streaming (one tensor in memory at a
time) instead of building a 16 GiB dict; ComfyUI will then keep part of it on CPU
while sampling -- correct but slower than the official bf16/w4a8 encoders.

    python convert_uncensored_encoder.py --check   # keys only
    python convert_uncensored_encoder.py           # write the file
"""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path

import numpy as np

MODELS = Path(r"D:\AI\ComfyUI-BSAI_v010\ComfyUI\models")
SOURCE = MODELS / "text_encoders" / "Qwen-Image-2.1-Turbo-Uncensored-Q8_0.gguf"
TARGET = MODELS / "text_encoders" / "qwen3vl_8b_heretic_fp16.safetensors"

LLAMA_SD_MAP = {
    "blk.": "model.layers.",
    "attn_norm": "input_layernorm",
    "attn_q_norm.": "self_attn.q_norm.",
    "attn_k_norm.": "self_attn.k_norm.",
    "attn_v_norm.": "self_attn.v_norm.",
    "attn_q": "self_attn.q_proj",
    "attn_k": "self_attn.k_proj",
    "attn_v": "self_attn.v_proj",
    "attn_output": "self_attn.o_proj",
    "ffn_up": "mlp.up_proj",
    "ffn_down": "mlp.down_proj",
    "ffn_gate": "mlp.gate_proj",
    "ffn_norm": "post_attention_layernorm",
    "token_embd": "model.embed_tokens",
    "output_norm": "model.norm",
    "output.weight": "lm_head.weight",
}


def map_key(key: str) -> str:
    if key.startswith("blk."):
        _, rest = key.split(".", 1)
        index, _, tail = rest.partition(".")
        # Per-head norms first: they must not be mistaken for the projections.
        for old, new in (("attn_q_norm.", "self_attn.q_norm."),
                         ("attn_k_norm.", "self_attn.k_norm."),
                         ("attn_v_norm.", "self_attn.v_norm.")):
            if tail.startswith(old):
                return f"model.layers.{index}." + new + tail[len(old):]
        for old, new in LLAMA_SD_MAP.items():
            if old.endswith("."):
                continue
            if tail.startswith(old):
                tail = new + tail[len(old):]
                break
        return f"model.layers.{index}.{tail}"
    for old, new in LLAMA_SD_MAP.items():
        if old.endswith("."):
            continue
        if key.startswith(old):
            return new + key[len(old):]
    return key


def _dequant(tensor) -> np.ndarray:
    """Dequantise one GGUF tensor to fp16."""
    qtype = tensor.tensor_type.name
    shape = tuple(int(size) for size in tensor.shape)
    if qtype == "F32":
        return np.asarray(tensor.data, dtype=np.float32).reshape(shape).astype(np.float16)
    if qtype in ("F16", "BF16"):
        raw = np.ascontiguousarray(tensor.data).view(np.uint8)
        if qtype == "F16":
            return raw.view(np.float16).reshape(shape).copy()
        return (raw.view(np.uint16).astype(np.uint32) << 16).view(np.float32).reshape(shape).astype(np.float16)
    if qtype == "Q8_0":
        raw = np.ascontiguousarray(tensor.data).view(np.uint8).reshape(-1)
        blocks = raw.reshape(-1, 34)
        scales = np.ascontiguousarray(blocks[:, :2]).view(np.float16).astype(np.float32).reshape(-1)
        values = np.ascontiguousarray(blocks[:, 2:]).view(np.int8).astype(np.float32)
        flat = (values * scales[:, None]).reshape(-1)
        rows = shape[0] if len(shape) == 2 else 1
        cols = int(np.prod(shape)) // rows
        padded = ((cols + 31) // 32) * 32
        return flat.reshape(rows, padded)[:, :cols].reshape(shape).astype(np.float16)
    raise SystemExit(f"unsupported tensor type {qtype} for {tensor.name}")


def read_tensor(tensor) -> np.ndarray:
    """Dequantise one GGUF tensor and orient 2-D weights as HF expects.

    llama.cpp stores linear weights transposed ([in, out]); transformers wants
    [out, in], so every 2-D tensor is transposed here regardless of its quant type.
    """
    array = _dequant(tensor)
    return array.T.copy() if array.ndim == 2 else array


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not SOURCE.is_file():
        print(f"missing: {SOURCE}")
        return 1

    import gguf
    reader = gguf.GGUFReader(str(SOURCE))
    tensors = list(reader.tensors)
    print(f"tensors: {len(tensors)}")
    for tensor in tensors[:6]:
        print(f"  {tensor.name:<44} {tensor.tensor_type.name:<6} {tuple(tensor.shape)} -> {map_key(tensor.name)}")
    if args.check:
        return 0

    # Pass 1: shapes and offsets for the header.
    entries = []
    offset = 0
    for tensor in tensors:
        shape = tuple(int(size) for size in tensor.shape)
        name = map_key(tensor.name)
        length = int(np.prod(shape)) * 2
        entries.append({"name": name, "shape": shape, "offset": offset, "length": length})
        offset += length
    header = {"__metadata__": {"format": "pt"}}
    for entry in entries:
        header[entry["name"]] = {"dtype": "F16", "shape": list(entry["shape"]),
                                 "data_offsets": [entry["offset"], entry["offset"] + entry["length"]]}
    blob = json.dumps(header, separators=(",", ":")).encode("utf-8")
    blob += b" " * ((8 - len(blob) % 8) % 8)

    print(f"writing {TARGET} ({offset / 1024**3:.2f} GiB of tensors)")
    with TARGET.open("wb") as out:
        out.write(struct.pack("<Q", len(blob)))
        out.write(blob)
        for index, tensor in enumerate(tensors):
            array = read_tensor(tensor)
            out.write(np.ascontiguousarray(array, dtype=np.float16).tobytes())
            del array
            if (index + 1) % 40 == 0:
                print(f"  {index + 1}/{len(tensors)}", flush=True)
    size = TARGET.stat().st_size / 1024**3
    print(f"done: {TARGET} ({size:.2f} GiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
