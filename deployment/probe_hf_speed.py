"""Measure single vs parallel throughput to the HF CDN for one large file."""
import concurrent.futures
import time

import requests

URL = "https://huggingface.co/cyberdelia/CyberRealisticPony/resolve/main/CyberRealisticPony_V18.0_F16.safetensors"
CHUNK = 4 * 1024 * 1024


def pull(start: int, seconds: float = 25.0) -> int:
    headers = {"Range": f"bytes={start}-{start + 64 * 1024 * 1024}"}
    written = 0
    started = time.time()
    with requests.get(URL, headers=headers, stream=True, timeout=45) as response:
        for chunk in response.iter_content(CHUNK * 4):
            written += len(chunk)
            if time.time() - started > seconds:
                break
    return written


started = time.time()
single = pull(0, 20)
single_speed = single / (time.time() - started) / 1e6
print(f"single stream: {single/1e6:.1f} MB at {single_speed:.2f} MB/s")

for workers in (4, 8):
    started = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(pull, (index + 1) * 512 * 1024 * 1024, 20.0) for index in range(workers)]
        total = sum(future.result() for future in futures)
    elapsed = time.time() - started
    print(f"{workers} streams: {total/1e6:.1f} MB at {total/elapsed/1e6:.2f} MB/s")
