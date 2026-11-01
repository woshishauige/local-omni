"""Drive the TTS worker once and report what came back.

Written as a file rather than an inline command because the request carries Chinese
text: PowerShell mangles quotes and encoding when piping it into another process, and
the worker then reports "bad json" for a request that was never malformed.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import wave

PY = r"D:\AI\Qwen-Local\tools\tts-venv\Scripts\python.exe"
SCRIPT = r"D:\AI\Qwen-Local\tools\tts_server.py"
CWD = r"D:\AI\Qwen-Local\tools\Qwen3-TTS-GGUF"
COPY_TO = Path(r"D:\AI\Qwen-Local\logs\tts-out")

REQUESTS = [
    {"text": "你好，我是本地部署的语音助手，现在正在测试中文语音合成。", "speaker": "uncle_fu", "language": "chinese"},
    {"text": "今天天气不错，适合出门散步。", "speaker": "serena", "language": "chinese"},
]


def main():
    COPY_TO.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    # QWEN_TTS_CPU=1 forces the synthesiser onto the processor; the worker reads the
    # same variable, so the two runs differ only in where the model lives.
    mode = "CPU" if env.get("QWEN_TTS_CPU", "").strip() not in ("", "0", "false") else "CUDA"
    print(f"=== TTS 引擎模式: {mode} ===")
    worker = subprocess.Popen(
        [PY, "-X", "utf8", SCRIPT],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", bufsize=1, cwd=CWD, env=env,
    )
    for index, request in enumerate(REQUESTS, 1):
        print(f"--- 请求 {index}: {request['speaker']} ---", flush=True)
        started = time.time()
        worker.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        worker.stdin.flush()
        # The decoder/speaker children inherit this stdout, so the stream also carries
        # their banners: read until a line actually parses as a reply object.
        payload = None
        while True:
            line = worker.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line.startswith("{"):
                print(f"   [worker] {line[:100]}")
                continue
            try:
                payload = json.loads(line)
                break
            except ValueError:
                print(f"   [unparsed] {line[:100]}")
        elapsed = time.time() - started
        if payload is None:
            print("   worker 无有效响应（可能崩溃）")
            break
        if payload.get("error"):
            print(f"   失败: {payload['error']}")
            continue
        path = Path(payload["path"])
        with wave.open(str(path)) as handle:
            seconds = handle.getnframes() / handle.getframerate()
            rate = handle.getframerate()
        target = COPY_TO / f"{request['speaker']}-test.wav"
        target.write_bytes(path.read_bytes())
        print(f"   OK {elapsed:.1f}s  音频 {seconds:.2f}s @ {rate}Hz  "
              f"RTF={elapsed/seconds:.2f}  -> {target.name}")

    worker.stdin.close()
    try:
        worker.wait(timeout=20)
    except subprocess.TimeoutExpired:
        worker.kill()
    errors = worker.stderr.read()
    if errors:
        print("\n--- worker stderr（截断）---")
        print(errors[-1200:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
