"""Live check of the new kick controls: kick all, and delete on kick.

Confirms against the running service that kicking removes the record (the device
cannot come back with its old cookie) and that one call clears every device while the
owner's own session survives. The passphrase is restored afterwards.
"""

import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8081"
LINK = (ROOT / "logs" / "admin-link.txt").read_text(encoding="ascii").strip()
PIN = "611761-992509-889670"
ok, bad = [], []


def check(label, condition, detail=""):
    print(f"{'ok  ' if condition else 'FAIL'} {label}{'' if condition else '  <- ' + str(detail)}")
    (ok if condition else bad).append(label)


def status(client):
    return client.get(f"{BASE}/local/share/status").json()


with httpx.Client(timeout=30, follow_redirects=True) as owner:
    owner.get(LINK)

    # Two fresh devices join, so the check does not depend on whatever is in the store.
    tokens = []
    for index in range(2):
        with httpx.Client(timeout=20) as device:
            device.post(f"{BASE}/__login", data={"token": PIN})
            tokens.append(device.cookies.get("qwen_access"))
    check("two devices joined", all(tokens), tokens)

    before = status(owner)
    devices_before = [item for item in before["sessions"] if item["kind"] != "admin"]
    check("panel lists the joined devices", len(devices_before) >= 2, len(devices_before))

    # Kick one of them and confirm the record is gone and its cookie is dead.
    target = next(item for item in devices_before if item["last_seen"] >= 0)
    handle = target["token"]
    kicked = owner.post(f"{BASE}/local/share/sessions/{handle}/revoke")
    check("single kick accepted", kicked.status_code == 200, kicked.text[:80])
    after = status(owner)
    handles_after = [item["token"] for item in after["sessions"]]
    check("the kicked record was deleted, not flagged", handle not in handles_after, handle)
    check("the count went down by one", len(after["sessions"]) == len(before["sessions"]) - 1,
          f'{len(before["sessions"])} -> {len(after["sessions"])}')

    # One click clears everything else that is not the owner.
    remaining = [item for item in after["sessions"] if item["kind"] != "admin"]
    all_kick = owner.post(f"{BASE}/local/share/sessions/revoke-all")
    check("kick-all accepted", all_kick.status_code == 200, all_kick.text[:80])
    check("kick-all reported the removed count",
          all_kick.json().get("kicked") == len(remaining),
          f'{all_kick.json().get("kicked")} vs {len(remaining)}')
    final = status(owner)
    check("no device records remain", [item["kind"] for item in final["sessions"]] == ["admin"],
          final["sessions"])
    check("the owner session survived", final["sessions"][0]["current"] is True)
    check("the passphrase is still shown", final["pin"] == PIN, final["pin"])

    # A kicked cookie no longer works: the record is gone, so it cannot resolve.
    with httpx.Client(timeout=20, cookies={"qwen_access": tokens[0]}) as stale:
        response = stale.get(f"{BASE}/local/status")
        check("a kicked cookie is refused", response.status_code == 401, response.status_code)

    unused = owner.post(f"{BASE}/local/share/sessions/forget-unused")
    check("tidy-up endpoint answers", unused.status_code == 200, unused.text[:80])
    check("tidy-up removed nothing (all records are active)",
          unused.json().get("removed") == 0, unused.json())

print()
print("RESULT:", "PASS" if not bad else f"{len(bad)} FAILED -> {bad}")
sys.exit(1 if bad else 0)
