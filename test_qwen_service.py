"""Process lifecycle tests use isolated paths and CPU-only dummy children."""

from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, call, patch
import urllib.error

import psutil

import qwen_service as service


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="qwen-service-")))
        for name, value in {
            "ROOT": self.root, "SCRIPT": self.root / "qwen_service.py",
            "MANAGER": self.root / "mode_server.py", "STOP": self.root / "service.stop",
            "SERVICE_RECORD": self.root / "supervisor.process.json",
            "MANAGER_RECORD": self.root / "supervisor-manager.process.json",
            "SERVICE_STATE": self.root / "supervisor.status.json",
        }.items():
            self.stack.enter_context(patch.object(service, name, value))

    def test_lock_excludes_parallel_starts_and_is_reusable(self):
        path = self.root / "instance.lock"
        with service.file_lock(path):
            with self.assertRaises(TimeoutError):
                with service.file_lock(path):
                    self.fail("Second lock must not succeed")
        with service.file_lock(path):
            pass

    def test_existing_lock_uses_open_existing_and_preserves_contents(self):
        path = self.root / "existing.lock"
        path.write_bytes(b"persistent-lock")
        proxy = Mock()
        proxy.name = path.name
        proxy.open.side_effect = path.open
        with service.file_lock(proxy):
            pass
        proxy.open.assert_called_once_with("r+b")
        self.assertEqual(path.read_bytes(), b"persistent-lock")

    def test_lock_creation_race_reopens_same_file(self):
        path = self.root / "raced.lock"
        path.write_bytes(b"\0")
        proxy = Mock()
        proxy.name = path.name
        proxy.open.side_effect = [FileNotFoundError(), FileExistsError(), path.open("r+b")]
        with service.file_lock(proxy):
            pass
        self.assertEqual(proxy.open.call_args_list, [call("r+b"), call("x+b"), call("r+b")])
        self.assertEqual(path.read_bytes(), b"\0")

    def test_permission_denial_does_not_try_create_or_replace_lock(self):
        path = Mock()
        path.open.side_effect = PermissionError("Permission denied: service-control.lock")
        with self.assertRaisesRegex(PermissionError, "service-control.lock"):
            with service.file_lock(path):
                self.fail("A denied lock must not be acquired")
        path.open.assert_called_once_with("r+b")

    def test_lock_initialization_failure_closes_and_releases_handle(self):
        path = self.root / "empty.lock"
        path.touch()
        handle = path.open("r+b")
        tracked = Mock(wraps=handle)
        tracked.write.side_effect = OSError("initialization failed")
        proxy = Mock()
        proxy.name = path.name
        proxy.open.return_value = tracked
        with self.assertRaisesRegex(OSError, "initialization failed"):
            with service.file_lock(proxy):
                self.fail("Failed initialization must not enter the critical section")
        self.assertTrue(handle.closed)
        tracked.close.assert_called_once_with()
        with service.file_lock(path):
            self.assertEqual(path.stat().st_size, 1)

    def test_record_rejects_pid_reuse_wrong_executable_and_other_script(self):
        process = Mock(pid=123)
        process.create_time.return_value = 1000.25
        process.exe.return_value = str(service.PYTHON)
        process.cmdline.return_value = [str(service.PYTHON), "-X", "utf8", str(service.MANAGER)]
        process.is_running.return_value = True
        record = service.record_process(process, service.MANAGER)
        service.write_json(service.MANAGER_RECORD, record)
        with patch.object(service.psutil, "Process", return_value=process):
            self.assertIs(service.owned_process(service.MANAGER_RECORD, service.MANAGER), process)
            process.create_time.return_value += 1
            self.assertIsNone(service.owned_process(service.MANAGER_RECORD, service.MANAGER))
            process.create_time.return_value -= 1
            process.exe.return_value = str(self.root / "other-python.exe")
            self.assertIsNone(service.owned_process(service.MANAGER_RECORD, service.MANAGER))
            process.exe.return_value = str(service.PYTHON)
            process.cmdline.return_value = [str(service.PYTHON), "-c", "pass", str(service.MANAGER)]
            self.assertIsNone(service.owned_process(service.MANAGER_RECORD, service.MANAGER))

    def test_start_recovers_error_and_returns_before_model_ready(self):
        service.STOP.write_text("stopped")
        with patch.object(service, "owned_process", return_value=Mock()), \
                patch.object(service, "spawn") as spawn, \
                patch.object(service, "gateway_status", return_value={"state": "error"}), \
                patch.object(service, "http_json", return_value={"state": "waiting"}) as request:
            self.assertEqual(service.start()["state"], "waiting")
            request.assert_called_once_with("/local/recover", method="POST")
            spawn.assert_not_called()
        self.assertFalse(service.STOP.exists())

    def test_start_retries_transient_gateway_500(self):
        responses = [
            {"service": "qwen-local-modes", "state": "error"},
            {"service": "qwen-local-modes", "state": "ready"},
        ]
        recovery_error = urllib.error.HTTPError(service.URL, 500, "warming up", {}, None)
        with patch.object(service, "owned_process", return_value=Mock()), \
                patch.object(service, "spawn"), \
                patch.object(service, "gateway_status", side_effect=responses), \
                patch.object(service, "http_json", side_effect=[recovery_error]):
            result = service.start()
        self.assertEqual(result["state"], "ready")

    def test_gateway_status_uses_short_probe_timeout(self):
        with patch.object(service, "http_json", return_value={"service": "qwen-local-modes", "state": "ready"}) as request:
            self.assertEqual(service.gateway_status(timeout=service.HEARTBEAT_TIMEOUT)["state"], "ready")
        request.assert_called_once_with("/local/status", timeout=service.HEARTBEAT_TIMEOUT)

    def test_start_retries_disconnected_and_malformed_recovery(self):
        for error in (urllib.error.URLError("connection reset"), ValueError("bad JSON"), TimeoutError()):
            with self.subTest(error=type(error).__name__), \
                    patch.object(service, "owned_process", return_value=Mock()), \
                    patch.object(service, "gateway_status", side_effect=[{"state": "error"}, {"state": "ready"}]), \
                    patch.object(service, "http_json", side_effect=error), \
                    patch.object(service.time, "sleep"):
                self.assertEqual(service.start()["state"], "ready")

    def test_start_does_not_report_error_or_invalid_recovery_as_success(self):
        for response in ({"state": "error"}, [], None):
            with self.subTest(response=response), \
                    patch.object(service, "owned_process", return_value=Mock()), \
                    patch.object(service, "gateway_status", side_effect=[{"state": "error"}, {"state": "ready"}]), \
                    patch.object(service, "http_json", return_value=response), \
                    patch.object(service.time, "sleep"):
                self.assertEqual(service.start()["state"], "ready")

    def test_start_keeps_terminal_recovery_rejections_visible(self):
        with patch.object(service, "owned_process", return_value=Mock()), \
                patch.object(service, "gateway_status", return_value={"state": "error"}), \
                patch.object(service, "http_json", side_effect=urllib.error.HTTPError(service.URL, 403, "forbidden", {}, None)):
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                service.start()

    def test_supervisor_identity_is_published_before_next_start(self):
        process = Mock(pid=8123)
        process.create_time.return_value = 1000.25
        with patch.object(service, "owned_process", return_value=None), \
                patch.object(service, "spawn", return_value=Mock(pid=8123)) as spawn, \
                patch.object(service.psutil, "Process", return_value=process):
            service.ensure_supervisor()
        spawn.assert_called_once_with(service.SCRIPT, "supervisor", detached=True)
        self.assertEqual(service.read_json(service.SERVICE_RECORD)["pid"], 8123)

    def test_a_deliberate_backend_switch_is_not_a_wedged_loop(self):
        """真机踩到：离开语音模式时按窗口换权重，探活答不上来，监督进程把网关杀在换档中间。

        换档窗口由 `qwen_config.mark_switching()` 写在文件里（带截止时间），窗口内不计数。
        """
        import qwen_config

        qwen_config.clear_switching()
        self.addCleanup(qwen_config.clear_switching)
        self.assertTrue(service.heartbeat_should_count(), "平时失败要计数")
        qwen_config.mark_switching(60)
        self.assertFalse(service.heartbeat_should_count(), "换档窗口内不计数")
        qwen_config.SWITCH_MARKER.write_text('{"until": 1}', encoding="ascii")
        self.assertTrue(service.heartbeat_should_count(), "过期标记照常计数（真卡死仍会被重启）")
        qwen_config.clear_switching()
        self.assertTrue(service.heartbeat_should_count(), "换完了也要恢复计数")

    def test_the_switch_marker_brackets_a_backend_load(self):
        """`Manager.load()` 必须在换档前挂标记、换完（含失败/取消）清掉。"""
        import inspect

        import mode_server

        source = inspect.getsource(mode_server.ModeManager.load)
        self.assertIn("mark_switching()", source.split("previous_backend")[0],
                      "标记要在动后端之前挂上")
        self.assertIn("clear_switching()", source.split("finally:")[-1],
                      "清标记要放在 finally 里，失败和取消也要清")

    def test_a_supervisor_from_another_scope_is_replaced(self):
        """口径不同的监督进程必须换掉，否则"这次要共享"永远不生效（真机踩过）。"""
        old = Mock(pid=33736)
        with patch.object(service.os.environ, "get", lambda key, default=None:
                          "share" if key == "QWEN_SHARE_SCOPE" else default), \
                patch.object(service, "published_scope", return_value="local"), \
                patch.object(service, "owned_process", side_effect=[old, None]), \
                patch.object(service, "terminate_owned") as terminate, \
                patch.object(service, "spawn", return_value=Mock(pid=909)) as spawn, \
                patch.object(service.psutil, "Process", return_value=Mock(pid=909, create_time=lambda: 1.0)):
            service.ensure_supervisor()
        terminate.assert_called_once()
        spawn.assert_called_once_with(service.SCRIPT, "supervisor", detached=True)

    def test_a_scope_change_that_cannot_be_enforced_is_reported_loudly(self):
        """换不掉（例如那个监督进程是管理员起的）时，要报错并给出命令，不能静默降级。"""
        old = Mock(pid=33736)
        with patch.object(service.os.environ, "get", lambda key, default=None:
                          "share" if key == "QWEN_SHARE_SCOPE" else default), \
                patch.object(service, "published_scope", return_value="local"), \
                patch.object(service, "owned_process", return_value=old), \
                patch.object(service, "terminate_owned",
                             side_effect=psutil.AccessDenied(33736)), \
                patch.object(service, "spawn") as spawn:
            with self.assertRaisesRegex(RuntimeError, "Stop-Process -Id 33736"):
                service.ensure_supervisor()
        spawn.assert_not_called()

    def test_the_scope_is_written_into_the_supervisor_record(self):
        process = Mock(pid=8123)
        process.create_time.return_value = 1000.25
        with patch.object(service.os.environ, "get", lambda key, default=None:
                          "share" if key == "QWEN_SHARE_SCOPE" else default), \
                patch.object(service, "published_scope", return_value=""), \
                patch.object(service, "owned_process", return_value=None), \
                patch.object(service, "spawn", return_value=Mock(pid=8123)), \
                patch.object(service.psutil, "Process", return_value=process):
            service.ensure_supervisor()
        self.assertEqual(service.read_json(service.SERVICE_RECORD)["share_scope"], "share")

    def test_the_supervisor_republishes_the_scope_in_its_own_record(self):
        """监督进程写自己的记录时也必须带上口径：少了它，stop/start 会把健康的口径读成"未知"。

        真机踩到两次：`stop`/`start` 报"监督进程是以「未知」口径启动的…它无法被结束"，而那个服务
        其实共享得好好的。根因就是启动方写下去的 `share_scope` 被监督进程自己那次写覆盖掉了。
        """
        process = Mock(pid=4321)
        process.create_time.return_value = 2000.5
        service.publish_identity(process, "share")
        record = service.read_json(service.SERVICE_RECORD)
        self.assertEqual(record["share_scope"], "share")
        self.assertEqual(record["pid"], 4321)
        # 没给口径时保持老格式（不塞空字段），否则老脚本起的服务会被误判成"明确说了未知"。
        service.publish_identity(process, "")
        self.assertNotIn("share_scope", service.read_json(service.SERVICE_RECORD))

    def test_an_unknown_scope_with_an_explicit_request_is_replaced_too(self):
        """记录还是老格式（没有口径字段）时不去猜：调用方说了要什么，就按它换掉。"""
        with patch.object(service.os.environ, "get", lambda key, default=None:
                          "share" if key == "QWEN_SHARE_SCOPE" else default), \
                patch.object(service, "published_scope", return_value=""), \
                patch.object(service, "owned_process", side_effect=[Mock(pid=1), None]), \
                patch.object(service, "terminate_owned") as terminate, \
                patch.object(service, "spawn", return_value=Mock(pid=2)) as spawn, \
                patch.object(service.psutil, "Process", return_value=Mock(pid=2, create_time=lambda: 1.0)):
            service.ensure_supervisor()
        terminate.assert_called_once()
        spawn.assert_called_once()

    def test_no_requested_scope_leaves_the_supervisor_alone(self):
        """调用方没给口径（老脚本、没有环境变量）时不许乱换监督进程。"""
        with patch.object(service.os.environ, "get", return_value=None), \
                patch.object(service, "published_scope", return_value="local"), \
                patch.object(service, "owned_process", return_value=Mock(pid=1)), \
                patch.object(service, "terminate_owned") as terminate, \
                patch.object(service, "spawn") as spawn:
            service.ensure_supervisor()
        terminate.assert_not_called()
        spawn.assert_not_called()

    def test_adopt_uses_gateway_identity_without_system_socket_permissions(self):
        process = Mock(pid=456)
        process.create_time.return_value = 1000.25
        with patch.object(service, "owned_process", side_effect=[None, process]), \
                patch.object(service.psutil, "net_connections", side_effect=psutil.AccessDenied()) as connections:
            self.assertIs(service.adopt_manager(), process)
        connections.assert_not_called()
        self.assertEqual(service.read_json(service.MANAGER_RECORD)["pid"], 456)

    def test_non_object_process_record_is_ignored(self):
        service.SERVICE_RECORD.write_text('[1, 2, 3]', encoding="utf-8")
        self.assertIsNone(service.owned_process(service.SERVICE_RECORD, service.SCRIPT))

    def test_lost_manager_record_can_be_repaired_without_trusting_reused_pid(self):
        original = Mock(pid=456)
        original.create_time.return_value = 1000.25
        current = Mock(pid=456)
        current.create_time.return_value = 1000.25
        with patch.object(service.psutil, "Process", return_value=current), \
                patch.object(service, "process_alive", return_value=True), \
                patch.object(service, "matches_script", return_value=True):
            service.restore_manager_record(original)
            self.assertEqual(service.read_json(service.MANAGER_RECORD)["pid"], 456)
            current.create_time.return_value = 2000.25
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                service.restore_manager_record(original)
            self.assertEqual(service.read_json(service.MANAGER_RECORD)["created_unix_ms"], 1000250)

    def test_stopping_sleeping_backend_does_not_wake_it_with_slots(self):
        with patch.object(service, "http_json", return_value={"is_sleeping": True}) as request:
            self.assertTrue(service.backend_idle())
        request.assert_called_once_with("/props", base="http://127.0.0.1:8082", timeout=5)

    def test_a_slow_slots_probe_is_retried_before_calling_the_backend_busy(self):
        """一个闲置但 /slots 响应慢的后端不能被判成"忙"。

        Measured on this machine: the first `/slots` after a long idle period timed out at 5 s
        and answered `is_processing=false` a moment later, while the process had burned 0.02 s of
        CPU. Judging it busy refused every stop exactly when a stop was the only way out.
        """
        responses = [{"n_ctx": 131072}, OSError("timed out"), [{"is_processing": False}]]

        def fake(path, **options):
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        with patch.object(service, "http_json", side_effect=fake):
            self.assertTrue(service.backend_idle())

    def test_a_slow_probe_that_never_answers_is_still_not_idle(self):
        with patch.object(service, "http_json",
                          side_effect=[{"n_ctx": 131072}, OSError("timed out"), OSError("timed out")]):
            self.assertFalse(service.backend_idle())

    def test_a_processing_slot_is_not_idle(self):
        with patch.object(service, "http_json",
                          side_effect=[{"n_ctx": 131072}, [{"is_processing": True}]]):
            self.assertFalse(service.backend_idle())

    def test_stop_busy_does_not_disable_restart_or_stop_anything(self):
        with patch.object(service, "gateway_status", return_value={"active_requests": 1}), \
                patch.object(service, "http_json") as request, \
                patch.object(service, "terminate_owned") as terminate:
            with self.assertRaisesRegex(RuntimeError, "generating"):
                service.stop()
            request.assert_not_called()
            terminate.assert_not_called()
        self.assertFalse(service.STOP.exists())

    def test_shutdown_race_restores_restart_marker(self):
        rejection = urllib.error.HTTPError(service.URL, 409, "busy", {}, None)
        with patch.object(service, "gateway_status", return_value={"active_requests": 0}), \
                patch.object(service, "adopt_manager", return_value=Mock()), \
                patch.object(service, "http_json", side_effect=rejection):
            with self.assertRaises(urllib.error.HTTPError):
                service.stop()
        self.assertFalse(service.STOP.exists())

    def test_orphan_busy_is_not_terminated(self):
        with patch.object(service, "gateway_status", return_value=None), \
                patch.object(service, "adopt_manager", return_value=None), \
                patch.object(service, "owned_backend", return_value=Mock()), \
                patch.object(service, "backend_idle", return_value=False), \
                patch.object(service, "terminate_owned") as terminate:
            with self.assertRaisesRegex(RuntimeError, "busy"):
                service.stop()
            terminate.assert_not_called()
        self.assertFalse(service.STOP.exists())

    def test_unknown_gateway_is_not_sent_shutdown(self):
        with patch.object(service, "gateway_status", return_value={"active_requests": 0}), \
                patch.object(service, "adopt_manager", return_value=None), \
                patch.object(service, "http_json") as request:
            with self.assertRaisesRegex(RuntimeError, "verified"):
                service.stop()
            request.assert_not_called()

    def test_successful_stop_disables_respawn(self):
        process = Mock()
        with patch.object(service, "gateway_status", return_value={"active_requests": 0}), \
                patch.object(service, "adopt_manager", return_value=process), \
                patch.object(service, "http_json") as request, \
                patch.object(service, "owned_backend", return_value=None), \
                patch.object(service, "owned_process", return_value=None):
            self.assertEqual(service.stop()["state"], "stopped")
            request.assert_called_once_with("/local/shutdown", method="POST", timeout=5)
            process.wait.assert_called_once_with(timeout=35)
        self.assertTrue(service.STOP.exists())

    def test_backoff_is_bounded(self):
        self.assertEqual([service.backoff(i) for i in range(1, 9)], [2, 4, 8, 16, 32, 60, 60, 60])

    def test_exit_during_liveness_probe_is_not_watchdog_failure(self):
        process = Mock()
        process.is_running.return_value = True
        process.status.side_effect = psutil.NoSuchProcess(123)
        self.assertFalse(service.process_alive(process))


class SupervisorIntegrationTests(unittest.TestCase):
    def test_dummy_manager_crash_restarts_and_stop_prevents_respawn(self):
        # The worker patches all paths before supervising; it never imports or starts the GPU backend.
        with tempfile.TemporaryDirectory(prefix="qwen-watchdog-") as temporary:
            root = Path(temporary)
            (root / "mode_server.py").write_text(
                "import os,time\nfrom pathlib import Path\n"
                "root=Path(__file__).parent\n"
                "with (root/'starts').open('a') as f:f.write(str(os.getpid())+'\\n')\n"
                "time.sleep(0.3)\nraise SystemExit(7)\n", encoding="utf-8")
            worker = (
                "import sys;from pathlib import Path;"
                f"sys.path.insert(0,{str(service.ROOT)!r});import qwen_service as s;"
                f"s.ROOT=Path({str(root)!r});s.MANAGER=s.ROOT/'mode_server.py';"
                "s.SCRIPT=s.ROOT/'worker.py';s.STOP=s.ROOT/'service.stop';"
                "s.SERVICE_RECORD=s.ROOT/'supervisor.process.json';"
                "s.MANAGER_RECORD=s.ROOT/'supervisor-manager.process.json';"
                "s.SERVICE_STATE=s.ROOT/'supervisor.status.json';"
                "s.adopt_manager=lambda:None;s.port_open=lambda port:False;s.supervise()"
            )
            child = subprocess.Popen([str(service.PYTHON), "-X", "utf8", "-c", worker],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 12
                starts = []
                while time.monotonic() < deadline:
                    if (root / "starts").exists():
                        starts = (root / "starts").read_text().splitlines()
                    if len(starts) >= 2:
                        break
                    if child.poll() is not None:
                        self.fail(child.communicate()[1].decode())
                    time.sleep(0.1)
                self.assertEqual(len(starts), 2, "Crashed manager was not restarted")
                self.assertNotEqual(starts[0], starts[1])
                (root / "service.stop").write_text("test explicit stop")
                stdout, stderr = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 0, stderr.decode())
                self.assertEqual(len((root / "starts").read_text().splitlines()), 2)
                self.assertIn("exited (7)", (root / "logs/supervisor.log").read_text())
                self.assertFalse((root / "supervisor.process.json").exists())
            finally:
                if child.poll() is None:
                    (root / "service.stop").write_text("test cleanup")
                    child.communicate(timeout=5)


class WedgedGatewayTests(unittest.TestCase):
    """启动时必须自己收拾"活着但不监听"的网关，否则启动器只会等到超时。

    真机踩到两次：网关接到 /local/shutdown 之后先停止监听、再去等模型空下来，然后卡在那里
    （既不应答也不退出）。它不占端口，所以按端口认领的逻辑看不见它，而它会让每一次启动都等到
    超时——所有者看到的就是"打不开"。
    """

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="qwen-wedged-")))
        for name, value in {
            "ROOT": self.root, "STOP": self.root / "service.stop",
            "MANAGER_RECORD": self.root / "supervisor-manager.process.json",
        }.items():
            self.stack.enter_context(patch.object(service, name, value))

    def test_a_gateway_alive_but_not_listening_is_ended_before_the_wait(self):
        fake = Mock(pid=4242)
        with patch.object(service, "owned_process", return_value=fake), \
                patch.object(service, "port_open", return_value=False), \
                patch.object(service, "terminate_owned") as ended, \
                patch.object(service, "ensure_supervisor"), \
                patch.object(service, "gateway_status", return_value={"state": "ready"}), \
                patch.object(service, "ensure_session"):
            result = service.start()
        ended.assert_called_once_with(fake, service.MANAGER_RECORD, service.MANAGER)
        self.assertEqual(result["state"], "ready")

    def test_a_gateway_that_is_listening_is_left_alone(self):
        fake = Mock(pid=4243)
        with patch.object(service, "owned_process", return_value=fake), \
                patch.object(service, "port_open", return_value=True), \
                patch.object(service, "terminate_owned") as ended, \
                patch.object(service, "ensure_supervisor"), \
                patch.object(service, "gateway_status", return_value={"state": "ready"}), \
                patch.object(service, "ensure_session"):
            service.start()
        ended.assert_not_called()


if __name__ == "__main__":
    unittest.main()
