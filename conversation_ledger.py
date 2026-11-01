"""One history and one context meter for every way of talking to the model.

Typing, attaching a picture and speaking are three views of the same conversation, but they
used to keep three separate ideas of it: the chat app owned the transcript and its context
meter, voice mode kept an array of its own turns, and a spoken turn therefore vanished from
the written thread and never counted towards the window.

This module is the shared answer. It is keyed by the same conversation identifier the chat
app already sends (`X-Conversation-Id`), it records both written and spoken turns, and it
reports how much of the context window is left — measured with the running model's own
tokenizer, so the number matches what the model will actually be asked to read.

Everything here is best-effort and fail-open: a ledger that cannot be written must not break
a chat turn, so persistence errors are logged and dropped.
"""

import json
import logging
import os
import time
import uuid
from collections import OrderedDict
from pathlib import Path

LOGGER = logging.getLogger("qwen.conversation")

# Turns kept per conversation, and conversations kept in memory. A spoken reply is a couple
# of sentences and a written one is at most a few thousand tokens, so this bounds the file
# without bounding what the model can be reminded of.
MAX_TURNS_PER_CONVERSATION = 400
MAX_CONVERSATIONS = 40
# How many of the most recent turns a spoken turn replays, and how many characters that may
# cost. A voice turn must stay cheap: it runs on every spoken sentence boundary.
VOICE_HISTORY_TURNS = 8
VOICE_HISTORY_CHARS = 4000
SAVE_DEBOUNCE_SECONDS = 2.0

# Titles. A new conversation is *called* "新对话" until it has its first turn, at which point the
# owner's own words become the title — cut to TITLE_CHARS so every row in the rail is the same
# shape and a long question cannot push the date out of it. A title the owner typed by hand
# (rename) is never shortened and never overwritten by a later turn.
DEFAULT_TITLE = "新对话"
TITLE_CHARS = 10
RENAME_CHARS = 40


def estimate_tokens(text):
    """A cheap token estimate for when the tokenizer is not available or not worth asking.
    Chinese is close to one token per character and Latin text closer to one per four, so the
    two are counted separately: a single divisor over all characters under-counted Chinese by
    about half (measured against this model's tokenizer), and an under-count is the dangerous
    direction for a context meter. Only a fallback: `ConversationLedger.count`-based figures
    come from the model's own tokenizer.
    """
    if not text:
        return 0
    body = str(text)
    wide = sum(1 for char in body if "\u2e80" <= char <= "\u9fff" or "\uff00" <= char <= "\uffef")
    narrow = len(body) - wide
    return max(1, round(wide + narrow / 3.6))


def auto_title(text):
    """The title a conversation takes from the first thing the owner says in it.

    Whitespace is collapsed first (a pasted paragraph would otherwise spend the whole title on
    indentation) and the result is cut to TITLE_CHARS, so every row in the rail is the same
    shape whether the opening line was a word or a page.
    """
    return " ".join(str(text or "").split())[:TITLE_CHARS]


class ConversationLedger:
    """Per-conversation turns and context accounting, shared by every mode."""

    def __init__(self, path, context_size=8192, reserve_tokens=512):
        self.path = Path(path)
        self.context_size = int(context_size)
        self.reserve_tokens = int(reserve_tokens)
        self._conversations = OrderedDict()
        self._dirty = False
        self._saved_at = 0.0
        # Writes are what "most recent" means, and two writes can land in the same
        # millisecond: ordering by this counter rather than by the timestamp is what stops a
        # just-used conversation from sorting below an older one.
        self._sequence = 0
        self.load()

    # ------------------------------------------------------------------ persistence

    def load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        items = raw.get("conversations") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            key = str(item.get("id") or "").strip()
            if not key:
                continue
            self._conversations[key] = {
                "id": key,
                "title": str(item.get("title") or "")[:120],
                "created_at": float(item.get("created_at") or time.time()),
                "updated_at": float(item.get("updated_at") or time.time()),
                "seq": 0,
                # The last prompt size this conversation actually sent, and the window it was
                # measured against. Kept on the conversation so a later read reports the same
                # measurement instead of silently falling back to an estimate — the figure the
                # panel shows must not depend on which route happened to answer.
                "measured": item.get("measured") if isinstance(item.get("measured"), dict) else None,
                "turns": [],
            }
            for turn in item.get("turns") or []:
                if not isinstance(turn, dict) or turn.get("role") not in ("user", "assistant"):
                    continue
                self._conversations[key]["turns"].append({
                    "role": turn["role"],
                    "content": str(turn.get("content") or "")[:8000],
                    "mode": str(turn.get("mode") or "text")[:16],
                    "at": float(turn.get("at") or time.time()),
                    "tokens": int(turn.get("tokens") or 0),
                })
        self._trim()

    def save(self, force=False):
        if not self._dirty:
            return
        if not force and time.monotonic() - self._saved_at < SAVE_DEBOUNCE_SECONDS:
            return
        payload = {"version": 1, "saved_at": time.time(),
                   "conversations": list(self._conversations.values())}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.path)
            self._dirty = False
            self._saved_at = time.monotonic()
        except OSError:
            LOGGER.exception("Conversation history could not be saved")

    def _trim(self):
        for entry in self._conversations.values():
            turns = entry["turns"]
            if len(turns) > MAX_TURNS_PER_CONVERSATION:
                entry["turns"] = turns[-MAX_TURNS_PER_CONVERSATION:]
        while len(self._conversations) > MAX_CONVERSATIONS:
            self._conversations.popitem(last=False)

    # ------------------------------------------------------------------ writing

    @staticmethod
    def normalize_id(conversation_id):
        """A usable key for a conversation identifier, or a fresh one.

        The chat app sends its own identifier and every mode must agree on it, so it is
        trimmed and bounded rather than rejected: a wasted key is better than a spoken turn
        filed under a different conversation from the written one.
        """
        key = str(conversation_id or "").strip()[:200]
        return key or f"local-{uuid.uuid4().hex[:12]}"

    def _entry(self, conversation_id):
        key = self.normalize_id(conversation_id)
        entry = self._conversations.get(key)
        if entry is None:
            entry = {"id": key, "title": "", "created_at": time.time(),
                     "updated_at": time.time(), "seq": 0, "measured": None, "turns": []}
            self._conversations[key] = entry
            self._trim()
        self._conversations.move_to_end(key)
        return entry
    def _touch(self, entry):
        """Mark a conversation as the most recently used one."""
        self._sequence += 1
        entry["seq"] = self._sequence

    def forget(self, conversation_id):
        """Remove a conversation outright. Returns whether there was one to remove.

        Used by the delete control in the history rail, and by the panel when a visit that was
        never spoken into is left: an empty visit should not become a row.
        """
        key = self.normalize_id(conversation_id)
        if key not in self._conversations:
            return False
        del self._conversations[key]
        self._dirty = True
        self.save()
        return True

    def forget_all(self):
        """Remove every conversation and say how many were removed."""
        removed = len(self._conversations)
        self._conversations.clear()
        self._dirty = True
        self.save(force=True)
        return removed

    def append_turn(self, conversation_id, role, content, mode="text", tokens=None):
        """Record one turn of the shared history and return how it was filed."""
        text = str(content or "")
        if role not in ("user", "assistant") or not text.strip():
            return None
        entry = self._entry(conversation_id)
        record = {
            "role": role, "content": text[:8000], "mode": str(mode or "text")[:16],
            "at": time.time(), "tokens": int(tokens if tokens is not None else estimate_tokens(text)),
        }
        entry["turns"].append(record)
        entry["updated_at"] = record["at"]
        self._touch(entry)
        # The first thing the owner says names the conversation. Whitespace is collapsed first
        # (a pasted paragraph would otherwise spend the whole title on indentation), and a title
        # the owner set by hand — or one already taken from an earlier turn — always wins.
        if not entry["title"] and not entry.get("renamed") and role == "user":
            entry["title"] = auto_title(text)
        self._trim()
        self._dirty = True
        self.save()
        return record

    def rename(self, conversation_id, title):
        """Name a conversation by hand. Returns the new title, or None if there is no such one.

        A manual title is final: later turns do not take it over, which is the difference
        between naming a thread after the question that opened it and naming it after whatever
        the owner meant by it.
        """
        value = " ".join(str(title or "").split())
        if not value:
            raise ValueError("标题不能为空")
        if len(value) > RENAME_CHARS:
            raise ValueError(f"标题最多 {RENAME_CHARS} 个字")
        key = self.normalize_id(conversation_id)
        entry = self._conversations.get(key)
        if entry is None:
            return None
        entry["title"] = value
        entry["renamed"] = True
        # Deliberately not touched: renaming is not activity, so the rail must not reorder itself
        # under the owner's cursor (updated_at and seq stay as they were).
        self._dirty = True
        self.save(force=True)
        return value

    def adopt(self, conversation_id):
        """Make sure a conversation exists, so its meter is available before its first turn."""
        return self._entry(conversation_id)["id"]

    def observe_request(self, conversation_id, request_payload, path, mode="text",
                        reply=None, prompt_tokens=None, window=None):
        """Record a written turn, and how much of the window its prompt used.

        Both halves are optional on purpose. A streaming reply is not captured here (its
        body is consumed as it is forwarded), so it contributes the question and the measured
        prompt size but not the answer; the next turn's prompt covers that text anyway, which
        is what the meter is for.
        """
        if not conversation_id:
            return None
        messages = request_payload.get("messages") if isinstance(request_payload, dict) else None
        prompt = request_payload.get("prompt") if isinstance(request_payload, dict) else None
        question = ""
        if isinstance(messages, list):
            for message in reversed(messages):
                if isinstance(message, dict) and message.get("role") == "user":
                    content = message.get("content")
                    question = content if isinstance(content, str) else json.dumps(
                        content, ensure_ascii=False)
                    break
        elif isinstance(prompt, str):
            question = prompt
        if question:
            self.append_turn(conversation_id, "user", question, mode=mode)
        if reply:
            self.append_turn(conversation_id, "assistant", reply, mode=mode)
        measured = prompt_tokens
        if measured is not None:
            self.record_measurement(conversation_id, measured, window=window)
        if measured is None:
            measured = estimate_tokens(question) if question else None
            if measured is not None:
                measured += int(request_payload.get("max_tokens") or 0)
        meter = self.meter(conversation_id, used_tokens=measured, mode=mode, window=window)
        self.save()
        return meter

    # ------------------------------------------------------------------ reading

    def record_measurement(self, conversation_id, used_tokens, window=None):
        """Remember the prompt size this conversation really sent.

        Called by whichever path produced it — the chat side's compressor or a spoken turn's
        backend usage — so the next read of the meter reports the measurement rather than
        re-deriving an estimate from the stored turns.
        """
        entry = self._entry(conversation_id)
        entry["measured"] = {"used_tokens": int(used_tokens),
                             "window": int(window) if window else None,
                             "at": time.time()}
        self._dirty = True
        self.save()
        return entry["measured"]

    def history(self, conversation_id, limit=VOICE_HISTORY_TURNS, max_chars=VOICE_HISTORY_CHARS):
        """The most recent turns, oldest first, bounded by count and by size.

        This is what a spoken turn replays so the model knows what was just typed, and what
        the voice panel lists so the owner can see the thread they are continuing.
        """
        entry = self._conversations.get(self.normalize_id(conversation_id))
        if not entry:
            return []
        selected, used = [], 0
        for turn in reversed(entry["turns"][-max(limit * 4, limit):]):
            if len(selected) >= limit:
                break
            cost = len(turn["content"])
            if selected and used + cost > max_chars:
                break
            selected.append(turn)
            used += cost
        selected.reverse()
        return [dict(turn) for turn in selected]

    def messages(self, conversation_id, limit=VOICE_HISTORY_TURNS, max_chars=VOICE_HISTORY_CHARS):
        """The same history shaped as chat messages, ready to prepend to a request."""
        return [{"role": turn["role"], "content": turn["content"]}
                for turn in self.history(conversation_id, limit, max_chars)]

    def meter(self, conversation_id, used_tokens=None, mode="text", window=None):
        """How much of the context window this conversation has left.

        ``used_tokens`` is the measured size of the prompt that was actually sent when the
        caller knows it; otherwise the last measured value, or an estimate over the stored
        turns, is used. The number is reported with its source so the panel never presents an
        estimate as a measurement.
        """
        entry = self._conversations.get(self.normalize_id(conversation_id)) if conversation_id else None
        size = int(window or self.context_size)
        measured = used_tokens
        source = "measured" if measured is not None else "unknown"
        if measured is None and entry is not None and entry.get("measured"):
            stored = entry["measured"]
            measured = int(stored.get("used_tokens") or 0)
            source = "measured"
            # The window the measurement belongs to wins unless the caller asked about a
            # different one, which is how a borrowed 9B reports its own 32 K and not the file's.
            if window is None and stored.get("window"):
                size = int(stored["window"])
        if measured is None and entry is not None:
            total = sum(turn.get("tokens") or estimate_tokens(turn["content"])
                        for turn in entry["turns"])
            measured, source = total, "estimated"
        if measured is None:
            # Nothing measured and no conversation to sum: a zero here would read as "the
            # window is empty" when the truth is "nothing is known about this window yet".
            measured, source = 0, "unknown"
        usable = max(1, size - self.reserve_tokens)
        remaining = max(0, usable - int(measured))
        ratio = min(1.0, int(measured) / usable)
        if remaining <= 0:
            level = "full"
        elif ratio >= 0.85:
            level = "critical"
        elif ratio >= 0.6:
            level = "tight"
        else:
            level = "ok"
        return {
            "conversation_id": entry["id"] if entry else self.normalize_id(conversation_id),
            "mode": mode,
            "turns": len(entry["turns"]) if entry else 0,
            "context_size": size,
            "reserve_tokens": self.reserve_tokens,
            "usable_tokens": usable,
            "used_tokens": int(measured),
            "remaining_tokens": remaining,
            "used_ratio": round(ratio, 4),
            "level": level,
            "source": source,
            "measured_at": time.time(),
        }

    def summary(self, conversation_id, used_tokens=None, mode="text", window=None):
        """The meter plus the recent thread: what a panel needs to show both at once."""
        return {
            "meter": self.meter(conversation_id, used_tokens=used_tokens, mode=mode, window=window),
            "history": self.history(conversation_id),
        }

    def conversations(self, scope=""):
        """Every known conversation, most recently used first, without their turn bodies.

        ``scope`` filters to one mode's threads (``"voice·"`` and so on). The gateway keeps one
        history per mode — a written thread must not turn up in 识图 — and a listing is the other
        half of that rule: what a panel shows is what that mode owns.
        """
        items = []
        for entry in sorted(self._conversations.values(),
                            key=lambda item: (item.get("seq") or 0, item["updated_at"]),
                            reverse=True):
            if scope and not entry["id"].startswith(scope):
                continue
            meter = self.meter(entry["id"])
            items.append({
                "id": entry["id"], "title": entry["title"], "turns": len(entry["turns"]),
                # What the rail prints. A conversation with no turns yet is *called* 新对话 —
                # sending the empty title to the UI made it fall back to the identifier
                # ("voice-x4amc…"), which reads as a bug rather than as an untitled thread.
                "display_title": entry["title"] or DEFAULT_TITLE,
                "renamed": bool(entry.get("renamed")),
                "created_at": entry["created_at"], "updated_at": entry["updated_at"],
                "last_mode": entry["turns"][-1]["mode"] if entry["turns"] else None,
                "used_tokens": meter["used_tokens"], "level": meter["level"],
            })
        return items
