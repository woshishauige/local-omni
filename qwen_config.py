"""Shared inference settings for the local UI and reproducible benchmarks."""

import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent

# --- text/vision backend -------------------------------------------------------
#
# The paths below are the *default* profile, kept as module-level constants so the
# tests and the mode server can still import MODEL/MMPROJ directly. A second model
# is selected by putting "model_profile" in settings.json: PROFILES maps a name to
# the paths and launch numbers that differ between models, and server_command()
# reads whichever one is active.
#
# The per-model numbers genuinely differ and must not be shared: a 27B IQ4_XS needs
# ~38 layers offloaded to fit a 12 GB card and has an MTP draft head, while a 9B
# fits entirely on the card and has no draft head. Getting this wrong is the
# difference between a fast model and one that spills to RAM, so each profile
# carries its own values rather than inheriting the 27B's.
# 2026-09-23 换血：三个 27B 候选（现役 ZeroRefusal IQ4_XS / Bonsai 2 CRACK /
# Bonsai 2 Abliterated）用同一套电池实测，CRACK 综合最优（见 _bonsai-eval/DECISION.md）。
# 它是 Qwen3.8-27B 的三元 2.13 bpw 压缩 + CRACK 去审查，必须跑 PrismML fork：
# stock llama.cpp 会以 unknown type 拒绝 PQ2_0 张量。
MODEL = ROOT / "models/Bonsai-2-27B-CRACK/Bonsai-2-27B-PQ2_0-CRACK.gguf"
MMPROJ = ROOT / "models/Bonsai-2-27B-CRACK/Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf"
# 后端二进制按 profile 区分：9B / 旧 27B 用 stock 构建，三元 27B 用 Prism fork。
#
# 2026-10-10/27 升级记录：
#   stock  b10819 → b11541 ✅ 保留：实测 pp512 2407→2518 t/s（+4.6%）、tg128 54.8→55.6（+1.6%，
#          在噪声内）；归档的 IQ4_XS 27B（34 层、带宽受限）tg 持平。**行为验证**：同样 20 道
#          选择题、全新会话、`chat_template_kwargs.enable_thinking=false` → 9B 得 11/20、
#          **0 题未解析、0 题漏思考**。上游之后又到 b11935（10-27），但 100 条提交里架构改动集中在
#          Vulkan/SYCL/OpenCL/Metal/Hexagon 与服务端重构，没有一条能加速我们这种单模型稠密 CUDA
#          解码，故不为它再付一次验证成本。
#   fork   b10709 → b10754（10-02）→ **b10826 ✅ 采用**（`runtime-prism-10826\`，10-25，commit c9c34bc9c）：
#          b10770（10-09）更快但**输出是垃圾**（任何提示只吐 `/` 铺满预算，20 题 6/20、3 题无字母、
#          9 题在"思考"）；排查排除了"开关没设对"（`--reasoning off`/`--reasoning-effort low`/
#          `--reasoning-budget 0` 增删都照旧）、"模型改版"（基础仓库 09-17 后只有文档提交）、
#          "应用层"（裸 llama-server 同样复现），性质属"PQ2_0 在特定 GPU 后端输出垃圾"一类
#          （官方 KNOWN_ISSUES 有同症状的 HIP 缺陷 #223），回归区间钉在 10-02(b10754 干净) →
#          10-09(b10770 垃圾)。**b10826 实测已修复**：同一批 20 题 13/20、0 未解析、0 思考、0 垃圾，
#          与 b10754/b10709 逐题同分；背靠背两轮 llama-bench pp512 832→853 t/s **+2.5%**、
#          tg128 39.1→40.2 **+2.7%**。该版架构改动：PQ2_0 Hadamard 融合、GDN 解码卷积链融合、
#          每步解码省 55-65 µs 主机时间、视觉编码器 head-72 走 MMA（作者称 2048² 上 2.2×，未复测）。
#          缺陷报告 `_bonsai-eval/PRISM-b10770-garbage-report.md` 已按"已被 b10826 修复"更新。
# 注意：llama-bench 绝对值跨会话会漂（同一 b10754 早先 1091/48.2、后来 832/39.1），
# 只有同一次会话里的背靠背对照才算数。
# 回滚方法：把下面两行指回 `runtime/`（b10819）与 `runtime-prism/`（b10709），两个目录都还在。
EXECUTABLE = ROOT / "runtime-stock-11935/llama-server.exe"
PRISM_EXECUTABLE = ROOT / "runtime-prism-10826/llama-server.exe"
SETTINGS_PATH = ROOT / "settings.json"

# The voice context window: one number, decided once, named so that nothing can quietly
# recompute it. Voice mode runs the 9B at a smaller window than text/vision because it always
# adds the recogniser (~1.1 GiB measured with probe_9b_context.py) and replays at most eight
# short turns, so the larger window would buy nothing and spend the margin a spoken turn needs.
#
#   32768    7.26 GiB / 3.20 free   (text+vision)
#   40960    as measured for voice: recogniser added, ~2 GiB still free
#  131072    9.16 GiB / 1.35 free   (fine for text/vision, no room beside the recogniser)
#
# It is referenced by the profile below, reported by /local/voice/config, and pinned by
# test_context_config.test_voice_window_is_named_and_locked — changing it has to be deliberate.
VOICE_CONTEXT_SIZE = 40960

PROFILES = {
    # The machine's existing, tuned model. Unchanged behaviour.
    "qwen3.8-27b": {
        "model": MODEL,
        "mmproj": MMPROJ,
        "alias": "qwen3.8-27b-local",
        # 三元 27B 只有 7.21 GB，全部 64 个 block 都能进 12 GB 显存；换血前的 IQ4_XS 有
        # 13.27 GB，必须留 27 层在内存，那才是它只有 ~9 tok/s 的原因（实测 32.3 tok/s 对 6.7）。
        # settings.json 里的 text/vision_gpu_layers 会对默认 profile 生效（active_gpu_layers），
        # 当前两键都不存在，所以这里 profile 的 64 就是实际值；将来若有人加上它们，
        # 上限是 read_settings 校验的 64，不能写 99。
        "executable": PRISM_EXECUTABLE,
        "gpu_layers": 64,
        "vision_gpu_layers": 64,
        # 这版没有 MTP 草稿头：向它请求 MTP 会让启动直接失败，所以必须是 False。
        "mtp": False,
        # 实测 16K 峰值 9.76 GB / 余量 2.18 GB；官方建议 12 GB 卡不超过 40K。
        "context_size": 32768,
        # 语音大脑自己的窗口。它比文字窗口小得多是有实测依据的：27B 大脑 @8192 占
        # +7653 MiB，加上识别器 +1244 MiB 后整卡 10725 / 12227 MiB（剩 1220 MiB）；
        # 而同驻的 TTS 合成器看到空闲 < 2600 MiB 会自动走 CPU（RTF 0.58，见语音清单）。
        "voice_context_size": 8192,
        # 思考档位：读它的 `tokenizer.chat_template` 得到的白名单（模板对非法值直接 raise）。
        "thinking_efforts": ("low", "medium", "xhigh"),
    },
    # 所有者要求保留的旧版 27B（IQ4_XS）：它比三元版在知识面上**实测高 3.6 个百分点**
    # （MMLU 0-shot 82.78% vs 79.18%，1000 题配对，p=0.001），代价是慢 4.8 倍（6.7 vs 32.3
    # tok/s），因为 13.27 GB 装不进 12 GB、必须留 27 层在内存。
    # 文件留在归档目录 `C:\_archive\qwen-27b\`（D 盘只剩 22 GB），所以这里写绝对路径；
    # 它用原配 stock 引擎，层数取实测能塞进显存且留出余量的 34 层。
    "qwen3.8-27b-iq4xs": {
        "model": Path(r"C:\_archive\qwen-27b\Qwen3.8-27B-ZeroRefusal-UD-IQ4_XS-V3-Final-MTP.gguf"),
        "mmproj": Path(r"C:\_archive\qwen-27b\Qweb3.8-mmproj-BF16.gguf"),
        "alias": "qwen3.8-27b-iq4xs-local",
        "executable": EXECUTABLE,
        "gpu_layers": 34,
        "vision_gpu_layers": 34,
        # 它带 MTP 草稿头，但这里保持关闭：开关关着时行为与三元版一致，便于对照；
        # 想恢复它原本的 +13% 速度，把这里和 settings.json 的 mtp_enabled 一起打开即可。
        "mtp": False,
        "context_size": 16384,
        "thinking_efforts": ("low", "medium", "xhigh"),
    },
    # Uncensored 9B. Small enough that every layer fits on the card with room to spare,
    # which is why it is also the voice profile: a spoken turn adds a ~1.1 GiB recogniser.
    # No MTP head ships with this conversion, so speculative decoding stays off.
    "qwen3.5-9b-uncensored": {
        "model": ROOT / "models/Qwen3.5-9B-Uncensored/Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q6_K.gguf",
        "mmproj": ROOT / "models/Qwen3.5-9B-Uncensored/mmproj-Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-BF16.gguf",
        "alias": "qwen3.5-9b-uncensored-local",
        # 32 layers total; 64 is the settings ceiling and comfortably covers them.
        "gpu_layers": 64,
        "vision_gpu_layers": 64,
        "mtp": False,
        # 它的模板里没有 `reasoning_effort`（实测 0 次），所以思考只有开/关两态。
        "thinking_efforts": (),
        # Measured with probe_9b_context.py on this card, per mode and per plugin:
        #
        #   context   text+vision (mmproj)   with the voice recogniser added
        #   32768     7.26 GiB / 3.20 free   8.33 GiB / 2.28 free
        #   81920     8.15 GiB / 2.34 free   (not safe)
        #  131072     9.16 GiB / 1.35 free   (not safe)
        #  147456     9.50 GiB / 1.01 free   over the margin
        #
        # Text and vision share one process, so they must share one value: giving them
        # different windows would turn every switch into a full reload and throw away the
        # point of the unified backend. 131072 is this model's own training maximum, and it
        # is the largest value that keeps a generation-safe margin. Voice mode always adds
        # the recogniser (~1.1 GiB), so it gets its own, smaller ceiling.
        "context_size": 131072,
        "voice_context_size": VOICE_CONTEXT_SIZE,
    },
}
DEFAULT_PROFILE = "qwen3.8-27b"

# Voice conversation is locked to one profile. The lock is about *memory*, not speed: a
# spoken turn needs the brain and the recogniser on the card at the same time, and the
# pairing has to be decided by the gateway that owns the backend (see `same_source`), so
# the constant lives here while `voice_mode` imports it.
#
# 2026-09-23: the brain moved from the 9B to the ternary 27B, because the reason for the old
# lock no longer held. Measured with both processes up, desktop included:
#   三元 27B 大脑 @8192（-ngl 99）= +7653 MiB，识别器 = +1244 MiB → 合计 10725 / 12227 MiB，剩 1220 MiB
# The old 27B (13.27 GB, 27 layers in RAM) could never do this; the ternary one can, and it
# is the smarter of the two (MMLU 0-shot 79.2% vs 76.5%). Speed is not the constraint either:
# 32.3 tok/s against a ~8-10 tok/s speech budget.
VOICE_PROFILE = "qwen3.8-27b"
VOICE_PROFILE_LABEL = "27B 三元去审查"

# Which resident weights each mode talks to, for the modes that have a language model of
# their own. Modes absent from this mapping (image, video generation) are served by ComfyUI on
# different weights entirely. "active" means whatever profile settings.json selects.
#
# 2026-09-23 实测：**三元 27B 读不了视频**（`probe_video_vision.py` 三条短片全部 HTTP 400
# "Failed to load image or audio file"），而 9B 三条全对（数字顺序、颜色顺序、运动方向）。
# 所以"识图要跑 9B"这条规则是真实的，但**目前只能靠选档实现**：这条映射只被决策/上报层
# （`mode_requires_profile` / `source_switch`）读到，而 `ModeManager.load()` 从头到尾用
# `active_paths(self.settings)`，所以把这里写成 9B **不会**让识图自动换档——实机验证过：
# 切到识图后 profile 仍是 qwen3.8-27b、别名仍是 CRACK、视频照样 400。
# 要让识图自动落在 9B，必须改加载路径（把 mode 的 profile 传进 `load()` 并贯穿命令行、
# 可执行文件、层数、窗口与进程记录），那是一次核心路径重构，需要重启才能验证。
MODE_PROFILE_SOURCE = {
    "text": "active",
    "vision": "active",
    # 语音也跟当前档位走（2026-09-23 晚修正）：原先这里写死 VOICE_PROFILE，于是选中 9B 时语音
    # 按钮直接不可点。实际能不能语音由档案自己的 `voice_context_size` 决定——9B 是 40960、
    # 三元 27B 是 8192、旧版 IQ4_XS 没有（14.25 GB 装不进 12 GB，配不上识别器）。
    "voice": "active",
}

# What the mode toolbar shows about each profile. Kept here beside the paths so a
# new model is described in exactly one place; the frontend renders whatever the
# gateway reports rather than carrying its own copy of these strings.
PROFILE_INFO = {
    "qwen3.8-27b": {
        "label": "27B 三元去审查",
        "tagline": "速度与智能兼得（Bonsai 2 三元压缩）",
        "size": "7.21 GB（三元 2.13 bpw）",
        "pros": [
            "速度与智能兼顾：三元压缩后能整份放进显卡",
            "显存占用小，适合和生图/视频任务来回切",
            "去审查，敏感题材的创作不容易被拒答",
            "文字、识图、工具调用都够用；语音对话也能用它（窗口 8K）",
        ],
        "cons": [
            "需要专用运行时（PrismML fork），不是原版 llama.cpp",
            "没有 MTP 草稿头，投机解码用不了",
            "上下文窗口比旧版小，长文要分段喂",
            "中文精确字数控制不稳定（要求 12 字可能给 9 字）",
        ],
        # 0-5, drawn for the comparison bars in the model panel. Intelligence and
        # speed are measured (or derived from measurements) on this machine.
        "scores": {"智能": 4, "速度": 5, "中文": 5, "识图": 5, "无审查": 5},
    },
    # 所有者要求保留的旧版 27B：知识面最强者，作为可选档留在选择器里。
    "qwen3.8-27b-iq4xs": {
        "label": "27B 旧版 IQ4_XS",
        "tagline": "知识面最强，慢 4.8 倍（保留档）",
        "size": "14.25 GB（=13.27 GiB，IQ4_XS 混合精度 + imatrix 标定）",
        "pros": [
            "知识面最广，难题上更稳",
            "中文写作与指令遵循都很好",
            "带 MTP 草稿头，打开后可再快一些",
            "来路清楚：官方模型 → 去审查微调 → imatrix 标定量化",
        ],
        "cons": [
            "装不进 12 GB 显存，要留一部分层在内存里，所以慢得多",
            "权重放在归档目录（C:\\_archive\\qwen-27b\\），需要时从那里读",
            "上下文窗口中等，长对话要靠压缩",
        ],
        "scores": {"智能": 5, "速度": 2, "中文": 5, "识图": 5, "无审查": 5},
    },
    "qwen3.5-9b-uncensored": {
        "label": "9B 无审查",
        "tagline": "最省显存、最快；把显卡让给 ComfyUI 时选它",
        "size": "6.85 GB（Q6_K）",
        "pros": [
            "最省显存、最快：要把显卡让给生图/视频时选它",
            "整份权重都在显存里，切回来几乎不用等",
            "原生识图，能读图中的文字与形状",
            "无审查（社区去审查版）",
        ],
        "cons": [
            "知识面不如 27B，难题上更容易出错",
            "上下文 32K（再大就溢出显存）",
            "没有 MTP 投机解码",
            "语音对话能用它（窗口 40K），但语音档位跟着当前模型走，"
            "别的档位选了就切过去",
        ],
        "scores": {"智能": 3, "速度": 5, "中文": 4, "识图": 5, "无审查": 5},
    },
}


def public_profiles():
    """Every selectable profile with its availability and trade-offs."""
    return [
        {
            "name": name,
            "label": info.get("label", name),
            "tagline": info.get("tagline", ""),
            "size": info.get("size", ""),
            "pros": info.get("pros", []),
            "cons": info.get("cons", []),
            "scores": info.get("scores", {}),
            "alias": profile["alias"],
            "installed": profile["model"].is_file() and profile["mmproj"].is_file(),
            "context_size": profile.get("context_size", 0),
            "gpu_layers": profile.get("gpu_layers", 0),
            "mtp": bool(profile.get("mtp")),
        }
        for name, profile in PROFILES.items()
        for info in (PROFILE_INFO.get(name, {}),)
    ]
DEFAULT_PORT = 8081
DEFAULTS = {
    # 生图：ComfyUI 队列里"没人真正在执行"的残留任务（GPU 长时间空闲）自动清掉，
    # 避免它永久挡住模式切换；判断依据是显存监控读到的 GPU 利用率。
    "comfy_clear_stale_on_switch": True,
    "comfy_autostart": True,
    "comfy_start_timeout": 300,
    # One backend for both text and vision. A mode switch used to restart llama-server:
    # measured 18.5 s, of which 8.5 s was the old server shutting down and 9.7 s was the
    # new one loading the 13.6 GB model again. A single server can carry the projector and
    # the MTP draft head at once (auditioned: modalities.vision=true and
    # speculative.types=none,draft-mtp), so with this on a switch only changes which slice
    # the gateway reports — no restart, no wait. Turn it off to go back to one process per
    # mode (the old behaviour, e.g. to keep text at its own layer count).
    "unified_backend": True,
    "default_mode": "text",
    "gpu_layers": 40,
    "threads": 12,
    "threads_batch": 16,
    "context_size": 8192,
    "sleep_idle_seconds": 300,
    "mtp_enabled": False,
    "mtp_draft_tokens": 2,
    "mtp_reason": "MTP is pending local validation.",
    "agent_enabled": True,
    "context_compression_enabled": True,
    "cache_ram_mib": 0,
    "context_checkpoints": 2,
    "checkpoint_min_step": 8192,
    "server_timeout_seconds": 3600,
    # Generate view: start ComfyUI on demand and how long to wait for it.
    "comfy_autostart": True,
    "comfy_start_timeout": 300,
}


def read_settings():
    settings = DEFAULTS.copy()
    if SETTINGS_PATH.exists():
        # utf-8-sig, not utf-8: this file is meant to be edited by hand, and Notepad writes a
        # BOM. A BOM is not a JSON document, so plain utf-8 made the whole app fail to import
        # (json.JSONDecodeError at line 1) — one invisible byte in a user-editable file was
        # enough to take the backend down. Reading it tolerantly costs nothing.
        settings.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig")))
    if settings["default_mode"] not in ("text", "vision", "image"):
        raise ValueError("default_mode must be text, vision, or image")
    for name, low, high in (
        ("gpu_layers", 0, 64), ("threads", 1, 32),
        ("threads_batch", 1, 32), ("mtp_draft_tokens", 1, 8),
        ("sleep_idle_seconds", 60, 86400),
        ("context_size", 512, 262144),
        ("cache_ram_mib", 0, 8192),
        ("context_checkpoints", 0, 32),
        ("checkpoint_min_step", 0, 262144),
        ("server_timeout_seconds", 600, 86400),
        ("comfy_start_timeout", 30, 1800),
    ):
        if type(settings[name]) is not int or not low <= settings[name] <= high:
            raise ValueError(f"Invalid setting: {name}")
    for name in ("mtp_enabled", "agent_enabled", "context_compression_enabled", "comfy_autostart",
                 "unified_backend"):
        if type(settings[name]) is not bool:
            raise ValueError(f"{name} must be a boolean")
    for name in ("text_gpu_layers", "vision_gpu_layers"):
        if name in settings and (type(settings[name]) is not int or not 0 <= settings[name] <= 64):
            raise ValueError(f"Invalid setting: {name}")
    return settings


SHARE_ENV_NAMES = {
    "access_token": "QWEN_ACCESS_TOKEN",
    "allowed_hosts": "QWEN_ALLOWED_HOSTS",
    "allowed_origins": "QWEN_ALLOWED_ORIGINS",
    "listen_host": "QWEN_LISTEN_HOST",
    "listen_port": "QWEN_LISTEN_PORT",
}

# Decided at import, before any library can import ``unittest`` on our behalf (see
# tests_are_running for what a live re-check cost).
_TESTS_AT_IMPORT = ("unittest" in sys.modules
                    or any("unittest" in arg or "pytest" in arg for arg in sys.argv))


def tests_are_running():
    """Whether this process is a test run.

    The sharing overlay is discovered by a conventional path, which is what lets
    the watchdog find the passphrase the gate is enforcing. That same convention
    would make every test suite adopt the machine's live sharing configuration and
    start demanding a passphrase it never logs in with, so a test process ignores
    the overlay unless it points at one explicitly with QWEN_SHARE_CONFIG.

    Setting QWEN_TEST_ISOLATION=0 turns the guard off, which is how the suite can
    still test the conventional-path behaviour itself.

    The answer is decided **once, at import**, and that part is not cosmetic: a live gateway
    that imports ``unittest`` later — any third-party library may, torch does, and the launch
    path pulls torch in for the GPU readout — used to flip this predicate to True *while
    running*. Every sharing setting is resolved through it, so such a process silently lost
    its own allowlist and listen host and began answering every shared address with
    ``400 This address is not shared by this Qwen service``. Measured on a gateway started by
    hand: the startup log listed ``0.0.0.0, *.trycloudflare.com, 192.168.1.10``, and the same
    call after the runtime import returned only the loopback names. A launcher that sets
    QWEN_SHARE_CONFIG never noticed, because that path wins before this guard is consulted;
    the trap is for every other way of starting the service.
    """
    if os.environ.get("QWEN_TEST_ISOLATION") == "0":
        return False
    return _TESTS_AT_IMPORT


def sharing_is_off():
    """Whether this start was explicitly scoped to local use.

    The launchers set QWEN_SHARE_SCOPE to "local" for the plain start (the original
    Start-Qwen.bat entry point) and to "share" for the sharing launcher. That is a
    marker rather than "blank the variables", because the service seeds its live
    passphrase from whatever configuration it can find: with the overlay merely
    hidden, a leftover passphrase could still leave the gate switched on in a
    session the owner started for local use only.
    """
    return os.environ.get("QWEN_SHARE_SCOPE", "").strip().lower() == "local"


def share_overlay():
    """The sharing overlay: QWEN_SHARE_CONFIG, else the conventional file.

    Falling back to the conventional path matters because more than the server
    resolves settings this way — the watchdog does too, and a process that never
    received the environment variable still has to find the passphrase that the
    gate is enforcing.

    A variable that is *set but empty* means "no overlay at all". That distinction
    is what lets a caller (a test, or a deliberately local-only launch) switch
    sharing off completely even though a share.config.json exists on disk.
    """
    if "QWEN_SHARE_CONFIG" in os.environ:
        configured = os.environ["QWEN_SHARE_CONFIG"].strip()
        if not configured:
            return None
        candidates = [Path(configured)]
    elif sharing_is_off() or tests_are_running():
        return None
    else:
        candidates = [ROOT / "share.config.json"]
    for path in candidates:
        try:
            if path.is_file():
                return path
        except (OSError, ValueError):
            continue
    return None


def share_setting(name, settings=None):
    """Sharing value: environment, then the overlay file, then local settings.

    As above, an environment variable that is set but empty means "unset on
    purpose": it hides the overlay for this process instead of falling through to
    it. A local-only start hides it too, whatever the environment says.
    """
    environ_name = SHARE_ENV_NAMES.get(name, f"QWEN_{name.upper()}")
    if sharing_is_off():
        return ""
    if environ_name in os.environ:
        from_environment = os.environ[environ_name].strip()
        if not from_environment:
            local = read_settings() if settings is None else settings
            value = local.get(name, "") if isinstance(local, dict) else ""
            return value.strip() if isinstance(value, str) else value
        return from_environment
    overlay_path = share_overlay()
    if overlay_path is not None:
        try:
            overlay = json.loads(overlay_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            overlay = None
        if isinstance(overlay, dict) and overlay.get(name) not in (None, ""):
            value = overlay[name]
            return value.strip() if isinstance(value, str) else value
    local = read_settings() if settings is None else settings
    value = local.get(name, "") if isinstance(local, dict) else ""
    return value.strip() if isinstance(value, str) else value


def gateway_port(settings=None):
    """Port the browser gateway listens on, shared by the server and its watchdog.

    The launcher probes and restarts the gateway by port, so both sides have to
    resolve it the same way: ``QWEN_GATEWAY_PORT`` wins outright, then the sharing
    override over settings.json, and an unusable value falls back to the default
    rather than breaking the watchdog.

    The dedicated variable exists because ``QWEN_LISTEN_PORT`` belongs to the
    sharing overlay: a local-only start (``QWEN_SHARE_SCOPE=local``) hides that
    overlay on purpose, which used to make the port impossible to move for a test
    instance or a second copy — it silently took 8081 and collided with whatever
    was already serving there.
    """
    override = os.environ.get("QWEN_GATEWAY_PORT", "").strip()
    if override:
        try:
            override_port = int(override)
        except ValueError:
            override_port = 0
        if 1 <= override_port <= 65535:
            return override_port
    try:
        port = int(str(share_setting("listen_port", settings)).strip())
    except (TypeError, ValueError):
        return DEFAULT_PORT
    return port if 1 <= port <= 65535 else DEFAULT_PORT


def active_profile_name(settings=None):
    """Which profile is selected, falling back to the default when unset/unknown.

    An unknown name is not an error: settings.json is hand-edited, and a typo must
    degrade to the working model rather than stop the service from starting.
    """
    settings = read_settings() if settings is None else settings
    name = str(settings.get("model_profile") or "").strip()
    return name if name in PROFILES else DEFAULT_PROFILE


# 换权重的窗口标记：网关在"故意换后端"期间回答不了探活，监督进程据此不把它当成卡死。
# 截止时间写在文件里，所以一个真的卡死（标记清不掉）最多宽限 SWITCH_GRACE_SECONDS 就会被照常重启。
SWITCH_MARKER = ROOT / "logs" / "manager.switching"
SWITCH_GRACE_SECONDS = 300


def mark_switching(seconds: int = SWITCH_GRACE_SECONDS) -> None:
    """记下"我正在换权重"，直到 now+seconds。写失败不影响换档本身。"""
    try:
        SWITCH_MARKER.parent.mkdir(parents=True, exist_ok=True)
        SWITCH_MARKER.write_text(f"{{'until': {time.time() + seconds:.0f}}}".replace("'", '"'),
                                 encoding="ascii")
    except OSError:
        pass


def clear_switching() -> None:
    try:
        SWITCH_MARKER.unlink(missing_ok=True)
    except OSError:
        pass


def switching_now() -> bool:
    """此刻是否在一个未过期的换档窗口里。"""
    try:
        raw = SWITCH_MARKER.read_text(encoding="ascii")
        return time.time() < float(json.loads(raw).get("until") or 0)
    except (OSError, ValueError, TypeError):
        return False


def profile_label(name):
    """One profile's human label, whatever profile it is.

    Voice mode needs this for the档位 it is *about to* serve, which is not necessarily the
    selected one at the moment it asks.
    """
    return (PROFILE_INFO.get((name or "").strip()) or {}).get("label") or (name or "")


def active_profile_label(settings=None):
    """The selected profile's human label, for anything that names the model to the owner.

    Lives here so a panel cannot carry its own copy: the prompt enhancer of the image and video
    queues reports the model it will actually use, and a hard-coded "Qwen3.8-27B" string there
    was wrong the moment the 9B was selected.
    """
    return profile_label(active_profile_name(settings))


def voice_context(settings=None):
    """The context window voice mode runs with, for the selected profile.

    Smaller than the text/vision window on purpose. Voice mode always adds its recogniser
    (~1.1 GiB measured), and it replays at most eight short turns, so the large window it
    would otherwise inherit buys nothing and spends the margin a spoken turn needs.
    Profiles that do not declare one fall back to their normal window.
    """
    settings = read_settings() if settings is None else settings
    if active_context_key(settings):
        return int(settings["context_size"])
    profile = PROFILES[active_profile_name(settings)]
    # The profile owns this number whenever it declares one: a value in settings.json was
    # tuned for one model, and letting it win would validate a 9B window against the 27B's.
    return int(profile.get("voice_context_size") or profile.get("context_size")
               or settings["context_size"])


def active_paths(settings=None):
    """The model and mmproj paths of the selected profile."""
    profile = PROFILES[active_profile_name(settings)]
    return profile["model"], profile["mmproj"]


def active_executable(settings=None):
    """The llama-server binary the selected profile must run on.

    The ternary Bonsai profile needs the PrismML fork; the Qwen GGUF profiles keep the
    stock build they were validated on. Building a command line could hard-code one
    binary, but the backend-adoption check has to compare against the binary the profile
    actually uses — otherwise a surviving Prism backend is rejected as a foreign process.
    """
    profile = PROFILES[active_profile_name(settings)]
    return Path(profile.get("executable") or EXECUTABLE)


def active_context_key(settings=None):
    """Whose window ``context_size`` describes, or None when the profile's own applies.

    The number belongs to the model: a value tuned for one profile must not be validated
    against another, which is why the profile normally wins. A caller that genuinely supplies
    its own window — voice mode running smaller on the same weights, a hand-written
    configuration, the context tests — says so through ``context_key``, because otherwise its
    value would be ignored in silence, which is exactly what happened to the voice window.

    The marker is a key in the settings themselves rather than a comparison against
    ``DEFAULTS``: a default value is still a value, and only the *presence* of the key can
    tell "someone asked for this number" from "nobody mentioned it".
    """
    settings = read_settings() if settings is None else settings
    return "context_key" in settings


def thinking_levels(settings=None):
    """Which thinking tiers the selected profile can actually express.

    Read out of each model's own `tokenizer.chat_template` during the swap rather than guessed:
    both 27B profiles accept `reasoning_effort` `low` / `medium` / `xhigh` (default `xhigh`)
    and their template *raises* on any other value, so the gateway must never forward one
    outside that tuple. The 9B's template has no `reasoning_effort` at all, so for it thinking
    is only off or on.
    """
    settings = read_settings() if settings is None else settings
    efforts = PROFILES[active_profile_name(settings)].get("thinking_efforts", ())
    return ("off",) + (tuple(efforts) if efforts else ("on",))


def expected_context(settings=None):
    """The context size the selected profile actually launches with.

    The profile wins over settings.json for the same reason the model path does: a
    number tuned for one model must not be validated against a different one. A caller that
    supplied its own ``context_size`` *and* named itself in ``context_key`` gets that value —
    which is how voice mode runs a smaller window on the same weights.
    """
    settings = read_settings() if settings is None else settings
    if active_context_key(settings):
        return int(settings["context_size"])
    profile = PROFILES[active_profile_name(settings)]
    return profile.get("context_size", settings["context_size"])


def active_gpu_layers(settings=None, mode=None):
    """How many layers the selected profile offloads, for a given mode.

    Kept beside the launch logic so the number the panel reports is by construction
    the number the command line uses. The per-mode overrides in settings.json were
    tuned for the 27B, so they apply only while the default profile is selected;
    otherwise a second model would inherit them and leave layers on the CPU.
    """
    settings = read_settings() if settings is None else settings
    profile = PROFILES[active_profile_name(settings)]
    if mode in ("text", "vision") and active_profile_name(settings) == DEFAULT_PROFILE:
        override = settings.get(f"{mode}_gpu_layers")
        if isinstance(override, int):
            return override
    return profile.get("gpu_layers", settings["gpu_layers"])


def mtp_capable(settings=None):
    """Whether the selected profile ships a draft head at all.

    The draft head is a property of the model, not of the switch: only some profiles
    carry one, and asking llama-server for MTP on a model without it fails the launch
    outright. Callers that need to *validate* a running backend (rather than build its
    command line) must ask this, or they will demand a flag the model can never set.
    """
    settings = read_settings() if settings is None else settings
    return bool(PROFILES[active_profile_name(settings)].get("mtp"))


def expected_mtp(settings=None, mode=None, *, unified=None):
    """The MTP state a backend launched for this profile and mode must report.

    A profile without a draft head can only ever read False, so that is the honest
    expectation rather than an unenforceable None. Callers validating a *running*
    backend should still consult :func:`mtp_capable` first, because a False that was
    forced by the model says nothing about whether the switch was honoured.
    """
    settings = read_settings() if settings is None else settings
    if not mtp_capable(settings):
        return False
    if unified is None:
        unified = bool(settings.get("unified_backend", False))
    return bool(settings["mtp_enabled"]) and (mode == "text" or unified)


def mode_requires_profile(mode, settings=None):
    """Which profile a mode needs resident, or None when it restarts nothing.

    ``None`` means the mode does not run on these weights at all (image and video are
    ComfyUI's), so it says nothing about which profile should be loaded.

    ``settings`` is threaded through rather than re-read from disk: the caller that owns a
    backend is the authority on which profile that backend is for, and a hidden re-read
    here once made a test's injected settings disagree with the file on disk.
    """
    source = MODE_PROFILE_SOURCE.get(mode)
    if source is None:
        return None
    return active_profile_name(settings) if source == "active" else source


def same_source(mode, *, launched=None, settings=None):
    """Whether entering ``mode`` reuses the weights that are already on the card.

    Same source means the mode change is a *plugin* change: the process, its context and
    its weights stay exactly as they are, and only the mode label (plus whatever the mode
    adds — a projector, a recogniser) differs. Different source means the weights
    themselves change, and the backend has to be replaced.

    ``launched`` is the profile the resident backend was actually started for. Without it
    the selected profile is used, which is what a caller that has no backend yet wants.
    """
    wanted = mode_requires_profile(mode, settings)
    if wanted is None:
        return False
    resident = launched or active_profile_name(settings)
    return wanted == resident


def source_switch(mode, *, launched=None, current_mode=None, settings=None, target=None):
    """Describe what entering ``mode`` costs, for the API and for the log.

    One place answers "does this reload the model?" so the gateway, the panel and the
    tests cannot disagree about it — the previous hardcoded per-mode policy is exactly
    how the 9B ended up being asked for MTP it does not have. ``current_mode`` is accepted
    for callers that want to phrase the answer relative to where they came from, and is
    not needed to decide the answer itself.

    ``target`` names the profile the caller explicitly wants (语音现在跟着请求里说的那一份走，
    而不是 settings 里当前选中的那一份）。不传就按模式自己的规则解析。
    """
    wanted = (target or "").strip() or mode_requires_profile(mode, settings)
    resident = launched or active_profile_name(settings)
    if wanted is None:
        return {
            "same_source": False, "reload": True, "profile": None, "resident": resident,
            "reason": f"{mode} 模式由 ComfyUI 承载另一套模型，换模式必然会换模型。",
        }
    if wanted == resident:
        return {
            "same_source": True, "reload": False, "profile": wanted, "resident": resident,
            "reason": f"{mode} 模式与常驻后端同源（{wanted}），只切换插件，不重新加载模型。",
        }
    return {
        "same_source": False, "reload": True, "profile": wanted, "resident": resident,
        "reason": f"{mode} 模式需要 {wanted}，当前常驻的是 {resident}，属于换底模：整套切换。",
    }


def server_command(mode, port=8082, settings=None, *, mtp=None, mmproj=None):
    """The llama-server command line for one mode.

    ``mtp`` and ``mmproj`` default to the per-mode policy (text gets the draft head, vision
    gets the projector) but can be set explicitly, which is how the gateway can audition or
    run a single backend that serves both.
    """
    if mode not in ("text", "vision"):
        raise ValueError("Unknown Qwen mode")
    settings = read_settings() if settings is None else settings
    profile = PROFILES[active_profile_name(settings)]

    # GPU layers and context come from the shared accessors above, so the panel's
    # reported numbers cannot drift from the ones this command line uses.
    gpu_layers = active_gpu_layers(settings, mode)
    context = expected_context(settings)

    command = [
        str(active_executable(settings)), "-m", str(profile["model"]),
        "--host", "127.0.0.1", "--port", str(port),
        "--alias", profile["alias"], "-c", str(context), "-np", "1",
        "-ngl", str(gpu_layers),
        "-b", "256", "-ub", "128", "-t", str(settings["threads"]),
        "-tb", str(settings["threads_batch"]),
        "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0", "--fit", "off",
        "--jinja", "--reasoning", "off", "--reasoning-effort", "low",
        "--no-reasoning-preserve", "--temp", "0.7", "--top-p", "0.9",
        "--top-k", "20", "--cache-prompt", "--offline",
        "--cache-ram", str(settings.get("cache_ram_mib", 0)),
        "--ctx-checkpoints", str(settings.get("context_checkpoints", 2)),
        "--checkpoint-min-step", str(settings.get("checkpoint_min_step", 8192)),
        "--timeout", str(settings.get("server_timeout_seconds", 3600)),
        "--cors-origins", "localhost",
        "--sleep-idle-seconds", str(settings["sleep_idle_seconds"]),
        "--log-timestamps",
    ]
    if mmproj if mmproj is not None else (mode == "vision"):
        command += ["--mmproj", str(profile["mmproj"]), "--no-mmproj-offload"]
    if settings["agent_enabled"]:
        command += ["--agent"]
    # Only a profile that actually ships a draft head may request MTP; asking for
    # one the model lacks fails the launch rather than degrading quietly.
    # Per-mode default: the draft head used to be a text-only arrangement. It is passed
    # explicitly when one process should serve both slices (see unified_backend).
    requested = (settings["mtp_enabled"] and mode == "text") if mtp is None else bool(mtp)
    if requested and profile.get("mtp"):
        command += [
            "--spec-type", "draft-mtp", "--spec-draft-n-max",
            str(settings["mtp_draft_tokens"]), "--spec-draft-p-min", "0",
        ]
    else:
        # No draft head, but part of the same effect is free: ngram speculation drafts from
        # repeated n-grams of the context instead of from a second model, so it costs no extra
        # weights. Measured on the ternary 27B (b10754, same command line, only this flag
        # changed): a repeat-and-continue prompt 31.4 -> 57.4 t/s (+82.8%), a natural explanation
        # prompt 31.7 -> 31.7 t/s (no change), both outputs byte-identical to the run without it.
        # So "none" became "ngram-mod": it helps exactly when the answer echoes text the model has
        # already seen, and it does not change what is said when it does not.
        command += ["--spec-type", "ngram-mod"]
    return command
