"""Measure parallel chunk throughput for one large HF file."""
import concurrent.futures
import time

import requests

URL = "https://huggingface.co/cyberdelia/CyberRealisticPony/resolve/main/CyberRealisticPony_V18.0_F16.safetensors"
SIZE = 6936869880


def pull(start: int, length: int, attempts: int = 5) -> int:
    last = None
    for attempt in range(attempts):
        try:
            response = requests.get(
                URL, headers={"Range": f"bytes={start}-{start + length - 1}"},
                stream=True, timeout=(30, 60),
            )
            if response.status_code not in (200, 206):
                raise RuntimeError(f"HTTP {response.status_code}")
            got = sum(len(chunk) for chunk in response.iter_content(512 * 1024))
            response.close()
            return got
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1 + attempt)
    print(f"  chunk {start} failed: {last}")
    return 0


for workers, length_mb in ((4, 16), (8, 16), (12, 8)):
    length = length_mb * 1024 * 1024
    starts = [((index * 7 + 3) * 64 * 1024 * 1024) % (SIZE - length * 2) for index in range(workers)]
    started = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        total = sum(pool.map(lambda start: pull(start, length), starts))
    elapsed = max(0.001, time.time() - started)
    print(f"workers={workers:>2} chunk={length_mb}MB -> {total/1e6:7.1f} MB in {elapsed:5.1f}s = {total/elapsed/1e6:5.2f} MB/s")
