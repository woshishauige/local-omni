"""One-off: fix the model-file echo in direct_image_run.py."""
from pathlib import Path

path = Path(r"D:\AI\deployment\direct_image_run.py")
text = path.read_text(encoding="utf-8")
old = """    print("nodes:", len(graph), "| model files:",
          {key: graph[node]["inputs"].get(input_name)
           for node, input_name in (("1", "unet_name"), ("2", "clip_name"), ("3", "vae_name")) if node in graph})"""
new = """    files = {}
    for node in ("1", "2", "3"):
        if node in graph:
            for name, value in graph[node]["inputs"].items():
                if name.endswith("_name") and isinstance(value, str):
                    files[f"{node}.{name}"] = value
    print("nodes:", len(graph), "| model files:", files)"""
assert old in text
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("fixed")
