"""A mode's thread is its own: what reaches the model is filtered to the mode serving it.

The gateway keeps one history *per mode* (see ``mode_server.mode_scope``), but the client sends
its own whole conversation on every turn — the packaged app keeps one thread on screen while the
toolbar switches which slice of the backend answers. So a question typed in 纯文本 still arrived
inside the vision prompt: the model read the typed thread while looking at a picture. The owner's
rule is that switching modes must not carry data across, and this module is the half that enforces
it on the way in.

The decision is made from the gateway's own records rather than by guessing:

* a **user** message is kept when its text is one this mode has recorded, or when it is the
  current turn (the last user message) — that one has not been recorded yet;
* an **assistant** message is kept when it follows a kept question, because it is that question's
  answer and therefore belongs to the same mode;
* **system/developer** messages are always kept: they are instructions, not history;
* anything else belongs to another mode and is dropped, which the caller logs with the count.

With nothing recorded yet for a mode, the result is the system prompt plus the current turn — a
fresh exchange, which is exactly what "no data crosses over" means.
"""

import json
from collections import Counter


def message_text(message) -> str:
    """The text of one message, normalised the way the ledger records it.

    ``conversation_ledger.observe_request`` stores a string content as-is and anything else as
    JSON, so the comparison here has to use the same two forms or the filter would drop the very
    turns it is supposed to keep.
    """
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def isolate(payload: dict, known_user_texts) -> tuple[dict, int]:
    """Return ``(payload, dropped)`` with the other modes' turns removed.

    ``known_user_texts`` is what this mode has recorded. It is matched **as a count, from the end**,
    not as a bare set: a question this mode asked once may appear several times in the client's
    thread (short lines like "继续" repeat constantly), and a set then legitimises *every* copy —
    measured: a typed "你好 / A1 / 你好 / A2 / 新问题" thread with one recorded "你好" kept the
    *other mode's* copy and its answer, which is exactly the leak this module exists to stop. Each
    recorded occurrence now licenses one message, the newest first, and the current turn is always
    kept without consuming one.

    The payload is returned unchanged (same object) when nothing had to be dropped, so a caller can
    tell the two cases apart without comparing bodies.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return payload, 0
    remaining = Counter()
    for text in known_user_texts or ():
        value = str(text or "")
        if value:
            remaining[value] += 1
    last_user = next((index for index in range(len(messages) - 1, -1, -1)
                      if isinstance(messages[index], dict)
                      and messages[index].get("role") == "user"), None)
    keep_user = [False] * len(messages)
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        if index == last_user:
            keep_user[index] = True
            continue
        text = message_text(message)
        if text and remaining.get(text, 0) > 0:
            remaining[text] -= 1
            keep_user[index] = True
    kept = []
    dropped = 0
    keep_run = False
    for index, message in enumerate(messages):
        role = message.get("role") if isinstance(message, dict) else None
        if role in ("system", "developer"):
            kept.append(message)
            continue
        if role == "user":
            keep_run = keep_user[index]
            if keep_run:
                kept.append(message)
            else:
                dropped += 1
            continue
        # Assistant replies, tool results and anything else follow the question they belong to:
        # a run that survived is the only way to reach one.
        if keep_run:
            kept.append(message)
        else:
            dropped += 1
    if not dropped:
        return payload, 0
    return {**payload, "messages": kept}, dropped
