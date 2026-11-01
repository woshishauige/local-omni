"""Mode control and streaming proxy for the bundled llama.cpp UI.

The default deployment stays loopback-only. Sharing the UI with another machine
is opt-in per machine: QWEN_LISTEN_HOST widens the bind address, QWEN_ALLOWED_
HOSTS names the addresses clients may use, and QWEN_ACCESS_TOKEN puts a shared
passphrase in front of every route. Without a token the service trusts whoever
can reach the port, which is why the public name and the token are configured
together rather than by widening the bind address alone.
"""

import asyncio
import base64
import binascii
from contextlib import asynccontextmanager, suppress
from datetime import datetime
import hmac
import hashlib
import html
import json
import logging
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
from urllib.parse import quote
import uuid

import anyio
import httpx
import psutil
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from starlette.datastructures import MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware
import uvicorn

from app.qwen_config import (
    MODEL,
    clear_switching,
    mark_switching,
    PROFILES,
    profile_label,
    ROOT,
    SETTINGS_PATH,
    SHARE_ENV_NAMES,
    active_executable,
    active_gpu_layers,
    active_paths,
    active_profile_name,
    expected_context,
    expected_mtp,
    gateway_port,
    mtp_capable,
    public_profiles,
    read_settings,
    server_command,
    share_setting,
    source_switch,
    tests_are_running,
    thinking_levels,
    voice_context,
)
from app.share.share_store import ShareStore
from app.usage.usage_store import UsageStore
from app.usage.usage_tracking import GENERATION_PATHS, UsageMiddleware
from app.context.context_compaction import CompactionError, ContextCompactor
from app.context.conversation_ledger import ConversationLedger, estimate_tokens
from app.context.page_lifecycle import PageLifecycle
from app.image.image_jobs import (
    ImageGenerateRequest,
    ImageJobMonitor,
    clear_dead_comfy_work,
    comfy_image_url,
    image_metadata,
)
from app import agent_briefing
from app.context import context_guard
from app.image import image_comfy
from app.image import image_pipelines
from app.context import loop_guard
from app import gpu_arbiter
from app.share import guest_policy
from app.context import mode_isolation
from app import prompt_enhance
from app.share import guest_uploads
from app.video import video_denoise
from app.video import video_routes
from app.voice import voice_input
from app.voice import voice_mode

UPSTREAM = "http://127.0.0.1:8082"
# The same port as a number, for the callers that must hand it to something else (voice
# mode borrows the backend on this port rather than starting a copy of its own).
UPSTREAM_PORT = 8082
LOGGER = logging.getLogger("qwen.local")
METADATA_PATHS = {"props", "models", "v1/models", "tools"}
COMFY = "http://127.0.0.1:8188"
COMFY_INPUT = ROOT.parent / "ComfyUI-BSAI_v010" / "ComfyUI" / "input"
# Where ComfyUI publishes: the generated pictures and films, including each visitor's own
# ``guests/<session>/`` folder. Served by the gateway rather than by ComfyUI's port, because a
# shared page opened from the tunnel cannot reach the owner's 127.0.0.1.
COMFY_OUTPUT = COMFY_INPUT.parent / "output"

# Sharing knobs. Read per request (not at import) so a restart is not needed to
# pick up an edited settings.json, and an environment variable always wins over
# the file value because the launcher is the outermost authority.
ACCESS_COOKIE = "qwen_access"
# Cookie carrying the owner's admin token, set only by an explicit local opt-in.
ACCESS_ADMIN_COOKIE = "qwen_admin"
# Two distinct parameters, deliberately: OWNER_PARAM marks a browser as the
# owner's and is exchanged for a cookie, while GATE_PARAM only carries the token
# for a request that must pass the gate. Sharing one name would make every probe
# answer with the cookie-exchange redirect instead of the route's own response,
# which the watchdog reads as an unhealthy gateway and turns into a restart loop.
OWNER_PARAM = "admin"
GATE_PARAM = "gate"
# Asking for the GPU immediately when it is busy with someone else's work. Only the
# owner's console sends it, and the proxy refuses it for anyone else.
GPU_PREEMPT_PARAM = "gpu"
GPU_PREEMPT_VALUE = "preempt"
ACCESS_TOKEN_ENV = "QWEN_ACCESS_TOKEN"
ALLOWED_HOSTS_ENV = "QWEN_ALLOWED_HOSTS"
ALLOWED_ORIGINS_ENV = "QWEN_ALLOWED_ORIGINS"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "0:0:0:0:0:0:0:1"}


def is_loopback_host(host):
    return host.strip().strip("[]").lower() in {name.strip("[]") for name in LOOPBACK_HOSTS}


def _parse_patterns(raw):
    """Allowlist entries from either a comma-separated string or a JSON list.

    The sharing overlay is hand-editable JSON, and there a list is the natural
    way to write several names. Stringifying it would turn one entry into a
    literal bracket expression that matches nothing.
    """
    if isinstance(raw, (list, tuple, set)):
        return [str(entry).strip() for entry in raw if str(entry).strip()]
    patterns = [entry.strip() for entry in str(raw).split(",")]
    return [entry for entry in patterns if entry]


def _local_settings():
    """Live manager settings, falling back to the file before the manager exists."""
    settings = getattr(manager, "settings", None)
    return settings if isinstance(settings, dict) else read_settings()


def access_token():
    """The passphrase in force.

    The store is asked first because rotating it from the manager panel is a live
    action, and an environment variable captured when the service was launched
    would otherwise shadow the rotation: the panel would report the new passphrase
    while the old one still worked. Environment variables and the sharing overlay
    remain the source a first launch seeds the store from.
    """
    stored = share_store.pin
    if isinstance(stored, str) and stored:
        return stored
    return str(share_setting("access_token", _local_settings()) or "").strip()


def access_entries(name):
    """Configured allowlist entries, which may include host:port and wildcards."""
    raw = share_setting(name, _local_settings())
    entries = _parse_patterns("" if raw is None else raw)
    if len(entries) > 64:
        raise RuntimeError(f"{name} accepts at most 64 entries")
    return entries


def allowed_host_names():
    """Host header values accepted: the local defaults plus anything configured.

    Misconfiguring the list fails closed. If the name a client actually asks for
    is missing, requests are rejected with 400 instead of being served, so an
    address that is not shared can never reach the service.
    """
    names = ["127.0.0.1", "localhost", "[::1]"]
    for name in (listening_host(), *access_entries("allowed_hosts")):
        if name and name not in names:
            names.append(name)
    return names


def allowed_origin_names():
    origins = {"http://127.0.0.1:8081", "http://localhost:8081"}
    origins.update(access_entries("allowed_origins"))
    return origins


def listening_host():
    """Bind address, so a widened bind shares the LAN address automatically."""
    host = str(share_setting("listen_host", _local_settings()) or "").strip()
    return host or "127.0.0.1"


def listening_port():
    """Same resolution the watchdog uses, so both watch the same port."""
    return gateway_port(manager.settings)


def seed_passphrase():
    """Adopt the configured passphrase on the very first start, and only then.

    The live value lives in the store so the manager panel can rotate it. Seeding
    only an empty store is what keeps a rotation from being reverted the next time
    the watchdog restarts this process with the older configured value.
    """
    if share_store.pin:
        return False
    configured = str(share_setting("access_token", _local_settings()) or "").strip()
    if len(configured) > 256:
        raise RuntimeError("access_token must be 256 characters or fewer")
    if configured:
        share_store.set_pin(configured)
        return True
    return False


def token_matches(candidate):
    """Constant-time comparison so a wrong token cannot be probed byte by byte."""
    expected = access_token()
    if not expected or not candidate:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def request_host(request):
    return split_host_port(request.headers.get("host", ""))


def split_host_port(header):
    if header.startswith("["):  # bracketed IPv6 literal, with or without a port
        name, _, remainder = header.partition("]")
        return (name + "]").lower(), remainder.lstrip(":").strip()
    name, _, port = header.partition(":")
    return name.lower(), port.strip()


def host_is_allowed(request):
    name, _ = request_host(request)
    if not name:
        return False
    for pattern in allowed_host_names():
        if pattern == "*":
            return True
        if pattern == name:
            return True
        if pattern.startswith("*.") and name.endswith(pattern[1:]) and name != pattern[2:]:
            # Starlette's own wildcard rule: one extra leading label, so an
            # attacker-controlled deep subdomain never matches *.example.com.
            return True
    return False


def write_plain_response(send, status, body, content_type="text/plain; charset=utf-8"):
    body = body.encode("utf-8")

    async def app(scope, receive, send_):
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", content_type.encode("ascii")),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
        ]})
        await send({"type": "http.response.body", "body": body})

    return app


def login_page(message=""):
    """The passphrase prompt. It never contains the passphrase itself."""
    notice = f'<p class="notice">{html.escape(message)}</p>' if message else ""
    action = "/__login"
    return HTMLResponse(
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex">'
        '<title>需要访问口令</title><style>'
        'body{margin:0;padding:0;background:#101216;color:#e8eaed;font:16px/1.6 system-ui,sans-serif}'
        'main{max-width:360px;margin:0 auto;padding:18vh 24px}'
        'h1{font-size:20px;margin:0 0 8px}p.hint{color:#9aa0a6;margin:0 0 24px}'
        'input{width:100%;box-sizing:border-box;padding:12px 14px;border-radius:10px;'
        'border:1px solid #3c4043;background:#1b1e23;color:#e8eaed;font-size:16px}'
        'button{width:100%;margin-top:14px;padding:12px;border:0;border-radius:10px;'
        'background:#8ab4f8;color:#101216;font-size:16px;font-weight:600;cursor:pointer}'
        'p.notice{color:#f28b82;margin:14px 0 0}'
        '</style></head><body><main>'
        '<h1>需要访问口令</h1>'
        '<p class="hint">请输入管理员提供的一次性口令，之后本设备会自动记住。</p>'
        f'{notice}'
        '<form method="post" action="/__login">'
        '<input type="password" name="token" autocomplete="current-password" '
        'autofocus placeholder="访问口令">'
        '<button type="submit">进入</button></form>'
        '</main></body></html>',
        status_code=401,
        headers={"Cache-Control": "no-store"},
    )


def derive_allowed_origins():
    """Extra trusted origins for state-changing requests, beyond same-origin.

    A browser may reach this service through a name that is not localhost, so a
    trusted origin follows the configured names instead of assuming loopback.
    Requests without an Origin header (GET/HEAD, and most non-browser clients)
    stay allowed: this guard exists to reject cross-site form posts, not to
    authenticate, which is the access token's job.
    """
    origins = set(allowed_origin_names())
    for name in access_entries("allowed_hosts"):
        if name == "*":
            origins.add("*")
            continue
        if "://" in name:
            origins.add(name.rstrip("/"))
            continue
        host, _, port = name.partition(":")
        for candidate in ((host, port) if port else ((host, "443"), (host, "80"))):
            scheme = "https" if candidate[1] == "443" else "http"
            suffix = "" if candidate[1] == "443" else f":{candidate[1]}"
            origins.add(f"{scheme}://{candidate[0]}{suffix}")
    return origins


def origin_is_trusted(origin, header_host):
    """Accept same-origin requests so a tunnel or LAN address keeps working.

    Behind a tunnel the scheme the client used (https) differs from the one this
    server sees (http), so the comparison is done on the host and port the client
    actually asked for; configured entries are the escape hatch for anything
    else, such as a distinct front-end origin.
    """
    if not origin:
        return True
    trusted = derive_allowed_origins()
    if "*" in trusted:
        return True
    normalized = origin.strip().rstrip("/")
    if normalized.lower() in {entry.lower() for entry in trusted}:
        return True
    origin_name, _, origin_port = normalized.partition("://")[2].partition("/")[0].partition(":")
    header_name, header_port = split_host_port(header_host)
    if not origin_name or origin_name.lower() != header_name:
        return False
    # Only the port the client actually used may differ from the forwarded one.
    return not origin_port or not header_port or origin_port == header_port


def write_process_record(path, data):
    """Publish a complete identity record, preserving the old one on write failure."""
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(data), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_backend_record(path):
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise ValueError("Backend identity record must be an object")
    for key in ("pid", "created_unix_ms", "port"):
        if type(record.get(key)) is not int or record[key] <= 0:
            raise ValueError(f"Invalid backend identity field: {key}")
    if not isinstance(record.get("model"), str) or not record["model"]:
        raise ValueError("Invalid backend identity field: model")
    return record


class ComfyBusyError(RuntimeError):
    pass


class AdoptedProcess:
    """Provide the Popen operations needed for a verified surviving backend."""

    def __init__(self, process):
        self.process = process
        self.pid = process.pid

    def poll(self):
        return None if self.process.is_running() else 0

    def terminate(self):
        self.process.terminate()

    def kill(self):
        self.process.kill()

    def wait(self):
        return self.process.wait()
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
    "content-encoding",
}
INJECTION = (
    '<link rel="stylesheet" href="/local/mode-control.css?v=12">'
    '<script defer src="/local/mode-control.js?v=25"></script>'
    # 关页面时让后台跟着收工：只发一个信标，判定与宽限期都在网关侧（page_lifecycle.py）。
    '<script defer src="/local/page-lifecycle.js?v=2"></script>'
    '<link rel="stylesheet" href="/local/image-panel.css?v=15">'
    '<script defer src="/local/image-panel.js?v=18"></script>'
    # The version query is what makes a browser pick up an edited panel file; without a
    # bump the old script stays cached and the panel keeps its previous behaviour.
    '<link rel="stylesheet" href="/local/video-panel.css?v=16">'
    '<script defer src="/local/video-panel.js?v=24"></script>'
    '<link rel="stylesheet" href="/local/usage-log.css?v=2">'
    '<script defer src="/local/usage-log.js?v=2"></script>'
    '<link rel="stylesheet" href="/local/context-control.css?v=2">'
    '<script defer src="/local/context-control.js?v=3"></script>'
    '<link rel="stylesheet" href="/local/share-manager.css?v=2">'
    # The floating task window is shell-level, not panel-level: it is injected on every page
    # and reads only the two progress endpoints, so it exists in every mode (纯文本识图/语音/
    # 生图/视频) and survives switching between them. Collapsed it is one movable circle.
    '<link rel="stylesheet" href="/local/task-hud.css?v=1">'
    '<script defer src="/local/task-hud.js?v=1"></script>'
    # Voice input is injected globally rather than per panel. In the chat it is docked at the
    # bottom-right of the conversation box (next to the per-conversation tool-permission chip,
    # in the same dock); every prompt box in the image and video panels carries a microphone of
    # its own in the bottom-right corner of the box; any other text field still gets the floating
    # button that follows the caret.
    '<link rel="stylesheet" href="/local/voice-input.css?v=8">'
    '<script defer src="/local/voice-input.js?v=10"></script>'
    # 思考档位：纯文、识图两种模式的对话框里一个小分段控件，选中的档位随请求。
    # `X-Qwen-Thinking` 走（网关按档位的模板白名单翻译）。全局注入、自己判断当前模式，
    # 所以在生图/视频/语音页里它不出现。
    '<link rel="stylesheet" href="/local/thinking-control.css?v=1">'
    '<script defer src="/local/thinking-control.js?v=1"></script>'
    # Inline and undelayed: the desktop entry opens this page as a standalone app
    # window, where the page title names the taskbar button, and the bundled UI sets
    # its own title while it boots. Re-applying it a few times is what keeps the
    # window labelled as the sharing panel instead of the generic product name.
    '<script>(function(){var t="Qwen 共享面板";var set=function(){if(document.title!==t)'
    'document.title=t;};set();[50,400,1500,4000].forEach(function(ms){setTimeout(set,ms);});'
    '})();</script>'
    '<script defer src="/local/share-manager.js?v=3"></script>'
    # Voice conversation mode. It mounts its launcher into the mode toolbar once
    # that exists, so it is loaded after the toolbar script.
    '<link rel="stylesheet" href="/local/voice-panel.css?v=10">'
    '<script defer src="/local/voice-panel.js?v=32"></script>'
    # 视频分析不再是一个独立模块：打包应用自己的附件区就能把视频发给模型（它的请求体里
    # 构、`input_video`），识图模式本来就能读视频。留一个「视频」入口只会让人以为视频识。
    # 是另一套能力，而且它还会在工具栏左侧多出一个模块。
)


class ModeRequest(BaseModel):
    mode: str


class ModelRequest(BaseModel):
    """Which installed model profile the text/vision backend should serve."""
    profile: str


def write_model_profile(name):
    """Persist the selected profile into settings.json, preserving every other key.

    The file is hand-edited and carries a long list of tuned values, so the whole
    document is rewritten from what was read plus this one change: dropping
    unknown keys here would silently reset the owner's configuration.
    """
    settings = read_settings()
    settings["model_profile"] = name
    SETTINGS_PATH.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def profile_installed(name):
    """Whether both files of a profile are on disk.

    A profile whose weights are still downloading must be refused with a message
    that says so, rather than launching llama-server to fail on a missing path.
    """
    for path in PROFILES[name].values():
        if isinstance(path, Path) and not path.is_file():
            return False
    return True


class ManagedStreamingResponse(StreamingResponse):
    def __init__(self, *args, cleanup, **kwargs):
        super().__init__(*args, **kwargs)
        self.cleanup = cleanup

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.cleanup()


class ModeManager:
    def __init__(self):
        self.settings = read_settings()
        self.mode = self.settings["default_mode"]
        self.state = "starting"
        self.error = None
        self.revision = 0
        self.active_requests = 0
        self.process = None
        self.log_files = []
        self.lock = asyncio.Lock()
        self.task = None
        self.client = None
        self.mtp_enabled = False
        self.last_switch_seconds = None
        # Why the current mode got there: same weights (a plugin change) or a different
        # profile (a whole-model switch). Recorded by load() so every entry point reports
        # the same explanation instead of each one guessing from the mode name.
        self.last_switch = None
        # Which profile and which window the resident backend was launched with. Both are
        # recorded at launch rather than recomputed: a mode may run different weights *or* a
        # different window on the same weights (voice does), and the status must describe the
        # process rather than the file.
        self.launched_profile = None
        self.launched_context = None
        self.html = None
        self.instance_id = uuid.uuid4().hex
        self.metadata = {}
        self.watchdog_task = None
        self.retry_at = 0
        self.recovery_attempts = 0
        self.sleeping = False
        self.health_failures = 0
        self.next_health_check = 0
        # True while voice mode holds the card with its own 9B and recogniser. It
        # suppresses this manager's automatic recovery so the 27B is not pulled back
        # in and does not evict the model the user is speaking to.
        self.voice_holds_card = False
        self.image_task = None
        self.image_result = None
        # A restart is the only action worth remembering; the queue verdict itself
        # is re-evaluated on every attempt so a slow GPU-idle window still recovers.
        self.comfy_restart_attempted = False
        # Off by default: clearing ComfyUI's queue cannot tell a stale entry that
        # outlived a crashed run apart from work another client just submitted, so
        # it must be opt-in rather than the shipped behaviour.
        self.clear_stale_comfy = False

    def mtp_reason(self, mtp_now, borrowed):
        """Why the MTP line reads what it reads, in the terms of the model that is loaded.

        The settings.json string describes the machine's default model, so reusing it while
        another profile is resident produced the worst kind of status: a sentence about MTP
        sitting next to a boolean that contradicted it.
        """
        if borrowed is not None:
            return (f"当前常驻的是 {borrowed}（被语音借用）；它"
                    + ("带草稿头，MTP 开着。" if mtp_now else "没有草稿头，MTP 关闭。"))
        if self.mode == "image":
            return "生图模式由 ComfyUI 托管 Klein，MTP 关闭。"
        if self.mode == "video":
            return "视频生成模式由 ComfyUI 托管 MiniMax-H3，MTP 关闭。"
        if not mtp_now:
            return ("当前模型没有草稿头（profile 未提供MTP），MTP 关闭。"
                    if not mtp_capable(self.settings) else "MTP 已在设置里关闭。")
        if self.mode == "text":
            return self.settings["mtp_reason"]
        return "识图模式与文本共用同一个后端（投影常驻、草稿头同时启用），切换模式不重启。"

    def status(self):
        if self.state == "ready" and self.process and self.process.poll() is not None:
            self.state = "error"
            self.error = "模型进程已退出，正在准备自动恢复。"
            self.retry_at = time.monotonic() + 2
            LOGGER.error("Backend exited: pid=%s", self.process.pid)
        job = image_jobs.snapshot() if image_jobs else None
        # Keep the UI in a "switching" state while a generation cycle owns the
        # backend; the native capability props must not be reloaded mid-job.
        job_busy = bool(image_jobs and image_jobs.progress()["busy"])
        reported_state = "switching" if job_busy and self.state != "error" else self.state
        image = {"job_id": job["id"], "state": job["state"], "phase": job["phase"],
                 "progress": job["progress"]} if job else {}
        # "video" is its own top-level mode beside text/vision/generate; only the
        # image pipeline needs its internal name hidden behind the panel's label.
        # While voice mode has borrowed the backend, it is the voice profile that is
        # actually answering, so every model-derived field is reported from *it*. Returning
        # the file's selection next to a loaded 9B is how the panel, the log and the switch
        # rule end up telling three different stories about one model.
        resident = getattr(self, "launched_profile", None) or active_profile_name(self.settings)
        borrowed = resident if resident != active_profile_name(self.settings) else None
        # Everything model-derived below comes from `resident`, MTP included: one backend
        # has one truth, and the panel must not be able to show two of them at once.
        mtp_now = self.mtp_enabled if borrowed is None else None
        if borrowed is not None:
            mtp_now = bool(PROFILES[borrowed].get("mtp")) and bool(self.settings["mtp_enabled"])
        public_mode = "generate" if self.mode == "image" else self.mode
        return {
            "service": "qwen-local-modes",
            "instance_id": self.instance_id,
            "mode": public_mode, "state": reported_state, "error": self.error,
            "revision": self.revision, "active_requests": self.active_requests,
            "mtp_enabled": self.mtp_enabled if borrowed is None else mtp_now,
            "mtp_reason": self.mtp_reason(mtp_now, borrowed),
            "last_switch_seconds": self.last_switch_seconds,
            # Report the numbers the *resident* profile launches with, not the settings
            # defaults and not the file's selection: with two models installed all three
            # can differ, and the panel must describe the model that is answering.
            "gpu_layers": active_gpu_layers(self.settings, self.mode) if borrowed is None else
                PROFILES[borrowed].get("gpu_layers", self.settings["gpu_layers"]),
            "threads": self.settings["threads"],
            # The window the resident process was launched with, not the file's selection: a
            # mode may run a smaller one on the same weights, and reporting the file's number
            # there is what made the panel promise room the process did not have.
            "context_size": getattr(self, "launched_context", None) or (
                expected_context(self.settings) if borrowed is None else
                PROFILES[borrowed].get("context_size", self.settings["context_size"])),
            "model_profile": resident,
            "model_alias": active_paths(self.settings)[0].stem if borrowed is None else
                PROFILES[borrowed]["model"].stem,
            # Which profile the file selects, when that is not the one resident. Reported
            # separately so "selected" and "serving" can never be confused for each other.
            "selected_profile": active_profile_name(self.settings),
            "borrowed_by_voice": borrowed is not None,
            # Which profile the *resident backend* was actually launched for, which is
            # not always the selected one (a hand-edited settings.json, or voice mode
            # holding the card). The panel and the switch rule both read this rather than
            # assuming the two agree.
            "loaded_profile": getattr(self, "launched_profile", None),
            # Whether the current mode reuses the resident weights or needed its own,
            # so the panel can say "只换插件" instead of showing an unexplained reload.
            "source": source_switch(self.mode, launched=getattr(self, "launched_profile", None),
                                    settings=self.settings),
            # ...and what the transition that got here actually decided, for the same
            # reason: one explanation, recorded where the transition happened.
            "last_switch": self.last_switch,
            "model_profiles": public_profiles(),
            # 思考档位：面板按这个清单渲染，网关按同一清单校验请求、—、一处定义。
            "thinking_levels": list(thinking_levels(self.settings)),
            "agent_enabled": self.settings["agent_enabled"],
            "sleeping": self.sleeping,
            "recovery_attempts": self.recovery_attempts,
            "retry_in_seconds": max(0, round(self.retry_at - time.monotonic())) if self.retry_at else None,
            "image": image,
        }

    async def recover(self, manual=False):
        async with self.lock:
            self.status()
            # Voice mode owns the card while it is up. Its 9B and the recogniser were
            # loaded precisely because the 27B does not fit beside them, so bringing
            # the 27B back here would evict the very model the user is talking to.
            if getattr(self, "voice_holds_card", False) and not manual:
                return False
            if self.state in {"starting", "switching", "stopping"} or self.active_requests:
                return False
            if self.state == "ready":
                return False
            if manual:
                self.recovery_attempts = 0
            if self.task and not self.task.done():
                return False
            self.state = "starting"
            self.error = None
            self.retry_at = 0
            self.task = asyncio.create_task(self.load(self.mode))
            return True

    async def watchdog(self):
        while True:
            await asyncio.sleep(2)
            self.status()
            if self.state == "ready" and time.monotonic() >= self.next_health_check:
                await self.check_backend_health()
            if self.state in {"waiting", "error"} and self.retry_at and time.monotonic() >= self.retry_at:
                await self.recover()

    async def check_backend_health(self):
        """Probe idle backends without waking a sleeping model or interrupting work."""
        # image and video deliberately hold no language backend: both hand the card to ComfyUI,
        # and video keeps it for the whole render. Probing there can only fail, and three
        # failures escalate into "模型服务连续未响应，正在准备自动恢复。 、measured live as a
        # 30-second ready 、error 、recover loop for as long as the mode stayed on video
        # (recover() then reloaded the language model, which ComfyUI refuses while it renders).
        if self.mode in {"image", "video"} or self.state != "ready" or self.active_requests or self.client is None:
            return
        process = self.process
        revision = self.revision
        self.next_health_check = time.monotonic() + 10

        def still_current() -> bool:
            """True while nothing else has reloaded, switched or started using the backend."""
            return (self.state == "ready" and not self.active_requests
                    and self.process is process and self.revision == revision)

        try:
            response = await self.client.get(f"{UPSTREAM}/health", timeout=3)
            response.raise_for_status()
            health = response.json()
            if not isinstance(health, dict) or health.get("status") != "ok":
                raise ValueError("Backend health response is not ready")
            # These two GET endpoints neither wake the model nor reset its idle timer.
            # A *lent* backend is the exception: a voice turn runs inside this same
            # process without going through the gateway, so the probe can land in the
            # middle of one. Its verdict would be misleading either way, and the voice
            # session reports its own failures, so the probe stands down while it lasts.
            if voice_mode.VOICE.shared_brain_port is not None:
                return
            response = await self.client.get(f"{UPSTREAM}/props", timeout=3)
            response.raise_for_status()
            props = response.json()
            if not isinstance(props, dict):
                raise ValueError("Invalid backend properties")
        except (httpx.HTTPError, OSError, ValueError) as exc:
            async with self.lock:
                if not still_current():
                    return
                self.health_failures += 1
                LOGGER.warning("Backend idle health check failed (%s/3): %s", self.health_failures, exc)
                if self.health_failures >= 3:
                    self.state = "error"
                    self.error = "模型服务连续未响应，正在准备自动恢复。"
                    self.sleeping = False
                    self.retry_at = time.monotonic() + 2
            return
        async with self.lock:
            if not still_current():
                return
            self.health_failures = 0
            self.sleeping = bool(props.get("is_sleeping", False))
            self.metadata["props"] = props

    async def adopt_backend(self):
        record_path = ROOT / "backend.process.json"
        if not record_path.exists():
            return False
        try:
            record = read_backend_record(record_path)
            process = psutil.Process(record["pid"])
            command = process.cmdline()
            mode = "vision" if "--mmproj" in command else "text"
            # With the unified backend the expected command is the same for both modes, so
            # adoption has to compare against that one instead of the per-mode line.
            unified = bool(self.settings.get("unified_backend", True))
            expected = self.unified_command() if unified else server_command(mode, settings=self.settings)
            # The selected profile decides which model is legitimate, not the module
            # constant: with a second model installed, comparing against MODEL would
            # reject the surviving backend of whichever profile is actually selected.
            expected_model, _ = active_paths(self.settings)
            valid = (
                abs(process.create_time() * 1000 - record["created_unix_ms"]) < 1500
                and Path(process.exe()).resolve() == Path(active_executable(self.settings)).resolve()
                and record["port"] == 8082 and Path(record["model"]).resolve() == expected_model.resolve()
                and command[1:] == expected[1:]
            )
            if not valid:
                LOGGER.warning("Backend identity/configuration did not match; leaving it untouched")
                return False
            response = await self.client.get(f"{UPSTREAM}/props", timeout=10)
            response.raise_for_status()
            props = response.json()
            # The profile owns the context size (the 9B runs 32 K where the 27B runs
            # 48 K), so validating against settings alone would reject a correctly
            # loaded second model.
            if props["default_generation_settings"]["n_ctx"] != expected_context(self.settings):
                return False
            self.process = AdoptedProcess(process)
            self.launched_command = None
            self.mode = mode
            self.mtp_enabled = bool("--spec-type" in command and "draft-mtp" in command)
            await self.cache_metadata(props)
            self.state = "ready"
            self.sleeping = bool(props.get("is_sleeping", False))
            self.health_failures = 0
            self.revision += 1
            LOGGER.info("Adopted surviving backend pid=%s mode=%s", process.pid, mode)
            return True
        except (OSError, ValueError, KeyError, TypeError, IndexError, RecursionError, psutil.Error, httpx.HTTPError):
            LOGGER.exception("Could not adopt recorded backend")
            return False

    async def remove_owned_orphan(self):
        """Stop only a recorded llama-server with a matching executable/model/port.

        This covers a manager crash while the backend survives. A stale or mismatched
        record is left alone so startup cannot terminate another local model.
        """
        record_path = ROOT / "backend.process.json"
        if not record_path.exists():
            return False
        try:
            record = read_backend_record(record_path)
            process = psutil.Process(record["pid"])
            command = process.cmdline()
            port_index = command.index("--port") + 1 if "--port" in command else -1
            model_index = command.index("-m") + 1 if "-m" in command else -1
            # Match whichever profile is selected, so a surviving 9B backend is still
            # recognised as ours instead of being treated as a foreign process.
            expected_model, _ = active_paths(self.settings)
            owned = (
                abs(process.create_time() * 1000 - record["created_unix_ms"]) < 1500
                and Path(process.exe()).resolve() == Path(active_executable(self.settings)).resolve()
                and int(record["port"]) == 8082
                and 0 < model_index < len(command) and Path(command[model_index]).resolve() == expected_model.resolve()
                and 0 < port_index < len(command) and command[port_index] == "8082"
            )
            if not owned:
                return False
            LOGGER.warning("Stopping surviving owned backend pid=%s before a clean reload", process.pid)
            process.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(process.wait), 15)
            except asyncio.TimeoutError:
                process.kill()
                await asyncio.to_thread(process.wait)
            record_path.unlink(missing_ok=True)
            (ROOT / "backend.pid").unlink(missing_ok=True)
            return True
        except (OSError, ValueError, KeyError, TypeError, IndexError, RecursionError, psutil.Error):
            LOGGER.exception("Could not inspect surviving backend record")
            return False

    async def cache_metadata(self, props):
        metadata = {"props": props}
        for path in METADATA_PATHS - {"props"}:
            try:
                response = await self.client.get(f"{UPSTREAM}/{path}", timeout=10)
                if response.is_success:
                    metadata[path] = response.json()
            except (httpx.HTTPError, OSError, ValueError):
                # Optional UI metadata must not discard a successfully loaded model.
                LOGGER.warning("Could not cache optional backend metadata: %s", path)
        self.metadata = metadata

    async def initialize(self):
        if not await self.adopt_backend():
            await self.remove_owned_orphan()
            await self.load(self.mode)

    def unified_command(self):
        """The single backend that serves both text and vision.

        The vision layer count is the conservative one (it was tuned for exactly this
        memory profile), and it is applied to text too —that is the price of never
        restarting, and it is smaller than the per-mode difference the old policy used.
        """
        return server_command(
            "vision", settings=self.settings,
            mtp=bool(self.settings["mtp_enabled"]), mmproj=True,
        )

    def backend_matches_unified(self) -> bool:
        """Whether the running backend already is the unified one.

        The command line is not re-read from the process: a `subprocess.Popen` has no
        ``cmdline()`` (only a psutil Process does), and asking for one raised
        AttributeError inside the switch itself —which is what turned every vision switch
        into a failed reload. The list we launched with is the authority for our own child,
        and an adopted backend is validated against psutil at adoption time.
        """
        process = self.process
        if process is None or process.poll() is not None:
            return False
        launched = getattr(self, "launched_command", None)
        if launched is None:
            return isinstance(process, AdoptedProcess)
        # A backend launched for another profile must never be reused by the slice
        # switch: the profiles differ in weights, context size and MTP capability, so
        # "no restart" would answer from the previous model under the new label.
        launched_profile = getattr(self, "launched_profile", None)
        if launched_profile is not None and launched_profile != active_profile_name(self.settings):
            return False
        # Neither may a backend whose *window* is wrong. Voice mode deliberately reloads the
        # same weights with a smaller window, so returning to text has to bring the window
        # back botherwise the chat side silently inherits the microphone's budget.
        launched_context = getattr(self, "launched_context", None)
        if launched_context and int(launched_context) != expected_context(self.settings):
            return False
        return list(launched) == list(self.unified_command())

    async def stop_backend(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(self.process.wait), 15)
            except asyncio.TimeoutError:
                self.process.kill()
                await asyncio.to_thread(self.process.wait)
        self.process = None
        self.launched_command = None
        for log_file in self.log_files:
            log_file.close()
        self.log_files.clear()
        (ROOT / "backend.pid").unlink(missing_ok=True)
        (ROOT / "backend.process.json").unlink(missing_ok=True)

    async def ensure_comfy_idle(self):
        try:
            response = await self.client.get("http://127.0.0.1:8188/queue", timeout=3)
        except httpx.ConnectError:
            return
        response.raise_for_status()
        queue = response.json()
        if queue.get("queue_running") or queue.get("queue_pending"):
            # A queued prompt can outlive the process that submitted it, and such a
            # leftover would block every mode switch. But an entry we did not submit is
            # indistinguishable from work another client is running right now -- an
            # H3 render in ComfyUI's own UI, a script driving /prompt -- so the clear
            # is behind manager.settings["comfy_clear_stale_on_switch"] and stays off
            # unless the user asks for it. With the setting off, a busy queue simply
            # raises ComfyBusyError and the switch waits, which is the safe outcome.
            if self.clear_stale_comfy and self.image_job_is_idle():
                # The GPU decides whether a foreign "running" entry is dead work or
                # someone else's live render; only the former is ever interrupted.
                guard = getattr(image_jobs, "queue_guard", None)
                if guard is not None:
                    guard.observe(await image_jobs.gpu.sample())
                    # A running video job is legitimate work too: during its model-load
                    # phase the GPU looks idle, which would otherwise read as leftover.
                    outcome = await clear_dead_comfy_work(
                        self.client, guard,
                        own_prompt_ids=(video_routes.active_prompt_id(),),
                        supervisor=None if self.comfy_restart_attempted else getattr(image_jobs, "supervisor", None))
                    if outcome["reason"]:
                        LOGGER.warning("ComfyUI queue check: %s", outcome["reason"])
                    if outcome["restarted"]:
                        self.comfy_restart_attempted = True
                with suppress(httpx.HTTPError, OSError):
                    response = await self.client.get("http://127.0.0.1:8188/queue", timeout=3)
                    queue = response.json()
            if queue.get("queue_running") or queue.get("queue_pending"):
                raise ComfyBusyError("ComfyUI 正在执行或排队，将在任务结束后自动加载Qwen、")
        response = await self.client.post(
            "http://127.0.0.1:8188/free",
            json={"unload_models": True, "free_memory": True}, timeout=15,
        )
        response.raise_for_status()

    def image_job_is_idle(self) -> bool:
        """True when no image job of this gateway owns the ComfyUI queue."""
        return image_jobs is None or not image_jobs.busy

    async def load(self, mode):
        started = time.monotonic()
        # 换权重期间回答不了探活：先挂一个带截止时间的标记，看门狗就不会、故意换档"当成卡死
        # （真机踩到：离开语音模式时被监督进程杀在换档中间，请求收到 500）。
        mark_switching()
        previous_backend = self.process
        replaced_backend = False
        # Phase timings, logged with the result: a mode switch is the slowest thing the
        # owner waits for, and "23.6 s" alone says nothing about where it went.
        marks: list[tuple[str, float]] = []

        def mark(label):
            marks.append((label, round(time.monotonic() - started, 2)))
        try:
            # Release ComfyUI models before loading any Qwen mode (or entering
            # image mode).  This is the single OOM guard for all transitions.
            mark("load_entered")
            # Unified backend: one process carries the projector and the MTP draft head, so
            # switching modes only changes which slice the gateway reports. Auditioned
            # before this existed: modalities.vision=true together with
            # speculative.types=none,draft-mtp on a single server.
            unified = bool(self.settings.get("unified_backend", True))
            if unified and mode in ("text", "vision") and self.backend_matches_unified():
                self.mode = mode
                # No restart happens here, so nothing re-probes the backend: MTP must be
                # derived from the profile that is *resident*, not from the switch. The
                # global flag was wrong exactly when voice mode had lent its 9B to the
                # chat side uthe panel then reported MTP on for a profile that has no
                # draft head, contradicting the context size right next to it.
                self.mtp_enabled = bool(self.settings["mtp_enabled"]) and mtp_capable(
                    {**self.settings, "model_profile": self.launched_profile}
                    if self.launched_profile else self.settings)
                self.sleeping = False
                self.revision += 1
                self.last_switch = source_switch(mode, launched=self.launched_profile,
                                                 settings=self.settings)
                self.last_switch_seconds = round(time.monotonic() - started, 2)
                self.state = "ready"
                self.error = None
                self.retry_at = 0
                self.recovery_attempts = 0
                mark("slice_switched_no_restart")
                LOGGER.info("Mode slice switched without a restart: mode=%s seconds=%s phases=%s",
                            mode, self.last_switch_seconds,
                            " ".join(f"{label}@{at}" for label, at in marks))
                return
            try:
                await self.ensure_comfy_idle()
                mark("comfy_idle")
            except ComfyBusyError:
                # The card may look busy only because our own language model still
                # holds it, which the utilisation verdict cannot tell apart from a
                # live render. Release the backend and judge again: a live render
                # keeps the card busy and still blocks, a dead entry is cleared.
                if self.process is None:
                    raise
                await self.stop_backend()
                replaced_backend = True
                mark("comfy_busy_stopped_backend")
                await self.ensure_comfy_idle()
                mark("comfy_idle_retry")
            await self.stop_backend()
            replaced_backend = True
            mark("previous_stopped")
            self.mode = mode
            self.mtp_enabled = False
            self.sleeping = False
            self.health_failures = 0
            # A language model is taking the card back, so it is no longer voice mode's
            # to hold: leaving the flag set would make the watchdog refuse to bring the
            # model back and leave the owner looking at a permanently waiting UI.
            if mode in ("text", "vision"):
                self.voice_holds_card = False
            mark("comfy_idle+previous_stopped")
            if mode in ("image", "video"):
                self.mtp_enabled = False
                self.sleeping = False
                self.revision += 1
                self.last_switch_seconds = round(time.monotonic() - started, 2)
                self.state = "ready"
                self.error = None
                self.retry_at = 0
                self.recovery_attempts = 0
                return
            logs = ROOT / "logs"
            logs.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            self.log_files = [
                (logs / f"{stamp}-{mode}.{suffix}.log").open("wb")
                for suffix in ("out", "err")
            ]
            mark("log_files_open")
            # Kept as the authority on what this child was started with: a Popen has no
            # cmdline(), so the unified check compares against this list instead.
            self.launched_command = (
                self.unified_command() if unified else server_command(mode, settings=self.settings)
            )
            # ...and *which* profile it was started for. The command line alone does not
            # always tell them apart, so this is what stops a model change from being
            # mistaken for a slice switch.
            self.launched_profile = active_profile_name(self.settings)
            # The window this process was actually launched with. Recorded rather than
            # recomputed later, because a mode may deliberately run a different one on the same
            # weights (voice does) and every status field must describe the process, not the file.
            self.launched_context = expected_context(self.settings)
            self.process = subprocess.Popen(
                self.launched_command,
                cwd=ROOT,
                stdout=self.log_files[0], stderr=self.log_files[1],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            mark("popen_returned")
            (ROOT / "backend.pid").write_text(str(self.process.pid), encoding="ascii")
            mark("process_spawned")
            # Record the model of the profile that was actually launched: this record
            # is what adoption and orphan cleanup compare against on the next start.
            launched_model, _ = active_paths(self.settings)
            write_process_record(ROOT / "backend.process.json", {
                "pid": self.process.pid,
                "created_unix_ms": int(psutil.Process(self.process.pid).create_time() * 1000),
                "model": str(launched_model),
                "port": 8082,
            })
            deadline = time.monotonic() + 180
            last_probe_error = None
            while time.monotonic() < deadline:
                exit_code = self.process.poll()
                if exit_code is not None:
                    raise RuntimeError(f"模型在 {mode} 模式初始化时退出（代码 {exit_code}），请查看 logs/{stamp}-{mode}.err.log。")
                try:
                    response = await self.client.get(f"{UPSTREAM}/health", timeout=2)
                    if response.status_code == 200 and response.json().get("status") == "ok":
                        mark("health_ok")
                        props = (await self.client.get(f"{UPSTREAM}/props", timeout=5)).json()
                        mark("props_ok")
                        if props["default_generation_settings"]["n_ctx"] != expected_context(self.settings):
                            raise RuntimeError("Backend context size differs from configured context size.")
                        # b10819 /props omits the slot's active speculative configuration.
                        probe = await self.client.post(
                            f"{UPSTREAM}/completion",
                            json={"prompt": "Hello", "n_predict": 2, "cache_prompt": False}, timeout=60,
                        )
                        probe.raise_for_status()
                        mark("completion_probe")
                        spec = probe.json()["generation_settings"].get("speculative.types", "none")
                        self.mtp_enabled = "draft-mtp" in spec
                        # A profile that ships no draft head can only ever report False,
                        # so there is nothing to validate for it: demanding True here was
                        # what made the 9B fail to load with "MTP status differs from
                        # validated configuration" on every retry.
                        if mtp_capable(self.settings):
                            if self.mtp_enabled != expected_mtp(self.settings, mode, unified=unified):
                                raise RuntimeError("Backend MTP status differs from validated configuration.")
                        if bool(props.get("modalities", {}).get("vision")) != (mode == "vision") and not unified:
                            raise RuntimeError("Backend vision capability differs from selected mode.")
                        await self.cache_metadata(props)
                        self.revision += 1
                        # The backend that answers is this one, launched for this profile,
                        # so the explanation is derived from it rather than from what was
                        # requested ithose differ exactly when the swap did not happen.
                        self.last_switch = source_switch(
                            mode, launched=self.launched_profile,
                            current_mode=self.mode, settings=self.settings)
                        self.last_switch_seconds = round(time.monotonic() - started, 2)
                        self.state = "ready"
                        self.error = None
                        self.retry_at = 0
                        self.recovery_attempts = 0
                        self.next_health_check = time.monotonic() + 10
                        LOGGER.info("Backend ready: mode=%s pid=%s seconds=%s phases=%s",
                                    mode, self.process.pid, self.last_switch_seconds,
                                    " ".join(f"{label}@{at}" for label, at in marks))
                        return
                except httpx.TransportError as exc:
                    # Windows executable/CUDA initialization can outlive a
                    # single connect probe. Keep the owned process alive until
                    # the overall deadline instead of restarting it every 2 s.
                    last_probe_error = type(exc).__name__ + (": " + str(exc) if str(exc) else "")
                await asyncio.sleep(0.5)
            detail = (f" 最近连接状态：{last_probe_error}" if last_probe_error else "")
            raise RuntimeError(f"模型加载超过 180 秒。{detail}请查看logs/{stamp}-{mode}.err.log。")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.exception("Backend load failed for %s", mode)
            self.error = str(exc).strip() or f"模型初始化失败（{type(exc).__name__}），服务将自动重试。"
            if not replaced_backend and previous_backend and previous_backend.poll() is None:
                self.state = "ready"
            else:
                await self.stop_backend()
                self.mode = mode
                if isinstance(exc, ComfyBusyError):
                    self.state = "waiting"
                    self.retry_at = time.monotonic() + 5
                else:
                    self.state = "error"
                    self.recovery_attempts += 1
                    self.retry_at = time.monotonic() + min(60, 2 ** min(self.recovery_attempts, 6))
        finally:
            # 换档结束（成功、失败、被取消都算）：标记清掉，看门狗恢复计数。
            clear_switching()


manager = ModeManager()
# Published at module scope so the route handlers can ask uvicorn to stop, and so a
# process that never started uvicorn (a test) can still reach the stop path.
server = None
# Device sessions for the shared address. Kept at module scope because the gate,
# the login route and the manager panel all read it, and it is the only place a
# presented cookie is turned into an identity. A test process deliberately starts
# with an empty store, so running the suite cannot begin enforcing the live
# passphrase stored on this machine.
share_store = ShareStore(ROOT / "logs", load_state=not tests_are_running())
image_jobs = None
usage_store = None
context_compactor = ContextCompactor(UPSTREAM, lambda: usage_store,
                                     label_getter=lambda: serving_model_label())
# One history and one context meter for typing, pictures and speech alike. Keyed by the
# conversation identifier the chat app already sends, it is what lets a spoken turn be
# visible in the written thread and counted against the same window (see
# conversation_ledger). The window follows the *resident* profile, so a borrowed 9B reports
# its own 32 K rather than the file's selection.
ledger = ConversationLedger(
    ROOT / "logs" / "conversation-history.json",
    context_size=expected_context(manager.settings),
    reserve_tokens=512,
)


def serving_model_label() -> str:
    """正在服务的那份权重的名字，用于上下文压缩这类记账行的模型名。

    真机踩到：压缩行原来写死兜底 `qwen3.8-27b-local`，于是**9B 时压缩记录显示27B**，
    看起来像"、9B 时去调27B"。实际推理一直由常驻后端承担，只是标签错了。这里按
    `launched_profile`（借用的）或当前选择的profile 取名，两种模型都如实。
    """
    resident = getattr(manager, "launched_profile", None)
    if resident and resident in PROFILES:
        stem = PROFILES[resident].get("model")
        if stem is not None:
            return getattr(stem, "stem", None) or str(stem)
    try:
        return active_paths(manager.settings)[0].stem
    except Exception:      # noqa: BLE001 - 取名失败不该影响压缩
        return "本机模型"


def _loop_guard_action(verdict) -> str:
    """这一级到底做了什么，写进网关日志（响应头里只放 ASCII 的原因码与级别）。"""
    level = int(verdict.get("level") or 1)
    if verdict.get("reason") == "step-budget":
        return "摘掉工具，要求它总结进展并给出下一步可行方案"
    if level >= loop_guard.LEVEL_LIMIT:
        return "摘掉全部工具，要求说明根因与下一步具体方案"
    if level == 2:
        return f"摘掉工具 {verdict.get('tool')}，要求换工具或换目标继续"
    return "工具照给，要求它说清原因、换一条可行方案并继续执行"


def ledger_window():
    """The context window of whichever backend is answering right now.
    Read from the backend that was actually launched, not recomputed from the settings: voice
    mode runs a smaller window on the same weights, and the panel must describe the window the
    model in front of the owner really has. Recomputing is how the status ended up advertising
    the text window while the process was serving 40960.
    """
    launched = getattr(manager, "launched_context", None)
    if launched:
        return int(launched)
    resident = getattr(manager, "launched_profile", None)
    settings = manager.settings
    if manager.mode == "voice":
        return voice_context({**settings, "model_profile": resident or active_profile_name(settings)})
    if resident and resident != active_profile_name(settings):
        return PROFILES[resident].get("context_size") or expected_context(settings)
    return expected_context(settings)


MODE_SCOPES = ("text", "vision", "voice")


def mode_scope(explicit: str = "") -> str:
    """Which mode's thread a conversation belongs to.

    The gateway keeps **one history per mode**. It used to keep one per chat-app conversation
    across every mode, so a written thread arrived in 识图 as if it belonged there: the model
    still had the typed context while looking at a picture, and the meter counted turns the
    vision view never produced. The owner's rule is the opposite — switching modes must not carry
    data across — and composing the key from the mode is what enforces it in one place, because
    every read and every write goes through the two helpers below.
    """
    value = str(explicit or "").strip().lower()
    if value in MODE_SCOPES:
        return value
    return manager.mode if manager.mode in MODE_SCOPES else "text"


def scoped_conversation(value: str, scope: str = "") -> str:
    """The ledger key for one conversation *within one mode*: ``vision·abc123``.

    An identifier that already carries a scope prefix is left alone, so a panel can hold on to an
    id it read from a listing and hand it straight back to delete or rename it.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    prefix, separator, rest = text.partition("·")
    if separator and prefix in MODE_SCOPES and rest.strip():
        return text
    return f"{mode_scope(scope)}·{text}"


def scope_filter(scope: str = "") -> str:
    """The prefix a listing must start with to belong to this mode."""
    return f"{mode_scope(scope)}·"


# The namespace a shared device's threads are filed under, inside the id half of the key.
GUEST_NAMESPACE_MARK = "guest-"


def guest_namespace(request) -> str:
    """The ledger namespace one caller owns: empty for the owner, ``guest-<session24>·`` for a
    shared device.

    The history used to be one namespace with the mode in front of it, and the only thing keeping
    a visitor out of the owner's threads was that the listing route "is only called by our own
    panels"  which is not a guard. Measured live over the LAN with nothing but the passphrase: the
    guest listing came back with the owner's thread id and title, and handing that id straight back
    returned the full text of the private conversation. Uploads and generated files already get a
    namespace per shared device; the history now gets the same treatment, derived from the session
    the caller is actually holding.
    """
    if request is None or caller_is_owner(request):
        return ""
    token = caller_token(request)
    if not token:
        # Not the owner and no session: nothing personal to claim, and an empty namespace still
        # cannot name a key that has content in it.
        return f"{GUEST_NAMESPACE_MARK}anonymous·"
    return f"{GUEST_NAMESPACE_MARK}{token[:24]}·"


def tenant_conversation(request, value: str = "", scope: str = "") -> str:
    """The ledger key *this caller* may use for a conversation.

    The mode decides the first segment (see scoped_conversation) and a visitor's namespace goes
    inside the id, so the two rules compose and every existing prefix filter keeps working:
    ``voice·guest-<session>·abc``.

    A key that already carries the caller's own namespace comes back unchanged —a row read out of
    a listing has to be usable as it is (talk in it, rename it, delete it). An id that carries
    *someone else's* namespace is not stripped: it is nested inside this caller's own, so it names
    a key that does not exist instead of the other tenant's thread.
    """
    key = scoped_conversation(value, scope)
    if not key:
        return ""
    namespace = guest_namespace(request)
    if not namespace:
        return key
    mode, _, rest = key.partition("·")
    if rest.startswith(namespace):
        return key
    return f"{mode}·{namespace}{rest}"


def tenant_filter(request, scope: str = "") -> str:
    """The prefix this caller's listing must start with: their mode, inside their namespace."""
    return f"{scope_filter(scope)}{guest_namespace(request)}"


def is_guest_key(key: str) -> bool:
    """Whether a ledger key belongs to a shared device rather than to the owner."""
    _, _, rest = str(key or "").partition("·")
    return rest.startswith(GUEST_NAMESPACE_MARK)


def caller_rows(request, rows):
    """A listing with the tenants the caller is not part of removed.

    A visitor's rows are already fenced off by :func:`tenant_filter`; this is the other direction.
    The owner's rail is the owner's own history, and another device's question turning up in it is
    the same complaint the per-mode split already answered —something that is not mine showing up
    in my list. A visitor never sees the owner's rows for the same reason.
    """
    if guest_namespace(request):
        return list(rows)
    return [row for row in rows if not is_guest_key(row.get("id"))]


# Questions this proxy has forwarded per scoped thread, newest last. See scope_user_texts: the
# ledger lags a response, this does not.
forwarded_user_texts: dict[str, list[str]] = {}
FORWARDED_TEXTS_LIMIT = 40


def scope_user_texts(key: str) -> set:
    """The questions this mode's thread knows, for the prompt filter.

    Two sources, and both are needed: the ledger (what has been recorded) and the questions this
    proxy has *just* forwarded. The ledger is written by the accounting middleware after a
    response completes, so a question asked a second ago may not be in it yet —measured live: the
    next turn then treated the previous one as "another mode's" and dropped it, and the model
    answered that it had no such context. Remembering the question as it is forwarded closes that
    window without guessing.
    """
    if not key:
        return set()
    known = set(forwarded_user_texts.get(key) or ())
    turns = ledger.history(key, limit=80, max_chars=8000)
    known.update(turn["content"] for turn in turns
                 if turn.get("role") == "user" and turn.get("content"))
    return known


def remember_user_text(key: str, payload) -> None:
    """Note the question this request carries, before it is forwarded to the model."""
    if not key or not isinstance(payload, dict):
        return
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = mode_isolation.message_text(message)
        if not text:
            return
        seen = forwarded_user_texts.setdefault(key, [])
        if text not in seen:
            seen.append(text)
            del seen[:-FORWARDED_TEXTS_LIMIT]
        return


def conversation_key(request, scope: str = ""):
    """Which conversation this request belongs to, from the header the chat app sends.

    Scoped by the mode being served, which is what keeps a typed turn out of the vision view
    (and a picture out of the typed thread), and —for a shared device —scoped again by the
    caller's own namespace, so the same header cannot name the owner's thread (tenant_conversation).
    """
    value = request.headers.get("x-conversation-id") or ""
    if not value.strip():
        return ""
    return ledger.normalize_id(tenant_conversation(request, value, scope))
# Native UI can resume a stream after returning to the tab. Its upstream stream
# does not exist until compaction finishes and the real generation is accepted.
pending_context_streams = {}


def initialize_usage_store():
    store = UsageStore(ROOT / "logs" / "usage.sqlite3")
    store.recover_interrupted()
    return store


@asynccontextmanager
async def lifespan(app):
    global usage_store, image_jobs
    if os.environ.get("QWEN_BIND_CHECK") == "1":
        # A bind probe verifies listener and gate behaviour over TCP; it must not
        # claim the live usage database or start a backend beside a running one.
        yield
        return
    try:
        # A configured passphrase seeds the store here rather than being read per
        # request, so a rotation made in the manager panel survives a restart.
        if seed_passphrase():
            LOGGER.info("Sharing: passphrase adopted from configuration")
    except Exception:
        LOGGER.exception("Sharing configuration could not be seeded; sharing stays off")
    if access_token():
        # Sharing is on, so the public address is part of starting up. The tunnel is
        # brought up in the background: waiting for cloudflared here would delay the
        # gateway, and the panel can report progress while it connects.
        manager.tunnel_task = asyncio.create_task(bring_tunnel_up())
    try:
        usage_store = await asyncio.to_thread(initialize_usage_store)
    except Exception:
        LOGGER.exception("Usage logging unavailable; model service will continue")
        usage_store = None
    manager.client = httpx.AsyncClient(
        timeout=httpx.Timeout(600, connect=5), trust_env=False,
        limits=httpx.Limits(max_connections=30, max_keepalive_connections=10),
    )
    # Published at module scope: the video routes reach the ComfyUI supervisor
    # through image_jobs.supervisor, and without this they find nothing to start
    # ComfyUI with and fail any run that begins while the server is down.
    global image_jobs
    # One prompt optimiser for both ComfyUI queues. It follows the *selected* profile —the same
    # `manager.load` path the mode buttons use —instead of pinning the prompt work to one model,
    # which is what made "生图的提示优、silently fall back to the raw prompt whenever the owner
    # was not already sitting in a text mode.
    prompt_enhancer = prompt_enhance.PromptEnhancer(
        manager.client, lambda mode: manager.load(mode), lambda: manager.settings,
        comfy_base=COMFY)
    image_jobs = ImageJobMonitor(manager.client, manager.load, enhancer=prompt_enhancer)
    # The video queue gets the same optimiser, plus the way to give the card back to ComfyUI once
    # the description has been rewritten (the language model is only there for the prompt work).
    video_routes.set_prompt_enhancer(prompt_enhancer, release=lambda: manager.load("video"))
    # The video routes cannot reach this module's `image_jobs` name (the monitor shadows the
    # module of the same name), and the supervisor that starts ComfyUI on demand lives on the
    # monitor: bound here so a video run that begins while ComfyUI is down starts it instead of
    # failing with "ComfyUI 未运行，且没有可用的启动器、.
    video_routes.set_supervisor_provider(lambda: image_jobs.supervisor)
    image_jobs.supervisor.autostart = bool(manager.settings.get("comfy_autostart", True))
    image_jobs.supervisor.start_timeout = float(manager.settings.get("comfy_start_timeout", 300))
    manager.clear_stale_comfy = bool(manager.settings.get("comfy_clear_stale_on_switch", True))
    if image_jobs is not None:
        image_jobs.clear_stale_comfy = manager.clear_stale_comfy
    # The video mode shares this ComfyUI but keeps its own job state, so a long H3
    # run never looks like an image job. It is registered at import time; passing
    # the client again here would only overwrite the lazy provider.
    manager.task = asyncio.create_task(manager.initialize())
    manager.watchdog_task = asyncio.create_task(manager.watchdog())
    # 页面关闭后的收工倒计时：平时什么都不做，只有收到页面信标才醒（、page_lifecycle.py）。
    manager.lifecycle_task = asyncio.create_task(page_watch.run())
    # The speech model is deliberately *not* warmed here any more.
    #
    # It used to be, reasoning that the first mic click should not be the one that pays for it.
    # Measured, that bought less than it cost: 60.2 s and about 1.1 GiB on every gateway start -
    # typed sessions included, which can never reach a recogniser - and it ran concurrently with the
    # model load, which is why a backend that comes up in ~6 s on an idle card was measured taking
    # 89.9 s. It is now warmed when someone actually enters the voice view, and only after the
    # synthesiser holds the card: see _warm_recogniser_late.
    try:
        yield
    finally:
        manager.state = "stopping"
        if image_jobs.busy:
            image_jobs.task.cancel()
            with suppress(asyncio.CancelledError):
                await image_jobs.task
        if manager.watchdog_task:
            manager.watchdog_task.cancel()
            try:
                await manager.watchdog_task
            except asyncio.CancelledError:
                pass
        if manager.task and not manager.task.done():
            manager.task.cancel()
            try:
                await manager.task
            except asyncio.CancelledError:
                pass
        if getattr(manager, "tunnel_task", None) and not manager.tunnel_task.done():
            manager.tunnel_task.cancel()
            with suppress(asyncio.CancelledError):
                await manager.tunnel_task
        # Everything this gateway put on the card goes back before the process exits: the
        # recogniser, the synthesis worker and voice mode's own copy of the 9B if it had
        # one. Leaving them to be cleaned up by whoever notices is how a stopped service
        # keeps several gigabytes that another application wanted.
        try:
            await voice_mode.VOICE.stop()
        except Exception:
            LOGGER.exception("Voice mode did not release its backends cleanly")
        with suppress(Exception):
            await _stop_tts()
        await manager.stop_backend()
        await manager.client.aclose()
        # The recognition worker is a separate process holding its own copy of the model:
        # it does not die with this one, so it is terminated here rather than leaked. The warm-up
        # task that used to be tracked here no longer exists at start-up; if the voice view started
        # one, this is still what reclaims its worker.
        voice_input.shutdown_worker()
        (ROOT / "manager.pid").unlink(missing_ok=True)
        (ROOT / "manager.process.json").unlink(missing_ok=True)


# Whether this gateway has already paid for the speech model in this process lifetime. Set only on
# success, so a failed load is retried the next time someone enters the voice view.
_recogniser_warmed = False


async def _warm_recogniser_late(synthesiser):
    """Load the speech model, but only once the synthesiser already holds the card.

    The order is the whole point on a 12 GiB card. Both models do coexist - measured 11,770 MiB used
    with all three up - but only if the synthesiser is built first: built while the recogniser is
    already resident, it finds less than its reserve free and falls back to the processor, and a
    reply synthesised on the processor is both slow and, when it fails outright, read out by the
    browser instead of spoken.

    Called from the voice-mode entry route, never at gateway start-up. Waiting on the synthesiser's
    own warm-up task is what makes "the engine exists" mean the engine, not just its process.
    """
    global _recogniser_warmed
    if _recogniser_warmed:
        return
    if synthesiser is not None:
        # Swallows a failed synthesiser on purpose: the recogniser is still worth having, and the
        # first spoken turn will start the engine again if it needs to.
        with suppress(Exception):
            await synthesiser
    if _recogniser_warmed:
        return
    try:
        await voice_input.warmup()
    except Exception:
        LOGGER.exception("voice: recogniser warm-up failed")
    else:
        _recogniser_warmed = True


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


class OwnerRouteDependency:
    """FastAPI dependency form of the owner-only check.

    A router that declares its own routes cannot call a helper at the top of each
    handler, so the same rule is exposed here for `Depends` and applied to every
    video route when the router is installed.
    """

    def __call__(self, request: Request):
        require_owner(request)


def owner_identity(request):
    """The owner's own label in the upload records.

    The owner's console may hold no session cookie at all (a loopback visit needs
    none), so its own uploads are filed under a fixed name rather than an arbitrary
    session token. A shared visitor always has a real cookie by the time it reaches
    the video routes, so this never invents one for a guest.
    """
    return str(request.cookies.get(ACCESS_COOKIE) or "") or guest_uploads.OWNER_SESSION


def request_identity(request):
    """Who is calling: the machine's owner, or one shared device.

    Passed to the video router, which needs this for two things a bare owner check
    cannot express: which session a clip upload belongs to, and which uploads a caller
    is allowed to list. Both proofs of ownership count, exactly as in
    `is_admin_session` —a tunnel visitor holds neither.
    """
    return is_admin_session(request), owner_identity(request)


def active_model_name():
    """What to file a request under when the caller did not name a model.

    Resolved from the selected profile on every request, so the usage table follows a
    model switch instead of reporting the profile the gateway happened to start with.
    The alias is preferred over the profile key because it is what llama-server itself
    reports, which makes these rows comparable with anything else on this machine.
    """
    try:
        alias = active_paths(manager.settings)[0].stem
    except (OSError, ValueError, KeyError, IndexError, AttributeError, TypeError):
        # A selection that cannot be resolved must not turn into a failed request; the
        # accounting layer records the miss rather than inventing a model name.
        LOGGER.exception("Could not resolve the active model for usage accounting")
        return None
    return alias or None


async def observe_generation(info):
    """File a written turn into the shared conversation history.

    Called by the accounting middleware for every generation request, so the transcript and
    the context meter cover what was typed exactly as they cover what was spoken. Streamed
    replies are not captured (their body is forwarded chunk by chunk), which costs the meter
    nothing: the next turn's measured prompt already includes that answer.
    """
    payload = info.get("payload") if isinstance(info.get("payload"), dict) else None
    key = str(info.get("conversation_id") or "").strip()
    if payload is None or not key:
        return
    ledger.observe_request(
        # One thread per mode: a typed turn is filed under ``text·…`` and can never be replayed
        # inside the vision thread.
        ledger.normalize_id(scoped_conversation(key, info.get("mode"))),
        payload, info.get("path"), mode=info.get("mode") or "text",
        reply=info.get("reply"), prompt_tokens=info.get("prompt_tokens"),
        window=ledger_window(),
    )


app.add_middleware(UsageMiddleware, get_store=lambda: usage_store, get_mode=lambda: manager.mode,
                   get_model=active_model_name, observe=observe_generation)


# Host validation lives in AccessGateMiddleware, which is installed outermost, so
# a rejected name never reaches the routes or the usage store. This middleware is
# kept as a second, central check and accepts any name the gate already approved.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])
# Starlette walks its route table when the application starts, so every route has
# to be attached here rather than from inside the lifespan, where it would be
# added too late to be matched. The handlers read the live monitor instead.
video_routes.install(app, lambda: manager.client, owner_dependency=OwnerRouteDependency(),
                     identity_resolver=request_identity, policy_store=guest_uploads.store)

# One GPU, one decider. The owner is never refused, a visitor is never admitted onto a
# card that has no room, and the owner can take the card from a visitor mid-generation.
gpu = gpu_arbiter.GpuArbiter()


class AccessGateMiddleware:
    """Reject unknown host names and unauthenticated callers, outermost.

    Pure ASGI rather than BaseHTTPMiddleware so the wrapping is transparent to
    every route, including the ones that stream responses. A request that gets
    through is tagged with its session so the routes can attribute activity.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        if not host_is_allowed(request):
            await write_plain_response(
                send, 400, "This address is not shared by this Qwen service.\n",
            )(scope, receive, send)
            return
        if request.url.path == "/__login":
            await self.app(scope, receive, send)
            return
        # The owner's own browser marks itself with ?admin=<token> once. Exchanging
        # it for a cookie is loopback-only, so a guest who somehow learned the URL
        # still cannot use it from the tunnel.
        presented_admin = request.query_params.get("admin", "")
        if presented_admin:
            try:
                require_local_session(request)
            except HTTPException:
                await write_plain_response(
                    send, 403, "管理员口令只能在本机使用。\n",
                )(scope, receive, send)
                return
            # Loopback already proved this is the owner's machine, so the visit also
            # gets a session: the console opens without a second passphrase prompt.
            # The session is a fixed function of the admin token rather than a new
            # one per visit, so a browser that arrives without cookies (a fresh
            # profile, a script, a reload loop) cannot fill the store with empty
            # sessions. Resetting the admin token is what rotates it.
            existing = request.cookies.get(ACCESS_COOKIE) or ""
            if share_store.authenticate(existing) is None:
                existing = share_store.ensure_admin_session(_admin_session_token())
            response = Response(status_code=303, headers={"Location": "/", "Cache-Control": "no-store"})
            response.set_cookie(
                ACCESS_COOKIE, existing, max_age=180 * 24 * 3600,
                httponly=True, samesite="lax", path="/",
            )
            response.set_cookie(
                ACCESS_ADMIN_COOKIE, share_store.admin_token, max_age=400 * 24 * 3600,
                httponly=True, samesite="lax", path="/",
            )
            await response(scope, receive, send)
            return
        presented = request.cookies.get(ACCESS_COOKIE) or request.query_params.get("k") or ""
        # The watchdog probes with GATE_PARAM and the admin token (it cannot hold a
        # cookie across restarts, and the passphrase may have been rotated). Without
        # this branch its probe is answered with the login page, which it reads as a
        # dead gateway and answers by restarting a perfectly healthy one —forever.
        # The request is mapped onto the owner's own stable session rather than given a
        # fresh one, so a watchdog that probes every five seconds cannot fill the store.
        gate_credential = request.query_params.get(GATE_PARAM) or ""
        if gate_credential and admin_token_matches(gate_credential):
            # The owner's own session is created here if it does not exist yet, so a
            # watchdog can probe a gateway that has just started, before any browser
            # has visited the console.
            share_store.ensure_admin_session(_admin_session_token(), source=client_source(request))
            presented = presented or _admin_session_token()
        session = share_store.authenticate(presented)
        # The passphrase gate only applies while an owner has configured one; a
        # loopback-only deployment serves its own machine without a login.
        if access_token() and session is None:
            # login_page() carries its own no-store header, which matters for the
            # tunnel route where a cached 401 would keep a visitor locked out.
            await login_page()(scope, receive, send)
            return
        if session is not None:
            scope["qwen.session_token"] = presented
            share_store.mark_seen(presented, client_source(request))
        await self.app(scope, receive, send)


class SessionAccountingMiddleware:
    """Count one served request and the bytes returned, per session.

    The request is counted when its response starts, so a long generation shows up
    in the manager panel while it is still running, and the bytes follow in a
    single addend once the whole body (streamed or not) has been sent.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        token = scope.get("qwen.session_token")
        if scope["type"] != "http" or not token:
            await self.app(scope, receive, send)
            return
        body_bytes = 0
        counted = False

        async def counting_send(message):
            nonlocal body_bytes, counted
            if message["type"] == "http.response.body":
                body_bytes += len(message.get("body", b""))
            elif message["type"] == "http.response.start" and not counted:
                counted = True
                share_store.note_request(token, scope.get("path", ""))
            await send(message)

        try:
            await self.app(scope, receive, counting_send)
        except BaseException:
            share_store.note_request(token, scope.get("path", ""))
            raise
        finally:
            if body_bytes:
                share_store.note_request(token, "", received=body_bytes)


# Installed first of the two so session accounting runs inside the gate, which is
# what puts the session token on the scope before any request is counted.
app.add_middleware(SessionAccountingMiddleware)
# Installed last so Starlette, which wraps in reverse, makes the gate the outermost
# layer: neither an unknown Host header nor an unauthenticated request reaches the
# usage tracker, the proxy, or the streaming routes below it.
app.add_middleware(AccessGateMiddleware)


def require_local_origin(request):
    """Reject cross-site state changes while still serving a shared address.

    The name cannot be assumed to be loopback once the UI is shared, so the
    check follows the request's own host and the configured allowlists. Browser
    history never caches a request rejected here; this is CSRF defence, and the
    access token at the gate is the authentication boundary.
    """
    if not origin_is_trusted(request.headers.get("origin"), request.headers.get("host", "")):
        raise HTTPException(403, "Local origin required")


def client_source(request):
    """Where a request came from, without trusting a spoofable forwarded host.

    Behind the tunnel every request arrives from the local tunnel process, so the
    Host header is what separates "opened the public address" from "opened the LAN
    address". The forwarded client address is only used when the proxy supplies
    one, and it is truncated to keep the panel readable.
    """
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for") or ""
    host, _ = request_host(request)
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (host or "unknown")[:64]


def require_local_session(request):
    """Only the machine itself, or a browser that proved the owner's token.

    Shared users reach the same routes as the owner's browser, so the routes that
    change the service (mode, image and video jobs, shutdown) must not be driven
    by a guest. Two independent proofs are accepted, and both are denied to a
    tunnel visitor: the request really arrived over loopback, or it carried the
    owner's admin token, which is only ever printed on this machine.
    """
    if peer_is_loopback(request):
        return True
    presented = request.query_params.get(GATE_PARAM, "")
    if presented and hmac.compare_digest(presented, share_store.admin_token):
        return True
    raise HTTPException(403, "该操作仅限本机管理员")


def peer_is_loopback(request):
    """Whether the request really arrived over the loopback interface.

    The whole 127.0.0.0/8 range counts, not just 127.0.0.1, so the check cannot be
    fooled by a source address that merely looks local.
    """
    client = getattr(request, "client", None)
    address = getattr(client, "host", None)
    if not address:
        return False
    return address == "::1" or address.startswith("127.")


def is_admin_session(request):
    """Whether this request may see the owner-only surface.

    Two independent proofs count, and neither is available to a shared visitor:
    the request really arrived over loopback, or the browser already exchanged the
    admin token from a loopback visit for the admin cookie. The cookie is only ever
    set over loopback, so a guest on the tunnel cannot obtain one.
    """
    if peer_is_loopback(request):
        return True
    if admin_token_matches(request.query_params.get(GATE_PARAM, "")):
        return True
    return bool(share_store.authenticate(request.cookies.get(ACCESS_COOKIE) or "")) and (
        admin_cookie_matches(request)
    )


def admin_token_matches(presented):
    return bool(presented) and hmac.compare_digest(presented, share_store.admin_token)


def admin_cookie_matches(request):
    return admin_token_matches(request.cookies.get(ACCESS_ADMIN_COOKIE) or "")


def admin_link():
    """The convenience URL that marks this browser as the owner's."""
    return f"http://127.0.0.1:{listening_port()}/?{OWNER_PARAM}={share_store.admin_token}"


def _admin_session_token():
    """A stable session id for the owner's own console.

    Derived from the admin token so the same browser keeps the same session across
    visits without a cookie, and so resetting the admin token retires it. It grants
    no extra rights: the owner tier is decided by the peer address or the admin
    cookie, never by this token.
    """
    return "owner-" + hashlib.sha256(
        f"owner-session:{share_store.admin_token}".encode("utf-8")).hexdigest()[:24]


def require_owner(request):
    """Owner-only routes: the passphrase is not enough, the machine must prove it.

    Sharing hands a guest the same origin the owner uses, so every route that
    starts or stops work, changes generation settings or writes files has to be
    closed separately. This is that closure, applied together with the origin
    check on routes the local UI drives.

    Feature routes a *visitor is meant to use* go through ``require_feature`` instead; what
    stays here is the host's own controls.
    """
    require_local_origin(request)
    require_local_session(request)


def caller_is_owner(request):
    """Whether this request is the machine's owner (loopback, or the owner's token/cookie)."""
    return is_admin_session(request)


def caller_token(request):
    """The session token of a logged-in caller, or "" for one that is not logged in."""
    return str(request.cookies.get(ACCESS_COOKIE) or "")


def require_feature(request, feature):
    """A user-facing feature: the owner always, a shared visitor when the owner opened it.

    This replaces the blanket owner check on the routes a visitor is meant to *use* —talking,
    generating images and films, switching modes (which is what also swaps the model and its
    projector/recogniser plugin), and handling their own uploads and downloads.

    What deliberately stays owner-only is the host's own surface, not the visitor's abilities:
    kicking a device, rotating the passphrase, taking the service or the tunnel down, the usage
    and routing ledgers, preempting the card, and deleting the owner's history or saved
    workflows. A visitor who could rotate the passphrase or stop the service would be holding
    the machine's keys rather than using it.
    """
    require_local_origin(request)
    if caller_is_owner(request):
        return
    token = caller_token(request)
    if not token or share_store.authenticate(token) is None:
        # Not the owner and not a logged-in shared device: the original refusal applies.
        raise HTTPException(403, "该操作仅限本机管理员")
    if not guest_policy.allows(feature):
        raise HTTPException(403, guest_policy.refusal(feature))


def require_any_feature(request, *features):
    """For a route that serves two kinds of file: an upload, or a generated result.

    ``/local/image/file`` and ``/local/video/file`` hand back both, so requiring both switches
    would refuse a caller who has every right to the bytes.
    """
    require_local_origin(request)
    if caller_is_owner(request):
        return
    token = caller_token(request)
    if not token or share_store.authenticate(token) is None:
        raise HTTPException(403, "该操作仅限本机管理员")
    if not guest_policy.allows_any(*features):
        raise HTTPException(403, guest_policy.refusal(features[0]))


@app.get("/local/share/guest-policy")
async def share_guest_policy(request: Request):
    """What shared visitors may do, for the sharing panel."""
    require_local_session(request)
    return JSONResponse(guest_policy.snapshot(), headers={"Cache-Control": "no-store"})


class GuestFeatureRequest(BaseModel):
    feature: str
    allowed: bool


@app.post("/local/share/guest-policy")
async def share_set_guest_policy(body: GuestFeatureRequest, request: Request):
    """Open or close one guest capability. Takes effect on the next request.

    One switch per capability rather than a single "everything on/off": the owner asked for a
    shared session that can use the machine, and the same panel has to be able to take one of
    those abilities back —for example letting guests render while keeping them out of the model
    picker —without closing the whole share.
    """
    require_local_session(request)
    try:
        guest_policy.policy().set_feature(body.feature, body.allowed)
    except ValueError as failure:
        raise HTTPException(400, str(failure))
    return JSONResponse(guest_policy.snapshot(), headers={"Cache-Control": "no-store"})


@app.post("/__login")
async def submit_login(request: Request):
    """Exchange the shared passphrase for this device's own session cookie.

    The cookie carries a per-device token rather than the passphrase, so rotating
    the passphrase later admits new devices without disturbing this one.
    """
    form = await request.form()
    source = client_source(request)
    if not token_matches(str(form.get("token", ""))):
        return login_page("口令不正确，请向管理员确认后重试。")
    target = request.query_params.get("next", "/")
    if not target.startswith("/") or target.startswith("//"):
        target = "/"
    label = device_label(request)
    token = share_store.create(kind="device", label=label, source=source)
    LOGGER.info("Sharing: new device session %s from %s", label, source)
    response = Response(status_code=303, headers={"Location": target, "Cache-Control": "no-store"})
    response.set_cookie(
        ACCESS_COOKIE, token, max_age=180 * 24 * 3600,
        httponly=True, samesite="lax", path="/",
    )
    if peer_is_loopback(request):
        # Logging in from this machine is also how the owner's browser becomes an
        # admin browser, so the manager panel is available without a second step.
        response.set_cookie(
            ACCESS_ADMIN_COOKIE, share_store.admin_token, max_age=400 * 24 * 3600,
            httponly=True, samesite="lax", path="/",
        )
    return response


def device_label(request):
    """A short human label so the owner can tell devices apart in the panel."""
    agent = request.headers.get("user-agent", "")
    lowered = agent.lower()
    platform = "电脑"
    for needle, name in (
        ("iphone", "iPhone"), ("ipad", "iPad"), ("android", "Android"),
        ("macintosh", "Mac"), ("windows", "Windows"), ("linux", "Linux"),
    ):
        if needle in lowered:
            platform = name
            break
    for needle, name in (("edg/", "Edge"), ("chrome/", "Chrome"), ("firefox/", "Firefox"),
                         ("safari/", "Safari")):
        if needle in lowered:
            return f"{platform} · {name}"[:60]
    return platform


@app.post("/__logout")
async def submit_logout(request: Request):
    """Forget this device's session, so the next visit asks for the passphrase."""
    share_store.revoke(request.cookies.get(ACCESS_COOKIE) or "")
    response = Response(status_code=303, headers={"Location": "/", "Cache-Control": "no-store"})
    response.delete_cookie(ACCESS_COOKIE, path="/")
    if admin_cookie_matches(request):
        # Signing out of the owner's own browser also drops its admin standing.
        response.delete_cookie(ACCESS_ADMIN_COOKIE, path="/")
    return response


# ------------------------------------------------------------------ sharing
# Owner-only surface behind /local/share. A guest session reaches these paths
# like any other, so each one starts by proving the caller is the machine itself
# or presented the admin token that is printed only on this host.
SHARE_GUEST_FIELDS = {
    "service", "instance_id", "mode", "state", "error", "revision",
    "active_requests", "last_switch_seconds", "sleeping", "image",
}

TUNNEL_TASK_NAME = "QwenCloudflaredTunnel"
_tunnel_state = {"checked_at": 0.0, "online": False}


def _run_hidden(command, timeout):
    """Run a Windows helper and read its output whatever code page it used.

    schtasks and friends print in the console code page, so decoding as UTF-8
    raises on a Chinese Windows; the status text is only searched, never parsed
    into data, so replacing undecodable bytes is the correct lossy choice. Text
    mode is what makes `errors` mean anything here: without it subprocess rejects
    the call outright and the tunnel would always look offline.
    """
    return subprocess.run(
        command, capture_output=True, text=True, errors="replace", timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def tunnel_process():
    """The running cloudflared process for this gateway, if there is one.

    The tunnel is detected by looking for its process rather than by parsing
    `schtasks` output: that output is localised ("Status" becomes "״̬" on a Chinese
    Windows), so a search for the English word "Running" silently reports a live
    tunnel as offline.
    """
    for process in psutil.process_iter(["name", "cmdline"]):
        try:
            if (process.info["name"] or "").lower() != "cloudflared.exe":
                continue
            command = " ".join(process.info["cmdline"] or [])
        except (psutil.Error, TypeError):
            continue
        if f"127.0.0.1:{listening_port()}" in command or "--url" in command:
            return process
    return None


def tunnel_executable():
    return ROOT / "tools" / "cloudflared.exe"


def tunnel_launcher():
    """Path of the windowless launcher the tunnel task runs.

    A hidden scheduled task still allocates a console for a console application, and
    a launcher that exits immediately takes the tunnel down with the task before it
    even registers. The launcher is therefore a pythonw wrapper that spawns
    cloudflared and then stays in the foreground of the task, holding it open.
    """
    return ROOT / "logs" / f"{TUNNEL_TASK_NAME}.vbs"


def tunnel_keeper():
    return ROOT / "tunnel_keeper.py"


def write_tunnel_launcher():
    """Write the VBS shim that runs the keeper with no window, and return its path."""
    keeper = tunnel_keeper()
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        pythonw = Path(sys.executable)
    if not keeper.is_file():
        raise HTTPException(503, f"找不到隧道守护脚本：{keeper}")
    path = tunnel_launcher()
    lines = [
        "' Runs the tunnel keeper through pythonw so no console exists at all.",
        "' Generated by mode_server.py; edits are overwritten on the next start.",
        'Set shell = CreateObject("WScript.Shell")',
        f'shell.CurrentDirectory = "{ROOT}"',
        f'shell.Run """{pythonw}"" ""{keeper}"" run", 0, True',
    ]
    try:
        # Written as bytes with explicit CRLF: text mode would translate the newlines
        # again and double-space the script.
        path.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
    except OSError as exc:
        raise HTTPException(503, f"无法写入隧道启动器：{exc}") from exc
    return path


def register_tunnel_task():
    """(Re)create the hidden scheduled task that carries the tunnel.

    Registering from here —rather than only from the share script —is what makes
    the panel's "恢复连接" work on a machine where the task was deleted or never
    created. The task runs the windowless VBS launcher, so starting the tunnel never
    flashes a console window.
    """
    launcher = write_tunnel_launcher()
    command = (
        "$a = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument '\"{0}\"' "
        "-WorkingDirectory '{1}'; "
        "$p = New-ScheduledTaskPrincipal -UserId \"$env:USERDOMAIN\\$env:USERNAME\" "
        "-LogonType Interactive -RunLevel Limited; "
        # ExecutionTimeLimit 0 = no limit: the keeper is meant to run until the owner
        # disconnects, and a task timeout would take the tunnel down with it.
        "$s = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero); "
        "Register-ScheduledTask -TaskName '{2}' -Action $a -Principal $p -Settings $s -Force "
        "| Out-Null; "
        # Register-ScheduledTask does not always persist the hidden flag, so the
        # settings are written again explicitly.
        "Set-ScheduledTask -TaskName '{2}' -Settings $s | Out-Null"
    ).format(launcher, ROOT, TUNNEL_TASK_NAME)
    completed = _run_hidden(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", command],
        timeout=30,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise HTTPException(503, f"无法注册隧道任务：{detail[0] if detail else completed.returncode}")


def start_tunnel():
    """Bring the tunnel up, (re)registering its task so the settings are current.

    Re-registering is deliberate rather than conditional: it refreshes the hidden
    window style and the exact command line every time the owner asks for the tunnel
    back, and it does not disturb a tunnel that is already running.

    A leftover task instance is ended first, and that is the part that had to be
    measured to be believed: after the tunnel was stopped once by hand, Task Scheduler
    still called the task Running, so every later `schtasks /Run` printed SUCCESS and
    did nothing at all —the keeper was never re-launched, and the only symptom was a
    gateway that reported the tunnel as online while no cloudflared existed. Ending the
    instance is what makes `/Run` mean "run".
    """
    register_tunnel_task()
    if tunnel_process() is not None:
        return True
    _run_hidden(["schtasks.exe", "/End", "/TN", TUNNEL_TASK_NAME], timeout=10)
    _run_hidden(["schtasks.exe", "/Run", "/TN", TUNNEL_TASK_NAME], timeout=10)
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if tunnel_process() is not None:
            return True
        time.sleep(0.5)
    return False


def stop_tunnel() -> bool:
    """把公网隧道收掉（页面关闭后的自动收尾用），返回是否真的停了什么。

    顺序有讲究：先 `schtasks /End` 结束任务实例——否则守护进程（keeper）会把 cloudflared
    再拉起来——然后才终止 cloudflared 本身。任务*不注销**，下次`start_tunnel()` 还是
    `/Run` 它；这一点是实测换来的：任务实例没结束时 `/Run` 会打印SUCCESS 却什么都不做。

    注意（写给以后看这里的人）：quick tunnel 的域名是**每次新建时随机给的*，所以停一次
    再起一次，公网地址就换了；已经发出去的链接因此失效。要固定地址得用命名隧道。
    """
    _run_hidden(["schtasks.exe", "/End", "/TN", TUNNEL_TASK_NAME], timeout=10)
    process = tunnel_process()
    if process is None:
        return False
    try:
        process.terminate()
        process.wait(timeout=10)
    except (psutil.Error, OSError):
        with suppress(psutil.Error, OSError):
            process.kill()
    _tunnel_state.update(checked_at=0.0, online=False)
    LOGGER.warning("Tunnel stopped for the page-close shutdown")
    return True


def share_online():
    """Whether the public tunnel is up, cached because it walks the process table."""
    now = time.monotonic()
    if now - _tunnel_state["checked_at"] < 5:
        return _tunnel_state["online"]
    try:
        online = tunnel_process() is not None
    except (psutil.Error, OSError):
        online = False
    _tunnel_state.update(checked_at=now, online=online)
    return online


def fresh_share_online():
    """`share_online` without its cache, for callers that must not read a stale verdict."""
    _tunnel_state["checked_at"] = 0.0
    return share_online()


def set_share_online(online):
    """One-click disconnect or resume, keeping the tunnel configuration."""
    _tunnel_state["checked_at"] = 0.0
    if online:
        if start_tunnel():
            online = True
        else:
            online = share_online()
    else:
        try:
            completed = _run_hidden(["schtasks.exe", "/End", "/TN", TUNNEL_TASK_NAME], timeout=10)
        except (OSError, subprocess.SubprocessError, UnicodeDecodeError, ValueError) as exc:
            raise HTTPException(503, f"无法操作隧道任务：{exc}")
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip().splitlines()
            raise HTTPException(503, f"隧道任务操作失败：{detail[0] if detail else completed.returncode}")
        # The task starts cloudflared through a launcher and then finishes, so ending
        # the task does not reach the tunnel process: it is stopped directly, which is
        # also what makes disconnect visible in the panel immediately.
        process = tunnel_process()
        if process is not None:
            try:
                process.terminate()
            except (psutil.Error, OSError):
                pass
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            _tunnel_state["checked_at"] = 0.0
            if not share_online():
                break
            time.sleep(0.5)
        online = share_online()
    LOGGER.info("Sharing: tunnel %s by owner", "resumed" if online else "disconnected")
    return online


def share_tunnel_url():
    """The public address cloudflared reported, so the panel can show the link.

    A quick tunnel prints its random hostname into its log and nowhere else, so the
    log is the only source of truth for it. The newest match is used, and a stale
    one from an earlier run is still better than showing the owner nothing.
    """
    log_path = ROOT / "logs" / "cloudflared.log"
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except (OSError, ValueError):
        return ""
    matches = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", text)
    return matches[-1] if matches else ""


async def bring_tunnel_up():
    """Start the public tunnel shortly after the gateway comes up, in sharing mode.

    Doing this at startup is what makes the desktop entry a single action: the owner
    double-clicks once and both the local service and the public address come up.

    The tunnel is only *started* when it is genuinely absent. The share launcher brings
    it up before the gateway is even ready, and the recovery path has to end the task
    instance before it can run it again —so asking for it unconditionally would tear
    down a perfectly good tunnel on every gateway restart, which is exactly the wrong
    way round for something whose URL changes each time it is recreated.
    """
    await asyncio.sleep(2)

    async def tunnel_is_up():
        """Whether a tunnel exists *now*, with the five-second cache bypassed.

        The plain probe caches its verdict for five seconds, which is longer than the
        window this has to observe: a tunnel that exists but is torn down moments later
        would otherwise be reported as established, and the retry below would never run.
        """
        return await asyncio.to_thread(fresh_share_online)

    for attempt in range(3):
        try:
            if await asyncio.to_thread(fresh_share_online):
                LOGGER.info("Sharing: tunnel already up at startup")
                return
            # Shielded: shutting the gateway down mid-start must not cancel a thread that
            # is already registering the tunnel task.
            online = await asyncio.shield(asyncio.to_thread(set_share_online, True))
            if online:
                # Established is not the same as staying established: measured on this
                # machine, a tunnel can disappear during the launcher's own startup, so
                # the claim is only accepted after it is still there a moment later.
                await asyncio.sleep(3)
                online = await tunnel_is_up()
        except asyncio.CancelledError:
            return
        except Exception:
            LOGGER.exception("Sharing: could not start the tunnel at startup")
            online = False
        if online:
            LOGGER.info("Sharing: tunnel online at startup")
            return
        await asyncio.sleep(10)
    LOGGER.warning("Sharing: the tunnel did not come up at startup; "
                   "the panel's 恢复连接 restarts it")


@app.get("/local/share/status")
async def share_status(request: Request):
    require_local_session(request)
    present = request.cookies.get(ACCESS_COOKIE) or ""
    online = share_online()
    payload = share_store.summarize(current_token=present, online=online)
    payload["instance_id"] = manager.instance_id
    payload["updated_at"] = time.time()
    payload["url"] = share_tunnel_url() if online else ""
    payload["guest_uploads"] = guest_upload_policy()
    # What a shared visitor may do, all four switches at once: the panel shows them beside the
    # upload policy, and the upload policy is now one of them (storage).
    payload["guest_policy"] = guest_policy.snapshot()
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


def guest_upload_policy():
    """The guest-upload switch plus what shared visitors have stored so far.

    Read through the router's own binding, not a fresh store: the panel and the routes
    must agree on the switch, or the panel could report guest uploads as open while
    every upload still fails.

    The switch itself now lives in ``guest_policy`` (the "storage" feature); the store keeps the
    ownership records, the quotas and their usage.
    """
    store = video_routes.policy_store()
    return {"policy": "guests" if guest_policy.allows("storage") else "off",
            "policies": list(guest_uploads.POLICIES),
            "limits": {"session_bytes": guest_uploads.GUEST_SESSION_BYTES,
                       "total_bytes": guest_uploads.GUEST_TOTAL_BYTES},
            "usage": store.usage()}


class GuestUploadPolicyRequest(BaseModel):
    policy: str


@app.get("/local/share/uploads")
async def share_upload_policy(request: Request):
    require_local_session(request)
    return JSONResponse(guest_upload_policy(), headers={"Cache-Control": "no-store"})


@app.post("/local/share/uploads")
async def share_set_upload_policy(body: GuestUploadPolicyRequest, request: Request):
    """Open or close clip upload for shared visitors.

    The switch is the "storage" capability of ``guest_policy``; this route is the older,
    upload-only way the panel used to reach it, kept so an existing panel (and the acceptance
    script) keeps working. Opening it also opens the matching download: a visitor may fetch back
    the clip it uploaded, and nothing else.
    """
    require_local_session(request)
    try:
        guest_policy.policy().set_feature("storage", body.policy == "guests")
        policy = video_routes.policy_store().set_policy(body.policy)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    LOGGER.info("Sharing: owner set the clip upload policy to %s", policy)
    return JSONResponse({**guest_upload_policy(), "policy": policy,
                         "guest_policy": guest_policy.snapshot()},
                        headers={"Cache-Control": "no-store"})


@app.post("/local/share/sessions/revoke-all")
async def share_revoke_all(request: Request):
    """Kick every shared device at once and delete their records.

    Registered before the single-session route so the literal path wins over the
    ``{handle}`` pattern.
    """
    require_local_session(request)
    tokens = share_store.revoke_all_devices(tokens_only=True)
    for token in tokens:
        video_routes.policy_store().forget_session(token)
        guest_uploads.forget_source(token)
    LOGGER.info("Sharing: owner kicked every device (%d record(s) removed)", len(tokens))
    return JSONResponse({"kicked": len(tokens)}, headers={"Cache-Control": "no-store"})


@app.post("/local/share/sessions/forget-unused")
async def share_forget_unused(request: Request):
    """Drop device records that never served a request, keeping every active one."""
    require_local_session(request)
    removed = share_store.forget_unused_devices()
    LOGGER.info("Sharing: owner cleared %d unused device record(s)", removed)
    return JSONResponse({"removed": removed}, headers={"Cache-Control": "no-store"})


@app.post("/local/share/sessions/{handle}/revoke")
async def share_revoke(handle: str, request: Request):
    require_local_session(request)
    token = share_store.token_for_handle(handle)
    if token is None:
        raise HTTPException(404, "找不到该会话")
    present = request.cookies.get(ACCESS_COOKIE) or ""
    if token == present:
        raise HTTPException(409, "不能踢出当前正在使用的会话")
    if not share_store.revoke(token):
        raise HTTPException(409, "该会话已失效")
    # A kicked device must also stop owning what it uploaded: its records are dropped
    # so re-joining with the passphrase does not inherit the earlier clips. The files
    # themselves stay on disk for the owner to keep or delete.
    dropped = video_routes.policy_store().forget_session(token)
    guest_uploads.forget_source(token)
    LOGGER.info("Sharing: owner revoked session %s and removed its record", handle)
    return JSONResponse({"revoked": True, "token": handle, "clips": len(dropped)},
                        headers={"Cache-Control": "no-store"})


@app.post("/local/share/stop")
async def share_stop(request: Request):
    """End sharing and shut this gateway down, from the panel.

    Stopping the tunnel alone would leave the service listening on every interface,
    which is not what "stop" means to the owner: this closes the public address and
    the local service together, and leaves the configuration in place so the next
    launch comes back with the same passphrase and session list.
    """
    require_local_session(request)
    try:
        set_share_online(False)
    except HTTPException as exc:
        # A tunnel that is already down must not block shutting the service down.
        LOGGER.warning("Sharing: tunnel stop reported %s", exc.detail)
    async with manager.lock:
        if manager.active_requests:
            raise HTTPException(409, "有请求正在生成，请等它结束后再停止。")
        manager.state = "stopping"
        # The marker keeps a supervisor from respawning the gateway after an
        # intentional stop; the next launcher removes it on start.
        (ROOT / "service.stop").write_text(datetime.now().isoformat(), encoding="ascii")
        if server is not None:
            server.should_exit = True
    LOGGER.info("Sharing: owner stopped sharing and the gateway from the panel")
    return JSONResponse({"stopped": True}, headers={"Cache-Control": "no-store"})


@app.post("/local/share/pin")
async def share_rotate_pin(request: Request):
    require_local_session(request)
    body = await request.json()
    pin = body.get("pin") if isinstance(body, dict) else None
    if not isinstance(pin, str) or not re.fullmatch(r"\d{6}-\d{6}-\d{6}", pin.strip()):
        raise HTTPException(400, "口令格式应为 123456-789012-345678")
    pin = pin.strip()
    rotated_at = share_store.set_pin(pin)
    # The running process reads the passphrase per request, so nothing has to be
    # restarted and devices that already joined keep their own session tokens.
    LOGGER.info("Sharing: passphrase rotated; %d existing session(s) unaffected",
                share_store.session_count())
    return JSONResponse({"pin": pin, "rotated_at": rotated_at},
                        headers={"Cache-Control": "no-store"})


@app.post("/local/share/online")
async def share_set_online(request: Request):
    require_local_session(request)
    body = await request.json()
    online = body.get("online") if isinstance(body, dict) else None
    if not isinstance(online, bool):
        raise HTTPException(400, "online 必须是布尔值")
    return JSONResponse({"online": set_share_online(online)}, headers={"Cache-Control": "no-store"})


def gpu_caller(request):
    """Who is asking for the GPU: (session token, is owner, label for the dialog).

    The label is what the owner sees in the "who is on the card" question, so it is
    taken from the session record the sharing panel already shows rather than invented
    here.
    """
    owner = is_admin_session(request)
    token = str(request.cookies.get(ACCESS_COOKIE) or "")
    if owner and not token:
        token = guest_uploads.OWNER_SESSION
    if not token:
        token = "anonymous"
    session = share_store.authenticate(token) if token and not owner else None
    label = ""
    if isinstance(session, dict):
        label = str(session.get("label") or "")
    if not label:
        label = "本机管理" if owner else device_label(request)
    return token, owner, label


class GpuPreemptRequest(BaseModel):
    mode: str = "interrupt"


@app.get("/local/gpu")
async def gpu_status(request: Request):
    """Live VRAM, who is on the card, and whether visitors are currently held off.

    Read by the owner's console to decide whether to answer the "someone else is
    using the GPU" question. Owner-only: it names the devices and their work.
    """
    require_local_session(request)
    payload = await gpu.status()
    payload["preempt_param"] = GPU_PREEMPT_PARAM
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.post("/local/gpu/preempt")
async def gpu_preempt(body: GpuPreemptRequest, request: Request):
    """Take the GPU from visiting users, either by holding them off or by stopping them.

    ``mode=interrupt`` cancels the jobs that are actually running (ComfyUI's queue and
    the video pipeline) and then forbids new visitor work; ``mode=queue`` only jumps the
    queue, letting whoever is mid-generation finish. Waiting is the honest default for
    "中断任务" because an in-flight language-model turn is streamed straight through and
    has no cancel hook —that is reported in ``stopped.in_flight`` rather than pretended
    away.
    """
    require_local_session(request)
    mode = (body.mode or "interrupt").strip().lower()
    if mode not in {"queue", "interrupt"}:
        raise HTTPException(400, "mode 只能是queue 、interrupt")
    gpu.begin_preempt("owner took the GPU" if mode == "queue" else "owner stopped visitor work")
    stopped = {"image": False, "video": False, "queue": 0, "in_flight": []}
    if mode == "interrupt":
        stopped = await stop_visitor_work()
    LOGGER.info("GPU: owner preempted visitors (mode=%s, stopped=%s)", mode, stopped)
    return JSONResponse({"preempting": True, "mode": mode, "stopped": stopped,
                         "status": await gpu.status()}, headers={"Cache-Control": "no-store"})


@app.post("/local/gpu/release")
async def gpu_release(request: Request):
    """Let visiting users use the card again."""
    require_local_session(request)
    gpu.end_preempt()
    return JSONResponse({"preempting": False, "status": await gpu.status()},
                        headers={"Cache-Control": "no-store"})


async def stop_visitor_work():
    """Stop what is on the card right now, and say what could not be stopped.

    Only visitor work is a target: cancelling the owner's own job while answering "take
    the card for me" would be the opposite of what was asked.
    """
    result = {"image": False, "video": False, "queue": 0, "in_flight": []}
    for entry in gpu.guests_working():
        if entry.kind in {"text", "vision"}:
            result["in_flight"].append({"label": entry.label, "kind": entry.kind,
                                        "kind_label": gpu_arbiter.KIND_LABELS.get(entry.kind, entry.kind)})
    monitor = video_routes.monitor()
    if monitor is not None and monitor.busy:
        with suppress(HTTPException, asyncio.CancelledError, httpx.HTTPError, OSError):
            await monitor.cancel()
            result["video"] = True
            LOGGER.info("GPU: cancelled the running video job")
    if image_jobs.busy:
        with suppress(HTTPException, asyncio.CancelledError, httpx.HTTPError, OSError):
            await image_jobs.cancel()
            result["image"] = True
            LOGGER.info("GPU: cancelled the running image job")
    client = manager.client
    if client is not None:
        try:
            response = await client.get(f"{image_comfy.COMFY_URL}/queue", timeout=10)
            queue = response.json() if response.status_code == 200 else {}
        except (httpx.HTTPError, OSError, ValueError):
            queue = {}
        pending = [item[1] for item in (queue.get("queue_pending") or []) if len(item) > 1]
        if pending:
            with suppress(httpx.HTTPError, OSError):
                await client.post(f"{image_comfy.COMFY_URL}/queue", json={"delete": pending}, timeout=15)
                result["queue"] = len(pending)
                LOGGER.info("GPU: cleared %d queued ComfyUI prompt(s)", len(pending))
    return result


@app.get("/local/voice/status")
async def voice_status():
    """Whether a local recognition model is ready, and what to do when it is not."""
    return JSONResponse(voice_input.describe(), headers={"Cache-Control": "no-store"})


@app.post("/local/voice/transcribe")
async def voice_transcribe(request: Request):
    """Recognize one recorded clip, on this machine's CPU.

    Raw audio body rather than multipart, matching the clip upload: the browser sends
    what the recorder produced and nothing has to be re-encoded into a form.
    """
    require_feature(request, "chat")
    language = request.query_params.get("language") or voice_input.DEFAULT_LANGUAGE
    payload = await request.body()
    try:
        result = await voice_input.transcribe(payload, language)
    except voice_input.VoiceError as error:
        LOGGER.info("voice: refused (%s) %s", error.status, error.message)
        return JSONResponse({"error": {"message": error.message}}, status_code=error.status)
    LOGGER.info("voice: %d byte(s) → %d char(s) in %ss (%s)",
                len(payload), len(result["text"]), result["seconds"], result["language"])
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@app.post("/local/voice/stream")
async def voice_stream_feed(request: Request):
    """Take one chunk of live 16 kHz PCM and answer with the text so far.

    The browser sends raw samples as it records, so text appears while the owner is still
    speaking. Sessions are keyed by the caller's own session token, so two consoles open
    on the same machine cannot read each other's audio. An empty body is a poll: it reads
    the current state and leaves the server's idle finalization alone.
    """
    require_feature(request, "chat")
    language = request.query_params.get("language") or voice_input.DEFAULT_LANGUAGE
    payload = await request.body()
    session = request_identity(request)[1] or "anonymous"
    try:
        snapshot = await voice_input.stream_feed(session, payload, language)
    except voice_input.VoiceError as error:
        return JSONResponse({"error": {"message": error.message}}, status_code=error.status)
    return JSONResponse(snapshot, headers={"Cache-Control": "no-store"})


@app.post("/local/voice/stream/finish")
async def voice_stream_finish(request: Request):
    """End the live recording and return the final transcript.

    Reads whatever audio has not been committed yet, so the last words are never lost to
    the streaming window.
    """
    require_feature(request, "chat")
    language = request.query_params.get("language") or voice_input.DEFAULT_LANGUAGE
    session = request_identity(request)[1] or "anonymous"
    try:
        snapshot = await voice_input.stream_finish(session, language)
    except voice_input.VoiceError as error:
        return JSONResponse({"error": {"message": error.message}}, status_code=error.status)
    LOGGER.info("voice: streaming finished with %d char(s) from %ss of audio",
                len(snapshot["text"]), snapshot["duration_seconds"])
    return JSONResponse(snapshot, headers={"Cache-Control": "no-store"})


@app.post("/local/voice/cancel")
async def voice_stream_cancel(request: Request):
    """Abandon a live recording without transcribing the remainder."""
    require_feature(request, "chat")
    session = request_identity(request)[1] or "anonymous"
    voice_input.stream_drop(session)
    return JSONResponse({"cancelled": True}, headers={"Cache-Control": "no-store"})


class SynthRequest(BaseModel):
    text: str
    speaker: str = "uncle_fu"
    language: str = "chinese"
    # A natural-language render instruction, handed to the engine, which renders it into the
    # prompt as a user turn ("用温柔的语气、 / "充满活力的播、). Empty means the engine's own
    # default delivery. This was previously not even in the protocol, so the engine's documented
    # emotion/style control could never be reached from the app.
    instruct: str = ""


# The TTS engine is driven over a pipe rather than imported: it binds llama.cpp and
# ONNX Runtime through ctypes, and those DLLs are already loaded into this process
# by the chat backend. A subprocess also means a synthesis crash cannot take the
# gateway down with it.
VOICE_TTS_SCRIPT = ROOT / "tools" / "tts_server.py"
_tts_process = None

# Synthesis belongs on the card when it fits. Measured end to end on a 9 s utterance:
# Talker and Predictor on CUDA plus the DirectML decoder take the run from RTF 1.47 to
# 0.59. The predictor alone is 78.6 M parameters read per generated token, so on the
# processor it is memory-bandwidth bound, and the ONNX decoder on the processor also
# has to fight llama.cpp for threads. The pair costs about 2.0 GB of VRAM, which is
# why the attempt is gated on that much being free and why a failure drops back to the
# processor instead of leaving voice mode silent.
TTS_GPU_RESERVE_MIB = 2600
_tts_force_cpu = False


def _gpu_free_mib():
    """Free VRAM in MiB, or None when the query cannot be answered.

    A helper, not an endpoint: it takes no `request` and authorises nothing - the feature
    check belongs to the handler that called it, and `voice_speak` already does its own.
    Reading `request` here raised NameError inside synthesis, which the endpoint reported
    as an opaque 500. The panel treats a failed synthesis as a reason to fall back to the
    browser's own voice, so the turn still "worked" and the failure was invisible - except
    that no audio was ever produced, and a turn with no audio has nothing to store, which
    is why the history had no voice bars to replay.
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None


def _tts_alive():
    return _tts_process is not None and _tts_process.poll() is None


async def _ensure_tts():
    """Start the synthesis worker on first use, once - and never hand back a useless one.

    `_tts_alive()` only asks whether the process exists. A worker whose pipes are gone, or whose
    engine never came up, stays "alive" while answering nothing or answering an error - and reusing
    it makes *every* sentence of a reply fail synthesis, which is exactly how a whole reply came to
    be read out by the browser's voice instead of spoken. So a running worker is asked a question
    before it is trusted, and one that cannot answer is replaced here rather than failing sentence
    by sentence for the rest of the session.
    """
    global _tts_process
    if _tts_alive():
        probe = _tts_process
        if await _tts_answers():
            return True
        # Only drop the worker this probe actually asked. `_warm_tts` and a turn can replace it
        # concurrently, and stopping whatever is current here would kill the replacement too.
        if _tts_process is probe:
            LOGGER.warning("voice: the synthesis worker is running but not answering; replacing it")
            await _stop_tts()
    if not VOICE_TTS_SCRIPT.is_file():
        return False
    python = ROOT / "tools" / "tts-venv" / "Scripts" / "python.exe"
    if not python.is_file():
        return False
    environment = dict(os.environ)
    # An explicit QWEN_TTS_CPU in the environment is the owner's decision and wins.
    if "QWEN_TTS_CPU" not in os.environ:
        if _tts_force_cpu:
            environment["QWEN_TTS_CPU"] = "1"
        else:
            free = _gpu_free_mib()
            if free is None or free < TTS_GPU_RESERVE_MIB:
                LOGGER.info("voice: %s MiB free on the card, synthesising on the processor", free)
                environment["QWEN_TTS_CPU"] = "1"
    # The worker's own stderr is kept rather than discarded. It is the only place the engine says
    # *why* it could not start - a failed ONNX allocation arrives as "bad allocation" on stderr and
    # nowhere else, while the caller only ever sees the worker's generic
    # {"error": "TTS engine unavailable"}. Losing that one line is why a follow-up turns into an
    # afternoon of guessing: the gateway log said "synthesis failed" and nothing more.
    try:
        worker_errors = open(ROOT / "logs" / "tts-worker.err.log", "a", encoding="utf-8", buffering=1)
    except OSError:
        worker_errors = subprocess.DEVNULL
    try:
        _tts_process = subprocess.Popen(
            [str(python), "-X", "utf8", str(VOICE_TTS_SCRIPT)],
            cwd=str(ROOT / "tools" / "Qwen3-TTS-GGUF"),
            stdout=subprocess.PIPE, stderr=worker_errors,
            stdin=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
            env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except OSError:
        LOGGER.exception("voice: could not start the TTS worker")
        _tts_process = None
        return False
    finally:
        # The child has its own handle by now; ours is only needed for the spawn.
        if worker_errors is not subprocess.DEVNULL:
            with suppress(OSError):
                worker_errors.close()
    return True


async def _synthesize(text, speaker, language, instruct="", voice=None):
    """Return WAV bytes, or None when no local synthesiser can serve the request.

    None is not an error: the browser falls back to its own speech synthesis, so
    voice mode keeps talking instead of failing when this backend is unavailable.

    The first call of a session also builds the engine (a few seconds of model load),
    so one retry is allowed: without it the very first spoken reply of every session
    would fall back to the browser voice for no reason the user could see.

    `instruct` and `voice` are passed through as given: the worker owns what they mean.
    """
    if not await _ensure_tts():
        return None
    payload = {"text": text, "speaker": speaker, "language": language}
    if instruct:
        payload["instruct"] = instruct
    if voice:
        payload["voice"] = voice
    request = json.dumps(payload, ensure_ascii=False)

    def exchange(timeout_hint):
        _tts_process.stdin.write(request + "\n")
        _tts_process.stdin.flush()
        line = _tts_process.stdout.readline()
        return line.strip() if line else ""

    for attempt in (1, 2):
        try:
            line = await asyncio.wait_for(asyncio.to_thread(exchange, attempt), timeout=180)
        except (asyncio.TimeoutError, OSError, ValueError):
            LOGGER.warning("voice: synthesis attempt %s failed", attempt, exc_info=True)
            # A timed-out read leaves the worker's reply queued; drop the process so
            # the next attempt starts from a known state.
            await _stop_tts()
            if attempt == 2 or not await _ensure_tts():
                return None
            continue
        if not line:
            await _stop_tts()
            if attempt == 2 or not await _ensure_tts():
                return None
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            # Something that was not this protocol wrote on the pipe. Say so instead of looping on
            # in silence: a reply the worker really did send, discarded without a word, is one of the
            # two ways a reply came out with no audio and nothing in the log to explain it.
            LOGGER.warning("voice: the worker's reply was not JSON (%r)", line[:120])
            continue
        if payload.get("error") or not payload.get("path"):
            LOGGER.warning("voice: synthesis failed: %s", payload.get("error"))
            return None
        path = Path(payload["path"])
        if not path.is_file():
            # The worker runs with tools/Qwen3-TTS-GGUF as its cwd while this process runs from the
            # project root, so a relative path in its reply names two different files. Look where the
            # worker would have written it before giving up - and never fail silently here, which is
            # the other way a text-only reply left no trace at all.
            for candidate in (ROOT / path, ROOT / "tools" / "Qwen3-TTS-GGUF" / path):
                if candidate.is_file():
                    LOGGER.info("voice: the worker's audio was found at %s (it said %s)",
                                candidate, payload["path"])
                    path = candidate
                    break
        if not path.is_file():
            LOGGER.warning("voice: the worker named %s but it is not a file (cwd=%s)",
                           payload["path"], Path.cwd())
            return None
        data = path.read_bytes()
        with suppress(OSError):
            path.unlink()
        return data
    return None


# The resident backend is launched with --sleep-idle-seconds 300: after five idle
# minutes it unloads its weights and reloads them on the next request. That is right for
# a chat page someone leaves open, and wrong for a spoken conversation, where a pause
# that long is ordinary - the user is reading or thinking. The reload showed up as a
# first token 16.41 s into the turn, against 0.4-1.2 s with the model resident.
VOICE_KEEPALIVE_SECONDS = int(os.environ.get("QWEN_VOICE_KEEPALIVE", "240") or 0)
_voice_keepalive = None


def _start_voice_keepalive():
    """Nudge the backend while a voice session holds the card, so it never sleeps.

    The task is self-terminating: each tick re-reads the flag it was started for and
    returns as soon as voice mode lets the card go, so no release path has to remember
    to cancel it. A one-token completion is the cheapest request that resets the idle
    timer - /health and /props never enter the task queue, so they do not count.
    """
    global _voice_keepalive
    if not VOICE_KEEPALIVE_SECONDS:
        return
    if _voice_keepalive is not None and not _voice_keepalive.done():
        return

    async def beat():
        while True:
            await asyncio.sleep(VOICE_KEEPALIVE_SECONDS)
            if not getattr(manager, "voice_holds_card", False):
                return
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    await client.post(
                        f"http://127.0.0.1:{voice_mode.VOICE.brain_port}/v1/chat/completions",
                        json={"messages": [{"role": "user", "content": "你好"}],
                              "max_tokens": 1, "temperature": 0, "stream": False},
                    )
            except Exception:  # noqa: BLE001 - a missed beat must not kill the task
                LOGGER.warning("voice: keep-alive nudge failed", exc_info=True)

    _voice_keepalive = asyncio.create_task(beat())
    LOGGER.info("voice: keeping the backend awake every %ss", VOICE_KEEPALIVE_SECONDS)


async def _stop_tts():
    """Drop the synthesis worker so the next call rebuilds it cleanly."""
    global _tts_process
    process, _tts_process = _tts_process, None
    if process is None:
        return
    with suppress(OSError):
        process.terminate()
    try:
        await asyncio.wait_for(asyncio.to_thread(process.wait), 10)
    except (asyncio.TimeoutError, OSError):
        with suppress(OSError):
            process.kill()


async def _tts_answers(timeout=45):
    """Whether the worker actually answers on its pipe - not whether its process exists.

    Distinct from `_tts_ready()`, which waits for the *engine* to be up: this asks only whether the
    pipe works, so a worker that is merely still building its engine is not mistaken for a dead one.
    The two failure shapes are both covered here: a closed pipe raises OSError on the write, and a
    pipe whose far end is gone comes back as an empty line. Building the engine has never taken more
    than about 20 s in the logs, so a worker that cannot answer within 45 s is treated as useless.
    """
    if not _tts_alive():
        return False
    # Take the process once: a concurrent `_stop_tts` (a replacement, or the GPU-to-processor retry)
    # can clear `_tts_process` between the check and the use, and the AttributeError that produced
    # used to surface as an opaque HTTP 500 on the very turn it was meant to help.
    process = _tts_process
    request = json.dumps({"op": "ping"}, ensure_ascii=False)

    def ask():
        process.stdin.write(request + "\n")
        process.stdin.flush()
        return process.stdout.readline()

    try:
        line = await asyncio.wait_for(asyncio.to_thread(ask), timeout=timeout)
    except Exception:  # noqa: BLE001 - a probe must never be the thing that fails a turn
        return False
    return bool((line or "").strip())


async def _tts_ready():
    """Ask the worker whether its engine is actually up.

    The worker builds the engine before answering a ping, so this is a readiness
    probe rather than a bare liveness check: it is what lets a failed GPU load be
    told apart from a worker that is merely still loading.
    """
    if not _tts_alive():
        return False
    request = json.dumps({"op": "ping"}, ensure_ascii=False)

    def ping():
        _tts_process.stdin.write(request + "\n")
        _tts_process.stdin.flush()
        return _tts_process.stdout.readline()

    try:
        line = await asyncio.wait_for(asyncio.to_thread(ping), timeout=300)
    except (asyncio.TimeoutError, OSError, ValueError):
        LOGGER.warning("voice: TTS readiness probe did not finish", exc_info=True)
        return False
    if not line:
        return False
    try:
        payload = json.loads(line)
    except ValueError:
        return False
    return bool(payload.get("ready"))


async def _warm_tts():
    """Build the engine while nobody is waiting, so the first reply is not delayed."""
    global _tts_force_cpu
    if not await _ensure_tts():
        return False
    if await _tts_ready():
        return True
    # The free-memory check can pass and the load still fail: another job may have
    # taken the memory in between, or the driver may refuse the allocation. Retrying
    # once on the processor keeps voice mode talking at a lower frame rate instead of
    # dropping the reply to the browser's own voice.
    if not _tts_force_cpu and "QWEN_TTS_CPU" not in os.environ:
        LOGGER.warning("voice: GPU synthesiser did not come up, retrying on the processor")
        _tts_force_cpu = True
        await _stop_tts()
        if await _ensure_tts():
            return await _tts_ready()
    return False


@app.post("/local/voice/speak")
async def voice_speak(body: SynthRequest, request: Request):
    """Synthesize a reply with the local TTS engine.

    Answers 503 with a reason instead of an error page when synthesis cannot run. This view no
    longer falls back to the browser's voice - that is the owner's rule for it - so the reason is
    what the owner actually sees, and an opaque 500 tells them nothing. The docstring used to promise
    a browser fallback, which stopped being true when the fallback was removed.
    """
    require_feature(request, "chat")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    if len(text) > 2000:
        text = text[:2000]
    speaker, voice = _resolve_voice(body.speaker)
    try:
        audio = await _synthesize(text, speaker, body.language, body.instruct, voice)
    except HTTPException:
        raise
    except Exception as exc:                       # noqa: BLE001 - never an opaque 500 here
        LOGGER.exception("voice: synthesis raised")
        raise HTTPException(503, f"本地语音合成出错：{type(exc).__name__}: {exc}") from exc
    if audio is None:
        raise HTTPException(503, "本地语音合成不可用")
    return Response(content=audio, media_type="audio/wav",
                    headers={"Cache-Control": "no-store"})


# Cloned voices: a reference recording plus the words spoken in it.
#
# The engine's anchor cannot be persisted - it is reference codes plus a speaker embedding,
# both derived - but the pair that builds it can be, so a cloned voice survives a restart by
# being prepared again on first use. The worker compares that pair against the anchor it already
# holds, so the preparation cost is paid once per voice rather than once per sentence.
VOICE_CLONE_DIR = ROOT / "voices"
VOICE_CLONE_INDEX = VOICE_CLONE_DIR / "cloned.json"
VOICE_CLONE_MAX_BYTES = 40 * 1024 * 1024


def _cloned_voices():
    try:
        rows = json.loads(VOICE_CLONE_INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [row for row in rows
            if isinstance(row, dict) and row.get("id") and row.get("audio")]


def _write_cloned_voices(rows):
    VOICE_CLONE_DIR.mkdir(parents=True, exist_ok=True)
    VOICE_CLONE_INDEX.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def _resolve_voice(speaker):
    """Turn the panel's voice id into (speaker, voice) for the worker.

    A cloned voice is named `clone:<id>`; a built-in one is its own name. Both travel the same
    field because that is what the picker is: one list of voices, from two sources.
    """
    name = str(speaker or "")
    if not name.startswith("clone:"):
        return name or "uncle_fu", None
    row = next((item for item in _cloned_voices() if item["id"] == name.split(":", 1)[1]), None)
    if row is None:
        raise HTTPException(404, "这个克隆音色已经不在了，请在设置里重新添加")
    return "uncle_fu", {"audio": row["audio"], "text": row.get("text", "")}


async def _tts_exchange(payload, timeout=300):
    """One request/reply on the worker pipe, with the worker's own error surfaced."""
    if not await _ensure_tts():
        raise HTTPException(503, "本地语音合成不可用")

    def exchange():
        _tts_process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        _tts_process.stdin.flush()
        return _tts_process.stdout.readline()

    try:
        line = await asyncio.wait_for(asyncio.to_thread(exchange), timeout=timeout)
    except (asyncio.TimeoutError, OSError, ValueError) as exc:
        await _stop_tts()
        raise HTTPException(504, f"语音工作进程没有回应：{exc}") from exc
    try:
        reply = json.loads((line or "").strip() or "{}")
    except ValueError as exc:
        raise HTTPException(502, "语音工作进程返回了无法解析的内容") from exc
    if reply.get("error") or reply.get("ok") is False:
        message = str(reply.get("error") or "这个音色不可用")
        # An engine that never came up answers every request with the same error, so a worker kept
        # here fails every remaining sentence of the reply. Ask its engine directly and drop the
        # worker when it is not up, so the next sentence rebuilds instead of repeating the failure.
        if "engine" in message.lower() and not await _tts_ready():
            LOGGER.warning("voice: the synthesis engine is not up; dropping the worker to rebuild")
            await _stop_tts()
        raise HTTPException(400, message)
    return reply


@app.get("/local/voice/voices")
async def voice_voices():
    """Which speakers the local synthesiser can render, for the panel's picker."""
    cloned = [{"id": f"clone:{row['id']}", "label": row.get("label") or row["id"],
               "kind": "克隆"} for row in _cloned_voices()]
    return JSONResponse({
        "engine": "qwen3-tts-customvoice",
        "ready": _tts_alive(),
        "voices": [
            {"id": "uncle_fu", "label": "傅叔", "kind": "男声"},
            {"id": "serena", "label": "Serena", "kind": "女声"},
            {"id": "vivian", "label": "Vivian", "kind": "女声"},
            {"id": "ryan", "label": "Ryan", "kind": "男声"},
            {"id": "aiden", "label": "Aiden", "kind": "男声"},
            {"id": "ono_anna", "label": "Anna", "kind": "女声"},
            {"id": "sohee", "label": "Sohee", "kind": "女声"},
            {"id": "eric", "label": "Eric", "kind": "四川话"},
            {"id": "dylan", "label": "Dylan", "kind": "北京话"},
        ] + cloned,
    }, headers={"Cache-Control": "no-store"})


@app.post("/local/voice/voice/clone")
async def voice_clone(request: Request, name: str = "", text: str = ""):
    """Register a reference recording as a selectable voice.

    The recording is checked by the engine before it is saved: an anchor needs the reference's
    own words as well as its sound, so a recording the engine cannot use is rejected here rather
    than failing every sentence of the next reply.
    """
    require_feature(request, "chat")
    payload = await request.body()
    if len(payload) < 4096:
        raise HTTPException(400, "参考音频太短，至少要说一句话")
    if len(payload) > VOICE_CLONE_MAX_BYTES:
        raise HTTPException(413, f"参考音频超过{VOICE_CLONE_MAX_BYTES // (1024 * 1024)} MB")

    VOICE_CLONE_DIR.mkdir(parents=True, exist_ok=True)
    voice_id = "v" + uuid.uuid4().hex[:10]
    target = VOICE_CLONE_DIR / f"{voice_id}.wav"
    target.write_bytes(payload)
    try:
        await _tts_exchange({"op": "voice", "audio": str(target),
                             "text": (text or "").strip()}, timeout=600)
    except HTTPException:
        with suppress(OSError):
            target.unlink()
        raise

    rows = _cloned_voices()
    rows.append({"id": voice_id,
                 "label": (name or "").strip() or f"克隆音色 {len(rows) + 1}",
                 "audio": str(target),
                 "text": (text or "").strip(),
                 "created_at": time.time()})
    _write_cloned_voices(rows)
    LOGGER.info("voice: registered cloned voice %s from %s bytes", voice_id, len(payload))
    return JSONResponse({"id": f"clone:{voice_id}", "label": rows[-1]["label"], "kind": "克隆"})


@app.delete("/local/voice/voice/clone")
async def voice_clone_forget(id: str = "", request: Request = None):
    require_feature(request, "chat")
    rows = _cloned_voices()
    target = next((row for row in rows if row["id"] == id), None)
    if target is None:
        raise HTTPException(404, "没有这个克隆音色")
    with suppress(OSError):
        Path(target["audio"]).unlink()
    _write_cloned_voices([row for row in rows if row["id"] != id])
    return JSONResponse({"removed": id})


class VoiceStartRequest(BaseModel):
    profile: str = ""
    # Which kind of turn is about to run. "text" brings up the language model alone -
    # a typed turn never reaches a recogniser, so starting one would spend VRAM and
    # startup time on a model it cannot call. "voice" additionally brings up the
    # recogniser, because the spoken path is the only one that can use it.
    need: str = "text"


async def _record_voice_turn(key, reply, stats, asked_at, window=None):
    """File a spoken reply in the shared history and return the stats with its usage.

    The token counts come from the backend that produced the reply —the streaming usage the
    server already knows —rather than from a follow-up /tokenize call, which would contend
    with the generation it is trying to measure for the single resident model. The prompt a
    spoken turn replays is the system prompt plus the shared history, which is what the meter
    reports, so the spoken and written paths are answering the same question about the same
    window.
    """
    text = str(reply or "").strip()
    stats = dict(stats or {})
    if text:
        counted = stats.get("completion_tokens")
        ledger.append_turn(key, "assistant", text, mode="voice",
                           tokens=counted if counted else estimate_tokens(text))
    replayed = ([{"role": "system", "content": voice_mode.SYSTEM_PROMPT}]
                + ledger.messages(key, limit=8, max_chars=4000))
    prompt_tokens = stats.get("prompt_tokens")
    if prompt_tokens:
        used = int(prompt_tokens) + int(stats.get("completion_tokens") or 0)
        source = "measured"
        # Recorded on the conversation, not just returned: the panel reads the meter again
        # later, and a later read must not turn this measurement back into an estimate.
        ledger.record_measurement(key, used, window=window)
    else:
        used = sum(estimate_tokens(message["content"]) for message in replayed)
        source = "estimated"
    meter = ledger.meter(key, used_tokens=used, mode="voice", window=window)
    # The meter's own source describes the ledger's numbers; this one describes where *these*
    # numbers came from, and the two differ exactly when the backend reported its usage.
    meter["source"] = source
    LOGGER.info("voice turn usage: prompt=%s completion=%s window=%s used=%s source=%s",
                stats.get("prompt_tokens"), stats.get("completion_tokens"), window,
                meter["used_tokens"], source)
    enriched = dict(stats)
    enriched["used_tokens"] = meter["used_tokens"]
    enriched["remaining_tokens"] = meter["remaining_tokens"]
    enriched["meter"] = meter
    return enriched


class VoiceChatRequest(BaseModel):
    text: str = ""
    history: list = []
    profile: str = ""
    # The conversation this spoken turn belongs to. Sent by the voice panel so a spoken turn
    # lands in the same history and the same context meter as the written ones; absent, the
    # turn is filed under a conversation of its own rather than lost.
    conversation_id: str = ""
    # Server-sent events when true, so the caller can start speaking the first clause
    # before the reply is finished.
    stream: bool = False


@app.get("/local/voice/config")
async def voice_config():
    """语音能给哪个档位用、窗口多大——规则与拒绝对话框都来自这里（详见voice_mode.py）。

    2026-09-23 晚修正：以前这里报的是"锁死三元 27B"，于是选中 9B 时语音按钮不可点。现在报的是
    **当前选中的档位**与它自己的语音窗口（9B 40960 / 三元 27B 8192）；`supported` 为假时才拒绝，
    `reason` 会说明是"没有语音窗口"还是"权重不在本机"。
    """
    # The window a spoken turn runs with, taken from the selected profile's own
    # `voice_context_size` and reported from here so the panel can show the number a spoken turn
    # will get *before* the first turn switches the backend. Asking the *running* backend instead
    # is how the voice page came to advertise the text window for a mode that runs a voice one.
    selected = active_profile_name(manager.settings)
    allowed = voice_mode.VoiceMode.profile_allowed(selected)
    window = (voice_context({**manager.settings, "model_profile": selected})
              if allowed else None)
    return JSONResponse({
        "profile": selected,
        "profile_label": profile_label(selected),
        "locked": False,
        "supported": allowed,
        "reason": "" if allowed else voice_mode.VoiceMode.refusal_message(selected),
        "context_size": window,
        "context_locked": True,
    }, headers={"Cache-Control": "no-store"})


@app.post("/local/voice/mode")
async def voice_mode_switch(body: VoiceStartRequest, request: Request):
    """Start or stop the voice pairing (9B brain + recogniser). No prompt, ever.

    The rule this route implements, and the reason it asks the owner nothing:

    * **Same source** —the card already holds the profile voice mode is locked to. The
      pairing borrows that backend: nothing is unloaded, nothing is spawned for the
      brain, and only the recogniser is new. This is a plugin change.
    * **Different source** —some other model holds the card. Voice mode still needs its
      own, so the gateway switches to it *itself* and waits, instead of returning "this
      will release the current model, continue?" and making the owner answer a question
      whose answer is always the same. The synthesiser is not up yet at this point, so
      nothing is killed while it is in use.

    Whatever happens, the model that answers is the one this route reports: the panel's
    label comes from the same profile this decision was made with.
    """
    require_feature(request, "chat")
    wanted = (body.profile or "").strip() or active_profile_name(manager.settings)
    if not voice_mode.VoiceMode.profile_allowed(wanted):
        raise HTTPException(409, voice_mode.VoiceMode.refusal_message(wanted))
    busy = None
    if not body.profile:
        async with manager.lock:
            manager.status()
            if manager.active_requests:
                busy = "请等待当前回复结束"
            else:
                await voice_mode.VOICE.stop()
                # The synthesis worker holds a loaded engine and two decoder children; left
                # running they accumulate one set per session.
                await _stop_tts()
                manager.voice_holds_card = False
                if manager.mode == "voice":
                    manager.mode = "text"
                manager.state = "ready"
                manager.error = None
                manager.revision += 1
                # Leaving voice mode has to give the chat side its window back: the backend was
                # reloaded at the smaller voice window, and "ready" is not a reason to keep it 。
                # the watchdog only intervenes on a backend that is *not* ready, so a silent
                # downgrade of the written thread is what happens without this. Two steps, so a
                # `/local/status` that arrives in between still sees a ready gateway.
                desired = expected_context({**manager.settings, "model_profile":
                                            manager.launched_profile or active_profile_name(manager.settings)})
                if (manager.process is not None and manager.process.poll() is None
                        and getattr(manager, "launched_context", None)
                        and int(manager.launched_context) != desired
                        and not (manager.task and not manager.task.done())):
                    manager.state = "starting"
                    manager.task = asyncio.create_task(manager.load(manager.mode))
                return JSONResponse({"started": False, **voice_mode.VOICE.status()})
        raise HTTPException(409, busy)

    switch = None
    voice_ready = False
    async with manager.lock:
        manager.status()
        if manager.active_requests:
            busy = "请等待当前回复结束"
        else:
            # Same source is decided from the profile the backend was actually *launched*
            # for, never from the one settings.json currently names: a hand-edited file
            # must not be able to make a 27B resident pass for the 9B.
            launched = getattr(manager, "launched_profile", None) or active_profile_name(manager.settings)
            switch = source_switch("voice", launched=launched, current_mode=manager.mode,
                                   settings=manager.settings, target=wanted)
            # Same *weights* is not the same *window*. Text and vision run at the profile's
            # full context; voice always adds the recogniser (~1.1 GiB measured), so borrowing
            # that backend leaves too little for a spoken turn to generate omeasured 531 MiB
            # free while the pair was up, against 2.1 GiB at the voice window. The window is
            # therefore part of the switch: same weights with a different window still reloads.
            resident_context = None
            if manager.metadata.get("props"):
                resident_context = (manager.metadata["props"].get("default_generation_settings")
                                    or {}).get("n_ctx")
            wanted_context = voice_context({**manager.settings, "model_profile": switch["profile"]})
            context_changed = bool(resident_context) and int(resident_context) != wanted_context
            if context_changed:
                switch = {**switch, "same_source": False, "reload": True,
                          "context_from": int(resident_context), "context_to": wanted_context,
                          "reason": (f"语音模式需要{wanted_context} 的窗口（识别后端要占约 1.1 GiB），"
                                     f"当前后端是{int(resident_context)}，因此重新加载同一套权重。")}
                # The share is being replaced, not kept: clearing it is what stops the pair
                # from claiming a brain that is about to be a different process.
                voice_mode.VOICE.release_brain()
            if switch["same_source"]:
                voice_mode.VOICE.profile = wanted
                voice_mode.VOICE.share_brain(UPSTREAM_PORT)
                manager.voice_holds_card = False
                manager.error = None
                if manager.mode != "voice":
                    manager.mode = "voice"
                    manager.revision += 1
                LOGGER.info("Voice mode reuses the resident backend: %s", switch["reason"])
                # Set by the borrow itself, so the flag cannot outlive the backend it
                # describes: a reload below would otherwise leave "the brain is ready"
                # standing while the process it referred to is gone.
                voice_ready = True
            else:
                # Hand the card over to the voice profile as a plain model switch. The
                # manual flag is cleared first or load() would refuse to bring anything
                # back —the same swap through /local/voice/mode used to need an explicit
                # "you may preempt" answer from the owner.
                manager.voice_holds_card = False
                manager.state = "switching"
                manager.error = None
                manager.recovery_attempts = 0
                manager.retry_at = 0
                # `load()` builds its command line from the *selected* profile, and voice
                # mode needs one that is not selected (the owner's default may well be the
                # other model). Making the target the selection for the duration of the
                # load is what keeps the spawned backend, the context it validates and the
                # profile this route then reports all describing the same model —loading
                # it any other way produced a 9B-labelled backend that was actually the
                # 27B, which is exactly the mismatch this route exists to prevent.
                previous_settings = manager.settings
                # 这套权重、为这一档现、的：先把档位告诉 voice_mode，它启动自己的大脑（以及
                # 借用分支）时才知道该用哪一份，否则会拿掉线时写死的默认档位去启动。
                voice_mode.VOICE.profile = switch["profile"]
                # The mode's own window, not the text/vision one: voice always adds the
                # recogniser, so a backend loaded at the full text window would leave the
                # pair without the margin a spoken turn needs (measured: 1.0 GiB free at
                # 131072, 2.1 GiB at 40960).
                manager.settings = {
                    **previous_settings, "model_profile": switch["profile"],
                    # The key is what makes this window authoritative rather than a stale
                    # settings.json value: the profile owns the number unless the caller says
                    # whose window it is writing.
                    "context_size": switch.get("context_to") or voice_context(previous_settings),
                    "context_key": f"voice:{switch['profile']}",
                }
                LOGGER.info("Voice mode switches the base model itself: %s", switch["reason"])
                try:
                    # Held as a coroutine and awaited below rather than scheduled and
                    # tracked: the voice reply must not race the model it is about to talk
                    # to, so this route does not return until the swap is done.
                    await manager.load("voice" if switch["reload"] else "text")
                finally:
                    manager.settings = previous_settings
                LOGGER.info("Voice mode switch done: resident=%s mode=%s error=%r",
                            getattr(manager, "launched_profile", None), manager.mode, manager.error)
        if busy is not None:
            raise HTTPException(409, busy)
        if switch is not None and not switch["same_source"]:
            if getattr(manager, "launched_profile", None) == wanted:
                if not voice_ready:
                    # Shared exactly once: the same-source branch above has already borrowed
                    # this backend, and doing it again here would be a second, redundant loan
                    # of a process that is already lent.
                    voice_mode.VOICE.share_brain(UPSTREAM_PORT)
                    manager.mode = "voice"
                    manager.revision += 1
                    voice_ready = True
            else:
                manager.voice_holds_card = True
    if not voice_ready:
        detail = manager.error or "语音后端没能接管显卡"
        return JSONResponse({"started": False, "error": detail,
                             "switch": switch, **voice_mode.VOICE.status()}, status_code=503)
    # The recogniser is started only when this turn is actually spoken. `need` is the caller's own
    # answer to "will this turn reach a recogniser", and the panel sets it from what it is about to
    # do - a typed turn sends "text", a recorded one sends "voice".
    #
    # It used to be forced on whenever a profile was present, and the panel always sends one, so a
    # purely typed turn paid for a recogniser it could never call: tens of seconds of load time and
    # about 1.1 GiB of the card. That reservation is also the stated reason the voice profile has to
    # shrink its context window, which is what makes the same weights reload on the first voice
    # turn - so the cost was not only the recogniser's own startup.
    started = await voice_mode.VOICE.ensure_ready(need_asr=body.need == "voice")
    if not started:
        # Voice mode never came up: hand the backend back and let the card go.
        voice_mode.VOICE.release_brain()
        manager.voice_holds_card = False
        return JSONResponse({"started": False, "error": voice_mode.VOICE.last_error,
                             "switch": switch,
                             **voice_mode.VOICE.status()}, status_code=503)
    # Build the synthesiser in the background: loading its engine takes a few seconds,
    # and doing it now means the first spoken reply is not the one that waits.
    synthesiser = asyncio.create_task(_warm_tts())
    # The recogniser follows the synthesiser rather than preceding it, and it is warmed here - when
    # someone has actually walked into the voice view - instead of at gateway start-up. See
    # _warm_recogniser_late for why that order decides whether synthesis runs on the card.
    asyncio.create_task(_warm_recogniser_late(synthesiser))
    # And keep the language model itself from unloading between turns.
    _start_voice_keepalive()
    return JSONResponse({"started": True, "switch": switch, **voice_mode.VOICE.status()})


@app.post("/local/voice/chat")
async def voice_chat(body: VoiceChatRequest, request: Request):
    """One voice turn: transcript in, spoken-ready reply out.

    The profile is validated even though the audio path never reads it: this route
    must refuse anything but the locked 9B, so a caller cannot quietly route a turn
    to the slower model by passing its name.

    `stream=true` answers with server-sent events so the caller can speak the first
    clause while the rest of the reply is still being generated.
    """
    require_feature(request, "chat")
    wanted = (body.profile or "").strip() or active_profile_name(manager.settings)
    if not voice_mode.VoiceMode.profile_allowed(wanted):
        raise HTTPException(409, voice_mode.VoiceMode.refusal_message(wanted))
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "缺少文本")
    # Seed the profile here too, not only on the mode route.
    #
    # `ensure_ready` starts a brain from `VOICE.profile`. The mode route seeds it on both its
    # branches, but this route is reachable without the mode route having run in this process at all:
    # `voiceModeReady` is page state, and a gateway restart does not clear it, so a panel that still
    # believes the old process is serving goes straight to a turn. Unseeded, start_brain used to fall
    # back to VOICE_PROFILE - the pre-2026-09-23 default, which is the ternary 27B - and pulled
    # 7.2 GB off disk only to report "27B 三元去审、大脑启动超时" 240 s later, for a turn that wanted
    # the selected model. The same line is why the error named a model nobody had asked for.
    voice_mode.VOICE.profile = wanted
    # A chat turn is text by definition: the transcript is already in hand by the time
    # this route is called, so it needs the language model and never a recogniser.
    if not await voice_mode.VOICE.ensure_ready(need_asr=False):
        raise HTTPException(503, voice_mode.VOICE.last_error or "语音后端未就绪")

    # One history per mode: a spoken turn joins the *voice* thread, so what the model is reminded
    # of here is what was said here (never the typed thread, and never a picture's turns). The
    # caller's own array is used only when nothing has been recorded yet (a first run, or a panel
    # that has not sent an identifier). A shared device's key also carries its own namespace, so a
    # spoken turn cannot be filed into the owner's voice thread by naming its id.
    key = ledger.normalize_id(tenant_conversation(
        request,
        body.conversation_id or conversation_key(request) or f"voice-{uuid.uuid4().hex[:8]}",
        "voice"))
    asked_at = time.time()
    ledger.append_turn(key, "user", text, mode="voice")
    history = ledger.messages(key, limit=8, max_chars=4000)[:-1] or body.history

    if not body.stream:
        try:
            reply, stats = await voice_mode.VOICE.answer(text, history)
        except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
            LOGGER.warning("voice chat failed", exc_info=True)
            raise HTTPException(502, f"语音对话失败：{exc}") from exc
        stats = await _record_voice_turn(key, reply, stats, asked_at, window=ledger_window())
        return JSONResponse({"reply": reply, "stats": stats, "meter": stats["meter"],
                             "conversation_id": key,
                       "profile": voice_mode.VOICE.voice_turn_profile()},
                            headers={"Cache-Control": "no-store"})

    answered = []

    async def events():
        queue: asyncio.Queue = asyncio.Queue()

        async def pump():
            try:
                async for piece in voice_mode.VOICE.answer_stream(text, history):
                    answered.append(piece)
                    await queue.put(("chunk", piece))
                await queue.put(("stats", dict(getattr(voice_mode.VOICE, "last_stream_stats", {}))))
                await queue.put(("done", None))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - surfaced to the caller as an event
                await queue.put(("error", str(exc)))

        task = asyncio.create_task(pump())
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "done":
                    yield "event: done\ndata: {}\n\n"
                    break
                if kind == "error":
                    yield f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    break
                if kind == "stats":
                    stats = await _record_voice_turn(key, "".join(answered), payload, asked_at,
                                                     window=ledger_window())
                    yield ("event: stats\ndata: "
                           + json.dumps(stats, ensure_ascii=False) + "\n\n")
                    continue
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
            # A reply that was cut off mid-sentence is still what the owner heard, so it is
            # filed rather than dropped: the transcript is what the model is reminded of.
            if answered and not any(turn["role"] == "assistant" and turn["at"] >= asked_at
                                    for turn in ledger.history(key, limit=4)):
                await _record_voice_turn(key, "".join(answered), {}, asked_at,
                                         window=ledger_window())

    return StreamingResponse(events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
    })


@app.get("/local/status")
async def status(request: Request):
    """Service status, with hardware detail withheld from shared visitors.

    A guest still needs mode, state and progress to use the UI, but not the GPU
    layer count, thread count, context size or the stored MTP note, which describe
    the owner's machine rather than the service they were invited to use.
    """
    payload = manager.status()
    if not is_admin_session(request):
        payload = {key: value for key, value in payload.items() if key in SHARE_GUEST_FIELDS}
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.get("/local/context/config")
async def context_config():
    # The window the *settings in force* ask for. The resident process may be running a
    # different one (voice), but this endpoint is what the chat app sizes its own compression
    # budget from, and that budget belongs to the thread it is writing, not to the microphone.
    return JSONResponse({
        "enabled": manager.settings.get("context_compression_enabled", True),
        "context_size": expected_context(manager.settings),
        "trigger_ratio": 0.75, "reserve_tokens": 4096,
    }, headers={"Cache-Control": "no-store"})


@app.get("/local/context/status")
async def context_status(conversation_id: str = "", request: Request = None):
    if len(conversation_id) > 512:
        raise HTTPException(400, "Conversation identifier is too long")
    status = context_compactor.get_status(tenant_conversation(request, conversation_id))
    if conversation_id:
        # The compactor's own payload stays exactly as it was (the chat app reads its fields
        # positionally); the conversation's meter rides alongside it —per mode, so the app's
        # number describes the thread this mode is actually using.
        status = {**status,
                  "conversation": ledger.meter(tenant_conversation(request, conversation_id),
                                               mode=mode_scope(),
                                               window=ledger_window())}
    return JSONResponse(status, headers={"Cache-Control": "no-store"})


class ConversationSeedRequest(BaseModel):
    conversation_id: str
    role: str
    content: str
    mode: str = "text"


class ConversationTitleRequest(BaseModel):
    title: str


@app.patch("/local/conversation")
async def conversation_rename(body: ConversationTitleRequest, conversation_id: str = "",
                              scope: str = "", request: Request = None):
    """Name a conversation by hand.

    The history rail gets a name from the owner's first words automatically (cut to ten
    characters); this is the override for when that name is wrong or when the thread is about
    something the opening line did not say. A manual title is final —later turns do not take
    it over —and renaming deliberately does not reorder the rail, because a row that jumps
    while it is being renamed is a row the owner has to find again.

    A shared device may rename its own rows: the rename control is in the panel everyone sees, and
    the key it acts on is derived from the caller (tenant_conversation), so "the owner's history"
    is not a key a visitor can name in the first place.
    """
    require_feature(request, "chat")
    if not conversation_id:
        raise HTTPException(400, "缺少会话标识")
    key = tenant_conversation(request, conversation_id, scope)
    try:
        title = ledger.rename(key, body.title)
    except ValueError as failure:
        raise HTTPException(400, str(failure))
    if title is None:
        raise HTTPException(404, "没有这条对话")
    LOGGER.info("Conversation renamed by the owner: %s → %s", key, title)
    item = next((entry for entry in ledger.conversations() if entry["id"] == key), None)
    return JSONResponse({"conversation_id": key, "title": title,
                         "display_title": (item or {}).get("display_title", title),
                         "renamed": True},
                        headers={"Cache-Control": "no-store"})


@app.post("/local/conversation/seed")
async def conversation_seed(body: ConversationSeedRequest, request: Request):
    """Append one turn to the shared history from outside a chat turn.

    Exists because "show me the history" is a question that needs history to exist, and the
    only other way to make some is to hold a conversation. Owner-only, bounded like any other
    ledger write, and it is also how an imported thread can be brought into the shared one.

    The turn's own ``mode`` decides which thread it joins, so a fixture for the voice rail is
    seeded with ``mode: "voice"`` and lands in the voice scope —the same rule every real turn
    follows.
    """
    require_owner(request)
    if body.role not in ("user", "assistant"):
        raise HTTPException(400, "role 必须是user 、assistant")
    content = (body.content or "").strip()
    if not content:
        raise HTTPException(400, "content 不能为空")
    key = ledger.adopt(scoped_conversation(body.conversation_id, body.mode))
    turn = ledger.append_turn(key, body.role, content[:8000], mode=body.mode)
    return JSONResponse({"conversation_id": key, "turn": turn,
                         "meter": ledger.meter(key, mode=body.mode, window=ledger_window())},
                        headers={"Cache-Control": "no-store"})


@app.delete("/local/conversation/all")
async def conversation_forget_all(request: Request = None):
    """Delete every conversation. Owner-only, and it says how many it removed.

    The recordings live in the browser (IndexedDB), so the panel deletes those too —this route
    is the server half of one action, not the whole of it.
    """
    require_owner(request)
    removed = ledger.forget_all()
    LOGGER.info("Conversations cleared by the owner: %s", removed)
    return JSONResponse({"cleared": removed}, headers={"Cache-Control": "no-store"})


@app.delete("/local/conversation")
async def conversation_forget(conversation_id: str = "", scope: str = "", request: Request = None):
    """Delete one conversation, its turns and its place in the history.

    Deliberately not restricted to empty conversations any more: the owner asked for a delete
    button that removes a thread, and a button that silently refuses a thread with content is
    worse than no button. The panel names the conversation and asks for confirmation first.

    A shared device deletes its own rows by the same rule as renaming: the key comes from
    tenant_conversation, so the id it hands over can only ever name its own thread.
    """
    require_feature(request, "chat")
    if not conversation_id:
        raise HTTPException(400, "缺少会话标识")
    key = tenant_conversation(request, conversation_id, scope)
    removed = ledger.forget(key)
    if removed:
        LOGGER.info("Conversation deleted by the owner: %s", key)
    return JSONResponse({"forgotten": removed}, headers={"Cache-Control": "no-store"})


@app.get("/local/conversation")
async def conversation_detail(conversation_id: str = "", limit: int = 12, window_kind: str = "",
                              scope: str = "", request: Request = None):
    """The history and context meter for one conversation, or the list of them.

    **One history per mode.** The thread is scoped by the mode serving the request (``text·…``,
    ``vision·…``, ``voice·…``), so a written conversation cannot arrive in 识图 and a picture
    cannot land in the typed thread: switching modes does not carry data across. A panel that
    wants its own scope says so (the voice page passes ``scope=voice``), and an id that already
    carries a scope is used as it is, so a row read from a listing can be acted on directly.

    **And one history per caller.** A shared device reads its own threads only: every key and every
    prefix here goes through the tenant helpers, so a visitor's listing is its own rows and an id
    belonging to the owner names nothing (see guest_namespace —this route is where the leak was
    measured from).

    ``window_kind=voice`` measures against the **voice** window (qwen_config.VOICE_CONTEXT_SIZE)
    instead of whichever backend is serving right now. The voice page asks for that on purpose:
    before the first spoken turn the shared backend is still running the text window, so the
    honest answer to "how much room does this conversation have" on that page is the window the
    spoken turn will actually run with —otherwise the page shows 131k for a mode locked to 40k.
    """
    require_feature(request, "chat")
    if len(conversation_id) > 512:
        raise HTTPException(400, "Conversation identifier is too long")
    limit = max(1, min(int(limit or 12), 60))
    window = ledger_window()
    if window_kind == "voice":
        window = voice_context({**manager.settings, "model_profile":
                                active_profile_name(manager.settings)})
    if not conversation_id:
        rows = caller_rows(request, ledger.conversations(scope=tenant_filter(request, scope)))
        return JSONResponse({"conversations": rows,
                             "scope": mode_scope(scope),
                             "context_size": window},
                            headers={"Cache-Control": "no-store"})
    key = ledger.adopt(tenant_conversation(request, conversation_id, scope))
    meter = ledger.meter(key, mode=mode_scope(scope), window=window)
    # Named, so a reader can tell "the window this mode is locked to" from "the window the
    # backend happens to hold": the panel's tooltip says so in words.
    meter["window_kind"] = "voice" if window_kind == "voice" else "serving"
    return JSONResponse({
        "conversation_id": key,
        "meter": meter,
        "history": ledger.history(key, limit=limit, max_chars=8000),
    }, headers={"Cache-Control": "no-store"})


async def await_while_connected(request, operation):
    """Cancel pre-response model work when its browser/client has gone away."""
    async def disconnected():
        # The body has already been consumed. Await the ASGI disconnect event
        # directly; is_disconnected() uses an internal cancel scope that can
        # swallow a simultaneous task cancellation during helper teardown.
        while True:
            message = await request.receive()
            if message["type"] == "http.disconnect":
                return
            await asyncio.sleep(0)

    work = asyncio.create_task(operation)
    monitor = asyncio.create_task(disconnected())
    try:
        completed, _ = await asyncio.wait({work, monitor}, return_when=asyncio.FIRST_COMPLETED)
        if work in completed:
            return await work
        raise HTTPException(499, "请求已取消。")
    finally:
        for task in (work, monitor):
            if not task.done():
                task.cancel()
        await asyncio.gather(work, monitor, return_exceptions=True)


def context_headers(state):
    if not state:
        return {}
    headers = {"x-qwen-context-state": str(state.get("state", "unchanged")),
               "x-qwen-context-cache-hit": "true" if state.get("cache_hit") else "false"}
    for name, key in (("original", "original_tokens"), ("effective", "prompt_tokens"),
                      ("saved", "saved_tokens")):
        value = state.get(key)
        if type(value) is int:
            headers[f"x-qwen-context-{name}-tokens"] = str(value)
    return headers


async def usage_operation(method, *args):
    if usage_store is None:
        raise HTTPException(503, "使用日志暂不可用，请重新启动本地服务或检查日志目录的写入权限。")
    try:
        return await asyncio.to_thread(getattr(usage_store, method), *args)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        LOGGER.exception("Usage log request failed: %s", method)
        raise HTTPException(503, "使用日志暂时无法读取，请稍后重试。聊天功能不受影响。") from exc


@app.get("/local/usage")
async def usage_log(request: Request):
    # Usage history, cost accounting and routing rules describe the owner's own
    # machine, and these three reads previously had no guard at all, so a shared
    # visitor could page through the request log and download it as CSV.
    require_owner(request)
    return JSONResponse(await usage_operation("query", dict(request.query_params)),
                        headers={"Cache-Control": "no-store"})


@app.get("/local/usage/preferences")
async def usage_preferences(request: Request):
    require_owner(request)
    return JSONResponse(await usage_operation("get_preferences"), headers={"Cache-Control": "no-store"})


@app.post("/local/usage/preferences")
async def update_usage_preferences(request: Request):
    require_owner(request)
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise HTTPException(400, "Invalid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON request body must be an object")
    return JSONResponse(await usage_operation("set_preferences", body), headers={"Cache-Control": "no-store"})


@app.get("/local/usage/export")
async def export_usage(request: Request):
    require_owner(request)
    data = await usage_operation("csv_rows", dict(request.query_params))
    return Response(data, media_type="text/csv; charset=utf-8", headers={
        "Cache-Control": "no-store",
        "Content-Disposition": 'attachment; filename="qwen-usage.csv"',
    })


@app.post("/local/routing")
async def record_routing(request: Request):
    require_owner(request)
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise HTTPException(400, "Invalid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON request body must be an object")
    route_id = await usage_operation("record_route", body)
    return JSONResponse({"stored": True, "id": route_id}, status_code=201,
                        headers={"Cache-Control": "no-store"})


@app.get("/local/routing")
async def routing_log(request: Request):
    require_owner(request)
    return JSONResponse(await usage_operation("query_routes", dict(request.query_params)),
                        headers={"Cache-Control": "no-store"})


@app.get("/local/routing/export")
async def export_routing(request: Request):
    require_owner(request)
    data = await usage_operation("csv_route_rows", dict(request.query_params))
    return Response(data, media_type="text/csv; charset=utf-8", headers={
        "Cache-Control": "no-store",
        "Content-Disposition": 'attachment; filename="qwen-routing-log.csv"',
    })


@app.post("/local/mode")
async def change_mode(body: ModeRequest, request: Request):
    require_feature(request, "switch")
    if body.mode not in {"text", "vision", "image", "generate", "video"}:
        raise HTTPException(400, "Mode must be text, vision, generate or video")
    requested_mode = "image" if body.mode == "generate" else body.mode
    async with manager.lock:
        manager.status()
        if manager.state in {"starting", "switching", "stopping"}:
            raise HTTPException(409, "A model transition is already in progress")
        if manager.active_requests:
            raise HTTPException(409, "Wait for the current response to finish")
        if requested_mode == manager.mode and manager.state == "ready":
            return manager.status()
        manager.state = "switching"
        manager.error = None
        manager.recovery_attempts = 0
        manager.retry_at = 0
        # Text and vision always run on the *selected* profile, so moving to them ends any
        # voice borrow: the backend is about to answer chat turns, not the microphone.
        if voice_mode.VOICE.shared_brain_port is not None:
            voice_mode.VOICE.release_brain()
            manager.voice_holds_card = False
        manager.task = asyncio.create_task(manager.load(requested_mode))
    return JSONResponse(manager.status(), status_code=202)


@app.post("/local/model")
async def change_model(body: ModelRequest, request: Request):
    """Switch the text/vision backend between installed model profiles.

    A model change is a mode change that always restarts: the two profiles differ
    in context size, layer count and MTP capability, so there is no slice to swap
    and the command line itself is different. Everything else follows the mode
    path, including going through manager.load so ComfyUI memory is released first.
    """
    require_feature(request, "switch")
    if body.profile not in PROFILES:
        raise HTTPException(
            400, "Unknown model profile. Expected one of: " + ", ".join(sorted(PROFILES)))
    if not profile_installed(body.profile):
        raise HTTPException(409, "该模型尚未下载完成，无法切换。")
    async with manager.lock:
        manager.status()
        if manager.state in {"starting", "switching", "stopping"}:
            raise HTTPException(409, "A model transition is already in progress")
        if manager.active_requests:
            raise HTTPException(409, "Wait for the current response to finish")
        if active_profile_name(manager.settings) == body.profile and manager.state == "ready":
            return manager.status()
        write_model_profile(body.profile)
        # reload from disk so the launch this triggers uses the new profile for its
        # context size, layer count and MTP decision, not the previous model's
        manager.settings = read_settings()
        # A backend lent to voice mode stops being a chat backend the moment another
        # profile is selected: the loan was "the model on the other end *is* the voice
        # model", and that is no longer true. Ending it here means the next voice turn
        # re-evaluates the borrow instead of talking to a process that has moved on.
        if voice_mode.VOICE.shared_brain_port is not None:
            voice_mode.VOICE.release_brain()
        manager.state = "switching"
        manager.error = None
        manager.recovery_attempts = 0
        manager.retry_at = 0
        manager.task = asyncio.create_task(manager.load(manager.mode))
    return JSONResponse(manager.status(), status_code=202)


@app.post("/local/image/generate")
async def generate_image(body: ImageGenerateRequest, request: Request):
    """Queue a Flux.2 Klein job and return immediately with its job id.

    The cycle itself (prompt optimization, model swap, sampling, decode, model
    restore) runs in the background so the browser can follow live progress.

    A job submitted while another one renders is not refused: this machine can only run
    one at a time, but 36-46 s of each cycle is only the language model being put back,
    and a caller tapping "生成" in that window was told 409 and read the view as locked.
    `image_jobs` keeps one waiting slot and starts it when the card frees up.
    """
    # Queueing art shares the owner's GPU and ComfyUI instance, so it stays an
    # owner action even though the same origin also serves invited guests.
    require_feature(request, "media")
    async with manager.lock:
        manager.status()
        if manager.state in {"starting", "switching", "stopping"} or manager.active_requests:
            raise HTTPException(409, "请等待当前模型任务完成")
        try:
            references = await _prepare_reference_names(body)
        except HTTPException:
            raise
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise HTTPException(502, f"参考图处理失败：{exc}") from exc
        # No await between this read and start(), so the verdict still holds when the job is
        # built: only a job that owns the card right now may mark the gateway as switching.
        # A job that is merely waiting must not, or /local/status would claim "switching"
        # until the next mode load and every other route would refuse in the meantime.
        will_wait = image_jobs.busy
        if not will_wait:
            manager.state = "switching"
        manager.error = None
        previous_mode = manager.mode if manager.mode in {"text", "vision"} else "text"
        session = "" if caller_is_owner(request) else caller_token(request)
        if session:
            # A visitor's pictures are written into its own folder: the save prefix is the
            # machine's setting, and a shared caller naming it would let it write anywhere
            # ComfyUI can reach.
            body.filename_prefix = f"guests/{session[:24]}/generated"
        snapshot = image_jobs.start(body, previous_mode, references, session=session)
    return JSONResponse({"job": snapshot}, status_code=202)


class ImageDenoiseRequest(BaseModel):
    # The still to denoise, as a browser data URL: the panel reads the picked file itself,
    # and the module needs neither ComfyUI nor an upload to run.
    image: str
    strength: float | None = None


@app.post("/local/image/denoise")
async def denoise_image(body: ImageDenoiseRequest, request: Request):
    """Denoise a single still —the image side's own module, split off from upscaling.

    ffmpeg on the CPU: seconds per picture, no VRAM, no model and no graph. The panel posts
    the picture it picked as a data URL and gets the denoised file back with the measured
    change, so the module can report what it actually did.
    """
    require_feature(request, "media")
    match = re.match(r"^data:image/(png|jpe?g|webp);base64,(.+)$", body.image or "", re.S)
    if not match:
        raise HTTPException(400, "降噪素材必须是一张 PNG / JPEG / WebP 图片")
    suffix = ".jpg" if match.group(1) in {"jpeg", "jpg"} else f".{match.group(1)}"
    try:
        payload = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(400, f"降噪素材解码失败：{exc}") from exc
    folder = image_comfy.COMFY_ROOT / "ComfyUI" / "output" / "denoised"
    folder.mkdir(parents=True, exist_ok=True)
    staged = folder / f"qwen-image-{uuid.uuid4().hex}{suffix}"
    staged.write_bytes(payload)
    strength = body.strength if body.strength is not None else 2.0
    try:
        result = await asyncio.to_thread(video_denoise.run_image, staged, strength)
    except video_denoise.DenoiseError as error:
        LOGGER.warning("image denoise rejected: %s", error)
        raise HTTPException(409, str(error)) from error
    produced = Path(result["target"])
    LOGGER.info("image denoise done: %s → %s", staged.name, produced.name)
    return JSONResponse({**result, "note": video_denoise.describe(result),
                         "image": comfy_image_url({"filename": produced.name,
                                                   "subfolder": folder.name,
                                                   "type": "output"})})


async def _prepare_reference_names(body: ImageGenerateRequest) -> list[str]:
    """Accept ComfyUI input filenames and browser data URLs, never both shapes raw."""
    from app.image.image_jobs import _prepare_reference_inputs, _safe_reference_names

    values = [*(body.reference_images or []), body.image_1, body.image_2]
    if any(isinstance(value, str) and value.startswith("data:image/") for value in values if value):
        # Uploading a reference needs ComfyUI's /upload/image, so the server has to
        # be up before the job starts; otherwise image-to-image fails with a bare
        # connection error while ComfyUI is simply idle and unstarted.
        try:
            await image_jobs.supervisor.ensure_running(timeout=image_jobs.supervisor.start_timeout)
        except (RuntimeError, OSError) as exc:
            raise HTTPException(503, f"需要先启动 ComfyUI 才能上传参考图：{exc}") from exc
        return await _prepare_reference_inputs(manager.client, body)
    return await _safe_reference_names(body)


def image_job_for_caller(job: dict | None, request: Request) -> dict | None:
    """One image job's payload, scoped to the caller.

    One image monitor serves the whole machine, so a shared device following *its* picture would
    otherwise read whatever the owner last typed —and read back the owner's results. The job is
    shown (a running render is not a secret, and the progress bar is the point), but another
    caller's words and files are not: the prompt becomes a placeholder and the produced file list
    is emptied.
    """
    if job is None or caller_is_owner(request):
        return job
    if job.get("session") and job["session"] == caller_token(request):
        return job
    redacted = dict(job)
    if redacted.get("prompt"):
        redacted["prompt"] = "（其他设备提交的任务）"
    redacted["negative_prompt"] = ""
    redacted["final_prompt"] = None
    redacted["images"] = []
    redacted["preview"] = None
    # Zeroed with it: the version drives whether the panel asks for the picture at all, and
    # another caller's picture must not be kept on screen by a version it already "has".
    redacted["preview_version"] = 0
    return redacted


@app.get("/local/image/progress")
async def image_progress(since: int = 0, preview: int = -1, request: Request = None):
    """Live progress for the running job plus the persisted history.

    ``preview`` is the sampler-preview version the caller already holds:

    * ``preview=-1`` (the default) — the caller does not want pictures at all. The HUD and any
      plain poller land here, and the answer stays ~1.2 KB.
    * ``preview=n`` — send the picture only when the job's version is not ``n``. The panel sends
      the version it is already showing, so an unchanged frame is never re-sent.

    Measured on the machine: the response was 85,745 bytes a poll with the preview (79,157 of it
    the base64 frame), and the panel polls once or twice a second — 6-8 MB for one generated
    image, on a visitor's phone over a tunnel.
    """
    if image_jobs is None:
        raise HTTPException(503, "生图服务正在初始化")
    payload = image_jobs.progress()
    job = image_job_for_caller(payload["job"], request)
    waiting = image_jobs.pending_for(caller_token(request), caller_is_owner(request))
    if waiting is not None:
        # This caller has a job of its own in the waiting slot, so that is what its panel is
        # waiting to see 、not the job that is holding the card right now (whose words and
        # files are not this caller's to read anyway).
        job = waiting
    if job is not None and job.get("preview") is not None:
        wanted = int(preview)
        if wanted < 0 or wanted == int(job.get("preview_version") or 0):
            job = {**job, "preview": None}
    events = job.get("events", []) if job else []
    return JSONResponse({
        "job": job,
        "busy": payload["busy"],
        "since": since,
        "events": events[since:] if since < len(events) else [],
        "event_count": len(events),
        "model": payload["model"],
    })


@app.get("/local/image/file")
async def image_file(filename: str = "", subfolder: str = "", type: str = "output",
                     request: Request = None):
    """Serve one produced or uploaded file, fenced to the caller.

    ComfyUI's own ``/view`` cannot do this job for a shared session: the URL would point at
    ``127.0.0.1:8188``, which on a visitor's machine is the visitor's own computer. So the
    gateway serves the bytes, and applies the same two-half check the video side uses —the file
    must live under a root this caller may read from, **and** (for a visitor) inside its own
    folder or be a file it uploaded.

    Owner: ComfyUI's whole output and input trees. Visitor: ``output/guests/<session>/`` plus the
    files recorded against its session, which is where its own generations and uploads live.
    """
    require_any_feature(request, "media", "storage")
    name = Path(str(filename or "")).name
    if not name or name != str(filename or ""):
        raise HTTPException(400, "文件名不合法")
    kind = str(type or "output").lower()
    roots = [COMFY_OUTPUT.resolve(), COMFY_INPUT.resolve()]
    root = COMFY_INPUT.resolve() if kind == "input" else COMFY_OUTPUT.resolve()
    sub = str(subfolder or "").strip().strip("/\\")
    target = (root / sub / name).resolve() if sub else (root / name).resolve()
    if not any(target.is_relative_to(candidate) for candidate in roots):
        raise HTTPException(403, "路径不在允许的目录里")
    owner = caller_is_owner(request)
    token = caller_token(request)
    if not owner:
        # Same two halves as the video side: the visitor's own output folder, or a file recorded
        # against its session (its uploads). Either one is enough —a generated picture carries
        # no record, and an upload is not inside the output folder.
        guest_dir = (COMFY_OUTPUT / "guests" / (token or "anonymous")[:24]).resolve()
        if not (target.is_relative_to(guest_dir)
                or guest_uploads.store().owned_by(target.name, token)):
            # 404 rather than 403: the response must not become a way to probe which file names
            # exist on the machine.
            raise HTTPException(404, "找不到这个文件")
    if not target.is_file():
        raise HTTPException(404, "找不到这个文件")
    return FileResponse(target, headers={"Cache-Control": "no-store"})


@app.get("/local/image/history")
async def image_history(request: Request = None):
    """This caller's own generation records.

    The history used to be the whole monitor's, which showed every device the owner's prompts and
    pictures. A shared device now sees the entries it submitted.
    """
    if caller_is_owner(request):
        return JSONResponse({"entries": image_jobs.history})
    token = caller_token(request)
    mine = [image_job_for_caller(entry, request) for entry in image_jobs.history
            if str(entry.get("session") or "") == token]
    return JSONResponse({"entries": mine})


@app.delete("/local/image/history")
async def clear_image_history(request: Request):
    require_owner(request)
    removed = await image_jobs.clear_history()
    return JSONResponse({"removed": removed, "entries": []})


@app.delete("/local/image/history/{job_id}")
async def delete_image_history(job_id: str, request: Request):
    require_owner(request)
    if not await image_jobs.remove_history(job_id):
        raise HTTPException(404, "未找到该生成记录")
    return JSONResponse({"removed": job_id})


@app.post("/local/image/cancel")
async def cancel_image(request: Request):
    require_feature(request, "media")
    # One monitor serves the whole machine, so the permission to generate is not a permission to
    # stop somebody else's job: a visitor may only cancel the job it submitted (the owner's jobs
    # carry no session, so a visitor can never match one).
    if image_jobs is not None and image_jobs.busy and not caller_is_owner(request):
        session = str((image_jobs.snapshot() or {}).get("session") or "")
        if session != caller_token(request):
            raise HTTPException(403, "这不是你的任务：机主正在跑的那一个只有机主能取消。")
    snapshot = await image_jobs.cancel()
    return JSONResponse({"job": snapshot})


@app.get("/local/image/meta")
async def image_meta():
    return JSONResponse(await image_metadata(manager.client, image_jobs))


@app.get("/local/image/gpu")
async def image_gpu(processes: int = 0, request: Request = None):
    """VRAM totals plus this job's footprint; per-process detail is opt-in.

    The per-process list is the machine's own layout —which programs are running on this card —
    and ``/local/status`` deliberately hides that class of detail from a shared visitor. The same
    rule applies here: a visitor gets the totals it needs to judge whether a render fits, not the
    list of what else is on the card.
    """
    snapshot = await image_jobs.gpu_snapshot()
    payload = {"gpu": snapshot, "available": snapshot is not None}
    if processes and caller_is_owner(request):
        payload["processes"] = await image_jobs.gpu.processes()
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.get("/local/image/workflow")
async def image_workflow(pipeline: str | None = None, width: int | None = None, height: int | None = None,
                         steps: int | None = None, cfg: float | None = None, denoise: float | None = None,
                         seed: int | None = None, negative_prompt: str | None = None):
    """The editable graph plus the values that would run with these parameters."""
    params = {"width": width, "height": height, "steps": steps, "cfg": cfg,
              "denoise": denoise, "seed": seed, "negative_prompt": negative_prompt}
    return JSONResponse(image_jobs.workflow(params, pipeline), headers={"Cache-Control": "no-store"})


@app.put("/local/image/workflow")
async def save_image_workflow(request: Request):
    """Persist per-node edits for one pipeline; they are merged into later runs."""
    require_feature(request, "media")
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是对象")
    pipeline_key = image_pipelines.pipeline(body.get("pipeline")).key
    overrides = body.get("overrides")
    if overrides is not None and not isinstance(overrides, dict):
        raise HTTPException(400, "overrides 必须是节点到参数的映射")
    try:
        clean = image_comfy.validate_overrides(pipeline_key, overrides or {})
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    image_comfy.save_pipeline_overrides(pipeline_key, clean)
    params = body.get("params") or {}
    return JSONResponse({
        "pipeline": pipeline_key,
        "overrides": clean,
        "workflow": image_jobs.workflow({**params, "prompt": ""}, pipeline_key),
    })


@app.delete("/local/image/workflow")
async def reset_image_workflow(request: Request, pipeline: str | None = None):
    require_owner(request)
    pipeline_key = image_pipelines.pipeline(pipeline).key
    image_comfy.save_pipeline_overrides(pipeline_key, {})
    return JSONResponse({
        "pipeline": pipeline_key,
        "overrides": {},
        "workflow": image_jobs.workflow({"prompt": ""}, pipeline_key),
    })


@app.post("/local/image/pipeline")
async def select_image_pipeline(request: Request):
    """Choose the pipeline later generations use (Flux.2 / Chroma / SDXL)."""
    require_feature(request, "media")
    body = await request.json()
    if not isinstance(body, dict) or not body.get("pipeline"):
        raise HTTPException(400, "缺少 pipeline")
    key = str(body["pipeline"])
    if key not in image_pipelines.PIPELINES:
        raise HTTPException(404, f"未知的管线：{key}")
    if image_jobs.busy:
        raise HTTPException(409, "生图任务进行中，请等待完成或先取消。")
    image_jobs.pipeline = key
    return JSONResponse({"pipeline": key, "model": image_jobs.model_info()})


@app.get("/local/image/workflows")
async def image_workflows():
    """One-click recipes with a plain-language description of each."""
    return JSONResponse({"workflows": image_jobs.workflows(), "current": image_jobs.pipeline},
                        headers={"Cache-Control": "no-store"})


@app.post("/local/image/workflows")
async def apply_image_workflow(request: Request):
    """Switch pipeline, LoRAs, steps, CFG and denoise with one click."""
    require_feature(request, "media")
    body = await request.json()
    if not isinstance(body, dict) or not body.get("key"):
        raise HTTPException(400, "缺少 key")
    if image_jobs.busy:
        raise HTTPException(409, "生图任务进行中，请等待完成或先取消。")
    async with manager.lock:
        if manager.active_requests:
            raise HTTPException(409, "请等待当前模型任务完成")
        payload = image_jobs.apply_workflow(str(body["key"]))
    return JSONResponse({**payload, "workflow_payload": image_jobs.workflow({"prompt": ""}, payload["pipeline"])},
                        headers={"Cache-Control": "no-store"})


@app.post("/local/image/presets")
async def apply_image_preset(request: Request):
    """Apply one-click LoRA presets (accelerators and quality add-ons)."""
    require_feature(request, "media")
    body = await request.json()
    if not isinstance(body, dict) or not body.get("key"):
        raise HTTPException(400, "缺少 key")
    if image_jobs.busy:
        raise HTTPException(409, "生图任务进行中，请等待完成或先取消。")
    pipeline_key = _known_pipeline(body.get("pipeline"))
    return JSONResponse(image_jobs.apply_preset(pipeline_key, str(body["key"])),
                        headers={"Cache-Control": "no-store"})


@app.delete("/local/image/presets")
async def clear_image_preset(request: Request, pipeline: str | None = None, key: str | None = None):
    require_owner(request)
    if image_jobs.busy:
        raise HTTPException(409, "生图任务进行中，请等待完成或先取消。")
    pipeline_key = _known_pipeline(pipeline)
    return JSONResponse(image_jobs.clear_presets(pipeline_key, key),
                        headers={"Cache-Control": "no-store"})


def _known_pipeline(value: str | None) -> str:
    """Resolve a pipeline key, refusing unknown ones instead of falling back."""
    if not value:
        return image_jobs.pipeline
    if value not in image_pipelines.PIPELINES:
        raise HTTPException(404, f"未知的管线：{value}")
    return value


@app.get("/local/image/pipelines")
async def image_pipelines_list():
    return JSONResponse({"pipelines": image_pipelines.describe_all(), "current": image_jobs.pipeline})

@app.post("/local/recover")
async def recover(request: Request):
    require_owner(request)
    changed = await manager.recover(manual=True)
    return JSONResponse(manager.status(), status_code=202 if changed else 200)


# 最近多久内有访客活动算"还在、——只用于状态展示；收工判定不看它（所有者规定的两位口径）。
GUEST_RECENT_SECONDS = 120


def task_running_label() -> str:
    """**任务位**：页面上有没有任务在跑——有就返回它的名字，没有返回空串。

    所有者规定的口径是有任务在跑就为1，任务结束自动转 0"，所以这里每次现读、一个都不缓存：
    对话请求、模型加载/切换、生图（含排队）、视频、补帧。收工只发生在它返回空串的时候；
    报出的名字会进日志与 `/local/lifecycle/status`，事后能对账。
    """
    if manager.active_requests:
        return f"对话请求 {manager.active_requests} 个进行中"
    if manager.state in {"starting", "switching", "stopping"}:
        return f"模型状态{manager.state}"
    if image_jobs is not None and (image_jobs.busy or image_jobs.pending):
        return "生图任务在跑或排队"
    monitor = video_routes.monitor()
    if monitor is not None and monitor.busy:
        return "视频任务在跑"
    interpolate = video_routes.interpolate_monitor()
    if interpolate is not None and interpolate.busy:
        return "补帧任务在跑"
    return ""


def recent_guest_seconds() -> float | None:
    """最近有访客活动就返回距今多少秒"

    只作为*状态信息*出现、`/local/lifecycle/status` 里：所有者规定的收工口径只看
    "有没有页面"有没有任务"两位，访客在不在线不再参与判定（一个访客开着页面不动、
    也没跑任何任务时，后台会按口径收工）。
    """
    now = time.time()
    newest = None
    for token, session in list(getattr(share_store, "sessions", {}).items()):
        if not isinstance(session, dict) or session.get("kind") == "admin":
            continue
        if session.get("revoked"):
            continue
        seen = float(session.get("last_seen") or 0.0)
        age = now - seen
        if 0 <= age <= GUEST_RECENT_SECONDS and (newest is None or age < newest):
            newest = age
    return None if newest is None else round(newest, 1)


async def shutdown_after_page_close() -> dict:
    """页面关闭且确认空闲：ComfyUI →隧道 、服务本身，逐项记账后退出。

    服务本身走的是和"关机"按钮同一条路（写 `service.stop` 再让 uvicorn 退出），
    所以监督进程不会把模型重新拉起来。
    """
    outcome = {"comfy": None, "tunnel": None, "service": "exiting"}
    try:
        outcome["comfy"] = (await asyncio.to_thread(image_jobs.supervisor.stop)
                            if image_jobs is not None else "no-monitor")
    except Exception as exc:  # noqa: BLE001 - 收尾失败不能让退出本身失。
        outcome["comfy"] = f"failed: {exc}"
        LOGGER.warning("page-close: stopping ComfyUI failed: %s", exc)
    try:
        outcome["tunnel"] = await asyncio.to_thread(stop_tunnel)
    except Exception as exc:  # noqa: BLE001
        outcome["tunnel"] = f"failed: {exc}"
        LOGGER.warning("page-close: stopping the tunnel failed: %s", exc)
    # 先留下停止标记，再让 uvicorn 退出：顺序反了的话监督进程会立刻把它拉回来。
    (ROOT / "service.stop").write_text(datetime.now().isoformat(), encoding="ascii")
    server.should_exit = True
    return outcome


page_watch = PageLifecycle(
    task_check=task_running_label,
    stopper=shutdown_after_page_close,
    enabled=lambda: bool(manager.settings.get("auto_stop_on_page_close", True)),
)


@app.post("/local/lifecycle/page-alive")
async def page_alive(request: Request):
    """页面的心跳（开页面时立刻一次，之后每20 秒一次）：*页面、、1**。

    只有这条路由能把页面位顶起来。曾经写成"任何请求都算页面在用"，但监督进程每10 秒探一次
    网关，那样页面位永远是 1、永远不会收工；任务那一侧由 `task_running_label()` 负责。
    """
    require_owner(request)
    return JSONResponse(page_watch.note_page_alive())


@app.post("/local/lifecycle/page-closed")
async def page_closed(request: Request):
    """页面发了"我关了"*页面位立刻 0**（不用等心跳超时，详见page_lifecycle.py）。

    只有机主的页面算数：访客关自己的标签页不该把机主的服务停掉，所以这里要求机主身份。
    信标用 `sendBeacon` 发出，浏览器不关心响应，202 只是给手工调用一个明确的答复。
    页面位变 0 之后，只要任务位也是 0，后台就自己收工；任务位是1 就一直等它跑完。
    """
    require_owner(request)
    return JSONResponse(page_watch.note_page_closed(), status_code=202)


@app.get("/local/lifecycle/status")
async def lifecycle_status(request: Request):
    """两个标识位+ 诊断信息，便于手工核对（`page`/`task`/还差多少秒算没页面）。

    只读：看状态**不**改变任何一位，否则"看一眼就把页面位顶上去了。
    """
    require_owner(request)
    return JSONResponse({**page_watch.flags(), "guests_recent_seconds": recent_guest_seconds()})


@app.post("/local/shutdown")
async def shutdown(request: Request):
    require_owner(request)
    async with manager.lock:
        if manager.active_requests:
            raise HTTPException(409, "Wait for the current response to finish")
        manager.state = "stopping"
        # If a supervisor owns this gateway, keep it from respawning the manager
        # after an intentional shutdown. The launcher removes this marker on start.
        (ROOT / "service.stop").write_text(datetime.now().isoformat(), encoding="ascii")
        server.should_exit = True
    return {"status": "stopping"}


@app.get("/local/{asset}")
async def local_asset(asset: str):
    if asset not in {"mode-control.js", "mode-control.css", "image-panel.js", "image-panel.css",
                     "page-lifecycle.js",
                     "video-panel.js", "video-panel.css",
                     "usage-log.js", "usage-log.css", "context-control.js", "context-control.css",
                     "share-manager.js", "share-manager.css",
                     "task-hud.js", "task-hud.css",
                     "voice-input.js", "voice-input.css",
                     "voice-panel.js", "voice-panel.css",
                     "thinking-control.js", "thinking-control.css"}:
        raise HTTPException(404)
    # These files change with every deployment while the URL stays the same, so
    # the browser must revalidate instead of holding a stale copy.
    return FileResponse(ROOT / "web" / asset, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })


@app.get("/sw.js")
async def service_worker():
    return FileResponse(ROOT / "web/sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


def apply_thinking_level(payload, level, settings):
    """Translate the panel's thinking tier into the arguments the chat template takes.

    Returns ``(payload, applied)``: ``applied`` is the tier that was written, or "" when the
    request was left alone. A tier outside the profile's whitelist is deliberately ignored
    rather than forwarded — both 27B templates raise on an unknown `reasoning_effort`, which
    would turn a stale page's header into a 500 for the whole turn. The 9B has no effort knob,
    so its "on" clears any effort instead of inventing one.
    """
    allowed = thinking_levels(settings)
    if not level:
        return payload, ""
    if level not in allowed:
        LOGGER.warning("Unknown thinking level %r (allowed: %s); request left alone",
                       level, ", ".join(allowed))
        return payload, ""
    kwargs = dict(payload.get("chat_template_kwargs") or {})
    if level == "off":
        kwargs["enable_thinking"] = False
        kwargs.pop("reasoning_effort", None)
    else:
        kwargs["enable_thinking"] = True
        if level == "on":
            kwargs.pop("reasoning_effort", None)
        else:
            kwargs["reasoning_effort"] = level
    return {**payload, "chat_template_kwargs": kwargs}, level


def admission_kind(kind, mode):
    """Which GPU threshold a request should be judged against.

    In the unified backend the language weights and the vision projection live in the *same*
    process, so a vision turn costs no new VRAM and swaps no weights. Judging it by the
    "needs a model swap" threshold (2048 MiB) refused every vision turn while any model was
    resident - measured live: 9B at 131072 left 314 MiB free, and an image question came back
    409 with "显卡可用显存不足（314 MiB < 2048 MiB）". Image/video jobs and profile switches do
    allocate, so those keep the strict threshold.
    """
    if kind == "vision" and mode == "vision":
        return "text"
    return kind


def has_media(value):
    """Inspect generation content, never tool definitions or output JSON schemas.

    In agent requests, a schema property named ``type`` may itself be a dict or
    list. Arbitrarily traversing all values both crashes on those schemas and
    mistakes their examples for actual image input.
    """
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            kind = item.get("type")
            if isinstance(kind, str) and kind in {
                "image_url", "input_image", "image", "video", "video_url", "input_video",
            }:
                return True
            if item.get("image_data") or item.get("multimodal_data"):
                return True
            pending.extend(item[key] for key in ("messages", "content", "input", "prompt") if key in item)
        elif isinstance(item, list):
            pending.extend(item)
    return False


def loading_page():
    return HTMLResponse(
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'{INJECTION}<title>Qwen</title></head><body>'
        '<main style="padding:100px 24px;font:16px system-ui">Qwen 模型加载中...</main>'
        '<script>async function retry(){try{const s=await(await fetch("/local/status")).json();'
        'if(s.state==="ready"){location.reload();return}'
        'if(s.state==="error"){document.querySelector("main").textContent=s.error||"Qwen 模型暂不可用，正在恢复;}'
        '}catch(e){}setTimeout(retry,3000)}setTimeout(retry,3000)</script></body></html>',
        headers={"Cache-Control": "no-store"},
    )


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def proxy(path: str, request: Request):
    is_work = request.method not in {"GET", "HEAD", "OPTIONS"}
    payload = None
    if request.method in {"GET", "HEAD"}:
        if path in METADATA_PATHS and path in manager.metadata:
            return JSONResponse(manager.metadata[path], headers={"Cache-Control": "no-store"})
        native_root = ROOT / "web/native"
        cached = (native_root / (path or "index.html")).resolve()
        if cached.is_relative_to(native_root.resolve()) and cached.is_file():
            if cached.suffix == ".html":
                html = cached.read_text(encoding="utf-8").replace("</head>", INJECTION + "</head>", 1)
                manager.html = html
                return HTMLResponse(html, headers={"Cache-Control": "no-store"})
            return FileResponse(cached)
    body = await request.body()
    reservation = None
    if is_work:
        require_local_origin(request)
        async with manager.lock:
            if manager.status()["state"] != "ready":
                return JSONResponse({"error": {"message": "模型正在切换，请稍后重试。"}}, status_code=503)
            request_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
            if request_type == "application/json" or request_type.endswith("+json"):
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    raise HTTPException(400, "Invalid JSON")
                if not isinstance(payload, dict):
                    raise HTTPException(400, "JSON request body must be an object")
                if manager.mode == "text" and has_media(payload):
                    return JSONResponse(
                        {"error": {"message": "当前为纯文本模式，请切换到识图模式后发送图片。"}}, status_code=400,
                    )
            # The GPU decision comes before the request is accounted or proxied: a
            # refused turn must not look like work that is already running.
            kind = admission_kind(gpu_arbiter.classify(path, request.method, payload), manager.mode)
            if kind is not None:
                session_token, owner, label = gpu_caller(request)
                wanted = request.query_params.get(GPU_PREEMPT_PARAM, "").strip().lower()
                if wanted and wanted != GPU_PREEMPT_VALUE:
                    # Strict on purpose: it makes "did the console actually ask to
                    # preempt" observable from the server side, not just from the reply.
                    raise HTTPException(400, f"{GPU_PREEMPT_PARAM} 只接受{GPU_PREEMPT_VALUE}")
                preempt = wanted == GPU_PREEMPT_VALUE
                if preempt and not owner:
                    raise HTTPException(403, "只有本机管理员可以抢占显卡")
                reservation = await gpu.reserve(session_token, owner, label, kind, path)
                admitted = await gpu.admit(reservation, session_token, owner, label, kind, path,
                                           preempt=preempt)
                if admitted.get("decision") != "allow":
                    if admitted.get("decision") == "ask":
                        LOGGER.info("GPU: owner asked before taking the card at %s", path)
                        return JSONResponse(
                            {"error": {"message": admitted["message"]}, "gpu": admitted},
                            status_code=409,
                        )
                    LOGGER.info("GPU: refused %s for %s (%s MiB free)", path, label,
                                (admitted.get("vram") or {}).get("free_mib"))
                    return JSONResponse(
                        {"error": {"message": admitted["message"]}, "gpu": admitted},
                        status_code=503,
                        headers={"Retry-After": str(admitted.get("retry_after") or 30)},
                    )
                reservation = admitted
            gpu_session = session_token if reservation is not None else ""
            manager.active_requests += 1
    response = None
    cleaned = False
    compression_state = None
    stream_ready = None
    # Scoped by the mode serving this request: the native UI's resume key, the compressor's
    # summary and the history all belong to *this* mode's thread (see mode_scope) —and, for a
    # shared device, to that device's own namespace (see guest_namespace).
    #
    # Resolved here, at delivery, and then handed to the accounting middleware through the scope:
    # that middleware writes the ledger *after* the response, and reading the mode again there
    # reads it one switch later. Measured live: a text turn whose write landed next to a switch to
    # 识图 was filed into the vision line, the prompt filter then counted that text turn as the
    # vision line's own, and the answer containing the passphrase went into the vision prompt.
    request_mode = mode_scope()
    conversation_id = tenant_conversation(request, request.headers.get("x-conversation-id", ""),
                                          request_mode)
    request.scope["qwen.mode"] = request_mode
    request.scope["qwen.conversation"] = conversation_id

    async def cleanup():
        nonlocal cleaned
        if cleaned:
            return
        cleaned = True
        try:
            if response is not None:
                with anyio.CancelScope(shield=True):
                    await response.aclose()
        finally:
            if stream_ready is not None:
                stream_ready.set()
                if pending_context_streams.get(conversation_id) is stream_ready:
                    pending_context_streams.pop(conversation_id, None)
            if is_work:
                manager.active_requests -= 1
                if gpu_session:
                    gpu.release(gpu_session)

    try:
        if request.method == "GET" and path == "v1/stream":
            pending = pending_context_streams.get(tenant_conversation(
                request, request.query_params.get("conv_id", "")))
            if pending is not None:
                await await_while_connected(request, pending.wait())
        if request.method == "POST" and path.rstrip("/") in {"v1/chat/completions", "chat/completions"} and isinstance(payload, dict):
            if len(conversation_id) > 512:
                raise HTTPException(400, "Conversation identifier is too long")
            prefill = any(type(payload.get(key)) is int and payload[key] == 0
                          for key in ("n_predict", "max_tokens", "max_completion_tokens"))
            if not prefill:
                if conversation_id and payload.get("stream"):
                    stream_ready = asyncio.Event()
                    pending_context_streams[conversation_id] = stream_ready
                # Before compressing: drop the turns that belong to another mode, so a typed
                # thread cannot ride into the vision prompt (mode_isolation explains the rule).
                #
                # Order matters and was wrong: the current question must be remembered *after* the
                # filter has decided, not before. Remembering it first put it in the known set, so
                # an *earlier* message with the same text —the typed turn the owner had just
                # asked —counted as "this mode's", and the answer that followed it came along.
                # Measured live: the same question asked in 纯文本and then in 识图 answered with
                # the passphrase that only the typed thread had been given.
                isolated, dropped = mode_isolation.isolate(payload, scope_user_texts(conversation_id))
                remember_user_text(conversation_id, payload)
                if dropped:
                    payload = isolated
                    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    LOGGER.info("Mode isolation (%s): dropped %s message(s) from another mode",
                                conversation_id, dropped)
                # 会动手的回合先给一、这台机器上怎么干活"的规程（来由、agent_briefing）。
                # 放在压缩之前：压缩要按真实的 system 消息算占用，塞在它后面就会被漏算。
                # 只按"这一轮带不带工具"判断，不按界面档位：带工具就是要在这台机器上动手。
                # 无论当时停在纯文本还是识图——规程说的都是命令行与失败处置，两边都成立。
                briefed, briefed_chars = agent_briefing.apply(payload, window=ledger_window())
                if briefed_chars:
                    payload = briefed
                    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    request.scope["qwen.briefing"] = briefed_chars
                    LOGGER.info("Agent briefing (%s): 应用没有 system 消息，已注入 %s 字环境/工具规程",
                                conversation_id or "-", briefed_chars)
                setting = request.headers.get("x-qwen-context-compression", "auto").lower()
                # 思考档位：面板在对话框里选的档位随请求头过来，这里翻译成模板认识的参数。
                # 只发白名单里的值—、7B 的模板对非法 effort 直接 raise（变、500），所。
                # "看不懂就不动这个请求"、尽力翻译"安全、B 不认 effort，自动退化成开关。
                level = (request.headers.get("x-qwen-thinking") or "").strip().lower()
                if level:
                    payload, applied = apply_thinking_level(payload, level, manager.settings)
                    if applied:
                        body = json.dumps(payload, ensure_ascii=False,
                                          separators=(",", ":")).encode("utf-8")
                        request.scope["qwen.thinking"] = applied
                # 循环防护：打转时打断一次并要求它换一条可行方案继续（详见 loop_guard.py）。
                # 、1 、*不摘工具**（摘了就只剩解释、没法继续），复发才逐级收紧。
                # 放在压缩之前：压缩会把工具调用折进摘要，先判才能看见"在打、的证据。
                verdict = loop_guard.inspect(payload.get("messages") or [])
                if verdict["loop"]:
                    payload, _note = loop_guard.break_loop(payload, verdict)
                    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    request.scope["qwen.loop_guard"] = verdict
                    LOGGER.warning("Loop guard (%s, 、%s 、: %s 已拦下%s 、、%s",
                                   verdict["reason"], verdict["level"], verdict["tool"],
                                   verdict["count"], _loop_guard_action(verdict))
                # 、*当前正在服务的后端窗、*（`ledger_window()` = 启动时的窗口，含 9B 、131072。
                # 语音借用）40960、7B 自己的窗口）、*不是** settings.json 里的原始键。
                # 真机踩到：这里原来传 `manager.settings["context_size"]`、 8192），压缩器就。
                # 8192 、摘要窗口"判定，于、4.3k 的对话也、装不、（报"单个较早回合超过摘要
                # 窗口"），、`summary_tokens = min(1536, max(256, 8192//32))` 正好= 256，摘要被截断
                # 、未完整结束，未替换原消息"。改成真实窗口后、B 、27B 都按自己的窗口自适应。
                prepared, compression_state = await await_while_connected(request, context_compactor.prepare(
                    payload, manager.client, ledger_window(),
                    scope=conversation_id, mode=manager.mode, force=setting == "force",
                    enabled=setting != "off" and manager.settings.get("context_compression_enabled", True),
                ))
                if prepared is not payload:
                    body = json.dumps(prepared, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                # 上下文护栏：一个回合自己长到超过窗口时压缩器没东西可压（它只压较早的回合）。
                # 上游只会回一句英、400；界面是流式、等的是 SSE，于是表现为"卡住"（见 context_guard）。
                if len(body) >= context_guard.COUNT_ABOVE_BYTES:
                    counted = await await_while_connected(
                        request, context_compactor.count_prompt(payload, manager.client))
                    if context_guard.overflows(counted, ledger_window()):
                        text = context_guard.message(counted, ledger_window())
                        LOGGER.warning("Context guard (%s): 本轮请求 %s token 超过窗口 %s，未发上游",
                                       conversation_id or "-", counted, ledger_window())
                        await cleanup()
                        headers = {"x-qwen-context-state": "overflow"}
                        if payload.get("stream"):
                            return Response(context_guard.reply_frame(text), status_code=200,
                                            media_type="text/event-stream", headers=headers)
                        return JSONResponse(context_guard.json_error(text), status_code=400,
                                            headers=headers)
        headers = {key: value for key, value in request.headers.items() if key.lower() not in HOP_HEADERS}
        headers.pop("x-qwen-context-compression", None)
        headers.pop("x-qwen-thinking", None)
        headers["accept-encoding"] = "identity"
        url = f"{UPSTREAM}/{path}"
        if request.url.query:
            url += "?" + request.url.query
        request_options = {}
        if request.method == "POST" and f"/{path.rstrip('/')}" in GENERATION_PATHS:
            # Allow long context prefill while keeping a finite bound on a
            # stalled backend. Connect/write/pool limits remain independent.
            request_options["timeout"] = httpx.Timeout(
                600, connect=5, read=manager.settings.get("server_timeout_seconds", 3600),
            )
        upstream_request = manager.client.build_request(
            request.method, url, headers=headers, content=body, **request_options,
        )
        response = await await_while_connected(request, manager.client.send(upstream_request, stream=True))
        if stream_ready is not None:
            stream_ready.set()
        response_headers = {key: value for key, value in response.headers.items() if key.lower() not in HOP_HEADERS}
        response_headers.update(context_headers(compression_state))
        verdict = request.scope.get("qwen.loop_guard")
        if verdict:
            # 头部只能、ASCII，所以这里只放工具名与原因码（原因语句在日志里）。
            response_headers["x-qwen-loop-guard"] = f"{verdict['tool']}/{verdict['count']}"
            response_headers["x-qwen-loop-guard-reason"] = str(verdict["reason"])
            response_headers["x-qwen-loop-guard-level"] = str(verdict.get("level", 1))
        if request.scope.get("qwen.briefing"):
            # 只在真的注入了才带这个头：面、自检脚本据此能看、这一轮动手前有没有规、。
            response_headers["x-qwen-agent-briefing"] = str(request.scope["qwen.briefing"])
        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type:
            raw_html = (await response.aread()).decode("utf-8")
            await cleanup()
            raw_html = raw_html.replace("</head>", INJECTION + "</head>", 1)
            manager.html = raw_html
            response_headers["cache-control"] = "no-store"
            response_headers.pop("etag", None)
            return HTMLResponse(raw_html, status_code=response.status_code, headers=response_headers)

        if "application/json" in content_type or "+json" in content_type:
            # Read non-streaming JSON before sending headers, so transport failures
            # produce a real 503 instead of a truncated 200 response.
            raw_body = await response.aread()
            await cleanup()
            return Response(raw_body, status_code=response.status_code, headers=response_headers)

        async def chunks():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            except (httpx.HTTPError, OSError) as exc:
                LOGGER.warning("Upstream stream interrupted: %s %s: %s", request.method, path, exc)
                manager.next_health_check = 0
                if "text/event-stream" not in content_type:
                    raise
                # Headers have already been sent; communicate failure using the
                # stream protocol. Never replay agent actions automatically.
                error = {"type": "error", "error": {
                    "message": "本地模型连接中断，请重试。服务正在检查并恢复。",
                    "type": "server_error", "code": "upstream_unavailable",
                }}
                yield ("\n\nevent: error\ndata: " + json.dumps(error, ensure_ascii=False) + "\n\n").encode("utf-8")

        return ManagedStreamingResponse(chunks(), status_code=response.status_code,
                                        headers=response_headers, cleanup=cleanup)
    except CompactionError as exc:
        await cleanup()
        return JSONResponse({"error": {"message": exc.message, "type": "context_compaction_error"}},
                            status_code=exc.status_code, headers={"x-qwen-context-state": "error"})
    except (httpx.HTTPError, OSError) as exc:
        LOGGER.warning("Upstream request failed: %s %s: %s", request.method, path, exc)
        manager.next_health_check = 0
        await cleanup()
        if request.method == "GET" and (not path or path.startswith("chat")):
            if manager.html:
                return HTMLResponse(manager.html, headers={"Cache-Control": "no-store"})
            return loading_page()
        return JSONResponse({"error": {"message": "Qwen 模型暂不可用，正在检查并恢复本地服务。"}}, status_code=503)
    except BaseException:
        await cleanup()
        raise


def main():
    # The uvicorn server is published at module scope (line ~1150) because the shutdown
    # routes flip ``server.should_exit``; without this declaration the assignment below would
    # create a local and every stop request would hit None.
    global server
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bind_host = listening_host()
    bind_port = listening_port()
    if not is_loopback_host(bind_host) and not access_token():
        # Refuse rather than silently exposing model control, file access and
        # generation to every machine that can reach the port.
        raise SystemExit(
            f"Refusing to listen on {bind_host} without an access token. "
            "Set QWEN_ACCESS_TOKEN (or access_token in settings.json) first."
        )
    (ROOT / "manager.pid").write_text(str(os.getpid()), encoding="ascii")
    write_process_record(ROOT / "manager.process.json", {
        "pid": os.getpid(), "created_unix_ms": int(psutil.Process().create_time() * 1000),
        "executable": psutil.Process().exe(), "script": str(ROOT / "mode_server.py"),
    })
    server = uvicorn.Server(uvicorn.Config(app, host=bind_host, port=bind_port, log_level="warning"))
    LOGGER.info(
        "Qwen gateway on %s:%s (shared names: %s, access token: %s)",
        bind_host, bind_port, ", ".join(allowed_host_names()),
        "required" if access_token() else "not set",
    )
    if access_token():
        # The URL is not logged: it contains the owner's admin token, so it goes to
        # a file on this machine instead of into a log the owner may share.
        admin_path = ROOT / "logs" / "admin-link.txt"
        try:
            admin_path.write_text(admin_link() + "\n", encoding="ascii")
            LOGGER.info("Owner console URL written to logs/admin-link.txt")
        except OSError as exc:
            LOGGER.warning("Could not record the owner console URL: %s", exc)
    server.run()
    return 0
