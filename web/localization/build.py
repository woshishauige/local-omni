"""Translate reviewed JS literals while preserving Svelte markup and expressions."""

import html.parser
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
NODE = "D:/nodejs/node.exe"
COMMAND = [NODE, "--expose-internals", str(ROOT / "catalog.cjs")]


class TextSegments(html.parser.HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.lines = source.splitlines(keepends=True)
        self.segments = []
        self.feed(source)

    def source_offset(self):
        line, column = self.getpos()
        return sum(map(len, self.lines[:line - 1])) + column

    def handle_data(self, text):
        if re.search(r"[A-Za-z]", text):
            self.segments.append((self.source_offset(), self.source_offset() + len(text), text))

    def handle_starttag(self, tag, attrs):
        raw = self.get_starttag_text()
        for name, value in attrs:
            if name not in {"title", "aria-label", "placeholder", "alt"} or not value:
                continue
            match = re.search(r"\b" + re.escape(name) + r"\s*=\s*([\"'])(.*?)\1", raw, re.S)
            if match and re.search(r"[A-Za-z]", match[2]):
                start = self.source_offset() + match.start(2)
                self.segments.append((start, start + len(match[2]), match[2]))


def segments(value):
    if re.search(r"<(?:[A-Za-z][^>]*|!)[>]", value):
        return TextSegments(value).segments
    return [(0, len(value), value)]


catalog = json.loads(subprocess.check_output(COMMAND, text=True, encoding="utf-8"))
if len(sys.argv) > 1 and sys.argv[1] == "extract":
    groups = {"settings": [], "chat": [], "advanced": []}
    for node in catalog["nodes"]:
        group = "settings" if node["id"] < 500000 else "chat" if node["id"] < 2740000 else "advanced"
        for start, end, text in segments(node["value"]):
            stripped = text.strip()
            if not re.search(r"[A-Za-z]{2}", stripped) or len(stripped) > 3000:
                continue
            if not (re.search(r"[A-Za-z] [A-Za-z]", stripped) or re.fullmatch(r"[A-Z][A-Za-z -]{1,45}[.!?:]?", stripped) or re.search(r"[A-Za-z].*\{\d+\}|\{\d+\}.*[A-Za-z]", stripped)):
                continue
            groups[group].append({"id": node["id"], "start": start, "end": end, "source": text, "target": "", "context": node["context"]})
    for name, values in groups.items():
        (ROOT / f"{name}.catalog.json").write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    print({name: len(values) for name, values in groups.items()})
else:
    reviewed = []
    segment_index = {f"{node['id']}:{start}": {"id": node["id"], "start": start, "end": end, "source": text}
                     for node in catalog["nodes"] for start, end, text in segments(node["value"])}
    for path in sorted(ROOT.glob("*.zh-CN.json")):
        translated = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(translated, dict):
            reviewed.extend(segment_index[key] | {"target": value} for key, value in translated.items())
        else:
            reviewed.extend(translated)
    by_id = {}
    for item in reviewed:
        if item.get("target") is None:
            continue
        by_id.setdefault(item["id"], []).append(item)
    nodes = {node["id"]: node for node in catalog["nodes"]}
    edits = []
    for node_id, items in by_id.items():
        value = nodes[node_id]["value"]
        original = value
        previous = len(value)
        for item in sorted(items, key=lambda item: item["start"], reverse=True):
            start, end = item["start"], item["end"]
            if value[start:end] != item["source"] or end > previous:
                raise ValueError(f"Translation source mismatch at {node_id}:{start}")
            value = value[:start] + item["target"] + value[end:]
            previous = start
        edits.append({"id": node_id, "source": original, "target": value})
    (ROOT / "edits.json").write_text(json.dumps(edits, ensure_ascii=False), encoding="utf-8")
    subprocess.run(COMMAND + ["build"], check=True)
