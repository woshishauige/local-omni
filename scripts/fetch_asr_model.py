"""Fetch a faster-whisper CTranslate2 model from ModelScope into models\\asr\\<size>.

Hugging Face is unreachable from this machine (both huggingface.co and hf-mirror.com
time out), so ModelScope is the only working source. The layout it publishes is the
one faster-whisper expects: config.json, tokenizer.json, vocabulary.txt, model.bin.

Usage:  python fetch_asr_model.py [size]      # size = small | medium | large-v3 | ...
"""
import json
import shutil
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET_ROOT = ROOT / "models" / "asr"
API = "https://www.modelscope.cn/api/v1/models/{repo}/repo/files?Revision=master&Root="
DOWNLOAD = "https://www.modelscope.cn/api/v1/models/{repo}/repo?Revision=master&FilePath={name}"
WANTED = {"config.json", "tokenizer.json", "vocabulary.txt", "vocabulary.json",
          "preprocessor_config.json"}


def repo_for(size):
    return f"pengzhendong/faster-whisper-{size}"


def list_files(repo):
    with urllib.request.urlopen(API.format(repo=repo), timeout=30) as response:
        payload = json.load(response)
    return [entry for entry in payload["Data"]["Files"] if entry["Type"] == "blob"]


def download(repo, name, target, expect_bytes):
    url = DOWNLOAD.format(repo=repo, name=urllib.parse.quote(name))
    started = time.time()
    done = 0
    temporary = target.with_name(target.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as handle:
        total = int(response.headers.get("Content-Length") or expect_bytes or 0)
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            handle.write(chunk)
            done += len(chunk)
            if done % (32 << 20) < (1 << 20) and total:
                speed = done / max(time.time() - started, 0.01) / 1e6
                print(f"    {name}: {done/1e6:.0f}/{total/1e6:.0f} MB · {speed:.2f} MB/s", flush=True)
    temporary.replace(target)
    seconds = max(time.time() - started, 0.01)
    return done, seconds


def main():
    size = (sys.argv[1] if len(sys.argv) > 1 else "small").strip()
    repo = repo_for(size)
    target_dir = TARGET_ROOT / size
    target_dir.mkdir(parents=True, exist_ok=True)
    model_file = target_dir / "model.bin"
    if model_file.is_file() and model_file.stat().st_size > 1_000_000:
        print(f"already present: {model_file} ({model_file.stat().st_size/1e6:.1f} MB)")
        return 0

    print(f"source : modelscope {repo}")
    print(f"target : {target_dir}")
    files = list_files(repo)
    print("files  : " + ", ".join(f"{f['Name']}({f['Size']/1e6:.1f}MB)" for f in files))

    for entry in files:
        name = entry["Name"]
        if name == "model.bin" or name in WANTED:
            destination = target_dir / name
            if destination.is_file() and destination.stat().st_size == entry["Size"]:
                print(f"  skip {name} (already complete)")
                continue
            print(f"  get  {name} ({entry['Size']/1e6:.1f} MB)")
            got, seconds = download(repo, name, destination, entry["Size"])
            print(f"  done {name}: {got/1e6:.1f} MB in {seconds:.1f}s "
                  f"({got/1e6/max(seconds,0.01):.2f} MB/s)")
    free = shutil.disk_usage(target_dir).free
    print(f"free space after download: {free/1e9:.1f} GB")
    print("OK: model ready at", target_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
