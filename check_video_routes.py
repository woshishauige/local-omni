"""Logic checks for the video layer that do not need a ten-minute H3 run."""
import json
import struct
from pathlib import Path
from urllib.parse import quote

import video_interpolate
import video_jobs
import video_routes

LORA_ROOT = video_jobs.WORKFLOW_ROOT.parents[3] / "models" / "loras"


def _lora_key_names(path: Path) -> set[str]:
    with path.open("rb") as handle:
        length = struct.unpack("<Q", handle.read(8))[0]
        header = json.loads(handle.read(length).decode("utf-8"))
    return {key for key in header if key != "__metadata__"}


def _lora_keys_are_prefixed(path: Path) -> bool:
    """ComfyUI silently loads nothing from a LoRA whose keys lack the model prefix."""
    keys = _lora_key_names(path)
    return bool(keys) and all(key.startswith("diffusion_model.") for key in keys)


def _lora_keys_match_the_model(path: Path) -> bool:
    """The prefix alone is not enough: the module names must be the model's.

    A kohya LoRA renamed only by its prefix keeps `blocks.0.attn_out_proj` where the model says
    `blocks.0.attn.out_proj`, loads nothing, and still looks healthy — measured as two renders
    that came out identical to three decimals, one with the LoRA and one without. The reference
    is the distilled LoRA that is known to load, so a mismatch fails here instead of silently.
    """
    reference = LORA_ROOT / "MiniMaxH3" / "TaoMate-H3-3step-dm.safetensors"
    if not reference.is_file():
        return True
    wanted = {key for key in _lora_key_names(reference) if key.endswith(".lora_A.weight")}
    ours = {key for key in _lora_key_names(path) if key.endswith(".lora_A.weight")}
    return bool(ours) and bool(wanted & ours)


def _model_chain_passes_a_lora(graph: dict, start: str, loras: set[str]) -> bool:
    """Walk the MODEL inputs back from the director; a LoRA must be on the way.

    The director no longer reads the LoRA node directly on every route: the acceleration
    workflows put `MiniMaxH3SigmaShift` (the AV clock) between the LoRA and the director, so
    "reads its LoRA" means "the model it samples with has been through one".
    """
    seen: set[str] = set()
    node_id = str(start)
    for _ in range(16):
        if node_id in loras:
            return True
        if node_id in seen:
            return False
        seen.add(node_id)
        node = graph.get(node_id)
        if not node:
            return False
        link = node["inputs"].get("model")
        if not isinstance(link, list):
            return False
        node_id = str(link[0])
    return False


def main() -> int:
    failures = []

    print("=== recipes ===")
    for entry in video_jobs.recipes():
        print(f"  {entry.key:<16} {entry.file}")
        if not (video_jobs.WORKFLOW_ROOT / entry.file).exists():
            failures.append(f"{entry.key}: workflow file missing")

    print("\n=== _read_report on a realistic outputs payload ===")
    # Shape taken from ComfyUI-H3-LocalDirector: the node returns two strings and
    # is an OUTPUT_NODE, so both land under their RETURN_NAMES keys.
    outputs = {
        "50": {
            "video_path": [r"D:\AI\ComfyUI-BSAI_v010\ComfyUI\output\video\H3-30s\projects\x\master.mp4"],
            "checkpoint_report": ["shots=4 frames=720 seconds=30.0 steps=20 cache=off"],
        },
        "51": {"text": ["shots=4 frames=720 seconds=30.0 steps=20 cache=off"]},
    }
    video, report = video_routes._read_report(outputs)
    print("  video :", video)
    print("  report:", report)
    if not video or not video.endswith("master.mp4"):
        failures.append("_read_report did not find the video path")
    if not report or "shots=4" not in report:
        failures.append("_read_report did not find the report")

    print("\n=== video_url maps into a servable root ===")
    job = video_routes.VideoJob("vid-test", "video_distill12", "p", {"shots": ["a", "b"]})
    inside = video_routes.OUTPUT_ROOT / "video" / "H3-30s" / "projects" / "x" / "master.mp4"
    job.video_path = str(inside)
    print("  url:", job.video_url())
    expected = "/local/video/file?path=" + quote(str(inside.resolve()))
    if job.video_url() != expected:
        failures.append(f"unexpected video url: {job.video_url()}")
    job.video_path = r"C:\Windows\not-ours.mp4"
    print("  outside every allowed root ->", job.video_url())
    if job.video_url() is not None:
        failures.append("a path outside the allowed roots should not be served")

    print("\n=== save location: default, relative and absolute ===")
    for value, wanted in ((None, video_routes.DEFAULT_OUTPUT_ROOT),
                          ("video", video_routes.OUTPUT_ROOT / "video"),
                          (r"D:\films", r"D:\films")):
        resolved = video_routes._resolve_output_root(value)
        print(f"  {str(value):<12} -> {resolved}")
        if resolved != str(wanted):
            failures.append(f"save location {value!r} resolved to {resolved}")

    print("\n=== folder chooser lists real folders ===")
    listing = video_routes._folder_browse(str(video_routes.OUTPUT_ROOT))
    print(f"  {listing['path']} -> {len(listing['folders'])} 个子文件夹，{len(listing['roots'])} 个盘符")
    if not listing["roots"] or listing["path"] is None:
        failures.append("the chooser lists neither drives nor folders")
    if any("\\" in entry["name"] or "/" in entry["name"] for entry in listing["folders"]):
        failures.append("the chooser returned something that is not a plain folder name")

    print("\n=== shot plan: one entry per shot, seeds always usable ===")
    body = video_routes.VideoGenerateRequest(
        prompt="p", shots=["a", "b", "c"],
        shot_plan=[video_routes.ShotPlan(seconds=6.0, steps=8, seed=-1),
                   video_routes.ShotPlan(seconds=99.0, steps=500, seed=None)])
    plan = video_routes._shot_plan(body)
    for entry in plan:
        print("  ", entry)
    if len(plan) != 3:
        failures.append(f"the plan should cover every shot, got {len(plan)}")
    if plan[1]["seconds"] != 13.75 or plan[1]["steps"] != 50:
        failures.append("out-of-range shot settings were not clamped")
    for index, entry in enumerate(plan, 1):
        if not isinstance(entry["seed"], int) or entry["seed"] < 0:
            failures.append(f"shot {index} did not get a random seed: {entry['seed']}")
    if plan[0]["seconds"] != 6.0 or plan[0]["steps"] != 8:
        failures.append("the panel's own per-shot values were not kept")
    if len({entry["seed"] for entry in plan}) != len(plan):
        failures.append("each shot should get its own random seed")

    print("\n=== every route carries its own LoRA wiring in its workflow file ===")
    for key in [entry.key for entry in video_jobs.recipes()]:
        graph = video_jobs.build_prompt(key, prompt="夜晚雨后的城市街道", shot_count=1,
                                        shot_seconds=[5.0])
        entry = video_jobs.recipe(key)
        loras = [node_id for node_id, node in graph.items()
                 if node["class_type"] == "LoraLoaderModelOnly"]
        model_in = graph[str(video_jobs.DIRECTOR)]["inputs"]["model"]
        name = graph[loras[0]]["inputs"]["lora_name"].split("\\")[-1] if loras else "无"
        strength = graph[loras[0]]["inputs"]["strength_model"] if loras else None
        tao = bool(loras) and "TaoMate" in graph[loras[0]]["inputs"]["lora_name"]
        print(f"  {key:<15} 文件={entry.file.split('/')[-1]:<28} 步数={entry.params.get('steps')} "
              f"LoRA={name} 强度={strength} 导演 model={model_in}")
        if tao and strength != 0.85:
            failures.append(f"{key}: the distill route ships strength {strength}, expected 0.85")
        if loras:
            dialed = video_jobs.build_prompt(key, prompt="夜晚雨后的城市街道", shot_count=1,
                                            shot_seconds=[5.0], lora_strength=0.7)
            dialed_lora = [node["inputs"] for node in dialed.values()
                           if node["class_type"] == "LoraLoaderModelOnly"]
            if dialed_lora[0]["strength_model"] != 0.7:
                failures.append(f"{key}: a requested strength does not reach the graph")
        # A route may chain a second LoRA on purpose (the NSFW overlay), so the label has to
        # name the LoRA family it carries instead of the single word 蒸馏.
        if bool(loras) and not any(word in entry.label for word in ("蒸馏", "加速", "NSFW")):
            failures.append(f"{key}: the recipe label and its LoRA wiring disagree")
        if loras and not _model_chain_passes_a_lora(graph, model_in[0], set(loras)):
            failures.append(f"{key}: the director does not read through its LoRA")
        # A route that names a LoRA must be able to load it: catching a missing or
        # unprefixed file here is cheaper than discovering it in a silent no-op render.
        for node_id in loras:
            name = graph[node_id]["inputs"]["lora_name"]
            path = LORA_ROOT / name
            if not path.is_file():
                print(f"      LoRA 文件缺失：{name}")
                failures.append(f"{key}: LoRA {name} is not on disk")
            elif not _lora_keys_are_prefixed(path):
                print(f"      LoRA 键没有 diffusion_model. 前缀：{name}")
                failures.append(f"{key}: LoRA {name} would load none of its tensors")
            elif not _lora_keys_match_the_model(path):
                print(f"      LoRA 键名与模型不一致（模块名不是 attn.out_proj / mlp.fc1 这一套）：{name}")
                failures.append(f"{key}: LoRA {name} keys do not match the model's modules")
            else:
                print(f"      LoRA 文件 {name} "
                      f"（{path.stat().st_size/1048576:.0f} MB，键前缀正常）")

    print("\n=== workflow leftovers must not reach a film ===")
    shipped = video_jobs.load_template("video_distill12")
    director = next(node for node in shipped["nodes"] if node["type"] == "H3MP4ShotEditor")
    shipped_prompt = video_jobs.named_widgets("H3MP4ShotEditor", director["widgets_values"])["global_prompt"]
    print(f"  文件自带的 global_prompt: {len(shipped_prompt)} 字符（面板提交时不带）")
    if not shipped_prompt:
        failures.append("the shipped workflow no longer carries a global prompt to test against")
    for key in ("video_distill12", "video_distill3", "video_balance6"):
        graph = video_jobs.build_prompt(key, prompt="夜晚雨后的城市街道", shot_count=1, shot_seconds=[5.0])
        slots = {node_id: node["inputs"] for node_id, node in graph.items()
                 if node["class_type"] == "H3OptionalReferenceImage"}
        armed = [node_id for node_id, inputs in slots.items() if inputs["enabled"]]
        named = [inputs.get("image") for inputs in slots.values()
                 if inputs.get("image") not in (None, "__none__")]
        print(f"  {key:<14} global_prompt={graph['50']['inputs']['global_prompt']!r} "
              f"参考图 {len(slots)} 个 / 启用 {len(armed)} / 带图 {named or '无'}")
        if graph["50"]["inputs"]["global_prompt"] != "":
            failures.append(f"{key}: the shipped film description is still prepended")
        if armed or named:
            failures.append(f"{key}: leftover reference images reach the prompt: {armed} {named}")

    print("\n=== only the references the caller sends stay on ===")
    graph = video_jobs.build_prompt("video_distill12", prompt="p",
                                    shot_references=[["a.png"], [], ["c1.png", "c2.png"]])
    slots = {node_id: node["inputs"] for node_id, node in graph.items()
             if node["class_type"] == "H3OptionalReferenceImage"}
    enabled = sorted(inputs.get("image") for inputs in slots.values() if inputs["enabled"])
    print("  启用:", enabled)
    if enabled != ["a.png", "c1.png", "c2.png"]:
        failures.append(f"the caller's own references are not the only enabled ones: {enabled}")

    print("\n=== upload route takes references and clips ===")
    print("  参考图:", sorted(video_routes.IMAGE_SUFFIXES),
          f"上限 {video_routes.IMAGE_MAX_BYTES // (1024 * 1024)} MB")
    print("  视频  :", sorted(video_routes.VIDEO_SUFFIXES),
          f"上限 {video_routes.VIDEO_MAX_BYTES // (1024 * 1024)} MB")
    for suffix in (".png", ".jpg", ".jpeg", ".webp"):
        if suffix not in video_routes.IMAGE_SUFFIXES:
            failures.append(f"参考图后缀 {suffix} 没被上传路由接受")
    for suffix in (".mp4", ".mov", ".webm"):
        if suffix not in video_routes.VIDEO_SUFFIXES:
            failures.append(f"视频后缀 {suffix} 没被上传路由接受")
    if video_routes.IMAGE_MAX_BYTES > video_routes.VIDEO_MAX_BYTES:
        failures.append("参考图的体积上限不该高于视频")
    if set(video_routes.IMAGE_SUFFIXES) & set(video_routes.VIDEO_SUFFIXES):
        failures.append("图片与视频的后缀集合不该重叠")

    print("\n=== structure guard rides along with the prompts ===")
    for guard in (False, True):
        graph = video_jobs.build_prompt("video_distill12", prompt="p", shots=["swing the spear"],
                                        shot_count=1, shot_seconds=[5.0],
                                        structure_guard=guard)
        text = graph["40"]["inputs"][video_jobs.SHOT_TEXT_INPUT]
        plan = json.loads(graph["50"]["inputs"]["storyboard"])["shots"][0]["prompt"]
        print(f"  structure_guard={str(guard):<5} 镜头文本={text.strip()[:52]!r} "
              f"带缓存节点={'80' in graph}")
        if guard and video_jobs.STRUCTURE_CONSTRAINT not in text:
            failures.append("structure_guard did not reach the shot text node")
        if guard and video_jobs.STRUCTURE_CONSTRAINT not in plan:
            failures.append("structure_guard did not reach the storyboard")
        if not guard and video_jobs.STRUCTURE_CONSTRAINT in text:
            failures.append("the constraint leaked in with the switch off")
        if "80" in graph:
            failures.append("a cache node reached the prompt")

    print("\n=== estimate scales with a shot's steps, seconds and recipe ===")
    def estimate(recipe_key, steps, seconds):
        probe = video_routes.VideoJob("v", recipe_key, "", {"shot_plan": [
            {"seconds": seconds, "steps": steps, "seed": 1}]})
        return video_routes._estimate_shot_seconds(probe, 1)

    for recipe_key, steps, seconds in (("video_distill12", 12, 7.5), ("video_distill12", 6, 7.5),
                                       ("video_distill12", 12, 5.0), ("video_distill3", 3, 7.5),
                                       ("video_balance6", 6, 7.5)):
        print(f"  {recipe_key:<16} {steps:>2} 步 {seconds:>4} s -> "
              f"{estimate(recipe_key, steps, seconds)/60:.1f} min")
    if estimate("video_distill12", 6, 7.5) >= estimate("video_distill12", 12, 7.5):
        failures.append("fewer steps should estimate lower")
    if estimate("video_distill12", 12, 5.0) >= estimate("video_distill12", 12, 7.5):
        failures.append("a shorter shot should estimate lower")
    if estimate("video_distill3", 3, 7.5) >= estimate("video_distill12", 12, 7.5):
        failures.append("the 3-step route should estimate lower than the 12-step route")

    print("\n=== routes registered ===")
    paths = sorted(route.path for route in video_routes.ROUTER.routes)
    for path in paths:
        print("  ", path)
    for expected in ("/local/video/recipes", "/local/video/generate", "/local/video/progress",
                     "/local/video/cancel", "/local/video/file", "/local/video/output",
                     "/local/video/folders", "/local/video/interpolate",
                     "/local/video/denoise", "/local/video/interpolate/progress"):
        if expected not in paths:
            failures.append(f"route missing: {expected}")

    print("\n=== prompt graph wiring (every route) ===")
    for entry in video_jobs.recipes():
        key = entry.key
        graph = video_jobs.build_prompt(key, prompt="a test film", seed=1)
        dangling = []
        for node_id, node in graph.items():
            for name, value in node["inputs"].items():
                if isinstance(value, list) and str(value[0]) not in graph:
                    dangling.append(f"{node_id}.{name}->{value[0]}")
        has_cache = any(n["class_type"] == "ApplyMiniMaxH3FirstBlockCache" for n in graph.values())
        print(f"  {key:<16} 节点={len(graph):<3} 缓存节点={has_cache} 悬空={dangling or '无'}")
        if dangling:
            failures.append(f"{key}: dangling links {dangling}")
        if has_cache:
            failures.append(f"{key}: a cache node reached the prompt")

    print("\n=== RIFE interpolation graph ===")
    graph = video_interpolate.build_prompt("film.mp4", "qwen-interp/x", 24.0, 2)
    chain = " -> ".join(f"{node_id}:{node['class_type']}" for node_id, node in sorted(graph.items()))
    print("  ", chain)
    print("   输出帧率:", graph["4"]["inputs"]["fps"], "倍率:", graph["3"]["inputs"]["multiplier"],
          "权重:", graph["3"]["inputs"]["ckpt_name"])
    dangling = [f"{node_id}.{name}->{value[0]}" for node_id, node in graph.items()
                for name, value in node["inputs"].items()
                if isinstance(value, list) and str(value[0]) not in graph]
    if dangling:
        failures.append(f"补帧图里有悬空连线：{dangling}")
    if graph["4"]["inputs"]["fps"] != 48.0:
        failures.append("补帧图的输出帧率不是 48")
    if graph["4"]["inputs"].get("audio") != ["2", 1]:
        failures.append("补帧图没有接上音轨")
    print("   发布文件名:", video_interpolate.published_name(Path(r"D:\out\shot_01.mp4"), 48))

    if failures:
        print("\nFAILURES:")
        for item in failures:
            print("  -", item)
        return 1
    print("\nall logic checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
