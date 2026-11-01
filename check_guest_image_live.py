"""访客生图线上复现：以真正的访客身份提交一次生图，看它到底走到哪一步。

为什么需要这个脚本：`check_guest_capability_live.py` 只验证访客**能调用**生图接口
（`/local/image/progress` 200、`/local/image/cancel` 不是 403），从来没有真的提交过任务。
所以"访客生图"这条链路在线上一次都没被跑通过——这次要跑的就是它。

它会：
  1. 用 `share.config.json` 里的口令，经隧道以访客身份登录（回环永远是机主，测不出访客路径）；
  2. POST `/local/image/generate`，记录 HTTP 状态与响应体（409/403 一眼可见）；
  3. 每 2 秒读一次 `/local/image/progress`，把 phase 变化按时间打出来，直到任务落定或超时；
  4. 结束后在磁盘上找 `ComfyUI/output/guests/<会话前 24 位>/` 里有没有真的落图；
  5. 收尾把模式切回纯文本，不留状态。

用法：
    python -X utf8 check_guest_image_live.py                 # 关闭提示词优化（纯生图链路）
    python -X utf8 check_guest_image_live.py --enhance       # 打开优化（面板默认）
    python -X utf8 check_guest_image_live.py --base https://<tunnel>.trycloudflare.com
"""

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_guest_capability_live import (  # noqa: E402 - sibling script reuse
    Visitor, body_text, read_share_config, tunnel_url,
)

ROOT = Path(__file__).resolve().parent
OUTPUT_ROOT = Path(r"D:\AI\ComfyUI-BSAI_v010\ComfyUI\output")


def guest_files(session_prefix):
    folder = OUTPUT_ROOT / "guests" / session_prefix
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.glob("*.png"))


def submit_and_follow(visitor, *, enhance, timeout, label):
    print(f"\n===== {label}（enhance_prompt={enhance}） =====")
    body = {
        "prompt": "一只戴墨镜的柴犬，纯色背景，极简",
        "pipeline": "sdxl",
        "width": 512,
        "height": 512,
        "steps": 4,
        "cfg": 1.0,
        "denoise": 1.0,
        "seed": 1234,
        "enhance_prompt": enhance,
    }
    started = time.time()
    status, payload = visitor.call("/local/image/generate", "POST", body)
    text = body_text(payload)
    print(f"[{time.time() - started:6.1f}s] POST /local/image/generate -> HTTP {status}")
    if status != 202:
        print(f"          响应体：{text[:400]}")
        return {"accepted": False, "status": status, "body": text}

    job_id = ""
    try:
        job_id = json.loads(text).get("job", {}).get("id", "")
    except ValueError:
        pass
    print(f"          job={job_id}")

    phases, last_phase, last_state = [], None, None
    settled = None
    while time.time() - started < timeout:
        code, raw = visitor.call("/local/image/progress")
        if code != 200:
            print(f"[{time.time() - started:6.1f}s] progress HTTP {code}")
            time.sleep(2)
            continue
        snapshot = json.loads(body_text(raw) or "{}")
        job = snapshot.get("job") or {}
        phase = job.get("phase") or ""
        state = job.get("state") or ""
        if phase != last_phase or state != last_state:
            print(f"[{time.time() - started:6.1f}s] phase={phase!r} state={state!r} "
                  f"busy={snapshot.get('busy')} 进度={job.get('progress')}")
            phases.append((round(time.time() - started, 1), phase, state))
            last_phase, last_state = phase, state
        if not snapshot.get("busy") and state not in ("running", "queued", ""):
            settled = job
            break
        if not snapshot.get("busy") and not job_id:
            settled = job
            break
        time.sleep(2)
    elapsed = time.time() - started
    if settled is None:
        print(f"[{elapsed:6.1f}s] 超时未落定：最后一次 phase={last_phase!r} state={last_state!r}")
    else:
        print(f"[{elapsed:6.1f}s] 落定：state={settled.get('state')!r} "
              f"error={settled.get('error')!r} images={settled.get('images')}")
        timings = settled.get("timings") or {}
        if timings:
            print("          timings: " + ", ".join(f"{k}={v}" for k, v in timings.items()))
        enhancer = settled.get("enhancer") or {}
        if enhancer:
            print(f"          enhancer: used={enhancer.get('used')} reason={enhancer.get('reason')!r}")
    return {"accepted": True, "job_id": job_id, "phases": phases,
            "settled": settled, "elapsed": round(elapsed, 1)}


def queue_probe(visitor, timeout, session_prefix):
    """连续提交两次：第二次必须被收进排队位，而不是回 409；第三次必须被拒。

    这台机器一次只能画一张，但每轮有 36-46 秒只是把语言模型装回来，此时图片已经在屏幕上。
    以前的实现在这个窗口里一律 409，访客连点几次就以为卡死了。
    """
    print("\n===== 排队复现：连续提交 =====")
    base = {
        "prompt": "一只戴墨镜的柴犬，纯色背景，极简",
        "pipeline": "sdxl", "width": 512, "height": 512, "steps": 4,
        "cfg": 1.0, "denoise": 1.0, "seed": 1234, "enhance_prompt": False,
    }
    codes = []
    for index, wait in ((1, 0), (2, 3), (3, 1)):
        if wait:
            time.sleep(wait)
        code, payload = visitor.call("/local/image/generate", "POST", dict(base))
        text = body_text(payload)
        state = ""
        try:
            state = (json.loads(text).get("job") or {}).get("state", "")
        except ValueError:
            pass
        codes.append((index, code, state, text[:120]))
        print(f"  第 {index} 次提交 -> HTTP {code}  state={state!r}  {text[:120] if code >= 400 else ''}")

    seen, deadline = {}, time.time() + timeout
    while time.time() < deadline:
        code, raw = visitor.call("/local/image/progress")
        snapshot = json.loads(body_text(raw) or "{}")
        job = snapshot.get("job") or {}
        if job.get("id") and job.get("state"):
            seen.setdefault(job["id"], []).append(job["state"])
            if len(seen[job["id"]]) == 1 or seen[job["id"]][-1] != seen[job["id"]][-2]:
                print(f"  [{time.time() - deadline + timeout:6.1f}s] {job['id']} -> {job['state']} "
                      f"phase={job.get('phase')} queue_ahead={job.get('queue_ahead')}")
        if not snapshot.get("busy") and len(seen) >= 2:
            break
        time.sleep(2)
    files = guest_files(session_prefix)
    print(f"  磁盘产出：{len(files)} 个 -> {', '.join(files)}")
    return codes, seen, files


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="")
    parser.add_argument("--enhance", action="store_true", help="打开提示词优化（面板默认）")
    parser.add_argument("--queue", action="store_true", help="连续提交，验证排队而不是 409")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()

    config = read_share_config()
    token = str(config.get("access_token") or "")
    base = args.base or tunnel_url()
    if not base or not token:
        print("SKIP: 找不到共享地址或口令")
        return 1
    print("访客入口：" + base)
    print("安全闸门：本脚本不取消任何人的任务，只提交自己的那一张。")

    visitor = Visitor(base)
    print("登录 HTTP", visitor.login(token))
    session = visitor.session_token()
    print("会话标识", session[:24] + "…")

    status, raw = visitor.call("/local/status")
    before = json.loads(body_text(raw) or "{}")
    print(f"提交前：state={before.get('state')} mode={before.get('mode')} "
          f"active_requests={before.get('active_requests')}")

    if args.queue:
        codes, seen, files = queue_probe(visitor, args.timeout, session[:24])
        visitor.call("/local/mode", "POST", {"mode": "text"})
        accepted = sum(1 for _, code, _, _ in codes if code == 202)
        refused = sum(1 for _, code, _, _ in codes if code == 409)
        queued = any("queued_behind" in states for states in seen.values())
        print(f"\n结果：202 收了 {accepted} 次，409 拒了 {refused} 次，"
              f"观察到排队状态={queued}，落盘 {len(files)} 张")
        ok = accepted == 2 and refused == 1 and queued
        print("结论：" + ("第二次是排队而不是拒绝，第三次被拒（一位队列按预期工作）。"
                          if ok else "排队行为与预期不符，见上面时间线。"))
        return 0 if ok else 1

    result = submit_and_follow(visitor, enhance=args.enhance,
                              timeout=args.timeout, label="访客生图")
    files = guest_files(session[:24])
    print(f"\n磁盘上 guests/{session[:24]}/ 的文件：" + (", ".join(files) if files else "（没有）"))

    # 收尾：把模式交回纯文本，别把面板留在生图视图
    visitor.call("/local/mode", "POST", {"mode": "text"})
    status, raw = visitor.call("/local/status")
    after = json.loads(body_text(raw) or "{}")
    print(f"收尾后：state={after.get('state')} mode={after.get('mode')}")

    if not result.get("accepted"):
        print("\n结论：访客的生图请求被拒（见上面 HTTP 状态与响应体）。")
        return 1
    if not files:
        print("\n结论：请求被接收了，但磁盘上没有访客产出——任务没有走完。")
        return 1
    print("\n结论：访客生图链路走通，图片落在自己的目录里。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
