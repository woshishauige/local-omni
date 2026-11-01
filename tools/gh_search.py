"""Search GitHub repositories by keyword.

Needed whenever the question is "does this runtime/project exist and what does it support" and the
agent-side web tools are rate-limited from this network. This machine reaches api.github.com, so
names, stars, languages and descriptions come from the source.

Usage:
    python tools/gh_search.py "audio.cpp gguf" "qwen audio agent" ["more queries"...]
"""

import json
import ssl
import sys
import urllib.parse
import urllib.request

API = "https://api.github.com/search/repositories"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    for query in sys.argv[1:]:
        params = {"q": query, "sort": "stars", "order": "desc", "per_page": "12"}
        url = API + "?" + urllib.parse.urlencode(params)
        print(f"\n=== {query} ===")
        try:
            request = urllib.request.Request(url, headers={
                "User-Agent": "qwen-local-survey/1.0",
                "Accept": "application/vnd.github+json",
            })
            with urllib.request.urlopen(request, timeout=40, context=ssl.create_default_context()) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:                      # noqa: BLE001 - report and continue
            print(f"  query failed: {type(exc).__name__}: {exc}")
            continue
        items = payload.get("items") or []
        if not items:
            print("  (nothing)")
            continue
        for item in items:
            description = (item.get("description") or "").replace("\n", " ")[:110]
            print(f"  {item.get('stargazers_count', 0):>7}★  {str(item.get('pushed_at', ''))[:10]}  "
                  f"{item.get('full_name')}")
            if description:
                print(f"           {description}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
