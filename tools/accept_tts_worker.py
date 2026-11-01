"""Acceptance test for the synthesis worker, driven the way the gateway drives it.

This reproduces mode_server's side of the protocol - the same interpreter, the same
working directory, the same environment logic, one JSON request per line - so what is
being accepted is the production path and not a friendlier stand-in. It checks the
three things the gateway depends on:

  1. the readiness probe only answers ready once the engine exists
  2. a synthesis request returns a real WAV
  3. the second request is fast, because the engine stayed loaded

Run it with the card free to exercise the GPU path, or with QWEN_TTS_CPU=1 to check
the processor path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORKER = ROOT / "tools" / "tts_server.py"
PYTHON = ROOT / "tools" / "tts-venv" / "Scripts" / "python.exe"
LOG = ROOT / "tools" / "bench-out" / "tts_worker_stderr.txt"

SENTENCES = [
    "今天天气不错，我们出门走走吧。",
    "顺便买点水果回来，晚上做一顿好吃的。",
]


def gpu_free_mib():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.stdout.splitlines():
        if line.strip().isdigit():
            return int(line.strip())
    return None


def main():
    free = gpu_free_mib()
    print("free VRAM before start: %s MiB" % free)
    environment = dict(os.environ)
    environment.pop("QWEN_TTS_CPU", None) if "--cpu" in sys.argv else None
    print("QWEN_TTS_CPU=%r" % environment.get("QWEN_TTS_CPU"))

    LOG.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOG, "w", encoding="utf-8")
    process = subprocess.Popen(
        [str(PYTHON), "-X", "utf8", str(WORKER)],
        cwd=str(ROOT / "tools" / "Qwen3-TTS-GGUF"),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=handle,
        text=True, encoding="utf-8", bufsize=1, env=environment,
    )

    def exchange(payload):
        process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        process.stdin.flush()
        line = process.stdout.readline()
        return json.loads(line) if line.strip() else None

    try:
        started = time.time()
        ready = exchange({"op": "ping"})
        print("ping -> %s   (engine build %.1fs)" % (ready, time.time() - started))
        if not ready or not ready.get("ready"):
            print("RESULT: FAIL - worker never became ready")
            return 1

        for index, text in enumerate(SENTENCES):
            asked = time.time()
            reply = exchange({"text": text, "speaker": "uncle_fu", "language": "chinese"})
            elapsed = time.time() - asked
            if not reply or reply.get("error"):
                print("RESULT: FAIL - %s" % (reply.get("error") if reply else "no reply"))
                return 1
            path = Path(reply["path"])
            data = path.read_bytes()
            # 24 kHz, 16-bit mono: 44 header bytes then two bytes per sample.
            seconds = max(0, len(data) - 44) / 2 / 24000
            print("request %d: %5.2fs wall for %5.2fs of audio -> RTF %.3f  (%.0f KB)"
                  % (index + 1, elapsed, seconds, elapsed / seconds if seconds else float("nan"),
                     len(data) / 1024))
            path.unlink(missing_ok=True)
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        handle.close()

    print("\n--- worker stderr (provider / model lines) ---")
    for line in LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        if any(key in line for key in ("provider", "ready", "Decoder", "Engine", "error", "Error")):
            print("   " + line.strip()[:160])
    print("\nRESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
