"""访客只能看到自己的历史：线上验收。

背景：这条界线曾经只写在注释里（"列表只有我们自己的面板会调"），实测被穿透——一台只有
共享口令的设备拿到了 owner 的会话 id 和标题，再把 id 递回来就整段读走了私人正文。
修法见 `mode_server.guest_namespace` / `tenant_conversation`：访客的每条历史都落在自己
会话的命名空间里，键形如 `text·guest-<会话>·<id>`。

这个脚本用真实地址（LAN + 共享口令，等同于别人家里的一台设备）验收四件事：

  1. owner 的历史不出现在访客的列表里；
  2. 访客拿着 owner 的 id 读，读不到（键会被嵌进访客自己的命名空间）；
  3. owner 的列表里也不出现访客的行（别人的东西不进我的历史栏）；
  4. 访客仍然能重命名、删除**自己的**行 —— 修的是越界，不是能力。

    python -X utf8 check_guest_ledger_fence.py
    python -X utf8 check_guest_ledger_fence.py --base https://<tunnel>.trycloudflare.com
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKS: list[bool] = []
SECRET = "OWNER-SECRET-7f3a"


def check(label, condition, detail=""):
    CHECKS.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'} {label:<44} {detail}")
    return bool(condition)


def call(opener, url, method="GET", body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with opener.open(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as failure:
        raw = failure.read().decode("utf-8", "replace")
        try:
            return failure.code, json.loads(raw or "{}")
        except ValueError:
            return failure.code, {"raw": raw[:200]}
    except (urllib.error.URLError, OSError) as failure:
        return 0, {"error": str(failure)}


def lan_address():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            return probe.getsockname()[0]
    except OSError:
        return ""


def owner_opener():
    link = (ROOT / "logs" / "admin-link.txt").read_text(encoding="ascii").strip()
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(urllib.request.Request(link), timeout=30) as response:
        check("owner 用管理员链接登录", response.status == 200)
    return opener


def visitor_opener(base):
    config = json.loads((ROOT / "share.config.json").read_text(encoding="utf-8"))
    token = str(config.get("access_token") or "")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    payload = urllib.parse.urlencode({"token": token}).encode()
    try:
        with opener.open(urllib.request.Request(base + "/__login", data=payload), timeout=30) as response:
            logged_in = response.status == 200
    except urllib.error.HTTPError as failure:
        print(f"访客登录失败：{failure.code}（口令不对或共享已关闭，后面的读数不算数）")
        return None, ""
    check("访客用共享口令登录", logged_in)
    session = next((cookie.value for cookie in jar if cookie.name.endswith("access")), "")
    check("访客拿到的是自己的会话，不是口令", bool(session) and session != token,
          f"{session[:12]}…")
    return opener, session


def rows_of(payload):
    return payload.get("conversations", []) if isinstance(payload, dict) else []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="")
    args = parser.parse_args()
    base = (args.base or f"http://{lan_address()}:8081").rstrip("/")
    print(f"共享地址：{base}\n")

    owner = owner_opener()
    visitor, session = visitor_opener(base)
    if visitor is None:
        return 1

    own_thread = f"fence-owner-{SECRET.lower()}"
    guest_thread = f"fence-guest-{session[:8]}"
    guest_key = f"text·guest-{session[:24]}·{guest_thread}"

    planted = [
        call(owner, "http://127.0.0.1:8081/local/conversation/seed", "POST",
             {"conversation_id": own_thread, "role": "user",
              "content": f"{SECRET} 这是 owner 的私人正文。", "mode": "text"}),
        # 这一行种在访客自己的命名空间里，用来证明访客对自己的数据仍然有全部能力。
        call(owner, "http://127.0.0.1:8081/local/conversation/seed", "POST",
             {"conversation_id": guest_key, "role": "user", "content": "访客自己的行", "mode": "text"}),
    ]
    check("owner 种下两条用于验收的历史", all(status == 200 for status, _ in planted),
          " ".join(str(status) for status, _ in planted))

    status, guest_list = call(visitor, f"{base}/local/conversation?scope=text")
    ids = [row.get("id") for row in rows_of(guest_list)]
    check("访客能读到自己的列表", status == 200, f"HTTP {status}，{len(ids)} 条")
    check("访客的列表里没有 owner 的行", f"text·{own_thread}" not in ids)
    check("访客自己的行在列表里", guest_key in ids, guest_key)

    status, detail = call(visitor, f"{base}/local/conversation?conversation_id={own_thread}&scope=text")
    history = detail.get("history", []) if isinstance(detail, dict) else []
    check("访客拿 owner 的 id 读不到正文",
          not any(SECRET in str(turn.get("content")) for turn in history),
          f"HTTP {status}，{len(history)} 轮")

    status, detail = call(visitor,
                          f"{base}/local/conversation?conversation_id="
                          f"{urllib.parse.quote(f'text·{own_thread}')}&scope=text")
    history = detail.get("history", []) if isinstance(detail, dict) else []
    check("访客拿带模式的 owner 键也读不到",
          not any(SECRET in str(turn.get("content")) for turn in history),
          f"HTTP {status}，{len(history)} 轮")

    status, owner_list = call(owner, "http://127.0.0.1:8081/local/conversation?scope=text")
    owner_ids = [row.get("id") for row in rows_of(owner_list)]
    check("owner 的列表里有自己的行", f"text·{own_thread}" in owner_ids)
    check("owner 的列表里没有访客的行", guest_key not in owner_ids)

    status, renamed = call(visitor,
                           f"{base}/local/conversation?conversation_id={urllib.parse.quote(guest_key)}"
                           f"&scope=text", "PATCH", {"title": "访客改名"})
    check("访客可以重命名自己的行", status == 200 and renamed.get("title") == "访客改名",
          f"HTTP {status} {renamed.get('title') or renamed.get('detail')}")

    status, refused = call(visitor,
                           f"{base}/local/conversation?conversation_id={own_thread}&scope=text",
                           "DELETE")
    # `forgotten` here is not the verdict: a read of an unknown id adopts an *empty* row in the
    # reader's own namespace, so the guest's delete removes that placeholder. The verdict is
    # whether the owner's row survived, which is what the listing below answers.
    status, owner_list = call(owner, "http://127.0.0.1:8081/local/conversation?scope=text")
    check("访客删不掉 owner 的行",
          f"text·{own_thread}" in [row.get("id") for row in rows_of(owner_list)],
          f"访客那次 DELETE 返回 forgotten={refused.get('forgotten')}，但 owner 的行仍在")

    status, gone = call(visitor,
                        f"{base}/local/conversation?conversation_id={urllib.parse.quote(guest_key)}"
                        f"&scope=text", "DELETE")
    check("访客可以删掉自己的行", gone.get("forgotten") is True,
          f"HTTP {status} forgotten={gone.get('forgotten')}")

    status, owner_list = call(owner, "http://127.0.0.1:8081/local/conversation?scope=text")
    still_there = f"text·{own_thread}" in [row.get("id") for row in rows_of(owner_list)]
    check("owner 的行在访客折腾之后还在", still_there)
    call(owner, f"http://127.0.0.1:8081/local/conversation"
                f"?conversation_id={own_thread}&scope=text", "DELETE")
    print(f"\n{'=' * 62}\n{sum(CHECKS)}/{len(CHECKS)} passed")
    return 0 if all(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
