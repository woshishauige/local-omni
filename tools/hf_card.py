"""Fetch model cards for a list of HuggingFace repositories.

Useful when the web tools are rate-limited: this machine reaches huggingface.co, so the licence,
the parameter count, the tags and the opening of the README come from the repository itself.

Usage:
    python tools/hf_card.py FireRedTeam/FireRedAudio tencent/AuK tencent/AuK-Flash
"""

import json
import re
import ssl
import sys
import urllib.request

API = "https://huggingface.co/api/models/"
RAW = "https://huggingface.co/{}/raw/main/README.md"
HEAD_CHARS = 2200


def get(url):
    request = urllib.request.Request(url, headers={"User-Agent": "qwen-local-survey/1.0"})
    with urllib.request.urlopen(request, timeout=40, context=ssl.create_default_context()) as response:
        return response.read().decode("utf-8", "replace")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    for repo in sys.argv[1:]:
        print(f"\n{'=' * 78}\n=== {repo}\n{'=' * 78}")
        try:
            info = json.loads(get(API + repo + "?blobs=true"))
        except Exception as exc:                      # noqa: BLE001
            print(f"  api failed: {type(exc).__name__}: {exc}")
            continue
        safetensors = info.get("safetensors") or {}
        total = safetensors.get("total")
        print(f"  downloads {info.get('downloads', 0):,}   likes {info.get('likes', 0)}   "
              f"last modified {str(info.get('lastModified', ''))[:10]}")
        if total:
            print(f"  parameters: {total:,}  ({total / 1e9:.2f} B)  "
                  f"dtypes {list((safetensors.get('parameters') or {}).keys())[:4]}")
        card = info.get("cardData") or {}
        if card.get("license"):
            print(f"  license: {card['license']}")
        if card.get("pipeline_tag"):
            print(f"  pipeline: {card['pipeline_tag']}")
        tags = [t for t in (info.get("tags") or []) if not t.startswith(("license:", "region:", "autotrain"))]
        print(f"  tags: {', '.join(tags[:16])}")

        # What has to be downloaded decides whether a variant is deployable here: a GGUF repo that
        # ships only the language model has no audio encoder, and one that ships an encoder still
        # needs a runtime able to use it.
        files = info.get("siblings") or []
        if files:
            total_bytes = 0
            shown = []
            for entry in files:
                name = entry.get("rfilename", "")
                size = entry.get("size")
                if size:
                    total_bytes += size
                if size and name.endswith((".gguf", ".safetensors", ".onnx", ".bin", ".pt")):
                    shown.append((size, name))
            shown.sort(reverse=True)
            print(f"  files: {len(files)} listed, {total_bytes / 1e9:.2f} GB if every size is real")
            for size, name in shown[:14]:
                print(f"    {size / 1e6:>10.1f} MB  {name}")

        try:
            readme = get(RAW.format(repo))
        except Exception as exc:                      # noqa: BLE001
            print(f"  readme failed: {type(exc).__name__}: {exc}")
            continue
        body = re.sub(r"^---.*?---\s*", "", readme, count=1, flags=re.S)
        body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
        body = re.sub(r"<[^>]+>", "", body)
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
        print("  --- readme ---")
        for line in body[:HEAD_CHARS].splitlines():
            if line.strip():
                print("  " + line[:150])
    return 0


if __name__ == "__main__":
    sys.exit(main())
