"""Find the largest context each 9B mode can hold without risking the card.

Guessing this is how a 12 GB card ends up thrashing: llama.cpp allocates the KV cache at
load time, so the number that matters is "free VRAM while it is loaded", and it can only be
measured. This loads the 9B exactly as the gateway would — same layer count, same cache
quantisation, same projector for the vision case — on a spare port, reads the card through
nvidia-smi, and unloads immediately. The running service is not touched.

    & ..\\ComfyUI-BSAI_v010\\python\\python.exe -X utf8 probe_9b_context.py
    & ..\\ComfyUI-BSAI_v010\\python\\python.exe -X utf8 probe_9b_context.py --contexts 32768 49152

Every run prints a table; the recommendation at the end is the largest context whose
remaining free VRAM stays above the safety margin (default 1024 MiB), because the owner's
other applications also need the card.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.qwen_config import EXECUTABLE, PROFILES  # noqa: E402

PROFILE = "qwen3.5-9b-uncensored"
PROBE_PORT = 8099
# The decoder buffer for a 9B at these context sizes, measured on this machine; it is not
# part of the KV cache and does not grow with context, so it is charged once.
SAFETY_MARGIN_MIB = 1024


def card_free():
    completed = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.free,memory.used", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=20)
    line = (completed.stdout or "").strip().splitlines()
    if not line:
        return None, None
    parts = [part.strip() for part in line[0].split(",")]
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None, None
    return int(parts[0]), int(parts[1])


def build_command(context, mmproj):
    profile = PROFILES[PROFILE]
    command = [
        str(EXECUTABLE), "-m", str(profile["model"]),
        "--host", "127.0.0.1", "--port", str(PROBE_PORT),
        "--alias", "context-probe", "-c", str(context), "-np", "1",
        "-ngl", str(profile.get("gpu_layers", 64)),
        "-b", "256", "-ub", "128", "-t", "16", "-tb", "16",
        "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0", "--fit", "off",
        "--jinja", "--reasoning", "off", "--spec-type", "none",
        "--cache-ram", "1024", "--no-warmup", "--offline",
    ]
    if mmproj:
        command += ["--mmproj", str(profile["mmproj"]), "--no-mmproj-offload"]
    return command


def wait_ready(process, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PROBE_PORT}/health", timeout=3) as response:
                if json.loads(response.read()).get("status") == "ok":
                    return True
        except (urllib.error.URLError, ValueError, OSError):
            pass
        time.sleep(1)
    return False


def start_backend(context, mmproj):
    """Load the probe backend and return (process, ready)."""
    logs = ROOT / "logs"
    out = (logs / "context-probe.out.log").open("wb")
    err = (logs / "context-probe.err.log").open("wb")
    process = subprocess.Popen(build_command(context, mmproj), cwd=str(ROOT),
                               stdout=out, stderr=err,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return process, wait_ready(process), out, err


def recognizer_command():
    """The voice recogniser exactly as voice_mode starts it, on the probe port + 1."""
    from app.voice.voice_mode import ASR_MMPROJ, ASR_MODEL, LLAMA

    return [
        str(LLAMA), "-m", str(ASR_MODEL), "--mmproj", str(ASR_MMPROJ),
        "--host", "127.0.0.1", "--port", str(PROBE_PORT + 1),
        "--alias", "asr-probe", "-c", "4096", "-ngl", "99",
        "--no-warmup", "--no-mmproj-offload",
    ]


def probe_voice_stack(context):
    """The whole voice pairing at once: borrowed backend plus recogniser.

    Voice mode reuses the chat backend and adds the recogniser, so its real ceiling is the
    *pair's* footprint. Measuring the backend alone would recommend a context that leaves no
    room for the second process voice mode always starts.
    """
    before_free, _ = card_free()
    backend, ready, out, err = start_backend(context, True)
    logs = ROOT / "logs"
    asr_out = (logs / "context-probe-asr.out.log").open("wb")
    asr_err = (logs / "context-probe-asr.err.log").open("wb")
    asr = subprocess.Popen(recognizer_command(), cwd=str(ROOT), stdout=asr_out, stderr=asr_err,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        asr_ready = False
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if asr.poll() is not None:
                break
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{PROBE_PORT + 1}/health", timeout=3) as response:
                    if json.loads(response.read()).get("status") == "ok":
                        asr_ready = True
                        break
            except (urllib.error.URLError, ValueError, OSError):
                pass
            time.sleep(1)
        time.sleep(6)
        free, used = card_free()
        return {"context": context, "mmproj": True, "ready": ready and asr_ready,
                "free_mib": free, "used_mib": used,
                "loaded_mib": (before_free - free) if (before_free and free) else None}
    finally:
        for process in (asr, backend):
            try:
                process.terminate()
                process.wait(timeout=20)
            except Exception:  # noqa: BLE001 - a stuck probe must not outlive the script
                process.kill()
        for handle in (asr_out, asr_err, out, err):
            handle.close()
        time.sleep(3)


def probe(context, mmproj, settle=4.0):
    before_free, _ = card_free()
    logs = ROOT / "logs"
    out = (logs / "context-probe.out.log").open("wb")
    err = (logs / "context-probe.err.log").open("wb")
    process = subprocess.Popen(build_command(context, mmproj), cwd=str(ROOT),
                               stdout=out, stderr=err,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        ready = wait_ready(process)
        # A loaded server holds its allocation steady; reading too early reports the
        # pre-allocation state and would flatter the bigger contexts.
        time.sleep(settle)
        free, used = card_free()
        return {"context": context, "mmproj": mmproj, "ready": ready,
                "free_mib": free, "used_mib": used,
                "loaded_mib": (before_free - free) if (before_free and free) else None}
    finally:
        try:
            process.terminate()
            process.wait(timeout=20)
        except Exception:  # noqa: BLE001 - a stuck probe must not outlive the script
            process.kill()
        out.close()
        err.close()
        # The driver frees an exited process's memory asynchronously; the next probe must not
        # start while the previous allocation is still being reclaimed.
        time.sleep(3)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--contexts", type=int, nargs="*",
                        default=[32768, 40960, 49152, 65536, 81920])
    parser.add_argument("--margin", type=int, default=SAFETY_MARGIN_MIB,
                        help="free VRAM that must remain, in MiB")
    parser.add_argument("--skip-mmproj", action="store_true",
                        help="only probe the text/vision backend without the projector")
    parser.add_argument("--voice", action="store_true",
                        help="probe the whole voice pairing (borrowed backend + recogniser)")
    args = parser.parse_args()

    baseline_free, baseline_used = card_free()
    print(f"起始可用显存 {baseline_free} MiB（已用 {baseline_used} MiB），安全余量 {args.margin} MiB")
    rows = []
    if args.voice:
        print("\n=== 语音（借用后端 + 识别后端） ===")
        for context in args.contexts:
            result = probe_voice_stack(context)
            rows.append(result)
            verdict = "安全" if (result["free_mib"] or 0) >= args.margin else "风险"
            print(f"  c={context:>6}  占用 {str(result['loaded_mib']):>6} MiB  "
                  f"剩余 {str(result['free_mib']):>6} MiB  {'就绪' if result['ready'] else '加载失败'}  {verdict}")
    else:
        for mmproj in ([False] if args.skip_mmproj else [False, True]):
            label = "识图（带投影）" if mmproj else "纯文字"
            print(f"\n=== {label} ===")
            for context in args.contexts:
                result = probe(context, mmproj)
                rows.append(result)
                verdict = "安全" if (result["free_mib"] or 0) >= args.margin else "风险"
                print(f"  c={context:>6}  占用 {str(result['loaded_mib']):>6} MiB  "
                      f"剩余 {str(result['free_mib']):>6} MiB  {'就绪' if result['ready'] else '加载失败'}  {verdict}")

    print("\n=== 结论 ===")
    groups = ([(True, "语音（后端+识别）")] if args.voice
              else [(False, "纯文字"), (True, "识图")])
    for mmproj, label in groups:
        candidates = [r for r in rows if r["mmproj"] == mmproj and r["ready"]
                      and (r["free_mib"] or 0) >= args.margin]
        best = max(candidates, key=lambda r: r["context"]) if candidates else None
        if best:
            print(f"  {label}: 建议 {best['context']}（剩余 {best['free_mib']} MiB）")
        else:
            print(f"  {label}: 探测范围内没有安全值")
    print("\n注：纯文字与识图共用同一个后端进程，取值必须相同，否则切换模式会因上下文变化而重启。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
