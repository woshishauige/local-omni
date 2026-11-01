"""Download the uncensored Qwen-Image-2.1 Turbo stack (hf-mirror, resumable).

Files (ComfyUI layout):
  models/diffusion_models/  Qwen-Image-2.1-Turbo-Uncensored-Q8_0.gguf   8.11 GiB  [done]
  models/text_encoders/     qwen3vl_8b_w4a8.safetensors                 5.88 GiB
  models/vae/               qwen_image_2.1_vae_bf16.safetensors         0.63 GiB

huggingface.co is unreachable from this network, so every request goes through
hf-mirror.com. The mirror closes long connections, so each file is cut into 8 MiB
ranges; completed ranges are kept and only the missing ones are fetched. Assembly
is atomic (temp file + rename) and thread errors are surfaced instead of silently
producing a truncated file.

    python fetch_turbo_uncensored.py            # download what is missing
    python fetch_turbo_uncensored.py --verify   # only check sizes
"""

from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path

import httpx

MODELS = Path(r"D:\AI\ComfyUI-BSAI_v010\ComfyUI\models")
OUT = Path(r"D:\AI\deployment\turbo-download")
WORKERS = 8
CHUNK = 8 * 1024 * 1024
MIRROR = "https://aifasthub.com"

FILES = [
    ("diffusion_models", "Qwen-Image-2.1-Turbo-Uncensored-Q8_0.gguf",
     f"{MIRROR}/AtomicChat/Qwen-Image-2.1-Turbo-Uncensored-GGUF/resolve/main/Qwen-Image-2.1-Turbo-Uncensored-Q8_0.gguf",
     8_709_520_096),
    ("text_encoders", "qwen3vl_8b_w4a8.safetensors",
     f"{MIRROR}/Comfy-Org/Qwen-Image-2.1/resolve/main/text_encoders/qwen3vl_8b_w4a8.safetensors",
     6_312_105_364),
    ("vae", "qwen_image_2.1_vae_bf16.safetensors",
     f"{MIRROR}/Comfy-Org/Qwen-Image-2.1/resolve/main/vae/qwen_image_2.1_vae_bf16.safetensors",
     675_509_688),
]


def chunk_size(index: int, expected: int) -> int:
    return min(CHUNK, expected - index * CHUNK)


def download(url: str, dest: Path, expected: int) -> None:
    if dest.is_file() and dest.stat().st_size == expected:
        print(f"[skip] {dest.name} already complete", flush=True)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    parts = OUT / f"{dest.name}.parts"
    parts.mkdir(exist_ok=True)
    count = (expected + CHUNK - 1) // CHUNK

    def fetch(index: int) -> None:
        part = parts / f"{index:05d}"
        start = index * CHUNK
        end = min(expected, start + CHUNK) - 1
        want = end - start + 1
        if part.is_file() and part.stat().st_size == want:
            return
        headers = {"Range": f"bytes={start}-{end}", "User-Agent": "dsh-local-fetch"}
        last_error: BaseException | None = None
        for attempt in range(4):
            try:
                with httpx.stream("GET", url, headers=headers, timeout=300, follow_redirects=True) as response:
                    if response.status_code not in (200, 206):
                        raise RuntimeError(f"HTTP {response.status_code}")
                    with part.open("wb") as handle:
                        for block in response.iter_bytes(262144):
                            handle.write(block)
                if part.stat().st_size != want:
                    raise RuntimeError(f"short read {part.stat().st_size}/{want}")
                return
            except BaseException as exc:  # noqa: BLE001 - transport errors of every kind
                last_error = exc
                time.sleep(1.5 + attempt * 2)
        raise RuntimeError(f"{dest.name} chunk {index} failed: {last_error}")

    pending = [index for index in range(count)
               if not (parts / f"{index:05d}").is_file()
               or (parts / f"{index:05d}").stat().st_size != chunk_size(index, expected)]
    print(f"[start] {dest.name}: {expected/1024**3:.2f} GiB, "
          f"{len(pending)}/{count} chunks to fetch", flush=True)

    lock = threading.Lock()
    errors: list[BaseException] = []
    done = 0
    started = time.time()
    total = len(pending)

    def worker() -> None:
        nonlocal done
        while True:
            with lock:
                if not pending:
                    return
                index = pending.pop(0)
            try:
                fetch(index)
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)
                return
            with lock:
                done += 1
                if done % 20 == 0 or done == total:
                    elapsed = max(1.0, time.time() - started)
                    print(f"  {dest.name}: {done}/{total} chunks "
                          f"{done * CHUNK / elapsed / 1024**2:.2f} MiB/s", flush=True)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(min(WORKERS, max(1, total)))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise SystemExit(f"{dest.name}: {len(errors)} chunk(s) failed: {errors[0]}")

    missing = [index for index in range(count)
               if not (parts / f"{index:05d}").is_file()
               or (parts / f"{index:05d}").stat().st_size != chunk_size(index, expected)]
    if missing:
        raise SystemExit(f"{dest.name}: {len(missing)} chunk(s) missing before assembly: {missing[:5]}")

    temporary = dest.with_name(dest.name + ".assembling")
    with temporary.open("wb") as out:
        for index in range(count):
            with (parts / f"{index:05d}").open("rb") as handle:
                while True:
                    block = handle.read(1024 * 1024)
                    if not block:
                        break
                    out.write(block)
    temporary.replace(dest)
    size = dest.stat().st_size
    if size != expected:
        raise SystemExit(f"{dest.name}: assembled {size} != expected {expected}")
    for index in range(count):
        (parts / f"{index:05d}").unlink(missing_ok=True)
    parts.rmdir()
    print(f"[done] {dest.name} {size/1024**3:.2f} GiB", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    missing = 0
    for folder, name, url, expected in FILES:
        dest = MODELS / folder / name
        if args.verify:
            ok = dest.is_file() and dest.stat().st_size == expected
            missing += 0 if ok else 1
            print(f"  {'ok' if ok else 'missing':8} {name} ({expected/1024**3:.2f} GiB expected)")
            continue
        download(url, dest, expected)
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
