"""Ask HuggingFace directly which open speech models exist and which are actually used.

The agent-side web tools were rate-limited (403) and direct fetch is unavailable, but this machine
has working internet - it downloaded every model in D:\\AI\\Qwen-Local over it. So the catalogue
questions are answered from the source instead of from an article about it:

  1. does Qwen publish any Qwen-Audio-3.1 weights at all
  2. which open ASR models people actually download
  3. which open TTS models people actually download
  4. which open audio-language / speech-to-speech models people actually download

Downloads are a blunt instrument - they measure usage, not quality - so this is a shortlist to
argue about, not a verdict. Nothing here is downloaded.

Usage: python tools/hf_survey.py
"""

import json
import ssl
import sys
import urllib.parse
import urllib.request

API = "https://huggingface.co/api/models"
TIMEOUT = 30

QUERIES = [
    ("Qwen org, audio in the name",
     {"author": "Qwen", "search": "audio", "limit": "100"}),
    ("Qwen org, omni in the name",
     {"author": "Qwen", "search": "omni", "limit": "100"}),
    ("open ASR, most downloaded",
     {"filter": "automatic-speech-recognition", "sort": "downloads", "direction": "-1", "limit": "20"}),
    ("open TTS, most downloaded",
     {"filter": "text-to-speech", "sort": "downloads", "direction": "-1", "limit": "20"}),
    ("audio -> text (audio LLM), most downloaded",
     {"filter": "audio-text-to-text", "sort": "downloads", "direction": "-1", "limit": "20"}),
    ("any pipeline, 'speech-to-speech' in the name, most downloaded",
     {"search": "speech-to-speech", "sort": "downloads", "direction": "-1", "limit": "15"}),
]


def fetch(params):
    url = API + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "qwen-local-survey/1.0"})
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=TIMEOUT, context=context) as response:
        return json.loads(response.read().decode("utf-8"))


def main():
    for title, params in QUERIES:
        print(f"\n=== {title} ===")
        try:
            rows = fetch(params)
        except Exception as exc:                      # noqa: BLE001 - report and continue
            print(f"  query failed: {type(exc).__name__}: {exc}")
            continue
        if not rows:
            print("  (nothing)")
            continue
        for item in rows:
            print(f"  {item.get('downloads', 0):>10,} dl  {item.get('likes', 0):>5} likes  "
                  f"{str(item.get('lastModified', ''))[:10]}  {item.get('id')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
