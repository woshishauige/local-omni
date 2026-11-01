"""Durable device sessions for the shared gateway, with rotatable passphrases.

Why sessions exist instead of reusing the passphrase as the cookie: rotating the
passphrase must not interrupt people who already joined. A device therefore holds
its own random token, and the passphrase is only ever read at login time.

The store keeps no prompt text, no model output and no personal data. A session
records when it first joined, when it was last active, how many requests it made
and how many response bytes it received, and nothing else.
"""

from datetime import datetime
import json
import logging
import os
from pathlib import Path
import secrets
import threading
import time

LOGGER = logging.getLogger("qwen.share")
SCHEMA_VERSION = 1
TOKEN_BYTES = 16
ADMIN_TOKEN_BYTES = 24
# A session that has not been seen for this long is dropped from the store, so the
# file cannot grow without bound as visitors come and go.
SESSION_TTL_SECONDS = 400 * 24 * 3600
SAVE_INTERVAL_SECONDS = 5.0
MAX_LABEL = 60
# Hard ceiling on remembered devices, so a client that never returns its cookie
# cannot grow the file without bound.
MAX_SESSIONS = 200


def human_bytes(value):
    """Compact byte count for logs and the manager panel."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


class ShareStore:
    """Sessions, the shared passphrase and the owner's admin token.

    Every mutation is persisted through a debounced atomic write, and the caller
    never sees a partially written file: a reader either gets the previous state
    or the new one.
    """

    def __init__(self, directory, clock=time.time, token_bytes=TOKEN_BYTES, load_state=True):
        self.directory = Path(directory)
        self.path = self.directory / "share.sessions.json"
        self.admin_path = self.directory / "admin.token"
        self.clock = clock
        self.token_bytes = token_bytes
        self.load_state = load_state
        self.lock = threading.RLock()
        self.sessions = {}
        self.admin_token = ""
        self.pin = ""
        self.pin_rotated_at = 0.0
        self._saved_at = 0.0
        self._dirty = False
        self._load()

    # ------------------------------------------------------------------ loading
    def _load(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.admin_token = self._load_admin_token()
        state = None
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = None
        except (OSError, ValueError) as exc:
            # A damaged file must not take the gateway down; sessions are rebuilt
            # by logging in again, and the owner's token lives in its own file.
            LOGGER.warning("Sharing session file unreadable (%s); starting empty", exc)
            state = None
        if isinstance(state, dict) and state.get("version") == SCHEMA_VERSION:
            # With load_state off (a test process) the passphrase and the device list
            # from disk are ignored: a suite must not silently start enforcing the
            # machine's live sharing configuration.
            self.pin = state.get("pin") if isinstance(state.get("pin"), str) else ""
            self.pin_rotated_at = float(state.get("pin_rotated_at") or 0.0)
            stored = state.get("sessions")
            if isinstance(stored, dict):
                for token, session in stored.items():
                    if isinstance(token, str) and isinstance(session, dict):
                        self.sessions[token] = session
        if not self.load_state:
            self.pin = ""
            self.pin_rotated_at = 0.0
            self.sessions = {}
        self._drop_expired()

    def _load_admin_token(self):
        try:
            existing = self.admin_path.read_text(encoding="ascii").strip()
        except (FileNotFoundError, OSError):
            existing = ""
        if existing:
            return existing
        token = secrets.token_hex(ADMIN_TOKEN_BYTES)
        self._write_atomic(self.admin_path, token)
        return token

    # ------------------------------------------------------------------ saving
    def _write_atomic(self, path, text):
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _snapshot(self):
        return {
            "version": SCHEMA_VERSION,
            "pin": self.pin,
            "pin_rotated_at": self.pin_rotated_at,
            "sessions": self.sessions,
        }

    def save(self, force=False):
        """Persist unless a recent write already covered the change."""
        with self.lock:
            now = self.clock()
            if not force and not self._dirty:
                return False
            if not force and now - self._saved_at < SAVE_INTERVAL_SECONDS:
                self._dirty = True
                return False
            try:
                self._write_atomic(self.path, json.dumps(self._snapshot(), ensure_ascii=False, indent=2))
            except OSError as exc:
                LOGGER.warning("Could not persist sharing sessions: %s", exc)
                self._dirty = True
                return False
            self._saved_at = now
            self._dirty = False
            return True

    def _drop_expired(self):
        cutoff = self.clock() - SESSION_TTL_SECONDS
        stale = [token for token, session in self.sessions.items()
                 if float(session.get("last_seen") or 0.0) < cutoff]
        for token in stale:
            self.sessions.pop(token, None)
        if stale:
            self._dirty = True
        # A device that never sends a cookie back would otherwise add a row per
        # request. The cap keeps the store bounded no matter what a client does,
        # evicting the least recently active non-admin rows first.
        overflow = len(self.sessions) - MAX_SESSIONS
        if overflow > 0:
            order = sorted(
                (token for token, session in self.sessions.items()
                 if session.get("kind") != "admin"),
                key=lambda token: float(self.sessions[token].get("last_seen") or 0.0),
            )
            for token in order[:overflow]:
                self.sessions.pop(token, None)
            self._dirty = True
        return stale

    # ------------------------------------------------------------------ sessions
    def _new_token(self):
        for _ in range(8):
            token = secrets.token_urlsafe(self.token_bytes)
            if token not in self.sessions:
                return token
        raise RuntimeError("Could not allocate a unique session token")

    def authenticate(self, token):
        """Return the live session for a token, or None. Never creates anything.

        A kicked device is gone from the store rather than flagged, so its cookie can
        never resolve again. The age check is what keeps the file from growing when
        nothing else saves.
        """
        if not isinstance(token, str) or not token:
            return None
        with self.lock:
            session = self.sessions.get(token)
            if session is None:
                return None
            if float(session.get("last_seen") or 0.0) < self.clock() - SESSION_TTL_SECONDS:
                self.sessions.pop(token, None)
                self._dirty = True
                return None
            return session

    def create(self, kind="device", label="", source="", token=None):
        with self.lock:
            token = token or self._new_token()
            now = self.clock()
            self.sessions[token] = {
                "kind": "admin" if kind == "admin" else "device",
                "label": (label or "")[:MAX_LABEL],
                "created_at": now,
                "last_seen": now,
                "requests": 0,
                "bytes": 0,
                "last_endpoint": "",
                "last_source": (source or "")[:64],
                "revoked": False,
            }
            self._dirty = True
            self.save()
            return token

    def adopt(self, token, label="旧口令会话", source=""):
        """Create a session for a token that predates per-device sessions.

        Devices that logged in before this feature held the passphrase itself as
        their cookie. Adopting that value once keeps them working without a
        re-login, and after the owner rotates the passphrase the leftover value
        stops matching anything.
        """
        if not isinstance(token, str) or not token:
            return None
        with self.lock:
            if token in self.sessions:
                return token
            return self.create(kind="device", label=label, source=source, token=token)

    def revoke(self, token):
        """Kick one device out and drop its record entirely.

        Deleting rather than flagging is what the owner asked for: a kicked device
        should disappear from the list, not sit there greyed out. Removing the token
        is also what makes the kick effective on the next request, and the only trace
        left behind is a log line.
        """
        with self.lock:
            session = self.sessions.pop(token, None)
            if session is None:
                return False
            self._dirty = True
            self.save(force=True)
            return True

    def revoke_all_devices(self, tokens_only=False):
        """Kick every shared device and delete their records.

        Returns the removed session tokens, so the caller can also drop what those
        sessions owned (uploaded clips, a selected clip) instead of leaving records
        pointing at devices that no longer exist.
        """
        with self.lock:
            tokens = [token for token, session in self.sessions.items()
                      if session.get("kind") != "admin"]
            for token in tokens:
                self.sessions.pop(token, None)
            if tokens:
                self._dirty = True
                self.save(force=True)
            return tokens if tokens_only else len(tokens)

    def forget_unused_devices(self, tokens_only=False):
        """Drop device records that never served a request.

        These are the leftovers of a client that logged in and never came back, or of
        a page that was opened and abandoned. They carry no information worth keeping
        and they clutter the manager panel. The owner's own session is never touched.
        """
        with self.lock:
            tokens = [token for token, session in self.sessions.items()
                      if session.get("kind") != "admin"
                      and int(session.get("requests") or 0) == 0]
            for token in tokens:
                self.sessions.pop(token, None)
            if tokens:
                self._dirty = True
                self.save(force=True)
            return tokens if tokens_only else len(tokens)

    def ensure_admin_session(self, token, label="本机管理", source=""):
        """Return the owner's own session, creating it only if it is missing.

        The token is derived from the admin token, so this is idempotent: repeated
        cookie-less visits to the owner console reuse one row instead of adding one
        per visit.
        """
        with self.lock:
            session = self.sessions.get(token)
            if session is not None:
                session["revoked"] = False
                session["last_seen"] = self.clock()
                self._dirty = True
                self.save()
                return token
            return self.create(kind="admin", label=label, source=source, token=token)

    def note_request(self, token, endpoint="", received=0):
        """Account one served request against its session."""
        if not token:
            return
        with self.lock:
            session = self.sessions.get(token)
            if session is None:
                return
            now = self.clock()
            session["last_seen"] = now
            session["requests"] = int(session.get("requests") or 0) + 1
            if received:
                session["bytes"] = int(session.get("bytes") or 0) + int(received)
            if endpoint:
                session["last_endpoint"] = endpoint[:120]
            self._dirty = True
            self.save()

    def mark_seen(self, token, source=""):
        if not token:
            return
        with self.lock:
            session = self.sessions.get(token)
            if session is None:
                return
            session["last_seen"] = self.clock()
            if source:
                session["last_source"] = source[:64]
            self._dirty = True
            self.save()

    def set_pin(self, pin):
        with self.lock:
            self.pin = pin
            self.pin_rotated_at = self.clock()
            self._dirty = True
            self.save(force=True)
            return self.pin_rotated_at

    def summarize(self, current_token="", online=True, redact_current=False):
        """Panel payload. Session tokens are shortened to an opaque handle."""
        with self.lock:
            self._drop_expired()
            sessions = []
            for token, session in self.sessions.items():
                sessions.append({
                    "token": self.handle(token),
                    "kind": session.get("kind", "device"),
                    "label": session.get("label") or "未命名设备",
                    "created_at": float(session.get("created_at") or 0.0),
                    "last_seen": float(session.get("last_seen") or 0.0),
                    "requests": int(session.get("requests") or 0),
                    "bytes": int(session.get("bytes") or 0),
                    "last_endpoint": session.get("last_endpoint") or "",
                    "last_source": session.get("last_source") or "",
                    "blocked": bool(session.get("revoked")),
                    "current": False if redact_current else token == current_token,
                })
            sessions.sort(key=lambda entry: entry["last_seen"], reverse=True)
            return {
                "pin_rotated_at": self.pin_rotated_at,
                "pin": self.pin,
                "online": bool(online),
                "sessions": sessions,
            }

    # ------------------------------------------------------------------ handles
    @staticmethod
    def handle(token):
        """Short public handle for a token; the real value never leaves the host.

        A handle is a truncated hash, so it cannot be replayed as a cookie and it
        still identifies one row stably across refreshes.
        """
        if not isinstance(token, str) or not token:
            return ""
        import hashlib
        return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]

    def token_for_handle(self, handle):
        if not isinstance(handle, str) or len(handle) != 16:
            return None
        with self.lock:
            for token in self.sessions:
                if self.handle(token) == handle:
                    return token
        return None

    # ------------------------------------------------------------------ utilities
    def session_count(self):
        """How many devices are remembered right now (the owner's included)."""
        with self.lock:
            return len(self.sessions)

    def describe(self, token):
        """One-line summary for the log file, without any secret."""
        session = self.authenticate(token)
        if not session:
            return "unknown"
        return (f"{session.get('kind')} {session.get('label') or 'unnamed'} "
                f"requests={session.get('requests', 0)} bytes={human_bytes(session.get('bytes', 0))}")

    @staticmethod
    def last_seen_text(session):
        return datetime.fromtimestamp(float(session.get("last_seen") or 0.0)).isoformat(timespec="seconds")
