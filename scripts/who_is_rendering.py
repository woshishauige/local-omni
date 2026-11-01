"""Who queued those 5 H3 renders? Read the gateway's own view of image/video work.

    python -X utf8 who_is_rendering.py

Prints the gateway's video + image progress as the owner, plus the ComfyUI queue head.
Read-only: no generation, no cancellation.
"""
import http.cookiejar
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8081"
ROOT = Path(__file__).resolve().parents[1]


def owner():
    link = (ROOT / "logs" / "admin-link.txt").read_text(encoding="ascii").strip()
    jar = http.cookiejar.CookieJar()
    machine = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    machine.open(link, timeout=30).read()
    return machine


def get(machine, path):
    try:
        with machine.open(BASE + path, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:200].decode("utf-8", "replace")


def main() -> int:
    machine = owner()
    for path in ("/local/video/progress", "/local/image/progress", "/local/status"):
        status, payload = get(machine, path)
        print(f"--- {path}  HTTP {status}")
        if isinstance(payload, dict):
            keep = {key: value for key, value in payload.items()
                    if key in ("state", "busy", "job", "queue_ahead", "mode", "detail", "shot",
                               "phase", "started", "session", "prompt", "error", "message")}
            text = json.dumps(keep, ensure_ascii=False)
            print(text[:700])
        else:
            print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
