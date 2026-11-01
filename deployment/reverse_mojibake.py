"""Reverse a CP936-mangled file whose original text is not in a .pyc.

Same corruption as ``repair_mojibake`` but with no constant oracle: the byte
stream is rebuilt pair by pair and every unknown pair is listed with its line
and neighbours, so a handful of spots can be settled by hand.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from repair_mojibake import two_byte_table  # noqa: E402

MARK = "\x00"


def reverse(path: Path, output: Path, report: bool = True) -> int:
    table = two_byte_table()
    text = path.read_bytes().decode("utf-8").lstrip("\ufeff")
    out = bytearray()
    unknown: list[int] = []
    for char in text:
        if char == "\u20ac":
            out.append(0x80)
            continue
        if any(0xE000 <= ord(char) <= 0xF8FF for _ in (0,)):
            out += b"\x00\x00"
            unknown.append(len(out) - 2)
            continue
        chunk = table.get(char)
        if chunk is None and ord(char) > 127:
            out += b"\x00\x00"
            unknown.append(len(out) - 2)
            continue
        out += char.encode("ascii") if ord(char) < 128 else chunk
    data = bytes(out)
    print(f"{path.name}: {len(text)} chars -> {len(data)} bytes, {len(unknown)} unknown pairs")
    if report:
        lines_before = data[:]
        for offset in unknown:
            start = max(0, offset - 60)
            print(f"  offset {offset}: ...{lines_before[start:offset + 60]!r}...")
    if not unknown:
        output.write_bytes(data.decode("utf-8").encode("utf-8"))
        print(f"clean reversal -> {output}")
        return 0
    output.write_bytes(data)
    print(f"partial reversal (unknown pairs kept as NULs) -> {output}")
    return len(unknown)


if __name__ == "__main__":
    raise SystemExit(reverse(Path(sys.argv[1]), Path(sys.argv[2])))
