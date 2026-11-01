"""Both slices must work on the unified backend: text, and real vision on an image.

A switch that is instant because the capability quietly went missing would be worse than
the slow one it replaced, so this drives a real text turn and a real image turn through
the running gateway.
"""
import base64
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
import http.cookiejar

BASE = "http://127.0.0.1:8081"
ROOT = Path(__file__).resolve().parent
IMAGE = ROOT.parent / "deployment" / "qwen-vision-test.png"
results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'} {label:46s} {detail}")


def opener():
    link = (ROOT / "logs" / "admin-link.txt").read_text(encoding="ascii").strip()
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.open(link, timeout=25).read()
    return op


def call(op, path, body, timeout=300):
    request = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with op.open(request, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except ValueError:
            return exc.code, {}


def switch(op, mode):
    started = time.time()
    status, payload = call(op, "/local/mode", {"mode": mode}, timeout=60)
    # The POST can answer while the gateway is still flipping the slice flag, and a turn
    # sent in that window is judged against the old mode. Wait for "ready" the way the
    # panel does.
    deadline = time.time() + 60
    while time.time() < deadline:
        request = urllib.request.Request(BASE + "/local/status")
        try:
            with op.open(request, timeout=20) as response:
                state = json.loads(response.read())
            if state.get("state") == "ready" and state.get("mode") == mode:
                break
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.05)
    return status, round(time.time() - started, 2), payload


def ask_text(op):
    status, payload = call(op, "/v1/chat/completions", {
        "messages": [{"role": "user", "content": "只回复两个字：收到"}],
        "max_tokens": 16, "stream": False,
    })
    content = ""
    if status == 200:
        content = (payload.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return status, content.strip()


def ask_vision(op):
    encoded = base64.b64encode(IMAGE.read_bytes()).decode()
    status, payload = call(op, "/v1/chat/completions", {
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "这张图里有什么？用一句话回答。"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}},
        ]}],
        "max_tokens": 60, "stream": False,
    })
    content = ""
    if status == 200:
        content = (payload.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return status, content.strip()


def main():
    op = opener()
    if not IMAGE.is_file():
        print("no test image at", IMAGE)
        return 1

    status, seconds, _ = switch(op, "text")
    check("switch to text is immediate", seconds < 1.5, f"{seconds}s (HTTP {status})")
    status, answer = ask_text(op)
    check("a text turn works on the unified backend", status == 200 and bool(answer),
          f"HTTP {status} · {answer[:40]!r}")

    status, seconds, _ = switch(op, "vision")
    check("switch to vision is immediate", seconds < 1.5, f"{seconds}s (HTTP {status})")
    started = time.time()
    status, answer = ask_vision(op)
    check("an image turn works on the unified backend", status == 200 and bool(answer),
          f"HTTP {status} · {round(time.time() - started, 1)}s · {answer[:60]!r}")

    status, seconds, _ = switch(op, "text")
    check("switching back is immediate too", seconds < 1.5, f"{seconds}s")
    status, answer = ask_text(op)
    check("text still works after a vision turn", status == 200 and bool(answer), answer[:40])
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
