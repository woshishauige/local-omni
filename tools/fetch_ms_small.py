"""Download the small companion files a TTS model needs, from ModelScope.

The 3.6 GB weight file was fetched first; these are the few-megabyte files beside
it (config, tokenizer, generation settings) that the export scripts read. Missing
any one of them fails the conversion with an unrelated-looking error, so this
pulls the whole published list rather than a hand-picked subset.

Usage:  python fetch_ms_small.py <repo> <dest-dir> [--max-mb N]
"""

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://www.modelscope.cn/api/v1/models/{repo}/repo/files?Revision=master&Root="
DOWNLOAD = "https://www.modelscope.cn/api/v1/models/{repo}/repo?Revision=master&FilePath={name}"


def list_files(repo, root=""):
    url = API.format(repo=repo) + urllib.parse.quote(root)
    request = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    return payload["Data"]["Files"]


def fetch(repo, name, target, expect):
    url = DOWNLOAD.format(repo=repo, name=urllib.parse.quote(name))
    request = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    temporary = target.with_name(target.name + ".part")
    with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as handle:
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            handle.write(chunk)
    got = temporary.stat().st_size
    if expect and got != expect:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"{name}: got {got} bytes, expected {expect}")
    temporary.replace(target)
    return got


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    repo, dest = sys.argv[1], Path(sys.argv[2])
    cap_mb = None
    if "--max-mb" in sys.argv:
        cap_mb = float(sys.argv[sys.argv.index("--max-mb") + 1])
    dest.mkdir(parents=True, exist_ok=True)

    files = [f for f in list_files(repo) if f["Type"] == "blob"]
    # Nested directories are published as their own listings, so recurse into them:
    # speech_tokenizer holds the audio codec config the export scripts require.
    trees = [f["Name"] for f in list_files(repo) if f["Type"] == "tree"]
    for tree in trees:
        for entry in list_files(repo, tree + "/"):
            if entry["Type"] == "blob":
                entry = dict(entry)
                entry["Name"] = f"{tree}/{entry['Name']}"
                files.append(entry)

    fetched = skipped = 0
    for entry in files:
        name, size = entry["Name"], entry["Size"]
        if cap_mb is not None and size > cap_mb * 1e6:
            print(f"  skip {name} ({size/1e6:.1f} MB over cap)")
            skipped += 1
            continue
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and target.stat().st_size == size:
            print(f"  ok   {name} (already present)")
            continue
        print(f"  get  {name} ({size/1e6:.2f} MB)", flush=True)
        try:
            got = fetch(repo, name, target, size)
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"  FAIL {name}: {exc}")
            continue
        print(f"  done {name} ({got/1e6:.2f} MB)")
        fetched += 1

    print(f"fetched={fetched} skipped={skipped} (nested: {', '.join(trees) or 'none'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
