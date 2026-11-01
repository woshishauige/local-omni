"""视频生成的动态代价模型：把"预计时间"从写死的数字变成可解释、会自我校准的估算。

为什么需要它：所有者指出面板上的预计时间只是静态数据——配方里写死的 `measured`（768×512、7.5 秒
一颗锚点），**完全没有尺寸项**：把 768×512 换成 1024×672，预计时间一模一样，而实测差很多。

模型形状（每一段，单位秒）：

    sampling = 每步秒数 × 步数 × (时长 / 7.5) × 尺寸因子
    overhead = 固定开销 × (0.5 + 0.5 × (时长 / 7.5))        # 编解码与保存，随时长亚线性增长
    单段     = (sampling + overhead) × 续段系数 × 全局校准
    冷启动   = 第一段且本次会话首次运行，另加 (冷载 − 热载)

尺寸因子 = (宽×高 / 768×512) ** 尺寸指数，指数默认 1.0（采样算力≈像素数），拿到两个以上不同尺寸的
真实样本后自动标定。

锚点全部来自本机实测（768×512 / 7.5 秒 / 第一段）：3 步 223 s、6 步 304 s、12 步 527 s、20 步 870 s。
其中 20 步那条的**面板入口已下线**，但 870 s 是真跑出来的测量值，作为锚点保留——锚点是测量，不是可选项。
`fixed` 与 `每步秒数` 由这四个点最小二乘拟合得到，不是手填的。

**自我校准**：每段跑完把（步数、时长、尺寸、是否续段、实际用时）写进 `logs/video-timing.json`；
`校准系数` = 最近样本里"实际/预测"的中位数，用来吸收机器状态漂移；`尺寸指数` 在有跨尺寸样本后
按对数—对数回归重估。没人跑过新尺寸时，`describe()` 会明确报告"尺寸项尚未标定"，面板据此说明。
"""

from __future__ import annotations

import json
import math
import statistics
import time
from pathlib import Path

from app.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT
SAMPLES_PATH = ROOT / "logs" / "video-timing.json"

# 本机实测锚点：(步数, 单段总用时秒)，条件为 768×512 / 7.5 秒 / 第一段 / 模型已加载。
ANCHORS: tuple[tuple[int, float], ...] = ((3, 223.0), (6, 304.0), (12, 527.0), (20, 870.0))
BASE_WIDTH, BASE_HEIGHT, BASE_SECONDS = 768, 512, 7.5
# 第二段起带 22 帧运动上下文，实测五次衔接的中位比值。
CONTINUATION_FACTOR = 1.45
# 本次会话首次运行装载 H3，之后每次重新装载（换模式后再回来）。
COLD_LOAD_SECONDS = 598.0
WARM_LOAD_SECONDS = 72.0
PIXEL_EXPONENT_DEFAULT = 1.0
MAX_SAMPLES = 60


def _fit_anchors() -> tuple[float, float]:
    """最小二乘拟合 (固定开销, 每步秒数)：由实测锚点算出，不手工填写。"""
    xs = [float(steps) for steps, _ in ANCHORS]
    ys = [float(total) for _, total in ANCHORS]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    denom = sum((x - mean_x) ** 2 for x in xs)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom if denom else 0.0
    return mean_y - slope * mean_x, slope


FIXED_SECONDS, SECONDS_PER_STEP = _fit_anchors()

_CACHE: dict | None = None


def _load_samples() -> list[dict]:
    try:
        data = json.loads(SAMPLES_PATH.read_text(encoding="utf-8"))
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write_samples(samples: list[dict]) -> None:
    try:
        SAMPLES_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SAMPLES_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(samples[-MAX_SAMPLES:], ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(SAMPLES_PATH)
    except OSError:
        pass                                   # 写不进去不影响估算，只是这次校准丢了


def samples() -> list[dict]:
    global _CACHE
    if _CACHE is None:
        _CACHE = _load_samples()
    return _CACHE


def measured_sizes() -> list[list[int]]:
    """跑过真实样本的尺寸，去重后按出现顺序返回。"""
    seen: list[list[int]] = []
    for item in samples():
        pair = [int(item.get("width") or 0), int(item.get("height") or 0)]
        if pair[0] > 0 and pair[1] > 0 and pair not in seen:
            seen.append(pair)
    return seen


def warm_samples() -> list[dict]:
    """只取**热**样本（模型已加载时跑的）。

    冷启动那一段把 H3 权重的装载也算了进去（本机实测约 600 秒），而模型里本来就有
    `cold_load_seconds` 这一项——再把冷样本喂进校准，等于把装载重复计算一遍。真机踩到：
    一次冷启动的 30.6 分钟样本让校准系数升到 1.42，所有估算被整体抬高 42%，
    而扣掉装载后它与解析预测只差 5%。
    """
    return [item for item in samples() if not item.get("cold")]


def pixel_exponent() -> tuple[float, bool]:
    """尺寸指数，以及它是否是真实标定出来的。

    只有在拿到**两个以上不同像素规模**的样本时才拟合；否则用默认 1.0 并如实报告未标定——
    假装精确比承认外推更糟。只用热样本（见 `warm_samples`）。
    """
    usable = [item for item in warm_samples()
              if item.get("wall_seconds") and item.get("predicted_base")
              and item.get("width") and item.get("height")]
    by_pixels: dict[float, list[float]] = {}
    for item in usable:
        pixels = float(item["width"]) * float(item["height"])
        ratio = float(item["wall_seconds"]) / float(item["predicted_base"])
        by_pixels.setdefault(pixels, []).append(ratio)
    if len(by_pixels) < 2:
        return PIXEL_EXPONENT_DEFAULT, False
    xs = [math.log(pixels / (BASE_WIDTH * BASE_HEIGHT)) for pixels in by_pixels]
    ys = [math.log(statistics.median(ratios)) for ratios in by_pixels.values()]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom <= 1e-9:
        return PIXEL_EXPONENT_DEFAULT, False
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
    # 夹在合理范围：小于 0.35 等于宣称尺寸几乎不影响算力，大于 1.6 等于宣称远超线性，都不合理。
    return max(0.35, min(1.6, slope)), True


def calibration() -> float:
    """热样本里"实际/预测"的中位数：吸收机器状态漂移（后台占用、温度、驱动）。

    冷启动样本被排除：装载时间由 `cold_load_seconds` 单独承担，混进来会重复计算。
    """
    ratios = [float(item["wall_seconds"]) / float(item["predicted"])
              for item in warm_samples()
              if item.get("wall_seconds") and item.get("predicted")]
    if not ratios:
        return 1.0
    recent = ratios[-12:]
    median = statistics.median(recent)
    return max(0.6, min(1.8, median))


def _shape(steps: int, seconds: float, width: int, height: int, position: int) -> dict:
    """不含冷启动与校准的解析部分（锚点在上面的拟合里）。"""
    steps = max(1, int(steps))
    seconds = max(0.5, float(seconds))
    pixels = max(1.0, float(width) * float(height))
    duration = seconds / BASE_SECONDS
    exponent, calibrated = pixel_exponent()
    size_factor = (pixels / (BASE_WIDTH * BASE_HEIGHT)) ** exponent
    sampling = SECONDS_PER_STEP * steps * duration * size_factor
    overhead = FIXED_SECONDS * (0.5 + 0.5 * duration)
    continuation = CONTINUATION_FACTOR if position > 1 else 1.0
    return {
        "steps": steps, "seconds": seconds, "width": int(width), "height": int(height),
        "position": int(position),
        "duration_factor": round(duration, 4),
        "size_factor": round(size_factor, 4),
        "pixel_exponent": round(exponent, 3),
        "size_calibrated": calibrated,
        "sampling_seconds": round(sampling, 1),
        "overhead_seconds": round(overhead, 1),
        "continuation_factor": continuation,
        "subtotal": round((sampling + overhead) * continuation, 1),
    }


def predict(*, steps: int, seconds: float, width: int, height: int, position: int = 1,
            cold: bool = False) -> dict:
    """一段的预计用时（秒），连同它是怎么算出来的。"""
    shape = _shape(steps, seconds, width, height, position)
    factor = calibration()
    cold_extra = max(0.0, COLD_LOAD_SECONDS - WARM_LOAD_SECONDS) if (cold and position == 1) else 0.0
    total = shape["subtotal"] * factor + cold_extra
    return {
        "seconds": round(total, 1),
        "shape": shape,
        "calibration": round(factor, 4),
        "cold_extra": round(cold_extra, 1),
        "samples": len(samples()),
        "size_calibrated": shape["size_calibrated"],
        "confidence": ("按本机实测锚点与尺寸标定" if shape["size_calibrated"]
                       else "按本机实测锚点，尺寸为线性外推（该尺寸还没跑过）"),
    }


def record(*, steps: int, seconds: float, width: int, height: int, position: int,
           wall_seconds: float, recipe: str = "", cold: bool = False) -> dict | None:
    """把一段的真实用时记下来，供后续校准。返回记录，异常一律吞掉。

    `cold=True` 表示这是本次会话第一次跑（含 H3 权重装载）：仍然记下来作为证据，
    但不参与校准与尺寸指数拟合——装载时间由 `cold_load_seconds` 单独承担。
    """
    global _CACHE
    if not wall_seconds or wall_seconds <= 0:
        return None
    shape = _shape(steps, seconds, width, height, position)
    item = {
        "at": round(time.time(), 1),
        "recipe": recipe,
        "cold": bool(cold),
        "steps": shape["steps"], "seconds": shape["seconds"],
        "width": shape["width"], "height": shape["height"], "position": shape["position"],
        # predicted_base = 只含解析部分（不含校准与冷启动），这样"实际/预测"才是干净的比值。
        "predicted_base": shape["subtotal"],
        "predicted": round(shape["subtotal"] * calibration(), 1),
        "wall_seconds": round(float(wall_seconds), 1),
    }
    items = list(samples())
    items.append(item)
    _CACHE = items[-MAX_SAMPLES:]
    _write_samples(_CACHE)
    return item


def describe() -> dict:
    """面板需要的一切：常量、标定状态、样本数。面板用同一套公式本地算，免得每次按键都请求。"""
    exponent, calibrated = pixel_exponent()
    return {
        "fixed_seconds": round(FIXED_SECONDS, 1),
        "seconds_per_step": round(SECONDS_PER_STEP, 3),
        "base_width": BASE_WIDTH, "base_height": BASE_HEIGHT, "base_seconds": BASE_SECONDS,
        "continuation_factor": CONTINUATION_FACTOR,
        "cold_load_seconds": COLD_LOAD_SECONDS, "warm_load_seconds": WARM_LOAD_SECONDS,
        "pixel_exponent": round(exponent, 3),
        "size_calibrated": calibrated,
        "calibration": round(calibration(), 4),
        "samples": len(samples()),
        "warm_samples": len(warm_samples()),
        "measured_sizes": measured_sizes(),
        "anchors": [[steps, total] for steps, total in ANCHORS],
    }
