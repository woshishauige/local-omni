"""第 2 档实测：正在运行的网关怎么处理"打转"，以及它有没有要求模型**换方案继续**。

只付极短的生成（`max_tokens=260`，9B 常驻，几秒钟），把构造好的历史发给**真正在跑的那个网关**，
看响应头与模型的答复形态。不生成图片/视频，不碰 ComfyUI。

    python -X utf8 check_loop_budget_live.py                 # 默认：打转三次 → 第 1 级
    python -X utf8 check_loop_budget_live.py --case repeat    # 同上（显式）
    python -X utf8 check_loop_budget_live.py --case relapse   # 复发 → 第 2 级（只摘一个工具）
    python -X utf8 check_loop_budget_live.py --case budget    # 一轮 24 次 → 超预算（硬停）
    python -X utf8 check_loop_budget_live.py --calls 23       # 预算边界对照：不该触发

身份用 `logs/admin-link.txt`（网关启动时写下的机主链接），与其它 check_*_live.py 一致。
"""
import argparse
import http.cookiejar
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from app.context import loop_guard

BASE = "http://127.0.0.1:8081"
ROOT = Path(__file__).resolve().parents[1]
TOOLS = [{"type": "function", "function": {"name": name}}
         for name in ("read_file", "grep", "pwsh")]


def opener():
    link = (ROOT / "logs" / "admin-link.txt").read_text(encoding="ascii").strip()
    jar = http.cookiejar.CookieJar()
    machine = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    machine.open(link, timeout=30).read()
    return machine


def failed_read(path: str, identifier: str) -> list[dict]:
    return [{"role": "assistant", "content": None, "tool_calls": [
                {"id": identifier, "type": "function",
                 "function": {"name": "read_file", "arguments": json.dumps({"path": path})}}]},
            {"role": "tool", "tool_call_id": identifier,
             "content": f"Error: 找不到文件 {path}（No such file）"}]


def busy_history(calls: int) -> list[dict]:
    messages = [{"role": "user", "content": "把项目里每个模块都看一遍"}]
    for index in range(calls):
        name = "read_file" if index % 2 else "grep"
        messages.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{index}", "type": "function",
             "function": {"name": name, "arguments": f'{{"n":{index}}}'}}]})
        messages.append({"role": "tool", "tool_call_id": f"c{index}",
                         "content": f"第 {index} 项内容正常"})
    return messages


def loopy_history(times: int = 3) -> list[dict]:
    messages = [{"role": "user", "content": "帮我读一下 D:/docs/手册.md 再总结"}]
    for index in range(times):
        messages += failed_read("D:/docs/手册.md", f"call-{index}")
    return messages


def build(case: str, calls: int) -> tuple[list[dict], str]:
    if case == "budget":
        return busy_history(calls), f"{calls} 次工具调用（各不相同、全部成功）"
    if case == "relapse":
        messages = loopy_history()
        verdict = loop_guard.inspect(messages)
        _, note = loop_guard.break_loop({"messages": messages}, verdict)
        messages = messages + [{"role": "user", "content": note}]
        messages += failed_read("D:/docs/手册.md", "again-0")
        return messages, "打转三次 → 网关已提醒 → **又发起同一次调用**"
    return loopy_history(), "同一次读取连续失败 3 次（参数完全相同）"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", default="repeat", choices=("repeat", "relapse", "budget"))
    parser.add_argument("--calls", type=int, default=0)
    parser.add_argument("--max-tokens", type=int, default=260)
    options = parser.parse_args()
    calls = options.calls or (loop_guard.STEP_BUDGET if options.case == "budget" else 3)

    messages, describe = build(options.case, calls)
    body = {"messages": messages, "stream": False, "max_tokens": options.max_tokens,
            "temperature": 0, "tools": TOOLS}
    request = urllib.request.Request(BASE + "/v1/chat/completions",
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    machine = opener()
    try:
        response = machine.open(request, timeout=300)
        status, headers, raw = response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        status, headers, raw = exc.code, exc.headers, exc.read()

    print(f"case      : {options.case}（{describe}）")
    print(f"HTTP {status}")
    print(f"x-qwen-loop-guard        = {headers.get('x-qwen-loop-guard')}")
    print(f"x-qwen-loop-guard-reason = {headers.get('x-qwen-loop-guard-reason')}")
    print(f"x-qwen-loop-guard-level  = {headers.get('x-qwen-loop-guard-level')}")
    if status != 200:
        print(raw[:400].decode("utf-8", "replace"))
        return 1
    message = ((json.loads(raw).get("choices") or [{}])[0].get("message")) or {}
    calls_made = message.get("tool_calls") or []
    print(f"回复里的工具调用 = {len(calls_made)}")
    for call in calls_made[:3]:
        function = call.get("function") or {}
        print(f"   → {function.get('name')} {str(function.get('arguments'))[:110]}")
    text = (message.get("content") or "").strip()
    print(f"回复文字（{len(text)} 字）= {text[:300].replace(chr(10), ' ')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
