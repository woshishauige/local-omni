"""Probe remote GGUF/ComfyUI files without downloading them.

Reads the first chunk of a GGUF (or safetensors header) through a range request
and prints `general.architecture` / `general.name`, so a candidate can be
accepted or rejected before spending a gigabyte of bandwidth.

    python probe_remote_gguf.py
"""

from __future__ import annotations

import json
import re
import struct
import subprocess

MIRRORS = ["https://aifasthub.com", "https://hf-mirror.com"]
CURL = r"C:\Windows\System32\curl.exe"

CANDIDATES = [
    ("unsloth/Qwen-Image-2.1-Turbo-GGUF", "qwen-image-2.1-turbo-Q8_0.gguf"),
    ("unsloth/Qwen-Image-2.1-Turbo-GGUF", "qwen-image-2.1-turbo-Q6_K.gguf"),
    ("Abiray/Qwen-Image-2.1-Turbo-GGUF", "qwen_image_2.1_turbo-Q8_0.gguf"),
    ("vantagewithai/Qwen-Image-2.1-turbo-ComfyUI-GGUF", "qwen_image_2.1_turbo-Q8_0.gguf"),
    ("vantagewithai/Qwen-Image-2.1-turbo-ComfyUI-GGUF", "qwen_image_2.1-Q8_0.gguf"),
    ("DogukanUrker/Qwen-Image-2.1-Turbo-GGUF", "qwen-image-2.1-turbo-Q8_0.gguf"),
]


def fetch_head(mirror: str, repo: str, name: str, size: int = 2 * 1024 * 1024) -> bytes | None:
    url = f"{mirror}/{repo}/resolve/main/{name}"
    result = subprocess.run(
        [CURL, "-sS", "-L", "--fail", "--max-time", "90", "-r", f"0-{size - 1}", url],
        capture_output=True, timeout=120)
    return result.stdout if result.returncode == 0 and result.stdout else None


def gguf_string(blob: bytes, key: bytes) -> str | None:
    index = blob.find(key)
    if index < 0:
        return None
    # GGUF kv: key (u64 len + bytes), value type (u32), then the value.
    offset = index + len(key)
    value_type = struct.unpack("<I", blob[offset:offset + 4])[0]
    if value_type != 8:  # 8 = GGUF string
        return None
    length = struct.unpack("<Q", blob[offset + 4:offset + 12])[0]
    if length > 512:
        return None
    return blob[offset + 12:offset + 12 + length].decode("utf-8", "replace")


def main() -> int:
    for repo, name in CANDIDATES:
        print(f"=== {repo}/{name}")
        blob = None
        for mirror in MIRRORS:
            blob = fetch_head(mirror, repo, name)
            if blob:
                print(f"  header bytes: {len(blob)} via {mirror}")
                break
        if not blob:
            print("  (range fetch failed)")
            continue
        if blob[:4] == b"GGUF":
            print(f"  GGUF version: {struct.unpack('<I', blob[4:8])[0]}")
            print(f"  general.architecture: {gguf_string(blob, b'general.architecture')!r}")
            print(f"  general.name: {gguf_string(blob, b'general.name')!r}")
            if b'qwen3vl' in blob[:400000]:
                print("  ! 头部出现 qwen3vl：可能是编码器而不是 DiT")
        elif blob[:1] in (b"{", b"\x00"):
            print("  (not GGUF; maybe safetensors)")
            print("  sample:", blob[:60])
        else:
            print("  unknown signature:", blob[:16])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
