"""Measure the streaming voice turn: first token, first sentence, and the full reply.

Uses the same SSE route the panel uses, so the numbers describe what a user actually
experiences rather than what the model reports internally.
"""

import http.cookiejar
import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8081"
TOKEN = "611761-992509-889670"

opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
)


def call(path, payload=None, raw=False, timeout=600):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(BASE + path, data=data, headers=headers,
                                     method="POST" if data else "GET")
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read()
            if raw:
                return response.status, body
            text = body.decode("utf-8", "replace")
            try:
                return response.status, json.loads(text)
            except ValueError:
                return response.status, text[:120]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:200]


def stream_turn(text):
    payload = {"text": text, "history": [], "profile": "qwen3.5-9b-uncensored", "stream": True}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        BASE + "/local/voice/chat", data=data,
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    started = time.time()
    first_token = None
    first_sentence = None
    stats = None
    full = ""
    buffer = ""
    with opener.open(request, timeout=600) as response:
        event = "message"
        for raw_line in response:
            line = raw_line.decode("utf-8", "replace").rstrip("\n")
            if line.startswith("event:"):
                event = line[6:].strip()
                continue
            if not line.startswith("data:"):
                if not line.strip():
                    event = "message"
                continue
            body = line[5:].strip()
            if event == "stats":
                try:
                    stats = json.loads(body)
                except ValueError:
                    pass
                continue
            if event == "done":
                break
            if event == "error":
                print(f"    错误: {body}")
                break
            try:
                piece = json.loads(body)
            except ValueError:
                continue
            now = time.time() - started
            if first_token is None:
                first_token = now
            full += piece
            buffer += piece
            parts = [p for p in buffer.replace("。", "。\x00").split("\x00") if p.strip()]
            # A sentence is complete when a final mark arrived; the remainder stays.
            if buffer and buffer[-1] in "。！？!?；;…":
                buffer = ""
                if first_sentence is None:
                    first_sentence = now
    total = time.time() - started
    return {
        "first_token": first_token,
        "first_sentence": first_sentence,
        "total": total,
        "stats": stats,
        "reply": full.strip(),
    }


def main():
    print("--- owner 会话 ---")
    print("   ", call(f"/?admin={TOKEN}")[0])

    print("--- 确保语音后端就绪 ---")
    status, result = call("/local/voice/mode", {"profile": "qwen3.5-9b-uncensored"})
    print(f"    HTTP {status}  brain={result.get('brain_ready') if isinstance(result, dict) else result}")

    for question in ("请用一句话介绍你自己", "什么是快速排序？简单说两句"):
        print(f"\n--- 流式提问: {question} ---")
        result = stream_turn(question)
        print(f"    回答: {result['reply']}")
        print(f"    首个 token   : {result['first_token']:.2f}s" if result["first_token"] else "    首个 token: n/a")
        print(f"    首句可合成   : {result['first_sentence']:.2f}s" if result["first_sentence"] else "    首句可合成: n/a")
        print(f"    整段完成     : {result['total']:.2f}s")
        if result["stats"]:
            print(f"    服务端统计   : {result['stats']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
