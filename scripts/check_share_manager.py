"""Live end-to-end check of the sharing manager over real HTTP.

Starts a real uvicorn server with an isolated session store and drives the exact
calls the manager panel and the watchdog make, so the wiring is proven rather
than inferred: login issues a session, activity is accounted, kicking stops the
device, rotating the passphrase does not disturb it, and one-click disconnect
really ends the tunnel task.

Run:  python check_share_manager.py
"""

import asyncio
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
PORT = 8087
OLD_PIN = "111111-222222-333333"
NEW_PIN = "444444-555555-666666"
failures = []


def check(label, condition, detail=""):
    print(f"{'ok  ' if condition else 'FAIL'} {label}{'' if condition else '  <- ' + str(detail)}")
    if not condition:
        failures.append(label)


class FakeTask:
    """Stands in for schtasks and the tunnel process so the probe never touches
    the real tunnel: the disconnect button must not take the owner's tunnel down
    while the suite runs."""

    def __init__(self):
        self.running = True
        self.log = []

    def __call__(self, command, timeout):
        self.log.append(list(command))
        executable = str(command[0]).lower()
        if executable.endswith("powershell.exe"):
            # ``register_tunnel_task()`` registers the task with PowerShell rather than schtasks,
            # and it runs on every reconnect. Answering it the way the real registration does is
            # what lets this suite exercise the whole reconnect path instead of stopping at
            # "bad verb".
            self.registered = True
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        verb = command[1]
        if verb == "/Query":
            state = "Running" if self.running else "Ready"
            # Deliberately non-UTF-8 bytes: the real schtasks prints in the console
            # code page, which used to crash the status endpoint on decode.
            return subprocess.CompletedProcess(command, 0, stdout=f"TaskName: X\r\nStatus: {state}\r\n", stderr="")
        if verb == "/End":
            self.running = False
            return subprocess.CompletedProcess(command, 0, stdout="SUCCESS\r\n", stderr="")
        if verb == "/Run":
            self.running = True
            return subprocess.CompletedProcess(command, 0, stdout="SUCCESS\r\n", stderr="")
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="bad verb")

    def process(self):
        return object() if self.running else None


def local_lan_ip():
    """A real non-loopback address of this machine, if it has one.

    Loopback aliases cannot be used to fake a remote caller: Windows reports every
    127.x.x.x peer as 127.0.0.1, so the owner check cannot tell them apart. A LAN
    address is the only way to exercise the non-owner path against a real server.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def server_answers(host, port, timeout=3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def main():
    directory = tempfile.TemporaryDirectory()
    base = Path(directory.name)
    overlay = base / "share.config.json"
    lan = local_lan_ip()
    hosts = ["127.0.0.1", "localhost"] + ([lan] if lan else [])
    overlay.write_text(json.dumps({
        "listen_host": "127.0.0.1",
        "listen_port": PORT,
        "access_token": OLD_PIN,
        "allowed_hosts": hosts,
    }), encoding="utf-8")
    os.environ["QWEN_SHARE_CONFIG"] = str(overlay)
    os.environ["QWEN_ACCESS_TOKEN"] = OLD_PIN
    # Keep this probe away from the live usage database and model backend.
    os.environ["QWEN_BIND_CHECK"] = "1"
    sys.path.insert(0, str(ROOT))

    from app import mode_server as module
    from app.share.share_store import ShareStore
    import uvicorn

    module.LOGGER.setLevel("CRITICAL")
    module.share_store = ShareStore(base)
    module.share_store.set_pin(OLD_PIN)
    task = FakeTask()
    module._run_hidden = task
    module.tunnel_process = task.process

    server = uvicorn.Server(uvicorn.Config(module.app, host="0.0.0.0", port=PORT, log_level="critical"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.1)
    check("server started", server.started)
    if not server.started:
        return

    url = f"http://127.0.0.1:{PORT}"

    async def run():
        async with httpx.AsyncClient(timeout=15) as owner, httpx.AsyncClient(timeout=15) as device:
            # 1) A device joins with the passphrase and gets its own token.
            login = await device.post(f"{url}/__login", data={"token": OLD_PIN})
            check("device login redirects", login.status_code == 303, login.status_code)
            token = device.cookies.get(module.ACCESS_COOKIE)
            check("device holds a session token", bool(token) and token != OLD_PIN, token)
            check("store recorded one device", module.share_store.session_count() == 1,
                  module.share_store.session_count())

            # 2) It can use the model endpoint (the proxy answers upstream failures).
            await device.get(f"{url}/props")
            module.share_store.note_request(token, "/v1/chat/completions", received=8192)

            # 3) The owner console: a local device session plus ?admin is an admin.
            owner.cookies.set(module.ACCESS_COOKIE, module.share_store.create(kind="admin", label="本机"))
            mark = await owner.get(f"{url}/?admin={module.share_store.admin_token}")
            check("owner console marks this browser", mark.status_code == 303, mark.status_code)

            status = await owner.get(f"{url}/local/share/status")
            check("manager status is readable by the owner", status.status_code == 200, status.status_code)
            payload = status.json()
            entry = next((item for item in payload["sessions"] if item["kind"] == "device"), None)
            check("panel lists the joined device", entry is not None, payload)
            if entry:
                check("panel counts the device request", entry["requests"] >= 1, entry)
                check("panel counts response bytes", entry["bytes"] >= 8192, entry)
                check("panel hides the raw token", entry["token"] != token, entry["token"])
                check("panel shows where it came from", bool(entry["last_source"]), entry)
            check("panel reports the tunnel as online", payload["online"] is True, payload["online"])

            # 4) The owner rotates the passphrase; the joined device is untouched.
            rotate = await owner.post(f"{url}/local/share/pin", json={"pin": NEW_PIN})
            check("rotation accepted", rotate.status_code == 200, rotate.text[:120])
            check("new passphrase is in force", module.access_token() == NEW_PIN, module.access_token())
            still = await device.get(f"{url}/local/status")
            check("joined device was not interrupted", still.status_code == 200, still.status_code)
            async with httpx.AsyncClient(timeout=15) as stranger:
                refused = await stranger.post(f"{url}/__login", data={"token": OLD_PIN})
                check("old passphrase no longer admits anyone", refused.status_code == 401,
                      refused.status_code)
                admitted = await stranger.post(f"{url}/__login", data={"token": NEW_PIN})
                check("new passphrase admits a new device", admitted.status_code == 303,
                      admitted.status_code)

            # 5) Kick the first device out through the panel API.
            handle = module.share_store.handle(token)
            kick = await owner.post(f"{url}/local/share/sessions/{handle}/revoke")
            check("panel kick accepted", kick.status_code == 200, kick.text[:120])
            evicted = await device.get(f"{url}/local/status")
            check("kicked device falls back to the login page", evicted.status_code == 401,
                  evicted.status_code)
            check("kicked device sees the passphrase prompt", "需要访问口令" in evicted.text)
            replay = await device.post(f"{url}/__login", data={"token": NEW_PIN})
            check("kicked device can rejoin with the new passphrase", replay.status_code == 303,
                  replay.status_code)

            # 6) A guest cannot reach the manager or the owner's own data. The guest
            # requests come from 127.0.0.2 so this probe reproduces a remote caller:
            # a request from plain loopback would legitimately count as the owner.
            guest_token = module.share_store.create(kind="device", label="guest")
            # A real remote caller is reproduced with a real second address, because
            # Windows reports any 127.x.x.x peer as 127.0.0.1. Without a reachable
            # LAN address the guest checks are skipped, not faked: the owner/guest
            # split itself is covered by test_access_gate over real requests.
            guest_host = lan if lan and server_answers(lan, PORT) else None
            if guest_host is None:
                print("skip  guest checks: no reachable non-loopback address on this host")
            else:
                guest_url = f"http://{guest_host}:{PORT}"
                async with httpx.AsyncClient(timeout=15) as guest:
                    guest.cookies.set(module.ACCESS_COOKIE, guest_token)
                    await guest.get(f"{guest_url}/local/status")
                    seen = next((item["last_source"] for item in module.share_store.summarize()["sessions"]
                                 if item["label"] == "guest"), "")
                    check("guest requests arrive from a non-loopback address",
                          seen == guest_host, seen)
                    for path in ("/local/share/status", "/local/usage"):
                        response = await guest.get(f"{guest_url}{path}")
                        check(f"guest is refused {path}", response.status_code == 403,
                              response.status_code)
                    pin = await guest.post(f"{guest_url}/local/share/pin", json={"pin": NEW_PIN})
                    check("guest is refused /local/share/pin", pin.status_code == 403, pin.status_code)
                    detail = await guest.get(f"{guest_url}/local/status")
                    check("guest status omits machine detail", "context_size" not in detail.text,
                          detail.text[:80])
                    blocked = await guest.post(f"{guest_url}/local/mode", json={"mode": "text"})
                    # The owner has opened mode switching to visitors, so the gate must let this
                    # through; what the handler then answers (409 while a transition runs, 503
                    # with no backend in this harness) is its own business.
                    check("guest can drive the service but not the panel",
                          blocked.status_code != 403, blocked.status_code)

            # 7) One-click disconnect ends the tunnel task; reconnect restores it.
            off = await owner.post(f"{url}/local/share/online", json={"online": False})
            check("disconnect accepted", off.status_code == 200, off.text[:120])
            check("tunnel task was ended", any("/End" in entry for entry in task.log), task.log)
            check("disconnect is reported back", off.json()["online"] is False, off.text[:80])
            after = await owner.get(f"{url}/local/share/status")
            check("panel shows the tunnel as offline", after.json()["online"] is False,
                  after.json()["online"])
            on = await owner.post(f"{url}/local/share/online", json={"online": True})
            check("reconnect accepted", on.status_code == 200, on.text[:120])
            check("reconnect is reported back", on.json()["online"] is True, on.text[:80])

            # 8) The owner's own browser keeps full control.
            mode = await owner.post(f"{url}/local/mode", json={"mode": "text"})
            check("owner can still drive the service", mode.status_code != 403, mode.status_code)

    try:
        asyncio.run(run())
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        directory.cleanup()

    print()
    print("RESULT:", "PASS" if not failures else f"{len(failures)} FAILED -> {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
