"""Usage accounting integration: no real model, ports, or production database."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx

from app import mode_server as module
from app.usage.usage_store import UsageStore
from app.usage.usage_tracking import UsageMiddleware


class UsageApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="qwen-usage-api-")))
        self.store = UsageStore(self.root / "usage.sqlite3")
        self.stack.enter_context(patch.object(module, "usage_store", self.store))
        manager = module.ModeManager()
        manager.state = "ready"
        self.stack.enter_context(patch.object(module, "manager", manager))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://127.0.0.1:8081")

    async def asyncTearDown(self):
        if module.manager.client:
            await module.manager.client.aclose()
        await self.client.aclose()
        self.stack.close()

    def backend(self, callback):
        module.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(callback))

    async def rows(self):
        response = await self.client.get("/local/usage")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_nonstream_usage_persists_without_message_content(self):
        self.backend(lambda request: httpx.Response(200, json={
            "choices": [{"message": {"content": "private output"}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30,
                      "prompt_tokens_details": {"cached_tokens": 5}},
        }))
        response = await self.client.post("/v1/chat/completions", json={
            "model": "local-model", "messages": [{"role": "user", "content": "private prompt"}],
            "tools": [{"type": "function", "function": {"name": "probe", "parameters": {"properties": {"type": {"type": "string"}}}}}],
        }, headers={"User-Agent": "Mozilla/5.0"})
        self.assertEqual(response.status_code, 200)
        data = await self.rows()
        row = data["rows"][0]
        self.assertEqual(data["total"], 1)
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["input_tokens"], 20)
        self.assertEqual(row["output_tokens"], 10)
        self.assertEqual(row["cache_read_tokens"], 5)
        self.assertIsNone(row["cache_write_tokens"])
        self.assertIsNone(row["ttft_ms"])
        self.assertTrue(row["agent"])
        self.assertEqual(row["source"], "Web")
        self.assertEqual(data["summary"]["total_tokens"], 30)
        self.assertIsNone(data["summary"]["cache_write_tokens"])
        self.assertNotIn("private", json.dumps(data))
        self.assertEqual(module.manager.active_requests, 0)
        self.assertNotIn(b"private", (self.root / "usage.sqlite3").read_bytes())

    async def test_stream_usage_option_is_added_and_wire_bytes_preserved(self):
        observed = []
        wire = ('data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"你好"}}]}\n\n'
                'data: {"choices":[],"usage":{"prompt_tokens":15,"completion_tokens":2,"total_tokens":17}}\n\n'
                'data: [DONE]\n\n').encode()

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for start in range(0, len(wire), 7):
                    yield wire[start:start + 7]

        def upstream(request):
            observed.append(json.loads(request.content))
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())
        self.backend(upstream)
        payload = {"model": "local-model", "messages": [], "stream": True,
                   "stream_options": {"include_usage": False, "include_obfuscation": False}}
        response = await self.client.post("/v1/chat/completions", json=payload)
        self.assertEqual(response.content, wire)
        self.assertTrue(observed[0]["stream_options"]["include_usage"])
        self.assertFalse(observed[0]["stream_options"]["include_obfuscation"])
        row = (await self.rows())["rows"][0]
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (15, 2))
        self.assertEqual(row["status"], "success")
        self.assertIsNotNone(row["ttft_ms"])

    async def test_failed_and_rejected_requests_record_errors_once(self):
        for payload, status in ((b"[", 400), (b"[]", 400)):
            response = await self.client.post("/v1/chat/completions", content=payload, headers={"Content-Type": "application/json"})
            self.assertEqual(response.status_code, status)
        module.manager.state = "starting"
        self.assertEqual((await self.client.post("/completion", json={"prompt": "x"})).status_code, 503)
        module.manager.state = "ready"
        self.backend(lambda request: httpx.Response(500, json={"error": {"message": "private prompt text"}}))
        self.assertEqual((await self.client.post("/completion", json={"prompt": "x"})).status_code, 500)
        data = await self.rows()
        self.assertEqual((data["total"], data["summary"]["error"]), (4, 4))
        self.assertTrue(all(row["input_tokens"] is None for row in data["rows"]))
        self.assertIsNone(data["summary"]["total_tokens"])
        self.assertIsNone(data["summary"]["tpm"])
        self.assertNotIn("private", json.dumps(data))

    async def test_non_generation_calls_are_not_double_counted(self):
        self.backend(lambda request: httpx.Response(200, json={"ok": True}))
        await self.client.get("/local/status")
        await self.client.get("/props")
        await self.client.post("/tools", json={"name": "get_datetime"})
        await self.client.post("/v1/streams/lookup", json={})
        await self.client.post("/v1/chat/completions/control", json={})
        self.assertEqual((await self.rows())["total"], 0)

    async def test_preferences_filters_and_csv_endpoints(self):
        saved = await self.client.post("/local/usage/preferences", json={"input_per_million": 10, "output_per_million": 50})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.backend(lambda request: httpx.Response(200, json={"usage": {"prompt_tokens": 100, "completion_tokens": 10}}))
        await self.client.post("/v1/chat/completions", json={"model": "example", "messages": []})
        data = (await self.client.get("/local/usage?model=example&status=success&timezone_offset_minutes=480")).json()
        self.assertEqual(data["total"], 1)
        self.assertAlmostEqual(data["summary"]["cost"], 0.0015)
        self.assertEqual((await self.client.get("/local/usage?model=missing")).json()["total"], 0)
        export = await self.client.get("/local/usage/export?model=example")
        self.assertEqual(export.status_code, 200)
        self.assertIn("attachment", export.headers["content-disposition"])
        self.assertIn("example", export.text)
        self.assertEqual((await self.client.get("/local/usage?page_size=10000")).status_code, 400)
        self.assertEqual((await self.client.post("/local/usage/preferences", json={"input_per_million": -1})).status_code, 400)
        self.assertEqual((await self.client.post("/local/usage/preferences", json={}, headers={"Origin": "https://other.example"})).status_code, 403)

    async def test_routing_log_accepts_conclusion_lists_and_exports(self):
        payload = {"id": "api-route-1", "decided_at": 1000, "source": "codex-auto-router", "router_build": "build-1",
                   "turn_hash": "turn-1", "decision_mode": "primary", "tier": "max", "model": "gpt-6-astra",
                   "effort": "max", "decision_reason": "qwen_primary", "conclusion": "开放问题且验证负担高，因此使用 max / max。",
                   "classifier_mode": "qwen", "classifier_used": True, "classifier_reason": "qwen_primary",
                   "classifier_kind": "proof", "classifier_confidence": "high", "qwen_tier": "max", "qwen_effort": "max",
                   "assessment": {"signals": ["formal_verification"]}, "resource": {"allow_qwen": True},
                   "cost_currency": "CNY", "cost_group": "default", "cost_group_multiplier": 1.0,
                   "cost_actual": 0.42, "cost_baseline": 2.10, "cost_saved": 1.68, "cost_actual_base": 0.42,
                   "cost_baseline_base": 2.10, "cost_input_tokens": 1200, "cost_cached_tokens": 400,
                   "cost_cache_write_tokens": 150,
                   "cost_output_tokens": 800, "cost_requests": 1, "cost_estimated": False,
                   "cost_source": "config", "cost_note": "按配置单价计算"}
        response = await self.client.post("/local/routing", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        result = (await self.client.get("/local/routing?start=0&end=2000&reason=验证负担")).json()
        self.assertEqual(result["total"], 1)
        self.assertIn("验证负担高", result["rows"][0]["conclusion"])
        row = result["rows"][0]
        self.assertEqual((row["cost_currency"], row["cost_group"], row["cost_group_multiplier"]), ("CNY", "default", 1.0))
        self.assertAlmostEqual(row["cost_actual"], 0.42)
        self.assertAlmostEqual(row["cost_baseline"], 2.10)
        self.assertAlmostEqual(row["cost_saved"], 1.68)
        self.assertEqual((row["cost_input_tokens"], row["cost_cached_tokens"], row["cost_cache_write_tokens"], row["cost_output_tokens"], row["cost_requests"]),
                         (1200, 400, 150, 800, 1))
        self.assertIs(row["cost_estimated"], False)
        self.assertEqual((row["cost_source"], row["cost_note"]), ("config", "按配置单价计算"))
        summary = result["summary"]
        self.assertEqual(summary["cost_routes"], 1)
        self.assertAlmostEqual(summary["cost_actual_total"], 0.42)
        self.assertAlmostEqual(summary["cost_saved_total"], 1.68)
        self.assertEqual(summary["cost_currency"], "CNY")
        export = await self.client.get("/local/routing/export?start=0&end=2000")
        self.assertEqual(export.status_code, 200)
        self.assertIn("conclusion", export.text)
        header = export.text.lstrip("\ufeff").splitlines()[0].split(",")
        self.assertEqual(header[-16:], ["cost_currency", "cost_group", "cost_group_multiplier", "cost_actual", "cost_baseline",
                                        "cost_saved", "cost_actual_base", "cost_baseline_base", "cost_input_tokens",
                                        "cost_cached_tokens", "cost_cache_write_tokens", "cost_output_tokens", "cost_requests", "cost_estimated",
                                        "cost_source", "cost_note"])

    async def test_database_failure_never_breaks_chat(self):
        self.backend(lambda request: httpx.Response(200, json={"usage": {"prompt_tokens": 3, "completion_tokens": 2}}))
        with patch.object(self.store, "begin", side_effect=OSError("disk unavailable")):
            self.assertEqual((await self.client.post("/completion", json={"prompt": "hello"})).status_code, 200)
        with patch.object(self.store, "finish", side_effect=OSError("disk full")):
            self.assertEqual((await self.client.post("/completion", json={"prompt": "hello"})).status_code, 200)
        self.assertEqual(module.manager.active_requests, 0)

    async def test_upstream_stream_error_and_truncation_are_visible(self):
        for suffix, expected in ((b'event: error\ndata: {"error":{"message":"offline"}}\n\n', "error"), (b"", "incomplete")):
            class Stream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n' + suffix
            self.backend(lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream()))
            response = await self.client.post("/v1/chat/completions", json={"messages": [], "stream": True})
            self.assertEqual(response.status_code, 200)
            row = (await self.rows())["rows"][0]
            self.assertEqual(row["status"], expected)
            self.assertIsNone(row["input_tokens"])
            await module.manager.client.aclose()

    async def test_client_disconnect_records_cancelled_with_unknown_usage(self):
        async def app(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
            await send({"type": "http.response.body", "body": b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n', "more_body": True})
            raise asyncio.CancelledError()
        async def receive():
            return {"type": "http.request", "body": b'{"stream":true}', "more_body": False}
        async def send(message):
            pass
        wrapped = UsageMiddleware(app, lambda: self.store, lambda: "text")
        with self.assertRaises(asyncio.CancelledError):
            await wrapped({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": [(b"content-type", b"application/json")]}, receive, send)
        row = (await self.rows())["rows"][0]
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNone(row["output_tokens"])

    async def test_cancellation_during_database_insert_does_not_leave_running_row(self):
        entered, release = threading.Event(), threading.Event()
        original = self.store.begin
        def delayed(record):
            entered.set()
            release.wait(3)
            return original(record)
        async def app(scope, receive, send):
            self.fail("Cancelled request must never reach inference")
        async def receive():
            return {"type": "http.request", "body": b'{}', "more_body": False}
        async def send(message):
            pass
        wrapped = UsageMiddleware(app, lambda: self.store, lambda: "text")
        with patch.object(self.store, "begin", side_effect=delayed):
            task = asyncio.create_task(wrapped({"type": "http", "method": "POST", "path": "/completion", "headers": []}, receive, send))
            await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        data = await self.rows()
        self.assertEqual(data["summary"]["running"], 0)
        self.assertEqual(data["rows"][0]["status"], "cancelled")

    async def test_large_request_passes_through_without_forcing_json_reencode(self):
        payload = b'{"stream":true,"messages":[{"role":"user","content":"large body"}]}'
        observed = []
        wire = b'data: {"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\ndata: [DONE]\n\n'
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield wire
        def backend(request):
            observed.append(request.content)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())
        self.backend(backend)
        with patch('app.usage.usage_tracking.MAX_REQUEST_INSPECTION', 20):
            response = await self.client.post('/v1/chat/completions', content=payload, headers={'Content-Type': 'application/json'})
        self.assertEqual(observed, [payload])
        self.assertEqual(response.content, wire)
        row = (await self.rows())["rows"][0]
        self.assertTrue(row["stream"])
        self.assertEqual(row["input_tokens"], 3)

    async def test_nonstream_responses_terminal_status_is_preserved(self):
        for status, expected in (("incomplete", "incomplete"), ("cancelled", "cancelled"), ("failed", "error")):
            self.backend(lambda request: httpx.Response(200, json={"status": status, "usage": {"input_tokens": 12, "output_tokens": 3}}))
            self.assertEqual((await self.client.post('/v1/responses', json={'input': 'hello'})).status_code, 200)
            row = (await self.rows())["rows"][0]
            self.assertEqual(row["status"], expected)
            self.assertEqual(row["input_tokens"], 12)
            await module.manager.client.aclose()


if __name__ == "__main__":
    unittest.main()
