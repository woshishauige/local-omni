"""Pull just the speaker encoder out of the Base checkpoint, without the 3.4 GB around it.

The Base repo ships one unsharded model.safetensors and there is no index to tell us which
part is which - but a safetensors file is a header of byte offsets followed by the tensors,
so the offsets can be read first and only the wanted bytes fetched. The speaker encoder is a
small mel network, so this turns a multi-gigabyte download into a few megabytes.

Writes models/tts/Qwen3-TTS-12Hz-1.7B-Base-partial/speaker_encoder.safetensors, reusing the
same header layout, so the result is a normal safetensors file that any loader can read.

Usage:
    python tools/fetch_speaker_encoder.py --inspect
    python tools/fetch_speaker_encoder.py            # download the speaker tensors
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import urllib.request
from pathlib import Path

REPO = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
FILE = "model.safetensors"
OUT = Path(r"D:\AI\Qwen-Local\models\tts\Qwen3-TTS-12Hz-1.7B-Base-partial")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Qwen-Local-SpeakerFetch/1.0"


def url() -> str:
    return "https://huggingface.co/%s/resolve/main/%s" % (REPO, FILE)


def ranged(start: int, end: int) -> bytes:
    """Inclusive byte range."""
    request = urllib.request.Request(url(), headers={
        "User-Agent": UA,
        "Range": "bytes=%d-%d" % (start, end),
    })
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def read_header():
    length = struct.unpack("<Q", ranged(0, 7))[0]
    if length <= 0 or length > 200 * 1024 * 1024:
        raise RuntimeError("implausible header length: %d" % length)
    header = json.loads(ranged(8, 8 + length - 1).decode("utf-8"))
    return length, header


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--prefix", default="speaker_encoder")
    args = parser.parse_args()

    print("reading the safetensors header from %s ..." % REPO)
    header_len, header = read_header()
    tensors = {k: v for k, v in header.items() if k != "__metadata__"}
    print("header %d bytes, %d tensors, %d bytes of tensor data"
          % (header_len, len(tensors),
             max((v["data_offsets"][1] for v in tensors.values()), default=0)))

    wanted = {k: v for k, v in tensors.items() if k.startswith(args.prefix)}
    if not wanted:
        print("\nno tensors start with %r. Names that mention 'speaker':" % args.prefix)
        for key in sorted(tensors):
            if "speaker" in key.lower():
                print("   " + key)
        print("\nfirst 25 tensor names for reference:")
        for key in sorted(tensors)[:25]:
            print("   " + key)
        return 1

    total = sum(v["data_offsets"][1] - v["data_offsets"][0] for v in wanted.values())
    print("\n%d tensor(s) under %r, %.1f MB of data:" % (len(wanted), args.prefix, total / 1e6))
    for key in sorted(wanted):
        info = wanted[key]
        size = info["data_offsets"][1] - info["data_offsets"][0]
        print("   %-52s %-8s %-18s %7.2f MB"
              % (key, info["dtype"], info["shape"], size / 1e6))

    if args.inspect:
        print("\n--inspect given; nothing downloaded.")
        return 0

    OUT.mkdir(parents=True, exist_ok=True)
    payload = bytearray()
    offsets = {}
    for key in sorted(wanted):
        info = wanted[key]
        start, end = info["data_offsets"]
        print("fetching %s (%.2f MB) ..." % (key, (end - start) / 1e6))
        chunk = ranged(8 + header_len + start, 8 + header_len + end - 1)
        if len(chunk) != end - start:
            raise RuntimeError("%s: got %d bytes, expected %d" % (key, len(chunk), end - start))
        offsets[key] = [len(payload), len(payload) + len(chunk)]
        payload.extend(chunk)

    new_header = {}
    for key in sorted(wanted):
        info = wanted[key]
        new_header[key] = {"dtype": info["dtype"], "shape": info["shape"],
                           "data_offsets": offsets[key]}
    blob = json.dumps(new_header, separators=(",", ":")).encode("utf-8")
    target = OUT / "speaker_encoder.safetensors"
    with open(target, "wb") as handle:
        handle.write(struct.pack("<Q", len(blob)))
        handle.write(blob)
        handle.write(bytes(payload))
    print("\nwrote %s (%.2f MB)" % (target, target.stat().st_size / 1e6))
    return 0


if __name__ == "__main__":
    sys.exit(main())
