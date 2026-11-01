"""Run one synthesis with the engine's own logs streaming live.

The worker redirects stderr into a pipe that is only read at the end, so the last
probe produced no evidence about where synthesis stalled. Here stderr is a separate
reader thread printing as it arrives, and every stage is wrapped so the first line
that does not appear identifies the blocking call.
"""

import logging
import os
from pathlib import Path
import subprocess
import sys
import threading

PY = r"D:\AI\Qwen-Local\tools\tts-venv\Scripts\python.exe"
CWD = r"D:\AI\Qwen-Local\tools\Qwen3-TTS-GGUF"

INNER = r'''
import logging, sys, time, os
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s",
                    stream=sys.stderr)
sys.path.insert(0, r"D:\AI\Qwen-Local\tools\Qwen3-TTS-GGUF")

def stage(name):
    print(f"### STAGE {name}", file=sys.stderr, flush=True)

stage("import")
from qwen3_tts_gguf.inference import TTSEngine, TTSConfig

stage("engine_init")
t0 = time.time()
engine = TTSEngine(model_dir="model-custom", onnx_provider="CPU", llm_use_gpu=False, verbose=True)
print(f"### engine ready={bool(engine)} in {time.time()-t0:.1f}s", file=sys.stderr, flush=True)

stage("create_stream")
stream = engine.create_stream()
print(f"### stream={stream is not None}", file=sys.stderr, flush=True)
if stream is None:
    sys.exit(3)

stage("build_prompt")
from qwen3_tts_gguf.inference.prompt_builder import PromptBuilder
try:
    from qwen3_tts_gguf.inference.stream import map_language
except Exception:
    map_language = lambda x: 2055
pb = stream.prompt_builder
t0 = time.time()
pdata = pb.build_custom_prompt("你好。", "uncle_fu", map_language("chinese"), None)
print(f"### prompt built in {time.time()-t0:.1f}s embd={len(pdata.embd)}", file=sys.stderr, flush=True)

stage("talker_prefill")
stream.talker.clear_memory()
t0 = time.time()
hidden = stream.talker.prefill(pdata, seq_id=0)
print(f"### prefill done in {time.time()-t0:.1f}s", file=sys.stderr, flush=True)

stage("done_no_synthesis")
print("### all stages reached", file=sys.stderr, flush=True)
'''


def main():
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    process = subprocess.Popen(
        [PY, "-X", "utf8", "-c", INNER],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", bufsize=1, cwd=CWD, env=env,
    )

    def pump(stream, label):
        for line in stream:
            print(f"[{label}] {line.rstrip()}", flush=True)

    threads = [
        threading.Thread(target=pump, args=(process.stdout, "out"), daemon=True),
        threading.Thread(target=pump, args=(process.stderr, "err"), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        code = process.wait(timeout=240)
        print(f"### exit code = {code}")
    except subprocess.TimeoutExpired:
        print("### TIMEOUT: 卡住了，上面最后一条 STAGE 就是阻塞点")
        process.kill()
    for thread in threads:
        thread.join(timeout=3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
