"""Remove conversations the browser checks left behind, without touching real ones.

The checks open voice mode repeatedly, and every visit registers a conversation; the ones that
were never spoken into are deleted by the panel itself, but a check that is killed mid-run can
leave a row. Two kinds of leftover qualify:

  * an identifier carrying a marker the checks use (`voice-history-check`, `probe-` …), or
  * a **zero-turn** conversation whose identifier is one the panels generate themselves
    (`voice-…` from entering voice mode, `local-…` from "新对话") and that has been idle for
    longer than --idle-minutes.

The second rule exists because a browser check that is interrupted leaves `voice-x4amc… · 0 轮`
rows in the rail, which is what the owner saw and reported as a mess. A conversation somebody
actually spoke into has turns, whatever its identifier, so it is never touched.

    & ..\\ComfyUI-BSAI_v010\\python\\python.exe -X utf8 tools\\prune_test_conversations.py
    & ..\\ComfyUI-BSAI_v010\\python\\python.exe -X utf8 tools\\prune_test_conversations.py --apply

Run it with the Qwen service **stopped**: the gateway holds the same history in memory and
writes it back on its next turn, so an edit made underneath it would be overwritten.
"""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from conversation_ledger import ConversationLedger  # noqa: E402

LEDGER = ROOT / "logs" / "conversation-history.json"
# Identifiers the checks create. Anything else is left alone no matter how empty it looks.
TEST_MARKERS = ("voice-history-check", "voice-history-check-older", "shared-demo",
                "meter-check", "ctx-check", "ctx-final", "accept-", "probe-",
                # 线上验收脚本自己建的会话（隔离/访客能力/审查探针）
                "isolation-live-", "window-proof", "switch-me", "chat-1", "audit-")
# Prefixes the panels generate for a visit of their own ("voice-…" on entering voice mode,
# "local-…" when the owner presses 新对话). Only ever pruned when they hold no turns.
GENERATED_PREFIXES = ("voice-", "local-")


def stale(item, idle_minutes):
    """Whether a zero-turn conversation is old enough to be a leftover rather than a live visit."""
    if not any(item["id"].startswith(prefix) for prefix in GENERATED_PREFIXES):
        return False
    updated = float(item.get("updated_at") or 0)
    return (time.time() - updated) > idle_minutes * 60


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="actually delete them")
    parser.add_argument("--idle-minutes", type=float, default=10,
                        help="只清理闲置超过这个分钟数的 voice-/local- 空会话（默认 10）")
    args = parser.parse_args()

    ledger = ConversationLedger(LEDGER, context_size=32768)
    listing = ledger.conversations()
    print(f"共 {len(listing)} 个会话")

    removed = 0
    for item in listing:
        marked = any(marker in item["id"] for marker in TEST_MARKERS)
        if not marked and not stale(item, args.idle_minutes):
            continue
        turns = ledger.history(item["id"], limit=1, max_chars=1)
        if turns:
            print(f"  保留（有内容）: {item['id']} · {item['turns']} 轮")
            continue
        if args.apply:
            ledger.forget(item["id"])
        removed += 1
        why = "测试标记" if marked else f"闲置空会话"
        print(f"  {'已删除' if args.apply else '可删除'}（{why}）: {item['id']}")

    print(f"\n{'已删除' if args.apply else '可删除'} {removed} 个空会话"
          + ("" if args.apply else "（加 --apply 才会真的删除）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
