"""Ask HuggingFace for repositories matching arbitrary queries.

The agent-side web tools are rate-limited from this network often enough that it is worth having
the local route to hand: this machine reaches huggingface.co, so names, sizes, download counts and
last-modified dates come from the catalogue itself rather than from an article about it.

Usage:
    python tools/hf_search.py "search=firered" "search=auk&author=tencent" "author=tencent&search=audio"
"""

import json
import ssl
import sys
import urllib.parse
import urllib.request

API = "https://huggingface.co/api/models"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    for query in sys.argv[1:]:
        params = dict(urllib.parse.parse_qsl(query))
        params.setdefault("limit", "40")
        params.setdefault("sort", "downloads")
        params.setdefault("direction", "-1")
        url = API + "?" + urllib.parse.urlencode(params)
        print(f"\n=== {query} ===")
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "qwen-local-survey/1.0"})
            with urllib.request.urlopen(request, timeout=40, context=ssl.create_default_context()) as response:
                rows = json.loads(response.read().decode("utf-8"))
        except Exception as exc:                      # noqa: BLE001 - report and continue
            print(f"  query failed: {type(exc).__name__}: {exc}")
            continue
        if not rows:
            print("  (nothing)")
            continue
        for item in rows:
            print(f"  {item.get('downloads', 0):>9,} dl  {item.get('likes', 0):>4} likes  "
                  f"{str(item.get('lastModified', ''))[:10]}  {item.get('id')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
