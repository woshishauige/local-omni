"""切换模式不互传数据的线上验收：同一个应用会话标识，文字那条线不能出现在识图里。

It drives the gateway the way the packaged app does — one conversation identifier, the whole
thread in every request — and asks a question the answer to which can only come from the other
mode's turns:

  1. 纯文本：让模型记住一个暗号（这一轮记进 `text·…`）
  2. 纯文本：追问暗号 —— 应当答得出（同一条线内正常）
  3. 切到识图，带上整串历史再问同一个暗号 —— 应当答不出（那一串属于文字那条线）

    & ..\\ComfyUI-BSAI_v010\\python\\python.exe -X utf8 check_mode_isolation_live.py
"""

import json
import os
from pathlib import Path
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8081"
# 每次跑用一条全新的会话与一个全新的暗号：文本匹配的隔离有条固有边界（两句话字面完全相同就分不出
# 哪一轮属于哪个模式），旧会话里记过同一句会让这条验收出现假象。用随机值，验收说的是"这一轮的
# 隔离是否生效"，而不是"上一轮留下的记录"。
RUN = f"{int(time.time())}-{os.getpid() % 10000}"
CONVERSATION = f"isolation-live-{RUN}"
TOKEN = f"紫色河马 {random.randint(100000, 999999)}"
# 问题本身也带一个一次性编号，理由同上：**字面完全相同**的两句分不出属于哪个模式，反复用同一句
# 验收时，之前那条会话会"许可"这一句，看起来像隔离失效。带编号后，这条验收测的才是它要测的东西。
FOLLOW_UP = f"我刚才让你记住的暗号是什么？（第 {RUN} 次询问）如果不知道就回答“不知道”。"
CHECKS = []


def check(label, condition, detail=""):
    CHECKS.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'} {label:<44} {detail}")
    return bool(condition)


def owner_opener():
    link = (ROOT / "logs" / "admin-link.txt").read_text(encoding="ascii").strip()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    opener.open(link)
    return opener


def call(opener, path, method="GET", body=None, timeout=300):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers={"content-type": "application/json"} if data else {})
    try:
        response = opener.open(request, timeout=timeout)
        return response.status, json.loads(response.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as failure:
        raw = failure.read().decode("utf-8", "replace")
        try:
            return failure.code, json.loads(raw or "{}")
        except ValueError:
            return failure.code, {"raw": raw}


def set_mode(opener, mode):
    call(opener, "/local/mode", "POST", {"mode": mode})
    for _ in range(90):
        _, status = call(opener, "/local/status")
        if status.get("state") == "ready" and status.get("mode") == mode:
            return True
        time.sleep(2)
    return False


def ask(opener, messages):
    """One chat turn exactly as the app sends it: the whole thread plus its identifier."""
    payload = {"model": "local", "messages": messages, "temperature": 0, "max_tokens": 60,
               "stream": False}
    data = json.dumps(payload).encode()
    request = urllib.request.Request(BASE + "/v1/chat/completions", data=data, method="POST",
                                     headers={"content-type": "application/json",
                                              "x-conversation-id": CONVERSATION})
    started = time.time()
    with opener.open(request, timeout=300) as response:
        body = json.loads(response.read().decode("utf-8", "replace"))
    reply = body.get("choices", [{}])[0].get("message", {}).get("content", "")
    print(f"      ({time.time() - started:.1f}s) {reply.strip().splitlines()[0][:70] if reply else '(空)'}")
    return reply or ""


def filed(opener, scope, minimum=1, patience=20):
    """Read one line, giving the ledger its second to catch up.

    记账发生在响应发完之后（`observe_generation` 在中间件的收尾阶段写），实测落地时间
    ≈1.0 秒：识图那一轮在 0.01 秒时两条线都还是 0 轮，1.03 秒时识图线 2 轮。立刻读会
    稳定地误判成"没记账"，所以这里等它一下——等的是同一件事，不是放宽判据。
    """
    deadline = time.time() + patience
    payload = {}
    while True:
        payload = call(opener, f"/local/conversation?conversation_id={CONVERSATION}"
                               f"&scope={scope}")[1]
        if len(payload.get("history") or []) >= minimum or time.time() >= deadline:
            return payload
        time.sleep(0.5)


def main():
    opener = owner_opener()
    check("回到纯文本模式", set_mode(opener, "text"))
    # 1–2) 文字那条线：让它记住暗号，再问一次，应当答得出。
    history = [{"role": "user", "content": f"请记住这个暗号：{TOKEN}。只回答“记住了”。"}]
    remembered = ask(opener, history)
    history.append({"role": "assistant", "content": remembered})
    history.append({"role": "user", "content": FOLLOW_UP})
    recalled = ask(opener, history)
    check("同一条线里问得出来（对照组）", TOKEN.split()[-1] in recalled or "河马" in recalled,
          recalled.strip()[:40])

    typed = filed(opener, "text", minimum=2)
    check("这一轮记在文本那条线上", any("暗号" in (turn.get("content") or "")
                                        for turn in typed.get("history", [])),
          f"{len(typed.get('history') or [])} 轮")

    # 3) 切到识图，带同一串历史再问：那串属于文字那条线，不该进来。
    check("切到识图模式", set_mode(opener, "vision"))
    history.append({"role": "assistant", "content": recalled})
    history.append({"role": "user", "content": FOLLOW_UP})
    after_switch = ask(opener, history)
    knows = (TOKEN.split()[-1] in after_switch) or ("河马" in after_switch)
    check("识图那条线看不到文字的历史（不互传）", not knows, after_switch.strip()[:40])

    scene = filed(opener, "vision", minimum=2)
    # The vision thread has its own turn pair (the question asked in vision mode and its answer)
    # and nothing else: the passphrase was introduced in the typed thread, so it must not appear
    # here at all — not in a question, not in an answer.
    history_text = " ".join((turn.get("content") or "") for turn in scene.get("history", []))
    check("识图那条线只记自己这一轮（不含文字的暗号）",
          bool(scene.get("history")) and TOKEN.split()[-1] not in history_text,
          f"{len(scene.get('history') or [])} 轮")
    listings = call(opener, "/local/conversation?scope=text")[1].get("conversations", [])
    check("两条线在列表面里也是分开的",
          any(item["id"].startswith("text·") for item in listings),
          f"{len(listings)} 条文本线")

    set_mode(opener, "text")
    print(f"\n{sum(CHECKS)}/{len(CHECKS)} passed")
    return 0 if all(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
