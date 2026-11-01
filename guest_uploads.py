"""Who may upload, and which clips a shared visitor is allowed to see.

Opening the clip upload to invited visitors is a deliberate owner decision, so the
default here is closed: the switch lives in one small JSON file the owner flips from
the sharing panel, and every route asks this module instead of guessing.

Two things this module exists to prevent, both of which the video routes had no
protection against while uploading was owner-only:

* **Cross-visitor visibility.** ``/local/video/source`` used to list every file in
  ComfyUI's input folder, which is where every visitor's upload lands. Each upload is
  therefore recorded against the session that made it, and a visitor lists (and may
  select) only their own.
* **Disk exhaustion.** A visitor could otherwise fill the owner's disk with clips.
  Uploads are counted per session and in total, and a floor of free space is kept.

The record holds a file name, a byte count and the owning session token — never file
content, and never anything about what a visitor typed.
"""

import json
import logging
import os
from pathlib import Path
import shutil
import threading
import time

LOGGER = logging.getLogger("qwen.video")

SCHEMA_VERSION = 1
# "off"    — only the machine's owner may upload (the original behaviour)
# "guests" — any logged-in shared device may upload clips and download its own
POLICIES = ("off", "guests")
DEFAULT_POLICY = "off"

OWNER_SESSION = "__owner__"
OWNER_LABEL = "本机管理"

# Ceilings per session, and for all shared visitors together. A clip is 10-100 MB in
# practice, so these allow real work while keeping one visitor from filling the disk.
GUEST_SESSION_BYTES = 4 * 1024 ** 3
GUEST_TOTAL_BYTES = 20 * 1024 ** 3
OWNER_SESSION_BYTES = 64 * 1024 ** 3
# Refuse an upload rather than leave the machine with no room to render into.
RESERVE_BYTES = 5 * 1024 ** 3

FILE_NAME = "guest-uploads.json"


def human_bytes(value):
    """Compact byte count for the panel and for refusal messages."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


def default_directory():
    """Where the switch and the ownership records live.

    Resolved late and through the config module on purpose: ``qwen_config.ROOT`` is
    patched by the test suite, and a path captured at import time would keep pointing
    at the machine's live files.
    """
    import qwen_config

    return Path(qwen_config.ROOT) / "logs"


class GuestUploads:
    """The upload switch plus per-file ownership and per-session byte counts."""

    def __init__(self, directory=None, directory_hook=None, clock=time.time):
        self._directory_hook = directory_hook
        self._directory = Path(directory) if directory is not None else None
        self.clock = clock
        self.lock = threading.RLock()
        self.policy = DEFAULT_POLICY
        self.policy_changed_at = 0.0
        # file name -> {"session", "bytes", "uploaded_at"}
        self.records = {}
        self._loaded = False

    # ------------------------------------------------------------------ storage
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
            if self.policy not in POLICIES:
                self.policy = DEFAULT_POLICY
            try:
                state = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return
            except (OSError, ValueError) as exc:
                # A damaged record must not stop the panel from working; the worst
                # case is that a visitor's earlier clips stop being listed.
                LOGGER.warning("Upload record unreadable (%s); starting empty", exc)
                return
            if not isinstance(state, dict):
                return
            if state.get("policy") in POLICIES:
                self.policy = state["policy"]
            self.policy_changed_at = float(state.get("policy_changed_at") or 0.0)
            files = state.get("files")
            if isinstance(files, dict):
                for name, entry in files.items():
                    if isinstance(name, str) and isinstance(entry, dict):
                        self.records[name] = entry

    def _save(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": SCHEMA_VERSION,
            "policy": self.policy,
            "policy_changed_at": self.policy_changed_at,
            "files": self.records,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        target = self.path
        temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------- policy
    def get_policy(self):
        self._ensure_loaded()
        return self.policy

    def guests_may_upload(self):
        """Whether a shared visitor may store material.

        The decision now lives with the other guest features (``guest_policy``: chat / media /
        switch / storage) instead of in a switch of its own, so the sharing panel shows one set of
        permissions rather than two overlapping ones. This method stays as the name the upload
        routes already ask.
        """
        import guest_policy

        return guest_policy.allows("storage")

    def set_policy(self, policy):
        if policy not in POLICIES:
            raise ValueError(f"未知的上传策略：{policy}")
        with self.lock:
            self._ensure_loaded()
            if policy != self.policy:
                self.policy = policy
                self.policy_changed_at = self.clock()
                self._save()
                LOGGER.info("Sharing: clip upload policy is now %s", policy)
        return self.policy

    # ------------------------------------------------------------------ records
    def note_upload(self, name, session, size):
        with self.lock:
            self._ensure_loaded()
            self.records[name] = {"session": session, "bytes": int(size or 0),
                                  "uploaded_at": self.clock()}
            self._save()

    def owner_of(self, name):
        self._ensure_loaded()
        entry = self.records.get(str(name))
        if isinstance(entry, dict):
            session = entry.get("session")
            return session if isinstance(session, str) else None
        return None

    def is_visible_to(self, name, identity):
        """Whether this caller may learn that `name` exists."""
        if identity.is_owner:
            return True
        return self.owned_by(name, identity.token)

    def owned_by(self, name, session):
        """Whether one session's records cover this file (an upload, or a produced result)."""
        owner = self.owner_of(name)
        return owner is not None and owner == session

    def visible_videos(self, names, identity):
        return [name for name in names if self.is_visible_to(name, identity)]

    def session_bytes(self, session):
        self._ensure_loaded()
        return sum(int(entry.get("bytes") or 0) for entry in self.records.values()
                   if isinstance(entry, dict) and entry.get("session") == session)

    def guest_bytes(self):
        self._ensure_loaded()
        return sum(int(entry.get("bytes") or 0) for entry in self.records.values()
                   if isinstance(entry, dict) and entry.get("session") not in (None, OWNER_SESSION))

    def usage(self):
        """Counters for the sharing panel."""
        self._ensure_loaded()
        guests = [name for name, entry in self.records.items()
                  if isinstance(entry, dict) and entry.get("session") not in (None, OWNER_SESSION)]
        return {
            "files": len(self.records),
            "guest_files": len(guests),
            "guest_bytes": self.guest_bytes(),
            "guest_total_limit": GUEST_TOTAL_BYTES,
        }

    def forget(self, names):
        """Drop records for files that no longer exist, so the counters stay honest."""
        with self.lock:
            self._ensure_loaded()
            changed = False
            for name in list(names):
                if name in self.records:
                    self.records.pop(name, None)
                    changed = True
            if changed:
                self._save()
            return changed

    def forget_session(self, session):
        """Called when a device is kicked: its records stop granting it anything."""
        with self.lock:
            self._ensure_loaded()
            dropped = [name for name, entry in self.records.items()
                       if isinstance(entry, dict) and entry.get("session") == session]
            for name in dropped:
                self.records.pop(name, None)
            if dropped:
                self._save()
            return dropped

    # ------------------------------------------------------------------- quotas
    def check_upload(self, identity, size, input_root):
        """Raise a (status, message) pair when this upload must be refused.

        Checked before a single byte is written, so a refusal never leaves a partial
        file behind.
        """
        size = int(size or 0)
        per_session = OWNER_SESSION_BYTES if identity.is_owner else GUEST_SESSION_BYTES
        used = self.session_bytes(identity.token)
        if used + size > per_session:
            return (413, f"本设备上传额度已用完：已用 {human_bytes(used)} / "
                         f"上限 {human_bytes(per_session)}，请联系机主清理或提高额度。")
        if not identity.is_owner:
            total = self.guest_bytes()
            if total + size > GUEST_TOTAL_BYTES:
                return (413, f"共享上传总空间已用完：已用 {human_bytes(total)} / "
                             f"上限 {human_bytes(GUEST_TOTAL_BYTES)}。")
        try:
            free = shutil.disk_usage(str(input_root)).free
        except OSError:
            free = None
        if free is not None and free - size < RESERVE_BYTES:
            return (507, f"本机磁盘剩余空间不足（剩 {human_bytes(free)}），已拒绝本次上传。")
        return None


# The live instance is bound by video_routes.install(); tests build their own.
_STORE = None
_SOURCES = {}


def store():
    """The process-wide store, created on first use.

    The path is resolved through a hook rather than captured here, so a patched
    ``qwen_config.ROOT`` (the test suite) is honoured instead of the machine's logs.
    """
    global _STORE
    if _STORE is None:
        _STORE = GuestUploads(directory_hook=default_directory)
    return _STORE


def set_store(value):
    global _STORE
    _STORE = value


def selected_source(session):
    """Which clip this caller last picked.

    Per session, because one shared global would let one visitor silently retarget the
    clip another visitor is about to process.
    """
    entry = _SOURCES.get(session)
    return entry["path"] if isinstance(entry, dict) else None


def select_source(session, path):
    _SOURCES[session] = {"path": path, "at": time.time()}
    return path


def forget_source(session):
    _SOURCES.pop(session, None)


def prune_sources(keep):
    """Drop selections belonging to sessions that no longer exist."""
    for session in list(_SOURCES):
        if session not in keep:
            _SOURCES.pop(session, None)
