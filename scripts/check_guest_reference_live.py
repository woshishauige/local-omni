"""第 2 档实测：访客能不能传参考图，以及"只认自己那张"这条围栏在线上成不成立。

零生成、零显卡，也不排队：三个请求都是"上传"或"提交"，最后一个故意用一个**不存在的配方**，
所以它既证明"自己的参考图能过围栏"，又不会真的排一次渲染（渲染代价见 `video_cost.py`）。

    python -X utf8 check_guest_reference_live.py

走**局域网地址**（回环永远是机主，测不出访客路径），口令来自 `share.config.json`。
"""
import json
import sys
from pathlib import Path
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_guest_capability_live import (  # noqa: E402 - 复用同目录脚本
    Visitor, lan_address, read_share_config,
)

ROOT = Path(__file__).resolve().parents[1]
COMFY = "http://127.0.0.1:8188"
# 上传路由按后缀与体积判定（内容由 ComfyUI 那边校验），这里给一段带 PNG 魔数的负载。
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 240


def owner_state(path):
    """机主视角读一次（回环即机主），用于报告任务表状态。"""
    link = (ROOT / "logs" / "admin-link.txt").read_text(encoding="ascii").strip()
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.open(link, timeout=30).read()
    with opener.open(f"http://127.0.0.1:{port()}{path}", timeout=30) as response:
        return json.loads(response.read())


def port() -> int:
    return int(read_share_config().get("listen_port") or 8081)


def queue_state():
    with urllib.request.urlopen(COMFY + "/queue", timeout=10) as response:
        queue = json.loads(response.read())
    return len(queue.get("queue_running") or []), len(queue.get("queue_pending") or [])


def main() -> int:
    config = read_share_config()
    token = str(config.get("access_token") or "")
    lan = lan_address()
    if not token or not lan:
        print(f"跳过：没有共享口令或局域网地址（token={bool(token)} lan={lan!r}）")
        return 0
    base = f"http://{lan}:{port()}"
    print(f"访客入口：{base}")

    before = queue_state()
    print(f"ComfyUI 队列（开始）：running={before[0]} pending={before[1]}")

    first, second = Visitor(base), Visitor(base)
    print(f"登录 HTTP：第一个 {first.login(token)}，第二个 {second.login(token)}")
    print(f"会话标识：{first.session_token()[:24]}… / {second.session_token()[:24]}…")

    status, body = first.call("/local/video/upload?name=ref.png", "POST", raw=PNG)
    print(f"\n[1] 访客上传参考图 → HTTP {status}")
    if status != 200:
        print("    " + body[:200].decode("utf-8", "replace"))
        return 1
    name = json.loads(body)["name"]
    print(f"    name={name}  bytes={len(PNG)}  （前缀 qwen-image-ref- 说明走的是参考图那条）")
    if not name.startswith("qwen-image-ref-"):
        print("    参考图没有走参考图前缀，检查上传分支")
        return 1

    status, body = second.call("/local/video/generate", "POST",
                               body={"prompt": "x", "shots": [], "shot_references": [[name]]})
    print(f"\n[2] 另一个访客拿这张图去生成 → HTTP {status}")
    print("    " + body[:200].decode("utf-8", "replace"))
    if status != 404:
        print("    应被当作不存在的素材拒绝（404）")
        return 1

    status, body = first.call("/local/video/generate", "POST",
                              body={"prompt": "x", "shots": [], "recipe": "不存在的配方",
                                    "shot_references": [[name]]})
    print(f"\n[3] 本人拿自己的图去生成（配方式不存在，只为看围栏是否放行）→ HTTP {status}")
    print("    " + body[:200].decode("utf-8", "replace"))
    if status != 404 or "参考图" in body.decode("utf-8", "replace"):
        print("    自己的参考图应该过围栏，随后因配方不存在而被拒")
        return 1

    after = queue_state()
    progress = owner_state("/local/video/progress")
    print(f"\nComfyUI 队列（结束）：running={after[0]} pending={after[1]}；"
          f"网关视频任务 busy={progress.get('busy')}")
    if after != before or progress.get("busy"):
        print("    这一轮不该产生任何渲染任务")
        return 1
    print("\n结论：访客能传参考图；别人的图当作不存在；自己的图能过围栏；全程没有排渲染。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
