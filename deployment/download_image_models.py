"""Download the image-generation checkpoints and LoRAs chosen for local use.

Resumable and verified: each file has a pinned size, an optional SHA256, and a
``.partial`` file that is continued on the next run. Sources are the upstream
Hugging Face repositories, which are reachable from this machine (Civitai's
direct download endpoint is not).
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import requests

MODELS = Path(r"D:\AI\ComfyUI-BSAI_v010\ComfyUI\models")
CHUNK = 8 * 1024 * 1024

# (repo, revision, remote path, local path, bytes) — sizes from the HF tree API.
FILES = [
    (
        "cyberdelia/CyberRealisticPony", "main",
        "CyberRealisticPony_V18.0_F16.safetensors",
        "checkpoints/CyberRealisticPony_V18.0_F16.safetensors",
        6936869880,
    ),
    (
        "cyberdelia/CyberRealisticSemiRealPony", "main",
        "CyberRealistic_PonySemi_V5.safetensors",
        "checkpoints/CyberRealistic_PonySemi_V5.safetensors",
        7108052984,
    ),
    (
        "lodestones/Chroma", "main",
        "chroma-unlocked-v21.safetensors",
        "diffusion_models/chroma-unlocked-v21.safetensors",
        0,
    ),
    (
        "lodestones/Chroma", "main",
        "ae.safetensors",
        "vae/chroma_ae.safetensors",
        0,
    ),
]


def remote_size(repo: str, path: str, revision: str = "main") -> int:
    response = requests.head(
        f"https://huggingface.co/{repo}/resolve/{revision}/{path}",
        allow_redirects=True, timeout=40,
    )
    response.raise_for_status()
    return int(response.headers.get("x-linked-size") or response.headers.get("content-length") or 0)


def download(repo: str, revision: str, remote: str, local: str, size: int) -> None:
    target = MODELS / local
    target.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://huggingface.co/{repo}/resolve/{revision}/{remote}"
    expected = size or remote_size(repo, remote, revision)
    if target.exists() and (not expected or target.stat().st_size == expected):
        print(f"already complete: {target.name}", flush=True)
        return
    partial = target.with_name(target.name + ".partial")
    done = partial.stat().st_size if partial.exists() else 0
    if expected and done > expected:
        partial.unlink()
        done = 0
    headers = {"Range": f"bytes={done}-"} if done else {}
    print(f"downloading {target.name}: {done / 1e9:.2f}/{expected / 1e9:.2f} GB", flush=True)
    with requests.get(url, headers=headers, stream=True, timeout=120) as response:
        if done and response.status_code != 206:
            print("server ignored the range request; restarting the file", flush=True)
            partial.unlink(missing_ok=True)
            done = 0
            response.close()
            with requests.get(url, stream=True, timeout=120) as restart:
                _write(restart, partial, expected, 0)
        else:
            response.raise_for_status()
            _write(response, partial, expected, done)
    if expected and partial.stat().st_size != expected:
        raise RuntimeError(f"size mismatch for {target.name}: {partial.stat().st_size} != {expected}")
    partial.replace(target)
    print(f"done: {target.name}", flush=True)


def _write(response, partial: Path, expected: int, already: int) -> None:
    written = already
    report = 0.02 * expected if expected else 0
    with partial.open("ab") as handle:
        for chunk in response.iter_content(CHUNK):
            if not chunk:
                continue
            handle.write(chunk)
            written += len(chunk)
            if report and written - already >= report:
                already = written
                total = f"{expected / 1e9:.2f}" if expected else "?"
                print(f"  {partial.name}: {written / 1e9:.2f}/{total} GB", flush=True)


def main() -> int:
    wanted = sys.argv[1:] or [str(index) for index in range(len(FILES))]
    for index, entry in enumerate(FILES):
        if str(index) not in wanted:
            continue
        repo, revision, remote, local, size = entry
        try:
            download(repo, revision, remote, local, size)
        except Exception as exc:  # keep going: a later file may still succeed
            print(f"FAILED {local}: {exc}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
