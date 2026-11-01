"""Switch the ComfyUI core between the stable daily build and the acceleration build.

The two states measured on this machine (both verified with the same 2-step shot, same seed):

  stable    v0.33.2 + comfy-kitchen 0.2.31 + comfy-aimdo 0.4.13
            daily driver; the new-core features below are inert here.
  accel     v0.39.2 + comfy-kitchen 0.2.37 + comfy-aimdo 0.5.5
            unlocks the two measured-effective items: int8_convrot video VAE decode
            (-6.2% end to end: 150.7 s vs 160.7 s) and T8's multi-rate sampler
            (the director's `audio_steps` actually takes effect; +0 time).

The acceleration build needs commit headroom: with only 28.7 GB of page file the process is
killed silently once commit hits zero (measured 17.8 GB -> 0.0 GB). Raise the page file to
about 64 GB before using `accel` for real work, and stop the chat model while rendering.

Usage:
  python switch-comfy-version.py stable            # back to the daily build
  python switch-comfy-version.py accel             # v0.39.2 + its pinned deps
  python switch-comfy-version.py status            # what is checked out / installed
  python switch-comfy-version.py accel --dry-run   # print the plan only
"""
import argparse
import pathlib
import subprocess
import sys

COMFY = pathlib.Path(r"D:\AI\ComfyUI-BSAI_v010\ComfyUI")
PYTHON = r"D:\AI\ComfyUI-BSAI_v010\python\python.exe"

STATES = {
    "stable": {"tag": "v0.33.2", "packages": ("comfy-kitchen==0.2.31", "comfy-aimdo==0.4.13")},
    "accel": {"tag": "v0.39.2", "packages": ("comfy-kitchen==0.2.37", "comfy-aimdo==0.5.5")},
}


def run(command: list[str], cwd: pathlib.Path | None = None) -> str:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    return (result.stdout or "") + (result.stderr or "")


def status() -> None:
    head = run(["git", "log", "-1", "--format=%h %s"], cwd=COMFY).strip()
    versions = run([PYTHON, "-m", "pip", "show", "comfy-kitchen", "comfy-aimdo"])
    print(f"core:      {head}")
    for line in versions.splitlines():
        if line.startswith(("Name:", "Version:")):
            print(f"           {line.strip()}")
    vae = COMFY / "models" / "vae" / "minimax_h3_video_vae_int8_convrot.safetensors"
    print(f"int8 VAE:  {'on disk' if vae.is_file() else 'missing'}"
          + (f" ({vae.stat().st_size / 1e9:.2f} GB)" if vae.is_file() else ""))


def switch(state: str, dry_run: bool) -> int:
    target = STATES[state]
    print(f"-> {state}: checkout {target['tag']} + pip install {' '.join(target['packages'])}")
    if dry_run:
        return 0
    output = run(["git", "checkout", target["tag"]], cwd=COMFY)
    print("   " + output.strip().splitlines()[-1] if output.strip() else "   checkout done")
    output = run([PYTHON, "-m", "pip", "install", *target["packages"]])
    tail = [line for line in output.splitlines() if "Successfully installed" in line
            or "already satisfied" in line]
    print("   " + (tail[-1] if tail else output.strip().splitlines()[-1]))
    print("   restart ComfyUI for the switch to take effect")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("state", choices=("stable", "accel", "status"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.state == "status":
        status()
        return 0
    return switch(args.state, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
