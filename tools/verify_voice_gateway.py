"""Verify the voice endpoints through the running gateway.

Talks to the gateway the way the browser does, including the owner cookie exchange
(`?admin=<token>`), so this exercises the real routing, the owner check and the
voice-mode lock rather than importing the modules directly.
"""

import http.cookiejar
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8081"
# The owner token changes on every start, so it is read from the file the gateway writes
# rather than copied here: a stale literal made this tool report a login failure as a
# voice-lock failure, which is the wrong lesson to learn from a red run.
TOKEN_FILE = Path(__file__).resolve().parent.parent / "logs" / "admin-link.txt"


def owner_token():
    try:
        link = TOKEN_FILE.read_text(encoding="ascii").strip()
    except OSError:
        return ""
    from urllib.parse import urlparse, parse_qs
    return (parse_qs(urlparse(link).query).get("admin") or [""])[0]


TOKEN = owner_token()

opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
)


def call(path, payload=None, method=None, raw=False):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(BASE + path, data=data, headers=headers,
                                     method=method or ("POST" if data else "GET"))
    try:
        with opener.open(request, timeout=240) as response:
            body = response.read()
            if raw:
                return response.status, body
            text = body.decode("utf-8", "replace")
            try:
                return response.status, json.loads(text)
            except ValueError:
                # The admin exchange answers with the app's HTML shell, which is
                # success but not JSON.
                return response.status, text[:120]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:200]


def main():
    print("--- 1) 取得 owner 会话 ---")
    status, _ = call(f"/?admin={TOKEN}")
    print(f"    admin 交换 -> HTTP {status}")

    print("--- 2) 语音配置（锁定信息）---")
    status, config = call("/local/voice/config")
    print(f"    HTTP {status} -> {config}")

    print("--- 3) 语音模式状态 ---")
    status, state = call("/local/voice/mode", {"profile": ""})
    print(f"    HTTP {status} -> state={state.get('state') if isinstance(state, dict) else state}")

    print("--- 4) 启动语音模式（三元 27B + ASR）---")
    started = time.time()
    status, result = call("/local/voice/mode", {"profile": "qwen3.8-27b"})
    print(f"    HTTP {status}  用时 {time.time()-started:.0f}s")
    if isinstance(result, dict):
        print(f"    brain_ready={result.get('brain_ready')} asr_ready={result.get('asr_ready')}")
    else:
        print(f"    {result}")

    print("--- 5) 语音对话（锁定路由）---")
    status, reply = call("/local/voice/chat", {"text": "你好，请用一句话介绍你自己",
                                              "profile": "qwen3.8-27b"})
    if isinstance(reply, dict):
        print(f"    HTTP {status}")
        print(f"    回答: {reply.get('reply')}")
        print(f"    性能: {reply.get('stats')}")
    else:
        print(f"    HTTP {status} -> {reply}")

    print("--- 6) 锁死校验：尝试用 9B（已不是语音档位）---")
    status, refused = call("/local/voice/chat", {"text": "测试", "profile": "qwen3.5-9b-uncensored"})
    print(f"    HTTP {status} -> {refused}")

    print("--- 7) 锁死校验：语音模式请求 9B ---")
    status, refused2 = call("/local/voice/mode", {"profile": "qwen3.5-9b-uncensored"})
    print(f"    HTTP {status} -> {refused2}")

    print("--- 8) 音色清单 ---")
    status, voices = call("/local/voice/voices")
    if isinstance(voices, dict):
        names = [v["label"] + f"({v['kind']})" for v in voices.get("voices", [])]
        print(f"    HTTP {status} -> {len(names)} 个: {', '.join(names)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
