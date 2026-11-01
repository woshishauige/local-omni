"""Time a real text -> vision -> text switch and report where the seconds go.

Reads the gateway's own phase log rather than guessing: `load()` now records
comfy_idle / process_spawned / health_ok / props_ok / completion_probe per switch.

Run the service first, then: python check_mode_switch.py
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
import http.cookiejar

BASE = "http://127.0.0.1:8081"
ROOT = Path(__file__).resolve().parent


def opener():
    link = (ROOT / "logs" / "admin-link.txt").read_text(encoding="ascii").strip()
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.open(link, timeout=25).read()
    return op


def call(op, method, path, body=None, timeout=300):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with op.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def latest_phase_lines(log_path, count=6):
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = [line for line in text.splitlines() if "Backend ready" in line]
    return lines[-count:]


def manager_log():
    logs = sorted((ROOT / "logs").glob("*-manager.err.log"), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


def switch(op, mode, label):
    status, payload = call(op, "POST", "/local/mode", {"mode": mode})
    wall = payload and json.loads(payload)
    print(f"{label}: HTTP {status} → {wall.get('state') if isinstance(wall, dict) else payload[:80]}")
    return status


def wait_ready(op, timeout=180):
    started = time.time()
    while time.time() - started < timeout:
        status, payload = call(op, "GET", "/local/status", timeout=20)
        if status == 200:
            state = json.loads(payload)
            if state.get("state") == "ready":
                return round(time.time() - started, 2), state
        time.sleep(1)
    return None, None


def main():
    op = opener()
    log = manager_log()
    print(f"manager log: {log.name if log else 'not found'}")
    before = len(latest_phase_lines(log, 100)) if log else 0

    results = {}
    for mode, label in (("vision", "text → vision"), ("text", "vision → text")):
        started = time.time()
        switch(op, mode, label)
        # Poll the status endpoint so the wall clock covers what the owner waits for, not
        # just the POST's own duration (the POST returns as soon as the switch is accepted).
        ready, state = wait_ready(op)
        results[mode] = {"wall": round(time.time() - started, 2), "ready": ready,
                         "reported": (state or {}).get("last_switch_seconds")}
        print(f"  wall clock until ready: {results[mode]['wall']}s "
              f"(gateway reported {results[mode]['reported']}s)")

    if log:
        print("\nphase timings from the gateway log:")
        for line in latest_phase_lines(log, 4):
            print("  " + line.strip()[-190:])
    print("\nsummary:", json.dumps(results, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
