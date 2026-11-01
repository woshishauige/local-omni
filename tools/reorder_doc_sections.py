"""Put the numbered sections of the sharing document back in order.

Written as a file because the rearrangement needs exact string handling, and a PowerShell
here-string round trip is exactly how Chinese text got mangled in this repository before.
"""
import re
from pathlib import Path

path = Path(__file__).resolve().parent.parent / "共享给其他人使用.md"
text = path.read_text(encoding="utf-8")

start = text.index("### 0.15 ")
end = text.index("### 0.14 ")
block = text[start:end]
rest = text[:start] + text[end:]
ordered = rest.rstrip() + "\n\n" + block.rstrip() + "\n"
path.write_text(ordered, encoding="utf-8")

for match in re.finditer(r"^### 0\.\d+.*$", ordered, re.M):
    print(ordered[:match.start()].count("\n") + 1, match.group(0))
print("总行数:", ordered.count("\n") + 1)
