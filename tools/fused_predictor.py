"""Fused code predictor: the whole 15-step acoustic prediction as one graph.

Why this exists
---------------
The shipped engine asks llama.cpp for one token at a time, so a single audio frame
costs 15 sequential decode+sample round trips - 187.5 per second of audio. Measured
on this machine, a predictor step takes 2.0 ms with the weights on the GPU, which is
only ~13% of the card's bandwidth, so the cost is fixed per call rather than per
byte: the model is 78.6M parameters and one step reads all of them.

This module computes all 15 codes for a frame inside one graph. The sequence is only
17 positions long (the talker hidden state, code 0, then fifteen code embeddings), so
the whole thing is unrolled statically and every step runs the five layers over a
fixed-shape buffer with a causal mask. Position k can only see positions <= k, so the
slots that are not filled in yet cannot influence the code being read.

Layout, taken from the project's own predictor.py and the exported checkpoint:
    position 0      proj(talker hidden)              2048 -> 1024
    position 1      proj(codec table 0[code_0])
    position k      proj(codec table k-1[code_{k-1}]) for k = 2..16
    code k is sampled from lm_head rows [(k-1)*2048, k*2048) read at position k

Sampling is the predictor's own chain - top_k, temperature, then a draw - re-expressed
as an inverse CDF so it stays inside the graph. The uniform that drives each draw is
an input, which keeps the graph deterministic and testable and avoids relying on
ONNX random operators that the DirectML provider may not implement.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Optional

import numpy as np
import torch
import torch.nn as nn

HIDDEN = 1024
N_LAYERS = 5
N_HEADS = 16
N_KV_HEADS = 8
HEAD_DIM = 128
INTERMEDIATE = 3072
VOCAB = 30720
N_CODEBOOKS = 15
CODEBOOK_SIZE = 2048
# Slot 0 holds the projected talker hidden state, slot 1 holds code 0, and slot k
# holds the embedding of code k-1. Code k is read from the logits at position k, and
# the last code (15) is read at position 15, so 16 slots is exactly enough - code 15
# is never fed back in, matching the engine's final step that samples without decoding.
SEQ_LEN = N_CODEBOOKS + 1  # 16
RMS_EPS = 1e-6
ROPE_THETA = 1_000_000.0
NEG = -30000.0  # large but still inside fp16 range


def rotate_half(x: torch.Tensor) -> torch.Tensor:
    half = x.shape[-1] // 2
    return torch.cat((-x[..., half:], x[..., :half]), dim=-1)


class RMSNorm(nn.Module):
    def __init__(self, weight: torch.Tensor):
        super().__init__()
        self.weight = nn.Parameter(weight.clone())

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        dtype = x.dtype
        x = x.float()
        x = x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + RMS_EPS)
        return (x * self.weight.float()).to(dtype)


class Attention(nn.Module):
    def __init__(self, weights: dict, layer: int):
        super().__init__()
        prefix = "layers.%d.self_attn." % layer
        self.q_proj = nn.Linear(HIDDEN, N_HEADS * HEAD_DIM, bias=False)
        self.k_proj = nn.Linear(HIDDEN, N_KV_HEADS * HEAD_DIM, bias=False)
        self.v_proj = nn.Linear(HIDDEN, N_KV_HEADS * HEAD_DIM, bias=False)
        self.o_proj = nn.Linear(N_HEADS * HEAD_DIM, HIDDEN, bias=False)
        self.q_norm = RMSNorm(weights[prefix + "q_norm.weight"])
        self.k_norm = RMSNorm(weights[prefix + "k_norm.weight"])
        self.load(weights, prefix)

    def load(self, weights: dict, prefix: str) -> None:
        with torch.no_grad():
            self.q_proj.weight.copy_(weights[prefix + "q_proj.weight"])
            self.k_proj.weight.copy_(weights[prefix + "k_proj.weight"])
            self.v_proj.weight.copy_(weights[prefix + "v_proj.weight"])
            self.o_proj.weight.copy_(weights[prefix + "o_proj.weight"])

    def forward(self, x: torch.Tensor, cos: torch.Tensor, sin: torch.Tensor,
                mask: torch.Tensor) -> torch.Tensor:
        batch, length, _ = x.shape
        q = self.q_proj(x).view(batch, length, N_HEADS, HEAD_DIM)
        k = self.k_proj(x).view(batch, length, N_KV_HEADS, HEAD_DIM)
        v = self.v_proj(x).view(batch, length, N_KV_HEADS, HEAD_DIM)
        q = self.q_norm(q)
        k = self.k_norm(k)
        q = q.transpose(1, 2)
        k = k.transpose(1, 2)
        v = v.transpose(1, 2)
        # Rotation and attention run in fp32 while the projections stay in the model
        # dtype. The attention tensors are 16 positions wide, so this costs nothing,
        # and it keeps RoPE's lowest frequencies - angles near 2e-5 at the last
        # position - out of fp16's subnormal range.
        compute = q.dtype
        q = q.float()
        k = k.float()
        v = v.float()
        q = q * cos + rotate_half(q) * sin
        k = k * cos + rotate_half(k) * sin
        # Broadcast the key/value heads across their query groups instead of
        # repeating them: identical arithmetic, and it exports as plain broadcasting
        # rather than depending on how the exporter lowers repeat_interleave.
        group = N_HEADS // N_KV_HEADS
        q = q.view(batch, N_KV_HEADS, group, length, HEAD_DIM)
        k = k.view(batch, N_KV_HEADS, 1, length, HEAD_DIM)
        v = v.view(batch, N_KV_HEADS, 1, length, HEAD_DIM)
        att = torch.matmul(q, k.transpose(-1, -2)) * (HEAD_DIM ** -0.5)
        att = att + mask.float()
        att = torch.softmax(att, dim=-1)
        out = torch.matmul(att, v)
        out = out.view(batch, N_HEADS, length, HEAD_DIM)
        out = out.transpose(1, 2).reshape(batch, length, N_HEADS * HEAD_DIM)
        return self.o_proj(out.to(compute))


class MLP(nn.Module):
    def __init__(self, weights: dict, layer: int):
        super().__init__()
        prefix = "layers.%d.mlp." % layer
        self.gate_proj = nn.Linear(HIDDEN, INTERMEDIATE, bias=False)
        self.up_proj = nn.Linear(HIDDEN, INTERMEDIATE, bias=False)
        self.down_proj = nn.Linear(INTERMEDIATE, HIDDEN, bias=False)
        with torch.no_grad():
            self.gate_proj.weight.copy_(weights[prefix + "gate_proj.weight"])
            self.up_proj.weight.copy_(weights[prefix + "up_proj.weight"])
            self.down_proj.weight.copy_(weights[prefix + "down_proj.weight"])

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.down_proj(torch.nn.functional.silu(self.gate_proj(x)) * self.up_proj(x))


class DecoderLayer(nn.Module):
    def __init__(self, weights: dict, layer: int):
        super().__init__()
        prefix = "layers.%d." % layer
        self.input_layernorm = RMSNorm(weights[prefix + "input_layernorm.weight"])
        self.post_attention_layernorm = RMSNorm(weights[prefix + "post_attention_layernorm.weight"])
        self.self_attn = Attention(weights, layer)
        self.mlp = MLP(weights, layer)

    def forward(self, x, cos, sin, mask):
        x = x + self.self_attn(self.input_layernorm(x), cos, sin, mask)
        return x + self.mlp(self.post_attention_layernorm(x))


class FusedCodePredictor(nn.Module):
    """All 15 codes of one audio frame in a single forward pass."""

    def __init__(self, model_dir: Path, top_k: int = 50, temperature: float = 0.6):
        super().__init__()
        model_dir = Path(model_dir)
        weights = load_safetensors(model_dir / "predictor_hf" / "model.safetensors")
        if weights["lm_head.weight"].shape[0] != VOCAB:
            raise ValueError("unexpected lm_head shape %s" % (weights["lm_head.weight"].shape,))

        self.top_k = top_k
        self.temperature = temperature
        self.layers = nn.ModuleList([DecoderLayer(weights, i) for i in range(N_LAYERS)])
        self.norm = RMSNorm(weights["norm.weight"])
        self.lm_head = nn.Linear(HIDDEN, VOCAB, bias=False)
        with torch.no_grad():
            self.lm_head.weight.copy_(weights["lm_head.weight"])

        self.register_buffer("cos", build_rope(SEQ_LEN, 0)[0].view(1, 1, SEQ_LEN, HEAD_DIM))
        self.register_buffer("sin", build_rope(SEQ_LEN, 0)[1].view(1, 1, SEQ_LEN, HEAD_DIM))
        self.register_buffer("mask", causal_mask(SEQ_LEN), persistent=False)

        proj_w, proj_b, tables = load_codec_assets(model_dir)
        self.register_buffer("proj_w", proj_w)          # [1024, 2048]
        self.register_buffer("proj_b", proj_b)          # [1024]
        # Codec tables pre-projected to the predictor's width, one per codebook.
        self.register_buffer("codec", tables)           # [16, 2048, 1024]

    def hidden_at(self, slots, position: int) -> torch.Tensor:
        seq = torch.cat(slots, dim=1)
        cos, sin, mask = self.cos, self.sin, self.mask
        h = seq
        for layer in self.layers:
            h = layer(h, cos, sin, mask)
        h = self.norm(h)
        return h[:, position]

    def sample(self, logits: torch.Tensor, uniform: torch.Tensor) -> torch.Tensor:
        """top_k -> temperature -> inverse-CDF draw, all in fp32."""
        x = logits.float()
        if self.top_k and self.top_k > 0:
            kth = torch.topk(x, self.top_k, dim=-1).values[..., -1:]
            x = torch.where(x < kth, torch.full_like(x, NEG), x)
        x = x / self.temperature
        probs = torch.softmax(x, dim=-1)
        cdf = torch.cumsum(probs, dim=-1)
        idx = (cdf < uniform.float().view(-1, 1)).sum(dim=-1)
        return torch.clamp(idx, max=CODEBOOK_SIZE - 1).to(torch.long)

    def forward(self, talker_hidden: torch.Tensor, code0: torch.Tensor,
                uniforms: torch.Tensor, greedy: bool = False,
                forced=None, return_logits: bool = False):
        """`forced` feeds a known code sequence forward instead of the module's own
        draws. It is a validation aid only - it proves that the transformer, the
        lm_head slicing and the step mapping agree with the engine when both are
        given identical inputs, which separates an implementation error from the
        q8_0-versus-bf16 weight difference. Exports always see the None branch.

        `return_logits` adds the raw per-step logit blocks as a second output; the
        export validation uses it to compare numbers rather than sampled codes."""
        dtype = self.proj_w.dtype
        h0 = (talker_hidden.to(dtype).float() @ self.proj_w.float().t()
              + self.proj_b.float()).to(dtype).view(1, 1, HIDDEN)
        table0 = self.codec[0]
        e0 = table0[code0].view(1, 1, HIDDEN)
        zeros = torch.zeros_like(h0)
        slots = [h0, e0] + [zeros] * (N_CODEBOOKS - 1)

        codes = []
        blocks = []
        for step in range(1, N_CODEBOOKS + 1):
            position = step
            h = self.hidden_at(slots, position)
            block = self.lm_head(h).view(-1)[(step - 1) * CODEBOOK_SIZE: step * CODEBOOK_SIZE]
            if return_logits:
                blocks.append(block)
            if greedy:
                code = torch.argmax(block.float()).view(1)
            else:
                code = self.sample(block.view(1, -1), uniforms[step - 1])
            codes.append(code)
            if step < N_CODEBOOKS:
                if forced is not None:
                    feed = torch.tensor([int(forced[step - 1])], dtype=torch.long,
                                        device=code.device)
                else:
                    feed = code
                slots[step + 1] = self.codec[step][feed].view(1, 1, HIDDEN)
        stacked = torch.stack(codes, dim=-1)  # [1, 15]
        if return_logits:
            return stacked, torch.cat(blocks).view(1, -1).float()
        return stacked


def causal_mask(length: int) -> torch.Tensor:
    mask = torch.full((1, 1, length, length), NEG)
    allowed = torch.tril(torch.ones(length, length, dtype=torch.bool))
    return torch.where(allowed, torch.zeros_like(mask), mask)


def build_rope(length: int, offset: int):
    inv_freq = 1.0 / (ROPE_THETA ** (torch.arange(0, HEAD_DIM, 2).float() / HEAD_DIM))
    positions = torch.arange(offset, offset + length).float()
    freqs = torch.outer(positions, inv_freq)
    emb = torch.cat((freqs, freqs), dim=-1)
    return emb.cos(), emb.sin()


def load_safetensors(path: Path) -> dict:
    with open(path, "rb") as handle:
        header_len = struct.unpack("<Q", handle.read(8))[0]
        header = json.loads(handle.read(header_len))
        base = handle.tell()
        tensors = {}
        for name, info in header.items():
            if name == "__metadata__":
                continue
            start, end = info["data_offsets"]
            handle.seek(base + start)
            raw = handle.read(end - start)
            dtype = {"F32": np.float32, "F16": np.float16, "BF16": None}[info["dtype"]]
            array = np.frombuffer(raw, dtype=np.uint16 if dtype is None else dtype)
            if dtype is None:
                array = (array.astype(np.uint32) << 16).view(np.float32)
            tensors[name] = torch.from_numpy(array.reshape(info["shape"]).copy())
    return tensors


def load_codec_assets(model_dir: Path):
    """Speaker/embedding side tables the predictor feeds on, already projected."""
    emb_dir = Path(model_dir) / "embeddings"
    proj_w = torch.from_numpy(np.load(emb_dir / "proj_weight.npy")).float()   # [1024, 2048]
    proj_b = torch.from_numpy(np.load(emb_dir / "proj_bias.npy")).float()     # [1024]
    tables = []
    for index in range(16):
        table = torch.from_numpy(np.load(emb_dir / ("codec_embedding_%d.npy" % index))).float()
        table = table[:CODEBOOK_SIZE]                                        # drop the 3072-row extras
        tables.append(table @ proj_w.t() + proj_b)
    return proj_w, proj_b, torch.stack(tables, dim=0)


def uniforms_for(rng: np.random.Generator, count: int = N_CODEBOOKS) -> torch.Tensor:
    return torch.from_numpy(rng.random(count).astype(np.float32))
