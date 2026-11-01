"""Cross-check every endpoint the panel calls against the routes that exist.

The panel is plain browser JavaScript, so a typo in a fetch path only shows up as
a failed request in the UI. This compares the two sides statically and also asks
the running gateway whether each path answers.
"""
import re
import sys
from pathlib import Path

import requests

WEB = Path(__file__).resolve().parent / "web"
GATEWAY = "http://127.0.0.1:8081"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mode_server  # noqa: E402

declared = set()
for route in mode_server.app.routes:
    path = getattr(route, "path", None)
    if path and path.startswith("/local/"):
        declared.add(path)

called = set()
for name in ("mode-control.js", "image-panel.js", "video-panel.js"):
    text = (WEB / name).read_text(encoding="utf-8")
    for match in re.finditer(r"""["'`](/local/[A-Za-z0-9_\-/]+)""", text):
        called.add(match.group(1).rstrip("/"))

print(f"routes declared: {len(declared)}")
print(f"paths called by the panels: {len(called)}\n")

missing = sorted(path for path in called
                 if path not in declared and not any(
                     path.startswith(d.split("{")[0]) and "{" in d for d in declared))
if missing:
    print("CALLED BUT NOT DECLARED:")
    for path in missing:
        print("   ", path)
else:
    print("every path a panel calls is declared")

print("\nlive probe (GET only; POST routes are reported as declared, not called):")
problems = []
for path in sorted(called):
    route = next((r for r in mode_server.app.routes
                  if getattr(r, "path", None) == path), None)
    methods = sorted(getattr(route, "methods", []) or []) if route else []
    if route is None:
        continue
    if "GET" in methods:
        try:
            response = requests.get(f"{GATEWAY}{path}", timeout=20)
            ok = response.status_code < 500
            print(f"   {path:<38} GET -> {response.status_code} {'ok' if ok else 'FAILED'}")
            if not ok:
                problems.append(path)
        except requests.RequestException as exc:
            print(f"   {path:<38} GET -> {exc}")
            problems.append(path)
    else:
        print(f"   {path:<38} methods={methods}")

print("\nRESULT:", "OK" if not missing and not problems else "PROBLEMS")
raise SystemExit(1 if missing or problems else 0)
