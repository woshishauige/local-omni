"""Verify the shared ComfyUI can serve every node the H3 video graph uses.

Starts a throwaway ComfyUI on a spare port with the exact flags the gateway
would use (read straight from ``image_comfy.COMFY_FLAGS``, so the list cannot
drift from production), asks it for its node types, and checks them against the
classes named in the shipped video workflows. Prints what is missing and exits
non-zero when the set is incomplete.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(r"D:\AI\ComfyUI-BSAI_v010")
PYTHON = ROOT / "python" / "python.exe"
MAIN = ROOT / "ComfyUI" / "main.py"
WORKFLOWS = ROOT / "ComfyUI" / "user" / "default" / "workflows" / "MiniMax-H3"
PORT = 8191
LOG = Path(r"D:\AI\_audit\whitelist_probe.log")

# Nodes that exist only in the browser graph and never appear in /object_info.
FRONTEND_ONLY = {"MarkdownNote", "Note"}

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.image import image_comfy  # noqa: E402


def workflow_class_types(folder: Path) -> dict[str, set[str]]:
    """class_type -> the workflow files that use it, for the folders we ship."""
    wanted: dict[str, set[str]] = {}
    for pattern in ("视频生成", "超分优化"):
        for path in sorted((folder / pattern).glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            for node in data["nodes"]:
                wanted.setdefault(node["type"], set()).add(path.name)
    return wanted


def main() -> int:
    wanted = workflow_class_types(WORKFLOWS)
    print(f"workflows name {len(wanted)} distinct node types")
    for name in sorted(wanted):
        print(f"   {name:<32} {'  '.join(sorted(wanted[name]))}")

    flags = [f.replace("8188", str(PORT)) for f in image_comfy.COMFY_FLAGS]
    print(f"\nlaunching probe on port {PORT} with the production flag list")
    # ComfyUI logs emoji; without UTF-8 the console writer dies with a
    # UnicodeEncodeError under a GBK code page. The gateway sets the same env.
    environment = {**os.environ, **image_comfy.COMFY_ENV}
    log = LOG.open("wb")
    process = subprocess.Popen(
        [str(PYTHON), "-s", "-u", str(MAIN), *flags], cwd=str(ROOT),
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        env=environment, creationflags=0x08000000 | 0x00000008,
    )
    info = None
    try:
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if process.poll() is not None:
                print(f"probe exited early with code {process.poll()}; see {LOG}")
                return 1
            try:
                info = requests.get(f"http://127.0.0.1:{PORT}/object_info", timeout=20).json()
                break
            except requests.RequestException:
                time.sleep(5)
        if info is None:
            print("probe did not come up within 300s")
            return 1

        print(f"probe reports {len(info)} node types")
        # MarkdownNote is a frontend-only annotation node: it never reaches the
        # API, so the workflows that carry one are still executable.
        missing = {name: files for name, files in wanted.items()
                   if name not in info and name not in FRONTEND_ONLY}
        if missing:
            print("\nMISSING:")
            for name, files in sorted(missing.items()):
                print(f"   {name:<32} needed by {', '.join(sorted(files))}")
            return 1
        print("\nall required node types are available")
        skipped = sorted(FRONTEND_ONLY & set(wanted))
        if skipped:
            print(f"ignored as frontend-only: {', '.join(skipped)}")

        # The image pipelines share this server, so check they resolve too.
        shared = ("CheckpointLoaderSimple", "UnetLoaderGGUF", "DualCLIPLoader", "VAELoader",
                  "SeedVR2Preprocess", "SeedVR2Conditioning", "SeedVR2PostProcessing",
                  "VAEEncodeTiled", "VAEDecodeTiled", "ResizeImageMaskNode", "KSampler", "SaveImage")
        gone = [name for name in shared if name not in info]
        print("image/upscale nodes still present:", "yes" if not gone else f"MISSING {gone}")

        if "--dump-schema" in sys.argv:
            for cls in ("H3MP4ShotEditor", "H3OptionalReferenceImage", "H3ModelLoaderAny",
                        "MiniMaxChunkFeedForward", "ApplyMiniMaxH3FirstBlockCache",
                        "PrimitiveStringMultiline", "StringConcatenate", "CLIPLoader", "VAELoader"):
                spec = info.get(cls)
                if spec is None:
                    print(f"\n{cls}: ABSENT")
                    continue
                print(f"\n=== {cls} ===")
                for section in ("required", "optional"):
                    for name, entry in (spec["input"].get(section) or {}).items():
                        kind = entry[0]
                        if isinstance(kind, list):
                            kind = f"COMBO[{len(kind)}]"
                        default = ""
                        if len(entry) > 1 and isinstance(entry[1], dict) and "default" in entry[1]:
                            default = f" default={entry[1]['default']!r}"
                        print(f"  [{section[:3]}] {name:<28} {kind}{default}")
                print(f"  outputs: {spec.get('output_name') or spec.get('return_types')}")
        return 1 if gone else 0
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())
