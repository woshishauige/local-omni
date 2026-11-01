"""Print the tensor inventory of a safetensors file without importing torch."""
import json
import struct
import sys
from pathlib import Path

path = Path(sys.argv[1])
with open(path, "rb") as handle:
    header_len = struct.unpack("<Q", handle.read(8))[0]
    header = json.loads(handle.read(header_len))

keys = [k for k in header if k != "__metadata__"]
print("tensor count:", len(keys))
for key in sorted(keys):
    info = header[key]
    print("%-58s %-8s %s" % (key, info["dtype"], info["shape"]))
