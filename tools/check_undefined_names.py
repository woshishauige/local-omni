"""Find names a function reads from global scope that the module never defines.

Written after a stray `require_feature(request, "chat")` was pasted into a helper that takes
no `request`: the name resolved to nothing, the helper raised NameError, and the endpoint
reported an opaque 500 that the caller silently absorbed. Nothing in the file looked wrong -
the line is correct everywhere else it appears.

`request` was one instance of a general failure mode, so this checks the whole file for it:
every function's global-scope reads are collected with symtable and compared against the
names the module actually binds (including bindings nested inside top-level try/if blocks,
but not inside function or class bodies, which are their own scopes).

Usage: python tools/check_undefined_names.py <file.py>
"""

import ast
import builtins
import symtable
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else r"D:\AI\Qwen-Local\mode_server.py"
source = open(PATH, encoding="utf-8").read()
tree = ast.parse(source)


def collect_module_names(statements, into):
    """Bindings visible at module scope: the statement list plus nested blocks in it."""
    for node in statements:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                for name in ast.walk(t):
                    if isinstance(name, ast.Name):
                        into.add(name.id)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            for name in ast.walk(node.target):
                if isinstance(name, ast.Name):
                    into.add(name.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                into.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            into.add(node.name)
        elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
            for field in ("body", "orelse", "finalbody", "handlers"):
                sub = getattr(node, field, None)
                if isinstance(sub, list) and sub and isinstance(sub[0], ast.ExceptHandler):
                    for handler in sub:
                        if handler.name:
                            into.add(handler.name)
                        collect_module_names(handler.body, into)
                elif isinstance(sub, list):
                    collect_module_names(sub, into)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            into.add(node.name)


module_names = set()
collect_module_names(tree.body, module_names)
known = module_names | set(dir(builtins))

top = symtable.symtable(source, PATH, "exec")
problems = []


def walk(table, path):
    for child in table.get_children():
        name = f"{path}.{child.get_name()}"
        if child.get_type() == "function":
            for symbol in child.get_symbols():
                # A global read: referenced here, not bound here, not a free variable.
                if symbol.is_global() and symbol.is_referenced() and not symbol.is_assigned():
                    if symbol.get_name() not in known:
                        problems.append((child.get_name(), symbol.get_name(), child.get_lineno()))
        walk(child, name)


walk(top, "")

if problems:
    for func, name, line in problems:
        print(f"  UNDEFINED: {func}() (line {line}) reads global `{name}`, "
              "which the module never binds -> NameError at call time")
else:
    print("  every global name a function reads is defined by the module or is a builtin")

print(f"module bindings: {len(module_names)}   functions scanned: "
      f"{sum(1 for _ in ast.walk(tree) if isinstance(_, (ast.FunctionDef, ast.AsyncFunctionDef)))}")
