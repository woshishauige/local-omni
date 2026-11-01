"""Online acceptance for the 9B default: it loads, it is named, and voice shares it.

Run against a gateway that is already serving:

    # 以所有者身份验收（令牌取自 logs\\admin-link.txt 的 ?admin=...）
    & ..\\ComfyUI-BSAI_v010\\python\\python.exe -X utf8 accept_9b_share.py `
        --admin 9dd8022a804290f13ffede9289966c722c04ab0737f4c841

    # 只验证 1、2 两项，不碰语音
    ... accept_9b_share.py --admin <token> --skip-voice

What it proves, in order:

1. The 9B profile is the one loaded and it is healthy. The old failure mode was a
   permanent `state: error` naming the MTP check, so the assertion is on the state *and*
   on the absence of that message.
2. A chat turn with the bundled UI's own request shape (no `model` field) is filed in the
   usage log under the 9B's name. The old fallback wrote the 27B's name, which is why the
   log contained no 9B row at all.
3. Opening voice mode over that same backend starts the recogniser and *borrows* the
   brain: no second model process appears, the gateway backend is the same PID, and the
   gateway reports `brain_shared`. Closing voice mode leaves the backend alive.

Every step prints what it measured, so a failure says which claim stopped holding.
"""

import argparse
import http.cookiejar
import json
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE_9B = "qwen3.5-9b-uncensored"
PROFILE_27B = "qwen3.8-27b"
MTP_ERROR = "MTP status differs"


class Client:
    """A tiny HTTP client that keeps the owner session cookie between calls."""

    def __init__(self, base, admin_token=None):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.admin_token = admin_token

    def login(self):
        """Exchange the admin token for the session cookie the routes expect."""
        if not self.admin_token:
            return False
        url = f"{self.base}/?admin={urllib.parse.quote(self.admin_token)}"
        with self.opener.open(url, timeout=20) as response:
            response.read()
        return any(cookie.name for cookie in self.jar)

    def call(self, path, method="GET", body=None, timeout=60, headers=None):
        """Returns (status, parsed-json-or-text). Never raises on 4xx/5xx."""
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("content-type", "application/json")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
                status = response.status
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", "replace")
            status = error.code
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw


def field(payload, key, default=None):
    """Read a key off a response that may be an error page rather than JSON."""
    return payload.get(key, default) if isinstance(payload, dict) else default


def llama_servers():
    """(pid, listening port) for every llama-server on this machine."""
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe'\" | "
        "ForEach-Object { \"$($_.ProcessId) $($_.CommandLine)\" }"
    )
    completed = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script],
                               capture_output=True, text=True, timeout=60)
    found = []
    for line in completed.stdout.splitlines():
        parts = line.split(None, 1)
        if not parts or not parts[0].isdigit():
            continue
        command = parts[1] if len(parts) > 1 else ""
        port = ""
        if "--port" in command:
            tail = command.split("--port", 1)[1].split()
            port = tail[0] if tail else ""
        found.append((int(parts[0]), port))
    return found


def port_owner(port):
    """The PID listening on a local port, or None."""
    script = (
        f"(Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue | "
        "Select-Object -First 1 -ExpandProperty OwningProcess)"
    )
    completed = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script],
                               capture_output=True, text=True, timeout=60)
    text = completed.stdout.strip()
    return int(text) if text.isdigit() else None


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8081", help="gateway address")
    parser.add_argument("--admin", default="", help="owner token from logs/admin-link.txt")
    parser.add_argument("--skip-voice", action="store_true", help="do not touch voice mode")
    parser.add_argument("--check-base-switch", action="store_true",
                        help="also switch to the 27B and let voice mode switch back (slow)")
    args = parser.parse_args()

    client = Client(args.base, args.admin)
    results = []
    if args.admin:
        logged_in = client.login()
        print(f"以所有者身份登录：{'成功' if logged_in else '失败'}")
    else:
        print("未提供 --admin：只读端点可用，语音与聊天会被口令门挡下")

    print("\n1. 9B 加载与状态")
    # Establish the precondition instead of inheriting whatever the last run left behind:
    # an earlier run's restart is asynchronous, so "the service I just started" is not the
    # same thing as "the service running the default profile".
    client.call("/local/model", "POST", {"profile": PROFILE_9B}, timeout=60)

    def wait_for(profile, seconds=240):
        """Poll until the machine serves ``profile`` and is ready; returns (http, status)."""
        deadline = time.monotonic() + seconds
        latest, latest_code = {}, None
        while time.monotonic() < deadline:
            latest_code, latest = client.call("/local/status", timeout=15)
            if (field(latest, "state") == "ready"
                    and field(latest, "loaded_profile") == profile
                    and field(latest, "model_profile") == profile):
                break
            time.sleep(3)
        return latest_code, latest

    code, status = wait_for(PROFILE_9B)
    results.append(check("网关应答 /local/status", code == 200 and bool(status),
                         f"http={code} state={field(status, 'state')}"))
    results.append(check("模型已就绪", field(status, "state") == "ready",
                         str(field(status, "error") or "")))
    results.append(check("错误不是 MTP 校验（旧缺陷的原话）",
                         MTP_ERROR not in str(field(status, "error") or ""),
                         str(field(status, "error") or "")))
    results.append(check("跑的是 9B profile", field(status, "model_profile") == PROFILE_9B,
                         str(field(status, "model_profile"))))
    results.append(check("9B 按 profile 关闭 MTP", field(status, "mtp_enabled") is False,
                         f"mtp_enabled={field(status, 'mtp_enabled')}"))
    results.append(check("上下文/层数取自 profile",
                         field(status, "context_size") == 32768 and field(status, "gpu_layers") == 64,
                         f"ctx={field(status, 'context_size')} ngl={field(status, 'gpu_layers')}"))

    print("\n2. 用量日志记的是真实模型")
    usage = ROOT / "logs" / "usage.sqlite3"
    before = None
    if usage.is_file():
        connection = sqlite3.connect(str(usage))
        before = connection.execute(
            "SELECT COALESCE(MAX(rowid), 0) FROM usage_requests").fetchone()[0]
        connection.close()
    # The bundled UI posts no model field; this body matches that shape on purpose. The
    # conversation header is what the UI does send, and it is what ties this turn to the
    # spoken ones checked below.
    conversation = f"accept-{int(time.time())}"
    code, reply = client.call("/v1/chat/completions", "POST", {
        "messages": [{"role": "user", "content": "记住这个暗号：蓝色鲸鱼"}],
        "max_tokens": 40, "stream": False,
    }, timeout=180, headers={"X-Conversation-Id": conversation})
    results.append(check("聊天请求成功", code == 200,
                         f"http={code} {str(reply)[:100] if code != 200 else ''}"))
    time.sleep(2)
    row = None
    total_9b = 0
    if before is not None:
        connection = sqlite3.connect(str(usage))
        row = connection.execute(
            "SELECT model, mode, status FROM usage_requests WHERE rowid > ? "
            "ORDER BY started_at DESC LIMIT 1", (before,)).fetchone()
        total_9b = connection.execute(
            "SELECT COUNT(*) FROM usage_requests WHERE model LIKE '%9B%'").fetchone()[0]
        connection.close()
    results.append(check("新记录已落库", row is not None, str(row)))
    if row:
        results.append(check("记录里的模型名是 9B", "9B" in row[0], row[0]))
    results.append(check("库中已有 9B 行", before is None or total_9b > 0, f"9B 行数={total_9b}"))

    if args.skip_voice:
        print("\n3. 语音共用后端（按要求跳过）")
        passed = sum(1 for item in results if item)
        print(f"\n=== {passed}/{len(results)} 项通过 ===")
        return 0 if passed == len(results) else 1

    print("\n3. 语音模式借用同一个后端")
    backend_pid = port_owner(8082)
    servers_before = llama_servers()
    code, started = client.call("/local/voice/mode", "POST", {"profile": PROFILE_9B}, timeout=300)
    results.append(check("语音模式已启动",
                         code == 200 and field(started, "started") is True,
                         f"http={code} error={str(field(started, 'error'))[:80]}"))
    results.append(check("网关报告大脑是共用的", field(started, "brain_shared") is True,
                         f"brain_shared={field(started, 'brain_shared')}"))
    results.append(check("共用的大脑端口就是网关后端", field(started, "brain_port") == 8082,
                         str(field(started, "brain_port"))))
    results.append(check("识别进程已就绪", field(started, "asr_ready") is True,
                         f"asr_ready={field(started, 'asr_ready')}"))
    if field(started, "asr_ready") is not True:
        # The recogniser loads in the background while the brain is already borrowed, so a
        # single reading can catch it mid-load and report a working voice mode as broken.
        # /local/voice/mode is idempotent: asking again returns the current state.
        deadline = time.monotonic() + 60
        latest = started
        while time.monotonic() < deadline:
            time.sleep(3)
            code, latest = client.call("/local/voice/mode", "POST", {"profile": PROFILE_9B},
                                       timeout=120)
            if field(latest, "asr_ready") is True:
                break
        results.append(check("识别进程随后就绪",
                             field(latest, "asr_ready") is True,
                             f"asr_ready={field(latest, 'asr_ready')}"))
    # The same-source rule, as the gateway states it: no reload, plugin only.
    decision = field(started, "switch") or {}
    results.append(check("本次属于同源切换（只换插件）",
                         decision.get("same_source") is True and decision.get("reload") is False,
                         str(decision.get("reason"))))
    servers_after = llama_servers()
    # 8097 is the recogniser, which voice mode starts on demand by design; what must not
    # appear is a second *language model* backend (the module's own 8098, or another copy
    # of the gateway's 8082).
    new_servers = [item for item in servers_after
                   if item not in servers_before and item[1] != "8097"]
    results.append(check("没有多起一个语言模型进程", not new_servers,
                         f"新增：{[item for item in servers_after if item not in servers_before]}"))
    results.append(check("网关后端仍是原来那个进程", port_owner(8082) == backend_pid,
                         f"{backend_pid} -> {port_owner(8082)}"))

    try:
        code, reply = client.call("/local/voice/chat", "POST",
                                  {"text": "暗号是什么？", "profile": PROFILE_9B,
                                   "conversation_id": conversation}, timeout=180)
        text = str(field(reply, "reply") or field(reply, "text") or "")
        results.append(check("语音回合有回复", code == 200 and bool(text.strip()),
                             f"http={code} reply={text[:60]!r}"))
        # The point of the shared ledger: what was typed a moment ago is in front of the model
        # when the owner starts talking.
        results.append(check("语音答得出文字里约定的内容", "蓝色鲸鱼" in text, text[:60]))

        code, detail = client.call("/local/conversation?conversation_id="
                                   + urllib.parse.quote(conversation) + "&limit=8", timeout=30)
        history = field(detail, "history") or []
        modes = [turn.get("mode") for turn in history]
        meter = field(detail, "meter") or {}
        results.append(check("写与说在同一条历史里",
                             code == 200 and len(history) >= 4
                             and "text" in modes and "voice" in modes,
                             f"http={code} turns={len(history)} modes={modes}"))
        results.append(check("两条路径共用一张上下文余量表",
                             meter.get("used_tokens", 0) > 0
                             and meter.get("remaining_tokens", 0) > 0
                             and meter.get("context_size") == 32768,
                             f"used={meter.get('used_tokens')} left={meter.get('remaining_tokens')} "
                             f"total={meter.get('context_size')} source={meter.get('source')}"))
        results.append(check("语音回合的余量取自模型分词器",
                             meter.get("source") == "measured", str(meter.get("source"))))
    finally:
        code, stopped = client.call("/local/voice/mode", "POST", {}, timeout=120)
        results.append(check("语音模式已关闭", code == 200 and field(stopped, "started") is False,
                             f"http={code}"))
    time.sleep(2)
    code, after = client.call("/local/status", timeout=15)
    results.append(check("关闭语音后后端仍然在线（没有来回卸载）",
                         field(after, "state") == "ready"
                         and field(after, "model_profile") == PROFILE_9B,
                         f"state={field(after, 'state')} profile={field(after, 'model_profile')}"))
    results.append(check("模型进程数回到起点", len(llama_servers()) <= len(servers_before),
                         f"{len(servers_before)} -> {len(llama_servers())}"))

    if args.check_base_switch:
        print("\n4. 换底座模型：语音自己切换，不询问（真的加载 27B，约 1–2 分钟）")
        code, _ = client.call("/local/model", "POST", {"profile": PROFILE_27B}, timeout=60)
        results.append(check("已切到底座 27B", code in (200, 202), f"http={code}"))
        landed = wait_for(PROFILE_27B)[1]
        results.append(check("27B 已成为常驻底座",
                             field(landed, "loaded_profile") == PROFILE_27B,
                             f"loaded={field(landed, 'loaded_profile')} "
                             f"state={field(landed, 'state')}"))

        code, started = client.call("/local/voice/mode", "POST", {"profile": PROFILE_9B},
                                    timeout=300)
        decision = field(started, "switch") or {}
        results.append(check("再次进入语音：判为换底模并整套切换",
                             decision.get("same_source") is False and decision.get("reload") is True,
                             str(decision.get("reason"))))
        results.append(check("语音没有反问、直接启动成功",
                             code == 200 and field(started, "started") is True,
                             f"http={code} error={str(field(started, 'error'))[:80]}"))
        code, status = client.call("/local/status", timeout=15)
        results.append(check("常驻底座已换成语音要的 9B",
                             field(status, "loaded_profile") == PROFILE_9B
                             and field(status, "model_profile") == PROFILE_9B,
                             f"loaded={field(status, 'loaded_profile')} "
                             f"selected={field(status, 'model_profile')}"))
        # Leave the machine on the profile the owner selected as default, and wait until it
        # really is — returning on the 202 is what left the next run starting from the 27B.
        client.call("/local/voice/mode", "POST", {}, timeout=120)
        client.call("/local/model", "POST", {"profile": PROFILE_9B}, timeout=60)
        code, restored = wait_for(PROFILE_9B)
        results.append(check("恢复默认 9B 并确认已就绪",
                             field(restored, "loaded_profile") == PROFILE_9B,
                             f"loaded={field(restored, 'loaded_profile')} "
                             f"state={field(restored, 'state')}"))

    passed = sum(1 for item in results if item)
    print(f"\n=== {passed}/{len(results)} 项通过 ===")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
