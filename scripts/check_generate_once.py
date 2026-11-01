"""真机验收：提交一次生图任务（会顺带把 ComfyUI 拉起来），确认产出可下载。

这条把"功能可用"落到像素上：请求 → 轮询进度 → 拿到产出文件并下载，全程走网关。
"""

import json
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def opener_with_owner():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.open((ROOT / "logs" / "admin-link.txt").read_text(encoding="ascii").strip(), timeout=20)
    return opener


def call(opener, path, method="GET", body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request("http://127.0.0.1:8081" + path, data=data, method=method,
                                     headers={"content-type": "application/json"} if data else {})
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as failure:
        raw = failure.read()
        status = failure.code
    try:
        return status, json.loads(raw.decode("utf-8", "replace") or "{}"), raw
    except ValueError:
        # 二进制产出（PNG/MP4）也走这个取回函数：不是 JSON 就把原样交回给调用者。
        return status, {"raw": raw[:200]}, raw


def main():
    owner = opener_with_owner()
    status, body, _ = call(owner, "/local/image/generate", "POST", {
        "prompt": "一只戴墨镜的柴犬，纯色背景，极简",
        "negative": "", "width": 512, "height": 512, "steps": 4, "cfg": 1.0,
        "seed": 1234, "enhance_prompt": False,
    })
    job_id = (body or {}).get("job", {}).get("id") or (body or {}).get("id")
    print(f"提交生图: HTTP {status} · job={job_id} · {str(body)[:120]}")
    if status not in (200, 202) or not job_id:
        return 1

    deadline = time.time() + 900
    last = None
    while time.time() < deadline:
        _, progress, _ = call(owner, "/local/image/progress")
        job = (progress or {}).get("job") or {}
        note = f"{job.get('state')} · {job.get('phase')} · {job.get('progress')}"
        if note != last:
            print("  进度:", note)
            last = note
        if job.get("state") in ("completed", "error", "cancelled"):
            break
        time.sleep(5)
    else:
        print("超时未完成")
        return 1

    job = (progress or {}).get("job") or {}
    images = job.get("images") or []
    print(f"最终: {job.get('state')} · 产出 {images}")
    if job.get("state") != "completed":
        print("失败详情:", json.dumps(job, ensure_ascii=False)[:400])
        return 1

    target = images[0] if images else ""
    if not target:
        print("没有产出路径")
        return 1
    # 进度里给的就是可直接取回的 URL（/local/image/file?filename=…&subfolder=…）。
    path = target if target.startswith("/") else "/local/image/file?path=" + urllib.parse.quote(str(target))
    status, _, raw = call(owner, path)
    print(f"下载产出: HTTP {status} · {len(raw)} 字节 · PNG 魔数={raw[:8] == b'\\x89PNG\\r\\n\\x1a\\n'}")
    return 0 if status == 200 and raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) > 1000 else 1


if __name__ == "__main__":
    sys.exit(main())
