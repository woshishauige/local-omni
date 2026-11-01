"""Can one llama-server serve both modes — main model + mmproj + MTP?

If it can, a mode switch stops being a restart (measured 18.5 s: 8.5 s to stop the old
server and 9.7 s to load the new one) and becomes a no-op. The audition runs on a spare
port with a small context, so it is memory-safe beside the live service.

Usage: python audition_combined_mode.py
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import qwen_config  # noqa: E402

PORT = 8099
BASE = f"http://127.0.0.1:{PORT}"


def post(path, body, timeout=120):
    request = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def get(path, timeout=20):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as response:
        return json.load(response)


def wait_health(process, deadline_seconds=180):
    started = time.time()
    while time.time() - started < deadline_seconds:
        if process.poll() is not None:
            return None, f"exited with code {process.poll()}"
        try:
            payload = get("/health", timeout=3)
            if payload.get("status") == "ok":
                return round(time.time() - started, 2), None
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.5)
    return None, "timeout"


def main():
    settings = qwen_config.read_settings()
    # Text settings, but with the vision layer count and the projector: the audition is
    # whether the draft head still engages when a projector is attached.
    settings = dict(settings)
    settings["text_gpu_layers"] = settings.get("vision_gpu_layers", 40)
    # Ask for both capabilities on one server: the projector *and* the draft head.
    command = qwen_config.server_command("vision", port=PORT, settings=settings,
                                         mtp=True, mmproj=True)
    for index, part in enumerate(command):
        if part == "-c":
            command[index + 1] = "8192"
    wants_mtp = "--spec-type" in command and "draft-mtp" in command
    print("command wants MTP:", wants_mtp)
    print("command:", " ".join(command[:8]), "…", " ".join(command[-10:]))

    logs = ROOT / "logs"
    out = (logs / "audition-combined.out.log").open("wb")
    err = (logs / "audition-combined.err.log").open("wb")
    started = time.time()
    process = subprocess.Popen(command, cwd=ROOT, stdout=out, stderr=err,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        elapsed, failure = wait_health(process)
        if failure:
            print("FAILED to become healthy:", failure)
            print("last error log lines:")
            print("\n".join((logs / "audition-combined.err.log").read_text(
                encoding="utf-8", errors="replace").splitlines()[-15:]))
            return 1
        print(f"healthy after {elapsed}s")

        props = get("/props")
        vision = bool(props.get("modalities", {}).get("vision"))
        print("props.modalities.vision:", vision)

        probe = post("/completion", {"prompt": "Hello", "n_predict": 2, "cache_prompt": False})
        spec = probe.get("generation_settings", {}).get("speculative.types", "none")
        print("speculative.types:", spec)
        mtp_live = "draft-mtp" in spec

        print("\nRESULT:")
        print(f"  vision capability present : {vision}")
        print(f"  MTP active with projector : {mtp_live}")
        print(f"  single backend for both   : {vision and mtp_live}")
        return 0 if (vision and mtp_live) else 2
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
        out.close()
        err.close()


if __name__ == "__main__":
    sys.exit(main())
