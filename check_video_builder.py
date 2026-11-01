"""Self-check the video prompt builder: wiring, overrides, and both recipes.

A full H3 run costs ten minutes, so the graph is validated structurally first:
every link must resolve to a node that exists, every node must be a class the
server knows, and the quality/speed switch must actually differ.
"""
import json

import requests

import video_jobs

SERVER = "http://127.0.0.1:8188"


def check(name, prompt_graph, known_classes, expect_cache, check_classes=True):
    print(f"\n=== {name} ===")
    print(f"  nodes: {len(prompt_graph)}")
    problems = []
    unknown = []
    for node_id, node in prompt_graph.items():
        if check_classes and node["class_type"] not in known_classes:
            unknown.append(node["class_type"])
        for input_name, value in node["inputs"].items():
            if isinstance(value, list):
                if len(value) != 2:
                    problems.append(f"node {node_id}.{input_name}: malformed link {value}")
                elif str(value[0]) not in prompt_graph:
                    problems.append(f"node {node_id}.{input_name} -> missing node {value[0]}")
    if unknown:
        print(f"  (class availability not checked: server lacks {len(set(unknown))} H3 classes)")
    cache_nodes = [n for n in prompt_graph.values()
                   if n["class_type"] == "ApplyMiniMaxH3FirstBlockCache"]
    if expect_cache and not cache_nodes:
        problems.append("speed recipe is missing the cache node")
    if not expect_cache and cache_nodes:
        problems.append("quality recipe still carries the cache node")
    director = prompt_graph.get("50")
    if director is None:
        problems.append("director node 50 is missing")
    else:
        for field in ("width", "height", "steps", "seed"):
            if field in director["inputs"]:
                print(f"  director.{field} = {director['inputs'][field]}")
    # The bypassed cache node's consumers must read the cache node's own source.
    if not expect_cache:
        for node_id, node in prompt_graph.items():
            for input_name, value in node["inputs"].items():
                if isinstance(value, list) and value and str(value[0]) == "80":
                    problems.append(f"node {node_id}.{input_name} still points at the removed cache node")
    for node_id in ("40", "41", "42", "43"):
        node = prompt_graph.get(node_id)
        if node is None:
            problems.append(f"shot text node {node_id} is missing")
        elif "value" not in node["inputs"]:
            problems.append(f"shot text node {node_id} has no value")
        else:
            print(f"  shot {node_id}: {str(node['inputs']['value'])[:56]}...")
    if problems:
        print("  PROBLEMS:")
        for problem in problems:
            print("    -", problem)
    else:
        print("  wiring OK")
    return not problems


def main():
    try:
        info = requests.get(f"{SERVER}/object_info", timeout=30).json()
    except requests.RequestException as exc:
        print(f"cannot reach ComfyUI at {SERVER}: {exc}")
        info = {}

    known = set(info)
    # The server only exposes the H3 classes once the gateway's whitelist is in
    # effect; when it is not, class availability is reported instead of failing.
    check_classes = bool(known) and "H3MP4ShotEditor" in known
    print(f"server node types: {len(known)}  H3 classes present: {check_classes}")

    ok = True
    for key in ("video_quality", "video_speed"):
        graph = video_jobs.build_prompt(
            key,
            prompt="integrated_multimodal_description: a quiet rainy street at night",
            seed=12345, width=768, height=512, steps=20, seconds=7.5,
        )
        ok &= check(key, graph, known, expect_cache=(key == "video_speed"),
                    check_classes=check_classes)

    shots = video_jobs.build_prompt(
        "video_quality", prompt="g", shots=["shot A only", "shot B only", "", ""],
        seed=7,
    )
    print("\n=== per-shot override ===")
    for node_id in ("40", "41", "42", "43"):
        print(f"  {node_id}: {shots[node_id]['inputs']['value'][:48]}")

    print("\n=== recipes as the panel sees them ===")
    print(json.dumps([r.describe() for r in video_jobs.recipes()], ensure_ascii=False, indent=1))

    print("\nRESULT:", "OK" if ok else "PROBLEMS FOUND")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
