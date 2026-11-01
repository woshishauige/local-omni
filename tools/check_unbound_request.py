"""Static check: any function that reads `request` without it being a parameter,
a local, or a module-level global.

`require_feature(request, "chat")` belongs in an endpoint handler, where FastAPI injects
`request: Request`. Copied into an ordinary helper it becomes a NameError at call time -
and inside a synthesis helper that surfaces as an HTTP 500 that no test here would catch,
because the fallback path (the browser's own voice) looks like a normal outcome.
"""

import ast
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else r"D:\AI\Qwen-Local\mode_server.py"
tree = ast.parse(open(PATH, encoding="utf-8").read())

module_level = set()
for node in tree.body:
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                module_level.add(t.id)
    elif isinstance(node, ast.AnnAssign):
        if isinstance(node.target, ast.Name):
            module_level.add(node.target.id)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for a in node.names:
            module_level.add(a.asname or a.name.split(".")[0])
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        module_level.add(node.name)
    elif isinstance(node, ast.Try):
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign):
                for t in sub.targets:
                    if isinstance(t, ast.Name):
                        module_level.add(t.id)

print("module-level binding named 'request':", "request" in module_level)

def params(fn):
    a = fn.args
    names = [x.arg for x in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)]
    if a.vararg:
        names.append(a.vararg.arg)
    if a.kwarg:
        names.append(a.kwarg.arg)
    return set(names)

def binds(fn):
    """Names this function binds itself: parameters plus every assignment inside it."""
    names = set(params(fn))
    for x in ast.walk(fn):
        if isinstance(x, ast.Name) and isinstance(x.ctx, (ast.Store, ast.Del)):
            names.add(x.id)
        elif isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(x.name)
        elif isinstance(x, (ast.Import, ast.ImportFrom)):
            for a in x.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(x, ast.ExceptHandler) and x.name:
            names.add(x.name)
        elif isinstance(x, (ast.comprehension,)):
            for t in ast.walk(x.target):
                if isinstance(t, ast.Name):
                    names.add(t.id)
    return names


broken = []


def visit(node, enclosing):
    """enclosing: names bound by every function scope between the module and this node.

    A nested closure reading `request` is fine when an outer function owns it - `exchange()`
    inside `_synthesize()` reads the payload that function just built. Only a read that no
    scope can satisfy is a NameError at call time.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            own = binds(child)
            reads = [x for x in ast.walk(child)
                     if isinstance(x, ast.Name) and x.id == "request" and isinstance(x.ctx, ast.Load)]
            if reads and "request" not in own and "request" not in enclosing:
                broken.append((child.name, child.lineno, sorted({r.lineno for r in reads})))
            visit(child, enclosing | own)
        else:
            visit(child, enclosing)


visit(tree, module_level)

if broken:
    for name, line, where in broken:
        print(f"  BROKEN: {name}() defined at line {line} reads `request` at line(s) {where}"
              " - no parameter, no local, no global")
else:
    print("  no unbound `request` reads")
print("scan complete")
