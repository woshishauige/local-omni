"""Compare HF mirrors/CDNs for the same checkpoint file."""
import concurrent.futures
import time

import requests

CANDIDATES = [
    ("cyberdelia (official)", "https://huggingface.co/cyberdelia/CyberRealisticPony/resolve/main/CyberRealisticPony_V18.0_F16.safetensors"),
    ("ZeusAIWorld v18", "https://huggingface.co/ZeusAIWorld/CyberRealisticPony-v18/resolve/main/CyberRealisticPony_V18.0_F16.safetensors"),
    ("hf-mirror (official path)", "https://hf-mirror.com/cyberdelia/CyberRealisticPony/resolve/main/CyberRealisticPony_V18.0_F16.safetensors"),
    ("modelscope mirror", "https://modelscope.cn/models/AI-ModelScope/CyberRealisticPony/resolve/master/CyberRealisticPony_V18.0_F16.safetensors"),
]


def one(url: str, offset: int, limit: int = 12 * 1024 * 1024) -> tuple[int, str]:
    try:
        response = requests.get(url, headers={"Range": f"bytes={offset}-{offset + limit - 1}"},
                                stream=True, timeout=(20, 45), allow_redirects=True)
        if response.status_code not in (200, 206):
            return 0, f"HTTP {response.status_code}"
        total = sum(len(chunk) for chunk in response.iter_content(512 * 1024))
        response.close()
        return total, ""
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {str(exc)[:60]}"


for label, url in CANDIDATES:
    started = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda index: one(url, index * 220 * 1024 * 1024), range(4)))
    got = sum(value for value, _ in results)
    errors = [error for _, error in results if error]
    elapsed = max(0.001, time.time() - started)
    flag = f"  errors={errors[0]}" if errors else ""
    print(f"{label:26} {got/1e6:7.1f} MB / {elapsed:4.1f}s = {got/elapsed/1e6:5.2f} MB/s{flag}")
