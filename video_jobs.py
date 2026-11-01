"""Video generation through the shared ComfyUI, for the panel's video mode.

The H3 director graph ships as a ComfyUI *workflow* export (37 nodes, 36 links).
Rather than re-deriving that wiring — which would silently drift from the file the
user actually maintains — this module reads the exported graph for its wiring,
then emits the API prompt the way ComfyUI expects it: widget values belong on the
node's named inputs, and only the values the panel actually sets are sent, so the
node's own defaults cover everything else.

Input names were read from /object_info, not guessed:
  H3MP4ShotEditor        width height fps seconds_per_shot steps seed shot_index action
                         use_first_shot_reference, optional output_root and shot_NN_ref_MM
  H3OptionalReferenceImage  image enabled
  PrimitiveStringMultiline  value
  ApplyMiniMaxH3FirstBlockCache  mode threshold start_percent end_percent
                         max_consecutive_hits temporal_guard

Every route is one workflow file in the shipped workflow folder, so a route can be opened
and run in ComfyUI exactly as the panel runs it; the builder only fills in this request's
own values (prompts, per-shot plan, seed, canvas, references) over that file.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

import requests

COMFY_URL = "http://127.0.0.1:8188"
WIDGET_KINDS = ("COMBO", "INT", "FLOAT", "STRING", "BOOLEAN")
_SCHEMA_CACHE: dict[str, dict] = {}

# ComfyUI is expected as a sibling of this project (same layout image_comfy.py uses).
WORKFLOW_ROOT = (Path(__file__).resolve().parent.parent / "ComfyUI-BSAI_v010" / "ComfyUI"
                 / "user" / "default" / "workflows" / "MiniMax-H3")

DIRECTOR = 50
SHOT_TEXT_NODES = (40, 41, 42, 43)   # one description per shot, in shot order
SHOT_TEXT_INPUT = "value"
CACHE_NODE = 80


@dataclass(frozen=True)
class Recipe:
    """One video recipe: a workflow file plus the parameters it runs with.

    Everything a route needs lives in its workflow file — including the step-distillation
    LoRA loader, wired in front of the director's model input — so a route can be opened
    and run in ComfyUI exactly as the panel runs it. `params` only carries the defaults the
    panel applies when the recipe is selected.
    """

    key: str
    label: str
    file: str
    summary: str
    detail: str
    params: dict = field(default_factory=dict)

    def describe(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "summary": self.summary,
            "detail": self.detail,
            "params": dict(self.params),
            # What the workflow file itself wires, so the panel can offer the strength dial
            # only where a distillation LoRA exists.
            "lora": lora_in_template(self.file),
        }


RECIPES: tuple[Recipe, ...] = (
    # Three routes on the panel, one graph shape: the distillation LoRA wired in front of the
    # director, differing only in the step count. Detail is Laplacian variance on the same
    # sexpro shot (the retired 20-step native render scored 225 there), and every wall clock is
    # a measured 7.5 s / 768x512 shot: 3/6 steps from the full four-shot films, 12 steps from
    # its shot 1 (its continuation is the measured shot 1 scaled by 1.45). The 20-step
    # distillation entry was retired from the panel on the owner's ask: its workflow file stays
    # unlisted beside 01/02, and its measured 870 s lives on as a `video_cost` anchor.
    Recipe(
        key="video_distill12",
        label="精细 · 12 步蒸馏",
        file="视频生成/05_视频_精细_12步蒸馏.json",
        summary="12 步蒸馏：细节 746，一段 7.5 秒约 527 s（比原版 20 步快 33%）。",
        detail="细节与耗时的性价比拐点：细节是原版 20 步的 3.3 倍而更快。"
               "选中即把逐段步数设为 12。",
        params={"steps": 12, "shot_index": 1, "use_first_shot_reference": True, "cache": 0,
                "measured": {"shot": 527, "continuation": 764}},
    ),
    Recipe(
        key="video_balance6",
        label="均衡 · 6 步蒸馏",
        file="视频生成/04_视频_均衡_6步蒸馏.json",
        summary="6 步蒸馏：细节 666，一段 7.5 秒约 304 s。",
        detail="动作幅度接近原生的一档，四段实测 31.7 分钟（比 20 步原生快 1.6 倍）。"
               "选中即把逐段步数设为 6。",
        params={"steps": 6, "shot_index": 1, "use_first_shot_reference": True, "cache": 0,
                # Measured on the same sexpro four shots (7.5 s / 768x512): first shot
                # 304.3 s, continuations 339.3 / 435.6 / 825.7 s (median 435.6).
                "measured": {"shot": 304, "continuation": 436}},
    ),
    Recipe(
        key="video_balance6_nsfw",
        label="均衡 · 6 步蒸馏 + NSFW",
        file="视频生成/10_视频_NSFW_6步.json",
        summary="TaoMate 0.85 串 AfterMidnight v1.2 1.0：音频与基准持平，用时 +8%。",
        detail="走 H3LoraStack 之外的核心加载器串接 + `MiniMaxH3SigmaShift` + euler/beta（作者要求的组合）。"
               "6 步实测 301.0 s（基准 308.7 s）、音频 30.2e-3（基准 31.2e-3）、细节 1313、噪声 8.72。"
               "LoRA 必须先转格式：kohya 键名 + 非点号模块名会被 ComfyUI 静默跳过。",
        params={"steps": 6, "shot_index": 1, "use_first_shot_reference": True, "cache": 0},
    ),
    Recipe(
        key="video_distill3",
        label="极速 · 3 步蒸馏",
        file="视频生成/03_视频_极速_3步蒸馏.json",
        summary="3 步蒸馏：细节 534，一段 7.5 秒约 223 s（四段 19.5 分钟）。",
        detail="细节优先的最低档，也是这支 LoRA 的训练步数（TaoMate-H3-**3step**）。"
               "人脸检出率是一个档位能不能用的硬指标：同提示词同种子下 2 步只有 28%、"
               "3 步 55%、6 步 92%，所以 2 步档已下线（画面跳变、脸部保持不住）。",
        params={"steps": 3, "shot_index": 1, "use_first_shot_reference": True, "cache": 0,
                # Measured on sexpro's own four shots (7.5 s / 768x512): first shot 222.6 s,
                # the three continuations 372.3 / 255.1 / 320.1 s (median 320).
                "measured": {"shot": 223, "continuation": 320}},
    ),
)
RECIPE_INDEX = {recipe.key: recipe for recipe in RECIPES}


def recipe(key: str | None) -> Recipe | None:
    return RECIPE_INDEX.get(key or "")


def recipes() -> tuple[Recipe, ...]:
    return RECIPES


def reference_slot_nodes(workflow: dict) -> dict[str, int]:
    """``shot_0N_ref_0M`` -> the H3OptionalReferenceImage node wired into it.

    The director's reference slots are links rather than widget values, so which
    node feeds a slot is only knowable from the exported link table.
    """
    links = {link[0]: link for link in workflow["links"]}
    slots: dict[str, int] = {}
    for node in workflow["nodes"]:
        if node["type"] != "H3MP4ShotEditor":
            continue
        for slot in node.get("inputs") or []:
            if slot.get("link") is None:
                continue
            if slot["name"].startswith("shot_") and "_ref_" in slot["name"]:
                link = links.get(slot["link"])
                if link is not None:
                    slots[slot["name"]] = int(link[1])
    return slots


def load_template(recipe_key: str) -> dict:
    """The exported workflow as a mutable dict, or a clear error if it moved."""
    entry = recipe(recipe_key)
    if entry is None:
        raise ValueError(f"未知的视频工作流：{recipe_key}")
    path = WORKFLOW_ROOT / entry.file
    if not path.exists():
        raise RuntimeError(f"找不到视频工作流文件：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def lora_in_template(file: str) -> dict | None:
    """The distillation LoRA a workflow file wires, if any: its name and widget strength.

    Read from the file rather than restated in Python, so the panel offers the strength
    dial exactly where a LoRA exists and shows the value a run would use by default.
    `H3LoraStack` carries up to four (name, strength) pairs: the first filled slot is the
    one the dial speaks for.
    """
    path = WORKFLOW_ROOT / file
    if not path.exists():
        return None
    workflow = json.loads(path.read_text(encoding="utf-8"))
    for node in workflow.get("nodes") or []:
        if node["type"] not in ("LoraLoaderModelOnly", "MiniMaxH3TurboLoRA", "H3LoraStack"):
            continue
        values = node.get("widgets_values") or []
        if not values:
            continue
        if node["type"] == "H3LoraStack":
            for index in range(0, len(values) - 1, 2):
                if str(values[index]) not in ("None", ""):
                    return {"name": str(values[index]), "strength": float(values[index + 1])}
            continue
        return {"name": str(values[0]),
                "strength": float(values[1]) if len(values) > 1 else 1.0}
    return None


def schema_for(class_type: str) -> dict:
    """The node's declared inputs, from the server that will run it.

    A V3 node rejects a prompt that omits any required input — the node's own
    default is not applied by the API — so the builder needs the declaration
    order to turn the exported ``widgets_values`` into named inputs.
    """
    cached = _SCHEMA_CACHE.get(class_type)
    if cached is not None:
        return cached
    response = requests.get(f"{COMFY_URL}/object_info/{class_type}", timeout=30)
    response.raise_for_status()
    payload = response.json()
    if class_type not in payload:
        raise RuntimeError(f"ComfyUI 没有这个节点：{class_type}")
    schema = payload[class_type]["input"]
    _SCHEMA_CACHE[class_type] = schema
    return schema


def widget_input_names(schema: dict) -> list[str]:
    """Widget input names in the order widgets_values holds them.

    A ``control_after_generate`` option adds a frontend-only widget right after
    its integer, which is why the seed consumes two slots.
    """
    names: list[str] = []
    for section in ("required", "optional"):
        for name, spec in (schema.get(section) or {}).items():
            if not isinstance(spec, list) or not spec:
                continue
            kind = spec[0]
            options = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            if options.get("forceInput"):
                continue
            is_widget = isinstance(kind, list) or (isinstance(kind, str) and kind in WIDGET_KINDS)
            if not is_widget:
                continue
            names.append(name)
            if "control_after_generate" in options:
                names.append("control_after_generate")
    return names


def named_widgets(class_type: str, widgets: list) -> dict:
    """The exported widget values, keyed by the input names the API expects."""
    schema = schema_for(class_type)
    names = widget_input_names(schema)
    named = {}
    for name, value in zip(names, widgets or []):
        if name == "control_after_generate":
            continue
        named[name] = value
    return named


STRUCTURE_CONSTRAINT = (
    "Only one person in frame, framing stays at a medium shot or closer, exactly one clear "
    "action, camera holds steady, no extra people and no overlapping bodies."
)
"""Appended to every shot when the panel's structure switch is on.

The measured levers behind it (report sections 6 and 12): one subject per shot, one action
per shot, and framing that keeps the body large in frame. It is prompt-level only — the
graph itself is untouched, so it can be switched off without changing anything else.
"""


def build_prompt(recipe_key: str, prompt: str = "", shots: list[str] | None = None,
                 seed: int | None = None, width: int | None = None, height: int | None = None,
                 steps: int | None = None, seconds: float | None = None,
                 shot_index: int | None = None, first_shot_reference: bool | None = None,
                 shot_references: list[list[str]] | None = None, fps: int | None = None,
                 project_name: str | None = None,
                 use_first_shot_reference: bool | None = None,
                 shot_count: int | None = None,
                 shot_seconds: list[float] | None = None,
                 action: str | None = None,
                 output_root: str | None = None,
                 structure_guard: bool = False,
                 shot_01_context: str | None = None,
                 shot_continuity: list[str] | None = None,
                 lora_strength: float | None = None) -> dict:
    """Turn a recipe plus the panel's parameters into an API prompt.

    ``prompt`` describes the film and is written into every shot's description,
    which is how the shipped workflow is authored: each shot text starts with
    ``integrated_multimodal_description: [Shot N]`` and carries that shot's action.
    Anything the caller leaves as None keeps whatever the file ships with — except
    the two things that would otherwise leak the demo project into a new film:

    * the shipped ``global_prompt`` (the workflow author's own 923-character
      xianxia description) is cleared, because the director prefixes it to every
      shot and a panel film would silently inherit it;
    * every per-shot reference slot the caller did not fill is switched off, since
      the exported workflow remembers the images it was last saved with.

    H3MP4ShotEditor renders exactly one shot per run, so ``shot_index`` and
    ``action`` select which one: ``next_shot`` takes the first shot the project is
    still missing, ``redo_shot`` takes ``shot_index``. Per-shot step counts and
    seeds follow from that — each shot is its own submission — while width and
    height stay shared because the clips are concatenated into one film.
    """
    workflow = load_template(recipe_key)
    nodes = {node["id"]: node for node in workflow["nodes"]}
    links = {link[0]: link for link in workflow["links"]}

    if DIRECTOR not in nodes or nodes[DIRECTOR]["type"] != "H3MP4ShotEditor":
        raise RuntimeError("视频工作流里找不到 H3MP4ShotEditor（节点 50）")

    # Shot durations. The director reads a per-shot "seconds" only from a JSON
    # storyboard, so per-shot timing means handing it that document directly and
    # bypassing the template's text-concatenation chain.
    plan_seconds = [float(value) for value in (shot_seconds or []) if value]

    widget_inputs: dict[int, dict] = {}

    def set_widget(node_id: int, name: str, value) -> None:
        widget_inputs.setdefault(node_id, {})[name] = value

    # Every widget the export carries becomes a named input, because a V3 node
    # rejects a prompt that omits a required input rather than falling back to its
    # own default. Overrides below are applied on top of this baseline.
    #
    # A name that the export feeds with a link must NOT be taken from the widget
    # list: the exported widgets_values only holds the values that are still
    # widgets, so zipping the whole schema order against it slides the remaining
    # values onto the wrong names. StringConcatenate is exactly this case — its
    # string_a/string_b are links while delimiter is the only real widget.
    for node in workflow["nodes"]:
        if node["type"] == "MarkdownNote":
            continue
        linked_names = {slot["name"] for slot in (node.get("inputs") or [])
                        if slot.get("link") is not None}
        named = named_widgets(node["type"], node.get("widgets_values"))
        named = {name: value for name, value in named.items() if name not in linked_names}
        if named:
            widget_inputs.setdefault(node["id"], {}).update(named)

    # The four shot-text nodes are concatenated with no delimiter and the director
    # splits the storyboard on a line-initial "[Shot N]" heading, so each node needs
    # both its own heading and a leading newline. Without the newline the joined
    # text is one line, only the first heading is line-initial, and the whole
    # storyboard collapses into a single shot.
    def heading(position: int, text: str) -> str:
        body = text.strip()
        if body.lower().startswith("integrated_multimodal_description:"):
            return "\n" + body
        return f"\nintegrated_multimodal_description: [Shot {position}] {body}"

    def guarded(text: str) -> str:
        """The optional structure constraint rides along with each shot's own text."""
        body = (text or "").strip()
        if not structure_guard:
            return body
        return f"{body} {STRUCTURE_CONSTRAINT}".strip() if body else STRUCTURE_CONSTRAINT

    if prompt and prompt.strip():
        text = guarded(prompt)
        for position, node_id in enumerate(SHOT_TEXT_NODES, start=1):
            if node_id in nodes:
                set_widget(node_id, SHOT_TEXT_INPUT, heading(position, text))

    plan = [str(shot) for shot in (shots or ())]
    # Per-shot 续接 / 切镜. Only used by the JSON storyboard below; the text nodes have no
    # field for it, and the director's own default is continue.
    shot_continuity = list(shot_continuity or [])
    for position, (node_id, shot) in enumerate(zip(SHOT_TEXT_NODES, plan), start=1):
        if node_id in nodes and shot.strip():
            set_widget(node_id, SHOT_TEXT_INPUT, heading(position, guarded(shot)))

    # The export ships four shot-text nodes, so a request for fewer shots has to
    # blank the extra nodes: they are concatenated into the storyboard whatever
    # they contain, and a leftover copy of the film description would be planned as
    # another shot.
    if shot_count is not None:
        for node_id in SHOT_TEXT_NODES[int(shot_count):]:
            if node_id in nodes:
                set_widget(node_id, SHOT_TEXT_INPUT, "")
    elif plan:
        for node_id in SHOT_TEXT_NODES[len(plan):]:
            if node_id in nodes:
                set_widget(node_id, SHOT_TEXT_INPUT, "")

    # A JSON storyboard carries a duration per shot, which the text form cannot.
    # Handing the director this document sets each shot's length; the text nodes
    # above remain filled so the graph is still readable in ComfyUI.
    if plan_seconds:
        count = max(len(plan), len(plan_seconds))
        shots_json = {"shots": [
            {
                "prompt": guarded((plan[index] if index < len(plan) else "") or prompt or ""),
                "seconds": plan_seconds[index] if index < len(plan_seconds) else 7.5,
                # "continue" pins the previous shot's tail as this shot's opening context,
                # "cut" starts it fresh. The director treats a missing field as continue, so
                # only an explicit cut is written into the document.
                **({"continuity": "cut"}
                   if (shot_continuity[index] if index < len(shot_continuity) else "continue")
                   == "cut" else {}),
            }
            for index in range(count)
        ]}
        set_widget(DIRECTOR, "storyboard", json.dumps(shots_json, ensure_ascii=False))

    if width:
        set_widget(DIRECTOR, "width", int(width))
    if height:
        set_widget(DIRECTOR, "height", int(height))
    if seconds:
        set_widget(DIRECTOR, "seconds_per_shot", float(seconds))
    if steps:
        set_widget(DIRECTOR, "steps", int(steps))
    if seed is not None and int(seed) >= 0:
        set_widget(DIRECTOR, "seed", int(seed))
    if shot_index is not None:
        set_widget(DIRECTOR, "shot_index", int(shot_index))
    if action:
        set_widget(DIRECTOR, "action", str(action))
    if use_first_shot_reference is not None:
        set_widget(DIRECTOR, "use_first_shot_reference", bool(use_first_shot_reference))
    elif first_shot_reference is not None:
        set_widget(DIRECTOR, "use_first_shot_reference", bool(first_shot_reference))
    if fps:
        set_widget(DIRECTOR, "fps", int(fps))
    # The director prefixes global_prompt to every shot, so the file's own demo
    # description is cleared: a panel film is described by what the panel says and
    # by nothing else.
    set_widget(DIRECTOR, "global_prompt", "")
    if output_root and str(output_root).strip():
        # Where the project folder is created; the node's own default stays
        # output/video/H3-30s so workflows opened in ComfyUI are unaffected.
        set_widget(DIRECTOR, "output_root", str(output_root).strip())
    if project_name and str(project_name).strip():
        # The director writes into output/video/H3-30s/<project_name>/, so this is
        # the save folder the panel exposes.
        set_widget(DIRECTOR, "project_name", str(project_name).strip())
    if shot_01_context and str(shot_01_context).strip():
        # Shot 1 continues from this clip the same way shot 2 continues from shot 1.
        set_widget(DIRECTOR, "shot_01_context", str(shot_01_context).strip())

    # Per-shot reference images: shot N's list fills the director's shot_0N_ref_0M
    # slots. Those slots are wires from H3OptionalReferenceImage nodes rather than
    # widget values, so the mapping is read from the exported link table. Every slot
    # the caller left empty is switched off here: the export remembers whatever it
    # was last saved with, and a run that uploaded nothing must not condition on it.
    slot_nodes = reference_slot_nodes(workflow)
    filled: set[str] = set()
    for shot_position, refs in enumerate(shot_references or [], start=1):
        usable = [str(name).strip() for name in (refs or []) if str(name).strip()]
        for slot, image_name in enumerate(usable[:5], start=1):
            key = f"shot_{shot_position:02d}_ref_{slot:02d}"
            node_id = slot_nodes.get(key)
            if node_id is None:
                continue
            filled.add(key)
            set_widget(node_id, "image", image_name)
            set_widget(node_id, "enabled", True)
    for key, node_id in slot_nodes.items():
        if key not in filled:
            # Cleared, not merely disabled: the prompt then says the slot is empty
            # instead of naming an image the file happened to be saved with.
            set_widget(node_id, "image", "__none__")
            set_widget(node_id, "enabled", False)

    # The shipped graph carries a first-block cache node in bypass (mode 4). No route
    # enables it any more: measured, it bought 1.08x and cost picture differences, and on
    # top of a distillation LoRA it was slower than the LoRA alone (330.5 s vs 280.5 s).
    # Its widget values stay from the export so a hand-enabled run behaves as before.
    cache = nodes.get(CACHE_NODE)
    if cache is not None:
        cache["mode"] = 4
        set_widget(CACHE_NODE, "threshold", 0.08)
        set_widget(CACHE_NODE, "start_percent", 0.10)
        set_widget(CACHE_NODE, "end_percent", 0.95)
        set_widget(CACHE_NODE, "max_consecutive_hits", 1)
        set_widget(CACHE_NODE, "temporal_guard", True)

    # Build the API graph. A bypassed node is not submitted, and anything that
    # pointed at it has to read that node's own upstream input instead —
    # ComfyUI's frontend does this remap when it converts the graph, so the
    # exported workflow's link table still points at the bypassed node.
    prompt_graph: dict[str, dict] = {}
    upstream: dict[str, list] = {}
    for node in workflow["nodes"]:
        if node["type"] == "MarkdownNote":
            continue
        inputs: dict = {}
        for slot in node.get("inputs") or []:
            link_id = slot.get("link")
            if link_id is None:
                continue
            link = links.get(link_id)
            if link is not None:
                inputs[slot["name"]] = [str(link[1]), link[2]]
        # The cache node is bypassed in the quality recipe, so its consumers take
        # whatever feeds the cache node itself.
        if node["id"] == CACHE_NODE:
            upstream[str(node["id"])] = inputs.get("model", ["1", 0])
        inputs.update(widget_inputs.get(node["id"], {}))
        prompt_graph[str(node["id"])] = {
            "class_type": node["type"],
            "inputs": inputs,
            "_meta": {"title": node.get("title", node["type"])},
        }
    prompt_graph.pop(str(CACHE_NODE), None)
    if lora_strength is not None:
        # The distill recipes carry their LoRA in the graph; the panel can dial it without
        # editing the file. A request that asks for a strength on a graph with no LoRA is
        # a mistake worth failing loudly on rather than ignoring.
        loras = [entry for entry in prompt_graph.values()
                 if entry["class_type"] in ("LoraLoaderModelOnly", "MiniMaxH3TurboLoRA",
                                            "H3LoraStack")]
        if not loras:
            raise RuntimeError("这条配方里没有蒸馏 LoRA，无法设置强度")
        for entry in loras:
            if entry["class_type"] == "H3LoraStack":
                # The stack holds up to four LoRAs; slot 1 is the one the dial speaks for.
                entry["inputs"]["strength_1"] = round(min(1.2, max(0.4, float(lora_strength))), 3)
                continue
            key = "strength_model" if "strength_model" in entry["inputs"] else "strength"
            entry["inputs"][key] = round(min(1.2, max(0.4, float(lora_strength))), 3)
    for entry in prompt_graph.values():
        for name, value in list(entry["inputs"].items()):
            if isinstance(value, list) and value and str(value[0]) not in prompt_graph:
                replacement = upstream.get(str(value[0]))
                if replacement is None:
                    raise RuntimeError(
                        f"video graph references node {value[0]} which is not in the prompt")
                entry["inputs"][name] = list(replacement)
    return prompt_graph
