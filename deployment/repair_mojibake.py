"""Reverse a "UTF-8 source read as CP936 and rewritten as UTF-8" corruption.

Reading a UTF-8 file with the system ANSI code page (CP936) and writing it back
as UTF-8 turns every Chinese character into mojibake. The mapping is per *byte
pair*, and a few byte pairs land in Windows CP936's user-defined areas, which
Python's own cp936 table does not know, so a plain re-encode cannot invert them.

The trick used here: rebuild the byte stream pair by pair, leaving a wildcard
for the pairs Python cannot map, then fill each wildcard by matching the
surrounding run against the string constants of the pre-corruption ``.pyc``,
which still states the original characters exactly.
"""

from __future__ import annotations

import ast
import importlib.util
import itertools
import marshal
import sys
from pathlib import Path

PUA = ((0xE000, 0xF8FF),)


def two_byte_table() -> dict[str, bytes]:
    """Every character this CP936 implementation can spell as two bytes."""
    table: dict[str, bytes] = {}
    for lead in range(0x81, 0xFF):
        for trail in list(range(0x40, 0x7F)) + list(range(0x80, 0xFF)):
            try:
                char = bytes([lead, trail]).decode("cp936")
            except UnicodeDecodeError:
                continue
            table.setdefault(char, bytes([lead, trail]))
    return table


def byte_width(char: str, table: dict[str, bytes]) -> bytes:
    """The original bytes behind one corrupted character, ``?`` when unknown."""
    if char == "?":                            # an unmappable pair, replaced by ANSI
        return b"?"
    if char == "\u20ac":                       # CP936 spells byte 0x80 as €
        return b"\x80"
    if any(low <= ord(char) <= high for low, high in PUA):
        return b"?"                            # one unmapped byte pair
    if ord(char) < 0x80:
        return char.encode("ascii")
    return table.get(char, b"?")


def runs(text: str) -> list[tuple[int, int]]:
    """Character ranges of maximal corrupted runs.

    A question mark only belongs to a run when it continues one: the ANSI
    decoder writes ``?`` for byte pairs it cannot map, and a genuine question
    mark in the source always sits between ASCII characters.
    """
    spans = []
    start = None
    for index, char in enumerate(text + " "):
        corrupted = ord(char) > 127 or (char == "?" and start is not None)
        if corrupted:
            start = index if start is None else start
        elif start is not None:
            spans.append((start, index))
            start = None
    return spans


def pyc_constants(path: Path) -> set[str]:
    """Every string constant of a compiled module, including nested code."""
    data = path.read_bytes()
    if data[:4] != importlib.util.MAGIC_NUMBER:
        raise SystemExit(f"{path}: not a {sys.version_info.major}.{sys.version_info.minor} bytecode file")
    found: set[str] = set()

    def walk(item) -> None:
        for const in item.co_consts:
            if isinstance(const, str):
                found.add(const)
            elif hasattr(const, "co_consts"):
                walk(const)

    walk(marshal.loads(data[16:]))
    return found


def candidates(units: list[bytes], constants: set[str]) -> set[tuple[str, int, int]]:
    """Windows inside constants that could have produced this corrupted run.

    Each unknown character hides one or two bytes (the ANSI decoder writes one
    ``?`` for a pair it cannot map, and a trailing lone byte also becomes one),
    so every width combination is tried and matched byte by byte.
    """
    unknown = [index for index, unit in enumerate(units) if unit == b"?"]
    found: set[tuple[str, int, int]] = set()
    for widths in itertools.product((1, 2), repeat=len(unknown)):
        pattern = b""
        for index, unit in enumerate(units):
            pattern += b"?" * widths[unknown.index(index)] if index in unknown else unit
        for constant in constants:
            raw = constant.encode("utf-8")
            if len(raw) < len(pattern):
                continue
            for start in range(len(raw) - len(pattern) + 1):
                window = raw[start:start + len(pattern)]
                if all(known == ord("?") or known == actual for known, actual in zip(pattern, window)):
                    found.add((constant, start, start + len(pattern)))
    return found


def anchored(raw: bytes, begin: int, finish: int, left: str, right: str) -> bool:
    """Whether the surviving ASCII neighbours still fit around this window.

    A swallowed byte pair can eat an ASCII character too, so an anchor may sit a
    byte or two away from the window; and an f-string splits one literal into
    several constants, so only the longest anchor that exists in this constant
    is required to match.
    """
    if left:
        anchor = left.encode("utf-8")[-6:]
        fits = False
        for size in range(len(anchor), 0, -1):
            piece = anchor[len(anchor) - size:]
            position = raw.rfind(piece, 0, begin + 1)
            if position >= 0 and begin - (position + size) <= 3:
                fits = True
                break
        if not fits and raw.rfind(anchor[-1:], 0, begin + 1) >= 0:
            return False
    if right:
        anchor = right.encode("utf-8")[:6]
        fits = False
        for size in range(len(anchor), 0, -1):
            piece = anchor[:size]
            position = raw.find(piece, finish)
            if position >= 0 and position - finish <= 3:
                fits = True
                break
        if not fits and raw.find(anchor[:1], finish) >= 0:
            return False
    return True


def swallowed_quotes(text: str, ends: list[int]) -> str:
    """Put back quote characters the ANSI decoder ate with its ``?`` byte.

    A literal that ends right after a Chinese character loses its closing quote
    to the byte pair the decoder cannot map: ``。`` + ``"`` becomes ``銆?``. The
    constants cannot show that quote, so it is restored from the quote balance
    of the offending line, at the end of the run that swallowed it.
    """
    track = sorted(range(len(ends)), key=lambda index: ends[index])
    used: set[int] = set()
    for _ in range(60):
        try:
            ast.parse(text)
            return text
        except SyntaxError as exc:
            lines = text.split("\n")
            start = sum(len(line) + 1 for line in lines[:exc.lineno - 1])
            line = lines[exc.lineno - 1]
            # The parser reports the error at the first token after the literal
            # that lost its quote, so any unused run end on that line can be the
            # place the quote belongs; the largest one is tried first.
            inside = [index for index in track
                      if index not in used and ends[index] <= start + len(line)]
            if not inside:
                inside = [index for index in track if index not in used]
            if not inside:
                raise
            index = max(inside, key=lambda item: ends[item])
            position = ends[index]
            opening = "".join(char for char in text[start:position] if char in "\"'")[-1:]
            if not opening:
                raise
            text = text[:position] + opening + text[position:]
            used.add(index)
            ends = [end + 1 if end >= position else end for end in ends]
            if len(used) > len(track):
                raise
    raise SystemExit("could not close every swallowed quote")


def repair(source: Path, pyc: Path, output: Path) -> int:
    table = two_byte_table()
    constants = pyc_constants(pyc)
    text = source.read_bytes().decode("utf-8").lstrip("\ufeff")

    replacements: list[tuple[int, int, str, bool]] = []
    unresolved = 0
    damaged = 0
    for start, end in runs(text):
        chunk = text[start:end]
        pattern = b"".join(byte_width(char, table) for char in chunk)
        if b"?" not in pattern:
            # A swallowed byte pair can merge with an ASCII neighbour and still
            # spell valid characters, so undamaged-looking runs are re-read too.
            # With no wildcard the byte stream is exact, so its own decoding is
            # the only possible original text.
            try:
                original = pattern.decode("utf-8")
            except UnicodeDecodeError:
                original = ""
            if not original or original == chunk:
                continue
            damaged += 1
            replacements.append((start, end, original, False))
            continue
        damaged += 1
        units = [byte_width(char, table) for char in chunk]
        found = candidates(units, constants)
        windows = {(constant, begin, finish) for constant, begin, finish in found}
        left, right = text[max(0, start - 8):start], text[end:end + 8]
        narrow = {(constant, begin, finish) for constant, begin, finish in windows
                  if anchored(constant.encode("utf-8"), begin, finish, left, right)}
        pool = narrow or windows
        # The right candidate is a piece of the longest literal it could belong
        # to, and an unmappable pair hides two bytes rather than one, so the
        # longest constant and the widest window win.
        longest = max(len(constant.encode("utf-8")) for constant, _, _ in pool)
        pool = {(constant, begin, finish) for constant, begin, finish in pool
                if len(constant.encode("utf-8")) == longest}
        widest = max(finish - begin for _, begin, finish in pool)
        pool = {(constant, begin, finish) for constant, begin, finish in pool
                if finish - begin == widest}
        originals = {constant.encode("utf-8")[begin:finish] for constant, begin, finish in pool}
        if len(originals) != 1:
            unresolved += 1
            print(f"  UNRESOLVED ({len(windows)} windows, {len(narrow)} anchored) "
                  f"chars {start}-{end}: {chunk!r}")
            continue
        raw = originals.pop()
        # When the widest reading needs more bytes than the literal offers, the
        # pair swallowed the character after it (usually the closing quote).
        greedy = sum(2 if unit == b"?" else len(unit) for unit in units)
        try:
            replacements.append((start, end, raw.decode("utf-8"), greedy > widest))
        except UnicodeDecodeError as exc:
            unresolved += 1
            print(f"  BROKEN WINDOW chars {start}-{end}: {chunk!r} ({exc})")

    fixed = text
    for start, end, original, _ in reversed(replacements):
        fixed = fixed[:start] + original + fixed[end:]
    # Ends of the runs that hid bytes, in the repaired text: only those can have
    # swallowed the quote that closed their literal.
    shift = 0
    ends: list[int] = []
    for start, end, original, wild in replacements:
        shift += len(original) - (end - start)
        if wild:
            ends.append(end + shift)
    fixed = swallowed_quotes(fixed, sorted(ends))
    # Write bytes, not text: text mode would turn every LF into CRLF and the
    # file already carries its own line endings.
    output.write_bytes(fixed.replace("\r\n", "\n").encode("utf-8"))
    print(f"{source.name}: {damaged} damaged runs, {len(replacements)} solved, "
          f"{unresolved} unresolved -> {output}")
    return unresolved


if __name__ == "__main__":
    raise SystemExit(repair(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])))
