"""Try the domestic mirrors for the same files and measure throughput."""
import concurrent.futures
import time

import requests

BASE = "CyberRealisticPony_V18.0_F16.safetensors"
CANDIDATES = [
    ("hf-mirror", f"https://hf-mirror.com/cyberdelia/CyberRealisticPony/resolve/main/{BASE}"),
    ("modelscope-cyberdelia", f"https://modelscope.cn/models/cyberdelia/CyberRealisticPony/resolve/master/{BASE}"),
    ("modelscope-search", "https://modelscope.cn/api/v1/dolphin/models?PageSize=10&Query=CyberRealisticPony"),
]


def timed(label: str, url: str, seconds: float = 20.0, workers: int = 1) -> None:
    def pull(offset: int) -> int:
        got = 0
        started = time.time()
        try:
            response = requests.get(url, headers={"Range": f"bytes={offset}-{offset + 32 * 1024 * 1024}"},
                                    stream=True, timeout=(20, 40), allow_redirects=True)
            if response.status_code not in (200, 206):
                return -response.status_code
            for chunk in response.iter_content(512 * 1024):
                got += len(chunk)
                if time.time() - started > seconds:
                    break
            response.close()
        except Exception as exc:  # noqa: BLE001
            print(f"  {label} error: {type(exc).__name__}: {str(exc)[:90]}")
        return got

    started = time.time()
    if workers == 1:
        total = pull(0)
        status = "" if total >= 0 else f"HTTP {-total}"
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(pull, [index * 96 * 1024 * 1024 for index in range(workers)]))
        total = sum(value for value in results if value > 0)
        status = "mixed" if any(value < 0 for value in results) else ""
    elapsed = max(0.001, time.time() - started)
    print(f"{label:24} {total/1e6:7.1f} MB / {elapsed:4.1f}s = {total/elapsed/1e6:5.2f} MB/s {status}")


timed("hf-mirror x1", CANDIDATES[0][1])
timed("hf-mirror x6", CANDIDATES[0][1], workers=6)
for label, url in CANDIDATES[1:]:
    try:
        response = requests.get(url, timeout=25)
        print(f"{label:24} status={response.status_code} bytes={len(response.content)} head={response.text[:80]!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"{label:24} FAILED {type(exc).__name__}: {str(exc)[:90]}")
