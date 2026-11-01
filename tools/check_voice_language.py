"""Does the voice prompt actually hold the reply in Chinese when the history is already English?

The claim under test: with the language pinned in `voice_mode.SYSTEM_PROMPT`, a Chinese turn still
gets a Chinese reply even when the replayed history contains an English answer - and with the old
prompt it does not.

That history is the whole point. `answer_stream` replays the last eight turns verbatim, so one
English reply used to be enough to hold the conversation in English. The check runs the same
two-turn history twice, once per prompt, and counts each reply's language.

Timing a prompt change is a real generation, so `--warm` first brings the shared backend up the way
the app does (POST /local/voice/mode for the selected profile) and releases it afterwards, leaving
the card as it was found. The two completions themselves are short and capped at 320 tokens.

Usage:
    python tools/check_voice_language.py [--warm] [--port 8082] [--keep]
"""

import argparse
import http.cookiejar
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GATEWAY = "http://127.0.0.1:8081"

OLD_PROMPT = "你是一个语音助手。回答要简洁口语化，控制在两三句话以内，适合朗读。"

# One English answer already in the conversation - the condition that used to be sticky.
HISTORY = [
    {"role": "user", "content": "你都能做什么"},
    {"role": "assistant", "content": "Sure! I can chat with you, answer questions, and help you "
                                     "get things done. What would you like to do?"},
]
ASK = "那先陪我聊会儿天吧"

CJK = re.compile(r"[\u3400-\u9fff]")
LATIN_WORD = re.compile(r"[A-Za-z]{3,}")


def owner_opener():
    token = (ROOT / "logs" / "admin.token").read_text(encoding="utf-8").strip()
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.open(f"{GATEWAY}/?admin={urllib.parse.quote(token)}", timeout=30).read()
    return opener


def post_json(opener, path, body, timeout=600):
    request = urllib.request.Request(
        f"{GATEWAY}{path}", data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_json(opener, path, timeout=60):
    with opener.open(f"{GATEWAY}{path}", timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def ask(port, system_prompt, max_tokens):
    messages = [{"role": "system", "content": system_prompt}] + HISTORY + [
        {"role": "user", "content": ASK}]
    payload = json.dumps({
        "messages": messages, "max_tokens": max_tokens, "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions", data=payload,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=300) as response:
        body = json.loads(response.read().decode("utf-8"))
    message = body["choices"][0]["message"]
    return (message.get("content") or message.get("reasoning_content") or "").strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8082)
    parser.add_argument("--warm", action="store_true",
                        help="bring the shared backend up first (the app's own route)")
    parser.add_argument("--keep", action="store_true",
                        help="do not release voice mode afterwards")
    parser.add_argument("--hold", type=float, default=0.0,
                        help="seconds to stay in voice mode after the replies, so the synthesiser "
                             "and the recogniser are both resident at once and the provider the "
                             "engine chose can be read")
    parser.add_argument("--max-tokens", type=int, default=200)
    parser.add_argument("--chat-only", action="store_true",
                        help="ask one voice turn WITHOUT calling the mode route first - the sequence "
                             "a panel produces when it still believes the previous gateway is ready, "
                             "which is what used to start the ternary 27B and time out")
    args = parser.parse_args()

    sys.path.insert(0, str(ROOT))
    from voice_mode import SYSTEM_PROMPT as NEW_PROMPT

    opener = None
    if args.chat_only:
        opener = owner_opener()
        config = get_json(opener, "/local/voice/config")
        print(f"chat-only: NO mode call · selected profile = {config.get('profile')}")
        started = time.monotonic()
        try:
            result = post_json(opener, "/local/voice/chat",
                               {"text": ASK, "stream": False, "conversation_id": ""}, timeout=600)
            reply = result.get("reply") or result.get("text") or ""
            print(f"  HTTP 200 after {time.monotonic() - started:.1f}s")
            print(f"  reply          : {str(reply)[:140]}")
            print(f"  conversation_id: {result.get('conversation_id')}")
            print(f"  response keys  : {sorted(result.keys())}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            print(f"  HTTP {exc.code} after {time.monotonic() - started:.1f}s: {body[:220]}")
        if not args.keep:
            try:
                post_json(opener, "/local/voice/mode", {"profile": ""})
                print("  voice mode released")
            except Exception as exc:                   # noqa: BLE001
                print(f"  release failed: {type(exc).__name__}: {exc}")
        return 0

    if args.warm:
        opener = owner_opener()
        config = get_json(opener, "/local/voice/config")
        profile = config.get("profile") or ""
        print(f"warming the backend · profile={profile} supported={config.get('supported')}")
        started = time.monotonic()
        result = post_json(opener, "/local/voice/mode", {"profile": profile, "need": "text"})
        print("  " + json.dumps({k: result.get(k) for k in
                                 ("started", "brain_ready", "asr_ready", "mode", "context_size")},
                                ensure_ascii=False))
        # health on the brain port is what "the model is actually serving" means
        deadline = time.monotonic() + 240
        ready = False
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/health", timeout=5):
                    ready = True
                    break
            except Exception:                          # noqa: BLE001
                time.sleep(2)
        print(f"  backend {'ready' if ready else 'NOT ready'} after {time.monotonic() - started:.1f}s")
        if not ready:
            return 1

    try:
        results = {}
        for label, prompt in (("old prompt", OLD_PROMPT), ("new prompt", NEW_PROMPT)):
            try:
                reply = ask(args.port, prompt, args.max_tokens)
            except Exception as exc:                   # noqa: BLE001
                print(f"{label}: request failed - {type(exc).__name__}: {exc}")
                continue
            cjk = len(CJK.findall(reply))
            words = LATIN_WORD.findall(reply)
            held = cjk >= 10 and not words
            results[label] = held
            print(f"\n=== {label} ===")
            print(f"  reply        : {reply[:160]}")
            print(f"  chinese chars: {cjk}    english words: {len(words)} {words[:6]}")
            print(f"  stayed Chinese: {'PASS' if held else 'FAIL'}")
        if results:
            print("\n================ verdict ================")
            print(f"  old prompt held Chinese : {'yes' if results.get('old prompt') else 'no'}")
            print(f"  new prompt held Chinese : {'yes' if results.get('new prompt') else 'no'}")
        if args.hold:
            print(f"\nholding voice mode for {args.hold:.0f}s so both models are resident ...")
            time.sleep(args.hold)
    finally:
        if opener is not None and not args.keep:
            try:
                post_json(opener, "/local/voice/mode", {"profile": ""})
                print("\nvoice mode released (card handed back)")
            except Exception as exc:                   # noqa: BLE001
                print(f"\nrelease failed: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
