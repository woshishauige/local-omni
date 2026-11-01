"""Final cold-start acceptance of the desktop entry, run the way a double-click runs it.

The shortcut executes wscript.exe with the launcher, so that is exactly what this
does, from a fully stopped state. Nothing else touches the service while it runs.
"""

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx

import mode_server as module

ROOT = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:8081"
VBS = ROOT / "Start-Qwen-Panel.vbs"
ok, bad = [], []


def check(label, condition, detail=""):
    print(f"{'ok  ' if condition else 'FAIL'} {label}{'' if condition else '  <- ' + str(detail)}")
    (ok if condition else bad).append(label)


def ps(command):
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True, text=True, errors="replace",
    )
    return completed.stdout.strip()


def titles():
    return [line.strip() for line in ps(
        "Get-Process msedge -ErrorAction SilentlyContinue | "
        "Where-Object { $_.MainWindowHandle -ne 0 } | "
        "Select-Object -ExpandProperty MainWindowTitle"
    ).splitlines() if line.strip()]


def consoles():
    return ps(
        "Get-Process powershell,pwsh,cmd,WindowsTerminal,conhost -ErrorAction SilentlyContinue | "
        "Where-Object { $_.MainWindowHandle -ne 0 } | Measure-Object | "
        "Select-Object -ExpandProperty Count"
    )


def listening():
    return ps(
        "(Get-NetTCPConnection -LocalPort 8081 -State Listen -ErrorAction SilentlyContinue | "
        "Measure-Object).Count"
    ) == "1"


def reachable():
    try:
        httpx.get(f"{BASE}/local/status", timeout=3)
        return True
    except Exception:
        return False


# --- 1. stop everything, exactly as the panel's own stop button would -------------
if reachable():
    link = (ROOT / "logs" / "admin-link.txt").read_text(encoding="ascii").strip()
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        client.get(link)
        client.post(f"{BASE}/local/share/stop", json={})
stopped = False
for _ in range(90):
    if not listening() and not reachable() and module.tunnel_process() is None:
        stopped = True
        break
    time.sleep(1)
check("service and tunnel are stopped before the test", stopped,
      f"listening={listening()} tunnel={module.tunnel_process()}")
before_consoles = consoles()

# --- 2. run the entry the way the shortcut does -----------------------------------
print("running the desktop entry ...")
subprocess.Popen(["wscript.exe", str(VBS)], cwd=str(ROOT))

started = time.monotonic()
panel_seen = False
while time.monotonic() - started < 240:
    if listening() and any("Qwen 共享面板" in title for title in titles()):
        panel_seen = True
        break
    time.sleep(2)

# --- 3. the tunnel registers a few seconds later ----------------------------------
payload = {}
for _ in range(60):
    if not reachable() or module.tunnel_process() is None:
        time.sleep(2)
        continue
    link = (ROOT / "logs" / "admin-link.txt").read_text(encoding="ascii").strip()
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        client.get(link)
        payload = client.get(f"{BASE}/local/share/status").json()
    if payload.get("online") and payload.get("url"):
        break
    time.sleep(2)
elapsed = time.monotonic() - started

print()
check("entry started the service", listening())
check("entry started the tunnel", module.tunnel_process() is not None)
check("panel opened as its own window", panel_seen, titles())
check("no console window appeared", consoles() == before_consoles,
      f"{before_consoles} -> {consoles()}")
check("panel shows the passphrase", bool(payload.get("pin")), payload.get("pin"))
check("panel shows the public address",
      str(payload.get("url", "")).startswith("https://"), payload.get("url"))
check("panel reports the tunnel online", payload.get("online") is True, payload.get("online"))
print(f"      (elapsed {elapsed:.0f}s) pin: {payload.get('pin')} url: {payload.get('url')}")

print()
print("RESULT:", "PASS" if not bad else f"{len(bad)} FAILED -> {bad}")
sys.exit(1 if bad else 0)
