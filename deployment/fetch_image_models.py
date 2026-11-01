"""Resilient parallel downloader for the image models.

The Hugging Face CDN from this machine caps a single stream near 0.6 MB/s and
sometimes drops TLS mid-stream, so each file is fetched as several chunks in
parallel, and finished chunks are recorded in a sidecar file so an interrupted
run resumes instead of restarting.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import threading
import time
from pathlib import Path

import requests

MODELS = Path(r"D:\AI\ComfyUI-BSAI_v010\ComfyUI\models")
CHUNK = 8 * 1024 * 1024
# The HF CDN throttles each connection (measured ~0.15-0.2 MB/s here), so the
# aggregate rate scales with the worker count; override with DSH_FETCH_WORKERS.
WORKERS = int(os.environ.get("DSH_FETCH_WORKERS", "8"))
ATTEMPTS = 15
PAUSE = 2.0
PRINT_LOCK = threading.Lock()

FILES = [
    (
        "CyberRealisticPony_V18.0_F16.safetensors",
        "https://huggingface.co/cyberdelia/CyberRealisticPony/resolve/main/CyberRealisticPony_V18.0_F16.safetensors",
        "checkpoints/CyberRealisticPony_V18.0_F16.safetensors",
        6936869880,
    ),
    (
        "CyberRealistic_PonySemi_V5.safetensors",
        "https://huggingface.co/cyberdelia/CyberRealisticSemiRealPony/resolve/main/CyberRealistic_PonySemi_V5.safetensors",
        "checkpoints/CyberRealistic_PonySemi_V5.safetensors",
        7108052984,
    ),
    (
        "t5xxl_fp8_e4m3fn.safetensors",
        "https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp8_e4m3fn.safetensors",
        "text_encoders/t5xxl_fp8_e4m3fn.safetensors",
        4896583184,
    ),
    (
        "ae.safetensors",
        "https://huggingface.co/lodestones/Chroma/resolve/main/ae.safetensors",
        "vae/chroma_ae.safetensors",
        335304388,
    ),
    # Legacy GGUF variant of the same SDXL realism family. Its pipeline is no
    # longer registered and the files were removed from disk on 2026-09-12, so
    # these three entries only matter if the GGUF path is wanted back.
    (
        "cyberrealisticXL_v100-Q8_0.gguf",
        "https://huggingface.co/InsecureErasure/CyberRealisticXL-GGUF/resolve/main/cyberrealisticXL_v100-Q8_0.gguf",
        "unet/cyberrealisticXL_v100-Q8_0.gguf",
        2736430048,
    ),
    (
        "cyberrealisticXL_v100_clip_g.safetensors",
        "https://huggingface.co/InsecureErasure/CyberRealisticXL-GGUF/resolve/main/clip/cyberrealisticXL_v100_clip_g.safetensors",
        "text_encoders/cyberrealisticXL_clip_g.safetensors",
        1389363370,
    ),
    (
        "cyberrealisticXL_v100_clip_l.safetensors",
        "https://huggingface.co/InsecureErasure/CyberRealisticXL-GGUF/resolve/main/clip/cyberrealisticXL_clip_l.safetensors",
        "text_encoders/cyberrealisticXL_clip_l.safetensors",
        246144832,
    ),
    # Speed LoRAs for the SDXL/Pony family: the accelerators the panel offers are
    # exactly these files, so they download through the same resumable path.
    (
        "lcm-lora-sdxl.safetensors",
        "https://huggingface.co/latent-consistency/lcm-lora-sdxl/resolve/main/pytorch_lora_weights.safetensors",
        "loras/lcm-lora-sdxl.safetensors",
        393929860,
    ),
    (
        "dmd2_sdxl_4step_lora_fp16.safetensors",
        "https://huggingface.co/tianweiy/DMD2/resolve/main/dmd2_sdxl_4step_lora_fp16.safetensors",
        "loras/dmd2_sdxl_4step_lora_fp16.safetensors",
        393929860,
    ),
    (
        "Hyper-SDXL-8steps-CFG-lora.safetensors",
        "https://huggingface.co/ByteDance/Hyper-SD/resolve/main/Hyper-SDXL-8steps-CFG-lora.safetensors",
        "loras/Hyper-SDXL-8steps-CFG-lora.safetensors",
        787400000,
    ),
    # Quality LoRAs that stack next to an accelerator: realism slider, face
    # helper and a detail slider, all SDXL and all small.
    (
        "sdxl_photorealistic_slider_v1-0.safetensors",
        "https://huggingface.co/ostris/photorealistic-slider-sdxl-lora/resolve/main/sdxl_photorealistic_slider_v1-0.safetensors",
        "loras/sdxl_photorealistic_slider_v1-0.safetensors",
        24240000,
    ),
    (
        "face_xl_v0_1.safetensors",
        "https://huggingface.co/ostris/face-helper-sdxl-lora/resolve/main/face_xl_v0_1.safetensors",
        "loras/face_xl_v0_1.safetensors",
        223700000,
    ),
    (
        "extremely-detailed.safetensors",
        "https://huggingface.co/ntc-ai/SDXL-LoRA-slider.extremely-detailed/resolve/main/extremely%20detailed.safetensors",
        "loras/extremely-detailed.safetensors",
        8800000,
    ),
    # Chroma1-HD itself (optional, 17.8 GB): the uncensored Flux-architecture
    # model. Listed last so the earlier indices keep their meaning.
    (
        "chroma-unlocked-v21.safetensors",
        "https://huggingface.co/lodestones/Chroma/resolve/main/chroma-unlocked-v21.safetensors",
        "diffusion_models/chroma-unlocked-v21.safetensors",
        17798564224,
    ),
]


def remote_size(url: str) -> int:
    response = requests.head(url, allow_redirects=True, timeout=45)
    response.raise_for_status()
    return int(response.headers.get("x-linked-size") or response.headers.get("content-length") or 0)


def fetch_chunk(url: str, start: int, end: int) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = requests.get(
                url, headers={"Range": f"bytes={start}-{end - 1}"},
                stream=True, timeout=(30, 60),
            )
            if response.status_code not in (200, 206):
                response.close()
                raise RuntimeError(f"HTTP {response.status_code}")
            data = b"".join(response.iter_content(1024 * 512))
            response.close()
            if len(data) != end - start:
                raise RuntimeError(f"short read {len(data)} != {end - start}")
            return data
        except Exception as exc:  # noqa: BLE001 - network errors vary widely
            last_error = exc
            time.sleep(min(PAUSE * attempt, 15))
    raise RuntimeError(f"chunk {start}-{end} failed after {ATTEMPTS} attempts: {last_error}")


def download(index: int) -> None:
    name, url, local, size = FILES[index]
    target = MODELS / local
    target.parent.mkdir(parents=True, exist_ok=True)
    state_path = target.with_suffix(target.suffix + ".state.json")
    if target.exists() and _verified(target):
        print(f"[{name}] already complete and verified", flush=True)
        return
    # The declared size is only a fallback: a wrong constant silently produced a
    # truncated checkpoint once, so the server's own length wins.
    try:
        total = remote_size(url) or size
    except Exception as exc:  # noqa: BLE001
        print(f"[{name}] HEAD failed ({exc}); using the declared size {size}", flush=True)
        total = size
    if size and total != size:
        print(f"[{name}] declared {size} but the server reports {total}; using the server", flush=True)
    state = json.loads(state_path.read_text()) if state_path.exists() else {"chunks": []}
    done = set(state["chunks"])
    partial = target.with_name(target.name + ".partial")
    if not partial.exists() or partial.stat().st_size != total:
        partial.parent.mkdir(parents=True, exist_ok=True)
        with partial.open("wb") as handle:
            handle.truncate(total)
        done.clear()
    remaining = [start for start in range(0, total, CHUNK) if start not in done]
    print(f"[{name}] {len(done) * CHUNK / 1e9:.2f}/{total / 1e9:.2f} GB done, "
          f"{len(remaining)} chunks left, {WORKERS} workers", flush=True)
    started = time.time()
    written = len(done) * CHUNK
    failures: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(fetch_chunk, url, start, min(start + CHUNK, total)): start for start in remaining}
        for future in concurrent.futures.as_completed(futures):
            start = futures[future]
            try:
                data = future.result()
            except Exception as exc:  # noqa: BLE001
                failures.append(str(exc))
                continue
            with partial.open("r+b") as handle:
                handle.seek(start)
                handle.write(data)
            done.add(start)
            written += len(data)
            state["chunks"] = sorted(done)
            state_path.write_text(json.dumps(state))
            elapsed = max(0.001, time.time() - started)
            rate = written / elapsed / 1e6
            left = (total - written) / 1e6
            with PRINT_LOCK:
                print(f"[{name}] {written / 1e9:.2f}/{total / 1e9:.2f} GB {rate:.2f} MB/s "
                      f"eta {left / max(0.01, rate) / 60:.0f} min", flush=True)
    if failures:
        raise RuntimeError(f"{len(failures)} chunks failed: {failures[0]}")
    if partial.stat().st_size != total:
        raise RuntimeError(f"size mismatch: {partial.stat().st_size} != {total}")
    partial.replace(target)
    state_path.unlink(missing_ok=True)
    # A wrong declared size can hide dropped chunks, so the safetensors header is
    # the final authority (it states the exact byte length the file must have).
    if not _verified(target):
        raise RuntimeError(f"{target.name} failed its header check; repair with repair_safetensors.py")
    print(f"[{name}] complete and verified: {target}", flush=True)


def _verified(path: Path) -> bool:
    """True when the file size matches what its own safetensors header demands."""
    if path.suffix != ".safetensors":
        return path.exists()
    try:
        with path.open("rb") as handle:
            header_len = int.from_bytes(handle.read(8), "little")
            if header_len <= 0 or header_len > 512 * 1024 * 1024:
                return False
            header = json.loads(handle.read(header_len).decode("utf-8"))
        largest = 0
        for key, value in header.items():
            if key == "__metadata__" or not isinstance(value, dict):
                continue
            offsets = value.get("data_offsets")
            if isinstance(offsets, list) and len(offsets) == 2:
                largest = max(largest, int(offsets[1]))
        return path.stat().st_size == 8 + header_len + largest
    except (OSError, ValueError, UnicodeDecodeError):
        return False


def main() -> int:
    wanted = [int(value) for value in sys.argv[1:]] or list(range(len(FILES)))
    for index in wanted:
        try:
            download(index)
        except Exception as exc:  # noqa: BLE001
            print(f"FAILED index {index}: {exc}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
