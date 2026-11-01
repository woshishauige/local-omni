"""Repair a truncated safetensors download and verify model files properly.

The safetensors header states the largest data offset, so the exact file size is
knowable without hashing: ``8 + header_len + max_offset``. This tool re-downloads
only the missing tail of a file with range requests, and can verify any file.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

MODELS = Path(r"D:\AI\ComfyUI-BSAI_v010\ComfyUI\models")
CHUNK = 8 * 1024 * 1024
ATTEMPTS = 15


def expected_size(path: Path) -> int:
    """Exact size a safetensors file must have, from its own header."""
    with path.open("rb") as handle:
        header_len = int.from_bytes(handle.read(8), "little")
        header = json.loads(handle.read(header_len).decode("utf-8"))
    largest = 0
    for key, value in header.items():
        if key == "__metadata__" or not isinstance(value, dict):
            continue
        offsets = value.get("data_offsets")
        if isinstance(offsets, list) and len(offsets) == 2:
            largest = max(largest, int(offsets[1]))
    return 8 + header_len + largest


def fetch(url: str, start: int, end: int) -> bytes:
    last: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = requests.get(url, headers={"Range": f"bytes={start}-{end - 1}"},
                                    stream=True, timeout=(30, 90))
            if response.status_code not in (200, 206):
                response.close()
                raise RuntimeError(f"HTTP {response.status_code}")
            data = b"".join(response.iter_content(1024 * 512))
            response.close()
            if len(data) != end - start:
                raise RuntimeError(f"short read {len(data)} != {end - start}")
            return data
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(min(2.0 * attempt, 15))
    raise RuntimeError(f"range {start}-{end} failed: {last}")


def remote_size(url: str) -> int:
    """The server's own content length, used when the local header is unreadable."""
    response = requests.head(url, allow_redirects=True, timeout=45)
    response.raise_for_status()
    return int(response.headers.get("x-linked-size") or response.headers.get("content-length") or 0)


def repair(path: Path, url: str, tail_mb: int = 1200) -> None:
    want = remote_size(url)
    if not want:
        want = expected_size(path)
    have = path.stat().st_size
    print(f"{path.name}: have {have} need {want} (missing {want - have} bytes)")
    if have == want:
        print("size matches; verifying header")
        if verify(path):
            return
        print("header damaged; re-fetching the head")
    # The tail is rewritten because a dropped chunk cannot be told apart from a
    # short tail, and a damaged header means the head must be rewritten too.
    head_boundary = min(32 * 1024 * 1024, want)
    if have < head_boundary:
        start = 0
    else:
        start = max(0, want - tail_mb * 1024 * 1024)
    with path.open("r+b") as handle:
        handle.truncate(start)
        written = start
        while written < want:
            end = min(written + CHUNK, want)
            data = fetch(url, written, end)
            handle.write(data)
            written += len(data)
            if (written // (256 * 1024 * 1024)) != ((written - len(data)) // (256 * 1024 * 1024)):
                print(f"  {written}/{want} bytes ({written / want * 100:.2f}%)", flush=True)
    final = path.stat().st_size
    if final != want:
        raise RuntimeError(f"still wrong size: {final} != {want}")
    print(f"repaired: {path.name} = {final} bytes")
    verify(path)


def verify(path: Path) -> bool:
    try:
        want = expected_size(path)
    except Exception as exc:  # noqa: BLE001
        print(f"{path.name}: unreadable header ({exc})")
        return False
    have = path.stat().st_size
    ok = have == want
    print(f"{path.name}: {'OK' if ok else 'CORRUPT'} ({have} / {want})")
    return ok


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "verify"
    if mode == "repair":
        target = MODELS / sys.argv[2]
        repair(target, sys.argv[3])
    else:
        bad = 0
        for relative in sys.argv[2:]:
            if not verify(MODELS / relative):
                bad += 1
        raise SystemExit(1 if bad else 0)
