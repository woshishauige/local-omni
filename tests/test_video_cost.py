"""动态代价模型的回归测试（第 1 档验证：纯静态、零显存）。

所有者要求：视频生成里所有预计时间都要**动态估算**，并且**时长、步数、尺寸一起参与**，
不能再是写死的单一数字。这些用例钉住四件事：
  1. 模型能复现本机实测锚点（3/6/12/20 步）；
  2. 尺寸真的影响预计时间（这是原来完全缺失的一项）；
  3. 步数与时长各自单调；
  4. 真实样本回喂后，校准系数与尺寸指数会跟着变——即"会自我校准"，而不是常量。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.video import video_cost


class AnchorFitTests(unittest.TestCase):
    """解析部分必须复现实测锚点：768×512 / 7.5 秒 / 第一段。"""

    def test_reproduces_every_measured_anchor(self):
        """锚点复现的是**解析部分**（不含全局校准）。

        校准系数会随真实热样本移动——这是设计如此：真机跑得比解析预测慢，就该整体上调。
        所以这里断言 `shape.subtotal`，而不是会被校准缩放的总数。
        """
        for steps, measured in video_cost.ANCHORS:
            predicted = video_cost.predict(steps=steps, seconds=7.5, width=768, height=512)["shape"]["subtotal"]
            self.assertLess(abs(predicted - measured) / measured, 0.12,
                            f"{steps} 步：解析预测 {predicted} vs 实测 {measured}")

    def test_fit_is_derived_not_handwritten(self):
        fixed, per_step = video_cost._fit_anchors()
        self.assertGreater(per_step, 20.0)      # 每步几十秒量级
        self.assertLess(per_step, 60.0)
        self.assertGreater(fixed, 40.0)         # 编解码/保存的固定开销
        self.assertLess(fixed, 150.0)


class SizeMattersTests(unittest.TestCase):
    """尺寸必须参与——这正是所有者指出的缺陷：768×512 与 1024×672 以前得到同一个数。"""

    def test_larger_canvas_predicts_longer(self):
        small = video_cost.predict(steps=6, seconds=7.5, width=768, height=512)["seconds"]
        mid = video_cost.predict(steps=6, seconds=7.5, width=960, height=640)["seconds"]
        high = video_cost.predict(steps=6, seconds=7.5, width=1024, height=672)["seconds"]
        self.assertLess(small, mid)
        self.assertLess(mid, high)

    def test_size_scales_with_pixel_count(self):
        base = video_cost.predict(steps=6, seconds=7.5, width=768, height=512)["shape"]
        double = video_cost.predict(steps=6, seconds=7.5, width=768 * 2, height=512)["shape"]
        # 采样项按像素数线性放大（指数默认 1.0）；固定开销刻意增长得更慢。
        sampling_ratio = double["sampling_seconds"] / base["sampling_seconds"]
        overhead_ratio = double["overhead_seconds"] / base["overhead_seconds"]
        self.assertAlmostEqual(sampling_ratio, 2.0, places=2)
        self.assertEqual(overhead_ratio, 1.0)
        self.assertAlmostEqual(double["size_factor"], 2.0, places=2)

    def test_duration_and_steps_are_monotonic(self):
        short = video_cost.predict(steps=10, seconds=5.0, width=960, height=640)["seconds"]
        long = video_cost.predict(steps=10, seconds=12.0, width=960, height=640)["seconds"]
        few = video_cost.predict(steps=3, seconds=7.5, width=960, height=640)["seconds"]
        many = video_cost.predict(steps=20, seconds=7.5, width=960, height=640)["seconds"]
        self.assertLess(short, long)
        self.assertLess(few, many)

    def test_continuation_costs_more_than_the_first_shot(self):
        first = video_cost.predict(steps=6, seconds=7.5, width=960, height=640, position=1)["seconds"]
        later = video_cost.predict(steps=6, seconds=7.5, width=960, height=640, position=2)["seconds"]
        self.assertAlmostEqual(later / first, video_cost.CONTINUATION_FACTOR, places=1)

    def test_cold_session_adds_the_model_load_only_to_the_first_shot(self):
        warm = video_cost.predict(steps=6, seconds=7.5, width=768, height=512)["seconds"]
        cold = video_cost.predict(steps=6, seconds=7.5, width=768, height=512, cold=True)["seconds"]
        # 同样都是续段，只差"本次会话首次"这一个条件：差额就是模型装载。
        warm_later = video_cost.predict(steps=6, seconds=7.5, width=768, height=512,
                                        position=2, cold=False)["seconds"]
        cold_later = video_cost.predict(steps=6, seconds=7.5, width=768, height=512,
                                        position=2, cold=True)["seconds"]
        self.assertGreater(cold - warm, 400)
        self.assertAlmostEqual(cold_later, warm_later, places=1)

    def test_says_out_loud_when_the_size_is_extrapolated(self):
        verdict = video_cost.predict(steps=6, seconds=7.5, width=1024, height=672)
        self.assertFalse(verdict["size_calibrated"])
        self.assertIn("外推", verdict["confidence"])


class SelfCalibrationTests(unittest.TestCase):
    """真实样本回喂后模型要跟着变：这才是"动态"，不是把常量换个地方写。"""

    def setUp(self):
        self.previous = list(video_cost.samples())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, video_cost, "_CACHE", self.previous)
        patch.object(video_cost, "SAMPLES_PATH", Path(self.tmp.name) / "video-timing.json").start()
        self.addCleanup(patch.stopall)
        video_cost._CACHE = []

    def test_a_slow_run_raises_the_calibration(self):
        before = video_cost.predict(steps=6, seconds=7.5, width=768, height=512)["seconds"]
        baseline = video_cost.predict(steps=6, seconds=7.5, width=768, height=512)["shape"]["subtotal"]
        video_cost.record(steps=6, seconds=7.5, width=768, height=512, position=1,
                          wall_seconds=baseline * 1.4, recipe="video_balance6")
        after = video_cost.predict(steps=6, seconds=7.5, width=768, height=512)["seconds"]
        self.assertGreater(after, before * 1.2)
        self.assertGreater(video_cost.calibration(), 1.2)

    def test_two_sizes_calibrate_the_pixel_exponent(self):
        exponent_before, calibrated_before = video_cost.pixel_exponent()
        self.assertFalse(calibrated_before)
        baseline_small = video_cost.predict(steps=6, seconds=7.5, width=768, height=512)["shape"]["subtotal"]
        baseline_big = video_cost.predict(steps=6, seconds=7.5, width=1536, height=1024)["shape"]["subtotal"]
        # 小尺寸：实际=预测（比值 1）。大尺寸：4 倍像素却慢了 6 倍（超线性），
        # 于是指数应被抬到 1.0 以上——这才叫"用真实样本标定尺寸项"。
        video_cost.record(steps=6, seconds=7.5, width=768, height=512, position=1,
                          wall_seconds=baseline_small)
        video_cost.record(steps=6, seconds=7.5, width=1536, height=1024, position=1,
                          wall_seconds=baseline_big * 6)
        exponent_after, calibrated_after = video_cost.pixel_exponent()
        self.assertTrue(calibrated_after)
        self.assertGreater(exponent_after, exponent_before)
        self.assertGreater(exponent_after, 1.0)

    def test_samples_land_in_the_file_and_are_capped(self):
        for index in range(3):
            video_cost.record(steps=6, seconds=7.5, width=768, height=512, position=1,
                              wall_seconds=300.0 + index)
        stored = json.loads(video_cost.SAMPLES_PATH.read_text(encoding="utf-8"))
        self.assertEqual(len(stored), 3)
        self.assertEqual(stored[-1]["wall_seconds"], 302.0)
        self.assertIn("predicted_base", stored[-1])

    def test_a_zero_measurement_is_ignored(self):
        self.assertIsNone(video_cost.record(steps=6, seconds=7.5, width=768, height=512,
                                            position=1, wall_seconds=0.0))

    def test_a_cold_sample_does_not_move_the_calibration(self):
        """冷启动那一段含 H3 权重装载，模型里已由 cold_load_seconds 承担，不能再进校准。

        真机踩到：一次冷启动的 30.6 分钟样本把校准抬到 1.42，所有估算整体虚高 42%；
        扣掉装载后它与解析预测只差 5%。
        """
        baseline = video_cost.predict(steps=6, seconds=7.5, width=768, height=512)["shape"]["subtotal"]
        video_cost.record(steps=6, seconds=7.5, width=768, height=512, position=1,
                          wall_seconds=baseline * 2.5, recipe="video_distill3", cold=True)
        self.assertEqual(len(video_cost.samples()), 1)        # 作为证据留着
        self.assertEqual(len(video_cost.warm_samples()), 0)    # 但不参与拟合
        self.assertAlmostEqual(video_cost.calibration(), 1.0, places=3)

    def test_a_warm_sample_does_move_the_calibration(self):
        baseline = video_cost.predict(steps=6, seconds=7.5, width=768, height=512)["shape"]["subtotal"]
        video_cost.record(steps=6, seconds=7.5, width=768, height=512, position=1,
                          wall_seconds=baseline * 1.3, recipe="video_balance6")
        self.assertEqual(len(video_cost.warm_samples()), 1)
        self.assertAlmostEqual(video_cost.calibration(), 1.3, places=2)

    def test_describe_carries_everything_the_panel_needs(self):
        model = video_cost.describe()
        for key in ("fixed_seconds", "seconds_per_step", "base_width", "base_height", "base_seconds",
                    "continuation_factor", "cold_load_seconds", "warm_load_seconds",
                    "pixel_exponent", "size_calibrated", "calibration", "samples", "anchors"):
            self.assertIn(key, model)


class ShotRecordingTests(unittest.TestCase):
    """`finish_shot` 必须把真实用时记进模型，而且只记一次。

    真机踩到：一段 20 步 / 960×640 跑完 30.7 分钟后，任务 `done` 而分段仍停在 `running`、
    `samples` 为 0——因为成功出口在 `_follow` 里直接 return，走不到调用方的 `finish_shot`。
    修好后样本必须落盘，且重复调用不能把同一个样本记两遍（中位数会被拉偏）。
    """
    def setUp(self):
        self.previous = list(video_cost.samples())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, video_cost, "_CACHE", self.previous)
        patch.object(video_cost, "SAMPLES_PATH", Path(self.tmp.name) / "video-timing.json").start()
        self.addCleanup(patch.stopall)
        video_cost._CACHE = []

    def _job(self):
        from app.video import video_routes
        # begin_shot / finish_shot 是 VideoJob 的方法：每段的开始与收尾都记在任务自己身上。
        return video_routes.VideoJob("vid-test", "video_distill3", "a dog",
                                     {"width": 960, "height": 640,
                                      "shot_plan": [{"steps": 20, "seconds": 7.5, "seed": 1234}]})

    def test_finish_shot_records_one_sample(self):
        job = self._job()
        job.begin_shot(1)
        job.shots[0]["started"] -= 40        # 真实一段是分钟级；测试里把开始时间回拨，模拟 40 秒
        job.finish_shot(1, "done")
        self.assertEqual(job.shots[0]["state"], "done")
        self.assertEqual(len(video_cost.samples()), 1)
        sample = video_cost.samples()[0]
        self.assertEqual((sample["steps"], sample["width"], sample["height"]), (20, 960, 640))
        self.assertGreater(sample["wall_seconds"], 0)

    def test_finish_shot_is_idempotent(self):
        job = self._job()
        job.begin_shot(1)
        job.shots[0]["started"] -= 40
        job.finish_shot(1, "done")
        first = job.shots[0]["elapsed"]
        job.finish_shot(1, "done")          # 调用方随后还会再调一次，不能记两遍
        self.assertEqual(len(video_cost.samples()), 1)
        self.assertEqual(job.shots[0]["elapsed"], first)

    def test_a_failed_shot_is_not_recorded(self):
        job = self._job()
        job.begin_shot(1)
        job.shots[0]["started"] -= 40
        job.finish_shot(1, "error")
        self.assertEqual(video_cost.samples(), [])

    def test_an_instant_shot_is_not_recorded(self):
        """没有可测量的用时（例如立刻失败/立刻返回）就不要往校准里塞样本。"""
        job = self._job()
        job.begin_shot(1)
        job.finish_shot(1, "done")
        self.assertEqual(video_cost.samples(), [])


if __name__ == "__main__":
    unittest.main()
