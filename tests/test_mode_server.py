import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.image import image_jobs
from app import mode_server as module
from app.qwen_config import DEFAULTS, PROFILES, expected_context, server_command


class ProcessRecordTests(unittest.TestCase):
    def test_process_record_is_published_only_after_temporary_write(self):
        path = module.ROOT / "mock-only-process.json"
        operations = []
        with patch.object(module.Path, "write_text", autospec=True,
                          side_effect=lambda target, data, **kwargs: operations.append(("write", target, data))), \
                patch.object(module.Path, "replace", autospec=True,
                             side_effect=lambda source, target: operations.append(("replace", source, target))), \
                patch.object(module.Path, "unlink", autospec=True) as unlink:
            module.write_process_record(path, {"pid": 123})
        self.assertEqual([entry[0] for entry in operations], ["write", "replace"])
        temporary = operations[0][1]
        self.assertNotEqual(temporary, path)
        self.assertEqual(temporary.parent, path.parent)
        self.assertEqual(json.loads(operations[0][2]), {"pid": 123})
        self.assertEqual(operations[1][1:], (temporary, path))
        unlink.assert_called_once_with(temporary, missing_ok=True)

    def test_failed_temporary_write_does_not_replace_existing_record(self):
        path = module.ROOT / "mock-only-process.json"
        with patch.object(module.Path, "write_text", autospec=True, side_effect=OSError("disk full")) as write, \
                patch.object(module.Path, "replace", autospec=True) as replace, \
                patch.object(module.Path, "unlink", autospec=True) as unlink:
            with self.assertRaisesRegex(OSError, "disk full"):
                module.write_process_record(path, {"pid": 123})
        self.assertNotEqual(write.call_args.args[0], path)
        replace.assert_not_called()
        unlink.assert_called_once_with(write.call_args.args[0], missing_ok=True)


class ConfigTests(unittest.TestCase):
    def test_context_size_reaches_both_modes_and_status(self):
        """The profile's context size is what launches, and settings.json can set it only
        for a profile that does not pin one — the tunable knobs below stay settings-driven."""
        settings = DEFAULTS | {"context_size": 16384, "cache_ram_mib": 3072,
                               "server_timeout_seconds": 7200}
        expected = str(expected_context(settings))
        for mode in ("text", "vision"):
            command = server_command(mode, settings=settings)
            self.assertEqual(command[command.index("-c") + 1], expected)
            self.assertEqual(command[command.index("--cache-ram") + 1], "3072")
            self.assertEqual(command[command.index("--timeout") + 1], "7200")
        with patch.object(module, "read_settings", return_value=settings):
            self.assertEqual(module.ModeManager().status()["context_size"], int(expected))

    def test_modes_preserve_precision_and_sampling(self):
        # Per-mode differences only exist with the unified backend off: with it on, one
        # process serves both slices on purpose (see the unified_backend setting).
        settings = DEFAULTS | {"mtp_enabled": True, "unified_backend": False,
                               "context_size": 8192, "text_gpu_layers": 38,
                               "vision_gpu_layers": 40}
        text = server_command("text", settings=settings)
        vision = server_command("vision", settings=settings)
        for command in (text, vision):
            for flag, value in (("-ctk", "q8_0"), ("-ctv", "q8_0"),
                                ("--temp", "0.7"), ("--top-p", "0.9"), ("--top-k", "20")):
                self.assertEqual(command[command.index(flag) + 1], value)
        self.assertNotIn("--mmproj", text)
        self.assertIn("--mmproj", vision)
        # 2026-09-23 换血：这个 profile 没有草稿头，所以两种模式都不能要 draft-mtp。
        # 兜底是 ngram-mod（同模型实测：重复型负载 +82.8%、自然文本不变、输出逐字相同）；
        # 精度与采样仍然按模式区分，这才是本用例要守的契约。
        self.assertEqual(text[text.index("--spec-type") + 1], "ngram-mod")
        self.assertEqual(vision[vision.index("--spec-type") + 1], "ngram-mod")

    def test_the_unified_backend_asks_for_both_capabilities_at_once(self):
        """What makes a mode switch instant: projector and draft head in one process.

        随包发布的 profile 现在都没有草稿头，所以"带草稿头的统一后端"这条路径用一个注入的
        合成 profile 继续覆盖；同一个用例同时断言当前 27B 的真实命令落到 ngram-mod 兜底。
        """
        settings = DEFAULTS | {"mtp_enabled": True, "unified_backend": True}
        capable = {"model": "models/synthetic.gguf", "mmproj": "models/synthetic-mmproj.gguf",
                   "alias": "synthetic-mtp", "gpu_layers": 1, "vision_gpu_layers": 1,
                   "mtp": True, "context_size": 4096}
        with patch.dict(PROFILES, {"synthetic-mtp": capable}):
            injected = settings | {"model_profile": "synthetic-mtp"}
            command = server_command("vision", settings=injected, mtp=True, mmproj=True)
            self.assertIn("--mmproj", command)
            self.assertEqual(command[command.index("--spec-type") + 1], "draft-mtp")
        shipped = server_command("vision", settings=settings, mtp=True, mmproj=True)
        self.assertIn("--mmproj", shipped)
        self.assertEqual(shipped[shipped.index("--spec-type") + 1], "ngram-mod")


class ThinkingLevelTests(unittest.TestCase):
    """The panel's thinking tier must reach the template intact — and never as a value the
    template would raise on (a wrong `reasoning_effort` is a 500 for the whole turn)."""

    def apply(self, level, profile="qwen3.8-27b", payload=None):
        settings = DEFAULTS | {"model_profile": profile}
        return module.apply_thinking_level(payload or {"messages": []}, level, settings)

    def test_off_turns_thinking_off_and_clears_a_stale_effort(self):
        payload, applied = self.apply("off", payload={
            "messages": [], "chat_template_kwargs": {"reasoning_effort": "xhigh", "other": 1}})
        self.assertEqual(applied, "off")
        self.assertIs(payload["chat_template_kwargs"]["enable_thinking"], False)
        self.assertNotIn("reasoning_effort", payload["chat_template_kwargs"])
        self.assertEqual(payload["chat_template_kwargs"]["other"], 1, "unrelated kwargs survive")

    def test_each_27b_tier_becomes_the_effort_the_template_accepts(self):
        for level in ("low", "medium", "xhigh"):
            with self.subTest(level=level):
                payload, applied = self.apply(level)
                self.assertEqual(applied, level)
                self.assertIs(payload["chat_template_kwargs"]["enable_thinking"], True)
                self.assertEqual(payload["chat_template_kwargs"]["reasoning_effort"], level)

    def test_a_tier_the_profile_cannot_express_is_ignored_not_forwarded(self):
        # 9B: no `reasoning_effort` in its template, so "低" must not be sent, and the request
        # must be left byte-identical rather than half-rewritten.
        original = {"messages": [], "chat_template_kwargs": {"enable_thinking": False}}
        payload, applied = self.apply("low", profile="qwen3.5-9b-uncensored", payload=dict(original))
        self.assertEqual(applied, "")
        self.assertEqual(payload, original)
        # ...while the two tiers it does have still work.
        for level in ("off", "on"):
            with self.subTest(level=level):
                payload, applied = self.apply(level, profile="qwen3.5-9b-uncensored")
                self.assertEqual(applied, level)
                self.assertIs(payload["chat_template_kwargs"]["enable_thinking"], level == "on")
                self.assertNotIn("reasoning_effort", payload["chat_template_kwargs"])

    def test_garbage_and_empty_headers_leave_the_request_alone(self):
        for level in ("", "  ", "HIGH", "ultra"):
            with self.subTest(level=level):
                original = {"messages": []}
                payload, applied = self.apply(level, payload=dict(original))
                self.assertEqual(applied, "")
                self.assertEqual(payload, original)


class ModeApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous_manager = module.manager
        module.manager = module.ModeManager()
        module.manager.state = "ready"
        module.manager.mode = "text"
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),
                                        base_url="http://127.0.0.1:8081")

    async def asyncTearDown(self):
        if module.manager.client:
            await module.manager.client.aclose()
        await self.client.aclose()
        module.manager = self.previous_manager

    async def test_media_is_rejected_in_text_mode_without_upstream_request(self):
        response = await self.client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
            ]}],
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(module.manager.active_requests, 0)

    async def test_agent_tool_schemas_are_forwarded_without_media_detection(self):
        observed = []

        def upstream(request):
            observed.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": []})

        module.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        payload = {
            "messages": [{"role": "user", "content": "Inspect this JSON schema"}],
            "tools": [{"type": "function", "function": {
                "name": "inspect", "parameters": {"type": "object", "properties": {
                    "type": {"type": ["string", "null"]},
                    "content": {"default": {"type": "image", "image_data": "example"}},
                }},
            }}],
            "response_format": {"type": "json_schema", "json_schema": {
                "schema": {"type": {"const": "image_url"}},
            }},
        }
        result = await self.client.post("/v1/chat/completions", json=payload)
        self.assertEqual(result.status_code, 200)
        # 工具 schema 里的 "type": "image_url" / image_data 是**声明**，不是这一轮带的图：
        # 既不能被判成媒体（那样纯文本档会 400），也不能改动 tools / response_format。
        self.assertEqual(len(observed), 1)
        forwarded = observed[0]
        self.assertEqual(forwarded["tools"], payload["tools"])
        self.assertEqual(forwarded["response_format"], payload["response_format"])
        # 唯一的差别是"带工具的回合会多一条环境/工具规程"（agent_briefing，见
        # test_agent_briefing.py）。这条 system 消息不是把 schema 当图看，所以只断言顺序与角色。
        self.assertEqual([m["role"] for m in forwarded["messages"]], ["system", "user"])
        self.assertIn("这台机器", forwarded["messages"][0]["content"])
        self.assertEqual(forwarded["messages"][1], payload["messages"][0])
        self.assertEqual(module.manager.active_requests, 0)

    def test_a_vision_turn_on_a_vision_backend_keeps_the_text_threshold(self):
        """识图回合在识图后端上不换权重：按文本口径（低门槛）判，否则常驻模型时永远 409。

        实测（10-31）：9B@131072 常驻时只剩 314 MiB 显存，问一张图直接 409
        "显卡可用显存不足（314 MiB < 2048 MiB）"——功能看起来就是坏的。统一后端下投影和
        语言权重在同一个进程里，这一轮不新占显存，所以按 text 判；而生图/生视频/换档位仍然
        走"要新载显存"的严格门槛。
        """
        self.assertEqual(module.admission_kind("vision", "vision"), "text")
        self.assertEqual(module.admission_kind("vision", "text"), "vision")
        self.assertEqual(module.admission_kind("image", "vision"), "image")
        self.assertEqual(module.admission_kind("video", "text"), "video")
        self.assertEqual(module.admission_kind("text", "text"), "text")
        self.assertIsNone(module.admission_kind(None, "vision"))

    async def test_media_input_formats_are_rejected_but_allowed_in_vision(self):
        module.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True})
        ))
        for payload in (
            {"input": [{"role": "user", "content": [{"type": "input_image", "image_url": "data:"}]}]},
            {"messages": [{"role": "user", "content": [{"type": "tool_result", "content": [
                {"type": "image", "source": {"type": "base64", "data": "AA=="}},
            ]}]}]},
            {"prompt": "describe", "image_data": [{"id": 0, "data": "AA=="}]},
            {"prompt": {"prompt_string": "describe", "multimodal_data": ["AA=="]}},
        ):
            with self.subTest(payload=payload):
                module.manager.mode = "text"
                self.assertEqual((await self.client.post("/completion", json=payload)).status_code, 400)
                module.manager.mode = "vision"
                self.assertEqual((await self.client.post("/completion", json=payload)).status_code, 200)
        self.assertEqual(module.manager.active_requests, 0)

    async def test_invalid_json_and_non_object_bodies_return_400(self):
        for body in (b"{broken", b"[]", b"null", b'"text"', b"[" * 2000 + b"]" * 2000):
            with self.subTest(body=body[:20]):
                result = await self.client.post("/completion", content=body,
                                               headers={"Content-Type": "Application/JSON; charset=utf-8"})
                self.assertEqual(result.status_code, 400)
                self.assertEqual(module.manager.active_requests, 0)

    async def test_non_string_content_type_does_not_crash_media_inspection(self):
        for value in ({}, [], ["text", "image"]):
            self.assertFalse(module.has_media({"messages": [{"content": [{"type": value, "text": "hello"}]}]}))

    async def test_switch_checks_origin_mode_and_busy_state(self):
        self.assertEqual((await self.client.post("/local/mode", json={"mode": "bad"})).status_code, 400)
        self.assertEqual((await self.client.post("/local/mode", json={"mode": "vision"},
                         headers={"Origin": "https://unrelated.example"})).status_code, 403)
        module.manager.active_requests = 1
        self.assertEqual((await self.client.post("/local/mode", json={"mode": "vision"})).status_code, 409)
        module.manager.active_requests = 0
        module.manager.state = "switching"
        self.assertEqual((await self.client.post("/local/mode", json={"mode": "text"})).status_code, 409)

    async def test_generation_is_counted_until_stream_finishes(self):
        entered, release = asyncio.Event(), asyncio.Event()

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                await release.wait()
                yield b'data: {"choices":[]}\n\ndata: [DONE]\n\n'

        module.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())
        ))
        generation = asyncio.create_task(self.client.post("/v1/chat/completions", json={"messages": []}))
        await asyncio.wait_for(entered.wait(), 3)
        self.assertEqual(module.manager.active_requests, 1)
        self.assertEqual((await self.client.post("/local/mode", json={"mode": "vision"})).status_code, 409)
        release.set()
        result = await generation
        self.assertIn("[DONE]", result.text)
        self.assertEqual(module.manager.active_requests, 0)

    async def test_failed_upstream_releases_busy_state(self):
        def failed(request):
            raise httpx.ConnectError("offline")
        module.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(failed))
        response = await self.client.post("/completion", json={"prompt": "test"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(module.manager.active_requests, 0)

    async def test_generation_read_deadline_is_configurable_without_changing_other_requests(self):
        observed = []

        def upstream(request):
            observed.append((request.method, request.url.path, request.extensions["timeout"]))
            return httpx.Response(200, json={"choices": []})

        module.manager.client = httpx.AsyncClient(
            transport=httpx.MockTransport(upstream), timeout=httpx.Timeout(600, connect=5),
        )
        for read_timeout in (900, 3600):
            module.manager.settings["server_timeout_seconds"] = read_timeout
            for path in sorted(module.GENERATION_PATHS):
                for stream in (False, True):
                    result = await self.client.post(path, json={"messages": [], "stream": stream})
                    self.assertEqual(result.status_code, 200)
                    timeout = observed[-1][2]
                    self.assertEqual(timeout["read"], read_timeout)
                    self.assertEqual(timeout["connect"], 5)
                    self.assertEqual(timeout["write"], 600)
                    self.assertEqual(timeout["pool"], 600)
        module.manager.settings.pop("server_timeout_seconds", None)
        await self.client.post("/completion", json={"prompt": "test"})
        self.assertEqual(observed[-1][2]["read"], 3600)
        for method, path in (("GET", "/health"), ("POST", "/tokenize"), ("POST", "/tools")):
            result = await self.client.request(method, path, json={})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(observed[-1][2]["read"], 600)
            self.assertEqual(observed[-1][2]["connect"], 5)
        self.assertEqual(module.manager.active_requests, 0)

    async def test_interrupted_sse_reports_failure_and_closes_response(self):
        closed = []

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[]}\n\n'
                raise httpx.ReadError("backend disappeared")

            async def aclose(self):
                closed.append(True)

        module.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())
        ))
        result = await self.client.post("/v1/chat/completions", json={"messages": [], "stream": True})
        self.assertEqual(result.status_code, 200)  # Streaming headers preceded the failure.
        self.assertIn("event: error", result.text)
        self.assertIn("upstream_unavailable", result.text)
        self.assertNotIn("[DONE]", result.text)
        self.assertEqual(closed, [True])
        self.assertEqual(module.manager.active_requests, 0)
        self.assertEqual(module.manager.next_health_check, 0)

    async def test_interrupted_json_returns_503_before_sending_headers(self):
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'{"choices":'
                raise httpx.ReadError("backend disappeared")

        module.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"content-type": "application/json"}, stream=Stream())
        ))
        result = await self.client.post("/v1/chat/completions", json={"messages": []})
        self.assertEqual(result.status_code, 503)
        self.assertIn("error", result.json())
        self.assertEqual(module.manager.active_requests, 0)

    async def test_cancel_before_headers_releases_busy_state(self):
        entered = asyncio.Event()
        async def pending(request):
            entered.set()
            await asyncio.Event().wait()
        module.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(pending))
        generation = asyncio.create_task(self.client.post("/v1/messages", json={"messages": []}))
        await asyncio.wait_for(entered.wait(), 3)
        self.assertEqual(module.manager.active_requests, 1)
        generation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await generation
        self.assertEqual(module.manager.active_requests, 0)

    async def test_cancel_during_stream_releases_busy_state(self):
        entered = asyncio.Event()
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                yield b"data: first\n\n"
                await asyncio.Event().wait()
        module.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=Stream())
        ))
        generation = asyncio.create_task(self.client.post("/v1/messages", json={"messages": []}))
        await asyncio.wait_for(entered.wait(), 3)
        generation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await generation
        self.assertEqual(module.manager.active_requests, 0)

    async def test_multipart_is_forwarded_without_json_parsing(self):
        observed = []
        def upstream(request):
            observed.append((module.manager.active_requests, request.headers["content-type"]))
            return httpx.Response(400, json={"error": "Model does not support audio"})
        module.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        result = await self.client.post("/v1/audio/transcriptions", files={"file": ("test.wav", b"RIFF")})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(observed[0][0], 1)
        self.assertTrue(observed[0][1].startswith("multipart/form-data"))
        self.assertEqual(module.manager.active_requests, 0)

    async def test_html_error_response_also_releases_busy_state(self):
        module.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(500, text="<html><head></head></html>",
                                          headers={"content-type": "text/html"})
        ))
        result = await self.client.post("/completion", json={"prompt": "test"})
        self.assertEqual(result.status_code, 500)
        self.assertEqual(module.manager.active_requests, 0)

    async def test_html_injection_keeps_original_app_and_origin(self):
        module.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"content-type": "text/html", "etag": "old"},
                                           text='<html><head></head><body><div id="native"></div></body></html>')
        ))
        response = await self.client.get("/test-native-route")
        self.assertIn('id="native"', response.text)
        self.assertIn('/local/mode-control.js', response.text)
        self.assertNotIn("etag", response.headers)
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_mode_switch_schedules_one_load(self):
        with patch.object(module.manager, "load", new_callable=AsyncMock) as load:
            response = await self.client.post("/local/mode", json={"mode": "vision"})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(module.manager.state, "switching")
            await module.manager.task
            load.assert_awaited_once_with("vision")

    async def test_initialize_removes_only_owned_orphan_before_reload(self):
        module.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(503)
        ))
        # The methods are mocked; never overwrite or delete live process records.
        with patch.object(module.manager, "adopt_backend", new_callable=AsyncMock, return_value=False), \
                patch.object(module.manager, "remove_owned_orphan", new_callable=AsyncMock) as remove, \
                patch.object(module.manager, "load", new_callable=AsyncMock) as load:
            await module.manager.initialize()
            remove.assert_awaited_once()
            load.assert_awaited_once_with(module.manager.mode)

    async def test_corrupt_backend_records_do_not_block_initialization_or_touch_processes(self):
        valid = {"pid": 123, "created_unix_ms": 1000, "port": 8082, "model": str(module.MODEL)}
        values = [None, 42, [], "text", {}, {"pid": []}]
        for key, value in (("pid", True), ("pid", 0), ("created_unix_ms", {}),
                           ("port", "8082"), ("model", [])):
            values.append(valid | {key: value})
        bodies = ["{broken", "[" * 2000 + "]" * 2000] + [json.dumps(value) for value in values]
        for body in bodies:
            with self.subTest(body=body[:80]), \
                    patch.object(module.Path, "exists", return_value=True), \
                    patch.object(module.Path, "read_text", return_value=body), \
                    patch.object(module.Path, "unlink") as unlink, \
                    patch.object(module.psutil, "Process") as process, \
                    patch.object(module.LOGGER, "exception"), \
                    patch.object(module.manager, "load", new_callable=AsyncMock) as load:
                await module.manager.initialize()
                load.assert_awaited_once_with(module.manager.mode)
                process.assert_not_called()
                unlink.assert_not_called()

    async def test_short_backend_commands_are_never_adopted_or_terminated(self):
        record = {"pid": 123, "created_unix_ms": 1000, "port": 8082, "model": str(module.MODEL)}
        executable = str(module.ROOT / "runtime/llama-server.exe")
        commands = ([], [executable, "-m"], [executable, "-m", str(module.MODEL), "--port"],
                    [executable, "--port", "8082", "-m"])
        for command in commands:
            with self.subTest(command=command), \
                    patch.object(module.Path, "exists", return_value=True), \
                    patch.object(module.Path, "read_text", return_value=json.dumps(record)), \
                    patch.object(module.Path, "unlink") as unlink, \
                    patch.object(module.psutil, "Process") as process:
                process.return_value.cmdline.return_value = command
                process.return_value.exe.return_value = executable
                process.return_value.create_time.return_value = 1
                self.assertFalse(await module.manager.adopt_backend())
                self.assertFalse(await module.manager.remove_owned_orphan())
                process.return_value.terminate.assert_not_called()
                process.return_value.kill.assert_not_called()
                unlink.assert_not_called()

    async def test_malformed_adopted_backend_props_return_false(self):
        record = {"pid": 123, "created_unix_ms": 1000, "port": 8082, "model": str(module.MODEL)}
        with patch.object(module.Path, "exists", return_value=True), \
                patch.object(module.Path, "read_text", return_value=json.dumps(record)), \
                patch.object(module.psutil, "Process") as process, \
                patch.object(module.LOGGER, "exception"):
            process.return_value.cmdline.return_value = server_command("text", settings=module.manager.settings)
            process.return_value.exe.return_value = str(module.ROOT / "runtime/llama-server.exe")
            process.return_value.create_time.return_value = 1
            for props in (None, [], {"default_generation_settings": []}):
                with self.subTest(props=props):
                    async with httpx.AsyncClient(transport=httpx.MockTransport(
                        lambda request: httpx.Response(200, content=json.dumps(props),
                                                      headers={"content-type": "application/json"})
                    )) as client:
                        module.manager.client = client
                        self.assertFalse(await module.manager.adopt_backend())
            process.return_value.terminate.assert_not_called()

    async def test_idle_health_checks_preserve_sleep_and_refresh_props(self):
        observed = []

        def upstream(request):
            observed.append((request.method, request.url.path))
            return httpx.Response(200, json={"status": "ok"} if request.url.path == "/health"
                                  else {"is_sleeping": True})

        module.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        module.manager.health_failures = 2
        await module.manager.check_backend_health()
        self.assertEqual(observed, [("GET", "/health"), ("GET", "/props")])
        self.assertEqual(module.manager.state, "ready")
        self.assertEqual(module.manager.health_failures, 0)
        self.assertTrue(module.manager.status()["sleeping"])
        self.assertTrue(module.manager.metadata["props"]["is_sleeping"])

    async def test_three_idle_health_failures_schedule_recovery(self):
        module.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(503)
        ))
        for _ in range(2):
            await module.manager.check_backend_health()
            self.assertEqual(module.manager.state, "ready")
        await module.manager.check_backend_health()
        self.assertEqual(module.manager.state, "error")
        self.assertGreater(module.manager.retry_at, 0)

    async def test_the_probe_stands_down_in_the_modes_that_hold_no_language_backend(self):
        """image and video hand the card to ComfyUI, so a probe there can only invent an outage."""
        observed = []

        def upstream(request):
            observed.append(request.url.path)
            return httpx.Response(503)

        module.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        for mode in ("image", "video"):
            module.manager.mode = mode
            module.manager.state = "ready"
            module.manager.health_failures = 2
            await module.manager.check_backend_health()
            self.assertEqual(observed, [], f"{mode} must not be probed")
            self.assertEqual(module.manager.state, "ready")
            self.assertEqual(module.manager.health_failures, 2,
                             "the counter must not climb towards a bogus recovery")
        # A language mode still gets probed, and the third failure is what escalates: the
        # counter was left at 2, so one more probe takes it to error and stops probing.
        module.manager.mode = "text"
        module.manager.state = "ready"
        await module.manager.check_backend_health()
        self.assertEqual(observed, ["/health"])
        self.assertEqual(module.manager.health_failures, 3)
        self.assertEqual(module.manager.state, "error")

    async def test_health_checks_skip_work_and_discard_results_when_work_starts(self):
        observed = []

        def upstream(request):
            observed.append(True)
            module.manager.active_requests = 1
            return httpx.Response(503)

        module.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        module.manager.active_requests = 1
        await module.manager.check_backend_health()
        self.assertEqual(observed, [])
        module.manager.active_requests = 0
        module.manager.health_failures = 2
        await module.manager.check_backend_health()
        self.assertEqual(module.manager.state, "ready")
        self.assertEqual(module.manager.health_failures, 2)

    async def test_recovery_continues_after_three_failures(self):
        module.manager.state = "error"
        module.manager.recovery_attempts = 3
        with patch.object(module.manager, "load", new_callable=AsyncMock) as load:
            self.assertTrue(await module.manager.recover())
            await module.manager.task
            load.assert_awaited_once_with("text")

    async def test_load_failure_backoff_is_bounded_and_never_disabled(self):
        with patch.object(module.manager, "ensure_comfy_idle", new_callable=AsyncMock,
                          side_effect=RuntimeError("temporarily unavailable")), \
                patch.object(module.manager, "stop_backend", new_callable=AsyncMock), \
                patch.object(module.time, "monotonic", return_value=100):
            for attempts, expected_delay in ((2, 8), (5, 60), (1000, 60)):
                module.manager.recovery_attempts = attempts
                await module.manager.load("text")
                self.assertEqual(module.manager.state, "error")
                self.assertEqual(module.manager.retry_at, 100 + expected_delay)

    async def test_stale_comfy_queue_entry_is_cleared_once_the_gpu_says_idle(self):
        """With the setting on, a leftover prompt is dropped only when the card is idle.

        The utilisation verdict is what separates a dead entry from another client's
        live render, so a busy card must still raise instead of interrupting.
        """
        calls: list[str] = []
        busy = {"running": ["leftover"]}

        def upstream(request):
            path = request.url.path
            if path == "/queue" and request.method == "GET":
                return httpx.Response(200, json={
                    "queue_running": [["1", pid] for pid in busy["running"]], "queue_pending": []})
            if path == "/interrupt":
                calls.append("interrupt")
                busy["running"] = []
                return httpx.Response(200, json={})
            if path == "/queue" and request.method == "POST":
                calls.append("clear")
                return httpx.Response(200, json={})
            if path.startswith("/history/"):
                return httpx.Response(200, json={})
            if path == "/free":
                calls.append("free")
                return httpx.Response(200, json={})
            return httpx.Response(404)

        module.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        module.manager.comfy_restart_attempted = False
        module.manager.clear_stale_comfy = True
        idle_monitor = image_jobs.ImageJobMonitor(None, None)
        idle_monitor.queue_guard = image_jobs.ComfyQueueGuard(idle_seconds=0, utilization=15)
        idle_monitor.queue_guard.observe({"utilization": 2})
        idle_monitor.gpu.sample = AsyncMock(return_value={"utilization": 2})
        previous_monitor = module.image_jobs
        module.image_jobs = idle_monitor
        try:
            with patch.object(module.manager, "image_job_is_idle", return_value=True):
                await module.manager.ensure_comfy_idle()
                self.assertEqual(calls, ["interrupt", "clear", "free"])
                self.assertFalse(module.manager.comfy_restart_attempted,
                                 "a plain interrupt is not a restart")
                # A busy card is somebody's live render: wait instead of interrupting.
                calls.clear()
                busy["running"] = ["live-render"]
                idle_monitor.gpu.sample = AsyncMock(return_value={"utilization": 90})
                idle_monitor.queue_guard.observe({"utilization": 90})
                with self.assertRaises(module.ComfyBusyError):
                    await module.manager.ensure_comfy_idle()
                self.assertEqual(calls, [])
        finally:
            module.image_jobs = previous_monitor
            await module.manager.client.aclose()

    async def test_a_busy_queue_is_judged_again_after_releasing_the_language_model(self):
        """Our own model's GPU use must not keep a dead queue entry alive forever."""
        attempts: list[str] = []
        busy = {"running": ["leftover"]}

        def upstream(request):
            path = request.url.path
            if path == "/queue" and request.method == "GET":
                return httpx.Response(200, json={
                    "queue_running": [["1", pid] for pid in busy["running"]], "queue_pending": []})
            if path == "/interrupt":
                attempts.append("interrupt")
                busy["running"] = []
                return httpx.Response(200, json={})
            if path == "/queue" and request.method == "POST":
                attempts.append("clear")
                return httpx.Response(200, json={})
            if path.startswith("/history/"):
                return httpx.Response(200, json={})
            if path == "/free":
                attempts.append("free")
                return httpx.Response(200, json={})
            return httpx.Response(404)

        module.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        module.manager.clear_stale_comfy = True
        module.manager.comfy_restart_attempted = False
        module.manager.process = object()          # a language model is holding the GPU
        monitor = image_jobs.ImageJobMonitor(None, None)
        monitor.queue_guard = image_jobs.ComfyQueueGuard(idle_seconds=0, utilization=15)
        monitor.gpu.sample = AsyncMock(return_value={"utilization": 3})
        previous = module.image_jobs
        module.image_jobs = monitor
        stops: list[bool] = []
        try:
            with patch.object(module.manager, "image_job_is_idle", return_value=True), \
                 patch.object(module.manager, "stop_backend", AsyncMock(side_effect=lambda: stops.append(True))):
                await module.manager.load("image")
        finally:
            module.image_jobs = previous
            module.manager.process = None
            await module.manager.client.aclose()
        self.assertTrue(stops, "the language model is released before the second verdict")
        self.assertIn("interrupt", attempts, "and the dead entry is then cleared")
        self.assertEqual(module.manager.mode, "image")

    async def test_busy_comfy_queue_is_never_interrupted_by_default(self):
        """A queue entry we did not submit may be another client's live render.

        With the setting off the switch must wait rather than interrupt; the
        switch defaults to on for this machine, but the behaviour it enables is
        still gated on the GPU being idle.
        """
        calls: list[str] = []

        def upstream(request):
            if request.url.path == "/queue" and request.method == "GET":
                return httpx.Response(200, json={"queue_running": [{"prompt_id": "someone-elses-render"}],
                                                 "queue_pending": []})
            calls.append(request.url.path)
            return httpx.Response(200, json={})

        module.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        module.manager.comfy_restart_attempted = False
        module.manager.clear_stale_comfy = False
        with patch.object(module.manager, "image_job_is_idle", return_value=True):
            with self.assertRaises(module.ComfyBusyError):
                await module.manager.ensure_comfy_idle()
        self.assertEqual(calls, [], "no interrupt and no free may be issued")
        self.assertFalse(module.manager.comfy_restart_attempted)

    async def test_live_image_job_is_never_interrupted(self):
        calls: list[str] = []

        def upstream(request):
            if request.url.path == "/queue":
                return httpx.Response(200, json={"queue_running": [{"prompt_id": "ours"}], "queue_pending": []})
            calls.append(request.url.path)
            return httpx.Response(200, json={})

        module.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        module.manager.cleared_stale_comfy = False
        with patch.object(module.manager, "image_job_is_idle", return_value=False):
            with self.assertRaises(module.ComfyBusyError):
                await module.manager.ensure_comfy_idle()
        self.assertEqual(calls, [])
        self.assertFalse(module.manager.comfy_restart_attempted)

    async def test_optional_metadata_failure_does_not_fail_model_load(self):
        def upstream(request):
            if request.url.path == "/tools":
                raise httpx.ReadTimeout("optional endpoint unavailable")
            return httpx.Response(200, json={"data": []})

        module.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        await module.manager.cache_metadata({"is_sleeping": False})
        self.assertIn("props", module.manager.metadata)
        self.assertIn("v1/models", module.manager.metadata)
        self.assertNotIn("tools", module.manager.metadata)


if __name__ == "__main__":
    unittest.main()
