"""第 2 档实测：两个标识位在真网关上的口径对不对。

所有者规定的口径：**页面位**（有没有前端 Qwen 页面）与**任务位**（页面上有没有任务在跑），
两位同时为 0 → 后台自动关闭；任一位是 1 → 都不关。

无损部分（默认）：
  1. 起点读两位（没有页面、没有任务 → 0/0，但"没见过页面"所以**不关**）；
  2. 发一次心跳 → 页面位 1，隔一会儿再读，仍是 1；
  3. 发一次真对话请求 → 任务位 1（顺带证明对话本身不会被"收工"打断），请求结束后自动回 0。

真收工（`--stop`）：发"我关了"信标 → 页面位 0，且此时任务位也是 0 → 等它自己把
ComfyUI、隧道、网关依次收掉。这一步就是真的停服务，脚本只观察、不重启，结束后会打印拉起命令。

    python -X utf8 check_page_close_live.py
    python -X utf8 check_page_close_live.py --stop
"""
import argparse
import http.cookiejar
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_guest_capability_live import Visitor, lan_address, read_share_config  # noqa: E402

ROOT = Path(__file__).resolve().parent
HOST = "http://127.0.0.1:8081"


def owner():
    link = (ROOT / "logs" / "admin-link.txt").read_text(encoding="ascii").strip()
    jar = http.cookiejar.CookieJar()
    machine = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    machine.open(link, timeout=30).read()
    return machine


def call(machine, path, method="GET", body=None, timeout=180):
    data = None if method == "GET" else json.dumps(body if body is not None else {}).encode()
    request = urllib.request.Request(HOST + path, data=data, method=method,
                                     headers={"content-type": "application/json"} if data else {})
    try:
        with machine.open(request, timeout=timeout) as response:
            payload = response.read()
            return response.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as failure:
        return failure.code, failure.read()[:200].decode("utf-8", "replace")


def flags(machine) -> dict:
    status, payload = call(machine, "/local/lifecycle/status")
    if status != 200 or not isinstance(payload, dict):
        return {"page": "?", "task": "?", "raw": payload}
    return payload


def port_open(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def tunnel_alive() -> bool:
    import psutil

    for process in psutil.process_iter(["name"]):
        try:
            if (process.info["name"] or "").lower() == "cloudflared.exe":
                return True
        except psutil.Error:
            continue
    return False


def lossless(machine) -> list[str]:
    failures = []
    start = flags(machine)
    print(f"[1] 起点：page={start.get('page')} task={start.get('task')} "
          f"见过页面={start.get('page_ever_seen')}")
    if start.get("page_ever_seen") and start.get("page") == 0 and start.get("task") == 0:
        failures.append("起点两位都是 0：服务马上会自己收工，后面的检查没有意义")

    call(machine, "/local/lifecycle/page-alive", "POST")
    time.sleep(1)
    opened = flags(machine)
    print(f"[2] 心跳之后：page={opened.get('page')} （心跳 {opened.get('heartbeat_seconds_ago')} 秒前）")
    if opened.get("page") != 1:
        failures.append("心跳没有把页面位顶成 1")

    # 真对话请求：任务位应该在它跑着的时候是 1，跑完自动回 0。
    result = {}
    def ask():
        result["status"], result["body"] = call(
            machine, "/v1/chat/completions", "POST",
            {"messages": [{"role": "user", "content": "数一下 1 到 40，只写数字"}],
             "max_tokens": 400, "stream": False}, timeout=300)

    worker = threading.Thread(target=ask, daemon=True)
    worker.start()
    busy = None
    for _ in range(30):
        time.sleep(0.5)
        busy = flags(machine)
        if busy.get("task") == 1:
            break
    print(f"[3] 对话请求进行中：task={busy.get('task')} （{busy.get('task_label')}）")
    if busy.get("task") != 1:
        failures.append("对话请求进行中，任务位却不是 1")
    worker.join(timeout=300)
    after = flags(machine)
    print(f"[4] 请求结束（HTTP {result.get('status')}）：task={after.get('task')}")
    if after.get("task") != 0:
        failures.append("请求结束后任务位没有自动回 0")

    config = read_share_config()
    lan = lan_address()
    if config.get("access_token") and lan:
        visitor = Visitor(f"http://{lan}:{config.get('listen_port') or 8081}")
        visitor.login(str(config["access_token"]))
        status, body = visitor.call("/local/lifecycle/page-closed", "POST", body={})
        print(f"[5] 访客发“我关了”：HTTP {status}（应为 403，关他自己的标签页不该动机主的服务）")
        if status != 403:
            failures.append("访客的信标没有被拒")
        still = flags(machine)
        if still.get("page") != 1:
            failures.append("访客的信标把机主的页面位改掉了")
    return failures


def real_stop(machine) -> int:
    print("\n=== 真收工：两位都是 0，等它自己关 ===")
    before = flags(machine)
    print(f"信标前：page={before.get('page')} task={before.get('task')}")
    status, armed = call(machine, "/local/lifecycle/page-closed", "POST")
    print(f"信标 → HTTP {status} {json.dumps(armed, ensure_ascii=False)}")
    if armed.get("page") != 0:
        print("页面位没有立刻变 0")
        return 1
    if armed.get("task") != 0:
        print("还有任务在跑，按口径不该关；先等任务结束再试")
        return 1
    for step in range(1, 13):
        time.sleep(10)
        gateway, comfy, tunnel = port_open(8081), port_open(8188), tunnel_alive()
        print(f"t+{step * 10}s  8081={gateway}  8188={comfy}  隧道={tunnel}")
        if not gateway and not comfy and not tunnel:
            print("结论：页面位与任务位同时为 0 → 网关、ComfyUI、隧道都自己收掉了。")
            print("重新开始用：python -X utf8 qwen_service.py start（共享口径见 share.config.json）")
            return 0
    print("两位都是 0 却没收工（超时 120 秒）")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop", action="store_true", help="再真停一次（服务会下线，需手工拉起）")
    options = parser.parse_args()
    machine = owner()
    failures = lossless(machine)
    print("\n无损部分：" + ("全部通过" if not failures else "有失败 → " + "；".join(failures)))
    if options.stop and not failures:
        return real_stop(machine)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
