"""Read-only: who submitted the ComfyUI prompts that are queued right now?

    python -X utf8 comfy_queue_who.py

Prints each queue entry's client_id / create_time / prompt id and the node titles, so the
submitter can be identified (gateway vs. a test vs. the ComfyUI window itself).
"""
import json
import sys
import urllib.request

COMFY = "http://127.0.0.1:8188"


def fetch(path):
    with urllib.request.urlopen(COMFY + path, timeout=15) as response:
        return json.loads(response.read())


def describe(entry):
    number, prompt_id, graph, extra = entry[0], entry[1], entry[2], entry[3]
    titles = sorted({(node.get("_meta") or {}).get("title", "") for node in graph.values()
                     if isinstance(node, dict)} - {""})
    client = (extra or {}).get("client_id", "")
    created = (extra or {}).get("create_time")
    return (f"#{number} {prompt_id[:8]} client_id={client[:40]} create_time={created}\n"
            f"     nodes={len(graph)} titles={titles[:4]}")


def main() -> int:
    queue = fetch("/queue")
    print(f"running={len(queue.get('queue_running') or [])} "
          f"pending={len(queue.get('queue_pending') or [])}")
    for entry in (queue.get("queue_running") or []) + (queue.get("queue_pending") or []):
        print(describe(entry))
    history = fetch("/history?max_items=8")
    print(f"--- history ({len(history)} entries) ---")
    for prompt_id, record in list(history.items())[:8]:
        extra = ((record.get("prompt") or [None, None, None, {}])[3]) or {}
        status = (record.get("status") or {}).get("status_str", "")
        print(f"{prompt_id[:8]} client_id={str(extra.get('client_id'))[:40]} "
              f"create_time={extra.get('create_time')} status={status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
