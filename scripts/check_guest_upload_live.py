"""Live acceptance for the guest clip upload (A): switch, upload, record, download.

Runs against the service on 127.0.0.1:8081 as the owner (the admin link supplies the
owner cookie). A true tunnel visitor cannot be simulated from this machine — a loopback
peer is the owner by design — so the guest-versus-owner split is covered by
``test_guest_uploads.py``; this file checks that the live wiring works end to end.

The uploaded clip is tiny and is deleted at the end, including its ownership record.
"""
import json
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
from pathlib import Path

BASE = "http://127.0.0.1:8081"
ROOT = Path(__file__).resolve().parents[1]
INPUT = Path(r"D:\AI\ComfyUI-BSAI_v010\ComfyUI\input")
RECORDS = ROOT / "logs" / "guest-uploads.json"

CLIP = b"\x00\x00\x00\x18ftypmp42" + b"live-acceptance" * 8
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print("%-4s %-58s %s" % ("PASS" if ok else "FAIL", name, detail))


def owner_opener():
    link = (ROOT / "logs" / "admin-link.txt").read_text(encoding="utf-8").strip()
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.open(link, timeout=25).read()
    return op


def call(op, method, path, body=None, headers=None, raw=None, timeout=30):
    hdrs = dict(headers or {})
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    request = urllib.request.Request(BASE + path, data=data, headers=hdrs, method=method)
    try:
        with op.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def policy_body():
    return json.loads(RECORDS.read_text(encoding="utf-8")) if RECORDS.is_file() else None


op = owner_opener()
uploaded = None
try:
    status, payload = call(op, "GET", "/local/share/uploads")
    body = json.loads(payload)
    check("1 owner reads the switch", status == 200 and body["policy"] == "off",
          "policy=%s" % body.get("policy"))

    status, payload = call(op, "POST", "/local/share/uploads", {"policy": "guests"})
    body = json.loads(payload)
    check("2 owner opens guest uploads", status == 200 and body["policy"] == "guests",
          "policy=%s" % body.get("policy"))
    check("3 switch persisted to disk", (policy_body() or {}).get("policy") == "guests",
          str(RECORDS))

    status, payload = call(op, "POST", "/local/video/upload?name=live-check.mp4",
                           raw=CLIP, headers={"Content-Type": "application/octet-stream"})
    info = json.loads(payload)
    uploaded = info.get("name")
    check("4 upload accepted", status == 200 and uploaded and uploaded.startswith("qwen-video-"),
          "%s %s byte(s)" % (uploaded, info.get("bytes")))
    check("5 clip landed in ComfyUI input", bool(uploaded) and (INPUT / uploaded).is_file(),
          str(INPUT / str(uploaded)))

    records = (policy_body() or {}).get("files", {})
    entry = records.get(uploaded or "", {})
    check("6 ownership recorded, content-free",
          set(entry) == {"session", "bytes", "uploaded_at"} and entry.get("bytes") == len(CLIP),
          json.dumps(entry, ensure_ascii=False))

    status, payload = call(op, "GET", "/local/video/source")
    listing = json.loads(payload)
    check("7 owner lists the new clip", status == 200 and uploaded in listing.get("videos", []),
          "%d clip(s) visible" % len(listing.get("videos", [])))

    status, payload = call(op, "POST", "/local/video/source", {"path": uploaded})
    check("8 owner can select it", status == 200 and json.loads(payload).get("selected") == uploaded)

    clip_path = (INPUT / str(uploaded)).as_posix()
    status, payload = call(op, "GET", "/local/video/file?path=" + urllib.parse.quote(clip_path))
    check("9 clip downloads back byte-for-byte", status == 200 and payload == CLIP,
          "HTTP %s, %d byte(s)" % (status, len(payload)))

    status, payload = call(op, "POST", "/local/share/uploads", {"policy": "off"})
    check("10 owner closes the switch again", status == 200 and json.loads(payload)["policy"] == "off")

    status, payload = call(op, "POST", "/local/share/uploads", {"policy": "everyone"})
    check("11 unknown policy refused", status == 400, "HTTP %s" % status)
finally:
    # Leave nothing behind: the clip, its record, and the switch state.
    if uploaded:
        records = (policy_body() or {}).get("files", {})
        records.pop(uploaded, None)
        state = policy_body() or {}
        state["files"] = records
        state["policy"] = "off"
        RECORDS.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        target = INPUT / uploaded
        if target.is_file():
            target.unlink()
    try:
        call(op, "POST", "/local/video/source", {"path": ""})
    except Exception:  # noqa: BLE001
        pass

print("\n%d/%d passed" % (sum(results), len(results)))
raise SystemExit(0 if all(results) else 1)
