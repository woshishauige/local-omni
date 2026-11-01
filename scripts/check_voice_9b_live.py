"""真机验收：**9B 档位下的语音对话**（所有者报的故障）。

在这之前 `voice_mode` 把语音写死成三元 27B，选中 9B 时语音标签变灰、路由 409。现在语音跟当前
档位走，这个脚本以 9B 为例把整条路走一遍：切到 9B → 读规则 → 起语音后端 → 真发一句话拿回复 →
退出语音模式（把文字窗口还回去）。

代价：两到三次**同权重重载**（语音窗口 40960 ≠ 文字窗口，语音模式会按窗口重载同一套权重），
加一次十几 token 的生成；不排任何生图/视频任务。

    python -X utf8 check_voice_9b_live.py
"""
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST = "http://127.0.0.1:8081"
PROFILE = "qwen3.5-9b-uncensored"


def owner():
    link = (ROOT / "logs" / "admin-link.txt").read_text(encoding="ascii").strip()
    jar = http.cookiejar.CookieJar()
    machine = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    machine.open(link, timeout=30).read()
    return machine


def call(machine, path, method="GET", body=None, timeout=300):
    data = None if method == "GET" else json.dumps(body or {}).encode()
    request = urllib.request.Request(HOST + path, data=data, method=method,
                                     headers={"content-type": "application/json"} if data else {})
    try:
        with machine.open(request, timeout=timeout) as response:
            payload = response.read()
            return response.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as failure:
        raw = failure.read()[:300].decode("utf-8", "replace")
        try:
            return failure.code, json.loads(raw)
        except ValueError:
            return failure.code, raw


def wait_ready(machine, timeout=240):
    deadline = time.monotonic() + timeout
    state = {}
    while time.monotonic() < deadline:
        _, state = call(machine, "/local/status")
        if isinstance(state, dict) and state.get("state") == "ready":
            return state
        time.sleep(3)
    return state


def main() -> int:
    machine = owner()
    failures = []

    status, state = call(machine, "/local/status")
    selected = state.get("model_profile")
    print(f"[0] 当前选中档位：{selected}（状态 {state.get('state')}）")
    if selected != PROFILE:
        print(f"[1] 切到 9B …")
        code, body = call(machine, "/local/model", "POST", {"profile": PROFILE})
        print(f"    HTTP {code} {str(body)[:90]}")
        if code not in (200, 202):
            failures.append("切不到 9B")
        state = wait_ready(machine)
        print(f"    就绪：state={state.get('state')} profile={state.get('model_profile')} "
              f"窗口={state.get('context_size')}")
        if state.get("model_profile") != PROFILE or state.get("state") != "ready":
            failures.append("切到 9B 之后没有就绪")

    code, config = call(machine, "/local/voice/config")
    print(f"[2] 语音规则：HTTP {code} profile={config.get('profile')} "
          f"supported={config.get('supported')} locked={config.get('locked')} "
          f"窗口={config.get('context_size')} reason={config.get('reason')!r}")
    if code != 200 or config.get("profile") != PROFILE:
        failures.append("语音规则没有报当前档位")
    if not config.get("supported"):
        failures.append("9B 被判成不支持语音")
    if config.get("locked"):
        failures.append("档位仍然是被锁死的（客户端就是靠 locked=false 放行语音标签）")
    if config.get("context_size") != 40960:
        failures.append(f"9B 的语音窗口应该是 40960，实际 {config.get('context_size')}")

    print("[3] 起语音后端（需要时会按语音窗口重载同一套权重，约 1 分钟）…")
    code, started = call(machine, "/local/voice/mode", "POST",
                         {"profile": PROFILE, "need": "text"}, timeout=600)
    print(f"    HTTP {code} brain_ready={started.get('brain_ready')} "
          f"profile={started.get('profile')} label={started.get('profile_label')} "
          f"shared={started.get('brain_shared')} asr={started.get('asr_ready')}")
    print(f"    切换说明：{(started.get('switch') or {}).get('reason')}")
    if code != 200 or not started.get("brain_ready"):
        failures.append(f"语音后端没起来：{started.get('error') or started}")
    if started.get("profile") != PROFILE:
        failures.append("语音报的档位不是 9B")

    code, reply = call(machine, "/local/voice/chat", "POST",
                       {"text": "只回答两个字：收到", "profile": PROFILE}, timeout=300)
    text = (reply or {}).get("reply") if isinstance(reply, dict) else ""
    print(f"[4] 真发一句话：HTTP {code} profile={reply.get('profile') if isinstance(reply, dict) else '?'} "
          f"回复={str(text)[:60]!r}")
    if code != 200 or not text:
        failures.append(f"9B 的语音回合没有回复：{str(reply)[:160]}")

    def manager_pid():
        """网关进程的 pid：离开语音模式不该把它换掉（换档被误判成卡死时它会变）。"""
        record = ROOT / "supervisor-manager.process.json"
        try:
            return json.loads(record.read_text(encoding="utf-8")).get("pid")
        except (OSError, ValueError):
            return None

    print("[5] 退出语音模式，把文字窗口还回去 …")
    pid_before = manager_pid()
    code, stopped = call(machine, "/local/voice/mode", "POST", {"profile": ""}, timeout=600)
    detail = stopped if isinstance(stopped, dict) else {"raw": str(stopped)[:120]}
    print(f"    HTTP {code} started={detail.get('started')} state={detail.get('state')} "
          f"raw={detail.get('raw', '')}")
    if code != 200:
        failures.append(f"退出语音模式失败：HTTP {code} {str(stopped)[:120]}")
    state = wait_ready(machine)
    pid_after = manager_pid()
    print(f"[6] 收尾：state={state.get('state')} profile={state.get('model_profile')} "
          f"窗口={state.get('context_size')} 网关 pid {pid_before} → {pid_after}")
    if pid_before and pid_after and pid_before != pid_after:
        failures.append("换档期间网关被重启了（探活误判成卡死）")

    print("\n结论：" + ("全部通过" if not failures else "有失败 → " + "；".join(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
