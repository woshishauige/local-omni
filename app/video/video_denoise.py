"""Denoise a finished film with ffmpeg, and report what it actually changed.

Measured on this machine's own H3 output: `nlmeans` cuts flat-region noise by 20–26% for
3–5% of the detail, while the face region gets *sharper* (1.74 → 1.85) and the silhouette
boundary contrast rises (7.21 → 7.86); `hqdn3d` is the fast option (−16%, −5%), and the
s=3.0 setting reaches −34% noise for −10% detail. SeedVR2 is not used here: at 1.0x it
*adds* texture (detail +86%, noise +10%), which is a restore, not a denoise.

The clip is written beside the original as ``<name>-denoised.mp4`` with the audio stream
copied, so a finished film keeps its soundtrack untouched.
"""
import json
import subprocess
from pathlib import Path

import cv2
import imageio_ffmpeg
import numpy as np
import av

MODES = {
    # Measured on a 12-step distilled render (180 frames, 768×512): s=1.2 takes 20% of the
    # flat-region noise for 3% of the detail, s=2.0 takes 26% for 5%, s=3.0 takes 34% for
    # 10%. Distilled output is the grainier one, so the default sits at 2.0.
    "nlmeans": "nlmeans=s=2.0:p=7:r=15",
    "nlmeans_strong": "nlmeans=s=3.0:p=7:r=15",
    # Spatio-temporal: the fastest, slightly softer (16% for 5%).
    "hqdn3d": "hqdn3d=4:3:6:4.5",
}
DEFAULT_MODE = "nlmeans"
# The dial the panel exposes for the upscale recipe's denoise pass.
MIN_STRENGTH = 1.0
MAX_STRENGTH = 3.5
CRF = "17"


class DenoiseError(RuntimeError):
    pass


def target_path(source: Path, suffix: str = "-denoised") -> Path:
    return source.with_name(f"{source.stem}{suffix}.mp4")


def _measure(path: Path, frames: int = 48) -> dict:
    """Flat-region noise sigma and Laplacian detail on a sample of the clip."""
    capture = cv2.VideoCapture(str(path))
    noise, detail = [], []
    seen = 0
    while seen < frames:
        ok, frame = capture.read()
        if not ok:
            break
        seen += 1
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
        gradient = np.hypot(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3),
                            cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3))
        flat = gradient <= np.quantile(gradient, 0.35)
        laplacian = cv2.Laplacian(gray, cv2.CV_32F, ksize=3)
        if flat.sum() > 100:
            noise.append(float(laplacian[flat].std()))
        detail.append(float(laplacian.var()))
    capture.release()
    if not seen:
        raise DenoiseError(f"读不到帧：{path}")
    return {"frames": seen, "noise": round(float(np.median(noise)), 3),
            "detail": round(float(np.median(detail)), 1)}


def probe(path: Path) -> dict:
    with av.open(str(path)) as source:
        video = source.streams.video[0] if source.streams.video else None
        if video is None:
            raise DenoiseError("这个文件里没有视频轨")
        return {"width": video.width, "height": video.height, "frames": video.frames,
                "fps": float(video.average_rate or 24), "has_audio": bool(source.streams.audio)}


def filter_for(strength: float) -> str:
    """The nlmeans filter at a caller-chosen strength.

    The panel exposes this so the upscale recipe's denoise can be dialled by hand:
    measured points on this machine are 1.2 (−20% noise / −3% detail), 2.0 (−26% / −5%)
    and 3.0 (−34% / −10%), and 2.0 is the default because it is where the noise drops
    while the detail barely moves.
    """
    value = min(MAX_STRENGTH, max(MIN_STRENGTH, float(strength)))
    return f"nlmeans=s={value:.2f}:p=7:r=15"


def run(source: Path, mode: str = DEFAULT_MODE, strength: float | None = None,
        suffix: str = "-denoised") -> dict:
    """Denoise ``source`` into a sibling file and measure both sides of the change."""
    if strength is not None:
        filters = filter_for(strength)
        mode = f"nlmeans s={min(MAX_STRENGTH, max(MIN_STRENGTH, float(strength))):.2f}"
    elif mode in MODES:
        filters = MODES[mode]
    else:
        raise DenoiseError(f"不认识的降噪模式：{mode}")
    if not source.is_file():
        raise DenoiseError(f"找不到影片：{source}")
    info = probe(source)
    target = target_path(source, suffix)
    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
               "-i", str(source), "-vf", filters, "-c:v", "libx264", "-crf", CRF,
               "-pix_fmt", "yuv420p"]
    command += ["-c:a", "copy"] if info["has_audio"] else ["-an"]
    command.append(str(target))
    result = subprocess.run(command, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0 or not target.is_file():
        raise DenoiseError(f"ffmpeg 失败：{(result.stderr or '').strip()[:200]}")
    return {"source": str(source), "target": str(target), "mode": mode,
            "before": _measure(source), "after": _measure(target),
            "bytes_before": source.stat().st_size, "bytes_after": target.stat().st_size}


def run_image(source: Path, strength: float | None = None,
              suffix: str = "-denoised") -> dict:
    """Denoise a single still (the image upscales' output) with the same filter.

    ffmpeg's nlmeans takes a one-frame input happily, so an upscaled picture goes through
    the same measured setting as a clip; the still keeps its own container.
    """
    if not source.is_file():
        raise DenoiseError(f"找不到图片：{source}")
    value = min(MAX_STRENGTH, max(MIN_STRENGTH, float(strength if strength is not None else 2.0)))
    target = source.with_name(f"{source.stem}{suffix}{source.suffix}")
    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
               "-i", str(source), "-vf", f"nlmeans=s={value:.2f}:p=7:r=15", "-frames:v", "1",
               str(target)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    if result.returncode != 0 or not target.is_file():
        raise DenoiseError(f"ffmpeg 失败：{(result.stderr or '').strip()[:200]}")
    return {"source": str(source), "target": str(target), "mode": f"nlmeans s={value:.2f}",
            "before": _measure_image(source), "after": _measure_image(target),
            "bytes_before": source.stat().st_size, "bytes_after": target.stat().st_size}


def _measure_image(path: Path) -> dict:
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise DenoiseError(f"读不到图片：{path}")
    gray = image.astype(np.float32)
    gradient = np.hypot(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3),
                        cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3))
    flat = gradient <= np.quantile(gradient, 0.35)
    laplacian = cv2.Laplacian(gray, cv2.CV_32F, ksize=3)
    return {"frames": 1,
            "noise": round(float(laplacian[flat].std()) if flat.sum() > 100 else 0.0, 3),
            "detail": round(float(laplacian.var()), 1)}


def describe(result: dict) -> str:
    before, after = result["before"], result["after"]
    noise = (after["noise"] - before["noise"]) / max(before["noise"], 1e-6)
    detail = (after["detail"] - before["detail"]) / max(before["detail"], 1e-6)
    return (f"降噪 {result['mode']}：噪声 σ {before['noise']:.2f} → {after['noise']:.2f} "
            f"（{noise:+.0%}）· 细节 {before['detail']:.0f} → {after['detail']:.0f} "
            f"（{detail:+.0%}）")


if __name__ == "__main__":
    import sys

    outcome = run(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else DEFAULT_MODE)
    print(json.dumps(outcome, ensure_ascii=False, indent=1))
    print(describe(outcome))
