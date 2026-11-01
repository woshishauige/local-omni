"""Probe which download route works from this machine for large model files."""
import os
import time
from pathlib import Path

import requests

URL = "https://huggingface.co/cyberdelia/CyberRealisticPony/resolve/main/CyberRealisticPony_V18.0_F16.safetensors"
MIRROR = URL.replace("huggingface.co", "hf-mirror.com")
TARGET = Path(r"D:\AI\logs-probe.bin")


def probe(label, url, headers=None, limit=8 * 1024 * 1024, seconds=60):
    headers = dict(headers or {})
    headers["Range"] = f"bytes=0-{limit - 1}"
    started = time.time()
    try:
        with requests.get(url, headers=headers, stream=True, timeout=45, allow_redirects=True) as response:
            status = response.status_code
            final = response.url.split("?")[0][:80]
            written = 0
            for chunk in response.iter_content(1024 * 256):
                written += len(chunk)
                if written >= limit or time.time() - started > seconds:
                    break
            speed = written / max(0.001, time.time() - started) / 1e6
            print(f"{label:28} status={status} got={written/1e6:.1f}MB {speed:.1f}MB/s host={final}")
    except Exception as exc:
        print(f"{label:28} FAILED {type(exc).__name__}: {str(exc)[:120]}")


print("huggingface_hub available:", end=" ")
try:
    import huggingface_hub  # noqa: F401
    print(huggingface_hub.__version__)
except Exception as exc:
    print("no", exc)

probe("direct", URL)
probe("direct + xet-off header", URL, {"x-use-xet": "0"})
probe("hf-mirror", MIRROR)
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
probe("modelscope-style CDN", "https://cdn-lfs.hf.co/repos/66/3c/663ca186e14047f710712f5b/27ee80911a11cf6a2dbc4deafaa1e264869b66be6716906216e54452eee43eb1")
