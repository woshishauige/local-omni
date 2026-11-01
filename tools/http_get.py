"""Fetch a URL and print it, optionally filtered.

Written because passing Python one-liners through PowerShell mangles embedded quotes, and because
"what does this project actually support" keeps needing the raw README rather than a summary.

Usage:
    python tools/http_get.py <url> [--chars N] [--grep PATTERN] [--grep PATTERN ...]
"""

import re
import ssl
import sys
import urllib.request


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    url = args[0]
    chars = 3000
    patterns = []
    index = 1
    while index < len(args):
        if args[index] == "--chars" and index + 1 < len(args):
            chars = int(args[index + 1])
            index += 2
        elif args[index] == "--grep" and index + 1 < len(args):
            patterns.append(args[index + 1])
            index += 2
        else:
            index += 1

    request = urllib.request.Request(url, headers={"User-Agent": "qwen-local-survey/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=45, context=ssl.create_default_context()) as response:
            text = response.read().decode("utf-8", "replace")
    except Exception as exc:                          # noqa: BLE001
        print(f"fetch failed: {type(exc).__name__}: {exc}")
        return 1

    print(f"# {url}  ({len(text):,} chars)")
    if patterns:
        regex = re.compile("|".join(patterns), re.IGNORECASE)
        hits = [line for line in text.splitlines() if regex.search(line)]
        print(f"# {len(hits)} matching line(s)")
        for line in hits[:120]:
            print("  " + line.strip()[:200])
    else:
        for line in text[:chars].splitlines():
            print(line[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
