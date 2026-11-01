"""Download the Qwen-Image-2.1 Turbo stack with curl (mirror-friendly).

What actually gets downloaded, and why:

  diffusion_models/Qwen-Image-2.1-Turbo-Q8_0.gguf                7.07 GiB  denoiser
  text_encoders/Qwen-Image-2.1-Turbo-Uncensored-Q8_0.gguf        8.11 GiB  abliterated Qwen3-VL encoder
  vae/qwen_image_2.1_vae_bf16.safetensors                        0.63 GiB  VAE

The "Uncensored" file turned out to be the *text encoder*
(Qwen3-VL-8B-Instruct-Abliterated-Uncensored, GGUF arch `qwen3vl`), not the
denoiser -- which matches the publisher's own finding that refusals live in the
encoder while the image model itself is unfiltered.

Python's TLS stack is refused by both mirrors on this network (SSL
UNEXPECTED_EOF) while curl.exe succeeds, so each 8 MiB range is fetched by a curl
subprocess. Ranges already on disk are kept, so re-running resumes.

    python fetch_turbo_curl.py            # fetch what is missing, then assemble
    python fetch_turbo_curl.py --verify   # only report sizes
"""

from __future__ import annotations

import argparse
import subprocess
import threading
import time
from pathlib import Path

MODELS = Path(r"D:\AI\ComfyUI-BSAI_v010\ComfyUI\models")
OUT = Path(r"D:\AI\deployment\turbo-download")
CHUNK = 8 * 1024 * 1024
WORKERS = 12
MIRRORS = ["https://aifasthub.com", "https://hf-mirror.com"]

FILES = [
    ("diffusion_models", "qwen-image-2.1-turbo-Q8_0.gguf",
     "/unsloth/Qwen-Image-2.1-Turbo-GGUF/resolve/main/qwen-image-2.1-turbo-Q8_0.gguf",
     7_640_860_480),
    ("diffusion_models", "qwen_image_2.1_int8_convrot.safetensors",
     "/Comfy-Org/Qwen-Image-2.1/resolve/main/diffusion_models/qwen_image_2.1_int8_convrot.safetensors",
     7_256_783_064),
    ("text_encoders", "Qwen-Image-2.1-Turbo-Uncensored-Q8_0.gguf",
     "/AtomicChat/Qwen-Image-2.1-Turbo-Uncensored-GGUF/resolve/main/Qwen-Image-2.1-Turbo-Uncensored-Q8_0.gguf",
     8_709_520_096),
    ("vae", "qwen_image_2.1_vae_bf16.safetensors",
     "/Comfy-Org/Qwen-Image-2.1/resolve/main/vae/qwen_image_2.1_vae_bf16.safetensors",
     675_509_688),
]

CURL = r"C:\Windows\System32\curl.exe"


def size_of(index: int, expected: int) -> int:
    return min(CHUNK, expected - index * CHUNK)


def fetch_chunk(url: str, part: Path, start: int, end: int, want: int) -> None:
    if part.is_file() and part.stat().st_size == want:
        return
    last = ""
    for attempt in range(6):
        result = subprocess.run(
            [CURL, "-sS", "-L", "--fail", "--max-time", "240", "--connect-timeout", "30",
             "-r", f"{start}-{end}", "-o", str(part), url],
            capture_output=True, text=True, timeout=300)
        if result.returncode == 0 and part.is_file() and part.stat().st_size == want:
            return
        last = (result.stderr or "").strip()[:120]
        time.sleep(2 + attempt * 3)
    raise RuntimeError(f"chunk {start}-{end} failed: {last}")


def download(path: str, dest: Path, expected: int) -> bool:
    if dest.is_file() and dest.stat().st_size == expected:
        print(f"[skip] {dest.name} complete", flush=True)
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    parts = OUT / f"{dest.name}.parts"
    parts.mkdir(exist_ok=True)
    count = (expected + CHUNK - 1) // CHUNK
    pending = [index for index in range(count)
               if not (parts / f"{index:05d}").is_file()
               or (parts / f"{index:05d}").stat().st_size != size_of(index, expected)]
    print(f"[start] {dest.name}: {expected / 1024**3:.2f} GiB, {len(pending)}/{count} chunks missing", flush=True)
    if pending:
        deadline = time.time() + 1500
        errors: list[str] = []
        lock = threading.Lock()
        done = 0
        started = time.time()
        total = len(pending)

        def worker(url: str) -> None:
            nonlocal done
            while True:
                with lock:
                    if not pending or time.time() > deadline:
                        return
                    index = pending.pop(0)
                start = index * CHUNK
                try:
                    fetch_chunk(url, parts / f"{index:05d}", start,
                                min(expected, start + CHUNK) - 1, size_of(index, expected))
                except BaseException as exc:  # noqa: BLE001
                    with lock:
                        errors.append(str(exc))
                    return
                with lock:
                    done += 1
                    if done % 25 == 0 or done == total:
                        elapsed = max(1.0, time.time() - started)
                        print(f"  {dest.name}: {done}/{total} chunks "
                              f"{done * CHUNK / elapsed / 1024**2:.2f} MiB/s", flush=True)

        threads = []
        for slot in range(min(WORKERS, max(1, total))):
            url = MIRRORS[slot % len(MIRRORS)] + path
            thread = threading.Thread(target=worker, args=(url,), daemon=True)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()
        still = [index for index in range(count)
                 if not (parts / f"{index:05d}").is_file()
                 or (parts / f"{index:05d}").stat().st_size != size_of(index, expected)]
        print(f"  {dest.name}: {len(still)} chunks still missing, errors={errors[:2]}", flush=True)
        if still:
            return True  # partial progress; caller loops

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
    ok = size == expected
    print(f"[{'done' if ok else 'BAD'}] {dest.name} {size / 1024**3:.2f} GiB "
          f"(expected {expected / 1024**3:.2f})", flush=True)
    if ok:
        for index in range(count):
            (parts / f"{index:05d}").unlink(missing_ok=True)
        parts.rmdir()
    return not ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--rounds", type=int, default=1)
    args = parser.parse_args()
    if args.verify:
        bad = 0
        for folder, name, _path, expected in FILES:
            dest = MODELS / folder / name
            ok = dest.is_file() and dest.stat().st_size == expected
            bad += 0 if ok else 1
            print(f"  {'ok' if ok else 'missing':8} {folder}/{name}")
        return 1 if bad else 0
    for _round in range(args.rounds):
        for folder, name, path, expected in FILES:
            download(path, MODELS / folder / name, expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
