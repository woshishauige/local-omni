"""两个标识位的网关侧测试（第 2 档：真实 ASGI 路由，零显存、不排任何渲染）。

`test_page_lifecycle.py` 钉的是状态机；这里钉的是接线与任务位的口径：
* 心跳把页面位顶成 1、信标立刻压回 0，状态路由只读；
* 信标/心跳都只认机主，访客关自己的标签页不影响机主；
* **普通轮询与探活不算"页面在用"**（监督进程每 10 秒探一次，否则永远不会收工）；
* 任务位把每一类活都点名列出来：对话、切模型、生图（含排队）、视频、补帧；
* 收工三步的顺序（ComfyUI → 隧道 → 服务本身）与失败兜底。
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import mode_server as module
import page_lifecycle
from test_access_gate import GateCase


class PageFlagRouteTests(GateCase):
    def setUp(self):
        super().setUp()
        self.previous = module.page_watch
        self.watch = page_lifecycle.PageLifecycle(
            task_check=lambda: "", stopper=lambda: "stopped-in-test", enabled=lambda: True,
            page_idle=10.0)
        module.page_watch = self.watch
        self.addCleanup(setattr, module, "page_watch", self.previous)

    async def test_the_heartbeat_turns_the_page_flag_on(self):
        payload = (await self.post("/local/lifecycle/page-alive", json={})).json()
        self.assertEqual(payload["page"], 1)
        self.assertEqual(payload["task"], 0)

    async def test_the_close_beacon_only_starts_the_countdown(self):
        """`pagehide` 在刷新和站内跳转时同样触发，所以这条路不能立刻报"没有页面"。

        原来它断言 `page == 0`：那等于"刷新一次就把整个栈收工"，网关退出后页面反而连不上。
        """
        await self.post("/local/lifecycle/page-alive", json={})
        response = await self.post("/local/lifecycle/page-closed", json={})
        self.assertEqual(response.status_code, 202, response.text)
        payload = response.json()
        self.assertEqual(payload["page"], 1, "信标只是把窗口从此刻重新开始")
        self.assertEqual(payload["closed_seconds_ago"], 0.0, "信标时刻要留痕，供诊断")
        # 窗口过去仍没有心跳，才算真的没有页面。
        beat = self.watch.heartbeat_at
        self.watch.clock = lambda: beat + self.watch.page_idle + 1.0
        self.assertEqual(self.watch.page_flag(), 0)

    async def test_the_status_route_is_read_only(self):
        await self.post("/local/lifecycle/page-alive", json={})
        payload = (await self.get("/local/lifecycle/status")).json()
        self.assertEqual(payload["page"], 1, "看一眼状态不该改变页面位")
        self.assertIn("guests_recent_seconds", payload)

    async def test_ordinary_traffic_does_not_raise_the_page_flag(self):
        """轮询、探活都不算页面在用——否则监督进程的 10 秒探活会让收工永远不发生。"""
        await self.get("/local/status")
        await self.get("/local/context/config")
        payload = (await self.get("/local/lifecycle/status")).json()
        self.assertEqual(payload["page"], 0)

    async def test_a_guest_heartbeat_is_refused(self):
        self.use_peer("203.0.113.9")
        token = self.session(label="访客")
        response = await self.post("/local/lifecycle/page-alive",
                                   cookies={module.ACCESS_COOKIE: token}, json={})
        self.assertEqual(response.status_code, 403, response.text)

    async def test_a_guest_beacon_is_refused(self):
        self.use_peer("203.0.113.9")
        token = self.session(label="访客")
        response = await self.post("/local/lifecycle/page-closed",
                                   cookies={module.ACCESS_COOKIE: token}, json={})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.watch.page_flag(), 0)


class StubMonitor:
    def __init__(self, busy=False, pending=None):
        self.busy = busy
        self.pending = pending


class TaskFlagTests(unittest.TestCase):
    """任务位：页面上有什么在跑就报出它的名字，全空返回空串。"""

    def setUp(self):
        self.previous_state = module.manager.state
        self.previous_requests = module.manager.active_requests
        self.previous_image_jobs = module.image_jobs
        self.addCleanup(setattr, module.manager, "state", self.previous_state)
        self.addCleanup(setattr, module.manager, "active_requests", self.previous_requests)
        self.addCleanup(setattr, module, "image_jobs", self.previous_image_jobs)
        module.manager.active_requests = 0
        module.manager.state = "ready"
        module.image_jobs = StubMonitor()
        for name in ("monitor", "interpolate_monitor"):
            patcher = patch.object(module.video_routes, name, lambda: None)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_nothing_running_is_an_empty_label(self):
        self.assertEqual(module.task_running_label(), "")

    def test_a_chat_request_counts(self):
        module.manager.active_requests = 2
        self.assertIn("对话", module.task_running_label())

    def test_a_model_switch_counts(self):
        module.manager.state = "switching"
        self.assertIn("switching", module.task_running_label())

    def test_a_pending_image_job_counts(self):
        module.image_jobs.pending = {"id": "img-test"}
        self.assertIn("生图", module.task_running_label())

    def test_a_running_image_job_counts(self):
        module.image_jobs.busy = True
        self.assertIn("生图", module.task_running_label())

    def test_a_running_video_job_counts(self):
        with patch.object(module.video_routes, "monitor", lambda: StubMonitor(busy=True)):
            self.assertIn("视频", module.task_running_label())

    def test_a_running_interpolation_counts(self):
        with patch.object(module.video_routes, "interpolate_monitor",
                          lambda: StubMonitor(busy=True)):
            self.assertIn("补帧", module.task_running_label())

    def test_the_task_flag_reads_the_label_live(self):
        """任务一结束，任务位必须自己回到 0（不缓存）。"""
        watch = page_lifecycle.PageLifecycle(task_check=module.task_running_label,
                                             stopper=lambda: None, enabled=lambda: True)
        module.image_jobs.busy = True
        self.assertEqual(watch.task_flag(), 1)
        module.image_jobs.busy = False
        self.assertEqual(watch.task_flag(), 0)


class TaskFlagWiringTests(unittest.TestCase):
    """不 patch 任何东西跑一遍真任务位：常量/名字写错时这里就会炸。"""

    def setUp(self):
        self.previous_state = module.manager.state
        self.previous_requests = module.manager.active_requests
        self.addCleanup(setattr, module.manager, "state", self.previous_state)
        self.addCleanup(setattr, module.manager, "active_requests", self.previous_requests)
        module.manager.active_requests = 0
        module.manager.state = "ready"

    def test_the_guest_scan_still_runs_for_status(self):
        sessions = {token: dict(entry) for token, entry in module.share_store.sessions.items()}
        sessions["test-guest-session"] = {"kind": "device", "label": "测试访客",
                                          "last_seen": __import__("time").time()}
        with patch.object(module.share_store, "sessions", sessions):
            self.assertIsInstance(module.recent_guest_seconds(), float)

    def test_the_whole_verdict_runs_unpatched(self):
        self.assertIsInstance(module.task_running_label(), str)


class ShutdownWiringTests(unittest.IsolatedAsyncioTestCase):
    """收工三步：ComfyUI → 隧道 → 服务本身，顺序不能反（反了监督进程会立刻把它拉回来）。"""

    async def test_the_stack_is_stopped_in_order(self):
        calls = []

        class StubSupervisor:
            def stop(self):
                calls.append("comfy")
                return True

        stub_image = SimpleNamespace(supervisor=StubSupervisor())
        stub_server = SimpleNamespace(should_exit=False)
        with patch.object(module, "image_jobs", stub_image), \
                patch.object(module, "server", stub_server), \
                patch.object(module, "stop_tunnel",
                             lambda: (calls.append("tunnel"), True)[1]):
            outcome = await module.shutdown_after_page_close()
        self.assertEqual(calls, ["comfy", "tunnel"])
        self.assertEqual(outcome["service"], "exiting")
        self.assertTrue(stub_server.should_exit)
        self.assertTrue((module.ROOT / "service.stop").exists(),
                        "停止标记必须在退出之前写好，否则监督进程会把服务拉回来")
        (module.ROOT / "service.stop").unlink(missing_ok=True)

    async def test_a_failing_comfy_stop_does_not_block_the_exit(self):
        class BrokenSupervisor:
            def stop(self):
                raise RuntimeError("ComfyUI 拒绝退出")

        stub_image = SimpleNamespace(supervisor=BrokenSupervisor())
        stub_server = SimpleNamespace(should_exit=False)
        with patch.object(module, "image_jobs", stub_image), \
                patch.object(module, "server", stub_server), \
                patch.object(module, "stop_tunnel", lambda: False):
            outcome = await module.shutdown_after_page_close()
        self.assertIn("failed", str(outcome["comfy"]))
        self.assertTrue(stub_server.should_exit)
        (module.ROOT / "service.stop").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
