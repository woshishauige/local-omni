"""Turn an uploaded clip into the "previous shot" of the first shot.

The H3 director continues a shot from the shot before it: it reads that file's last 22
frames and last second of audio as motion context, plus one frame as the appearance
reference. Shot 1 has no shot before it, so the panel can hand it a clip instead — and
that clip has to arrive in exactly the shape the continuation path expects: the render's
size, 24 fps, and an audio track (the upload may have none).

The conversion happens here rather than in the node pack: the gateway already owns the
upload, already knows the render's size, and already shells out to ffmpeg for the RIFE
post-process. The node only ever sees a file that matches its own contract.
"""
import hashlib
import json
import subprocess

import av

MIN_SECONDS = 1.0  # 22 context frames at 24 fps, plus the second of audio the node wants


def _ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def probe(path) -> dict:
    """Duration, size and whether the clip carries audio."""
    with av.open(str(path)) as source:
        video = source.streams.video[0]
        seconds = float(video.duration * video.time_base) if video.duration else 0.0
        if not seconds and source.duration:
            seconds = float(source.duration) / av.time_base
        return {"seconds": seconds, "width": video.width, "height": video.height,
                "fps": float(video.average_rate) if video.average_rate else 0.0,
                "audio": bool(source.streams.audio)}


def normalize(source, width: int, height: int, input_root) -> str:
    """Scale, pad and re-time an uploaded clip; return the file name to hand the node.

    The result is content-addressed, so the same clip and size are converted once.
    """
    info = probe(source)
    if info["seconds"] < MIN_SECONDS:
        raise ValueError(f"前文视频太短：至少需要 {MIN_SECONDS:.1f} 秒（还要留出最后 22 帧和 1 秒音频）")
    digest = hashlib.sha256()
    with open(source, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    digest.update(json.dumps([width, height], sort_keys=True).encode())
    target = input_root / f"qwen-h3-context-{digest.hexdigest()[:12]}-{width}x{height}.mp4"
    if target.is_file():
        return target.name
    scale = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
             f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24")
    command = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)]
    if info["audio"]:
        command += ["-map", "0:v:0", "-map", "0:a:0", "-c:a", "aac", "-b:a", "192k"]
    else:
        # A clip without sound still needs a track: the continuation reads one second
        # of audio off the end of the previous shot.
        command += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                    "-map", "0:v:0", "-map", "1:a:0", "-shortest", "-c:a", "aac", "-b:a", "192k"]
    command += ["-vf", scale, "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", str(target)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=900)
    if result.returncode or not target.is_file():
        raise ValueError("前文视频转码失败：" + (result.stderr or "")[-300:])
    return target.name
