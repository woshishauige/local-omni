"""Synthesise one sentence through the gateway, without a browser.

Every browser-based check in this repository ends by closing its page, and the gateway treats
"page closed and no task is running" as a reason to shut the whole stack down - so a browser check
cannot be used to confirm a fix and leave the service running. This talks to the gateway directly
instead: the owner link establishes the session cookie, and the synthesis endpoint is called with
a plain HTTP client.

Usage: python tools/speak_once.py ["text to say"] [--speaker uncle_fu] [--instruct "..."]
"""

import argparse
import http.cookiejar
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "tools" / "bench-out" / "humanize"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("text", nargs="?", default="你好，这是一次本机合成校验。")
    parser.add_argument("--speaker", default="uncle_fu")
    parser.add_argument("--instruct", default="")
    parser.add_argument("--save", default="")
    args = parser.parse_args()

    token = (ROOT / "logs" / "admin.token").read_text(encoding="utf-8").strip()
    base = "http://127.0.0.1:8081"
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    # The owner link is what grants the session; without it every /local/ call answers 401.
    opener.open(f"{base}/?admin={urllib.parse.quote(token)}", timeout=30).read()
    print(f"session cookies: {[c.name for c in jar]}")

    body = {"text": args.text, "speaker": args.speaker, "language": "chinese"}
    if args.instruct:
        body["instruct"] = args.instruct
    request = urllib.request.Request(
        f"{base}/local/voice/speak",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener.open(request, timeout=900) as response:
            payload = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}")
        return 1

    print(f"HTTP {status}  {len(payload):,} bytes  speaker={args.speaker} instruct={args.instruct or '(none)'}")
    if len(payload) < 512:
        print("that is too small to be audio")
        return 1
    if args.save:
        OUT.mkdir(parents=True, exist_ok=True)
        target = OUT / args.save
        target.write_bytes(payload)
        print(f"saved {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
