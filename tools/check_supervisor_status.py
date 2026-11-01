"""Is the Qwen service supervised right now?

Written to replace an earlier version that probed `supervisor.lock` and reported BLOCKED, which I
read as "a stalled process is blocking every start". That reading was wrong twice over:

  * a *healthy* supervisor holds its lock too, so BLOCKED is the normal state while the service is
    up - the probe could not tell "working" from "blocked";
  * `supervisor.lock` has been stale since 2026-09-07 (a process that will not let the file be
    renamed). `qwen_service.py` already detects exactly that and says so:

        supervisor.lock is held by a stale process that will not let it be renamed;
        new starters contend on supervisor.run.lock instead

    so the file that actually gates a start is `supervisor.run.lock`, and the stale one is
    expected - not a fault to be repaired, and never a reason to kill a process.

This asks the question that matters instead: is a supervisor alive, does it own the run lock, and
which manager is it recording as running.

Usage: python tools/check_supervisor_status.py
"""

import json
import os
import subprocess
import sys

ROOT = r"D:\AI\Qwen-Local"
RUN_LOCK = os.path.join(ROOT, "supervisor.run.lock")
LEGACY_LOCK = os.path.join(ROOT, "supervisor.lock")


def lock_state(path):
    """'held' / 'free' / 'missing' - tried non-blocking, released immediately."""
    if not os.path.exists(path):
        return "missing"
    try:
        handle = open(path, "r+b")
    except OSError as exc:
        return f"unreadable ({exc.strerror or exc})"
    try:
        import msvcrt
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LCKNBLCK if hasattr(msvcrt, "LCKNBLCK") else msvcrt.LK_NBLCK, 1)
        except OSError:
            return "held"
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return "free"
    finally:
        handle.close()


def read_json(name):
    path = os.path.join(ROOT, name)
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def alive(pid):
    if not pid:
        return False
    try:
        result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                capture_output=True, text=True, timeout=10)
        return str(pid) in (result.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return False


def main():
    status = read_json("supervisor.status.json") or {}
    supervisor = read_json("supervisor.process.json") or {}
    manager = read_json("supervisor-manager.process.json") or {}

    run = lock_state(RUN_LOCK)
    legacy = lock_state(LEGACY_LOCK)

    print(f"supervisor.run.lock : {run}")
    print(f"supervisor.lock     : {legacy}   (stale since 2026-09-07; qwen_service.py is built to "
          "ignore it and says so in supervisor.log)")
    print()
    print(f"status file         : {status or '(absent)'}")
    print(f"supervisor process  : pid {supervisor.get('pid')}  "
          f"{'alive' if alive(supervisor.get('pid')) else 'NOT RUNNING'}")
    print(f"manager process     : pid {manager.get('pid')}  "
          f"{'alive' if alive(manager.get('pid')) else 'NOT RUNNING'}")

    supervised = run == "held" and status.get("state") == "running" and alive(status.get("manager_pid"))
    print()
    if supervised:
        print("VERDICT: the service is up and supervised - the supervisor owns the run lock and "
              "records a live manager.")
        print("         Do not start another gateway by hand: a second, unmanaged copy holding "
              "port 8081 is what makes the supervisor's own heartbeat fail.")
    else:
        print("VERDICT: the service is NOT supervised right now.")
        print("         Start it the documented way (the QwenService task or "
              "Start-Qwen.ps1 -NoBrowser) rather than launching mode_server.py directly.")
    return 0 if supervised else 1


if __name__ == "__main__":
    sys.exit(main())
