"""Print the toolbar's own wording, so tests can assert the exact strings it shows."""
import json
import re
import sys
from pathlib import Path

source = Path(__file__).resolve().parent.parent / "web" / "mode-control.js"
block = source.read_text(encoding="utf-8").split("const words = {", 1)[1].split("};", 1)[0]
words = {}
for key, raw in re.findall(r'(\w+):\s*"((?:[^"\\]|\\.)*)"', block):
    try:
        words[key] = json.loads('"' + raw + '"')
    except ValueError:
        words[key] = raw
if len(sys.argv) > 1:
    for name in sys.argv[1:]:
        print(f"{name} = {words.get(name)!r}")
else:
    print(json.dumps(words, ensure_ascii=False, indent=2, sort_keys=True))
