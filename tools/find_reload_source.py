"""Find what makes the page reload itself.

A CDP navigation trace showed the document being replaced once in the middle of a
spoken turn, which wipes the panel and every observation collected so far. The only
self-reload in the injected scripts is guarded, so the remaining source is in the
bundled app. Minified, so this prints the neighbourhood of each occurrence and every
endpoint-looking string near it.
"""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent / "web"


def main():
    targets = list((WEB / "native").rglob("*.js")) + [p for p in WEB.glob("*.js")]
    print("scanning %d javascript files" % len(targets))
    for path in targets:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(r"location\.reload\s*\(", text):
            start = max(0, match.start() - 700)
            window = text[start:match.end() + 120]
            # Only the tail matters; minified context is dense.
            print("\n" + "=" * 76)
            print("%s  @%d" % (path.relative_to(WEB), match.start()))
            print("=" * 76)
            print(window[-820:])
            endpoints = sorted(set(re.findall(r"[\"'`](/[a-zA-Z0-9_\-/.]{2,60})[\"'`]", window)))
            if endpoints:
                print("\n  endpoints in this neighbourhood: %s" % endpoints[-14:])
            keys = sorted(set(re.findall(r"[\"'`]([A-Za-z_][A-Za-z0-9_]{3,40})[\"'`]\s*:", window)))
            if keys:
                print("  object keys here: %s" % keys[-16:])


if __name__ == "__main__":
    main()
