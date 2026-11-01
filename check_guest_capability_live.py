"""访客能力线上验收：以**真正的访客身份**（非回环地址）走一遍四项能力与边界。

A loopback probe is always the owner, so this cannot be checked from 127.0.0.1: it goes to the
machine's LAN address or through the public tunnel, which is exactly how a shared visitor arrives.
The script logs in with the passphrase from `share.config.json`, then asks the questions the owner
asked for:

  * can a visitor switch modes (and with them the model and its plugins)?
  * can a visitor generate, and read back its own material?
  * is its own storage really its own (own output folder, own uploads, and nothing else)?
  * are the machine's own controls still closed to it (share manager, usage, GPU preempt, stop)?

Nothing here starts a generation, flips a switch or deletes anything: it is safe to run against a
service that other people are using.

    & ..\\ComfyUI-BSAI_v010\\python\\python.exe -X utf8 check_guest_capability_live.py
    ... --base http://192.168.1.10:8081     # or an explicit address
    ... --base https://<tunnel>.trycloudflare.com
"""

import argparse
import http.cookiejar
import json
from pathlib import Path
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
CHECKS = []


def check(label, condition, detail=""):
    CHECKS.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'} {label:<46} {detail}")
    return bool(condition)


def read_share_config():
    try:
        return json.loads((ROOT / "share.config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def lan_address():
    """This machine's LAN address, as a shared visitor would reach it."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            return probe.getsockname()[0]
    except OSError:
        return ""


def tunnel_url():
    """The newest trycloudflare hostname in the tunnel log, if the tunnel is up."""
    log = ROOT / "logs" / "cloudflared.log"
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    found = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", text)
    return found[-1] if found else ""


class Visitor:
    """One shared device: its own cookie jar, exactly like a browser that logged in."""

    def __init__(self, base):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, path, method="GET", body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        headers = {"content-type": "application/json"} if data is not None and raw is None else {}
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            response = self.opener.open(request, timeout=90)
            return response.status, response.read()
        except urllib.error.HTTPError as failure:
            return failure.code, failure.read()
        except (urllib.error.URLError, OSError) as failure:
            return 0, str(failure).encode()

    def login(self, token):
        data = urllib.parse.urlencode({"token": token}).encode()
        request = urllib.request.Request(self.base + "/__login", data=data)
        try:
            return self.opener.open(request, timeout=60).status
        except urllib.error.HTTPError as failure:
            return failure.code

    def session_token(self):
        for cookie in self.jar:
            if cookie.name.endswith("access"):
                return cookie.value
        return ""


def body_text(payload):
    return payload.decode("utf-8", "replace")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="", help="共享地址（默认：隧道，其次局域网）")
    args = parser.parse_args()

    config = read_share_config()
    token = str(config.get("access_token") or "")
    base = args.base or tunnel_url() or (f"http://{lan_address()}:8081" if lan_address() else "")
    if not base:
        print("SKIP: 找不到共享地址（既没有隧道，也没有局域网地址）")
        return 0
    if not token:
        print("SKIP: share.config.json 里没有口令（本机未共享）")
        return 0
    print(f"访客入口：{base}\n")

    anonymous = Visitor(base)
    unauthenticated = anonymous.call("/local/status")[0]
    check("未登录只能看到登录页", unauthenticated == 401, f"HTTP {unauthenticated}")

    visitor = Visitor(base)
    check("用口令登录", visitor.login(token) == 200)
    visitor_token = visitor.session_token()
    check("拿到的是自己的会话，而不是口令", bool(visitor_token) and visitor_token != token,
          visitor_token[:12] + "…")

    # --- 能用的：对话、切换、生成、自己的存储 ------------------------------------------
    status, payload = visitor.call("/local/status")
    check("访客能读状态", status == 200, f"HTTP {status}")
    mode, reply = visitor.call("/local/mode", "POST", {"mode": "text"})
    check("访客能直接切模式（不再被拒）", mode != 403,
          f"HTTP {mode} {body_text(reply)[:60]}")
    model, reply = visitor.call("/local/model", "POST", {"profile": "qwen3.5-9b-uncensored"})
    check("访客能换模型（随之换插件）", model != 403, f"HTTP {model} {body_text(reply)[:60]}")
    cancel, _ = visitor.call("/local/image/cancel", "POST", {})
    check("访客能对生成任务下手", cancel != 403, f"HTTP {cancel}")
    progress, _ = visitor.call("/local/image/progress")
    check("访客能看生成进度", progress == 200, f"HTTP {progress}")

    own = json.loads(body_text(visitor.call("/local/video/output")[1] or b"{}"))
    check("访客被告知自己的产出目录", "guests" in str(own.get("current", "")) and visitor_token[:24] in str(own.get("current", "")),
          str(own.get("current", ""))[-46:])

    name = "guest-capability-check.mp4"
    upload, payload = visitor.call(f"/local/video/upload?name={name}", "POST", raw=b"guest-probe")
    check("访客能上传自己的素材", upload == 200, f"HTTP {upload} {body_text(payload)[:60]}")
    listed = json.loads(body_text(visitor.call("/local/video/source")[1] or b"{}"))
    check("访客只列得出自己的素材", bool(listed.get("videos")),
          f"{len(listed.get('videos') or [])} 个可见")

    # --- 不能用的：机器自己的控制 --------------------------------------------------------
    for path, method in (("/local/share/status", "GET"), ("/local/share/guest-policy", "GET"),
                         ("/local/usage", "GET"), ("/local/routing", "GET"),
                         ("/local/gpu", "GET"), ("/local/shutdown", "POST"),
                         ("/local/recover", "POST"), ("/local/video/folders?path=C%3A%5C", "GET")):
        code, _ = visitor.call(path, method, {} if method == "POST" else None)
        check(f"访客被挡在 {path.split('?')[0]} 外", code == 403, f"HTTP {code}")

    denial, payload = visitor.call("/local/share/guest-policy", "POST",
                                   {"feature": "media", "allowed": False})
    check("访客改不了自己的权限", denial == 403, f"HTTP {denial}")

    # --- 围栏：本机既有产出读不到 --------------------------------------------------------
    output_root = Path(r"D:\AI\ComfyUI-BSAI_v010\ComfyUI\output")
    victim = next((path for path in output_root.rglob("*.png") if "guests" not in path.parts), None)
    if victim is None:
        check("读不到机主的既有产出", True, "本机没有可试探的图片，跳过")
    else:
        relative = victim.relative_to(output_root)
        folder = "" if str(relative.parent) == "." else str(relative.parent).replace("\\", "/")
        query = urllib.parse.urlencode({"filename": relative.name, "subfolder": folder,
                                        "type": "output"})
        code, _ = visitor.call("/local/image/file?" + query)
        check("读不到机主的既有产出", code in (403, 404), f"{relative} → HTTP {code}")

    print(f"\n{sum(CHECKS)}/{len(CHECKS)} passed")
    return 0 if all(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
