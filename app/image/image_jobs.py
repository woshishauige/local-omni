"""Image generation jobs for the local Qwen gateway.

One Flux.2 Klein job owns the whole cycle: Qwen prompt optimization, unloading
the language model, queueing in ComfyUI, watching the sampler over ComfyUI's
websocket, decoding, and finally restoring the language model. The cycle runs in
the background so the browser can show live progress instead of blocking on one
long request, and the same telemetry is written to a local history file that
survives restarts.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterable
import contextlib
import json
import logging
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

import httpx
import websockets
from fastapi import HTTPException
from pydantic import BaseModel

from app.image import image_comfy
from app.image import image_pipelines
from app import prompt_enhance
from app.video import video_denoise
from app.image.image_comfy import ComfySupervisor, GpuMonitor

COMFY = "http://127.0.0.1:8188"
COMFY_WS = "ws://127.0.0.1:8188/ws"
# The language-model port and its system prompts now live in prompt_enhance, which owns the
# "which model optimises the prompt" rule for both the image and the video queue.
HISTORY_PATH = Path(__file__).resolve().parent / "logs" / "image-history.json"
HISTORY_LIMIT = 200
PREVIEW_LIMIT = 220 * 1024
EVENT_LOG_LIMIT = 40
GENERATION_TIMEOUT = 1800
POLL_INTERVAL = 1.0
# Loading the image model on this machine is the longest silent phase (about
# 20 s); anything past this without a progress note or sampler step means the
# job is wedged and must stop claiming the backend.
STALL_SECONDS = 180

LOGGER = logging.getLogger("image-jobs")


class ImageGenerateRequest(BaseModel):
    prompt: str
    negative_prompt: str = ""
    pipeline: str | None = None
    width: int | None = None
    height: int | None = None
    steps: int | None = None
    cfg: float | None = None
    seed: int | None = None
    # Optional like cfg/steps: a caller that does not care (an upscale recipe, for
    # instance) may leave it out or send null instead of guessing a number.
    denoise: float | None = None
    # SeedVR2's multiplier lives in the composer like steps/CFG; without it every
    # "放大" recipe inherited whatever the workflow file happened to save.
    scale: float | None = None
    filename_prefix: str | None = None
    image_1: str | None = None
    image_2: str | None = None
    reference_images: list[str] | None = None
    enhance_prompt: bool = True
    # nlmeans strength for the upscale recipe that denoises after SeedVR2 (1.0–3.5,
    # 2.0 is the measured default). Distinct from `denoise`, which is the sampler's.
    video_denoise: float | None = None
    # SeedVR2's redraw ratio: 1.0 (default) keeps its output as it is, lower mixes the
    # restored frames back with the plain resize. That mix is the only strength control the
    # model offers, and it is what calms the hair/eye texture boil measured in motion.
    upscale_strength: float | None = None


async def _safe_reference_names(body: ImageGenerateRequest) -> list[str]:
    refs = list(body.reference_images or [])
    refs.extend(x for x in (body.image_1, body.image_2) if x)
    if len(refs) > 2:
        raise HTTPException(400, "最多支持两张参考图")
    clean = []
    for name in refs:
        if not isinstance(name, str) or not name.strip() or Path(name).name != name:
            raise HTTPException(400, "参考图必须是已上传到 ComfyUI/input 的文件名")
        clean.append(name)
    return clean


async def _upload_reference_data_urls(client: httpx.AsyncClient, values: list[str | None]) -> list[str]:
    """Upload browser data URLs to ComfyUI and return safe input filenames."""
    names: list[str] = []
    for value in values:
        if not value:
            continue
        if not isinstance(value, str) or not value.startswith("data:image/"):
            raise HTTPException(400, "参考图必须是 PNG/JPEG/WebP 图片")
        try:
            header, encoded = value.split(",", 1)
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError, base64.binascii.Error) as exc:
            raise HTTPException(400, "参考图数据无效") from exc
        mime = header.split(";", 1)[0].lower()
        ext = {"data:image/png": ".png", "data:image/jpeg": ".jpg", "data:image/webp": ".webp"}.get(mime)
        if ext is None or len(raw) > 20 * 1024 * 1024:
            raise HTTPException(400, "参考图格式或大小不受支持")
        filename = f"qwen-image-ref-{uuid.uuid4().hex}{ext}"
        response = await client.post(
            f"{COMFY}/upload/image",
            files={"image": (filename, raw, mime)},
            data={"overwrite": "true"}, timeout=60,
        )
        response.raise_for_status()
        names.append(str(response.json().get("name") or filename))
    return names


async def _prepare_reference_inputs(client: httpx.AsyncClient, body: ImageGenerateRequest) -> list[str]:
    """Normalize uploaded filenames and browser data URLs into Comfy names."""
    values = list(body.reference_images or ())
    values.extend(x for x in (body.image_1, body.image_2) if x)
    if len(values) > 2:
        raise HTTPException(400, "最多支持两张参考图")
    names: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise HTTPException(400, "参考图不能为空")
        value = value.strip()
        if value.startswith("data:image/"):
            names.extend(await _upload_reference_data_urls(client, [value]))
        elif Path(value).name == value:
            names.append(value)
        else:
            raise HTTPException(400, "参考图必须是已上传到 ComfyUI/input 的文件名")
    return names


def comfy_image_url(image: dict) -> str:
    """A URL the *browser* can load for one produced file.

    Served by the gateway rather than by ComfyUI's own port: the panel is opened from the shared
    address too, and ``127.0.0.1:8188`` on a visitor's machine is the visitor's own computer. The
    gateway route applies the same ownership fence as the video side, so "download your own
    picture" works for a shared session and reveals nothing about anyone else's.
    """
    # The gateway owns the ComfyUI base URL; fall back to the local default so
    # this module stays usable on its own in tests.
    try:
        from app import mode_server
        base = getattr(mode_server, "COMFY", COMFY)
    except ImportError:
        base = COMFY
    if base and not str(base).startswith("http://127.0.0.1:8188"):
        # A gateway that re-points ComfyUI keeps the old behaviour of handing out its URL.
        query = urlencode({k: image[k] for k in ("filename", "subfolder", "type") if image.get(k)})
        return f"{base}/view?{query}"
    query = urlencode({k: image[k] for k in ("filename", "subfolder", "type") if image.get(k)})
    return f"/local/image/file?{query}"


def _load_history() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    try:
        stored = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        LOGGER.warning("Image history file is unreadable; starting a new one", exc_info=True)
        return []
    return [entry for entry in stored if isinstance(entry, dict)] if isinstance(stored, list) else []


def _write_history(entries: list[dict]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = HISTORY_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(HISTORY_PATH)


def _graph_parameters(graph: dict, pipeline_key: str) -> dict:
    """Read the values that will actually execute back out of the built graph."""
    labels = image_comfy.workflow_labels(pipeline_key)
    parameters = {}
    for name, target in labels.items():
        node = graph.get(target["node"]) or {}
        found = node.get("inputs", {}).get(target["input"])
        if isinstance(found, (int, float)):
            parameters[name] = int(found) if isinstance(found, int) else found
    return parameters


class ImageJob:
    """Everything one generation cycle exposes to the UI."""

    def __init__(self, job_id: str, prompt: str, negative_prompt: str, params: dict, references: list[str]):
        self.id = job_id
        self.prompt = prompt
        self.negative_prompt = negative_prompt
        self.params = params
        self.references = references
        self.created = time.time()
        self.updated = self.created
        self.phase = "queued"
        self.restoring = False
        self.started = time.time()
        self.finished: float | None = None
        self.step = 0
        self.max_step = int(params.get("steps") or 0)
        self.progress = 0.0
        self.queue_position: int | None = None
        self.queue_running = 0
        self.queue_pending = 0
        self.preview: str | None = None
        # Bumped every time `preview` changes. The panel polls this endpoint one to two
        # times a second, and the preview is ~79 KB of base64: measured, an unchanged
        # picture was re-sent on every poll (85,745 bytes a time, ~6-8 MB per generated
        # image, on a phone over the tunnel). The caller now sends the version it already
        # shows and gets the picture only when this number moves.
        self.preview_version = 0
        # How many jobs are ahead of this one when it was accepted into the waiting slot.
        self.queue_ahead = 0
        self.prompt_id: str | None = None
        self.final_prompt: str | None = None
        self.images: list[str] = []
        # Set by the upscale recipe that denoises after SeedVR2: the ffmpeg result, its
        # measured before/after numbers and the playable URL of the denoised cut.
        self.denoise: dict | None = None
        self.error: str | None = None
        self.state = "running"
        self.enhanced = False
        # Filled in by the enhancing phase: which model did the prompt work, and whether it ran
        # at all. Reported so the panel names the model that actually answered.
        self.enhancer: dict | None = None
        # The session that submitted this job: one monitor serves the whole machine, so this is
        # what keeps the owner's prompt out of a visitor's view and decides who may read the
        # resulting files back.
        self.session = ""
        self.estimate_calibrated = False
        self.pending_step_started: float | None = None
        self.step_samples: list[float] = []
        self.events: list[dict] = []
        self.timings: dict[str, float] = {}
        self.marks: dict[str, float] = {}
        self.graph: list[dict] = []
        self.gpu: dict | None = None
        # Sampling wall clock: ComfyUI reports steps sparsely, so the gateway
        # projects the current step from these timestamps between messages.
        self.sampling_started: float | None = None
        self.last_step_at: float | None = None

    # -- progress bookkeeping -------------------------------------------------
    def mark(self, name: str) -> None:
        self.marks[name] = time.monotonic()

    def finish_mark(self, name: str, key: str | None = None) -> None:
        if name in self.marks:
            self.timings[key or name] = round(time.monotonic() - self.marks[name], 3)
            del self.marks[name]

    def set_step(self, value: int, total: int) -> None:
        now = time.monotonic()
        value = max(0, int(value))
        if self.pending_step_started is not None and self.step > 0 and value > self.step:
            # Only count the time actually spent between two reported steps; the
            # first report includes model initialization, which is not a step.
            sample = now - self.pending_step_started
            if sample > 0:
                self.step_samples.append(sample)
        self.pending_step_started = now
        # ComfyUI can report a lower value after a projected estimate; keep the
        # bar monotonic.
        if self.state == "running" and value < self.step:
            value = self.step
        self.step = value
        self.max_step = max(int(total) or 0, self.max_step, self.step)
        self.updated = time.time()
        self.last_step_at = now
        if self.max_step:
            self.progress = min(1.0, self.step / self.max_step)

    def note_queue_started(self) -> None:
        """The language model is gone; the request now waits for ComfyUI."""
        self.mark("queue")

    def note_execution_started(self) -> None:
        """ComfyUI picked the prompt up: the queue wait is over."""
        self.finish_mark("queue")
        self.mark("load")

    def note_sampling_begun(self) -> None:
        """The sampler reported its first step, so weight loading has finished."""
        self.finish_mark("load")
        self.mark("sampling")
        self.sampling_started = time.monotonic()
        self.pending_step_started = self.sampling_started

    def note_sampling_finished(self) -> None:
        self.finish_mark("sampling")

    def project_step(self) -> None:
        """Fill the gap between ComfyUI's sparse step messages.

        Real samplers on this machine report the first and last step only, which
        would leave the bar frozen; the time since the last observed step and the
        measured per-step cost give a monotonic estimate that real events override.
        """
        if self.state != "running" or self.phase != "sampling" or not self.max_step:
            return
        if self.last_step_at is None or self.step <= 0 or self.step >= self.max_step:
            return
        average = sum(self.step_samples) / len(self.step_samples) if self.step_samples else None
        if average is None:
            # A single reported step: use the time it took to reach it.
            average = max(1.0, self.last_step_at - (self.sampling_started or self.last_step_at)) / self.step
        estimated = self.step + int((time.monotonic() - self.last_step_at) / max(0.2, average))
        estimated = min(self.max_step - 1, estimated)
        if estimated > self.step:
            self.step = estimated
            self.progress = min(1.0, self.step / self.max_step)

    def step_seconds(self) -> float | None:
        return sum(self.step_samples) / len(self.step_samples) if self.step_samples else None

    def eta_seconds(self) -> float | None:
        average = self.step_seconds()
        if average is None or not self.max_step:
            return None
        return max(0.0, average * (self.max_step - self.step))

    def note(self, key: str, detail: str = "") -> None:
        self.events.append({"at": time.time(), "key": key, "detail": detail})
        del self.events[:-EVENT_LOG_LIMIT]
        self.updated = time.time()

    def _core_fields(self) -> dict:
        """The keys the live snapshot and the saved history record share.

        Keeping one copy means a field added for the panel cannot silently go
        missing from the history file (and vice versa).
        """
        return {
            "id": self.id,
            "state": self.state,
            # Who submitted it (empty for the owner): the read routes use this to show a caller
            # its own job and to keep another caller's words out of it.
            "session": self.session,
            "prompt": self.prompt,
            "negative_prompt": self.negative_prompt,
            "final_prompt": self.final_prompt,
            "enhanced": self.enhanced,
            # Which model wrote the prompt this job used, when the optimiser ran: the rail and
            # the history file then name the model that actually did the work.
            "enhancer": self.enhancer,
            "params": self.params,
            "images": self.images,
            "denoise": self.denoise,
            "prompt_id": self.prompt_id,
            "error": self.error,
            "timings": dict(self.timings),
            "gpu": dict(self.gpu) if self.gpu else None,
            "model": self.model_summary(),
        }

    def snapshot(self, stalled: bool = False) -> dict:
        return {
            **self._core_fields(),
            "phase": self.phase,
            "restoring": self.restoring,
            "stalled": stalled,
            "step": self.step,
            "max_step": self.max_step,
            "progress": round(self.progress, 4),
            "queue_position": self.queue_position,
            "queue_running": self.queue_running,
            "queue_pending": self.queue_pending,
            "preview": self.preview,
            "preview_version": self.preview_version,
            "queue_ahead": self.queue_ahead,
            "references": len(self.references),
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            "elapsed": round((self.finished or time.time()) - self.started, 2),
            "total_seconds": round((self.finished or time.time()) - self.created, 2),
            "step_seconds": round(self.step_seconds(), 3) if self.step_seconds() else None,
            "steps_per_second": round(1 / self.step_seconds(), 3) if self.step_seconds() else None,
            "eta_seconds": round(self.eta_seconds(), 1) if self.eta_seconds() is not None else None,
            "estimate_calibrated": self.estimate_calibrated,
            "events": list(self.events),
            "graph": list(self.graph),
        }

    def model_summary(self) -> dict:
        """What actually ran, for the live panel and the history record."""
        pipeline = image_pipelines.pipeline(self.params.get("pipeline"))
        summary = {
            "pipeline": pipeline.key,
            "pipeline_name": pipeline.name,
            "family": pipeline.family,
            "sampler": self.params.get("sampler_name") or pipeline.defaults.get("sampler", "euler"),
            "loras": [value for key, value in self.params.items() if key.endswith(".lora_name") and value],
        }
        for node in pipeline.nodes:
            for field in node.fields:
                if field.choices in {"checkpoints", "diffusion_models", "text_encoders", "vae"}:
                    summary.setdefault(field.input, self.params.get(field.input))
        summary["unet"] = summary.get("ckpt_name") or summary.get("unet_name") or "-"
        return summary

    def history_record(self) -> dict:
        return {
            **self._core_fields(),
            "time": self.finished or time.time(),
            "references": [],
            "duration": {
                "total": round((self.finished or time.time()) - self.created, 2),
                "sampling": self.timings.get("sampling"),
            },
            "steps_per_second": round(1 / self.step_seconds(), 3) if self.step_seconds() else None,
        }


class ComfyQueueGuard:
    """Track how long the GPU has been idle while ComfyUI reports work running.

    A dead queue entry leaves the card at idle; a live render does not. ``observe``
    is fed every VRAM sample the job loop already takes, so the verdict costs no
    extra process spawn.
    """

    IDLE_SECONDS = 25.0
    UTILIZATION_PERCENT = 15.0

    def __init__(self, idle_seconds: float | None = None, utilization: float | None = None):
        self.idle_seconds = self.IDLE_SECONDS if idle_seconds is None else idle_seconds
        self.utilization_threshold = self.UTILIZATION_PERCENT if utilization is None else utilization
        self.idle_since: float | None = None
        self.utilization: float | None = None

    def observe(self, statistics: dict | None, now: float | None = None) -> None:
        moment = time.monotonic() if now is None else now
        utilization = (statistics or {}).get("utilization")
        self.utilization = utilization
        if utilization is None or utilization >= self.utilization_threshold:
            self.idle_since = None
            return
        if self.idle_since is None:
            self.idle_since = moment

    def idle_for(self, now: float | None = None) -> float:
        moment = time.monotonic() if now is None else now
        return 0.0 if self.idle_since is None else max(0.0, moment - self.idle_since)

    def looks_dead(self, now: float | None = None) -> bool:
        if self.idle_since is None:
            # No idle window at all: the latest sample said the card is working.
            return False
        return self.idle_for(now) >= self.idle_seconds


async def clear_dead_comfy_work(client, guard: "ComfyQueueGuard", *, own_prompt_ids: Iterable[str] = (),
                                supervisor=None) -> dict:
    """Drop queue entries nobody is executing; returns what was done.

    ``{"cleared": [...], "restarted": bool, "reason": str}`` — the caller only logs
    the reason, so a live render simply keeps waiting.

    ``own_prompt_ids`` is every prompt that is legitimately running: the image job's
    and the video monitor's. Without the video one, a mode switch during an H3 run's
    model-load phase (GPU briefly idle) misreads the render as leftover work and
    interrupts it.
    """
    queue = await _comfy_queue(client)
    if queue is None:
        return {"cleared": [], "restarted": False, "reason": "ComfyUI 不可达"}
    own = {value for value in own_prompt_ids if value}
    foreign = [entry for entry in queue["running"] if entry and entry not in own]
    if not foreign:
        return {"cleared": [], "restarted": False, "reason": "队列中没有别人的任务"}
    if await _all_finished(client, foreign):
        return {"cleared": [], "restarted": False, "reason": "队列记账滞后，实际已结束"}
    if not guard.looks_dead():
        return {"cleared": [], "restarted": False,
                "reason": f"GPU 仍在工作（利用率 {guard.utilization if guard.utilization is not None else '?'}%），判断为真实任务"}
    with contextlib.suppress(httpx.HTTPError, OSError):
        await client.post(f"{COMFY}/interrupt", timeout=10)
        await client.post(f"{COMFY}/queue", json={"delete": foreign}, timeout=10)
        await asyncio.sleep(0.5)
    if not (await _comfy_queue(client) or {"running": [], "pending": []})["running"]:
        LOGGER.warning("Dropped dead ComfyUI queue entries %s (GPU idle for %.0fs)", foreign, guard.idle_for())
        return {"cleared": foreign, "restarted": False, "reason": "GPU 长时间空闲，判定为残留任务"}
    if supervisor is not None:
        LOGGER.error("ComfyUI still lists %s after a delete; restarting the server", foreign)
        if supervisor.restart():
            return {"cleared": foreign, "restarted": True, "reason": "队列项无法删除，已重启 ComfyUI"}
    return {"cleared": [], "restarted": False, "reason": "队列项仍在，且无法重启"}


async def _comfy_queue(client) -> dict | None:
    try:
        response = await client.get(f"{COMFY}/queue", timeout=5)
        response.raise_for_status()
        queue = response.json()
    except (httpx.HTTPError, OSError, ValueError):
        return None
    return {"running": [pid for pid in (ImageJobMonitor._prompt_id_of(entry) for entry in queue.get("queue_running") or []) if pid],
            "pending": [pid for pid in (ImageJobMonitor._prompt_id_of(entry) for entry in queue.get("queue_pending") or []) if pid]}


async def _all_finished(client, prompt_ids: list[str]) -> bool:
    for prompt_id in prompt_ids:
        try:
            response = await client.get(f"{COMFY}/history/{prompt_id}", timeout=10)
            response.raise_for_status()
            if prompt_id not in (response.json() or {}):
                return False
        except (httpx.HTTPError, OSError, ValueError):
            return False
    return True


class ImageJobMonitor:
    """Owns the single running image job and the persisted result history."""

    def __init__(self, client, mode_loader, supervisor: ComfySupervisor | None = None,
                 gpu: GpuMonitor | None = None, enhancer: prompt_enhance.PromptEnhancer | None = None):
        self.client = client
        self.mode_loader = mode_loader
        # Prompt optimisation runs on whichever model is selected, loaded on demand. It used to
        # be a bare call to the language-model port with a comment claiming "Qwen is still loaded
        # in the user's last mode" — true only when the owner had just come from the chat view,
        # and never true from the 生图 view, where the model has been released to ComfyUI.
        # The loader is read through this object so a caller that replaces `mode_loader` (tests,
        # and the gateway if it ever re-binds) is the one the optimiser uses.
        self.enhancer = enhancer or prompt_enhance.PromptEnhancer(
            client, lambda mode: self.mode_loader(mode))
        self.supervisor = supervisor or ComfySupervisor(client)
        self.gpu = gpu or GpuMonitor()
        self.job: ImageJob | None = None
        # One accepted-but-not-yet-started job. A generation here costs 60-80 seconds, of
        # which the last ~40 are the language model being swapped back in; refusing every
        # tap during that tail is what made a shared visitor read the view as locked. The
        # job is accepted, told it is waiting, and started the moment this one settles.
        self.pending: dict | None = None
        self.task: asyncio.Task | None = None
        self.history = _load_history()
        self.history_lock = asyncio.Lock()
        self.pipeline = image_pipelines.DEFAULT_PIPELINE
        # One verdict holder for "ComfyUI says busy but the GPU has been idle".
        self.queue_guard = ComfyQueueGuard()
        # Drop a queue entry that is provably dead (GPU idle) instead of waiting for
        # a generation timeout; settings.json / comfy_clear_stale_on_switch controls it.
        self.clear_stale_comfy = True
        # model_info() 被面板/HUD 以 4 Hz 轮询，而它每次都会读一遍 workflow 覆盖文件并 JSON 解析。
        # 用 (当前管线, 覆盖文件指纹) 作缓存键：文件没变就直接返回上一次的结果。
        self._model_info: dict | None = None
        self._model_info_key: tuple | None = None

    # -- public API -----------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def _stalled(self) -> bool:
        """True when a job has produced no progress for far longer than it should.

        A killed task or a wedged ComfyUI must not leave the gateway reporting
        "switching" forever: the mode buttons stay disabled in that state.
        """
        job = self.job
        if not self.busy or job is None:
            return False
        last = job.updated or job.started
        return (time.time() - last) > STALL_SECONDS

    def progress(self) -> dict:
        stalled = self._stalled()
        return {
            "job": self.job.snapshot(stalled=stalled) if self.job else None,
            # `busy` is what the UI may trust; `monitor_busy` is the raw task state.
            "busy": self.busy and not stalled,
            "monitor_busy": self.busy,
            "stalled": stalled,
            "history_count": len(self.history),
            "model": self.model_info(),
        }

    def snapshot(self) -> dict | None:
        return self.job.snapshot(stalled=self._stalled()) if self.job else None

    def model_info(self) -> dict:
        key = (self.pipeline, image_comfy.overrides_stamp())
        if self._model_info is not None and self._model_info_key == key:
            return self._model_info
        pipeline = image_pipelines.pipeline(self.pipeline)
        overrides = image_comfy.pipeline_overrides(pipeline.key)
        info = {
            "pipeline": pipeline.key,
            "pipeline_name": pipeline.name,
            "nsfw": pipeline.nsfw,
            "summary": pipeline.summary,
            "notes": pipeline.notes,
            "family": pipeline.family,
            "defaults": dict(pipeline.defaults),
            # Not a fixed string: the optimiser runs on the selected profile, so the panel has to
            # ask which one that is (it said "Qwen3.8-27B 本地" even with the 9B selected).
            "enhancer": self.enhancer.describe()["label"] + " · 本地",
            "backend": "ComfyUI 127.0.0.1:8188",
        }
        # The model that will actually load, resolved the same way the graph does.
        for node in pipeline.nodes:
            for field in node.fields:
                if field.choices in {"checkpoints", "diffusion_models"}:
                    try:
                        info[field.input] = overrides.get(node.id, {}).get(field.input) or image_comfy._default_model(pipeline.key, field.input)
                    except RuntimeError:
                        info[field.input] = None
                    break
        info["unet"] = info.get("ckpt_name") or info.get("unet_name") or "-"
        info["text_encoder"] = next((name for name in info.values() if isinstance(name, str) and ("clip" in name or "t5" in name.lower())), "-")
        loras = [overrides.get(node, {}).get("lora_name") for node in ("11", "12") if overrides.get(node, {}).get("lora_name") not in (None, "None", "")]
        info["loras"] = loras
        active = set(loras)
        info["presets"] = [
            {"key": preset.key, "label": preset.label, "role": preset.role}
            for preset in image_pipelines.presets_for(pipeline.key) if preset.lora in active
        ]
        info["ready"], info["missing"] = self.pipeline_ready(pipeline.key)
        self._model_info, self._model_info_key = info, key
        return info

    # -- one-click LoRAs -------------------------------------------------------
    def presets(self, pipeline_key: str | None = None) -> list[dict]:
        """One-click LoRAs for a pipeline, with availability and active state."""
        key = pipeline_key or self.pipeline
        available = set(image_comfy.list_models("loras"))
        overrides = image_comfy.pipeline_overrides(key)
        active = {values.get("lora_name") for values in overrides.values() if isinstance(values, dict)}
        return [preset.describe(available=preset.lora in available, active=preset.lora in active)
                for preset in image_pipelines.presets_for(key)]

    def apply_preset(self, pipeline_key: str, preset_key: str) -> dict:
        """Put a preset's LoRA into its role's slot and persist the workflow file."""
        pipeline = image_pipelines.pipeline(pipeline_key)
        preset = image_pipelines.lora_preset(preset_key)
        if preset is None or preset not in image_pipelines.presets_for(pipeline.key):
            raise HTTPException(404, f"{pipeline.name} 没有这个 LoRA 预设：{preset_key}")
        slots = image_pipelines.preset_slots(pipeline.key)
        if not slots:
            raise HTTPException(400, f"{pipeline.name} 不支持 LoRA 叠加")
        if preset.lora not in set(image_comfy.list_models("loras")):
            raise HTTPException(409, f"LoRA 文件尚未下载：{preset.lora}（可运行 deployment/fetch_image_models.py）")
        overrides = image_comfy.pipeline_overrides(pipeline.key)
        self._drop_presets(overrides, role=preset.role)
        slot = slots.get(preset.role) or sorted(slots.values())[0]
        overrides.setdefault(slot, {}).update({
            "lora_name": preset.lora,
            "strength_model": preset.strength,
            "strength_clip": preset.strength,
        })
        # Sampler and scheduler live in the workflow file, not in the composer, so
        # the accelerator has to persist the pair that makes it converge.
        for name in image_pipelines.SAMPLER_INPUTS:
            value = preset.params.get(name)
            target = image_pipelines.param_node(pipeline.key, name) if value else None
            if target is None:
                continue
            overrides.setdefault(target[0], {})[target[1]] = value
        clean = image_comfy.validate_overrides(pipeline.key, overrides)
        image_comfy.save_pipeline_overrides(pipeline.key, clean)
        return {"pipeline": pipeline.key, "applied": preset.describe(available=True, active=True),
                "params": dict(preset.params), "overrides": clean,
                "presets": self.presets(pipeline.key),
                "workflow": self.workflow({"prompt": ""}, pipeline.key)}

    # -- one-click workflows ---------------------------------------------------
    def workflows(self, pipeline_key: str | None = None) -> list[dict]:
        """Every recipe with availability, active state and what it needs."""
        current = pipeline_key or self.pipeline
        by_pipeline: dict[str, dict[str, dict]] = {}
        for key in {entry.pipeline for entry in image_pipelines.workflows_for_all()} | {current}:
            by_pipeline[key] = {preset["key"]: preset for preset in self.presets(key)}
        entries = []
        for entry in image_pipelines.workflows_for_all():
            ready, _missing = self.pipeline_ready(entry.pipeline)
            presets = by_pipeline.get(entry.pipeline, {})
            active_presets = {key for key, preset in by_pipeline.get(current, {}).items() if preset["active"]}
            presets_available = all(presets.get(key, {}).get("available", False) for key in entry.presets)
            active = (entry.pipeline == current and set(entry.presets) == active_presets and presets_available)
            described = entry.describe(available=ready, active=bool(active))
            described["presets_available"] = presets_available
            entries.append(described)
        return entries

    def apply_workflow(self, workflow_key: str) -> dict:
        """Switch pipeline, LoRA presets and parameters together."""
        entry = image_pipelines.workflow(workflow_key)
        if entry is None:
            raise HTTPException(404, f"未知的工作流：{workflow_key}")
        pipeline = image_pipelines.pipeline(entry.pipeline)
        ready, missing = self.pipeline_ready(pipeline.key)
        if not ready:
            raise HTTPException(409, f"{pipeline.name} 还缺少模型文件（{'、'.join(missing)}），无法切换。")
        self.clear_presets(pipeline.key)
        applied: list[str] = []
        for preset_key in entry.presets:
            payload = self.apply_preset(pipeline.key, preset_key)
            applied.append(payload["applied"]["key"])
        self.pipeline = pipeline.key
        return {
            "workflow": entry.describe(available=True, active=True),
            "pipeline": pipeline.key,
            "presets": applied,
            "params": dict(entry.params),
            "model": self.model_info(),
            "workflows": self.workflows(pipeline.key),
        }

    def clear_presets(self, pipeline_key: str, preset_key: str | None = None) -> dict:
        """Remove one preset (or every preset) from the saved workflow file."""
        pipeline = image_pipelines.pipeline(pipeline_key)
        preset = image_pipelines.lora_preset(preset_key) if preset_key else None
        if preset_key and preset is None:
            raise HTTPException(404, f"未知的 LoRA 预设：{preset_key}")
        overrides = image_comfy.pipeline_overrides(pipeline.key)
        self._drop_presets(overrides, role=preset.role if preset else None)
        # An accelerator also owns its sampler and scheduler, so clearing it puts
        # those back to the pipeline default unless the user changed them by hand.
        if preset is None or preset.role == "speed":
            self._drop_preset_sampler(overrides, pipeline.key)
        clean = image_comfy.validate_overrides(pipeline.key, overrides)
        image_comfy.save_pipeline_overrides(pipeline.key, clean)
        # Only an accelerator (or a full reset) changes the composer parameters;
        # removing a quality LoRA leaves the step count alone.
        restores = preset is None or preset.role == "speed"
        return {"pipeline": pipeline.key, "cleared": preset.key if preset else "all",
                "params": dict(pipeline.defaults) if restores else {}, "overrides": clean,
                "presets": self.presets(pipeline.key),
                "workflow": self.workflow({"prompt": ""}, pipeline.key)}

    @staticmethod
    def _drop_presets(overrides: dict, role: str | None = None) -> None:
        """Drop preset LoRAs from every slot, so one role never stacks twice."""
        for node_id, values in list(overrides.items()):
            if not isinstance(values, dict):
                continue
            preset_role = image_pipelines.preset_role_of(values.get("lora_name"))
            if preset_role and (role is None or preset_role == role):
                overrides.pop(node_id)

    @staticmethod
    def _drop_preset_sampler(overrides: dict, pipeline_key: str) -> None:
        """Remove sampler/scheduler edits that came from an accelerator preset."""
        proposed = image_pipelines.preset_sampler_values(pipeline_key)
        for name in image_pipelines.SAMPLER_INPUTS:
            target = image_pipelines.param_node(pipeline_key, name)
            if target is None:
                continue
            node_id, input_name = target
            values = overrides.get(node_id)
            if isinstance(values, dict) and str(values.get(input_name)) in proposed[name]:
                values.pop(input_name, None)
                if not values:
                    overrides.pop(node_id, None)

    @staticmethod
    def pipeline_ready(pipeline_key: str) -> tuple[bool, list[str]]:
        """Whether every file this pipeline needs is present, and what is missing.

        The panel marks a pipeline that cannot run yet, instead of letting the
        first generation fail deep inside ComfyUI.
        """
        missing: list[str] = []
        for node in image_pipelines.pipeline(pipeline_key).nodes:
            for field in node.fields:
                if field.choices not in {"checkpoints", "diffusion_models", "unet_gguf", "text_encoders", "vae"}:
                    continue
                if image_comfy._choose_model(pipeline_key, field.input) is None:
                    folder = image_comfy._choice_folder(pipeline_key, field.input)
                    if folder not in missing:
                        missing.append(folder)
        return (not missing), missing

    def start(self, body: ImageGenerateRequest, previous_mode: str, reference_names: list[str],
              session: str = "") -> dict:
        """Accept one job: run it now, or hold it in the single waiting slot.

        One generation on this machine is a serial GPU cycle (ComfyUI's models out, the
        language model in for the prompt work, then back again), so two pictures cannot run
        at once. But the cycle's last 36-46 seconds are only the language model being put
        back, and the picture is already on screen: refusing every tap in that window is
        what a shared visitor read as "locked". So the second request is accepted, told it
        is waiting, and starts by itself when the first one settles.
        """
        job = self._build_job(body, reference_names, session)
        if self.busy:
            if self.pending is not None:
                raise HTTPException(409, "已有一张排在队列里了，等它开工再提交。")
            job.state = "queued_behind"
            job.phase = "queued_behind"
            job.queue_ahead = 1
            job.note("queued_behind", self.job.id if self.job else "")
            self.pending = {"job": job, "body": body, "previous_mode": previous_mode}
            return job.snapshot()
        return self._launch(job, body, previous_mode)

    def _build_job(self, body: ImageGenerateRequest, reference_names: list[str],
                   session: str) -> ImageJob:
        """Validate one request and build its job (same 400/409 refusals as before)."""
        job_id = f"img-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
        pipeline_key = body.pipeline or self.pipeline
        pipeline = image_pipelines.pipeline(pipeline_key)
        # An image upscaler samples from an uploaded image, so the panel's prompt
        # field cannot stand in for a missing reference. The video twin takes its
        # source from the video route's selection instead, and the graph builder is
        # what refuses a run with no clip chosen.
        if pipeline.references_required and not reference_names and pipeline.key != "seedvr2_video":
            raise HTTPException(400, "放大管线需要一张参考图：请先在“参考图”里选一张图片。")
        defaults = pipeline.defaults
        # "只降噪不放大" is a panel-side route (it calls /local/video/denoise-source), so a
        # job that asks for it here would silently upscale instead: refuse, loudly.
        if defaults.get("skip_upscale"):
            raise HTTPException(409, "这条配方只做降噪、不跑放大：请在视频界面用它，"
                                     "接口是 POST /local/video/denoise-source。")
        if body.scale is not None and not 1.0 <= float(body.scale) <= 4.0:
            raise HTTPException(400, "放大倍率需要在 1.0 到 4.0 之间")
        params = {
            "pipeline": pipeline.key,
            "width": body.width or defaults["width"],
            "height": body.height or defaults["height"],
            "steps": body.steps or defaults["steps"],
            "cfg": body.cfg if body.cfg is not None else defaults["cfg"],
            "denoise": body.denoise if body.denoise is not None else float(defaults.get("denoise", 1.0)),
            "seed": -1 if body.seed is None else body.seed,
            "scale": float(body.scale) if body.scale is not None else defaults.get("scale"),
            # The strength of the CPU denoise pass the "1.25× + 降噪" upscale recipe runs
            # after SeedVR2. Independent of the sampler's `denoise` above.
            "video_denoise": (float(body.video_denoise) if body.video_denoise is not None
                              else defaults.get("video_denoise")),
            "upscale_strength": (float(body.upscale_strength)
                                 if body.upscale_strength is not None
                                 else defaults.get("upscale_strength")),
            # Video upscaling writes a clip, not a still, so it gets its own folder
            # instead of landing among the generated images.
            "filename_prefix": body.filename_prefix or (
                "video/upscaled" if pipeline.key == "seedvr2_video" else "qwen/generated"),
        }
        job = ImageJob(job_id, body.prompt.strip(), body.negative_prompt.strip(), params, reference_names)
        job.enhanced = bool(body.enhance_prompt)
        # Who asked for this picture. One monitor serves the whole machine, so this is what keeps
        # the owner's prompt out of a visitor's progress view (and the visitor's out of the
        # owner's) and what decides which saved results a caller may read back.
        job.session = session
        return job

    def _launch(self, job: ImageJob, body: ImageGenerateRequest, previous_mode: str) -> dict:
        self.job = job
        job.state = "running"
        job.queue_ahead = 0
        job.note("accepted", image_pipelines.pipeline(job.params["pipeline"]).name)
        self.task = asyncio.create_task(
            self._run(job, body, previous_mode),
            name=f"image-job-{job.id}",
        )
        return job.snapshot()

    def _start_pending(self) -> None:
        """Start the one job that was accepted while the previous one was still running."""
        waiting = self.pending
        if waiting is None:
            return
        self.pending = None
        job = waiting["job"]
        job.note("queue_started")
        try:
            self._launch(job, waiting["body"], waiting["previous_mode"])
        except Exception:  # noqa: BLE001 - a failed hand-off must not kill the gateway
            LOGGER.warning("Queued image job could not start", exc_info=True)

    def pending_for(self, session: str, owner: bool) -> dict | None:
        """The waiting job, as this caller may see it (None when it belongs to someone else)."""
        waiting = self.pending
        if waiting is None:
            return None
        job = waiting["job"]
        if not owner and job.session != session:
            return None
        return job.snapshot(stalled=self._stalled())

    async def cancel(self) -> dict:
        job = self.job
        if not self.busy or job is None:
            # Nothing is rendering: the only thing left to cancel is the job that was
            # accepted into the waiting slot.
            waiting = self.pending
            if waiting is None:
                raise HTTPException(409, "当前没有可取消的生图任务。")
            self.pending = None
            queued = waiting["job"]
            queued.state = "cancelled"
            queued.phase = "cancelled"
            queued.error = "已取消"
            queued.finished = time.time()
            queued.note("cancelled")
            return queued.snapshot()
        job.note("cancel_requested")
        if job.prompt_id:
            with contextlib.suppress(httpx.HTTPError, OSError):
                await self.client.post(f"{COMFY}/interrupt", json={"prompt_id": job.prompt_id}, timeout=15)
            with contextlib.suppress(httpx.HTTPError, OSError):
                await self.client.post(f"{COMFY}/queue", json={"delete": [job.prompt_id]}, timeout=15)
        task, self.task = self.task, None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # The runner marks its own state when it observes the cancellation, but a
        # cancelled task may stop before that point; keep the reported result honest.
        if job.state == "running":
            job.state = "cancelled"
            job.phase = "cancelled"
            job.error = "已取消"
        job.finished = job.finished or time.time()
        # A cancelled run can stop before its own finally reaches the hand-off (the persist
        # await is itself cancellable), which would leave the waiting job in the slot with
        # nothing running. Hand the card over here as well; it is a no-op if the finally did.
        self._start_pending()
        return job.snapshot()

    async def clear_history(self) -> int:
        async with self.history_lock:
            removed = len(self.history)
            self.history = []
            _write_history(self.history)
            return removed

    async def remove_history(self, job_id: str) -> bool:
        async with self.history_lock:
            remaining = [entry for entry in self.history if entry.get("id") != job_id]
            if len(remaining) == len(self.history):
                return False
            self.history = remaining
            _write_history(self.history)
            return True

    async def comfy_status(self) -> dict:
        """Queue depth and reachability of ComfyUI, without touching the model."""
        try:
            response = await self.client.get(f"{COMFY}/queue", timeout=3)
            response.raise_for_status()
            queue = response.json()
        except (httpx.HTTPError, OSError, ValueError):
            return {"reachable": False, "running": 0, "pending": 0}
        running = len(queue.get("queue_running") or [])
        pending = len(queue.get("queue_pending") or [])
        return {"reachable": True, "running": running, "pending": pending}

    async def gpu_snapshot(self) -> dict | None:
        """Live VRAM reading for the monitor, independent of a running job."""
        statistics = await self.gpu.sample()
        if statistics is None:
            return None
        self.gpu.observe(statistics)
        return self.gpu.snapshot()

    def workflow(self, params: dict, pipeline_key: str | None = None) -> dict:
        """The graph that would run next, with the composer values applied.

        The template carries each node's resolved value so the editor shows what
        will actually execute. Linked parameters are filtered out of the stored
        edits by ``image_comfy.load_overrides``, so a stale seed or step count
        written by an older version can never override the current request.
        """
        key = pipeline_key or self.pipeline
        graph = self._resolved_graph(params, key)
        pipeline = image_pipelines.pipeline(key)
        return {
            "pipeline": key,
            "pipelines": image_pipelines.describe_all(),
            "pipeline_name": pipeline.name,
            "summary": pipeline.summary,
            "notes": pipeline.notes,
            # An upscaler samples from an uploaded image: the panel hides the prompt
            # and asks for a reference instead of a text description.
            "references_required": pipeline.references_required,
            "defaults": dict(pipeline.defaults),
            "template": self._template_with_values(graph["prompt"], key),
            "sync": image_comfy.workflow_labels(key),
            "graph": image_comfy.describe_graph(graph, key),
            # Only a job that is actually executing counts as "running": the last
            # finished job must not keep the workflow card showing its own graph
            # instead of the one the next generation will use.
            "running": self.job.graph if (self.job and self.busy) else [],
            "overrides": image_comfy.pipeline_overrides(key),
            "models": {kind: image_comfy.list_models(kind) for kind in ("checkpoints", "diffusion_models", "unet_gguf", "text_encoders", "vae", "loras")},
            "presets": self.presets(key),
            "preset_slots": image_pipelines.preset_slots(key),
            "default_negative": image_comfy._default_negative(),
        }

    def _template_with_values(self, graph: dict, pipeline_key: str) -> list[dict]:
        overrides = image_comfy.pipeline_overrides(pipeline_key)
        template = []
        for spec in image_comfy.workflow_template(pipeline_key):
            node = graph.get(spec["id"]) or {}
            inputs = node.get("inputs") or {}
            fields = []
            for field in spec["fields"]:
                stored = overrides.get(spec["id"], {}).get(field["input"])
                value = stored if stored is not None else inputs.get(field["input"], field.get("value"))
                fields.append({**field, "value": value})
            template.append({**spec, "fields": fields})
        return template

    def _resolved_graph(self, params: dict, pipeline_key: str) -> dict:
        """Build the graph the next run would use: request parameters plus saved edits.

        Width, height, steps, CFG, denoise and seed are request parameters (the
        panel's own fields), while every other saved node edit is applied on top.
        """
        key = pipeline_key or self.pipeline
        defaults = image_pipelines.pipeline(key).defaults
        # The builder may be called before a reference is chosen (the panel renders
        # the graph first), so an empty reference list is fine here.
        return image_comfy.build_graph(
            key,
            params.get("prompt") or "preview",
            negative_prompt=params.get("negative_prompt") or image_comfy._default_negative(),
            references=[],
            width=int(params.get("width") or defaults["width"] or 0),
            height=int(params.get("height") or defaults["height"] or 0),
            steps=int(params.get("steps") or defaults["steps"]),
            cfg=float(params.get("cfg") if params.get("cfg") is not None else defaults["cfg"]),
            denoise=float(params.get("denoise") if params.get("denoise") is not None else 1.0),
            seed=int(params.get("seed") if params.get("seed") is not None else -1),
            filename_prefix=str(params.get("filename_prefix") or "qwen/generated"),
            overrides=image_comfy.pipeline_overrides(key),
        )

    # -- the generation cycle -------------------------------------------------
    async def _run(self, job: ImageJob, body: ImageGenerateRequest, previous_mode: str) -> None:
        listener: asyncio.Task | None = None
        phase = "init"
        try:
            phase = "starting_comfy"
            job.mark("total")
            job.phase = phase
            job.note(phase)
            # ComfyUI is started on demand and in the background, before the
            # language model is unloaded, so a start failure costs no model swap.
            job.mark("comfy")
            state = await self.supervisor.ensure_running(on_note=job.note)
            job.finish_mark("comfy", "comfy_start")
            job.note("comfy_ready", str(state.get("device") or ""))
            self.gpu.begin(await self.gpu.sample())

            phase = "enhancing"
            job.phase = phase
            job.note(phase)
            job.mark("enhance")
            if body.enhance_prompt:
                # The prompt work runs on the model the owner has selected — loaded here, on
                # demand, because entering this view released the language model for ComfyUI.
                final_prompt, job.enhancer = await self.enhancer.enhance(
                    body.prompt, body.negative_prompt, kind="image")
            else:
                final_prompt = body.prompt.strip()
                job.enhancer = {**self.enhancer.describe(), "used": False, "reason": "已关闭提示词优化"}
            job.finish_mark("enhance", "prompt")
            job.final_prompt = final_prompt

            phase = "loading_model"
            job.phase = phase
            job.note(phase)
            await self.mode_loader("image")
            # The language model is gone now: whatever VRAM the image job uses
            # from here on is attributable to the generation itself.
            self.gpu.release(await self.gpu.sample())
            phase = "queued"
            job.note_queue_started()

            # Saved workflow edits (model files, LoRAs, sampler, save prefix) ride
            # on top of the request parameters for the selected pipeline. Every
            # required file is checked first so an incomplete pipeline fails with
            # an actionable message instead of a ComfyUI validation error.
            ready, missing = self.pipeline_ready(job.params["pipeline"])
            if not ready:
                pipeline_name = image_pipelines.pipeline(job.params["pipeline"]).name
                raise RuntimeError(
                    f"{pipeline_name} 还缺少模型文件（{ '、'.join(missing) }），"
                    f"请下载对应模型到 {image_comfy.MODELS_ROOT} 下的对应目录，或在“工作流”卡片里选择已有文件。"
                )
            built = image_comfy.build_graph(
                job.params["pipeline"],
                final_prompt,
                negative_prompt=job.negative_prompt,
                references=job.references,
                # An upscaler takes its size from the image, so the request's
                # canvas size only applies to the pipelines that make one.
                width=int(job.params["width"] or 0),
                height=int(job.params["height"] or 0),
                steps=int(job.params["steps"]),
                cfg=float(job.params["cfg"]),
                denoise=float(job.params["denoise"]),
                seed=job.params["seed"],
                scale=job.params.get("scale"),
                filename_prefix=str(job.params.get("filename_prefix") or "qwen/generated"),
                overrides=image_comfy.pipeline_overrides(job.params["pipeline"]),
                upscale_strength=job.params.get("upscale_strength"),
            )
            graph = built["prompt"]
            job.graph = image_comfy.describe_graph(graph, job.params["pipeline"])
            job.params.update(built["parameters"])
            job.params.update(_graph_parameters(graph, job.params["pipeline"]))
            client_id = uuid.uuid4().hex
            graph = {"prompt": graph, "client_id": client_id}
            response = await self.client.post(f"{COMFY}/prompt", json=graph, timeout=30)
            response.raise_for_status()
            queued = response.json()
            job.prompt_id = queued.get("prompt_id")
            if not job.prompt_id:
                raise RuntimeError("ComfyUI 未返回 prompt_id")
            job.phase = "queued"
            job.note("queued", job.prompt_id)

            listener = asyncio.create_task(self._listen(job, client_id))
            await self._await_completion(job)
            job.finish_mark("queue")
            job.finish_mark("load")
            job.finish_mark("sampling")
            await self._collect_images(job)
            # The "1.25× + 降噪" upscale recipe finishes on the CPU: SeedVR2 has just
            # published a restored clip (more detail, a little more noise), and the ffmpeg
            # pass takes the grain back out. The numbers ride along in the job snapshot so
            # the panel can show what it actually did.
            if job.params.get("video_denoise"):
                await self._denoise_result(job)

            job.state = "completed"
            job.phase = "completed"
            job.progress = 1.0
            job.note("completed")
            self.gpu.freeze(await self.gpu.sample())
        except asyncio.CancelledError:
            job.state = "cancelled"
            job.phase = "cancelled"
            job.error = "已取消"
            job.note("cancelled")
            self.gpu.freeze(await self.gpu.sample())
        except (httpx.ConnectError, OSError) as exc:
            job.state = "error"
            job.phase = "error"
            job.error = f"ComfyUI 未启动或连接中断（{COMFY}）。" if phase in {"queued", "loading_model"} else str(exc)
            job.note("error", job.error)
            LOGGER.warning("Image job could not reach ComfyUI: %s", exc)
        except (httpx.HTTPError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            job.state = "error"
            job.phase = "error"
            job.error = str(exc)
            job.note("error", str(exc))
            LOGGER.warning("Image job failed: %s", exc, exc_info=True)
        finally:
            if listener:
                listener.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await listener
            job.finished = time.time()
            job.finish_mark("total", "total")
            # Restore the language model the user came from, then persist. The
            # phase stays on the outcome so the UI keeps reporting it.
            # 有排队作业、且**下一张不需要语言模型**（提示词优化关闭）时，才跳过恢复：
            # 那种情况下下一张不会碰语言模型，装回来（实测约 40 s）纯粹白费。
            # 下一张若开了提示词优化，它的 enhancer.ensure_model() 正好复用这次恢复装回的模型
            # （0 秒），跳过恢复只是把同一次装载挪到 enhance 里，不省时间——所以照旧恢复。
            pending_body = (self.pending or {}).get("body")
            queued_without_model = self.pending is not None and not (
                pending_body is not None and pending_body.enhance_prompt)
            if queued_without_model:
                job.note("restore_skipped_queued")
                LOGGER.info("Image job %s: queued job %s needs no language model, "
                            "skipping the restore", job.id, (self.pending or {}).get("job"))
            else:
                job.restoring = True
                job.note("restoring")
                job.mark("restore")
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self.mode_loader(previous_mode)
                job.finish_mark("restore")
                job.restoring = False
            job.progress = 1.0
            job.note("settled")
            job.finished = time.time()
            job.timings["total"] = round(job.finished - job.created, 3)
            await self._persist(job)
            # The card is free again: start whatever was accepted while this one ran. Done
            # after the persist so a waiting caller's snapshot is already the settled one.
            self._start_pending()

    async def _await_completion(self, job: ImageJob) -> None:
        deadline = time.monotonic() + GENERATION_TIMEOUT
        next_recovery_check = 0.0
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
            await self._refresh_queue(job)
            await self._refresh_gpu(job)
            job.project_step()
            # A dead entry ahead of us never finishes: judge it by the GPU and drop
            # it, otherwise this job waits until the generation timeout.
            if self.clear_stale_comfy and job.phase == "queued" and time.monotonic() >= next_recovery_check:
                next_recovery_check = time.monotonic() + 5
                outcome = await clear_dead_comfy_work(
                    self.client, self.queue_guard, own_prompt_ids=(job.prompt_id,),
                    supervisor=self.supervisor)
                if outcome["cleared"]:
                    job.note("queue_recovered", outcome["reason"])
            history = await self.client.get(f"{COMFY}/history/{job.prompt_id}", timeout=15)
            if history.status_code != 200:
                continue
            item = history.json().get(job.prompt_id)
            if not item:
                continue
            status = item.get("status", {}) or {}
            if status.get("status_str") == "error" or status.get("completed") is False and status.get("messages"):
                raise RuntimeError(self._comfy_error(status))
            if status.get("completed"):
                return
        raise RuntimeError("图像生成超时")

    @staticmethod
    def _comfy_error(status: dict) -> str:
        for kind, payload in status.get("messages") or []:
            if kind in {"execution_error", "execution_interrupted"} and isinstance(payload, dict):
                detail = payload.get("exception_message") or payload.get("node_type") or kind
                return f"ComfyUI 工作流执行失败：{detail}"
        return "ComfyUI 工作流执行失败"

    async def _refresh_queue(self, job: ImageJob) -> None:
        """Queue position and depth; also the fallback progress estimate."""
        try:
            response = await self.client.get(f"{COMFY}/queue", timeout=5)
            response.raise_for_status()
            queue = response.json()
        except (httpx.HTTPError, OSError, ValueError):
            return
        running = queue.get("queue_running") or []
        pending = queue.get("queue_pending") or []
        job.queue_running = len(running)
        job.queue_pending = len(pending)
        if job.phase in {"queued", "loading_model"}:
            for index, entry in enumerate(pending):
                if self._prompt_id_of(entry) == job.prompt_id:
                    job.queue_position = index + 1
                    break
            else:
                job.queue_position = 0 if any(self._prompt_id_of(entry) == job.prompt_id for entry in running) else None
        if any(self._prompt_id_of(entry) == job.prompt_id for entry in running):
            if job.phase == "queued":
                job.note_execution_started()
                job.phase = "sampling"
                job.note("sampling")
        elif job.phase == "sampling" and not job.step and job.max_step == 0:
            job.queue_position = 1

    async def _refresh_gpu(self, job: ImageJob) -> None:
        """VRAM total/used plus this job's own footprint, sampled every tick."""
        statistics = self.gpu.observe(await self.gpu.sample())
        self.queue_guard.observe(statistics)
        snapshot = self.gpu.snapshot()
        if snapshot:
            job.gpu = snapshot

    @staticmethod
    def _prompt_id_of(entry) -> str | None:
        if isinstance(entry, (list, tuple)) and len(entry) > 1 and isinstance(entry[1], str):
            return entry[1]
        if isinstance(entry, dict):
            return entry.get("prompt_id")
        return None

    async def _listen(self, job: ImageJob, client_id: str) -> None:
        """Follow ComfyUI's websocket for step progress and sampler previews."""
        try:
            async with websockets.connect(
                f"{COMFY_WS}?clientId={client_id}", max_size=8 * 1024 * 1024,
                open_timeout=10, close_timeout=2, ping_interval=20,
            ) as connection:
                async for message in connection:
                    if isinstance(message, bytes):
                        picture = self._binary_preview(message)
                        if picture:
                            job.preview = picture
                            job.preview_version += 1
                        continue
                    payload = json.loads(message)
                    event = payload.get("type")
                    data = payload.get("data") or {}
                    if data.get("prompt_id") and job.prompt_id and data["prompt_id"] != job.prompt_id:
                        continue
                    if event == "progress":
                        if job.sampling_started is None:
                            job.note_sampling_begun()
                        job.set_step(data.get("value") or 0, data.get("max") or 0)
                        if data.get("max") and (data.get("value") or 0) >= data["max"]:
                            job.note_sampling_finished()
                        preview = data.get("preview_image")
                        if isinstance(preview, str) and preview:
                            job.preview = preview if preview.startswith("data:") else f"data:image/png;base64,{preview}"
                            job.preview_version += 1
                    elif event == "progress_state":
                        self._apply_progress_state(job, data.get("nodes") or {})
                    elif event == "executing" and data.get("node") is None and job.phase in {"sampling", "queued"}:
                        job.phase = "decoding"
                        job.note("decoding")
        except asyncio.CancelledError:
            raise
        except (OSError, websockets.WebSocketException, ValueError, TypeError):
            # The queue/history polling path stays authoritative when the
            # websocket is unavailable; progress then falls back to elapsed time.
            LOGGER.info("ComfyUI progress websocket unavailable", exc_info=True)

    @staticmethod
    def _apply_progress_state(job: ImageJob, nodes: dict) -> None:
        for state in nodes.values():
            if not isinstance(state, dict):
                continue
            total = state.get("max") or 0
            if total and (state.get("value") or 0) >= 0:
                job.set_step(state.get("value") or 0, total)
                return

    @staticmethod
    def _binary_preview(message: bytes) -> str | None:
        # ComfyUI binary frames are a 4-byte event type followed by image bytes.
        if len(message) <= 8 or len(message) > PREVIEW_LIMIT:
            return None
        payload = message[8:]
        head = payload[:4]
        mime = "image/jpeg" if head[:3] == b"\xff\xd8\xff" else "image/png" if head[:4] == b"\x89PNG" else None
        if mime is None:
            return None
        return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"

    async def _collect_images(self, job: ImageJob) -> None:
        response = await self.client.get(f"{COMFY}/history/{job.prompt_id}", timeout=20)
        response.raise_for_status()
        item = (response.json() or {}).get(job.prompt_id) or {}
        images: list[str] = []
        for node in (item.get("outputs") or {}).values():
            for image in node.get("images") or []:
                if isinstance(image, dict) and image.get("filename"):
                    images.append(comfy_image_url(image))
                elif isinstance(image, str):
                    images.append(image)
        job.images = images

    async def _denoise_result(self, job: ImageJob) -> None:
        """Run the ffmpeg denoise pass over whatever ComfyUI just published.

        The upscale recipe that asks for this publishes a clip with more detail and a
        little more noise; the pass writes the denoised cut beside it and becomes the
        job's result, with the measured before/after numbers attached.
        """
        strength = float(job.params["video_denoise"])
        response = await self.client.get(f"{COMFY}/history/{job.prompt_id}", timeout=20)
        response.raise_for_status()
        item = (response.json() or {}).get(job.prompt_id) or {}
        produced: Path | None = None
        for node in (item.get("outputs") or {}).values():
            for key in ("gifs", "videos", "images"):
                for entry in node.get(key) or []:
                    if not isinstance(entry, dict) or not entry.get("filename"):
                        continue
                    candidate = (image_comfy.COMFY_ROOT / "ComfyUI" / "output"
                                 / (entry.get("subfolder") or ""))
                    candidate = candidate / entry["filename"]
                    if candidate.is_file() and candidate.suffix.lower() in (
                            ".mp4", ".mkv", ".webm", ".mov", ".png", ".jpg", ".jpeg", ".webp"):
                        produced = candidate
        if produced is None:
            raise RuntimeError("放大任务没有产出可降噪的文件")
        job.phase = "denoising"
        job.note("denoising", f"nlmeans s={strength:g}")
        if produced.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            result = await asyncio.to_thread(video_denoise.run_image, produced, strength)
        else:
            result = await asyncio.to_thread(video_denoise.run, produced,
                                             video_denoise.DEFAULT_MODE, strength, "-denoised")
        target = Path(result["target"])
        job.denoise = {**result, "note": video_denoise.describe(result),
                       "video": comfy_image_url({"filename": target.name,
                                                 "subfolder": target.parent.name,
                                                 "type": "output"})}
        job.images = [job.denoise["video"]]
        LOGGER.info("upscale denoise: %s → %s (%s)", produced.name, target.name,
                    job.denoise["note"])

    async def _persist(self, job: ImageJob) -> None:
        async with self.history_lock:
            self.history = [entry for entry in self.history if entry.get("id") != job.id]
            self.history.insert(0, job.history_record())
            del self.history[HISTORY_LIMIT:]
            _write_history(self.history)


async def image_metadata(client, monitor: ImageJobMonitor) -> dict:
    """Pipelines, model files, ComfyUI state and GPU for the generate view."""
    queue = await monitor.comfy_status()
    supervisor = await monitor.supervisor.status()
    models = {}
    for kind in ("checkpoints", "diffusion_models", "unet_gguf", "text_encoders", "vae", "loras"):
        models[kind] = await image_comfy.comfy_choices(client, kind)
    pipelines = []
    for key in image_pipelines.PIPELINES:
        ready, missing = ImageJobMonitor.pipeline_ready(key)
        described = image_pipelines.pipeline(key).describe(ready=ready, missing=missing)
        described["damaged"] = image_comfy.integrity_warnings(key)
        pipelines.append(described)
    return {
        "comfy": {**queue, **{key: value for key, value in supervisor.items() if key in {"managed", "version", "device", "error"}}},
        "current": monitor.model_info(),
        "pipelines": pipelines,
        "models": models,
        "gpu": await monitor.gpu_snapshot(),
        "workflow_file": str(image_comfy.WORKFLOW_PATH),
    }
