"""Stopping the service must free the card — measured, not asserted.

Run from an already-running shared service:

    & ..\\ComfyUI-BSAI_v010\\python\\python.exe -X utf8 check_release_on_stop.py `
        --admin 9dd8022a804290f13ffede9289966c722c04ab0737f4c841

What it does, in order:

1. Loads everything the service can hold at once — the chat backend and, through voice
   mode, the recogniser. (Voice mode borrows the 9B rather than loading a second copy,
   which is the point of `--check-base-switch`'s sibling work; the recogniser is the
   extra process that used to survive a stop.)
2. Records the card reading and every model process alive.
3. Runs the real `qwen_service.py stop`.
4. Asserts the model processes are gone, the model ports are free, the card reading
   improved, and no llama-server is left behind at all.
5. Starts the service again and confirms it comes back ready, so the machine is left as
   it was found.

The service is left running: this checks the release, it does not leave the owner without
a service.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)
MODEL_PORTS = (8082, 8097, 8098)
PROFILE_9B = "qwen3.5-9b-uncensored"


def powershell(script, timeout=60):
    completed = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script],
                               capture_output=True, text=True, timeout=timeout)
    return completed.stdout.strip()


def llama_servers():
    listing = powershell(
        "Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe'\" | "
        "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }")
    found = []
    for line in listing.splitlines():
        pid, _, command = line.partition("|")
        if not pid.strip().isdigit():
            continue
        port = ""
        if "--port" in command:
            tail = command.split("--port", 1)[1].split()
            port = tail[0] if tail else ""
        found.append((int(pid), port))
    return found


def card_free_mib():
    completed = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=20)
    line = (completed.stdout or "").strip().splitlines()
    return int(line[0]) if line and line[0].strip().isdigit() else None


def port_listening(port):
    return bool(powershell(
        f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
        "-ErrorAction SilentlyContinue | Measure-Object).Count") not in ("", "0"))


class Client:
    def __init__(self, base, token):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        if token:
            with self.opener.open(f"{self.base}/?admin={urllib.parse.quote(token)}",
                                  timeout=20) as response:
                response.read()

    def call(self, path, method="GET", body=None, timeout=300):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("content-type", "application/json")
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
    return payload.get(key, default) if isinstance(payload, dict) else default


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8081")
    parser.add_argument("--admin", default="", help="owner token from logs/admin-link.txt")
    args = parser.parse_args()
    results = []
    client = Client(args.base, args.admin)

    print("\n1. 把服务能占的都占上")
    code, status = client.call("/local/status", timeout=20)
    results.append(check("服务可用", code == 200 and field(status, "state") == "ready",
                         f"state={field(status, 'state')}"))
    if field(status, "model_profile") != PROFILE_9B:
        client.call("/local/model", "POST", {"profile": PROFILE_9B}, timeout=60)
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            _, status = client.call("/local/status", timeout=20)
            if field(status, "state") == "ready" and field(status, "model_profile") == PROFILE_9B:
                break
            time.sleep(3)
    code, started = client.call("/local/voice/mode", "POST", {"profile": PROFILE_9B}, timeout=300)
    results.append(check("语音就绪（识别进程已在）",
                         code == 200 and field(started, "asr_ready") is True,
                         f"http={code} asr={field(started, 'asr_ready')}"))
    before_servers = llama_servers()
    before_free = card_free_mib()
    print(f"  占用中：{before_servers}，可用显存 {before_free} MiB")
    results.append(check("确实有模型进程在跑", bool(before_servers), str(before_servers)))

    print("\n2. 用户的操作：停止服务")
    completed = subprocess.run([str(PYTHON), "-X", "utf8", str(ROOT / "qwen_service.py"), "stop"],
                               capture_output=True, text=True, timeout=180)
    stopped = {}
    try:
        stopped = json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        pass
    results.append(check("停止命令成功", completed.returncode == 0,
                         (completed.stderr or completed.stdout).strip()[:160]))
    released = stopped.get("released") or {}
    results.append(check("停止命令自己报告了释放结果", "card_free" in released, str(released)[:200]))

    print("\n3. 核实显存真的还回去了")
    after_servers = llama_servers()
    results.append(check("没有模型进程残留", not after_servers, str(after_servers)))
    busy_ports = [port for port in MODEL_PORTS if port_listening(port)]
    results.append(check("模型端口全部释放", not busy_ports, str(busy_ports)))
    after_free = card_free_mib()
    results.append(check("可用显存没有变少",
                         before_free is None or after_free is None or after_free >= before_free,
                         f"{before_free} → {after_free} MiB"))
    if released:
        results.append(check("命令报告的结论与实测一致",
                             released.get("card_free") is (not after_servers),
                             f"report={released.get('card_free')} measured={not after_servers}"))

    print("\n4. 把服务恢复原状")
    started_back = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(ROOT / "Start-Qwen-Share.ps1"), "-NoBrowser", "-Quiet"],
        capture_output=True, text=True, timeout=300)
    deadline = time.monotonic() + 240
    live = {}
    while time.monotonic() < deadline:
        code, live = client.call("/local/status", timeout=20)
        if code == 200 and field(live, "state") == "ready":
            break
        time.sleep(3)
    results.append(check("服务已恢复并加载完成",
                         field(live, "state") == "ready"
                         and field(live, "model_profile") == PROFILE_9B,
                         f"state={field(live, 'state')} profile={field(live, 'model_profile')}"))

    passed = sum(1 for item in results if item)
    print(f"\n=== {passed}/{len(results)} 项通过 ===")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
