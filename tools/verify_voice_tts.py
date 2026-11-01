"""Verify speech synthesis through the running gateway, with the voice pairing live.

Owns the same cookie exchange the browser uses, so this exercises the real route and
the real worker rather than the module in isolation.
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


def main():
    print("--- owner 会话 ---")
    print("   ", call(f"/?admin={TOKEN}")[0])

    print("--- 启动语音模式（9B + ASR）---")
    started = time.time()
    status, result = call("/local/voice/mode", {"profile": "qwen3.5-9b-uncensored"})
    print(f"    HTTP {status}  {time.time()-started:.0f}s  "
          f"brain={result.get('brain_ready') if isinstance(result, dict) else result} "
          f"asr={result.get('asr_ready') if isinstance(result, dict) else ''}")

    print("--- 音色清单 ---")
    status, voices = call("/local/voice/voices")
    print(f"    HTTP {status}  ready={voices.get('ready') if isinstance(voices, dict) else voices}")

    print("--- TTS 合成（这是之前失败的一环）---")
    for speaker, text in (("uncle_fu", "你好，我是本地部署的语音助手。"),
                          ("eric", "这句用的是四川方言音色。")):
        started = time.time()
        status, audio = call("/local/voice/speak", {"text": text, "speaker": speaker,
                                                   "language": "chinese"}, raw=True)
        elapsed = time.time() - started
        if status == 200 and isinstance(audio, (bytes, bytearray)) and len(audio) > 1000:
            out = rf"D:\AI\Qwen-Local\logs\tts-out\gateway-{speaker}.wav"
            with open(out, "wb") as handle:
                handle.write(audio)
            print(f"    {speaker:10} HTTP {status}  {len(audio)/1024:.1f} KB  {elapsed:.1f}s  -> {out}")
        else:
            detail = audio[:160] if isinstance(audio, (bytes, bytearray)) else audio
            print(f"    {speaker:10} HTTP {status}  {detail}")

    print("--- 语音对话（文字输入路径）---")
    status, reply = call("/local/voice/chat", {"text": "用一句话回答：你好",
                                              "profile": "qwen3.5-9b-uncensored"})
    if isinstance(reply, dict):
        print(f"    HTTP {status}  回答: {reply.get('reply')}")
        print(f"    性能: {reply.get('stats')}")
    else:
        print(f"    HTTP {status}  {reply}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
