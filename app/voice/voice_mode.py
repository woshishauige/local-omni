"""Voice conversation backend: a language brain + Qwen ASR.

The brain is the *same* model the chat page talks to whenever the selected profile is the
one voice will serve, so voice mode usually borrows the gateway's already-loaded backend
instead of loading a second copy (`share_brain`). Only the recogniser is then an extra
process, which is what makes switching between typing and talking cheap.

**Which profile may serve voice is decided by the profile itself** (`voice_context_size`):
9B = 40960, ternary 27B = 8192, the old IQ4_XS = none at all (14.25 GB cannot fit beside the
recogniser on a 12 GB card). 2026-09-23 晚之前这里写死成三元 27B，后果是"选中 9B 时语音按钮
不可点"；现在语音跟着当前选中的档位走，能不能用由那份档案的数字决定，未知档位、没有语音窗口的
档位、权重不在本机的档位才会被拒绝——而且拒绝时说的是**具体哪一条**不满足。

Every process this module started itself is stopped with it, because the card cannot hold a
second brain, the recogniser and the synthesiser all at once. A borrowed brain is never
killed here: the gateway owns it.
"""

import asyncio
from pathlib import Path
import json
import logging
import os
import subprocess
import sys
import time

import httpx

from app.qwen_config import (PROFILES, VOICE_PROFILE, VOICE_PROFILE_LABEL, active_executable,
                         active_paths, profile_label, voice_context)

LOGGER = logging.getLogger("qwen.voice-mode")

from app.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT

# The single profile voice mode will ever serve. Everything else is refused. Re-exported
# from qwen_config because the gateway has to make the same "same weights or not?"
# decision this module does, and one definition cannot drift from itself.
__all__ = ["VOICE_PROFILE", "VOICE_PROFILE_LABEL", "VoiceMode", "VOICE"]

# The recogniser is a small standard GGUF and keeps the stock build it was validated on.
LLAMA = ROOT / "runtime" / "llama-server.exe"


# 大脑的三件套（可执行文件、权重、投影器）与窗口都**按档位现取**：三元 27B 只在 PrismML fork 上
# 加载，写死 stock 路径就是"用读不了它的二进制去启动它"。函数而不是常量，是因为服务的档位会变。
def brain_llama(profile: str) -> Path:
    return active_executable({"model_profile": profile})


def brain_paths(profile: str):
    return active_paths({"model_profile": profile})


def brain_context(profile: str) -> int:
    return int(voice_context({"model_profile": profile}))


def voice_capable(profile: str) -> bool:
    """这份档案能不能当语音大脑——由它自己的数字决定，不靠一张写死的名单。"""
    info = PROFILES.get((profile or "").strip())
    return bool(info) and bool(info.get("voice_context_size"))
ASR_MODEL = ROOT / "models" / "asr" / "qwen3-asr-0.6b" / "Qwen3-ASR-0.6B-Q8_0.gguf"
ASR_MMPROJ = ROOT / "models" / "asr" / "qwen3-asr-0.6b" / "mmproj-Qwen3-ASR-0.6B-Q8_0.gguf"

BRAIN_PORT = 8098
ASR_PORT = 8097

# Voice mode trades context for speed headroom: a spoken turn is a handful of sentences, and
# the recogniser is co-resident on the same card. The number comes from the profile's own
# `voice_context_size` (9B 40960 / 三元 27B 8192), so the window the panel reports and the window
# the brain launches with cannot drift apart.

# The output language is pinned deliberately, and it is the only thing in this prompt that decides
# it. Nothing here used to name a language, so the model took its cue from the conversation instead -
# and because the last eight turns are replayed verbatim (`answer_stream`), a single English reply
# kept pulling every following turn into English. That is the whole mechanism behind "why is it
# speaking English again": it is not that one turn went wrong, it is that the turn stays in the
# history. Mixed language is also what makes the synthesiser read English words with Chinese
# prosody, so pinning it fixes the audio as well as the text.
SYSTEM_PROMPT = (
    "你是一个语音助手。回答要简洁口语化，控制在两三句话以内，适合朗读。"
    "始终用简体中文回答；即使对方夹带英文单词，也仍旧用中文；"
    "只有当对方明确要求换用别的语言时才改用那种语言。"
)


class VoiceModeError(RuntimeError):
    """Raised when voice mode cannot serve a request, with a user-facing message."""


class VoiceMode:
    """Owns the 9B brain and the recogniser for as long as voice mode is active."""

    def __init__(self):
        self.brain = None
        self.asr = None
        # 这份实例正在服务哪个档位：启动脑或借用网关后端时写下来，状态与记账都读它。
        self.profile = ""
        self.brain_ready = False
        self.asr_ready = False
        self.started_at = 0.0
        self.last_error = ""
        # Port of the gateway's own backend when it already serves this profile. Set, the
        # brain is *shared*: nothing is spawned for it and nothing may kill it, because the
        # process belongs to the gateway. Only the recogniser is then a voice-mode extra,
        # which is the whole point — switching between typing and talking keeps one model
        # resident instead of reloading several gigabytes twice.
        self.shared_brain_port = None
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0))

    # ------------------------------------------------------------------ sharing

    def share_brain(self, port):
        """Point voice turns at an already-loaded backend for this profile."""
        port = int(port)
        if self.shared_brain_port != port:
            LOGGER.info("voice mode will share the backend on port %s", port)
        self.shared_brain_port = port
        self.brain_ready = True

    def release_brain(self):
        """Forget the shared backend. Never terminates it: the gateway owns it."""
        if self.shared_brain_port is not None:
            LOGGER.info("voice mode stopped sharing the backend on port %s", self.shared_brain_port)
        self.shared_brain_port = None
        self.brain_ready = False

    @property
    def brain_port(self):
        return self.shared_brain_port or BRAIN_PORT

    @property
    def recognizer_port(self):
        """Where the recogniser listens, i.e. the port this mode adds to the machine."""
        return ASR_PORT

    def voice_turn_profile(self):
        """The profile that actually answers a voice turn right now.

        It is reported rather than assumed so the panel can say which model is behind the
        microphone instead of showing a label that has stopped being true — the whole point
        of taking the model identity from one place. 还没开始服务时回落到默认档位（三元 27B，
        它是"没有特别指定"时的老默认），启动或借用后端时会被写成真实的那一份。
        """
        return self.profile or VOICE_PROFILE

    # ------------------------------------------------------------------ checks

    @staticmethod
    def profile_allowed(profile):
        """Whether a profile may serve voice turns.

        Exposed as a function so both the API layer and the tests assert the same rule, and
        so the refusal reason is written in exactly one place. 判定只看那份档案自己：
        ① 认识这个档位；② 它有语音窗口（`voice_context_size`）；③ 权重在本机。
        """
        name = (profile or "").strip()
        info = PROFILES.get(name)
        if not info:
            return False
        return bool(info.get("voice_context_size")) and info["model"].is_file()

    @staticmethod
    def refusal_message(profile=""):
        """为什么这一档不能语音——说清是三条里的哪一条，别让人去猜。"""
        name = (profile or "").strip()
        info = PROFILES.get(name)
        if not info:
            return f"不认识的模型档位「{name or '空'}」，语音对话无法启动。"
        if not info.get("voice_context_size"):
            return (f"{profile_label(name)} 没有语音窗口：语音的大脑和识别器要同时驻留，"
                    f"这一档的权重装不下这一对。请换 9B 或 27B 三元版。")
        return f"{profile_label(name)} 的权重不在本机（{info['model']}），语音对话无法启动。"

    def status(self):
        return {
            "engine": "voice-mode",
            "profile": self.voice_turn_profile(),
            "profile_label": profile_label(self.voice_turn_profile()),
            # 档位不再固定：跟着当前选中的模型走（`/local/voice/config` 会报当前这一份）。
            "locked": False,
            "brain_ready": self.brain_ready,
            "asr_ready": self.asr_ready,
            # Readiness is about the language model, because that is what every turn
            # needs. The recogniser is started on demand by the spoken path and is
            # reported separately, so a typed turn is not held back by a model it will
            # never call.
            "state": "ready" if self.brain_ready else (
                "starting" if (self.brain or self.asr) else "idle"),
            "error": self.last_error,
            "brain_port": self.brain_port,
            "brain_shared": self.shared_brain_port is not None,
            # Whether this session started the brain itself or borrowed the chat backend.
            # Both are "same weights"; only the second avoids a load, and the panel says
            # which one happened instead of guessing from a spinner.
            "brain_owner": "gateway" if self.shared_brain_port is not None else (
                "voice-mode" if self._alive(self.brain) else "none"),
            "asr_port": self.recognizer_port,
            # 这一档自己的语音窗口（9B 40960 / 三元 27B 8192）；借用常驻后端时窗口是后端的，报 None。
            # 写死一个常量在这里就是这次那个 500：档位不再固定之后，`VOICE_CTX` 已经没有定义了。
            "context_size": (brain_context(self.voice_turn_profile())
                             if self.shared_brain_port is None else None),
        }

    # ------------------------------------------------------------------ process helpers

    def _alive(self, process):
        return process is not None and process.poll() is None

    async def _wait_health(self, port, timeout=180.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            try:
                response = await self._client.get(f"http://127.0.0.1:{port}/health", timeout=4)
                if response.status_code == 200 and response.json().get("status") == "ok":
                    return True
            except (httpx.HTTPError, OSError, ValueError):
                continue
        return False

    async def _adopt_running(self, port):
        """Adopt a healthy backend already listening on a voice port.

        The gateway keeps no handle on processes started before it (or by a previous
        gateway), and adopting by health check rather than by PID is what stops a
        second copy from being launched onto an occupied port.
        """
        try:
            response = await self._client.get(f"http://127.0.0.1:{port}/health", timeout=4)
            return response.status_code == 200 and response.json().get("status") == "ok"
        except (httpx.HTTPError, OSError, ValueError):
            return False

    async def start_asr(self):
        """Start the recogniser. It is small (~0.95 GB) and always wanted first."""
        if self._alive(self.asr):
            self.asr_ready = await self._wait_health(ASR_PORT, 20)
            return self.asr_ready
        if await self._adopt_running(ASR_PORT):
            self.asr_ready = True
            LOGGER.info("voice mode adopted the recogniser already on port %s", ASR_PORT)
            return True
        if not (ASR_MODEL.is_file() and ASR_MMPROJ.is_file()):
            self.last_error = "识别模型文件缺失"
            return False
        logs = ROOT / "logs"
        logs.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = (logs / f"{stamp}-voicemode-asr.out.log").open("wb")
        err = (logs / f"{stamp}-voicemode-asr.err.log").open("wb")
        try:
            self.asr = subprocess.Popen(
                [str(LLAMA), "-m", str(ASR_MODEL), "--mmproj", str(ASR_MMPROJ),
                 "--host", "127.0.0.1", "--port", str(ASR_PORT),
                 "--alias", "qwen3-asr-voice", "-c", "4096", "-ngl", "99",
                 "--no-warmup", "--no-mmproj-offload"],
                cwd=str(ROOT), stdout=out, stderr=err,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except OSError as exc:
            self.last_error = f"无法启动识别后端：{exc}"
            return False
        self.asr_ready = await self._wait_health(ASR_PORT)
        if not self.asr_ready:
            self.last_error = "识别后端启动超时"
        return self.asr_ready

    async def start_brain(self, profile: str = ""):
        """Start the brain of the profile this turn will use, every layer on the GPU.

        Skipped entirely when the gateway is already serving that profile: the model is on
        the card, and starting a second copy of it is exactly the waste this avoids.

        Never guessed. `VOICE_PROFILE` used to be the silent fallback here, and it is the
        pre-2026-09-23 default - the ternary 27B - so a caller that had not said which profile it
        wanted got 7.2 GB of the wrong model pulled off disk and a "27B 三元去审查 大脑启动超时"
        240 s later, on a machine whose selected model was the 9B. Naming one is now the caller's
        job; refusing is the honest answer when nobody did.
        """
        profile = (profile or self.profile).strip()
        if not profile:
            self.last_error = "还没有指定语音档位：请先进入语音界面，再开始对话。"
            LOGGER.warning("voice: start_brain refused - no profile has been named")
            return False
        if not self.profile:
            self.profile = profile
        if self.shared_brain_port is not None:
            self.brain_ready = await self._wait_health(self.shared_brain_port, 30)
            if not self.brain_ready:
                self.last_error = "共用的聊天后端没有响应"
            return self.brain_ready
        if self._alive(self.brain):
            self.brain_ready = await self._wait_health(BRAIN_PORT, 20)
            return self.brain_ready
        if await self._adopt_running(BRAIN_PORT):
            self.brain_ready = True
            LOGGER.info("voice mode adopted the %s already on port %s", profile, BRAIN_PORT)
            return True
        executable = brain_llama(profile)
        model, _projector = brain_paths(profile)
        context = brain_context(profile)
        if not model.is_file():
            self.last_error = f"{profile_label(profile)} 模型文件缺失（{model}）"
            return False
        logs = ROOT / "logs"
        logs.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = (logs / f"{stamp}-voicemode-brain.out.log").open("wb")
        err = (logs / f"{stamp}-voicemode-brain.err.log").open("wb")
        try:
            self.brain = subprocess.Popen(
                [str(executable), "-m", str(model),
                 "--host", "127.0.0.1", "--port", str(BRAIN_PORT),
                 "--alias", profile, "-c", str(context),
                 "-ngl", "99", "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0",
                 "--reasoning", "off", "--no-warmup"],
                cwd=str(ROOT), stdout=out, stderr=err,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except OSError as exc:
            self.last_error = f"无法启动语音大脑：{exc}"
            return False
        self.brain_ready = await self._wait_health(BRAIN_PORT, 240)
        if not self.brain_ready:
            self.last_error = f"{profile_label(profile)} 大脑启动超时"
        return self.brain_ready

    async def ensure_ready(self, need_asr: bool = False):
        """Bring up what the turn in hand actually needs.

        A typed turn needs the language model and nothing else: it never reaches a
        recogniser, so starting one would spend VRAM and startup time on a model the
        turn cannot use - and the panel's own recognition does not run here at all (it
        goes through voice_input's faster-whisper). Only the spoken path asks for the
        recogniser, and it asks explicitly.
        """
        if need_asr:
            await self.start_recognizer()
        async with self._lock:
            want_brain = not self.brain_ready
            want_asr = need_asr and not self.asr_ready
            if want_brain or want_asr:
                self.last_error = ""
                self.started_at = self.started_at or time.time()
                if want_asr:
                    await self.start_asr()
                if want_brain:
                    await self.start_brain()
            if need_asr:
                return self.brain_ready and self.asr_ready
            return self.brain_ready

    async def start_recognizer(self):
        """Bring the recogniser up on its own, without touching the brain.

        Exposed so entering voice mode can warm it while the owner reads the panel, and so
        the microphone route can ask for it without going through a whole turn's readiness
        check. Returns whether it is up.
        """
        async with self._lock:
            if self.asr_ready:
                return True
            self.last_error = ""
            await self.start_asr()
            return self.asr_ready

    async def stop(self):
        """Release both backends, including any this object merely adopted.

        An adopted process is not in `self.brain`/`self.asr`, so it is also located
        by the port it listens on: otherwise closing voice mode would leave a model
        holding the card with no handle left to release it.

        A *shared* brain is the exception and is deliberately never touched: it is the
        gateway's own backend, and killing it would unload the very model that sharing
        exists to keep resident.
        """
        for name, port in (("brain", self.brain_port), ("asr", ASR_PORT)):
            if name == "brain" and self.shared_brain_port is not None:
                # Still healthy, still the gateway's. Just stop pointing at it.
                self.release_brain()
                continue
            process = getattr(self, name)
            stopped = False
            if self._alive(process):
                process.terminate()
                try:
                    await asyncio.wait_for(asyncio.to_thread(process.wait), 15)
                except asyncio.TimeoutError:
                    process.kill()
                stopped = True
            if not stopped:
                killed = await asyncio.to_thread(self._kill_port_owner, port)
                if killed:
                    LOGGER.info("voice mode released adopted process on port %s", port)
            setattr(self, name, None)
            setattr(self, f"{name}_ready", False)

    @staticmethod
    def _kill_port_owner(port):
        """Terminate whatever listens on a voice port. Returns True if it killed one."""
        try:
            import psutil
        except ImportError:
            return False
        for connection in psutil.net_connections(kind="tcp"):
            if connection.laddr and connection.laddr.port == port and connection.status == "LISTEN":
                if not connection.pid:
                    continue
                try:
                    victim = psutil.Process(connection.pid)
                    name = (victim.name() or "").lower()
                    # Only ever terminate our own server, never an unrelated program
                    # that happens to have taken the port.
                    if "llama" not in name:
                        continue
                    victim.terminate()
                    victim.wait(timeout=15)
                    return True
                except (psutil.Error, OSError):
                    continue
        return False

    # ------------------------------------------------------------------ inference

    async def transcribe(self, wav_bytes, language="chinese"):
        import base64
        payload = {
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "转写这段音频。"},
                {"type": "input_audio", "input_audio": {
                    "data": base64.b64encode(wav_bytes).decode("ascii"), "format": "wav"}},
            ]}],
            "max_tokens": 256, "temperature": 0,
        }
        response = await self._client.post(f"http://127.0.0.1:{ASR_PORT}/v1/chat/completions", json=payload)
        response.raise_for_status()
        text = response.json()["choices"][0]["message"].get("content", "")
        # The recogniser wraps its answer as "language Chinese<asr_text>…".
        if "<asr_text>" in text:
            text = text.split("<asr_text>", 1)[1]
        return text.strip()

    async def answer(self, text, history=None):
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for turn in (history or [])[-8:]:
            if turn.get("role") in ("user", "assistant") and turn.get("content"):
                messages.append({"role": turn["role"], "content": turn["content"]})
        messages.append({"role": "user", "content": text})
        payload = {
            "messages": messages, "max_tokens": 320, "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        started = time.monotonic()
        response = await self._client.post(f"http://127.0.0.1:{self.brain_port}/v1/chat/completions", json=payload)
        response.raise_for_status()
        body = response.json()
        message = body["choices"][0]["message"]
        reply = (message.get("content") or message.get("reasoning_content") or "").strip()
        timings = body.get("timings", {})
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return reply, {
            "seconds": round(time.monotonic() - started, 2),
            "tokens_per_second": round(timings.get("predicted_per_second", 0), 1),
            "completion_tokens": usage.get("completion_tokens", 0),
            # The prompt size the backend reports, so the shared context meter can show the
            # same kind of number for a spoken turn as for a written one.
            "prompt_tokens": usage.get("prompt_tokens"),
        }

    async def answer_stream(self, text, history=None):
        """Yield the reply in pieces as the model produces them.

        Streaming is what makes the wait short: at ~47 tok/s the first clause exists
        well before the sentence does, so the synthesiser can start on it instead of
        idling until the whole reply is finished.
        """
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for turn in (history or [])[-8:]:
            if turn.get("role") in ("user", "assistant") and turn.get("content"):
                messages.append({"role": turn["role"], "content": turn["content"]})
        messages.append({"role": "user", "content": text})
        payload = {
            "messages": messages, "max_tokens": 320, "stream": True,
            # Ask for the token counts the server already knows. Counting them afterwards with
            # a separate /tokenize call would contend with this very generation for the one
            # backend — the gateway's own timeout was reached that way — and the usage object
            # the stream can carry costs nothing.
            "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": False},
        }
        started = time.monotonic()
        first_token_at = None
        tokens = 0
        usage = {}
        async with self._client.stream(
            "POST", f"http://127.0.0.1:{self.brain_port}/v1/chat/completions", json=payload
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if isinstance(chunk.get("usage"), dict):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content") or delta.get("reasoning_content") or ""
                if not piece:
                    continue
                if first_token_at is None:
                    first_token_at = time.monotonic() - started
                tokens += 1
                yield piece
        elapsed = time.monotonic() - started
        self.last_stream_stats = {
            "first_token_seconds": round(first_token_at or 0, 2),
            "seconds": round(elapsed, 2),
            "chunks": tokens,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        }


VOICE = VoiceMode()
