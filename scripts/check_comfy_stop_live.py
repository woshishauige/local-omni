"""第 2 档实测：`ComfySupervisor.stop()` 现在真的能把 ComfyUI 停下来吗。

背景：这一步是"关页面收工"的最后一步之一，而 19:51 的真机收工里它是唯一失败的一步
（`name 'psutil' is not defined`），ComfyUI 因此继续占着 8188。修好导入后在这里实测一次：
调用它、等一会儿、看 8188 是否关闭。

零显卡代价：停掉的是空闲的 ComfyUI（下次生图/视频任务会按需把它拉起来，这条路径本会话已验过）。

    python -X utf8 check_comfy_stop_live.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import socket
import sys
import time

import httpx

from app.image import image_comfy


def port_open(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def main() -> int:
    if not port_open(8188):
        print("ComfyUI 本来就没在跑（8188 关闭）——没什么可停的，视为通过")
        return 0
    supervisor = image_comfy.ComfySupervisor(httpx.AsyncClient())
    print(f"停止前 8188：开着")
    stopped = supervisor.stop()
    print(f"stop() 返回：{stopped}  last_error={supervisor.last_error!r}")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and port_open(8188):
        time.sleep(1)
    closed = not port_open(8188)
    print(f"停止后 8188：{'已关闭' if closed else '仍在监听'}")
    return 0 if (stopped and closed) else 1


if __name__ == "__main__":
    sys.exit(main())
