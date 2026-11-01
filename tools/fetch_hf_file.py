"""Fetch a Hugging Face file with the standard library only.

No pip install, no huggingface_hub: this machine already has a Python that can
reach huggingface.co, and a 7 GB download should not depend on a package being
installable. Hugging Face answers a /resolve/ URL with a redirect to a CDN, so
the whole job is: follow redirects, stream to a .part file, then rename.

Resume matters here. A 7 GB transfer over a home link will be interrupted, and
restarting from zero is the difference between a download that finishes and one
that does not, so a partial file is continued with a Range request whenever the
server offers one.

Usage:  python fetch_hf_file.py <repo> <filename> <dest-dir>
"""

import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

CHUNK = 1 << 20
# A stalled socket must fail the attempt rather than hang for hours; the loop
# below simply starts a new attempt and resumes.
TIMEOUT = 60
ATTEMPTS = 200


def remote_size(repo, name):
    url = f"https://huggingface.co/{repo}/resolve/main/{name}"
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        length = response.headers.get("Content-Length")
        return int(length) if length else None


def download_once(repo, name, target):
    """One resume attempt. Returns (downloaded_this_round, total_or_None, done)."""
    url = f"https://huggingface.co/{repo}/resolve/main/{name}"
    have = target.stat().st_size if target.exists() else 0
    headers = {"User-Agent": "curl/8"}
    if have:
        headers["Range"] = f"bytes={have}-"

    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        # 206 means the server honoured the Range; 200 means it ignored it and we
        # must not append to what is already on disk.
        if have and response.status == 200:
            have = 0
        total = None
        if response.headers.get("Content-Length"):
            total = int(response.headers["Content-Length"]) + have
        mode = "ab" if have else "wb"
        done = have
        started = time.time()
        last_report = 0.0
        with target.open(mode) as handle:
            while True:
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                now = time.time()
                if now - last_report >= 5:
                    last_report = now
                    speed = (done - have) / max(now - started, 0.01) / 1e6
                    if total:
                        pct = done / total * 100
                        print(f"    {done/1e9:.2f}/{total/1e9:.2f} GB ({pct:.1f}%) "
                              f"{speed:.1f} MB/s", flush=True)
                    else:
                        print(f"    {done/1e9:.2f} GB {speed:.1f} MB/s", flush=True)
    return done, total


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    repo, name, dest = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / Path(name).name
    part = target.with_name(target.name + ".part")

    try:
        expected = remote_size(repo, name)
        print(f"source : https://huggingface.co/{repo}")
        print(f"file   : {name}")
        print(f"size   : {expected/1e9:.2f} GB" if expected else "size   : unknown")
        print(f"target : {target}", flush=True)
    except Exception as exc:  # noqa: BLE001 - a HEAD failure is not fatal
        expected = None
        print(f"HEAD failed ({exc}); continuing without a size", flush=True)

    if target.exists() and expected and target.stat().st_size == expected:
        print(f"already complete: {target} ({target.stat().st_size/1e9:.2f} GB)")
        return 0

    for attempt in range(1, ATTEMPTS + 1):
        try:
            done, total = download_once(repo, name, part)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"  attempt {attempt} interrupted: {exc}", flush=True)
            time.sleep(min(2 * attempt, 20))
            continue
        if total and done < total:
            print(f"  attempt {attempt} short ({done}/{total}); resuming", flush=True)
            continue
        if total is None and expected and done < expected:
            print(f"  attempt {attempt} short ({done}/{expected}); resuming", flush=True)
            continue
        part.replace(target)
        print(f"OK: {target} ({target.stat().st_size/1e9:.2f} GB)")
        return 0

    print("FAILED: too many interrupted attempts")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
