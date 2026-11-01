"""Watch what happens to VRAM and the language-model process during one image job.

Submits a generation and samples, every two seconds: the job phase, whether the
llama-server process is alive, and the GPU's used memory. Used to explain slow
runs where ComfyUI apparently could not get the VRAM.
"""

from __future__ import annotations

import json
import subprocess
import time

import requests

GATEWAY = "http://127.0.0.1:8081"
PROMPT = ("photo of a handsome young asian man, athletic build, natural sunlight, "
          "detailed skin texture, looking at camera, 50mm portrait")


def gpu_used() -> int:
    output = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=15,
    ).stdout.strip().splitlines()
    return int(output[0]) if output else -1


def llama_alive() -> int:
    output = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq llama-server.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, timeout=15,
    ).stdout
    return output.count("llama-server.exe")


def wait_ready(seconds: float = 300) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        status = requests.get(f"{GATEWAY}/local/status", timeout=15).json()
        if status.get("state") == "ready" and not status.get("active_requests"):
            return
        time.sleep(3)
    raise SystemExit(f"gateway not ready: {status}")


def main() -> int:
    wait_ready()
    print(f"before: vram_used={gpu_used()} MiB  llama={'up' if llama_alive() else 'down'}", flush=True)
    response = requests.post(f"{GATEWAY}/local/image/generate", timeout=60, json={
        "prompt": PROMPT, "negative_prompt": "lowres, bad anatomy, bad hands, watermark, text",
        "enhance_prompt": False, "pipeline": "sdxl", "width": 832, "height": 1216, "seed": 20250912,
    })
    if response.status_code != 202:
        raise SystemExit(f"submit failed {response.status_code}: {response.text[:200]}")
    job_id = response.json()["job"]["id"]
    started = time.time()
    while time.time() - started < 420:
        payload = requests.get(f"{GATEWAY}/local/image/progress", timeout=15).json()
        job = payload.get("job") or {}
        print(f"  t+{time.time() - started:5.1f}s phase={job.get('phase', '-'):<16} "
              f"step={job.get('step')}/{job.get('max_step')} vram={gpu_used():>6} MiB "
              f"llama={'up' if llama_alive() else 'down'}", flush=True)
        if job.get("id") == job_id and not payload.get("busy") and job.get("state") != "running":
            print(json.dumps({"state": job.get("state"), "timings": job.get("timings"),
                              "params": job.get("params"), "gpu": job.get("gpu"),
                              "images": job.get("images")}, ensure_ascii=False, indent=2))
            print(f"after: vram_used={gpu_used()} MiB llama={'up' if llama_alive() else 'down'}")
            return 0
        time.sleep(2)
    raise SystemExit("timed out following the job")


if __name__ == "__main__":
    raise SystemExit(main())
