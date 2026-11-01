"""Live bind check: a real socket, real Host headers, real shared passphrase.

Complements test_access_gate.py, which drives the app through ASGI transport. This
one proves the uvicorn host/port resolution and the gate together, over TCP.
"""

import asyncio
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "bind-check-pin"
PROBE_PORT = 8085
overlay = ROOT / "logs" / "bind-check-share.json"
overlay.write_text(json.dumps({
    "listen_host": "127.0.0.1",
    "listen_port": PROBE_PORT,
    "access_token": TOKEN,
    "allowed_hosts": ["127.0.0.1", "localhost", "127.1.1.1"],
}), encoding="utf-8")

os.environ["QWEN_SHARE_CONFIG"] = str(overlay)
# A probe must not touch the live usage database or start a model backend.
os.environ["QWEN_BIND_CHECK"] = "1"
sys.path.insert(0, str(ROOT))

import tempfile  # noqa: E402

from app import mode_server as module  # noqa: E402  (import after the overlay is in place)
import uvicorn  # noqa: E402

import httpx  # noqa: E402
from app.share.share_store import ShareStore  # noqa: E402

module.LOGGER.setLevel("CRITICAL")
# The passphrase in force lives in the session store, and a probe process starts
# with an empty one, so the overlay value is seeded exactly like a real start. An
# isolated store is used deliberately: seeding the machine's own store would write
# this probe's passphrase into it and drop the owner's live sessions.
probe_store = tempfile.TemporaryDirectory()
module.share_store = ShareStore(Path(probe_store.name), load_state=False)
module.share_store.admin_token = "bind-check-admin-token"
module.share_store.set_pin(TOKEN)
failures = []


def check(label, condition, detail=""):
    print(f"{'ok  ' if condition else 'FAIL'} {label}{'' if condition else '  <- ' + str(detail)}")
    if not condition:
        failures.append(label)


def reachable(host, port):
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


async def main():
    resolved_host = module.listening_host()
    resolved_port = module.listening_port()
    check("overlay drives the bind address", (resolved_host, resolved_port) == ("127.0.0.1", PROBE_PORT),
          (resolved_host, resolved_port))
    check("overlay drives the passphrase", module.access_token() == TOKEN, module.access_token())
    check("allowlist follows the overlay", "127.1.1.1" in module.allowed_host_names(),
          module.allowed_host_names())

    server = uvicorn.Server(uvicorn.Config(module.app, host=resolved_host, port=resolved_port,
                                          log_level="critical"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    check("server reports started", server.started, "timed out waiting for uvicorn")
    if not server.started:
        return
    check("port is reachable over TCP", reachable("127.0.0.1", PROBE_PORT))

    async with httpx.AsyncClient(timeout=10) as client:
        # 1) A listed name without a passphrase gets the login page, not content.
        response = await client.get(f"http://127.0.0.1:{PROBE_PORT}/local/status")
        check("unauthenticated request is 401", response.status_code == 401, response.status_code)
        check("unauthenticated body is the login page", "需要访问口令" in response.text)
        check("login page carries no passphrase", TOKEN not in response.text)

        # 2) A name that was never shared is refused even with the passphrase.
        response = await client.get(f"http://127.0.0.1:{PROBE_PORT}/local/status",
                                    headers={"host": "not-shared.example.com"})
        check("unshared Host is 400", response.status_code == 400, response.status_code)

        # 3) The passphrase itself is not a credential: it only buys a session at
        #    login. Putting it in the URL must not admit anyone.
        response = await client.get(f"http://127.0.0.1:{PROBE_PORT}/local/status?k={TOKEN}")
        check("the passphrase in a URL does not admit anyone", response.status_code == 401,
              response.status_code)

        # 4) Exchanging the passphrase yields this device's own session token.
        response = await client.post(f"http://127.0.0.1:{PROBE_PORT}/__login", data={"token": TOKEN})
        check("login redirects", response.status_code == 303, response.status_code)
        cookie = client.cookies.get(module.ACCESS_COOKIE)
        check("login issues a session token, not the passphrase",
              bool(cookie) and cookie != TOKEN, cookie)
        check("the session is recorded for the manager panel",
              module.share_store.session_count() == 1, module.share_store.session_count())
        response = await client.get(f"http://127.0.0.1:{PROBE_PORT}/local/status")
        check("cookie alone is accepted", response.status_code == 200, response.status_code)
        check("route returns live status", "state" in response.text, response.text[:120])

        # 5) A cross-site form post is still refused.
        response = await client.post(f"http://127.0.0.1:{PROBE_PORT}/local/mode",
                                     json={"mode": "text"},
                                     headers={"origin": "http://evil.example.com"})
        check("foreign origin is 403", response.status_code == 403, response.status_code)

        # 6) The same-origin form post is not blocked by the origin guard.
        response = await client.post(f"http://127.0.0.1:{PROBE_PORT}/local/mode",
                                     json={"mode": "text"},
                                     headers={"origin": f"http://127.0.0.1:{PROBE_PORT}"})
        check("same-origin post passes the origin guard", response.status_code != 403, response.status_code)

    server.should_exit = True
    thread.join(timeout=10)


try:
    asyncio.run(main())
finally:
    overlay.unlink(missing_ok=True)

print()
print("RESULT:", "PASS" if not failures else f"{len(failures)} FAILED -> {failures}")
sys.exit(1 if failures else 0)
