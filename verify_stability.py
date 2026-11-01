"""Opt-in real-model smoke test. Uses temporary files and ports 18081/18082.

Run manually with the bundled Python. Does not modify production settings,
chats, PID records, or model weights. Logs are retained in the printed temp path.
"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import psutil

SOURCE = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:18081"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(path, payload=None, timeout=90):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    with OPENER.open(req, timeout=timeout) as response:
        body = response.read()
        return response.status, json.loads(body) if "json" in response.headers.get("content-type", "") else body


def wait_ready(predicate=lambda status: True, timeout=210):
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        try:
            _, status = request("/local/status", timeout=4)
            if status.get("state") != previous:
                print(json.dumps({"state": status}, ensure_ascii=False), flush=True)
                previous = status.get("state")
            if status.get("state") == "ready" and predicate(status):
                return status
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise TimeoutError("Isolated service did not become ready")


def recorded_process(stage, name):
    record = json.loads((stage / name).read_text())
    process = psutil.Process(record["pid"])
    assert abs(process.create_time() * 1000 - record["created_unix_ms"]) < 20
    command = process.cmdline()
    if name == "backend.process.json":
        assert Path(process.exe()).resolve() == (SOURCE / "runtime/llama-server.exe").resolve()
        assert command[command.index("--port") + 1] == "18082"
    else:
        assert any(str(stage) in value for value in command)
    return process


def main():
    for port in (18081, 18082):
        with socket.socket() as check:
            if check.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError(f"Test port {port} already in use")
    stage = Path(tempfile.mkdtemp(prefix="qwen-stability-"))
    print(json.dumps({"test_directory": str(stage)}), flush=True)
    for name in ("qwen_service.py", "mode_server.py", "qwen_config.py", "usage_store.py", "usage_capture.py", "usage_tracking.py", "Start-Qwen.bat", "Start-Qwen.ps1"):
        code = (SOURCE / name).read_text(encoding="utf-8").replace("8081", "18081").replace("8082", "18082")
        if name == "qwen_config.py":
            lines = code.splitlines()
            for index, line in enumerate(lines):
                if line.startswith(("MODEL = ROOT /", "MMPROJ = ROOT /", "EXECUTABLE = ROOT /")):
                    lines[index] = line.replace("ROOT /", f"Path({str(SOURCE)!r}) /")
            code = "\n".join(lines) + "\n"
        if name == "mode_server.py":
            code = code.replace('(ROOT / "runtime/llama-server.exe").resolve()', f'(Path({str(SOURCE)!r}) / "runtime/llama-server.exe").resolve()')
        if name == "Start-Qwen.ps1":
            code = "\n".join(
                "$qwenPython = '" + str(Path(sys.executable)).replace("'", "''") + "'" if line.startswith("$qwenPython =") else line
                for line in code.splitlines()) + "\n"
        (stage / name).write_text(code, encoding="utf-8")
    settings = json.loads((SOURCE / "settings.json").read_text(encoding="utf-8"))
    settings["sleep_idle_seconds"] = 60  # Accelerated idle test; production stays 300.
    (stage / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    shutil.copytree(SOURCE / "web", stage / "web")
    result = {"test_directory": str(stage), "checks": []}
    try:
        launch = subprocess.run(["cmd.exe", "/d", "/c", str(stage / "Start-Qwen.bat"), "-NoBrowser"],
                                cwd=stage, stdin=subprocess.DEVNULL, capture_output=True, timeout=75)
        print(launch.stdout.decode(errors="replace"), flush=True)
        assert launch.returncode == 0, launch.stderr.decode(errors="replace")
        ready = wait_ready()
        assert ready["agent_enabled"] and ready["context_size"] == settings["context_size"]
        result["checks"].append("BAT startup with agent enabled and configured context")
        first_supervisor = recorded_process(stage, "supervisor.process.json").pid
        first_manager = recorded_process(stage, "supervisor-manager.process.json").pid
        # Two concurrent launches must reuse the existing processes.
        with ThreadPoolExecutor(max_workers=2) as pool:
            launches = list(pool.map(lambda _: subprocess.run(
                [str(sys.executable), "-X", "utf8", str(stage / "qwen_service.py"), "start"],
                capture_output=True, timeout=75), range(2)))
        assert all(item.returncode == 0 for item in launches)
        assert recorded_process(stage, "supervisor.process.json").pid == first_supervisor
        assert recorded_process(stage, "supervisor-manager.process.json").pid == first_manager
        result["checks"].append("concurrent starts reuse one supervisor and gateway")
        _, html = request("/")
        assert b"agent-defaults.js?v=2" in html and b"mode-control.js" in html
        _, tools = request("/tools")
        result["tool_endpoint_type"] = type(tools).__name__
        payload = {"model": "qwen3.8-27b-local", "messages": [{"role": "user", "content": "只回答：连接正常"}], "max_tokens": 32, "temperature": 0}
        code, completion = request("/v1/chat/completions", payload)
        assert code == 200 and completion["choices"]
        result["reply"] = completion["choices"][0]["message"].get("content")
        result["checks"].append("real non-streaming chat")
        tool = {"type": "function", "function": {"name": "local_probe", "description": "Return a test type", "parameters": {"type": "object", "properties": {"type": {"type": "string"}}, "required": ["type"]}}}
        agent = dict(payload, tools=[tool], tool_choice={"type": "function", "function": {"name": "local_probe"}}, max_tokens=100)
        code, completion = request("/v1/chat/completions", agent)
        assert code == 200 and completion["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "local_probe", completion
        result["checks"].append("real agent tool call with dictionary-valued schema type (500 regression)")
        stream = dict(payload, tools=[tool], tool_choice="none", stream=True)
        code, data = request("/v1/chat/completions", stream)
        assert code == 200 and b"[DONE]" in data and b'"error"' not in data, data[-500:]
        result["checks"].append("real streaming chat with agent schema")
        for invalid in ([], {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]}]}):
            try:
                request("/v1/chat/completions", invalid)
                raise AssertionError("Expected HTTP 400")
            except urllib.error.HTTPError as error:
                assert error.code == 400
        result["checks"].append("invalid requests return 400, never gateway 500")
        print(json.dumps({"checks_so_far": result}, ensure_ascii=False), flush=True)
        old_backend = recorded_process(stage, "backend.process.json")
        old_revision = ready["revision"]
        old_backend.terminate()
        old_backend.wait(timeout=15)
        ready = wait_ready(lambda status: status["revision"] > old_revision)
        assert recorded_process(stage, "backend.process.json").pid != old_backend.pid
        result["checks"].append("real model process crash recovers automatically")
        survivor = recorded_process(stage, "backend.process.json").pid
        instance = ready["instance_id"]
        old_manager = recorded_process(stage, "supervisor-manager.process.json")
        old_manager.terminate()
        old_manager.wait(timeout=15)
        ready = wait_ready(lambda status: status["instance_id"] != instance)
        assert recorded_process(stage, "backend.process.json").pid == survivor
        result["checks"].append("gateway crash recovers and adopts surviving model")
        print(json.dumps({"waiting_for_idle_sleep": True, "checks_so_far": result}, ensure_ascii=False), flush=True)
        ready = wait_ready(lambda status: status["sleeping"], timeout=95)
        assert ready["state"] == "ready"
        result["checks"].append("idle health probes allow model to sleep")
        code, completion = request("/v1/chat/completions", payload)
        assert code == 200 and completion["choices"]
        result["checks"].append("real chat wakes sleeping model")
    finally:
        stop = subprocess.run([str(sys.executable), "-X", "utf8", str(stage / "qwen_service.py"), "stop"],
                              capture_output=True, timeout=70)
        result["stop_exit_code"] = stop.returncode
        if stop.returncode:
            print(stop.stderr.decode(errors="replace"), flush=True)
            (stage / "service.stop").write_text("isolated test cleanup")
            for name in ("supervisor.process.json", "supervisor-manager.process.json", "backend.process.json"):
                try:
                    process = recorded_process(stage, name)
                    process.terminate()
                    process.wait(timeout=15)
                except (OSError, ValueError, psutil.Error):
                    pass
        (stage / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"result": result}, ensure_ascii=False), flush=True)
    assert result["stop_exit_code"] == 0


if __name__ == "__main__":
    main()
