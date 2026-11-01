"""Read-only: what are the queued ComfyUI prompts actually rendering?

    python -X utf8 comfy_queue_prompts.py

Prints the prompt/storyboard text and output paths of every queued entry, which is how a
real owner's job is told apart from junk a test queued.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import sys
import urllib.request

COMFY = "http://127.0.0.1:8188"
INTERESTING = ("text", "prompt", "storyboard", "shot", "path", "prefix", "filename", "name",
               "seconds", "duration", "steps", "width", "height")


def fetch(path):
    with urllib.request.urlopen(COMFY + path, timeout=20) as response:
        return json.loads(response.read())


def main() -> int:
    queue = fetch("/queue")
    entries = (queue.get("queue_running") or []) + (queue.get("queue_pending") or [])
    print(f"running={len(queue.get('queue_running') or [])} pending={len(queue.get('queue_pending') or [])}")
    for entry in entries:
        number, prompt_id, graph, extra = entry[0], entry[1], entry[2], entry[3]
        print(f"--- #{number} {prompt_id[:8]} create={extra.get('create_time')}")
        for node in graph.values():
            if not isinstance(node, dict):
                continue
            title = (node.get("_meta") or {}).get("title", "")
            for key, value in (node.get("inputs") or {}).items():
                if not isinstance(value, str) or len(value) < 3:
                    continue
                if any(word in key.lower() for word in INTERESTING):
                    print(f"    {title}.{key} = {value[:110]!r}")
                elif any(word in value.lower() for word in INTERESTING):
                    pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
