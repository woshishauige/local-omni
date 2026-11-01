"""Startup regressions without touching service files, ports, or model processes."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


from contextlib import ExitStack
import io
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from app import mode_server as module
from app.qwen_config import DEFAULTS


class ModelStartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        # settings.json's context_size belongs to whichever model it was tuned for, so the
        # shipped profile's own window wins (expected_context). 49152 is here on purpose:
        # the backend must report the profile's 32768 and the validator must accept it.
        self.settings = DEFAULTS | {"context_size": 49152, "mtp_enabled": True}
        self.patches.enter_context(patch.object(module, "read_settings", return_value=self.settings))
        self.manager = module.ModeManager()
        self.now = 1000.0
        self.patches.enter_context(patch.object(module, "time", SimpleNamespace(monotonic=lambda: self.now)))

        async def advance(seconds):
            self.now += seconds

        self.sleep = self.patches.enter_context(patch.object(module.asyncio, "sleep", side_effect=advance))
        self.patches.enter_context(patch.object(self.manager, "ensure_comfy_idle", new=AsyncMock()))
        self.stop = self.patches.enter_context(patch.object(
            self.manager, "stop_backend", wraps=self.manager.stop_backend,
        ))
        self.patches.enter_context(patch.object(module.Path, "mkdir"))
        self.log_handles = []

        def open_log(*args, **kwargs):
            handle = io.BytesIO()
            self.log_handles.append(handle)
            return handle

        self.patches.enter_context(patch.object(module.Path, "open", side_effect=open_log))
        self.patches.enter_context(patch.object(module.Path, "write_text"))
        self.patches.enter_context(patch.object(module.Path, "unlink"))
        self.patches.enter_context(patch.object(module, "write_process_record"))
        self.patches.enter_context(patch.object(module.psutil, "Process", return_value=SimpleNamespace(
            create_time=lambda: 100.0,
        )))
        self.process = Mock(pid=12345)
        self.process.poll.return_value = None
        self.process.wait.return_value = 0
        self.popen = self.patches.enter_context(patch.object(module.subprocess, "Popen", return_value=self.process))
        self.patches.enter_context(patch.object(module, "LOGGER"))
        self.requests = []
        self.health_attempts = 0

    async def asyncTearDown(self):
        if self.manager.client is not None:
            await self.manager.client.aclose()
        for handle in self.log_handles:
            handle.close()

    def response(self, request):
        self.requests.append((request.method, request.url.path))
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/props":
            return httpx.Response(200, json={
                "default_generation_settings": {"n_ctx": 32768},
                "modalities": {"vision": False},
            })
        if request.url.path == "/completion":
            # 2026-09-23 换血：三元 Bonsai 2 没有草稿头，探针如实回报 none。
            return httpx.Response(200, json={"generation_settings": {"speculative.types": "none"}})
        return httpx.Response(200, json={"data": []})

    async def test_transient_connect_timeouts_do_not_kill_or_replace_loading_process(self):
        def upstream(request):
            if request.url.path == "/health":
                self.health_attempts += 1
                if self.health_attempts <= 3:
                    self.now += 2
                    raise httpx.ConnectTimeout("", request=request)
            return self.response(request)

        self.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        await self.manager.load("text")

        self.assertEqual(self.health_attempts, 4)
        self.popen.assert_called_once()
        self.stop.assert_awaited_once()
        self.process.terminate.assert_not_called()
        self.process.kill.assert_not_called()
        self.assertIs(self.manager.process, self.process)
        self.assertEqual(self.manager.state, "ready")
        self.assertIsNone(self.manager.error)
        self.assertFalse(self.manager.mtp_enabled)
        self.assertEqual(self.manager.revision, 1)
        self.assertEqual(self.manager.recovery_attempts, 0)
        self.assertEqual(self.requests.count(("POST", "/completion")), 1)
        self.assertEqual(self.manager.metadata["props"]["default_generation_settings"]["n_ctx"], 32768)

    async def test_permanent_connect_timeout_expires_at_total_deadline_and_reports_cause(self):
        def upstream(request):
            self.assertEqual(request.url.path, "/health")
            self.health_attempts += 1
            self.now += 2
            raise httpx.ConnectTimeout("", request=request)

        self.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        await self.manager.load("text")

        self.popen.assert_called_once()
        self.assertEqual(self.stop.await_count, 2)
        self.process.terminate.assert_called_once()
        self.process.kill.assert_not_called()
        self.assertIsNone(self.manager.process)
        self.assertEqual(self.manager.state, "error")
        self.assertGreaterEqual(self.now, 1180)
        self.assertLess(self.now, 1183)
        self.assertGreater(self.health_attempts, 1)
        self.assertLessEqual(self.health_attempts, 73)
        self.assertIn("180", self.manager.error)
        self.assertIn("ConnectTimeout", self.manager.error)
        self.assertIn(".err.log", self.manager.error)
        self.assertEqual(self.manager.recovery_attempts, 1)
        self.assertGreater(self.manager.retry_at, self.now)
        self.assertTrue(all(handle.closed for handle in self.log_handles))

    async def test_process_exit_reports_exit_code_and_specific_log(self):
        self.process.poll.return_value = 3221225781
        self.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(self.response))
        await self.manager.load("text")

        self.popen.assert_called_once()
        self.assertEqual(self.stop.await_count, 2)
        self.process.terminate.assert_not_called()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.manager.state, "error")
        self.assertIn("3221225781", self.manager.error)
        self.assertIn("-text.err.log", self.manager.error)
        self.assertIsNone(self.manager.process)
        self.assertTrue(all(handle.closed for handle in self.log_handles))

    async def test_exception_without_text_still_produces_useful_error(self):
        self.popen.side_effect = RuntimeError()
        self.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(self.response))
        await self.manager.load("text")

        self.assertEqual(self.manager.state, "error")
        self.assertIn("RuntimeError", self.manager.error)
        self.assertTrue(self.manager.error.strip())
        self.assertEqual(self.manager.recovery_attempts, 1)
        self.assertGreater(self.manager.retry_at, self.now)
        self.assertEqual(self.stop.await_count, 2)
        self.assertEqual(self.requests, [])
        self.assertTrue(all(handle.closed for handle in self.log_handles))


if __name__ == "__main__":
    unittest.main()
