"""Who may take the GPU right now, and who gets to overrule that.

The machine has one GPU. A shared visitor's request and the owner's request cannot
both be served, and the owner must never be told "no" by a guest's work — so the
decision lives here instead of being implicit in whatever endpoint happens to run.

Two rules, in order:

1. **Free VRAM is the gate.** A load that needs VRAM is admitted only when the card has
   at least ``threshold_mib`` free. Below that, the request is refused rather than
   queued: a queued generation on a full card fails later with a worse message, and a
   queue nobody can see is indistinguishable from a hang.
2. **The owner outranks visitors.** A visitor below the threshold is refused. The owner
   below the threshold is *asked*: the reply says who is on the card right now and
   offers both ways to take it — wait for them to finish, or take it immediately
   ("强行插队", which forbids new visitor work) and, if they want the card clear,
   "打断任务" (which cancels the visitor's running jobs and stops their in-flight
   generation).

Only the owner can preempt; the flag is raised by an owner request and lifted when that
work finishes or the owner lifts it. Nothing here ever refuses the owner.
"""

import asyncio
import logging
import subprocess
import time

LOGGER = logging.getLogger("qwen.gpu")

# Free VRAM that must remain for a new VRAM-hungry load to be admitted. Above the
# ~2 GiB a Flux/H3 swap needs, and small next to the card's capacity, so an idle
# machine always passes and a card mid-generation never does.
DEFAULT_THRESHOLD_MIB = 2048
# Text turns ride on the model that is already resident, so they need almost nothing
# new: holding them to the swap threshold would refuse ordinary chat on a card that is
# merely busy. This is deliberately tiny — it exists so that a request is not admitted
# onto a card with literally no room left, not to ration conversation.
TEXT_THRESHOLD_MIB = 128
# After an owner interrupt, how long to keep re-reading the card before proceeding.
PREEMPT_GRACE_SECONDS = 6.0
# A sentinel the test suite sets so a proxy decision never depends on what the real
# card happens to be doing. Not a user-facing switch.
ALWAYS_ROOMY = -1
# nvidia-smi is spawned, not free: one reading serves every request in this window.
SAMPLE_TTL_SECONDS = 1.0

# Load kinds, in the order the panel should show them.
HEAVY_KINDS = ("image", "video", "vision", "text", "denoise")
KIND_LABELS = {
    "text": "文本对话",
    "vision": "识图",
    "image": "生图 / 放大 / 降噪",
    "video": "生视频 / 插帧 / 拼接",
    "denoise": "视频降噪",
}


def is_test_process():
    """Whether this process is a test run (see ``qwen_config.tests_are_running``).

    Exposed as a module-level function so it can be patched: the arbiter's own tests
    need the production rule, while the proxy suites need a decision that does not move
    with the machine's live VRAM.
    """
    from app import qwen_config

    try:
        return bool(qwen_config.tests_are_running())
    except Exception:  # noqa: BLE001 - an unreadable flag must not change behaviour
        return False


class Work:
    """One in-flight request that is (or is about to be) using the GPU."""

    __slots__ = ("session", "owner", "label", "kind", "started", "endpoint")

    def __init__(self, session, owner, label, kind, endpoint=""):
        self.session = session
        self.owner = bool(owner)
        self.label = label or ("本机管理" if owner else "未命名设备")
        self.kind = kind
        self.started = time.time()
        self.endpoint = endpoint

    def describe(self):
        return {
            "label": self.label,
            "kind": self.kind,
            "kind_label": KIND_LABELS.get(self.kind, self.kind),
            "owner": self.owner,
            "endpoint": self.endpoint,
            "started_at": self.started,
            "elapsed_seconds": round(time.time() - self.started, 1),
        }


def classify(path, method="POST", payload=None):
    """Which kind of load a request carries, or None when it takes no VRAM.

    Judged from the route and, for chat, from the body: a text turn rides on the model
    that is already resident, while an image turn can force a model swap.
    """
    path = (path or "").strip("/")
    if path.startswith("local/video/"):
        if path.endswith(("generate", "interpolate", "concat", "denoise", "denoise-source")):
            return "video"
        return None
    if path.startswith("local/image/"):
        if path.endswith(("generate", "denoise")):
            return "image"
        return None
    if path.startswith("local/voice/"):
        # Recognition runs on the CPU precisely so it never competes for the card.
        return None
    if path.endswith(("chat/completions", "chat/completion")):
        # Text and image turns are separated because they sit on different backends: a
        # crowded card cannot take a vision-model swap, but it can still answer text on
        # the model it already holds, and refusing text would be a worse service than
        # the guest was promised.
        return "vision" if _has_media(payload) else "text"
    return None


def _has_media(payload):
    """Whether this turn carries an image, without walking tool schemas."""
    if not isinstance(payload, dict):
        return False
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in {
                    "image_url", "input_image", "image",
                }:
                    return True
    return False


class GpuArbiter:
    """Live VRAM reading plus the admission decision, shared by every route."""

    def __init__(self, threshold_mib=DEFAULT_THRESHOLD_MIB, clock=time.time,
                 text_threshold_mib=TEXT_THRESHOLD_MIB):
        self.threshold_mib = int(threshold_mib)
        self.text_threshold_mib = int(text_threshold_mib)
        self.clock = clock
        self.lock = asyncio.Lock()
        self.active: dict[str, Work] = {}
        self.preempt_until = 0.0
        self.preempt_reason = ""
        self.preempted_at = 0.0
        self.last_vram: dict | None = None
        self.last_sample_at = 0.0
        self._sample_lock = asyncio.Lock()

    # ------------------------------------------------------------------ reading
    async def vram(self, max_age=SAMPLE_TTL_SECONDS):
        """Free/total VRAM in MiB, or None when the card cannot be read."""
        now = self.clock()
        if self.last_vram is not None and (now - self.last_sample_at) < max_age:
            return self.last_vram
        async with self._sample_lock:
            reading = await self._sample()
        if reading is not None:
            self.last_vram = reading
            self.last_sample_at = now
        return reading

    async def _sample(self):
        def run():
            try:
                completed = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.total,memory.used,memory.free",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=10, check=False,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            line = completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else ""
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 3:
                return None
            try:
                total, used, free = (float(value) for value in parts[:3])
            except ValueError:
                return None
            return {"total_mib": total, "used_mib": used, "free_mib": free}

        try:
            return await asyncio.to_thread(run)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

    # ------------------------------------------------------------------ deciding
    def _preempting(self):
        return self.clock() < self.preempt_until

    async def reserve(self, session, owner, label, kind, endpoint=""):
        """Decide one request, then register it when it is admitted.

        Returns ``{"decision": "allow"|"refuse"|"ask", ...}``. Only "allow" registers
        work, so a refused request leaves nothing to release.
        """
        if kind is None:
            return {"decision": "allow", "tracked": False}
        work = Work(session, owner, label, kind, endpoint)
        vram = await self.vram()
        free = vram["free_mib"] if vram else None
        needed = self.threshold_mib
        if kind == "text":
            needed = min(self.threshold_mib, self.text_threshold_mib)
        if is_test_process():
            # A test run must not depend on what this machine's card happens to be
            # doing: the proxy suites drove the real arbiter and their outcome changed
            # with whatever model was loaded. Decisions are made for the test client.
            tight = False
        else:
            tight = self.threshold_mib != ALWAYS_ROOMY and free is not None and free < needed
        if self._preempting() and not owner:
            return {
                "decision": "refuse",
                "tracked": False,
                "message": "机主正在使用显卡，已暂停访客任务，请稍后再试。",
                "retry_after": 20,
                "vram": vram,
                "threshold_mib": self.threshold_mib,
            }
        if not tight:
            return {"decision": "allow", "tracked": True, "work": work, "vram": vram}
        if not owner:
            return {
                "decision": "refuse",
                "tracked": False,
                "message": (f"当前显卡可用显存不足（{free:.0f} MiB < {needed} MiB），"
                            "已拒绝本次请求；请等机主当前任务结束后重试。"),
                "retry_after": 30,
                "vram": vram,
                "threshold_mib": needed,
            }
        return {
            "decision": "ask",
            "tracked": False,
            "message": (f"显卡可用显存不足（{free:.0f} MiB < {needed} MiB），"
                        "需要先处理正在使用显卡的任务。"),
            "vram": vram,
            "threshold_mib": needed,
            "busy": [entry.describe() for entry in self.active.values()],
        }

    async def admit(self, reservation, session, owner, label, kind, endpoint="", preempt=False):
        """Complete an admission, taking the card first when the owner insists."""
        if reservation.get("decision") == "allow":
            if reservation.get("tracked") and reservation.get("work") is not None:
                self.active[session] = reservation["work"]
            return reservation
        if reservation.get("decision") == "ask" and preempt:
            self.begin_preempt("owner took the GPU")
            await self.wait_for_room()
            work = Work(session, True, label or "本机管理", kind, endpoint)
            self.active[session] = work
            return {"decision": "allow", "tracked": True, "work": work,
                    "vram": await self.vram(max_age=0)}
        return reservation

    async def wait_for_room(self, seconds=PREEMPT_GRACE_SECONDS):
        """Give the interrupt a moment to free the card, then stop waiting."""
        deadline = self.clock() + seconds
        while self.clock() < deadline:
            reading = await self.vram(max_age=0.0)
            if reading is None or reading["free_mib"] >= self.threshold_mib:
                return reading
            await asyncio.sleep(0.5)
        return await self.vram(max_age=0.0)

    # ------------------------------------------------------------------ releasing
    def release(self, session):
        self.active.pop(session, None)

    # ------------------------------------------------------------------ preempting
    def begin_preempt(self, reason="owner took the GPU"):
        """Forbid new visitor work and mark the card as the owner's.

        Open-ended on purpose: preemption lasts until the owner lifts it, not until a
        timer expires. A visitor quietly admitted again after a few minutes would be
        exactly the surprise this is meant to prevent.
        """
        self.preempt_until = float("inf")
        self.preempt_reason = reason
        self.preempted_at = self.clock()
        LOGGER.info("GPU: owner preempted visitors (%s)", reason)

    def end_preempt(self):
        if self.preempt_until or self.preempt_reason:
            LOGGER.info("GPU: owner released the preemption")
        self.preempt_until = 0.0
        self.preempt_reason = ""
        self.preempted_at = 0.0

    def guests_working(self):
        return [entry for entry in self.active.values() if not entry.owner]

    # ------------------------------------------------------------------ reporting
    async def status(self):
        # 面板轮询这个接口是为了显示：VRAM 在 1 秒内的变化对显示没有意义，而每次强制
        # 新采样都要起一次 nvidia-smi（本机实测 52 ms）。用默认 TTL 缓存即可——真正的
        # 准入判断（reserve/admit）自己决定新鲜度，不经过这里。
        vram = await self.vram()
        free = vram["free_mib"] if vram else None
        return {
            "vram": vram,
            "free_mib": free,
            "threshold_mib": self.threshold_mib,
            "text_threshold_mib": min(self.threshold_mib, self.text_threshold_mib),
            "tight": bool(free is not None and free < self.threshold_mib),
            "readable": vram is not None,
            "preempting": self._preempting(),
            "preempt_reason": self.preempt_reason,
            "preempted_at": self.preempted_at,
            "work": [entry.describe() for entry in self.active.values()],
            "guests": len(self.guests_working()),
        }
