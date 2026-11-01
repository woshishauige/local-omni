"""一次最小视频渲染 + 显存采样：回答"960×640 是否超负荷"，同时给代价模型补真实样本。

最小口径（所有者规定）：配方 `video_distill3`（3 步蒸馏）、单镜头、960×640、7.5 秒、关提示词优化。
渲染期间每 10 秒读一次 `/local/image/gpu`，记录显存占用、利用率与阶段；结束后核对：
  * 峰值/典型显存占用（判断"只用 25% 显存"是容量问题还是 H3 的按需卸载）；
  * `logs/video-timing.json` 里是否落下真实用时样本；
  * 代价模型的预测与实际相差多少（尺寸项因此获得第二个尺寸的真实数据）。

    & ..\\ComfyUI-BSAI_v010\\python\\python.exe -X utf8 check_video_min_live.py
"""

import http.cookiejar
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COST_SAMPLES = ROOT / "logs" / "video-timing.json"


def opener_with_owner():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.open((ROOT / "logs" / "admin-link.txt").read_text(encoding="ascii").strip(), timeout=20)
    return opener


def call(opener, path, method="GET", body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request("http://127.0.0.1:8081" + path, data=data, method=method,
                                     headers={"content-type": "application/json"} if data else {})
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as failure:
        raw, status = failure.read(), failure.code
    try:
        return status, json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        return status, {"raw": raw[:200]}


def main():
    owner = opener_with_owner()
    import video_cost

    model = call(owner, "/local/video/recipes")[1].get("cost_model") or {}
    print("代价模型:", {k: model.get(k) for k in
                     ("seconds_per_step", "fixed_seconds", "pixel_exponent", "size_calibrated",
                      "calibration", "samples")})
    prediction = video_cost.predict(steps=3, seconds=7.5, width=960, height=640)
    print(f"本次预测（video_distill3 · 1 段 · 960×640 · 3 步）: {prediction['seconds']} 秒 "
          f"= {prediction['seconds'] / 60:.1f} 分钟 · {prediction['confidence']}")

    status, body = call(owner, "/local/video/generate", "POST", {
        "recipe": "video_distill3",
        "prompt": "一只戴墨镜的柴犬在纯色背景前缓慢转头，极简，柔和光线",
        "shots": [""],
        # 步数必须放在 shot_plan 里：不发行级条目时 `_shot_plan` 会按默认 **20 步**处理
        # （第一次跑这个脚本就踩到了：预计 1289 秒而不是 267 秒，白烧了二十分钟显卡）。
        "shot_plan": [{"steps": 3, "seconds": 7.5}],
        "width": 960, "height": 640,
        "steps": 3, "seconds_per_shot": 7.5,
        "enhance_prompt": False,
        "project_name": "cost-probe",
        "structure_guard": False,
    }, timeout=60)
    job_id = (body.get("job") or {}).get("id") or body.get("id")
    print(f"提交: HTTP {status} · job={job_id} · {str(body)[:160]}")
    if status not in (200, 202) or not job_id:
        return 1

    samples, started, last_phase = [], time.time(), None
    while time.time() - started < 3600:
        _, gpu = call(owner, "/local/image/gpu")
        vram = gpu.get("vram") or {}
        samples.append({"t": round(time.time() - started, 1), "used": vram.get("used_mib"),
                        "total": vram.get("total_mib"), "free": gpu.get("free_mib")})
        _, progress = call(owner, "/local/video/progress")
        job = progress.get("job") or {}
        phase = job.get("phase") or ""
        if phase != last_phase:
            print(f"  [{time.time() - started:6.1f}s] {phase} · 显存 {vram.get('used_mib')} MiB"
                  f" · {job.get('position') or 0}/{job.get('total') or 0} 段")
            last_phase = phase
        if job.get("state") in ("done", "completed", "error", "cancelled"):
            break
        time.sleep(10)

    elapsed = time.time() - started
    used = [s["used"] for s in samples if isinstance(s["used"], (int, float))]
    total = samples[0]["total"] if samples and samples[0]["total"] else None
    print(f"\n结束：{job.get('state')} · 用时 {elapsed / 60:.1f} 分钟")
    if job.get("error"):
        print("  错误:", job["error"])
    if used and total:
        print(f"  显存采样 {len(used)} 次：最低 {min(used)} / 中位 {int(statistics.median(used))} / "
              f"最高 {max(used)} MiB（共 {total} MiB，峰值占 {100 * max(used) / total:.0f}%）")
    for shot in job.get("shots") or []:
        print(f"  第 {shot.get('index')} 段: {shot.get('state')} · 预计 {shot.get('estimate')} 秒"
              f" · 实际 {shot.get('elapsed')} 秒")

    if COST_SAMPLES.exists():
        stored = json.loads(COST_SAMPLES.read_text(encoding="utf-8"))
        print(f"\n代价模型样本 {len(stored)} 个，最近一条: "
              f"{ {k: stored[-1].get(k) for k in ('steps', 'width', 'height', 'predicted_base', 'wall_seconds')} }")
    print(f"新校准系数 {video_cost.calibration():.3f} · "
          f"尺寸指数 {video_cost.pixel_exponent()[0]:.2f}（已标定={video_cost.pixel_exponent()[1]}）")
    return 0 if job.get("state") in ("done", "completed") else 1


if __name__ == "__main__":
    sys.exit(main())
