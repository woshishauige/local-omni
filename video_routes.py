"""Job control and API routes for the panel's video mode.

Kept separate from image_jobs so a ten-minute H3 run cannot disturb the image
pipeline's state, and so the two can be reasoned about independently.

The director node is a ComfyUI OUTPUT_NODE that writes master.mp4 itself and
returns its absolute path as a STRING. There is no SaveVideo wire and no IMAGE
output, so progress cannot be read from a sampler callback the way the image
pipeline does it: this monitor follows the queue and the node's own report.

It renders exactly one shot per submission, so a long film is a chain: shot 1,
then shot 2, and so on, each submitted with that shot's own duration, step count
and seed. The submission that fills the last missing shot is also the one that
assembles the clips into master.mp4.

The film is written into the save location the panel picked — ComfyUI/output/video
by default — and one project folder inside it. Because that folder may sit outside
ComfyUI's output tree, the file route serves an absolute path that still has to be
inside a folder this session is allowed to serve from.
"""
import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import string
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
import imageio_ffmpeg
from pydantic import BaseModel

import guest_policy
import guest_uploads
import image_comfy
import qwen_config
import video_context
import video_cost
import video_interpolate
import video_denoise
import video_jobs

import httpx

LOGGER = logging.getLogger("qwen.video")
COMFY = image_comfy.COMFY_URL
OUTPUT_ROOT = image_comfy.COMFY_ROOT / "ComfyUI" / "output"

# A test process must never spend the owner's GPU. The image suite always talks to a fake
# transport, but the video integration tests drive this real route — and on 2026-09-21 a
# suite run queued **five 20-step H3 renders** onto the live ComfyUI (their storyboard was
# the test's own `{"prompt": "x"}`, ~32 minutes of card time nobody was waiting for).
# A deliberate live probe opts in explicitly.
TEST_RENDER_ENV = "QWEN_ALLOW_TEST_RENDERS"
TEST_RENDER_REFUSAL = ("测试进程不会真的提交视频渲染：一次 H3 片段就是几十分钟的显卡。"
                       f"要跑真实探针请设 {TEST_RENDER_ENV}=1。")


def renders_are_refused() -> bool:
    """Whether this process is a test run that has not opted into real renders."""
    if os.environ.get(TEST_RENDER_ENV) == "1":
        return False
    return bool(qwen_config.tests_are_running())

# Where a film is written when the panel does not say otherwise, and where the
# upscale recipes read an uploaded clip from. Both are view preferences that live
# in memory: the recipes themselves are the shared workflow folders.
DEFAULT_OUTPUT_ROOT = OUTPUT_ROOT / "video"
_OUTPUT: dict[str, str | None] = {"root": None}
_SOURCE: dict[str, str | None] = {"path": None}


class Identity:
    """Who is calling: the machine's owner, or one logged-in shared device.

    The token is the device's own session token, which is what keeps one visitor's
    uploads and clip selection separate from another's.
    """

    __slots__ = ("is_owner", "token")

    def __init__(self, is_owner: bool, token: str):
        self.is_owner = bool(is_owner)
        self.token = token or ""


def _deny_identity(request: Request) -> Identity:
    """Default resolver: nobody is anybody.

    Replaced by ``install()``. Until then every route stays owner-only, so a
    misconfigured or half-installed router fails closed rather than open.
    """
    return Identity(False, "")


# The owner's own label, for the places that are not describing a caller: a job's film
# is produced by an owner-run generation, so its snapshot is always owner-visible.
OWNER_IDENTITY = Identity(True, guest_uploads.OWNER_SESSION)


_IDENTITY = _deny_identity
_POLICY_STORE = None


def set_identity_resolver(resolver) -> None:
    """Bind the gateway's identity resolver (loopback/admin => owner)."""
    global _IDENTITY
    _IDENTITY = resolver if callable(resolver) else _deny_identity


def set_policy_store(store) -> None:
    """Bind the upload-policy store: an instance, or a factory resolved per call.

    A factory is accepted because the conventional path is derived from
    ``qwen_config.ROOT``, which a test process replaces after this module is imported.
    Resolving through a callable keeps one switched-off machine's records out of a test
    run, and keeps tests out of the machine's real records.
    """
    global _POLICY_STORE
    _POLICY_STORE = store


def _policy():
    if _POLICY_STORE is None:
        return guest_uploads.store()
    return _POLICY_STORE() if callable(_POLICY_STORE) else _POLICY_STORE


def policy_store():
    """The live upload-policy store, for callers outside this router.

    Exposed so the sharing panel writes to the same object the routes read from: a
    second store instance on the same file would let the panel report a switch that
    the routes are not honouring.
    """
    return _policy()


def _identity(request: Request) -> Identity:
    """Identity of one call, or a non-owner when no resolver is installed."""
    try:
        resolved = _IDENTITY(request)
    except Exception:  # noqa: BLE001 - an unresolvable caller is not the owner
        LOGGER.warning("video: identity resolution failed; treating caller as a guest",
                       exc_info=True)
        return Identity(False, "")
    if isinstance(resolved, Identity):
        return resolved
    owner, token = resolved
    return Identity(bool(owner), str(token))


def _caller(request: Request | None) -> Identity:
    """Identity for a handler that may be driven without an HTTP request.

    ``None`` means the handler was called directly rather than through the route table,
    which is how its own unit tests exercise it; the route guard has not run in that
    case, and every handler that accepts ``None`` here is owner-only, so the owner is
    the honest answer. A real request always carries its own identity.
    """
    return OWNER_IDENTITY if request is None else _identity(request)

# Routes a shared visitor may call, by the feature the owner has opened for them: see
# GUEST_ROUTES below. (The old four-entry whitelist — "upload a clip, read back your own clip" —
# is what the owner asked to replace.)


# Which guest feature opens which video route; a route absent from this map stays owner-only.
#
# The map replaced a four-entry whitelist ("upload, and read back your own clip") because the
# owner asked for a shared session that can actually use the machine: a visitor may now generate,
# follow and cancel their own job, and post-process their own files. What is not here is the
# machine's own surface — the save-location picker (``/local/video/folders``) would hand a
# visitor the host's folder tree, and the upload-policy routes belong to the sharing panel.
GUEST_ROUTES = {
    ("POST", "/local/video/upload"): "storage",
    ("GET", "/local/video/source"): "storage",
    ("POST", "/local/video/source"): "storage",
    # Serves both an uploaded clip and a finished film, so either feature opens it.
    ("GET", "/local/video/file"): ("media", "storage"),
    ("GET", "/local/video/recipes"): "media",
    ("GET", "/local/video/output"): "media",
    ("POST", "/local/video/generate"): "media",
    ("GET", "/local/video/progress"): "media",
    ("GET", "/local/video/history"): "media",
    ("POST", "/local/video/cancel"): "media",
    ("GET", "/local/video/comfy"): "media",
    ("POST", "/local/video/interpolate"): "media",
    ("GET", "/local/video/interpolate/progress"): "media",
    ("POST", "/local/video/concat"): "media",
    ("POST", "/local/video/denoise"): "media",
    ("POST", "/local/video/denoise-source"): "media",
}


class GuestRouteGuard:
    """The permission decision for one video route.

    Written as a FastAPI dependency because the router owns its own routes: the
    gateway's owner-only helper cannot be called at the top of a handler that the
    router declares. A route is callable by a visitor only when the owner has opened the
    feature that route belongs to, so the switch is a real second gate rather than
    something the panel merely displays.
    """

    def __init__(self, method: str, path: str, owner_dependency=None):
        self.method = method
        self.path = path
        self.owner_dependency = owner_dependency

    def __call__(self, request: Request):
        identity = _identity(request)
        if identity.is_owner:
            return identity
        feature = GUEST_ROUTES.get((self.method, self.path))
        if feature:
            allowed = (guest_policy.allows_any(*feature) if isinstance(feature, tuple)
                       else guest_policy.allows(feature))
            if allowed:
                return identity
            if identity.token:
                # A logged-in shared device whose feature the owner has closed: name the switch,
                # so the panel's message is "the owner closed this" rather than the generic
                # "本机管理员" wording, which reads as "you may not exist here".
                raise HTTPException(403, guest_policy.refusal(
                    feature if isinstance(feature, str) else feature[0]))
        if self.owner_dependency is not None:
            # The gateway decides, so its wording and its own 403 stay authoritative.
            self.owner_dependency(request)
        raise HTTPException(403, "该操作仅限本机管理员")


def guest_root(identity: Identity) -> Path:
    """Where one visitor's own generated files live: a folder per session token.

    "Their own storage" is a physical folder, not only a record in a registry: a guest's
    uploads are tagged with its session and its *output* goes here, so the file route can fence
    a caller to its own material by path rather than by lookup.
    """
    return OUTPUT_ROOT / "guests" / (identity.token or "anonymous")[:24]



def _visible_videos(identity: Identity) -> list[str]:
    """Clips this caller may see: everything for the owner, its own uploads for a guest."""
    return _policy().visible_videos(_list_input_videos(), identity)


class ShotPlan(BaseModel):
    """One shot's own settings.

    The panel tunes these per shot because the director samples each shot in its
    own run. Width and height are deliberately absent: the shots are concatenated
    into one film, so the frame size has to be the same for all of them.
    """

    seconds: float = 7.5
    steps: int = 20
    # None means "pick a fresh one". A negative seed is not a value the node can
    # use — it would keep the workflow file's fixed seed and repeat the same film.
    seed: int | None = None
    # "continue" (default) pins the previous shot's tail as this shot's opening context;
    # "cut" starts it fresh, which is what a real edit between beats wants: the model then
    # does not have to snap its first frames onto a tail it is not continuing.
    continuity: str = "continue"


class VideoGenerateRequest(BaseModel):
    prompt: str = ""
    # 12 steps is now the panel's first (finest) route: the 20-step distillation entry was
    # retired from the panel, so nothing may default to a route the panel no longer offers.
    recipe: str = "video_distill12"
    shots: list[str] | None = None
    # Per-shot reference images, by ComfyUI input filename. Shot 1's list drives
    # the shot_01_ref_NN slots, and so on.
    shot_references: list[list[str]] | None = None
    shot_plan: list[ShotPlan] | None = None
    # Where the project folder is created. Empty keeps the default below; a
    # relative value is read from ComfyUI's output directory.
    output_root: str | None = None
    # Append the structure constraint (one subject, medium shot or closer, one action
    # per shot) to every shot's prompt. Prompt-level only: the graph is untouched.
    structure_guard: bool = False
    # Strength for the distillation LoRA a recipe ships with. None keeps the workflow
    # file's own value (0.85 for the two distill routes); the panel exposes it because
    # 0.85 measured 11-18% less grain than 1.0 for a small detail cost.
    lora_strength: float | None = None
    width: int | None = None
    height: int | None = None
    fps: int | None = None
    # The director writes to <output_root>/<project_name>/, so this is the folder
    # inside the save location that holds one film.
    project_name: str | None = None
    use_first_shot_reference: bool | None = None
    # Shot 1 has no shot before it; this uploaded clip (a ComfyUI input file name) is
    # used as its preceding context, exactly the way shot 2 continues from shot 1.
    first_shot_context: str | None = None
    # Optimise the film description with the model the owner currently has selected before the
    # storyboard is built. Off by default: it loads that language model on demand (and releases it
    # again before ComfyUI takes the card), which is a real cost the owner opts into.
    enhance_prompt: bool = False


class VideoJob:
    """One H3 run. Holds only what the panel needs to render progress."""

    def __init__(self, job_id: str, recipe_key: str, prompt: str, params: dict):
        self.id = job_id
        self.recipe = recipe_key
        self.prompt = prompt
        self.params = params
        # The session that submitted this film. One monitor serves the whole machine, so this is
        # what keeps the owner's prompt out of a visitor's progress view (and vice versa).
        self.submitted_by = ""
        self.state = "queued"
        self.phase = "queued"
        self.progress = 0.0
        self.error: str | None = None
        self.prompt_id: str | None = None
        self.video_path: str | None = None
        self.report: str | None = None
        # 本次网关会话里这一段是不是"第一次跑"（要装载 H3 权重）。由监控器在受理时置位，
        # 代价模型用它区分冷/热样本。
        self.cold_first = False
        # Which model optimised the film description, and what it was before. None when the
        # owner left the enhancer off.
        self.enhancer: dict | None = None
        # How long that step actually took, so the panel's estimate can use a number measured on
        # this machine instead of a constant.
        self.enhance_seconds: float | None = None
        self.started = time.time()
        self.finished: float | None = None
        self.notes: list[dict] = []
        # One row per shot, so the panel can show what the run is doing shot by shot
        # instead of one anonymous bar. The estimates are filled in by the monitor.
        self.shots: list[dict] = [
            {"index": index, "seconds": shot["seconds"], "steps": shot["steps"],
             "seed": shot["seed"], "state": "pending", "estimate": None,
             "started": None, "finished": None, "elapsed": None}
            for index, shot in enumerate(params.get("shot_plan") or [], start=1)
        ]

    def note(self, phase: str, message: str = "") -> None:
        self.phase = phase
        self.notes.append({"t": round(time.time() - self.started, 2), "phase": phase,
                           "message": message})
        if len(self.notes) > 60:
            del self.notes[:20]

    def begin_shot(self, position: int) -> None:
        row = self.shots[position - 1]
        row["state"] = "running"
        row["started"] = time.time()

    def finish_shot(self, position: int, state: str) -> None:
        row = self.shots[position - 1]
        row["state"] = state
        row["finished"] = time.time()
        if row["started"] and row.get("elapsed") is None:
            row["elapsed"] = round(row["finished"] - row["started"], 1)
        # 真实用时回喂给代价模型：下一次估算就有这台机器当下的校准（详见 video_cost）。
        # 只记一次：`finish_shot` 在两个地方被调用（`_follow` 的成功出口与调用方），重复记账会
        # 把同一个样本灌进校准里，把中位数拉偏。
        if state == "done" and row.get("elapsed") and not row.get("recorded"):
            row["recorded"] = True
            video_cost.record(
                steps=int(row.get("steps") or 1), seconds=float(row.get("seconds") or 7.5),
                width=int(self.params.get("width") or video_cost.BASE_WIDTH),
                height=int(self.params.get("height") or video_cost.BASE_HEIGHT),
                position=position, wall_seconds=float(row["elapsed"]), recipe=self.recipe,
                # 本次会话第一次跑：这段里含 H3 权重装载，不能当普通样本喂校准（会重复计算）。
                cold=bool(self.cold_first and position == 1),
            )

    def eta(self) -> float | None:
        """Rough seconds left, from the per-shot estimates the monitor filled in."""
        if self.state not in ("queued", "running"):
            return None
        remaining = 0.0
        for row in self.shots:
            estimate = row.get("estimate") or 0.0
            if row["state"] == "pending":
                remaining += estimate
            elif row["state"] == "running" and row["started"]:
                remaining += max(0.0, estimate - (time.time() - row["started"]))
        return round(remaining, 1)

    def snapshot(self) -> dict:
        running = next((row["index"] for row in self.shots if row["state"] == "running"), None)
        return {
            "id": self.id,
            "recipe": self.recipe,
            "state": self.state,
            "phase": self.phase,
            "progress": round(self.progress, 3),
            "error": self.error,
            "video": self.video_url(),
            # The absolute path too: the panel hands it back to the interpolation
            # post-process, which is a separate job on the same file.
            "path": self.video_path,
            "report": self.report,
            # Which model rewrote the film description (None when the enhancer was off), and what
            # the owner's own words were.
            "enhancer": self.enhancer,
            # Measured wall clock of that step, and whether the run has one at all: the rail's
            # estimate and its 提示词优化 stage are both driven from these two fields.
            "enhance_seconds": self.enhance_seconds,
            # Who submitted this film, so a progress view can show the job without showing
            # another caller's words.
            "submitted_by": self.submitted_by,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
            "notes": self.notes[-40:],
            # Everything the monitor shows: which shot is running, what each shot was
            # asked for, how long each took or is expected to take, and the time left.
            "shots": [dict(row) for row in self.shots],
            "position": running,
            "total": len(self.shots),
            "eta": self.eta(),
            "settings": {
                "width": self.params.get("width"), "height": self.params.get("height"),
                "output_root": self.params.get("output_root"),
                "project_name": self.params.get("project_name"),
                "structure_guard": bool(self.params.get("structure_guard")),
                "lora_strength": self.params.get("lora_strength"),
                "first_shot_context": self.params.get("first_shot_context"),
                # The panel draws the 提示词优化 stage only when the run actually has one, so the
                # job reports it rather than the panel trusting whatever the switch says now.
                "enhance_prompt": bool(self.params.get("enhance_prompt")),
            },
            "prompt_id": self.prompt_id,
        }

    def video_url(self) -> str | None:
        """A relative URL the browser can play, or None while there is no file.

        The path is absolute because the save location may sit outside ComfyUI's
        output tree; ``_servable_roots`` is what decides whether that is allowed.
        """
        if not self.video_path:
            return None
        target = Path(self.video_path).resolve()
        if not any(target.is_relative_to(root) for root in _servable_roots(OWNER_IDENTITY)):
            return None
        return "/local/video/file?path=" + quote(str(target))


class VideoMonitor:
    def __init__(self, client, mode_loader=None):
        self.client = client
        self.mode_loader = mode_loader
        self.job: VideoJob | None = None
        self.task: asyncio.Task | None = None
        self.history: list[dict] = []
        # 本进程里已经跑完的段数：为 0 时下一段属于冷启动（含 H3 权重装载），
        # 代价模型据此把冷/热样本分开记账。
        self.shots_measured = 0

    @property
    def busy(self) -> bool:
        return bool(self.task and not self.task.done())

    def progress(self) -> dict:
        return {
            "busy": self.busy,
            "job": self.job.snapshot() if self.job else None,
        }

    def start(self, body: VideoGenerateRequest, submitted_by: str = "") -> dict:
        if renders_are_refused():
            raise HTTPException(409, TEST_RENDER_REFUSAL)
        if self.busy:
            raise HTTPException(409, "已有视频任务正在运行，请等待完成或先取消。")
        entry = video_jobs.recipe(body.recipe)
        if entry is None:
            raise HTTPException(404, f"未知的视频工作流：{body.recipe}")
        plan = _shot_plan(body)
        output_root = _resolve_output_root(body.output_root)
        # A visitor's films go to its own folder: the save location is the owner's setting, and
        # letting a shared caller name a path would let it write anywhere on the machine.
        if submitted_by:
            output_root = str(guest_root(Identity(False, submitted_by)))
        else:
            _OUTPUT["root"] = output_root
        first_shot_context = _prepare_context(body.first_shot_context, body.width, body.height,
                                              submitted_by or guest_uploads.OWNER_SESSION)
        project_name = (body.project_name or "").strip()
        # Created here, not left to the node: a save location that cannot be written fails now
        # instead of after the first shot has been sampled.
        _prepare_project_folder(Path(output_root), project_name if not submitted_by else "")
        params = {
            "recipe": entry.key,
            "prompt": (body.prompt or "").strip(),
            # Kept in shot order, positions included: an empty entry means "use the
            # film description", and the plan is indexed the same way.
            "shots": [str(text or "") for text in (body.shots or [])],
            "shot_references": [list(refs or []) for refs in (body.shot_references or [])],
            "shot_plan": plan,
            "output_root": output_root,
            "structure_guard": bool(body.structure_guard),
            "lora_strength": (None if body.lora_strength is None
                              else min(1.2, max(0.4, float(body.lora_strength)))),
            # The clip shot 1 continues from, already converted to this render's size.
            "first_shot_context": first_shot_context,
            "width": body.width,
            "height": body.height,
            "fps": body.fps,
            "project_name": project_name or None,
            "use_first_shot_reference": body.use_first_shot_reference,
            # Kept on the job so the panel can say whether the film description it is reading
            # was rewritten, and by which model.
            "enhance_prompt": bool(body.enhance_prompt),
        }
        job = VideoJob(f"vid-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}",
                       entry.key, params["prompt"], params)
        job.submitted_by = submitted_by
        # 本进程里还没跑过任何一段 → 这一段是冷启动（含 H3 权重装载）。
        job.cold_first = self.shots_measured == 0
        for position in range(1, len(job.shots) + 1):
            job.shots[position - 1]["estimate"] = round(_estimate_shot_seconds(job, position), 1)
        self.job = job
        job.note("accepted", entry.label)
        self.task = asyncio.create_task(self._run(job), name=f"video-job-{job.id}")
        return job.snapshot()

    async def prepare_prompt(self, job: VideoJob) -> None:
        """Rewrite the film description with the model the owner currently has selected.

        Called before ComfyUI takes the card. The language model is loaded on demand through the
        gateway's own mode loader — the same path the mode buttons use, which is what makes this
        "follow the current model" rather than a second, fixed idea of the prompt model — and it
        is released again immediately afterwards, because H3 needs the VRAM.

        Per-shot prompts the owner typed are never touched: this rewrites the film description
        only, which is what the shots without their own text inherit.
        """
        if not job.params.get("enhance_prompt") or _ENHANCER is None:
            return
        job.note("enhancing", "用当前模型优化提示词")
        original = job.prompt
        started = time.monotonic()
        enhanced, info = await _ENHANCER.enhance(original, kind="video")
        job.enhance_seconds = round(time.monotonic() - started, 1)
        job.prompt = enhanced
        job.params["prompt"] = enhanced
        # Both sides are kept: the panel can then show what the model rewrote it from.
        job.enhancer = {**info, "original": original, "rewritten": enhanced,
                        "seconds": job.enhance_seconds}
        job.note("enhanced", f"{info.get('label') or ''}｜"
                 + ("已优化" if info.get("used") else _enhancement_reason(info)))
        if _RELEASE_FOR_RENDER is not None:
            with contextlib.suppress(Exception):
                await _RELEASE_FOR_RENDER()

    async def _run(self, job: VideoJob) -> None:
        try:
            job.state = "running"
            await self.prepare_prompt(job)
            job.note("starting_comfy")
            await _ensure_comfy(self.client)
            plan = job.params["shot_plan"]
            total = len(plan)
            durations = [entry["seconds"] for entry in plan]
            for position, shot in enumerate(plan, start=1):
                job.note("building_graph", f"第 {position}/{total} 段")
                graph = video_jobs.build_prompt(
                    job.recipe,
                    prompt=job.prompt,
                    shots=job.params.get("shots"),
                    shot_references=job.params.get("shot_references"),
                    seed=shot["seed"],
                    steps=shot["steps"],
                    width=job.params.get("width"),
                    height=job.params.get("height"),
                    fps=job.params.get("fps"),
                    project_name=job.params.get("project_name"),
                    use_first_shot_reference=job.params.get("use_first_shot_reference"),
                    shot_count=total,
                    shot_seconds=durations,
                    shot_index=position,
                    action="redo_shot",
                    output_root=job.params.get("output_root"),
                    structure_guard=job.params.get("structure_guard", False),
                    lora_strength=job.params.get("lora_strength"),
                    shot_continuity=[entry.get("continuity", "continue")
                                     for entry in (job.params.get("shot_plan") or [])],
                    # Only shot 1 takes a hand-supplied predecessor; the rest follow
                    # the shot the chain just rendered.
                    shot_01_context=(job.params.get("first_shot_context")
                                     if position == 1 else None),
                )
                job.note("submitting", f"第 {position}/{total} 段 · {len(graph)} 个节点")
                client_id = uuid.uuid4().hex
                response = await self.client.post(f"{COMFY}/prompt",
                                                  json={"prompt": graph, "client_id": client_id},
                                                  timeout=60)
                if response.status_code != 200:
                    job.state = "error"
                    job.error = f"第 {position} 段被 ComfyUI 拒绝：{response.text[:360]}"
                    return
                job.prompt_id = response.json()["prompt_id"]
                job.note("generating", f"第 {position}/{total} 段 · {shot['steps']} 步")
                job.begin_shot(position)
                if not await self._follow(job, position, total):
                    if job.state == "error":
                        job.finish_shot(position, "error")
                    return
                job.finish_shot(position, "done")
        except asyncio.CancelledError:
            job.state = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001
            job.state = "error"
            job.error = str(exc)
        finally:
            job.finished = time.time()
            if job.state == "running":
                job.state = "done" if job.video_path else "error"
                if not job.video_path and not job.error:
                    job.error = "任务结束但没有产出视频文件"
            self.history.insert(0, job.snapshot())
            del self.history[20:]

    async def _follow(self, job: VideoJob, position: int, total: int) -> bool:
        """Follow one shot. False ends the run: it failed, or the film is finished."""
        estimate = _estimate_shot_seconds(job, position)
        began = time.monotonic()
        while time.monotonic() - began < 6 * 3600:
            if self.task is not None and self.task.cancelled():
                return False
            elapsed = time.monotonic() - began
            share = min(0.99, elapsed / estimate) if estimate else 0.0
            job.progress = (position - 1 + share) / total
            history = await self._history(job.prompt_id)
            if history is not None:
                status = history.get("status", {})
                for message in status.get("messages", []):
                    if message and message[0] == "execution_error":
                        detail = message[1]
                        job.state = "error"
                        job.error = (f"第 {position} 段 {detail.get('node_type')}: "
                                     f"{detail.get('exception_message', '')[:300]}")
                        return False
                outputs = history.get("outputs") or {}
                if position < total:
                    # An intermediate submission only wrote its own shot; the film
                    # appears when the last one assembles the clips.
                    job.finish_shot(position, "done")      # 记下这一段的真实用时（代价模型校准）
                    self.shots_measured += 1
                    return True
                job.video_path, job.report = _read_report(outputs)
                job.progress = 1.0
                job.note("done", f"第 {total}/{total} 段完成")
                job.state = "done" if job.video_path else "error"
                if not job.video_path:
                    job.error = "任务完成但没有 video_path 输出"
                # 最后一段也在成功出口收尾：调用方在 `_follow` 返回 False 时直接 return，
                # 永远走不到它自己的 `finish_shot`——实测就是这样丢掉一整条 30 分钟的样本。
                job.finish_shot(position, "done" if job.video_path else "error")
                if job.video_path:
                    self.shots_measured += 1
                return False
            await asyncio.sleep(5)
        job.state = "error"
        job.error = f"第 {position} 段超过 6 小时仍未完成"
        return False

    async def _history(self, prompt_id: str | None) -> dict | None:
        if not prompt_id:
            return None
        try:
            response = await self.client.get(f"{COMFY}/history/{prompt_id}", timeout=30)
            response.raise_for_status()
            payload = response.json()
        except Exception:  # noqa: BLE001
            return None
        return payload.get(prompt_id)

    async def cancel(self) -> dict:
        job = self.job
        if job is None or not self.busy:
            raise HTTPException(409, "当前没有可取消的视频任务。")
        job.note("cancel_requested")
        if job.prompt_id:
            for path, body in (("/interrupt", {"prompt_id": job.prompt_id}),
                               ("/queue", {"delete": [job.prompt_id]})):
                try:
                    await self.client.post(f"{COMFY}{path}", json=body, timeout=15)
                except Exception:  # noqa: BLE001
                    pass
        assert self.task is not None
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass
        if job.state == "running":
            job.state = "cancelled"
        return job.snapshot()


def _shot_plan(body: VideoGenerateRequest) -> list[dict]:
    """One settings entry per shot, in shot order.

    The panel sends a full plan; a caller that only names shots gets the recipe's
    defaults. Seeds left out or negative are replaced with a random one, because
    the node would otherwise keep the workflow file's fixed seed and hand back the
    same film every time.
    """
    entries = [entry.model_dump() for entry in (body.shot_plan or [])]
    count = len(body.shots or []) or len(entries) or 1
    while len(entries) < count:
        entries.append(ShotPlan().model_dump())
    plan = entries[:count]
    for entry in plan:
        seed = entry.get("seed")
        if seed is None or seed < 0:
            entry["seed"] = secrets.randbelow(2 ** 31)
        seconds = entry.get("seconds")
        steps = entry.get("steps")
        entry["seconds"] = min(13.75, max(5.0, float(seconds) if seconds is not None else 7.5))
        entry["steps"] = min(50, max(1, int(steps) if steps is not None else 20))
        # Anything other than an explicit "cut" continues the shot before it.
        entry["continuity"] = "cut" if str(entry.get("continuity") or "").lower() == "cut" \
            else "continue"
    return plan


def _resolve_output_root(value: str | None) -> str:
    """The folder films are written into: the panel's choice or the default.

    A relative value is read from ComfyUI's output directory, so ``video`` and
    ``ComfyUI/output/video`` mean the same folder. Anything else is taken as the
    absolute folder it looks like; the project folder is created inside it.
    """
    text = (value or "").strip()
    if not text:
        return str(DEFAULT_OUTPUT_ROOT)
    root = Path(text).expanduser()
    if not root.is_absolute():
        root = OUTPUT_ROOT / root
    root = Path(root).resolve()
    if root.exists() and not root.is_dir():
        raise HTTPException(400, f"保存位置不是文件夹：{root}")
    return str(root)


def _prepare_project_folder(root: Path, name: str) -> Path:
    """Create ``<root>/<name>/`` before the job is submitted, and reject a name Windows refuses.

    The director creates this folder itself, but it creates it *before* sampling: a folder that
    turns out to be unwritable — or a name Windows rejects — used to surface as
    ``Error while serializing: I/O error: 系统找不到指定的路径`` after the shot had already been
    sampled (measured: 313 s into a 6-step shot). Here the same problem costs one second and says
    which folder it is. The name rule mirrors the director's own (``project_path``).
    """
    if not name:
        return root
    if name in (".", "..") or name != name.strip(" .") or re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
        raise HTTPException(400, f"保存名称不能包含 < > : \" / \\ | ? *，也不能以点或空格结尾：{name}")
    folder = root / name
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise HTTPException(400, f"保存位置写不进去：{folder}（{error}）") from error
    return folder


def _servable_roots(identity: Identity) -> tuple[Path, ...]:
    """Folders a file may be served from at all, for this caller.

    The owner: ComfyUI's whole output tree, its input folder, plus the save location the panel
    picked. A shared visitor: only its **own** output folder and the input folder it uploads
    into — so "download your own material" is fenced by path, not merely by a lookup that a
    future route could forget to perform.

    Inside the fence there is still a second check (``_servable``): a visitor must own the file.
    The two together are what keep one visitor's film away from another's.
    """
    if not identity.is_owner:
        return (guest_root(identity).resolve(), INPUT_ROOT.resolve())
    roots = [OUTPUT_ROOT.resolve(), INPUT_ROOT.resolve()]
    chosen = _OUTPUT["root"]
    if chosen:
        roots.append(Path(chosen).resolve())
    return tuple(roots)


def _servable(path: str, identity: Identity, missing: str = "找不到这个文件") -> Path:
    """Resolve a caller-named file and refuse anything this caller may not have.

    Both the report route and the MP4 route go through here, so the containment check
    (and its 403) cannot drift apart between them. For a shared visitor the check has
    a second half: being inside the input folder is not enough, the file also has to
    be one this session uploaded. The refusal is a 404 so the response cannot be used
    to probe which file names exist on the machine.
    """
    target = Path(path).resolve()
    if not any(target.is_relative_to(root) for root in _servable_roots(identity)):
        raise HTTPException(403, "路径不在允许的输出目录里")
    if not identity.is_owner:
        # Two ways a visitor's file is its own: it lives in the visitor's own output folder
        # (everything ComfyUI writes for it), or it is a file recorded against its session (every
        # upload). Either half alone is enough — requiring both would hide a visitor's own film
        # from it, and requiring only the folder would let a guessed upload name through.
        own = guest_root(identity).resolve()
        if not (target.is_relative_to(own) or _policy().is_visible_to(target.name, identity)):
            raise HTTPException(404, missing)
    if not target.is_file():
        raise HTTPException(404, missing)
    return target


def _folder_browse(path: str | None) -> dict:
    """Subfolders of one directory, for the panel's save-location picker."""
    roots = [{"name": f"{letter}:\\", "path": f"{letter}:\\"}
             for letter in string.ascii_uppercase if Path(f"{letter}:\\").exists()]
    text = (path or "").strip()
    if not text:
        return {"path": None, "parent": None, "roots": roots, "folders": []}
    current = Path(text).expanduser()
    if not current.is_absolute():
        raise HTTPException(400, "请填绝对路径")
    current = Path(current).resolve()
    if not current.is_dir():
        raise HTTPException(404, f"找不到这个文件夹：{current}")
    try:
        folders = sorted(({"name": child.name, "path": str(child)} for child in current.iterdir()
                          if child.is_dir() and not child.name.startswith(".")),
                         key=lambda entry: entry["name"].lower())
    except OSError as exc:
        raise HTTPException(403, f"读不了这个文件夹：{exc.strerror or exc}") from exc
    parent = current.parent
    return {"path": str(current), "parent": str(parent) if parent != current else None,
            "roots": roots, "folders": folders}


def _estimate_shot_seconds(job: VideoJob, position: int) -> float:
    """一段的预计用时：交给统一的动态代价模型（时长 × 步数 × 尺寸，并随实测自我校准）。

    以前这里是写死的：`87 + (752.4-87) × 步数/20 × 时长/7.5`，还挂着一个早就不存在的配方键
    （`video_speed`），**尺寸完全不参与**——768×512 与 1024×672 会得到同一个数。现在四个实测锚点
    （3/6/12/20 步 = 223/304/527/870 秒）在 `video_cost` 里最小二乘拟合，尺寸项与校准系数都由
    真实跑完的样本支撑；没有跨尺寸样本时它明确报告"线性外推"。（20 步那条面板入口已按要求下线，
    它留下的 870 秒实测仍然是锚点之一——锚点是测量值，不是可选项。）
    """
    shot = job.params["shot_plan"][position - 1]
    return video_cost.predict(
        steps=int(shot["steps"]), seconds=float(shot["seconds"]),
        width=int(job.params.get("width") or video_cost.BASE_WIDTH),
        height=int(job.params.get("height") or video_cost.BASE_HEIGHT),
        position=position,
    )["seconds"]


def _prepare_context(name: str | None, width: int | None, height: int | None,
                     session: str = guest_uploads.OWNER_SESSION) -> str | None:
    """Convert the uploaded clip shot 1 continues from, and return its input file name.

    The director reads the previous shot's tail itself, so the clip has to be in the
    shape that path expects: the render's size, 24 fps, and an audio track.
    """
    if not name or not str(name).strip():
        return None
    base = Path(str(name).strip()).name
    # 访客只能拿自己传的片段当前情：别人的（含机主的）一律当不存在。
    if session != guest_uploads.OWNER_SESSION and not _policy().owned_by(base, session):
        raise HTTPException(404, f"找不到前文视频：{name}")
    source = INPUT_ROOT / base
    if not source.is_file():
        raise HTTPException(400, f"找不到前文视频：{name}")
    try:
        normalized = video_context.normalize(source, int(width or 768), int(height or 512), INPUT_ROOT)
    except ValueError as failure:
        raise HTTPException(400, str(failure)) from failure
    if normalized:
        # The converted clip lands in the input folder too, so it is recorded against
        # the requester: otherwise the owner's own context files would be the only ones
        # in that folder with no owner, and a guest-visible listing would hide them.
        _policy().note_upload(Path(normalized).name, session,
                              _file_size(INPUT_ROOT / Path(normalized).name))
    return normalized


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _read_report(outputs: dict) -> tuple[str | None, str | None]:
    """Pull the film path and the checkpoint report out of the node's payload.

    H3MP4ShotEditor is an OUTPUT_NODE returning ('video_path', 'checkpoint_report').
    The report is itself a JSON document whose "video" field names the assembled
    file, so the path is readable from the report even when the video_path slot is
    absent or empty. Values that are not paths are ignored rather than mistaken
    for one.
    """
    video = report = None
    for node in outputs.values():
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            first = value[0] if isinstance(value, list) and value else None
            if not isinstance(first, str):
                # A list of counts (completed_shots) must not be read as a report.
                continue
            text = first.strip()
            if key == "video_path" and text:
                video = text
                continue
            if report is None:
                report = text[:2000]
            # The director's report carries the assembled file's location.
            if video is None and text.startswith("{"):
                try:
                    parsed = json.loads(text)
                except ValueError:
                    continue
                candidate = parsed.get("video") if isinstance(parsed, dict) else None
                if isinstance(candidate, str) and candidate.lower().endswith(".mp4"):
                    video = candidate
    if video is None and report:
        # A bare path is also acceptable to the file route's containment check.
        stripped = report.strip()
        if stripped.lower().endswith(".mp4"):
            video = stripped
    return video, report


def _enhancement_reason(info: dict) -> str:
    """One short line for the film's progress when the prompt was left as written.

    The enhancer reports why in its own words, and one of those reasons ("ComfyUI 正在执行或排队，
    将在任务结束后自动加载 Qwen") was a sentence about model loading shown in the middle of a
    render. The wait in ``PromptEnhancer.ensure_model`` is what stops it happening; this keeps any
    older wording from reaching the rail.
    """
    reason = str(info.get("reason") or "未优化")
    if "ComfyUI" in reason and "排队" in reason:
        return "ComfyUI 忙，未优化"
    return reason


_SUPERVISOR_PROVIDER = None


def set_supervisor_provider(provider) -> None:
    """Bind the object that can start ComfyUI on demand (the image monitor's supervisor).

    The gateway publishes its monitor as the *name* ``image_jobs`` inside mode_server, which
    shadows the module of the same name — so ``import image_jobs`` over here reaches the module,
    where no ``supervisor`` attribute has ever existed. A video run that began while ComfyUI was
    down therefore failed with "ComfyUI 未运行，且没有可用的启动器。" instead of starting it
    (measured: after ComfyUI died mid-interrupt, both video runs at 02:09 died in 2 s).
    """
    global _SUPERVISOR_PROVIDER
    _SUPERVISOR_PROVIDER = provider


def _supervisor():
    """The live supervisor, from the bound provider or the module attribute."""
    provider = _SUPERVISOR_PROVIDER
    found = provider() if callable(provider) else provider
    if found is not None:
        return found
    import image_jobs
    return getattr(image_jobs, "supervisor", None)


async def _ensure_comfy(client) -> None:
    """Bring ComfyUI up before a long job, the way the image path does."""
    try:
        await client.get(f"{COMFY}/system_stats", timeout=5)
        return
    except Exception:  # noqa: BLE001
        pass
    supervisor = _supervisor()
    if supervisor is None:
        raise RuntimeError("ComfyUI 未运行，且没有可用的启动器。")
    await supervisor.ensure_running(timeout=supervisor.start_timeout)


ROUTER = APIRouter()

# Uploads land in ComfyUI's input folder, which is where LoadVideo, LoadImage and the
# H3 per-shot reference slots all read from. A clip is far larger than a reference
# image, so the two get their own limits and filename prefixes — but they share this
# one route, because "put this file where ComfyUI can read it" is the same step.
INPUT_ROOT = image_comfy.COMFY_ROOT / "ComfyUI" / "input"
VIDEO_SUFFIXES = {".mp4", ".mov", ".webm"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_MAX_BYTES = 800 * 1024 * 1024
IMAGE_MAX_BYTES = 64 * 1024 * 1024


class VideoPipelineRequest(BaseModel):
    path: str


@ROUTER.post("/local/video/upload")
async def video_upload(request: Request, identity: Identity = Depends(_identity)):
    """Accept a shot reference image or an upscale source clip.

    Uploaded as a raw body rather than multipart so the browser can stream a large
    file without encoding it into a form.

    A shared visitor may store either kind here when the owner has opened guest uploads
    (the "storage" switch); the file is recorded against its session, which is what limits it
    to its own files afterwards. 参考图以前只给机主，理由是"参考图只用来驱动生成任务，而生成
    仅限机主"——那条理由早就过期了（所有者要求访客能自己生成，见 `GUEST_ROUTES`），所以这里
    跟着开放；`check_references()` 会在提交时核对每张图确实是这个会话自己传的。
    """
    if not identity.is_owner and not _policy().guests_may_upload():
        raise HTTPException(403, "机主尚未开放访客上传")
    name = str(request.query_params.get("name") or "clip.mp4")
    suffix = Path(name).suffix.lower()
    if suffix in VIDEO_SUFFIXES:
        prefix, limit = "qwen-video-", VIDEO_MAX_BYTES
    elif suffix in IMAGE_SUFFIXES:
        # The reference slots take input-folder image names, so a reference image is
        # stored, not inlined as a data URL the way the image panel does it.
        prefix, limit = "qwen-image-ref-", IMAGE_MAX_BYTES
    else:
        raise HTTPException(400, "只支持 MP4 / MOV / WebM 视频，或 PNG / JPEG / WebP 参考图")
    raw = await request.body()
    if not raw:
        raise HTTPException(400, "上传内容为空")
    if len(raw) > limit:
        raise HTTPException(413, f"文件超过 {limit // (1024 * 1024)} MB 上限")
    refusal = _policy().check_upload(identity, len(raw), INPUT_ROOT)
    if refusal is not None:
        status, message = refusal
        LOGGER.warning("video upload refused (%s, %s byte(s)): %s", identity.token[:8], len(raw), message)
        raise HTTPException(status, message)
    INPUT_ROOT.mkdir(parents=True, exist_ok=True)
    target = INPUT_ROOT / f"{prefix}{uuid.uuid4().hex}{suffix}"
    target.write_bytes(raw)
    _policy().note_upload(target.name, identity.token, len(raw))
    return JSONResponse({"name": target.name, "bytes": len(raw)})


@ROUTER.get("/local/video/source")
async def video_source(identity: Identity = Depends(_identity)):
    """The clip chooser: what this caller selected, and what it may pick from.

    A visitor sees only its own uploads. Listing every file in the input folder would
    show one visitor what another one uploaded, and that folder holds both.
    """
    session = guest_uploads.OWNER_SESSION if identity.is_owner else identity.token
    videos = _visible_videos(identity)
    selected = guest_uploads.selected_source(session)
    if selected not in videos:
        selected = None
    return JSONResponse({"selected": selected, "videos": videos})


@ROUTER.post("/local/video/source")
async def select_video_source(body: VideoPipelineRequest, identity: Identity = Depends(_identity)):
    """Choose which clip this caller's upscale recipes run on.

    Per session: one shared selection would let a visitor silently retarget the clip
    the owner — or another visitor — is about to process.
    """
    videos = _visible_videos(identity)
    if body.path not in videos:
        raise HTTPException(404, "该视频不在你的可用片段里")
    session = guest_uploads.OWNER_SESSION if identity.is_owner else identity.token
    guest_uploads.select_source(session, body.path)
    if identity.is_owner:
        # The generation path and the denoise fallback read this global, which only
        # owner-run jobs ever reach.
        _SOURCE["path"] = body.path
    return JSONResponse({"selected": body.path})


def _list_input_videos() -> list[str]:
    if not INPUT_ROOT.is_dir():
        return []
    names = []
    for path in sorted(INPUT_ROOT.iterdir()):
        if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES:
            names.append(path.name)
    # Newest first: the file the user just uploaded is the one they want.
    names.sort(key=lambda name: (INPUT_ROOT / name).stat().st_mtime, reverse=True)
    return names


@ROUTER.get("/local/video/recipes")
async def video_recipes():
    # `cost_model` 让面板用同一套公式本地估算：常量、尺寸指数、校准系数与标定状态都在这里，
    # 免得每敲一个数字都向服务端要一次预计时间。
    return JSONResponse({"recipes": [r.describe() for r in video_jobs.recipes()],
                         "current": video_jobs.RECIPES[0].key,
                         "cost_model": video_cost.describe()})


@ROUTER.get("/local/video/output")
async def video_output(request: Request, identity: Identity = Depends(_identity)):
    """Where films are written: the default, and whatever the panel last picked.

    A shared visitor is told its own folder, because that is where its films actually go — the
    save-location picker stays the owner's, and letting a visitor name a path would let it write
    anywhere on the machine.
    """
    if not identity.is_owner:
        root = guest_root(identity)
        return JSONResponse({"default": str(root), "current": str(root)})
    return JSONResponse({"default": str(DEFAULT_OUTPUT_ROOT),
                         "current": _OUTPUT["root"] or str(DEFAULT_OUTPUT_ROOT)})


@ROUTER.get("/local/video/folders")
async def video_folders(path: str | None = None):
    """Subfolders of one directory, for the panel's save-location picker."""
    return JSONResponse(_folder_browse(path))


def check_references(body: "VideoGenerateRequest", identity: Identity) -> None:
    """每张参考图都必须是这个调用者自己的——访客只能用自己传的那几张。

    上传的文件名是随机名，猜不到；但"猜不到"不是围栏：这里按会话记录核对，别人的（包括机主的）
    参考图一律当作不存在（404，不确认它存在）。机主不受限。
    """
    if identity.is_owner:
        return
    for group in body.shot_references or []:
        for entry in group or []:
            name = Path(str(entry or "").strip()).name
            if name and not _policy().is_visible_to(name, identity):
                raise HTTPException(404, f"参考图不可用：{entry}")


@ROUTER.post("/local/video/generate")
async def video_generate(body: VideoGenerateRequest, request: Request = None,
                         identity: Identity = Depends(_identity)):
    """Start a film. Every attempt is logged, accepted or rejected: a click that
    ends in a refusal has to be visible from the gateway's own log.

    A shared visitor submits with its own session, which routes the film into its own output
    folder and tags the job so the owner's prompt never shows up in the visitor's progress view.
    """
    check_references(body, identity)
    _guard_idle()
    monitor = _monitor()
    try:
        snapshot = monitor.start(body, submitted_by="" if identity.is_owner else identity.token)
    except HTTPException as error:
        LOGGER.warning("video generate rejected: %s %s", error.status_code, error.detail)
        raise
    job = monitor.job
    LOGGER.info("video generate accepted: %s · %s 段 · %s · %s",
                snapshot["id"], len(job.params["shot_plan"]), job.params["output_root"], snapshot["recipe"])
    return JSONResponse({"job": snapshot}, status_code=202)


class InterpolateRequest(BaseModel):
    # The finished film, as the job snapshot reports it.
    path: str
    multiplier: int = 2


@ROUTER.post("/local/video/interpolate")
async def start_interpolation(body: InterpolateRequest, request: Request = None):
    """RIFE-interpolate a finished film (24 -> 48 fps) and publish it beside it.

    Named so it does not shadow the ``video_interpolate`` module it drives.
    """
    _guard_idle()
    source = _servable(body.path, _caller(request))
    monitor = _interpolate()
    multiplier = min(4, max(2, int(body.multiplier)))
    try:
        snapshot = monitor.start(source, multiplier)
    except video_interpolate.InterpolateError as error:
        LOGGER.warning("interpolate rejected: %s", error)
        raise HTTPException(409, str(error)) from error
    LOGGER.info("interpolate accepted: %s · %s ×%s", snapshot["id"], source.name, multiplier)
    return JSONResponse({"job": snapshot}, status_code=202)


def _clip_seconds(path: Path) -> float:
    """How long a clip runs, for the concat result's own report."""
    info = video_denoise.probe(path)
    return info["frames"] / max(info["fps"], 1.0)


class ConcatRequest(BaseModel):
    # Clips in the order they should play: ComfyUI input file names (what the panel
    # uploads) or paths inside the servable output tree.
    clips: list[str]
    name: str | None = None
    # Match every segment's brightness to the first one before concatenating: the chain's
    # shot-to-shot exposure drift is what reads as a jump at the join.
    normalize: bool = False


@ROUTER.post("/local/video/concat")
async def concat_clips(body: ConcatRequest, request: Request = None):
    """Stitch separately processed clips back into one film.

    Upscaling is bounded by VRAM, so a long film is handled as segments (per shot, or by
    cutting the master) — this is the step that puts them back together. The clips must
    share size and frame rate, which the H3 chain guarantees for a film's own shots.
    """
    identity = _caller(request)
    clips = [Path(str(entry).strip()) for entry in (body.clips or []) if str(entry).strip()]
    if len(clips) < 2:
        raise HTTPException(400, "至少要选两段视频才能拼接")
    resolved: list[Path] = []
    for entry in clips:
        if entry.parent == Path(".") and (INPUT_ROOT / entry.name).is_file():
            if not _policy().is_visible_to(entry.name, identity):
                raise HTTPException(404, f"找不到这一段：{entry.name}")
            resolved.append(INPUT_ROOT / entry.name)
        else:
            resolved.append(_servable(str(entry), identity, missing=f"找不到这一段：{entry.name}"))
    probe = await asyncio.to_thread(video_denoise.probe, resolved[0])
    for path in resolved[1:]:
        other = await asyncio.to_thread(video_denoise.probe, path)
        if (other["width"], other["height"]) != (probe["width"], probe["height"]):
            raise HTTPException(409, f"{path.name} 的尺寸与第一段不同：拼接要求分辨率一致")
        if abs(other["fps"] - probe["fps"]) > 0.5:
            raise HTTPException(409, f"{path.name} 的帧率与第一段不同：拼接要求帧率一致")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target_dir = DEFAULT_OUTPUT_ROOT / "stitched"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{(body.name or '').strip() or stamp}-stitched.mp4"
    # Optional brightness match: each segment is shifted to the first segment's mean luma,
    # which is what a shot-to-shot exposure drift shows up as. Measured per clip before the
    # command is built, and reported back so the panel can show what was applied.
    gains: list[float] = []
    if body.normalize:
        gains = await asyncio.to_thread(_seam_gains, resolved)
    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y"]
    for path in resolved:
        command += ["-i", str(path)]
    if gains:
        stages = "".join(f"[{index}:v]eq=brightness={gain}[v{index}];"
                         for index, gain in enumerate(gains))
        streams = "".join(f"[v{index}][{index}:a]" for index in range(len(resolved)))
    else:
        stages = ""
        streams = "".join(f"[{index}:v][{index}:a]" for index in range(len(resolved)))
    command += ["-filter_complex", f"{stages}{streams}concat=n={len(resolved)}:v=1:a=1[v][a]",
                "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-crf", "17",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", str(target)]
    result = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True)
    if result.returncode != 0 or not target.is_file():
        LOGGER.warning("concat failed: %s", (result.stderr or "")[:300])
        raise HTTPException(409, f"拼接失败：{(result.stderr or '').strip()[:160]}")
    seconds = 0.0
    for path in resolved:
        seconds += await asyncio.to_thread(_clip_seconds, path)
    seconds = round(seconds, 2)
    LOGGER.info("concat done: %s 段 → %s", len(resolved), target.name)
    return JSONResponse({"target": str(target), "clips": [str(path) for path in resolved],
                         "seconds": seconds, "brightness": gains,
                         "video": "/local/video/file?path=" + quote(str(target))})


def _seam_gains(clips: list[Path]) -> list[float]:
    """Per-segment brightness shifts that make every join level.

    Measured the other way first — shift each clip to the first clip's *average* luma — and
    the seams got worse: a shot brightens across its own 7.5 s, so matching averages leaves
    the tail of one clip against the head of the next. What continuity needs is the join to
    level, so each segment is shifted by the difference between the previous clip's last
    frames and its own first frames, accumulated so the shifts do not fight each other.
    """
    shift = 0.0
    gains = [0.0]
    for index in range(1, len(clips)):
        delta = _edge_luma(clips[index - 1], "tail") + shift - _edge_luma(clips[index], "head")
        shift += delta
        gains.append(round(shift / 255.0, 4))
    return gains


def _edge_luma(path: Path, edge: str, frames: int = 8) -> float:
    """Mean luma of a clip's first or last frames."""
    import cv2

    capture = cv2.VideoCapture(str(path))
    values = []
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        values.append(float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean()))
    capture.release()
    if not values:
        raise HTTPException(409, f"读不到帧：{path.name}")
    window = values[:frames] if edge == "head" else values[-frames:]
    return sum(window) / len(window)


def _mean_luma(path: Path, frames: int = 24) -> float:
    """Average luma of a clip's first frames: what the brightness match aims at."""
    import cv2

    capture = cv2.VideoCapture(str(path))
    values = []
    while len(values) < frames:
        ok, frame = capture.read()
        if not ok:
            break
        values.append(float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean()))
    capture.release()
    if not values:
        raise HTTPException(409, f"读不到帧：{path.name}")
    return sum(values) / len(values)


class DenoiseSourceRequest(BaseModel):
    # The clip the video route has selected (a ComfyUI input file name), or a fresh one.
    name: str | None = None
    mode: str = video_denoise.DEFAULT_MODE
    strength: float | None = None


@ROUTER.post("/local/video/denoise-source")
async def denoise_source(body: DenoiseSourceRequest, identity: Identity = Depends(_identity)):
    """Denoise the selected clip without upscaling it — the "只降噪" recipe.

    The uploaded clip lives in ComfyUI's input folder, which the file route does not serve,
    so it is copied into the output tree first and the denoised cut is published from there.

    Whose clip, and whose output tree, are both the caller's business: the fallback used to read
    the **owner's** selection (and the one global ``_SOURCE``), and every result was written into
    the owner's output tree — so a shared visitor could denoise the machine owner's material and
    have the copy land in the owner's folder. A visitor's own selection and its own
    ``output/guests/<session>/`` are used instead, and a clip it does not own is treated as absent.
    """
    session = guest_uploads.OWNER_SESSION if identity.is_owner else identity.token
    name = (body.name or guest_uploads.selected_source(session)
            or (_SOURCE.get("path") if identity.is_owner else None) or "").strip()
    if not name:
        raise HTTPException(400, "还没有选择要降噪的视频")
    source = INPUT_ROOT / Path(name).name
    if not source.is_file():
        raise HTTPException(404, "这个视频不在 ComfyUI 的输入目录里")
    if not identity.is_owner and not _policy().is_visible_to(source.name, identity):
        # Not this caller's upload: refuse as if it did not exist (no confirmation of the name).
        raise HTTPException(404, "这个视频不在 ComfyUI 的输入目录里")
    target_dir = (guest_root(identity) if not identity.is_owner else DEFAULT_OUTPUT_ROOT) / "denoised"
    target_dir.mkdir(parents=True, exist_ok=True)
    staged = target_dir / source.name
    shutil.copyfile(source, staged)
    strength = body.strength if body.strength is not None else 2.0
    try:
        result = await asyncio.to_thread(video_denoise.run, staged, body.mode, strength)
    except video_denoise.DenoiseError as error:
        LOGGER.warning("denoise-source rejected: %s", error)
        raise HTTPException(409, str(error)) from error
    produced = Path(result["target"])
    LOGGER.info("denoise-source done: %s → %s", staged.name, produced.name)
    return JSONResponse({**result, "note": video_denoise.describe(result),
                         "video": "/local/video/file?path=" + quote(str(produced))})


@ROUTER.get("/local/video/interpolate/progress")
async def video_interpolate_progress():
    return JSONResponse(_interpolate().progress())


class DenoiseRequest(BaseModel):
    # The finished film, as the job snapshot reports it.
    path: str
    mode: str = video_denoise.DEFAULT_MODE


@ROUTER.post("/local/video/denoise")
async def denoise_film(body: DenoiseRequest, request: Request = None):
    """Denoise a finished film beside itself, and report the measured change.

    ffmpeg on the CPU, seconds per clip: no VRAM, no model, and the numbers come back with
    the file so the panel can show what it actually did (measured on this machine's own
    output: nlmeans −27% flat-region noise for −3% detail).
    """
    source = _servable(body.path, _caller(request))
    try:
        result = await asyncio.to_thread(video_denoise.run, source, body.mode)
    except video_denoise.DenoiseError as error:
        LOGGER.warning("denoise rejected: %s", error)
        raise HTTPException(409, str(error)) from error
    target = Path(result["target"])
    LOGGER.info("denoise done: %s → %s (%s)", source.name, target.name, result["mode"])
    return JSONResponse({**result, "video": "/local/video/file?path=" + quote(str(target)),
                         "note": video_denoise.describe(result)})


@ROUTER.get("/local/video/comfy")
async def video_comfy():
    """Per-step telemetry for the monitor, proxied from ComfyUI.

    The H3 node publishes its own state at /h3-monitor/state: phase, step counter,
    a latent sampler preview, per-phase timings and an ETA. ComfyUI is a different
    origin, so the panel reads it through here, together with the queue depth.
    """
    client = _client()
    monitor = queue = None
    try:
        response = await client.get(f"{COMFY}/h3-monitor/state", timeout=5)
        if response.status_code == 200:
            monitor = response.json()
    except (httpx.HTTPError, ValueError):
        monitor = None
    try:
        response = await client.get(f"{COMFY}/queue", timeout=5)
        if response.status_code == 200:
            payload = response.json()
            queue = {"running": len(payload.get("queue_running") or []),
                     "pending": len(payload.get("queue_pending") or [])}
    except (httpx.HTTPError, ValueError):
        queue = None
    return JSONResponse({"monitor": monitor, "queue": queue})


def visible_job(payload: dict | None, identity: Identity) -> dict | None:
    """One job's snapshot, with another caller's words taken out of it.

    The machine has one video monitor, so a visitor following *its* film would otherwise read
    whatever the owner last typed — and the owner would read the visitor's. The wording is
    replaced rather than the job hidden: the progress, the shots and the result file are all
    visible, because a running render is not a secret; the prompt is.
    """
    if payload is None or identity.is_owner:
        return payload
    if payload.get("submitted_by") and payload["submitted_by"] == identity.token:
        return payload
    redacted = dict(payload)
    if redacted.get("prompt"):
        redacted["prompt"] = "（其他设备提交的任务）"
    redacted.pop("final_prompt", None)
    return redacted


async def machine_comfy_state(client) -> dict | None:
    """What ComfyUI itself is rendering, slim enough to ride along with every poll.

    The gateway's monitor only knows the jobs *it* submitted, and that state lives in memory:
    a restart forgets a render that is still going inside ComfyUI. Measured live — after a
    gateway restart a visitor's H3 film kept sampling inside ComfyUI for over an hour while
    `/local/video/progress` answered `busy:false, job:null`, so the floating task window showed
    "no task" next to a running render. The H3 node publishes its own state, so the progress
    answer carries it now (without the base64 preview the panel's richer route ships).

    Returns ``None`` when ComfyUI is unreachable or has nothing to say.
    """
    try:
        response = await client.get(f"{COMFY}/h3-monitor/state", timeout=5)
        response.raise_for_status()
        state = response.json()
    except (httpx.HTTPError, OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    running = pending = 0
    try:
        queue = (await client.get(f"{COMFY}/queue", timeout=5)).json() or {}
        running = len(queue.get("queue_running") or [])
        pending = len(queue.get("queue_pending") or [])
    except (httpx.HTTPError, OSError, ValueError):
        pass
    return {
        "phase": state.get("phase"),
        "step": state.get("step"),
        "steps": state.get("steps"),
        "project": state.get("project"),
        "shot": state.get("shot"),
        "shots": state.get("shots"),
        "elapsed_seconds": state.get("elapsed_seconds"),
        "sampling_eta_seconds": state.get("sampling_eta_seconds"),
        "queue_running": running,
        "queue_pending": pending,
    }


@ROUTER.get("/local/video/progress")
async def video_progress(identity: Identity = Depends(_identity)):
    payload = _monitor().progress()
    # When this gateway has no job of its own, ask ComfyUI what it is doing: a render can
    # outlive a gateway restart, and "no task" next to a working GPU is worse than no window.
    comfy = None if payload.get("busy") else await machine_comfy_state(_client())
    return JSONResponse({**payload, "job": visible_job(payload.get("job"), identity),
                         "comfy": comfy})


@ROUTER.post("/local/video/cancel")
async def video_cancel(identity: Identity = Depends(_identity)):
    """Cancel the running film — but only your own.

    One monitor serves the whole machine, so "cancel" with no ownership check let a shared visitor
    stop the film the owner was rendering (and the other way round). The permission to *use* video
    generation is not a permission to touch somebody else's job; the owner's jobs carry no session.
    """
    monitor = _monitor()
    job = monitor.job
    if monitor.busy and job is not None and not identity.is_owner and job.submitted_by != identity.token:
        raise HTTPException(403, "这不是你的任务：机主正在跑的那一个只有机主能取消。")
    return JSONResponse(await monitor.cancel())


@ROUTER.get("/local/video/history")
async def video_history(identity: Identity = Depends(_identity)):
    entries = _monitor().history
    if identity.is_owner:
        return JSONResponse({"history": entries})
    mine = [visible_job(entry, identity) for entry in entries
            if str(entry.get("submitted_by") or "") == identity.token]
    return JSONResponse({"history": mine})


@ROUTER.get("/local/video/file")
async def video_file(path: str, identity: Identity = Depends(_identity)):
    """Serve a finished MP4 so the browser can play it inline.

    The path is absolute because a film can be written outside ComfyUI's output
    tree; it is still checked against the folders this caller may serve from, and a
    shared visitor may only fetch files its own session put there.
    """
    target = _servable(path, identity, missing="找不到该视频文件")
    return FileResponse(target, media_type="video/mp4", filename=target.name)


_CLIENT_PROVIDER = None
_MONITOR: "VideoMonitor | None" = None
# Prompt work for the video queue: the enhancer object (which owns "use the selected model") and
# the way to hand the card back to ComfyUI once the prompt is written. Both are injected by the
# gateway, because only it knows how to load a mode.
_ENHANCER = None
_RELEASE_FOR_RENDER = None


def set_prompt_enhancer(enhancer, release=None) -> None:
    """Bind the prompt optimiser used by the video queue.

    ``release`` is called after the description has been rewritten and before the render starts:
    the language model is only there for the prompt work, and H3 needs the VRAM back.
    """
    global _ENHANCER, _RELEASE_FOR_RENDER
    _ENHANCER = enhancer
    _RELEASE_FOR_RENDER = release
_INTERPOLATE: "video_interpolate.InterpolateMonitor | None" = None


_LOCAL_CLIENT: "httpx.AsyncClient | None" = None
_LOCAL_CLIENT_LOOP: "asyncio.AbstractEventLoop | None" = None


def _running_loop() -> "asyncio.AbstractEventLoop | None":
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _fallback_client() -> httpx.AsyncClient:
    """A client that always answers, for the routes that only talk to ComfyUI.

    The shared client belongs to the gateway's lifespan: it is created while the gateway starts and
    closed at shutdown, and a start that short-circuits (the bind probe) never creates it at all.
    Routes whose entire job is a request to ComfyUI must not depend on it — the per-step telemetry
    route did, and answered ``503 网关尚未就绪`` in exactly those states. The owner read that as a
    failed video generation while the render itself was running fine, because the panel shows the
    last failed request in its status line.

    The cached client is **tied to the loop it was created in**. An ``httpx.AsyncClient`` binds its
    connection pool on first use, so handing the same object to a second loop fails later with
    ``RuntimeError: Event loop is closed`` — measured here as a test that passed alone and failed
    right after another one (every ``IsolatedAsyncioTestCase`` gets its own loop, while this module
    keeps its state). The gateway runs one loop, so in production this only ever *adopts* the loop
    it is already in; anywhere two loops really exist, the stale client is replaced instead of
    reused.
    """
    global _LOCAL_CLIENT, _LOCAL_CLIENT_LOOP
    loop = _running_loop()
    if _LOCAL_CLIENT is not None and not _LOCAL_CLIENT.is_closed:
        if _LOCAL_CLIENT_LOOP is None and loop is not None:
            # Built before any loop existed; nothing has been bound yet, so adopt this one.
            _LOCAL_CLIENT_LOOP = loop
        if _LOCAL_CLIENT_LOOP is loop:
            return _LOCAL_CLIENT
    # Logged once per fallback client, not once per call: the panel polls this every few
    # seconds, and the old per-call line filled the gateway log with 1,278 copies.
    LOGGER.info("video: using a local client for ComfyUI traffic (no shared client yet)")
    _LOCAL_CLIENT = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5), trust_env=False,
                                      limits=httpx.Limits(max_connections=10,
                                                          max_keepalive_connections=5))
    _LOCAL_CLIENT_LOOP = loop
    return _LOCAL_CLIENT


def _client() -> httpx.AsyncClient:
    """The shared client when the gateway has one, otherwise a client of our own.

    Never raises: a request that reaches this point is a real request from the panel, and refusing
    it with 503 tells the owner nothing they can act on. Callers that need the *language model*
    reach it through the gateway's own routes, not through this client.
    """
    client = _CLIENT_PROVIDER() if callable(_CLIENT_PROVIDER) else _CLIENT_PROVIDER
    if client is None:
        client = _fallback_client()
    return client


def _monitor() -> VideoMonitor:
    """The live monitor, created on first use once the gateway has its client."""
    global _MONITOR
    if _MONITOR is None:
        _MONITOR = VideoMonitor(_client())
    return _MONITOR


def monitor() -> "VideoMonitor | None":
    """The live monitor if one exists, for callers outside this router.

    The GPU arbiter uses this to cancel a running film when the owner takes the card;
    it must not create a monitor, so it returns None rather than instantiating one.
    """
    return _MONITOR


def _interpolate() -> "video_interpolate.InterpolateMonitor":
    global _INTERPOLATE
    if _INTERPOLATE is None:
        _INTERPOLATE = video_interpolate.InterpolateMonitor(_client())
    return _INTERPOLATE


def _guard_idle() -> None:
    """One GPU, one job: a film run and an interpolation must not overlap."""
    if _monitor().busy or _interpolate().busy:
        raise HTTPException(409, "已有视频任务正在运行，请等待完成或先取消。")


def interpolate_monitor() -> "video_interpolate.InterpolateMonitor | None":
    """The interpolation monitor if one exists — for callers that must not create one.

    页面关闭后的收尾要问一句"还有没有活在跑"；问这句话不该顺手把监控对象建出来，
    所以规矩和 `monitor()` 一样：只回答已经存在的那个。
    """
    return _INTERPOLATE


def active_prompt_id() -> str | None:
    """The ComfyUI prompt a running video job owns, for the queue cleaner.

    A mode switch while an H3 run is loading its model sees an idle GPU and would
    otherwise mistake the render for leftover work and interrupt it.
    """
    job = _MONITOR.job if _MONITOR is not None else None
    return job.prompt_id if job is not None and _MONITOR.busy else None


def install(app, client_provider, owner_dependency=None, identity_resolver=None,
            policy_store=None) -> None:
    """Attach the router now; bind the shared client lazily.

    Routes must exist before the ASGI application starts walking its table, but
    the HTTP client is created later inside the lifespan, so it is resolved on the
    first request instead of here.

    Every route gets a per-route guard rather than one blanket owner dependency: the
    handlers upload files, spend GPU time on the owner's ComfyUI and read output
    folders, so an invited guest could drive them directly even though the panel hides
    that view. A visitor is admitted only for the features the owner has opened in
    ``guest_policy`` (``GUEST_ROUTES`` maps each route to one); everything else still goes
    through the gateway's own owner check, whose 403 wording stays authoritative. The identity
    resolver decides which session a caller is, which is what keeps one visitor's clips, film
    output folder and clip choice away from another's.
    """
    global _CLIENT_PROVIDER
    _CLIENT_PROVIDER = client_provider
    if identity_resolver is not None:
        set_identity_resolver(identity_resolver)
    if policy_store is not None:
        set_policy_store(policy_store)
    for route in ROUTER.routes:
        for method in getattr(route, "methods", None) or ():
            route.dependencies.append(Depends(GuestRouteGuard(method, route.path, owner_dependency)))
    app.include_router(ROUTER)
