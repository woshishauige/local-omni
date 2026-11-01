"""Keep the Cloudflare tunnel alive inside one windowless process.

The scheduled task runs this file through pythonw.exe, so nothing has a console at
all. Two things matter and both were learned the hard way:

* The task has to stay busy for as long as the tunnel lives. An earlier version
  launched cloudflared from a script that exited immediately and the tunnel was
  reaped with the task before it finished registering.
* The tunnel must not be a child of anything that can be torn down with a console.
  A process started with ``subprocess.Popen`` inherits the console and job of its
  parent, so it dies when that console closes; ``exec`` replaces this process image
  instead, leaving exactly one process that holds the tunnel open.

Commands:
    run     become cloudflared and stay alive (what the scheduled task runs)
    stop    terminate the tunnel for this gateway
    online  print True/False for the manager's status probe
"""

import os
from pathlib import Path
import subprocess
import sys
import time

import psutil

from app.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT
EXECUTABLE = ROOT / "tools" / "cloudflared.exe"
LOG = ROOT / "logs" / "cloudflared.log"
PORT = int(os.environ.get("QWEN_TUNNEL_PORT", "8081"))
URL = f"http://127.0.0.1:{PORT}"
POLL_SECONDS = 5.0


def tunnel_process():
    """The cloudflared process serving this gateway, if it is running."""
    for process in psutil.process_iter(["name", "cmdline"]):
        try:
            if (process.info["name"] or "").lower() != "cloudflared.exe":
                continue
            command = " ".join(process.info["cmdline"] or [])
        except (psutil.Error, TypeError):
            continue
        if f"127.0.0.1:{PORT}" in command or "--url" in command:
            return process
    return None


def spawn():
    """Start cloudflared with no console at all, and with no parent to lose.

    DETACHED_PROCESS is the part that matters: Windows never allocates a console for
    cloudflared, so nothing can display one. The exec-based version did allocate a
    console, and Windows Terminal showed it as a "cloudflared.exe" window. The child
    is also left parentless on purpose, so nothing that started it can take it down
    when its own console or job closes.
    """
    LOG.parent.mkdir(parents=True, exist_ok=True)
    creation = 0
    if os.name == "nt":
        creation = (subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NEW_PROCESS_GROUP)
    handle = open(LOG, "ab", buffering=0)
    try:
        return subprocess.Popen(
            [str(EXECUTABLE), "tunnel", "--no-autoupdate",
             "--logfile", str(LOG), "--url", URL],
            cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=handle, stderr=handle,
            creationflags=creation, close_fds=True,
        )
    finally:
        handle.close()


def run():
    """Hold the task open for as long as the tunnel lives.

    The task has to stay in the running state, otherwise Task Scheduler considers the
    work finished and nothing is left to keep the tunnel alive. A tunnel that is
    already up is therefore adopted rather than ignored: the keeper still watches it,
    so the task stays busy and a tunnel that dies is restarted by the next task run.
    """
    if not EXECUTABLE.is_file():
        print(f"cloudflared not found: {EXECUTABLE}", file=sys.stderr)
        return 1
    process = tunnel_process()
    if process is None:
        process = psutil.Process(spawn().pid)
    while True:
        try:
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(POLL_SECONDS)
    return 0


def stop():
    process = tunnel_process()
    if process is None:
        return 0
    try:
        process.terminate()
        process.wait(timeout=10)
    except (psutil.Error, OSError):
        try:
            process.kill()
        except (psutil.Error, OSError):
            pass
    return 0


def online():
    print("True" if tunnel_process() is not None else "False")
    return 0


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    actions = {"run": run, "stop": stop, "online": online}
    if command not in actions:
        print(f"unknown command: {command}", file=sys.stderr)
        sys.exit(2)
    sys.exit(actions[command]())
