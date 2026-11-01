"""打印代价模型的估算表，用于人工核对"尺寸/步数/时长是否真的共同影响预计时间"。

    & ..\\ComfyUI-BSAI_v010\\python\\python.exe -X utf8 show_video_cost.py
"""
import video_cost

print("锚点（本机实测，768×512 / 7.5 秒 / 第一段）:", video_cost.ANCHORS)
print(f"拟合结果：固定开销 {video_cost.FIXED_SECONDS:.1f} 秒 + 每步 {video_cost.SECONDS_PER_STEP:.2f} 秒")
print(f"校准系数 {video_cost.calibration():.3f} | 样本 {len(video_cost.samples())} 个 "
      f"| 尺寸指数 {video_cost.pixel_exponent()[0]:.2f}（已标定={video_cost.pixel_exponent()[1]}）")
print()
print("尺寸 × 步数（7.5 秒 / 第一段）：")
head = "  " + "尺寸".ljust(12) + "".join(f"{s} 步".rjust(11) for s in (3, 6, 12, 20))
print(head)
for width, height in ((768, 512), (960, 640), (1024, 672)):
    cells = []
    for steps in (3, 6, 12, 20):
        value = video_cost.predict(steps=steps, seconds=7.5, width=width, height=height)["seconds"]
        cells.append(f"{value / 60:.1f}分".rjust(11))
    print("  " + f"{width}×{height}".ljust(12) + "".join(cells))
print()
print("同尺寸下时长的影响（960×640 / 6 步 / 第一段）：")
for seconds in (5.0, 7.5, 10.0, 13.75):
    value = video_cost.predict(steps=6, seconds=seconds, width=960, height=640)["seconds"]
    print(f"  {seconds:>5.2f} 秒 → {value / 60:>5.1f} 分钟")
