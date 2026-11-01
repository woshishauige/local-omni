"""两个标识位的回归测试（第 1 档：纯静态、零显存、不起服务）。

所有者规定的口径：**页面位**（有没有前端 Qwen 页面）与**任务位**（页面上有没有任务在跑），
两位**同时为 0** 才自动关后台；任一位是 1 都不关。这些用例把假时钟、假任务探针、假收工
塞进状态机，钉住这条口径和三个不误伤的细节。
"""
import asyncio
import unittest

import page_lifecycle


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class LifecycleHarness(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.task = ""
        self.stops = []
        self.enabled = True
        self.lifecycle = page_lifecycle.PageLifecycle(
            task_check=lambda: self.task,
            stopper=lambda: self.stops.append(self.clock.now),
            enabled=lambda: self.enabled,
            clock=self.clock,
            page_idle=10.0,
        )

    def tick(self, times=1, step=1.0):
        async def drive():
            task = asyncio.create_task(self.lifecycle.run(tick=0.01))
            for _ in range(times):
                self.clock.advance(step)
                await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(drive())

    def open_page(self):
        self.lifecycle.note_page_alive()


class PageFlagTests(LifecycleHarness):
    """页面位：有页面 1、没页面 0，靠心跳自动检测。"""

    def test_without_any_page_it_is_zero(self):
        self.assertEqual(self.lifecycle.page_flag(), 0)

    def test_a_heartbeat_turns_it_on(self):
        self.open_page()
        self.assertEqual(self.lifecycle.page_flag(), 1)

    def test_it_turns_off_after_the_heartbeat_window(self):
        self.open_page()
        self.clock.advance(9.0)
        self.assertEqual(self.lifecycle.page_flag(), 1, "窗口内还算有页面")
        self.clock.advance(2.0)
        self.assertEqual(self.lifecycle.page_flag(), 0, "超过窗口就没页面了")

    def test_the_close_beacon_starts_the_countdown(self):
        """`pagehide` 也会在刷新时触发，所以它只能开始计时，不能立刻判成"没有页面"。"""
        self.open_page()
        self.lifecycle.note_page_closed()
        self.assertEqual(self.lifecycle.page_flag(), 1, "刚收到信标时仍在窗口内")
        self.clock.advance(11.0)
        self.assertEqual(self.lifecycle.page_flag(), 0, "窗口过去仍没有心跳才算没有页面")

    def test_a_refresh_does_not_close_the_stack(self):
        """刷新：旧页面先 pagehide，新页面随后加载。以前这一下就把整个栈关了。"""
        self.open_page()
        self.lifecycle.note_page_closed()          # 旧页面卸载
        self.clock.advance(1.5)                    # 新页面起来并心跳
        self.lifecycle.note_page_alive()
        self.tick(times=8, step=1.0)
        self.assertEqual(self.stops, [], "刷新不该让网关收工")

    def test_the_heartbeat_comes_back_after_a_refresh(self):
        self.open_page()
        self.lifecycle.note_page_closed()
        self.open_page()
        self.assertEqual(self.lifecycle.page_flag(), 1)
        self.assertIsNone(self.lifecycle.flags()["closed_seconds_ago"])


class ThrottledTabTests(unittest.TestCase):
    """后台标签被浏览器节流时，不能把它当成"页面关了"。

    所有者反馈："网页没关但是一直报错说没连接上"。原因之一就是窗口太短：隐藏页的定时器可以
    一分钟才响一次，45 秒的窗口会把还开着的页面判成关了，于是服务自己收工，页面再也连不上。
    """

    def setUp(self):
        self.clock = FakeClock()
        self.lifecycle = page_lifecycle.PageLifecycle(
            task_check=lambda: "", stopper=lambda: None, enabled=lambda: True, clock=self.clock)

    def test_the_default_window_tolerates_several_throttled_beats(self):
        self.assertGreaterEqual(page_lifecycle.PAGE_IDLE_SECONDS,
                                page_lifecycle.HEARTBEAT_SECONDS * 5,
                                "至少容得下五次被节流的心跳")

    def test_a_minute_of_silence_keeps_the_page_alive(self):
        self.lifecycle.note_page_alive()
        self.clock.advance(70.0)          # 隐藏标签典型的一次心跳间隔
        self.assertEqual(self.lifecycle.page_flag(), 1, "一分钟没心跳不等于页面关了")

    def test_a_long_freeze_eventually_counts_as_gone(self):
        self.lifecycle.note_page_alive()
        self.clock.advance(page_lifecycle.PAGE_IDLE_SECONDS + 1)
        self.assertEqual(self.lifecycle.page_flag(), 0)

    def test_a_heartbeat_after_the_freeze_revives_the_page(self):
        self.lifecycle.note_page_alive()
        self.clock.advance(page_lifecycle.PAGE_IDLE_SECONDS + 1)
        self.assertEqual(self.lifecycle.page_flag(), 0)
        self.lifecycle.note_page_alive()
        self.assertEqual(self.lifecycle.page_flag(), 1, "页面回来发一次心跳就该复活")

    def test_the_beacon_shortens_the_window_instead_of_waiting_it_out(self):
        """真关窗不该等满 150 秒；但窗口也不会短于 page_idle（单测把 page_idle 设小时行为不变）。"""
        self.lifecycle.note_page_alive()
        self.lifecycle.note_page_closed()
        self.assertEqual(self.lifecycle.flags()["window_seconds"],
                         min(page_lifecycle.PAGE_IDLE_SECONDS,
                             page_lifecycle.CLOSE_AFTER_BEACON_SECONDS))
        small = page_lifecycle.PageLifecycle(task_check=lambda: "", stopper=lambda: None,
                                            enabled=lambda: True, clock=self.clock, page_idle=10.0)
        small.note_page_alive()
        small.note_page_closed()
        self.assertEqual(small.flags()["window_seconds"], 10.0, "窗口不会比 page_idle 更长")
        self.lifecycle.note_page_alive()
        self.assertEqual(self.lifecycle.flags()["window_seconds"], page_lifecycle.PAGE_IDLE_SECONDS,
                         "心跳回来之后窗口恢复")


class TaskFlagTests(LifecycleHarness):
    """任务位：有任务 1、没任务 0，现读不缓存。"""

    def test_no_task_is_zero(self):
        self.assertEqual(self.lifecycle.task_flag(), 0)

    def test_a_running_task_is_one(self):
        self.task = "生图任务在跑或排队"
        self.assertEqual(self.lifecycle.task_flag(), 1)
        self.assertEqual(self.lifecycle.flags()["task_label"], "生图任务在跑或排队")

    def test_it_falls_back_to_zero_when_the_task_ends(self):
        self.task = "视频任务在跑"
        self.assertEqual(self.lifecycle.task_flag(), 1)
        self.task = ""
        self.assertEqual(self.lifecycle.task_flag(), 0, "任务结束必须自动转 0")

    def test_a_broken_probe_counts_as_a_running_task(self):
        def broken():
            raise RuntimeError("探针坏了")
        self.lifecycle.task_check = broken
        self.assertEqual(self.lifecycle.task_flag(), 1, "探不动时宁可不关")


class CloseDecisionTests(LifecycleHarness):
    """两位同时为 0 才关；任一位是 1 都不关。"""

    def test_both_zero_closes_the_stack_once(self):
        self.open_page()
        self.lifecycle.note_page_closed()
        self.tick(times=3, step=1.0)
        self.assertEqual(self.stops, [], "信标之后还有窗口，先不收工")
        self.tick(times=12, step=1.0)              # 越过心跳窗口
        self.assertEqual(len(self.stops), 1, self.stops)
        self.assertTrue(self.lifecycle.flags()["stopped"])
        self.tick(times=3, step=1.0)
        self.assertEqual(len(self.stops), 1, "收工只该发生一次")

    def test_an_open_page_never_closes(self):
        self.open_page()
        for _ in range(12):               # 页面每 2 秒续一次心跳，窗口 10 秒
            self.clock.advance(2.0)
            self.lifecycle.note_page_alive()
        self.tick(times=4, step=1.0)
        self.assertEqual(self.stops, [], "页面开着就不该关")
        self.assertEqual(self.lifecycle.page_flag(), 1)

    def test_a_running_task_never_closes_even_without_a_page(self):
        self.open_page()
        self.lifecycle.note_page_closed()
        self.task = "对话请求 1 个进行中"
        self.tick(times=10, step=3.0)
        self.assertEqual(self.stops, [], "有任务在跑就不能关，不管页面在不在")

    def test_it_closes_after_the_task_finishes(self):
        self.open_page()
        self.lifecycle.note_page_closed()
        self.task = "生图任务在跑或排队"
        self.tick(times=4, step=3.0)
        self.assertEqual(self.stops, [])
        self.task = ""                        # 任务结束 → 任务位自动转 0
        self.tick(times=4, step=1.0)
        self.assertEqual(len(self.stops), 1, self.stops)

    def test_a_page_that_was_never_seen_never_closes(self):
        """服务刚起来、浏览器还没打开页面时，页面位也是 0；不加这条网关会启动即自杀。"""
        self.tick(times=20, step=5.0)
        self.assertEqual(self.stops, [])
        self.assertFalse(self.lifecycle.flags()["page_ever_seen"])

    def test_the_switch_can_turn_the_whole_thing_off(self):
        self.enabled = False
        self.open_page()
        self.lifecycle.note_page_closed()
        self.tick(times=20, step=5.0)
        self.assertEqual(self.stops, [])
        self.assertFalse(self.lifecycle.should_close())

    def test_a_failing_stop_is_recorded_and_the_loop_survives(self):
        def broken():
            raise RuntimeError("收工失败")
        self.lifecycle.stopper = broken
        self.open_page()
        self.lifecycle.note_page_closed()
        self.tick(times=14, step=1.0)              # 越过窗口，才会真的尝试收工
        self.assertFalse(self.lifecycle.flags()["stopped"])


class FlagReportTests(LifecycleHarness):
    def test_the_report_shows_both_flags_and_the_tuning_knobs(self):
        report = self.lifecycle.flags()
        self.assertEqual(report["page"], 0)
        self.assertEqual(report["task"], 0)
        self.assertEqual(report["page_idle_seconds"], 10.0)
        self.assertEqual(report["heartbeat_seconds"], page_lifecycle.HEARTBEAT_SECONDS)
        self.assertTrue(report["enabled"])

    def test_should_close_follows_the_two_flags(self):
        self.assertFalse(self.lifecycle.should_close(), "没见过页面就不关")
        self.open_page()
        self.assertFalse(self.lifecycle.should_close(), "页面位是 1 就不关")
        self.lifecycle.note_page_closed()
        self.assertFalse(self.lifecycle.should_close(), "信标之后还有窗口，先不关")
        self.clock.advance(11.0)
        self.assertTrue(self.lifecycle.should_close(), "窗口过去且没有任务才关")
        self.task = "视频任务在跑"
        self.assertFalse(self.lifecycle.should_close(), "任务位变 1 就不关")


if __name__ == "__main__":
    unittest.main()
