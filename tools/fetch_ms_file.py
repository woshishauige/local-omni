"""Fetch a file from ModelScope with the standard library only.

Same shape as fetch_hf_file.py but pointed at ModelScope's repo endpoint, which
serves the bytes directly. ModelScope measured ~7x faster than huggingface.co from
this machine (8.3 MB/s vs 1.2 MB/s), so the TTS weights come from here.

Usage:  python fetch_ms_file.py <repo> <filename> <dest-dir>
"""

import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CHUNK = 1 << 20
TIMEOUT = 60
ATTEMPTS = 200
API = "https://www.modelscope.cn/api/v1/models/{repo}/repo"


def url_for(repo, name):
    return API.format(repo=repo) + "?Revision=master&FilePath=" + urllib.parse.quote(name)


def remote_size(repo, name):
    request = urllib.request.Request(url_for(repo, name), method="HEAD",
                                     headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        length = response.headers.get("Content-Length")
        return int(length) if length else None


def download_once(repo, name, target):
    have = target.stat().st_size if target.exists() else 0
    headers = {"User-Agent": "curl/8"}
    if have:
        headers["Range"] = f"bytes={have}-"
    request = urllib.request.Request(url_for(repo, name), headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        if have and response.status == 200:
            have = 0
        total = None
        if response.headers.get("Content-Length"):
            total = int(response.headers["Content-Length"]) + have
        done = have
        started = time.time()
        last = 0.0
        with target.open("ab" if have else "wb") as handle:
            while True:
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                now = time.time()
                if now - last >= 5:
                    last = now
                    speed = (done - have) / max(now - started, 0.01) / 1e6
                    pct = f" ({done/total*100:.1f}%)" if total else ""
                    print(f"    {done/1e9:.2f}/{total/1e9:.2f} GB{pct} {speed:.1f} MB/s"
                          if total else f"    {done/1e9:.2f} GB {speed:.1f} MB/s", flush=True)
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
        print(f"source : modelscope {repo}")
        print(f"file   : {name}")
        print(f"size   : {expected/1e9:.2f} GB" if expected else "size   : unknown", flush=True)
    except Exception as exc:  # noqa: BLE001
        expected = None
        print(f"HEAD failed ({exc}); continuing", flush=True)

    if target.exists() and expected and target.stat().st_size == expected:
        print(f"already complete: {target}")
        return 0

    for attempt in range(1, ATTEMPTS + 1):
        try:
            done, total = download_once(repo, name, part)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"  attempt {attempt} interrupted: {exc}", flush=True)
            time.sleep(min(2 * attempt, 20))
            continue
        if total and done < total:
            print(f"  attempt {attempt} short; resuming", flush=True)
            continue
        part.replace(target)
        print(f"OK: {target} ({target.stat().st_size/1e9:.2f} GB)")
        return 0
    print("FAILED: too many interrupted attempts")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
