"""Live acceptance check for the sharing manager against the running service.

This is the end-to-end counterpart to test_access_gate.py: the unit suite drives the
app in-process, while this one talks to the real gateway over HTTP with the real
session store, the real admin token and the real tunnel task.

Run it while the service is up:  python check_share_live.py
"""

from pathlib import Path
import sys

import httpx

BASE = "http://127.0.0.1:8081"
LINK_FILE = Path(r"D:\AI\Qwen-Local\logs\admin-link.txt")
PIN_FILE = Path(r"D:\AI\Qwen-Local\share.config.json")
SESSION_FILE = Path(r"D:\AI\Qwen-Local\logs\share.sessions.json")
ok = []
bad = []


def check(label, condition, detail=""):
    print(f"{'ok  ' if condition else 'FAIL'} {label}{'' if condition else '  <- ' + str(detail)}")
    (ok if condition else bad).append(label)


def configured_pin():
    """The passphrase the server is actually enforcing.

    The session store outranks the configuration file: the passphrase is seeded from
    ``share.config.json`` only when the store is empty, so an owner who rotated it in
    the panel would make a check that read the file alone report a false failure.
    """
    import json
    try:
        stored = json.loads(SESSION_FILE.read_text(encoding="utf-8")).get("pin")
    except (OSError, ValueError):
        stored = None
    if isinstance(stored, str) and stored.strip():
        return stored.strip()
    return json.loads(PIN_FILE.read_text(encoding="utf-8"))["access_token"]


def main():
    if not LINK_FILE.is_file():
        print("admin-link.txt 不存在：请先启动服务（Start-Qwen-Local.ps1）")
        return 1
    link = LINK_FILE.read_text(encoding="ascii").strip()
    pin = configured_pin()

    # 1) One click on the owner link: both cookies, no passphrase prompt.
    with httpx.Client(timeout=30, follow_redirects=True) as owner:
        page = owner.get(link)
        check("owner link opens the console", page.status_code == 200, page.status_code)
        check("owner link issued a session", "qwen_access" in owner.cookies)
        check("owner link issued admin standing", "qwen_admin" in owner.cookies)
        check("index page is the real UI", "mode-control.js" in page.text, page.text[:60])
        check("sharing manager is injected", "share-manager.js" in page.text)

        for asset in ("share-manager.js", "share-manager.css"):
            response = owner.get(f"{BASE}/local/{asset}")
            kind = response.headers.get("content-type", "")
            check(f"{asset} is served to the owner",
                  response.status_code == 200 and "html" not in kind,
                  f"{response.status_code} {kind}")

        status = owner.get(f"{BASE}/local/share/status")
        check("manager status is readable", status.status_code == 200, status.status_code)
        payload = status.json()
        check("tunnel is reported online", payload["online"] is True, payload.get("online"))
        check("passphrase is shown to the owner", payload["pin"] == pin, payload.get("pin"))
        check("the public address is served to the panel",
              isinstance(payload.get("url"), str) and payload["url"].startswith("https://"),
              payload.get("url"))
        check("owner session is marked current",
              any(entry["current"] for entry in payload["sessions"]), payload["sessions"])

        detail = owner.get(f"{BASE}/local/status").json()
        check("owner status keeps machine detail", "context_size" in detail, list(detail)[:6])

        # 2) Rotation with a genuinely different passphrase: new devices get in,
        #    the previous one stops working, and this session is untouched.
        rotated = "999888-777666-555444"
        change = owner.post(f"{BASE}/local/share/pin", json={"pin": rotated})
        check("owner can rotate the passphrase", change.status_code == 200, change.text[:80])
        check("rotated passphrase is in force", change.json()["pin"] == rotated,
              change.json().get("pin"))
        after = owner.get(f"{BASE}/local/share/status")
        check("owner session survives rotation", after.status_code == 200, after.status_code)
        with httpx.Client(timeout=20) as newcomer:
            admitted = newcomer.post(f"{BASE}/__login", data={"token": rotated})
            check("a new device joins with the rotated passphrase", admitted.status_code == 303,
                  admitted.status_code)
        with httpx.Client(timeout=20) as stale:
            refused = stale.post(f"{BASE}/__login", data={"token": pin})
            check("the previous passphrase no longer admits anyone", refused.status_code == 401,
                  refused.status_code)
        restore = owner.post(f"{BASE}/local/share/pin", json={"pin": pin})
        check("passphrase restored for the shared link", restore.status_code == 200,
              restore.text[:80])

        with httpx.Client(timeout=20) as stranger:
            wrong = stranger.post(f"{BASE}/__login", data={"token": "000000-000000-000000"})
            check("a wrong passphrase is refused", wrong.status_code == 401, wrong.status_code)

        handle = next(entry["token"] for entry in after.json()["sessions"] if entry["current"])
        self_kick = owner.post(f"{BASE}/local/share/sessions/{handle}/revoke")
        check("owner cannot kick their own session", self_kick.status_code == 409,
              self_kick.status_code)

    # 3) The watchdog path: log in like the supervisor does, then probe.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app import qwen_service

    was_ready = qwen_service.SESSION_READY.is_set()
    qwen_service.SESSION_READY.clear()
    try:
        bootstrapped = qwen_service.ensure_session()
        check("watchdog can log itself in", bootstrapped, bootstrapped)
        try:
            status = qwen_service.http_json(qwen_service.gateway_url("/local/status"), timeout=10)
            check("watchdog probe is answered, not redirected",
                  status.get("service") == "qwen-local-modes", status)
        except Exception as exc:  # noqa: BLE001 - the check reports whatever happened
            check("watchdog probe is answered, not redirected", False,
                  f"{type(exc).__name__}: {exc}")
    finally:
        if was_ready:
            qwen_service.SESSION_READY.set()

    print()
    print("RESULT:", "PASS" if not bad else f"{len(bad)} FAILED -> {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
