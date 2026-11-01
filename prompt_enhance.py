"""One prompt enhancer for the image and video queues, driven by the selected model.

生图 and 生成视频 both open with "turn my sentence into a real prompt". That step is a language
model call, so it needs a language model — and the one it uses must be **whatever the owner has
currently selected**, not a fixed profile:

* the selection is read from ``qwen_config`` (the same ``model_profile`` the mode toolbar shows),
* the backend is loaded on demand through the caller's own mode loader (``manager.load``), so
  there is no second, parallel idea of "the model" that can drift from the first,
* the label reported to the UI is the selected profile's label, so a panel cannot claim the 27B
  optimised a prompt the 9B actually wrote.

Two measured failures this replaces:

* the image job assumed "Qwen is still loaded in the user's last mode", but entering the 生图 view
  releases the language model to ComfyUI, so the request talked to a closed port and the job
  silently used the raw prompt (``previous_mode`` is never ``"image"``, so the branch that was
  supposed to load it never ran);
* the video path had no such step at all, and the enhancer's name was the literal string
  "Qwen3.8-27B 本地" even while the 9B was selected.

Failure stays fail-open exactly as before: an unreachable or slow model means the owner's own
words are used, and the caller is told which one it got.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

import httpx

import qwen_config

try:  # the Klein prompt lives with the ComfyUI deployment that owns the workflow
    from deployment.flux2_klein_workflow import QWEN_PROMPT_SYSTEM
except ImportError:  # pragma: no cover - path shim for a source checkout
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from deployment.flux2_klein_workflow import QWEN_PROMPT_SYSTEM

LOGGER = logging.getLogger("qwen.prompt")

UPSTREAM = "http://127.0.0.1:8082"

# How long a prompt step waits for ComfyUI to finish before it gives up and uses the owner's own
# words. Loading the language model means freeing ComfyUI's VRAM, which the gateway refuses to do
# while ComfyUI has work; waiting is what turns "ComfyUI 正在执行或排队，将在任务结束后自动加载
# Qwen" from a message the owner cannot act on into something that simply happens. Ten minutes
# covers a finishing shot (measured 870–1262 s per shot at 20 steps is the worst case, 223–320 s
# at 3 steps), and the caller stays visible in the progress rail while it waits.
QUEUE_WAIT_SECONDS = 600.0
QUEUE_POLL_SECONDS = 5.0


class ComfyStillBusy(RuntimeError):
    """ComfyUI never went idle inside the wait window, so the model cannot be loaded yet."""

# The video director reads Chinese scene descriptions (one per shot, with camera and motion), so
# asking for an English Klein prompt there produced the wrong document. One system prompt per
# *kind* of generation — the model underneath is the dynamic part, the task is the fixed part.
VIDEO_PROMPT_SYSTEM = (
    "你是短片分镜提示词作者。把用户的一句话扩写成一条可直接拍摄的中文画面描述，用于文生视频模型。"
    "要求：只写一段，不要标题、不要分点、不要解释；包含主体与动作、环境与光线、镜头运动（如缓推、横移、"
    "跟拍）、以及整体色调风格；不要出现镜头编号或分段标注；不超过 120 个汉字。"
)


class PromptEnhancer:
    """Enhance a prompt with the currently selected model, loading it first if needed."""

    def __init__(self, client: httpx.AsyncClient, load_mode=None, settings_getter=None,
                 upstream: str = UPSTREAM, comfy_base: str | None = None,
                 queue_wait: float = QUEUE_WAIT_SECONDS,
                 queue_interval: float = QUEUE_POLL_SECONDS):
        self.client = client
        self.load_mode = load_mode
        self.settings_getter = settings_getter or qwen_config.read_settings
        self.upstream = upstream.rstrip("/")
        # Where to ask whether the card is free. None (a unit test, a bare router import) makes
        # the wait a no-op instead of a guess.
        self.comfy_base = (comfy_base or "").rstrip("/") or None
        self.queue_wait = queue_wait
        self.queue_interval = queue_interval
        # What the last call actually used, so the job record can say which model wrote the
        # prompt instead of reporting the selection at the time somebody read the page.
        self.last_used: dict | None = None

    # -- what the UI is told ---------------------------------------------------
    def describe(self) -> dict:
        """Which model will do the prompt work, in the owner's own terms."""
        settings = self.settings_getter() or {}
        profile = qwen_config.active_profile_name(settings)
        return {
            "profile": profile,
            "label": qwen_config.active_profile_label(settings),
            "dynamic": True,
        }

    # -- the work --------------------------------------------------------------
    async def wait_for_comfy_idle(self) -> bool:
        """Wait until ComfyUI has nothing running or queued; False if that never happened.

        The language model and H3 cannot both hold the card, so the gateway frees ComfyUI's VRAM
        before loading Qwen — and it refuses to do that while ComfyUI has work. Waiting here is
        the same promise the old message made ("将在任务结束后自动加载 Qwen"), except the step
        actually completes instead of putting a sentence in the film's progress.
        """
        if not self.comfy_base:
            return True
        deadline = time.monotonic() + max(0.0, self.queue_wait)
        while True:
            try:
                response = await self.client.get(f"{self.comfy_base}/queue", timeout=5)
                response.raise_for_status()
                queue = response.json() or {}
                busy = bool(queue.get("queue_running") or queue.get("queue_pending"))
            except (httpx.HTTPError, OSError, ValueError):
                # An unreachable ComfyUI is the loader's problem, not this wait's.
                return True
            if not busy:
                return True
            if time.monotonic() >= deadline:
                LOGGER.warning("ComfyUI stayed busy for %.0f s; the prompt was left as written",
                               self.queue_wait)
                return False
            await asyncio.sleep(max(0.05, self.queue_interval))

    async def ensure_model(self) -> dict:
        """Load the selected language model, and say which one that is.

        Called before every enhancement rather than assumed: the caller may be sitting in the
        image or video view, where the language model has already been released to ComfyUI.
        ``manager.load`` is the same path the mode buttons use, so the profile loaded here is the
        one the owner selected — that is the whole point of "follow the current model".
        """
        if not await self.wait_for_comfy_idle():
            raise ComfyStillBusy("ComfyUI 忙")
        if self.load_mode is not None:
            await self.load_mode("text")
        return self.describe()

    async def enhance(self, prompt: str, negative: str = "", kind: str = "image") -> tuple[str, dict]:
        """Return ``(prompt_to_use, who_did_it)``; never raises for a model problem."""
        original = (prompt or "").strip()
        info = self.describe()
        if not original:
            return original, {**info, "used": False, "reason": "空提示词"}
        try:
            info = await self.ensure_model()
        except ComfyStillBusy as busy:
            # Short on purpose: the film keeps the owner's own words, and the progress line says
            # so in four characters instead of a sentence about model loading.
            LOGGER.warning("Prompt enhancer skipped: %s", busy)
            return original, {**info, "used": False, "reason": "ComfyUI 忙，未优化"}
        except Exception as failure:  # noqa: BLE001 - a load failure must not kill the job
            LOGGER.warning("Prompt enhancer could not load the current model: %s", failure)
            return original, {**info, "used": False, "reason": f"模型未就绪：{failure}"}
        content = f"User request: {original}"
        if (negative or "").strip():
            content += f"\nNegative constraints (do not include these): {negative.strip()}"
        payload = {
            "messages": [
                {"role": "system", "content": VIDEO_PROMPT_SYSTEM if kind == "video" else QWEN_PROMPT_SYSTEM},
                {"role": "user", "content": content},
            ],
            "temperature": 0.35, "top_p": 0.9, "max_tokens": 256,
            "stream": False,
        }
        try:
            response = await self.client.post(f"{self.upstream}/v1/chat/completions",
                                              json=payload, timeout=90)
            response.raise_for_status()
            result = response.json().get("choices", [{}])[0].get("message", {}).get("content", "")
            if isinstance(result, str) and result.strip():
                used = {**info, "used": True, "reason": ""}
                self.last_used = used
                return result.strip(), used
            reason = "模型没有返回内容"
        except (httpx.HTTPError, OSError, ValueError, KeyError, IndexError, TypeError) as failure:
            reason = f"调用失败：{failure}"
            LOGGER.warning("Prompt enhancement failed (%s); using the owner's own words",
                           kind, exc_info=True)
        return original, {**info, "used": False, "reason": reason}
