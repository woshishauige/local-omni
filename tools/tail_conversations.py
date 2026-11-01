"""Read the shared conversation ledger and print its recent turns.

The ledger is the only place the actual conversation survives (the panel's own copy lives in the
browser), so it is what answers questions like "why is the reply in English?" - whether the owner's
own turn was English, or whether a Chinese question produced an English answer.

Usage:
    python tools/tail_conversations.py [--turns 6] [--chars 90] [--only voice]
"""

import argparse
import json
from pathlib import Path

LEDGER = Path(r"D:\AI\Qwen-Local\logs\conversation-history.json")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--turns", type=int, default=6)
    parser.add_argument("--chars", type=int, default=90)
    parser.add_argument("--only", default="")
    args = parser.parse_args()

    payload = json.loads(LEDGER.read_text(encoding="utf-8"))
    conversations = payload.get("conversations") or []
    print(f"{LEDGER.name}: {len(conversations)} conversation(s), "
          f"saved {payload.get('saved_at')}")

    for entry in conversations:
        turns = entry.get("turns") or []
        if args.only and not any(t.get("mode") == args.only for t in turns):
            continue
        print(f"\n=== {entry.get('id')}  ({len(turns)} turn(s))")
        for turn in turns[-args.turns:]:
            text = str(turn.get("content") or "").replace("\n", " ")
            print(f"  [{turn.get('mode'):<5}] {turn.get('role'):<9} {text[:args.chars]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
