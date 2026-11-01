"""What this machine actually keeps about a conversation, and what a guest leaves.

Three separate stores have to be told apart, because "is there a history" has three
different answers depending on which one is meant:

  * the chat transcript itself - the server never holds it; context_compaction.py says
    so in its first line, and the only server-side trace is an in-memory scope entry
    keyed by the *hash* of the conversation id
  * usage.sqlite3 - durable, one row per request, written by UsageMiddleware
  * share.sessions.json - one entry per device that redeemed the passphrase

This prints the schema and the shape of each so the answer is read off the store
rather than inferred from the code. Session tokens are truncated on purpose.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"


def section(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def inspect_usage():
    path = LOGS / "usage.sqlite3"
    section("usage.sqlite3  (%s)" % (path.stat().st_size if path.exists() else "missing"))
    if not path.exists():
        return
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    tables = [row[0] for row in connection.execute(
        "select name from sqlite_master where type='table' order by name")]
    for table in tables:
        count = connection.execute("select count(*) from %s" % table).fetchone()[0]
        print("\n-- table %-22s rows = %d" % (table, count))
        columns = connection.execute("pragma table_info(%s)" % table).fetchall()
        print("   columns: " + ", ".join(c[1] for c in columns))
        try:
            row = connection.execute("select * from %s order by rowid desc limit 1" % table).fetchone()
        except sqlite3.Error:
            row = None
        if row:
            for key in row.keys():
                value = row[key]
                text = "" if value is None else str(value)
                if len(text) > 110:
                    text = text[:110] + "..."
                print("   %-24s %s" % (key, text))
        # Does any column hold free text that could be a message body?
        for column in columns:
            name = column[1]
            if name.lower() in ("text", "content", "message", "body", "prompt"):
                print("   !! column %r could carry message content" % name)
    connection.close()


def inspect_sessions():
    path = LOGS / "share.sessions.json"
    section("share.sessions.json  (%s bytes)" % (path.stat().st_size if path.exists() else "missing"))
    if not path.exists():
        return
    state = json.loads(path.read_text(encoding="utf-8"))
    print("top-level keys:", list(state.keys()))
    sessions = state.get("sessions") or {}
    admin = state.get("admin") or state.get("admin_token")
    print("session entries:", len(sessions) if isinstance(sessions, (dict, list)) else "?")
    items = sessions.items() if isinstance(sessions, dict) else enumerate(sessions)
    shown = 0
    for key, item in items:
        if shown >= 6:
            break
        shown += 1
        if isinstance(item, dict):
            brief = {k: (str(v)[:60] if not isinstance(v, (int, float, bool, type(None))) else v)
                     for k, v in item.items()}
            print("  token %s...  %s" % (str(key)[:10], json.dumps(brief, ensure_ascii=False)))
        else:
            print("  ", str(item)[:100])
    if admin:
        print("admin token present:", str(admin)[:8] + "...")


def inspect_uploads():
    path = LOGS / "guest-uploads.json"
    section("guest-uploads.json")
    if not path.exists():
        print("missing")
        return
    print(path.read_text(encoding="utf-8")[:400])


def inspect_client_storage():
    section("where the chat transcript lives (native bundle)")
    bundle = ROOT / "web" / "native" / "_app" / "immutable" / "bundle.zh-CN.js"
    if not bundle.exists():
        print("bundle not found")
        return
    text = bundle.read_text(encoding="utf-8", errors="replace")
    for pattern, label in (
        (r'indexedDB\.open\(\s*["\'`]([^"\'`]{1,40})', "indexedDB.open(name)"),
        (r'\.createObjectStore\(\s*["\'`]([^"\'`]{1,40})', "createObjectStore(name)"),
        (r'localStorage\.setItem\(\s*["\'`]([^"\'`]{1,60})', "localStorage key written"),
    ):
        found = sorted(set(re.findall(pattern, text)))
        print("\n%s:" % label)
        for item in found[:12]:
            print("   ", item)
        if not found:
            print("    (none)")
    for word in ("conversations", "messages", "chats", "idb"):
        print("  occurrences of %-14r: %d" % (word, text.count(word)))


if __name__ == "__main__":
    inspect_usage()
    inspect_sessions()
    inspect_uploads()
    inspect_client_storage()
