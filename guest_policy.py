"""What a shared visitor is allowed to do, decided in one place by the owner.

Sharing hands a visitor the same origin the owner uses. The original posture was "the
passphrase buys you a chat box": uploading, generating, and switching modes were all
owner-only, and a visitor who clicked a mode tab was told "该操作仅限本机管理员". The owner
has since asked for the opposite — a visitor should be able to *use the machine*, including
switching modes (and with them the model and its plugins) so a shared session is not a
read-only view of whatever the owner happened to load.

So permissions became a small, explicit, owner-controlled feature set instead of a hard
owner/guest split:

    chat     — talking to the model: text, vision, and the voice pairing
    media    — image and video generation, and the post-processing of a caller's own files
    switch   — mode switching, which is also what swaps the model and its projector/plugin
    storage  — uploads and downloads, each caller confined to their own material

Everything a visitor creates is filed against the visitor's own session token, and their
outputs are written under their own folder (``guests/<token>/``), so "their own storage" is a
physical fact, not just a record lookup.

**Still owner-only, deliberately**, and not because a visitor could not be trusted with the
*feature*: kicking a device, rotating the passphrase, taking the service or the tunnel down,
reading the usage/routing ledgers, preempting the card, and deleting the owner's history or
saved workflows. Those are the host's own controls — a visitor who could rotate the passphrase
or stop the service would be holding the machine's keys rather than using it.

The defaults are on (the owner asked for this), the file is one small JSON document next to the
other sharing state, and the sharing panel flips them. Turning a feature off takes effect on the
next request — no restart — because every route asks this module rather than caching a decision.
"""

import json
import logging
import os
from pathlib import Path
import threading
import time

LOGGER = logging.getLogger("qwen.share")

SCHEMA_VERSION = 1

# Ordered so the panel lists them the way the sentence reads: talk, make things, change what is
# loaded, keep your own files.
FEATURES = ("chat", "media", "switch", "storage")

LABELS = {
    "chat": "对话与语音",
    "media": "生图与生成视频",
    "switch": "模式/模型切换",
    "storage": "上传与下载",
}

DESCRIPTIONS = {
    "chat": "访客可以和模型对话（文字、识图），也能用语音页与本地识别、合成。",
    "media": "访客可以提交生图与生成视频任务，并处理自己那份文件（降噪、补帧、拼接）。",
    "switch": "访客可以自己切模式，包括随之切换模型与投影/识别插件；机主正在跑的任务不会被抢。",
    "storage": "访客可以上传素材、下载自己生成与上传的文件；每个访客只看得见自己的那份。",
}

# Refusal wording says what would fix it, because the caller cannot flip the switch themselves.
REFUSALS = {
    "chat": "机主尚未开放访客对话（文字/识图/语音）。",
    "media": "机主尚未开放访客生图与生成视频。",
    "switch": "机主尚未开放访客切换模式与模型。",
    "storage": "机主尚未开启访客上传与下载。",
}

DEFAULT_FEATURES = {name: True for name in FEATURES}

FILE_NAME = "guest-policy.json"
# The old single-purpose upload switch, read once so an owner who had already opened uploads
# keeps them open instead of silently losing that decision to a new default.
LEGACY_FILE_NAME = "guest-uploads.json"


def default_directory():
    """Where the switch lives; resolved late so a patched ROOT (tests) is honoured."""
    import qwen_config

    return Path(qwen_config.ROOT) / "logs"


class GuestPolicy:
    """The feature switches, loaded from and saved to one small JSON document."""

    def __init__(self, directory=None, directory_hook=None, clock=time.time, features=None):
        self._directory = Path(directory) if directory is not None else None
        self._directory_hook = directory_hook
        self.clock = clock
        self.lock = threading.RLock()
        self.features = dict(features or DEFAULT_FEATURES)
        self.changed_at = 0.0
        self._loaded = False

    @property
    def directory(self):
        if self._directory is not None:
            return self._directory
        if self._directory_hook is not None:
            return Path(self._directory_hook())
        return default_directory()

    @property
    def path(self):
        return self.directory / FILE_NAME

    def _ensure_loaded(self):
        if self._loaded:
            return
        with self.lock:
            if self._loaded:
                return
            self._loaded = True
            state = None
            try:
                state = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                state = None
            except (OSError, ValueError) as exc:
                LOGGER.warning("Guest policy unreadable (%s); keeping the defaults", exc)
            if isinstance(state, dict):
                stored = state.get("features")
                if isinstance(stored, dict):
                    for name in FEATURES:
                        if name in stored:
                            self.features[name] = bool(stored[name])
                self.changed_at = float(state.get("changed_at") or 0.0)
                return
            # No policy file yet: the old single-purpose upload switch was an explicit owner
            # decision about exactly one of these features, so it is honoured either way. The
            # other three start from the defaults (open) because the owner asked for a shared
            # session that can actually use the machine.
            legacy = self.directory / LEGACY_FILE_NAME
            try:
                old = json.loads(legacy.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            if isinstance(old, dict) and old.get("policy") in ("guests", "off"):
                self.features["storage"] = old["policy"] == "guests"
                self.changed_at = float(old.get("policy_changed_at") or 0.0)
                self._save()

    def _save(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {"version": SCHEMA_VERSION, "features": self.features,
                   "changed_at": self.changed_at}
        target = self.path
        temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------ reading
    def allows(self, feature):
        if feature not in FEATURES:
            raise ValueError(f"未知的访客能力：{feature}")
        self._ensure_loaded()
        return bool(self.features.get(feature))

    def allows_any(self, *features):
        """For a route that serves two kinds of file: an upload, or a generated result.

        ``/local/video/file`` hands back both a clip a visitor uploaded (storage) and the film a
        visitor generated (media), so requiring both switches would refuse a caller who has every
        right to the bytes.
        """
        return any(self.allows(feature) for feature in features)

    def snapshot(self):
        """What the panel shows: every switch, its wording, and what it opens."""
        self._ensure_loaded()
        return {
            "features": {name: bool(self.features.get(name)) for name in FEATURES},
            "labels": dict(LABELS),
            "descriptions": dict(DESCRIPTIONS),
            "changed_at": self.changed_at,
        }

    def refusal(self, feature):
        return REFUSALS.get(feature, "该功能对访客未开放。")

    # ------------------------------------------------------------------ writing
    def set_feature(self, feature, allowed):
        if feature not in FEATURES:
            raise ValueError(f"未知的访客能力：{feature}")
        with self.lock:
            self._ensure_loaded()
            allowed = bool(allowed)
            if self.features.get(feature) != allowed:
                self.features[feature] = allowed
                self.changed_at = self.clock()
                self._save()
                LOGGER.info("Sharing: guest feature %s is now %s", feature, allowed)
        return self.snapshot()

    def set_all(self, allowed):
        with self.lock:
            self._ensure_loaded()
            allowed = bool(allowed)
            if any(self.features.get(name) != allowed for name in FEATURES):
                for name in FEATURES:
                    self.features[name] = allowed
                self.changed_at = self.clock()
                self._save()
                LOGGER.info("Sharing: every guest feature is now %s", allowed)
        return self.snapshot()


# The live instance is bound at import time; tests build their own and bind it with set_policy().
_POLICY = None


def policy():
    global _POLICY
    if _POLICY is None:
        _POLICY = GuestPolicy(directory_hook=default_directory)
    return _POLICY


def set_policy(value):
    global _POLICY
    _POLICY = value


def allows(feature):
    return policy().allows(feature)


def allows_any(*features):
    return policy().allows_any(*features)


def snapshot():
    return policy().snapshot()


def refusal(feature):
    return policy().refusal(feature)
