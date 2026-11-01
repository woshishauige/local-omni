"""Bring the conversation ledger's on-disk snapshot in line with the running ledger.

`ConversationLedger.forget()` does call `save()`, but `save()` is debounced and returns early
without clearing `_dirty` when the window has not elapsed. The last deletion in a burst is
therefore left pending, and if the gateway restarts before any later mutation flushes it, the
conversations a moment ago deleted come back - which is exactly what happened here after the
cleanup ran and the gateway was restarted.

The live ledger is authoritative and already correct; this only writes the same state to disk,
so the running process and the snapshot agree. Any later save overwrites this with the same
content. A backup is kept and the replacement is atomic.

Usage: python tools/sync_conversation_snapshot.py <snapshot.json> <id-to-drop> [<id> ...]
"""

import json
import os
import shutil
import sys
import time

path = sys.argv[1]
drop = set(sys.argv[2:])
if not drop:
    print("nothing to drop; pass at least one conversation id")
    sys.exit(1)

raw = open(path, encoding="utf-8").read()
payload = json.loads(raw)
before = payload.get("conversations", [])
keep = [c for c in before if c.get("id") not in drop]
removed = [c.get("id") for c in before if c.get("id") in drop]

print(f"snapshot written at : {time.strftime('%H:%M:%S', time.localtime(payload.get('saved_at', 0)))}")
print(f"conversations       : {len(before)} -> {len(keep)}  (dropping {len(removed)})")
for name in removed:
    print(f"   drop {name}")
for entry in keep:
    print(f"   keep {entry.get('id')}  turns={len(entry.get('turns') or [])}")

if not removed:
    print("nothing matched; file left untouched")
    sys.exit(0)

backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
shutil.copy2(path, backup)

payload["conversations"] = keep
temporary = path + ".tmp"
with open(temporary, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False)

# Validate before letting it replace the real file: a ledger that cannot be parsed costs
# every conversation, and this file is only ever a cache of what the process already has.
with open(temporary, encoding="utf-8") as handle:
    check = json.load(handle)
assert isinstance(check.get("conversations"), list), "rebuilt snapshot is not a conversation list"
for entry in check["conversations"]:
    assert "id" in entry and isinstance(entry.get("turns"), list), f"malformed entry {entry!r}"

os.replace(temporary, path)
print(f"\nreplaced; backup at {os.path.basename(backup)}")
final = json.loads(open(path, encoding="utf-8").read())
print(f"file now holds {len(final['conversations'])} conversation(s)")
