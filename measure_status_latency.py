"""Measure how slow the toolbar's status poll gets while modes switch.

The toolbar polls /local/status every 2 s and calls the service disconnected after an 8 s
timeout, so a status call that blocks behind a model load shows up in the browser as
"本地服务连接失败" even though nothing is broken. This measures the endpoint instead of
guessing: it polls in a background thread while switching modes and reports the worst
latency, which is the number that decides whether the badge appears.

    & ..\\ComfyUI-BSAI_v010\\python\\python.exe -X utf8 measure_status_latency.py `
        --admin 9dd8022a804290f13ffede9289966c722c04ab0737f4c841
"""

import argparse
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOLBAR_TIMEOUT_SECONDS = 8.0


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

    def call(self, path, method="GET", body=None, timeout=120):
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


class Poller:
    """Hammers /local/status the way the toolbar does, and records every latency.

    It gets its own client, and therefore its own cookie jar and connection pool: sharing
    one opener with the thread that drives the switches serialises the two — urllib's
    cookie processor takes a lock — and then the poller reports the *other* thread's work
    as its own latency, which is exactly the mistake this harness exists to avoid.
    """

    def __init__(self, base, token, interval=0.25):
        self.client = Client(base, token)
        self.interval = interval
        self.samples = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                code, _ = self.client.call("/local/status", timeout=30)
            except Exception as failure:  # noqa: BLE001 - the latency is the point
                code = f"error:{type(failure).__name__}"
            self.samples.append((round(time.monotonic() - started, 3), code))
            self.stop.wait(self.interval)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=5)

    def report(self, label):
        if not self.samples:
            print(f"  {label}: 没有采样")
            return 0.0, 0
        times = [sample[0] for sample in self.samples]
        over = [sample for sample in self.samples if sample[0] > TOOLBAR_TIMEOUT_SECONDS]
        failures = [sample for sample in self.samples if str(sample[1]).startswith("error")]
        print(f"  {label}: n={len(times)} 中位 {statistics.median(times):.3f}s "
              f"最大 {max(times):.3f}s 超时(>8s) {len(over)} 次 请求失败 {len(failures)} 次")
        if over:
            print(f"    超时样本: {over[:6]}")
        if failures:
            print(f"    失败样本: {failures[:6]}")
        return max(times), len(over) + len(failures)


def wait_ready(client, profile=None, seconds=300):
    deadline = time.monotonic() + seconds
    status = {}
    while time.monotonic() < deadline:
        code, status = client.call("/local/status", timeout=20)
        if code == 200 and status.get("state") == "ready" and (
                profile is None or status.get("model_profile") == profile):
            return status
        time.sleep(2)
    return status


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8081")
    parser.add_argument("--admin", default="")
    parser.add_argument("--profile", default="qwen3.5-9b-uncensored")
    args = parser.parse_args()
    client = Client(args.base, args.admin)

    print("基线：空闲时的状态轮询")
    with Poller(args.base, args.admin) as poller:
        time.sleep(4)
    worst, bad = poller.report("空闲")
    total_worst, total_bad = worst, bad

    print("\n切换模式（纯文字 → 识图）时的状态轮询")
    with Poller(args.base, args.admin) as poller:
        client.call("/local/mode", "POST", {"mode": "vision"}, timeout=60)
        wait_ready(client)
        client.call("/local/mode", "POST", {"mode": "text"}, timeout=60)
        wait_ready(client)
    worst, bad = poller.report("切换中")
    total_worst, total_bad = max(total_worst, worst), total_bad + bad

    print("\n换底模（9B → 27B → 9B）时的状态轮询")
    with Poller(args.base, args.admin) as poller:
        client.call("/local/model", "POST", {"profile": "qwen3.8-27b"}, timeout=60)
        wait_ready(client, "qwen3.8-27b")
        client.call("/local/model", "POST", {"profile": args.profile}, timeout=60)
        wait_ready(client, args.profile)
    worst, bad = poller.report("换底模中")
    total_worst, total_bad = max(total_worst, worst), total_bad + bad

    print("\n进入语音时（借走后端并启动识别）的状态轮询")
    with Poller(args.base, args.admin) as poller:
        client.call("/local/voice/mode", "POST", {"profile": args.profile}, timeout=300)
        client.call("/local/voice/mode", "POST", {}, timeout=120)
    worst, bad = poller.report("语音中")
    total_worst, total_bad = max(total_worst, worst), total_bad + bad

    print(f"\n最差一次 {total_worst:.3f}s（工具栏超时阈值 {TOOLBAR_TIMEOUT_SECONDS}s）"
          f"，超时或失败共 {total_bad} 次")
    print("结论：" + ("工具栏会误报断联" if total_bad else "工具栏阈值安全"))
    return 0 if not total_bad else 1


if __name__ == "__main__":
    sys.exit(main())
