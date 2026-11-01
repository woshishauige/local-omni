"""RIFE frame interpolation for a finished film, driven from the panel.

A 24 fps H3 shot moves up to ~33 px per frame at its fastest, which reads as judder
and raises the per-frame warping error (see _audit/H3-打斗畸变与不流畅-实测分析-20260913.md).
RIFE doubles the frame rate by synthesising the in-between frames, so the graph is:

    LoadVideo -> GetVideoComponents -> RIFE VFI -> CreateVideo -> SaveVideo

The clip is loaded from ComfyUI's input folder, so a copy of the film is dropped there
for the run and removed afterwards; the interpolated file is published next to the
original as ``<name>-<fps>fps.mp4``.
"""
import asyncio
import shutil
import time
import uuid
from pathlib import Path

import av
import httpx

from app.image import image_comfy

COMFY = image_comfy.COMFY_URL
INPUT_ROOT = image_comfy.COMFY_ROOT / "ComfyUI" / "input"
OUTPUT_ROOT = image_comfy.COMFY_ROOT / "ComfyUI" / "output"
# The pack's default weights; rife49 is the newest of the ones on this machine.
DEFAULT_MODEL = "rife49.pth"


class InterpolateError(RuntimeError):
    pass


def probe(path: Path) -> dict:
    """What the film is, so the graph can double its frame rate."""
    with av.open(str(path)) as source:
        if not source.streams.video:
            raise InterpolateError("这个文件里没有视频轨")
        video = source.streams.video[0]
        return {
            "fps": float(video.average_rate) if video.average_rate else 24.0,
            "frames": int(video.frames or 0),
            "seconds": round(float(source.duration) / 1e6, 3) if source.duration else None,
            "width": video.width,
            "height": video.height,
            "audio": bool(source.streams.audio),
        }


def build_prompt(input_name: str, prefix: str, source_fps: float, multiplier: int = 2,
                 model: str = DEFAULT_MODEL) -> dict:
    """The API graph, with the frames handed straight from the loader to RIFE."""
    return {
        "1": {"class_type": "LoadVideo", "inputs": {"file": input_name},
              "_meta": {"title": "成片"}},
        "2": {"class_type": "GetVideoComponents", "inputs": {"video": ["1", 0]},
              "_meta": {"title": "拆画面与音轨"}},
        "3": {"class_type": "RIFE VFI",
              "inputs": {"ckpt_name": model, "frames": ["2", 0],
                         "clear_cache_after_n_frames": 10, "multiplier": int(multiplier),
                         "fast_mode": True, "ensemble": True, "scale_factor": 1.0,
                         "dtype": "float16", "torch_compile": False, "batch_size": 1},
              "_meta": {"title": f"RIFE ×{multiplier}"}},
        "4": {"class_type": "CreateVideo",
              "inputs": {"images": ["3", 0], "fps": round(source_fps * multiplier, 6),
                         "audio": ["2", 1]},
              "_meta": {"title": "合成视频"}},
        "5": {"class_type": "SaveVideo",
              # h264's dynamic combo gains an `encoding` choice; re-encoding at the
              # film's own quality keeps the interpolated copy as sharp as the source
              # (the "auto" default re-encodes at a lower CRF and measurably softens it).
              "inputs": {"video": ["4", 0], "filename_prefix": prefix,
                         "format": "mp4", "codec": "h264",
                         "encoding": "re-encode", "crf": 17.0},
              "_meta": {"title": "保存"}},
    }


def published_name(source: Path, fps: float) -> str:
    """``shot_01.mp4`` at 48 fps becomes ``shot_01-48fps.mp4`` next to it."""
    return f"{source.stem}-{round(fps)}fps{source.suffix or '.mp4'}"


class InterpolateJob:
    def __init__(self, job_id: str, source: Path, multiplier: int):
        self.id = job_id
        self.source = source
        self.multiplier = multiplier
        self.state = "queued"
        self.phase = "queued"
        self.error: str | None = None
        self.progress = 0.0
        self.target: Path | None = None
        self.prompt_id: str | None = None
        self.started = time.time()
        self.finished: float | None = None

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "state": self.state,
            "phase": self.phase,
            "progress": round(self.progress, 3),
            "error": self.error,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
            "source": str(self.source),
            "target": str(self.target) if self.target else None,
            "video": self.video_url(),
        }

    def video_url(self) -> str | None:
        if self.target is None:
            return None
        from urllib.parse import quote
        return "/local/video/file?path=" + quote(str(self.target))


class InterpolateMonitor:
    """One interpolation at a time; the GPU is shared with the film jobs."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.job: InterpolateJob | None = None
        self.task: asyncio.Task | None = None

    @property
    def busy(self) -> bool:
        return bool(self.task and not self.task.done())

    def progress(self) -> dict:
        return {"busy": self.busy, "job": self.job.snapshot() if self.job else None}

    def start(self, source: Path, multiplier: int = 2, model: str = DEFAULT_MODEL) -> dict:
        if self.busy:
            raise InterpolateError("已有补帧任务正在运行。")
        if not source.is_file():
            raise InterpolateError(f"找不到要补帧的文件：{source}")
        info = probe(source)
        if info["frames"] < 2:
            raise InterpolateError("视频太短，无法补帧")
        job = InterpolateJob(f"itp-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}",
                             source, multiplier)
        self.job = job
        self.task = asyncio.create_task(self._run(job, info, model), name=f"interpolate-{job.id}")
        return job.snapshot()

    async def _run(self, job: InterpolateJob, info: dict, model: str) -> None:
        staged: Path | None = None
        produced: Path | None = None
        try:
            job.state = "running"
            job.phase = "staging"
            INPUT_ROOT.mkdir(parents=True, exist_ok=True)
            staged = INPUT_ROOT / f"qwen-interp-{uuid.uuid4().hex}{job.source.suffix or '.mp4'}"
            shutil.copyfile(job.source, staged)
            prefix = f"qwen-interp/{uuid.uuid4().hex}"
            graph = build_prompt(staged.name, prefix, info["fps"], job.multiplier, model)
            job.phase = "submitting"
            response = await self.client.post(f"{COMFY}/prompt",
                                              json={"prompt": graph, "client_id": uuid.uuid4().hex},
                                              timeout=60)
            if response.status_code != 200:
                raise InterpolateError(f"ComfyUI 拒绝了补帧任务：{response.text[:300]}")
            job.prompt_id = response.json()["prompt_id"]
            job.phase = "interpolating"
            produced = await self._follow(job, info)
            if produced is None:
                return
            target = job.source.with_name(published_name(job.source, info["fps"] * job.multiplier))
            shutil.move(str(produced), str(target))
            job.target = target
            job.progress = 1.0
            job.phase = "done"
            job.state = "done"
        except asyncio.CancelledError:
            job.state = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001
            job.state = "error"
            job.error = str(exc)
        finally:
            job.finished = time.time()
            if staged is not None:
                staged.unlink(missing_ok=True)

    async def _follow(self, job: InterpolateJob, info: dict) -> Path | None:
        """Wait for the graph, then find the file SaveVideo wrote."""
        began = time.monotonic()
        # RIFE is small but not free: a 24 fps clip measured here interpolates at
        # roughly real time on this GPU, so the estimate is generous and only paces
        # the bar.
        estimate = max(30.0, float(info["seconds"] or 5.0) * 2.0)
        while time.monotonic() - began < 3600:
            if self.task is not None and self.task.cancelled():
                return None
            job.progress = min(0.95, (time.monotonic() - began) / estimate)
            try:
                response = await self.client.get(f"{COMFY}/history/{job.prompt_id}", timeout=30)
                payload = response.json().get(job.prompt_id) if response.status_code == 200 else None
            except (httpx.HTTPError, ValueError):
                payload = None
            if payload:
                status = payload.get("status", {})
                for message in status.get("messages", []):
                    if message and message[0] == "execution_error":
                        detail = message[1]
                        job.state = "error"
                        job.error = (f"{detail.get('node_type')}: "
                                     f"{detail.get('exception_message', '')[:300]}")
                        return None
                found = _saved_file(payload.get("outputs") or {})
                if found is None:
                    job.state = "error"
                    job.error = "补帧结束但没有找到输出文件"
                    return None
                return found
            await asyncio.sleep(3)
        job.state = "error"
        job.error = "补帧超过 1 小时仍未完成"
        return None


def _saved_file(outputs: dict) -> Path | None:
    """The path SaveVideo reported, resolved inside ComfyUI's output folder."""
    for node in outputs.values():
        if not isinstance(node, dict):
            continue
        for key in ("video", "images", "gifs"):
            entries = node.get(key)
            if isinstance(entries, dict):
                entries = [entries]
            if not isinstance(entries, list) or not entries or not isinstance(entries[0], dict):
                continue
            entry = entries[0]
            name = entry.get("filename")
            if not isinstance(name, str) or not name:
                continue
            candidate = (OUTPUT_ROOT / (entry.get("subfolder") or "") / name).resolve()
            if candidate.is_relative_to(OUTPUT_ROOT.resolve()) and candidate.is_file():
                return candidate
    return None
