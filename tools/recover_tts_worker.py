"""Recover the original tts_server.py's engine construction from its stale bytecode.

I rewrote tools/tts_server.py having read only its head and tail, and reconstructed the middle
`ensure_engine()` from inference - which was wrong in three ways (a bad import, a default model
directory, and local_playback left on). Rather than guess again, this reads the bytecode that the
original file produced, which still carries its constants and its disassembly.

Usage: tools\\tts-venv\\Scripts\\python.exe tools\\recover_tts_worker.py
"""

import dis
import marshal
import sys
from pathlib import Path

PATH = Path(r"D:\AI\Qwen-Local\tools\__pycache__\tts_server.cpython-313.pyc")
WANTED = {"ensure_engine", "_config", "synthesize", "main"}

if not PATH.is_file():
    print(f"no bytecode at {PATH}")
    sys.exit(1)

print(f"python {sys.version.split()[0]} reading {PATH.name}")
code = marshal.loads(PATH.read_bytes()[16:])   # 16-byte header since 3.7


def walk(node, depth=0):
    for const in node.co_consts:
        if not hasattr(const, "co_code"):
            continue
        args = const.co_varnames[:const.co_argcount]
        print(f"\n{'  ' * depth}def {const.co_name}({', '.join(args)})")
        if const.co_name in WANTED:
            strings = [c for c in const.co_consts if isinstance(c, str)]
            numbers = [c for c in const.co_consts if isinstance(c, (int, float)) and not isinstance(c, bool)]
            print(f"{'  ' * depth}  strings: {strings}")
            print(f"{'  ' * depth}  numbers: {numbers}")
            print(f"{'  ' * depth}  names  : {list(const.co_names)}")
            print(f"{'  ' * depth}  --- disassembly ---")
            for line in dis.Bytecode(const).dis().splitlines():
                print(f"{'  ' * depth}  {line}")
        walk(const, depth + 1)


walk(code)
