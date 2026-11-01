"""Compaction gateway contract tests; no live model, sockets, or production DB."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import asyncio
from contextlib import ExitStack
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import HTTPException

from app import mode_server as module
from app.context.context_compaction import CompactionError
from app.context.conversation_ledger import ConversationLedger


class ContextGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.manager = module.ModeManager()
        self.manager.state = "ready"
        self.manager.mode = "text"
        self.manager.settings["context_size"] = 49152
        # 把档位钉死：`ModeManager()` 会读机器上的 settings.json，而"当前选中哪个模型"是主人的
        # 状态、不是这条契约的一部分。钉之前，主人把选择从 27B 换到 9B 之后，下面那条
        # `ledger_window()` 窗口断言就会莫名其妙地失败（32768 变成 131072）。
        self.manager.settings["model_profile"] = "qwen3.8-27b"
        self.manager.settings["context_compression_enabled"] = True
        self.compactor = Mock()

        async def unchanged(payload, client, context_size, **options):
            return payload, {"state": "unchanged", "original_tokens": 24, "prompt_tokens": 24, "saved_tokens": 0}

        self.compactor.prepare = AsyncMock(side_effect=unchanged)
        self.compactor.get_status.return_value = {"state": "idle", "summary_count": 0}
        self.stack.enter_context(patch.object(module, "manager", self.manager))
        self.stack.enter_context(patch.object(module, "context_compactor", self.compactor))
        self.stack.enter_context(patch.object(module, "pending_context_streams", {}))
        self.stack.enter_context(patch.object(module, "usage_store", None))
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=module.app), base_url="http://127.0.0.1:8081",
        )
        self.tasks = []
        self.requests = []
        self.backend(self.default_backend)

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.client.aclose()
        if self.manager.client:
            await self.manager.client.aclose()
        self.stack.close()

    def backend(self, callback):
        self.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(callback))

    def default_backend(self, request):
        self.requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    def start(self, operation):
        task = asyncio.create_task(operation)
        self.tasks.append(task)
        return task

    async def post_chat(self, **options):
        options.setdefault("json", {"messages": [{"role": "user", "content": "继续"}]})
        return await self.client.post("/v1/chat/completions", **options)

    async def test_compaction_gets_the_launched_window_not_the_settings_key(self):
        """压缩必须拿到**正在服务的后端窗口**，而不是 settings.json 里的原始键。

        真机踩到（09-21 13:06–13:07，三条 `incomplete` 记录）：路由把
        `manager.settings["context_size"]`（= 8192）当摘要窗口传下去，而实际加载的 9B 是 131072。
        于是 4.3k 的对话也被判"装不下"（报"单个较早回合超过摘要窗口"），
        且 `summary_tokens = min(1536, max(256, 8192//32))` 正好 = 256，摘要被截断成
        "本地历史摘要未完整结束，未替换原消息"。这里钉住：传的是 launched 窗口。
        """
        self.manager.launched_context = 131072          # 实际启动的 9B 窗口
        self.manager.launched_profile = "qwen3.5-9b-uncensored"
        self.assertEqual(self.manager.settings["context_size"], 49152)   # 设置里的另一个值
        await self.post_chat(headers={"x-qwen-context-compression": "force"})
        self.assertTrue(self.compactor.prepare.await_count >= 1)
        passed = self.compactor.prepare.await_args.args[2]
        self.assertEqual(passed, 131072,
                         "压缩窗口必须是 ledger_window()，不能是 settings['context_size']")

    async def test_compaction_window_follows_a_borrowed_smaller_window(self):
        """27B/9B/语音借用：窗口永远跟着"正在服务的那份"，所以两者都自适应。"""
        self.manager.launched_context = 40960            # 例如语音借用时的窗口
        await self.post_chat(headers={"x-qwen-context-compression": "force"})
        self.assertEqual(self.compactor.prepare.await_args.args[2], 40960)

    async def test_a_looping_tool_call_is_interrupted_but_keeps_its_tools(self):
        """同一次工具调用反复失败时要打断——但**不能只会打断**（所有者明确要求）。

        第 1 级的处置是：工具照给，追加一条指令，要求它说清失败原因、给出另一条可行方案、
        并**立刻按新方案继续**。摘掉工具是第 3 级才做的事：摘了就只能解释，这一轮等于废掉。
        打包界面里没有任何步数上限（实测 bundle 中 `max_steps` 出现 0 次），所以刹车装在网关。
        """
        messages = [{"role": "user", "content": "帮我查这份文档"}]
        for index in range(3):
            messages.append({"role": "assistant", "content": None, "tool_calls": [
                {"id": f"call-{index}", "type": "function",
                 "function": {"name": "read_file", "arguments": '{"path":"D:/docs/手册.md"}'}}]})
            messages.append({"role": "tool", "tool_call_id": f"call-{index}",
                             "content": "Error: 找不到文件（No such file）"})
        payload = {"model": "local", "messages": messages, "stream": False,
                   "tools": [{"type": "function", "function": {"name": "read_file"}},
                             {"type": "function", "function": {"name": "grep"}}]}
        response = await self.post_chat(json=payload)
        self.assertEqual(response.status_code, 200)
        forwarded = json.loads(self.requests[0].content)
        self.assertIn("tools", forwarded, "第 1 级不摘工具：它还要能继续干活")
        self.assertEqual([tool["function"]["name"] for tool in forwarded["tools"]],
                         ["read_file", "grep"])
        note = forwarded["messages"][-1]["content"]
        self.assertIn("循环防护", note)
        self.assertIn("可行方案", note)
        self.assertIn("继续执行", note)
        self.assertIn("read_file", note)
        self.assertEqual(response.headers.get("x-qwen-loop-guard"), "read_file/3")
        self.assertEqual(response.headers.get("x-qwen-loop-guard-reason"), "repeat-identical")
        self.assertEqual(response.headers.get("x-qwen-loop-guard-level"), "1")

    async def test_a_healthy_multi_step_turn_keeps_its_tools(self):
        messages = [{"role": "user", "content": "整理文档"}]
        for index in range(4):
            messages.append({"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{index}", "type": "function",
                 "function": {"name": "read_file", "arguments": f'{{"path":"doc{index}.md"}}'}}]})
            messages.append({"role": "tool", "tool_call_id": f"c{index}",
                             "content": f"第 {index} 份文档内容"})
        payload = {"model": "local", "messages": messages, "stream": False,
                   "tools": [{"type": "function", "function": {"name": "read_file"}}]}
        response = await self.post_chat(json=payload)
        forwarded = json.loads(self.requests[0].content)
        self.assertIn("tools", forwarded, "正常的多步任务不能被误判成循环")
        self.assertIsNone(response.headers.get("x-qwen-loop-guard"))

    async def test_an_endless_but_productive_turn_hits_the_step_budget(self):
        """"一直在干活、永远不收尾"也要刹住：一轮里工具调用到 24 次就摘工具让它总结。

        前两条判据只管"原地打转"；这条管"每次都成功、但没完没了"——打包界面没有步数上限，
        网关是最后一道刹车。这里走的是**真实路由**，确认这个分支在 HTTP 层也生效。
        """
        messages = [{"role": "user", "content": "把项目里每个模块都看一遍"}]
        for index in range(24):
            name = "read_file" if index % 2 else "grep"
            messages.append({"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{index}", "type": "function",
                 "function": {"name": name, "arguments": f'{{"n":{index}}}'}}]})
            messages.append({"role": "tool", "tool_call_id": f"c{index}",
                             "content": f"第 {index} 项内容正常"})
        payload = {"model": "local", "messages": messages, "stream": False,
                   "tools": [{"type": "function", "function": {"name": "read_file"}}]}
        response = await self.post_chat(json=payload)
        forwarded = json.loads(self.requests[0].content)
        self.assertNotIn("tools", forwarded, "超预算后不能再把工具交给模型")
        note = forwarded["messages"][-1]["content"]
        self.assertIn("循环防护", note)
        self.assertIn("24", note)
        self.assertEqual(response.headers.get("x-qwen-loop-guard-reason"), "step-budget")
        self.assertEqual(response.headers.get("x-qwen-loop-guard-level"), "3")
        self.assertEqual(response.headers.get("x-qwen-loop-guard", "").endswith("/24"), True)

    async def test_prepared_payload_forwarded_without_mutating_client_history(self):
        payload = {"messages": [{"role": "system", "content": "system"},
                                {"role": "user", "content": "older"},
                                {"role": "assistant", "content": "answer"},
                                {"role": "user", "content": "latest"}],
                   "tools": [{"type": "function", "function": {"name": "read"}}],
                   "temperature": 0.7, "max_tokens": 400}
        original = copy.deepcopy(payload)
        submitted = []

        async def compress(request_payload, client, context_size, **options):
            submitted.append(request_payload)
            result = copy.deepcopy(request_payload)
            result["messages"][1:3] = [{"role": "assistant", "content": "摘要"}]
            return result, {"state": "compressed", "original_tokens": 38000,
                            "prompt_tokens": 6000, "saved_tokens": 32000}

        self.compactor.prepare.side_effect = compress
        # The thread's own turns are on record for the text scope, which is what the prompt filter
        # (mode isolation) compares against: without them the "older" exchange counts as another
        # mode's and is dropped before the compressor ever sees it.
        key = module.scoped_conversation("chat-1")
        for entry in payload["messages"]:
            if entry.get("role") == "user":
                module.ledger.append_turn(key, "user", entry["content"], mode="text")
        result = await self.post_chat(json=payload, headers={"X-Conversation-ID": "chat-1"})
        self.assertEqual(result.status_code, 200)
        forwarded = json.loads(self.requests[0].content)
        self.assertEqual(forwarded["messages"][1]["content"], "摘要")
        self.assertEqual(forwarded["messages"][-1], payload["messages"][-1])
        self.assertEqual(forwarded["tools"], payload["tools"])
        self.assertEqual(forwarded["max_tokens"], 400)
        self.assertEqual(payload, original)
        self.assertEqual(submitted[0], original)
        self.assertEqual(result.headers["x-qwen-context-state"], "compressed")
        self.assertEqual(result.headers["x-qwen-context-original-tokens"], "38000")
        self.assertEqual(result.headers["x-qwen-context-effective-tokens"], "6000")
        self.assertEqual(result.headers["x-qwen-context-saved-tokens"], "32000")
        self.assertEqual(self.requests[0].headers["x-conversation-id"], "chat-1")
        self.assertEqual(self.manager.active_requests, 0)

    async def test_compression_header_and_mode_contract(self):
        for setting, force, enabled in ((None, False, True), ("auto", False, True),
                                         ("OFF", False, False), ("force", True, True)):
            with self.subTest(setting=setting):
                headers = {"X-Conversation-ID": "separate-conversation"}
                if setting is not None:
                    headers["X-Qwen-Context-Compression"] = setting
                response = await self.post_chat(headers=headers)
                self.assertEqual(response.status_code, 200)
                args, options = self.compactor.prepare.await_args
                self.assertIs(args[1], self.manager.client)
                # 窗口来自 ledger_window()（正在服务的后端），**不是** settings['context_size']（49152）。
                # 这条断言从前写的是 49152，等于把这个 bug 当成契约钉住了：压缩器按 8192 判定时，
                # 4.3k 的对话也会被判"装不下"，摘要还被压到 256 tokens 截断。
                self.assertEqual(args[2], module.ledger_window())
                # 随包发布的 27B 是三元 Bonsai 2，窗口 32768（不是 settings.json 里那个 49152，
                # 也不是从前 9B 的 131072）：这个数字属于模型，谁被服务就以谁为准。
                self.assertEqual(args[2], 32768)
                # The compressor is scoped to the mode's own thread, like the history it summarises.
                self.assertEqual(options, {"scope": "text·separate-conversation", "mode": "text",
                                           "force": force, "enabled": enabled})
                self.assertNotIn("x-qwen-context-compression", self.requests[-1].headers)
        self.manager.settings["context_compression_enabled"] = False
        await self.post_chat(headers={"X-Qwen-Context-Compression": "force"})
        self.assertFalse(self.compactor.prepare.await_args.kwargs["enabled"])
        self.manager.mode = "vision"
        await self.post_chat()
        self.assertEqual(self.compactor.prepare.await_args.kwargs["mode"], "vision")

    async def test_prefill_zero_aliases_skip_compaction_and_resume_registration(self):
        for key in ("n_predict", "max_tokens", "max_completion_tokens"):
            with self.subTest(key=key):
                response = await self.post_chat(json={"messages": [], "stream": True, key: 0},
                                                headers={"X-Conversation-ID": "prefill"})
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("prefill", module.pending_context_streams)
        self.compactor.prepare.assert_not_awaited()
        # False is not the integer zero; malformed values must not bypass preparation.
        await self.post_chat(json={"messages": [], "n_predict": False})
        self.compactor.prepare.assert_awaited_once()

    async def test_other_api_paths_do_not_trigger_compaction(self):
        for path in ("/completion", "/v1/responses", "/tools", "/tokenize"):
            response = await self.client.post(path, json={"messages": []})
            self.assertEqual(response.status_code, 200)
        self.compactor.prepare.assert_not_awaited()
        for path in ("/chat/completions", "/v1/chat/completions/"):
            response = await self.client.post(path, json={"messages": []})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.compactor.prepare.await_count, 2)

    async def test_compaction_blocks_mode_switch_and_releases_after_generation(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def prepare(payload, *args, **options):
            entered.set()
            await release.wait()
            return payload, {"state": "compressed"}

        self.compactor.prepare.side_effect = prepare
        generation = self.start(self.post_chat())
        await asyncio.wait_for(entered.wait(), 2)
        self.assertEqual(self.manager.active_requests, 1)
        self.assertEqual((await self.client.post("/local/mode", json={"mode": "vision"})).status_code, 409)
        self.assertEqual((await self.client.post("/local/shutdown")).status_code, 409)
        self.assertEqual(self.requests, [])
        release.set()
        self.assertEqual((await generation).status_code, 200)
        self.assertEqual(self.manager.active_requests, 0)

    async def test_compaction_error_is_explained_and_releases_request_and_resume(self):
        self.compactor.prepare.side_effect = CompactionError(400, "最新消息过长，请分段发送。")
        result = await self.post_chat(json={"messages": [], "stream": True},
                                      headers={"X-Conversation-ID": "failed"})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(result.json()["error"], {"message": "最新消息过长，请分段发送。",
                                                 "type": "context_compaction_error"})
        self.assertEqual(result.headers["x-qwen-context-state"], "error")
        self.assertEqual(self.manager.active_requests, 0)
        self.assertEqual(module.pending_context_streams, {})
        self.assertEqual(self.requests, [])

    async def test_transport_error_during_compaction_releases_request(self):
        self.compactor.prepare.side_effect = httpx.ReadError("offline")
        response = await self.post_chat()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.manager.active_requests, 0)

    async def test_unexpected_error_releases_request(self):
        self.compactor.prepare.side_effect = RuntimeError("synthetic bug")
        with self.assertRaisesRegex(RuntimeError, "synthetic bug"):
            await self.post_chat(json={"messages": [], "stream": True},
                                  headers={"X-Conversation-ID": "failed"})
        self.assertEqual(self.manager.active_requests, 0)
        self.assertEqual(module.pending_context_streams, {})

    async def test_cancelling_compaction_cancels_work_and_releases_pending(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def pending(*args, **options):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.compactor.prepare.side_effect = pending
        generation = self.start(self.post_chat(json={"messages": [], "stream": True},
                                               headers={"X-Conversation-ID": "cancelled"}))
        await asyncio.wait_for(entered.wait(), 2)
        generation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await generation
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.manager.active_requests, 0)
        self.assertEqual(module.pending_context_streams, {})
        self.assertEqual(self.requests, [])

    async def test_client_disconnect_during_main_prefill_cancels_upstream(self):
        entered, disconnected, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def upstream(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.backend(upstream)
        raw = json.dumps({"messages": [{"role": "user", "content": "continue"}], "stream": True}).encode()
        first = True
        sent = []

        async def receive():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": raw, "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                 "http_version": "1.1", "method": "POST", "scheme": "http",
                 "path": "/v1/chat/completions", "raw_path": b"/v1/chat/completions",
                 "query_string": b"", "root_path": "", "server": ("127.0.0.1", 8081),
                 "client": ("127.0.0.1", 42000),
                 "headers": [(b"host", b"127.0.0.1:8081"), (b"content-type", b"application/json"),
                             (b"x-conversation-id", b"disconnect-prefill")]}
        generation = self.start(module.app(scope, receive, send))
        await asyncio.wait_for(entered.wait(), 2)
        self.assertEqual(self.manager.active_requests, 1)
        disconnected.set()
        await asyncio.wait_for(generation, 2)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.manager.active_requests, 0)
        self.assertEqual(module.pending_context_streams, {})
        self.assertEqual(next(item["status"] for item in sent if item["type"] == "http.response.start"), 499)

    async def test_native_resume_waits_for_summary_and_main_upstream_headers(self):
        preparing, prepared = asyncio.Event(), asyncio.Event()
        main_entered, main_headers = asyncio.Event(), asyncio.Event()
        stream_entered, stream_finish = asyncio.Event(), asyncio.Event()
        main_wire = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'

        async def prepare(payload, *args, **options):
            preparing.set()
            await prepared.wait()
            return payload, {"state": "compressed"}

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                stream_entered.set()
                await stream_finish.wait()
                yield main_wire

        async def upstream(request):
            self.requests.append(request)
            if request.method == "POST":
                main_entered.set()
                await main_headers.wait()
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())
            return httpx.Response(200, content=b"resumed", headers={"content-type": "text/plain"})

        self.compactor.prepare.side_effect = prepare
        self.backend(upstream)
        generation = self.start(self.post_chat(json={"messages": [], "stream": True},
                                               headers={"X-Conversation-ID": "same-id"}))
        await asyncio.wait_for(preparing.wait(), 2)
        resume = self.start(self.client.get("/v1/stream?conv_id=same-id"))
        other = await self.client.get("/v1/stream?conv_id=other-id")
        self.assertEqual(other.status_code, 200)
        await asyncio.sleep(0.02)
        self.assertFalse(resume.done())
        self.assertEqual(len(self.requests), 1)  # Only the unrelated conversation reached upstream.
        prepared.set()
        await asyncio.wait_for(main_entered.wait(), 2)
        await asyncio.sleep(0.02)
        self.assertFalse(resume.done())
        main_headers.set()
        await asyncio.wait_for(stream_entered.wait(), 2)
        self.assertEqual((await asyncio.wait_for(resume, 2)).content, b"resumed")
        self.assertEqual(self.manager.active_requests, 1)
        stream_finish.set()
        self.assertEqual((await generation).content, main_wire)
        self.assertEqual(self.manager.active_requests, 0)
        self.assertEqual(module.pending_context_streams, {})

    async def test_cancelled_main_request_unblocks_native_resume(self):
        entered = asyncio.Event()

        async def prepare(*args, **options):
            entered.set()
            await asyncio.Event().wait()

        self.compactor.prepare.side_effect = prepare
        generation = self.start(self.post_chat(json={"messages": [], "stream": True},
                                               headers={"X-Conversation-ID": "cancelled"}))
        await asyncio.wait_for(entered.wait(), 2)
        resume = self.start(self.client.get("/v1/stream?conv_id=cancelled"))
        await asyncio.sleep(0.02)
        self.assertFalse(resume.done())
        generation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await generation
        self.assertEqual((await asyncio.wait_for(resume, 2)).status_code, 200)
        self.assertEqual(self.manager.active_requests, 0)
        self.assertEqual(module.pending_context_streams, {})

    async def test_stream_bytes_unchanged_and_upstream_closed_once(self):
        wire = ('event: progress\ndata: {"progress":0.5}\n\n'
                'data: {"choices":[{"delta":{"content":"你好"}}]}\n\n'
                'data: {"choices":[],"usage":{"prompt_tokens":17,"completion_tokens":2}}\n\n'
                'data: [DONE]\n\n').encode("utf-8")
        closed = []

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for start in range(0, len(wire), 5):
                    yield wire[start:start + 5]

            async def aclose(self):
                closed.append(True)

        self.backend(lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream()))
        response = await self.post_chat(json={"messages": [], "stream": True})
        self.assertEqual(response.content, wire)
        self.assertEqual(closed, [True])
        self.assertEqual(self.manager.active_requests, 0)

    async def test_context_metadata_endpoints_and_length_validation(self):
        config = await self.client.get("/local/context/config")
        self.assertEqual(config.status_code, 200)
        # The window is asserted through the same rule the launcher uses, so this test says
        # "the endpoint reports the window that will be launched" rather than hardcoding a
        # number that belongs to a profile and drifts whenever one is retuned.
        body = config.json()
        self.assertEqual({key: body[key] for key in ("enabled", "trigger_ratio", "reserve_tokens")},
                         {"enabled": True, "trigger_ratio": 0.75, "reserve_tokens": 4096})
        self.assertEqual(body["context_size"], module.expected_context(self.manager.settings))
        self.assertEqual(config.headers["cache-control"], "no-store")
        status = await self.client.get("/local/context/status", params={"conversation_id": "会话-1"})
        body = status.json()
        # The compactor's own payload is unchanged, and the shared conversation's meter rides
        # alongside it: the chat app reads its fields by name, so nothing it reads moved.
        self.assertEqual({key: body[key] for key in ("state", "summary_count")},
                         {"state": "idle", "summary_count": 0})
        self.assertEqual(body["conversation"]["conversation_id"], module.scoped_conversation("会话-1"))
        self.assertIn("remaining_tokens", body["conversation"])
        self.compactor.get_status.assert_called_once_with(module.scoped_conversation("会话-1"))
        self.assertEqual(status.headers["cache-control"], "no-store")
        rejected = await self.client.get("/local/context/status", params={"conversation_id": "x" * 513})
        self.assertEqual(rejected.status_code, 400)
        rejected = await self.post_chat(headers={"X-Conversation-ID": "x" * 513})
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(self.manager.active_requests, 0)
        self.compactor.prepare.assert_not_awaited()

    async def test_voice_window_is_reported_before_the_switch(self):
        """语音页必须报"要用的那个窗口"，而不是"现在正在服务的那个窗口"，档位跟着当前选择走。

        这里选三元 27B 当例子：它当聊天档位时是 32768，当语音档位时是 8192。第一轮语音之前后端
        可能还停在文字窗口，所以面板问的必须是"语音要用的窗口"，否则主人会以为语音上下文被悄悄改了。
        2026-09-24 修正：档位不再锁死（9B 也能语音，窗口 40960），所以这里同时钉住"报的是当前选中的
        那一份"与 `locked=False`（客户端就是靠它放行语音标签的）。
        """
        self.manager.settings["model_profile"] = "qwen3.8-27b"
        settings = {**self.manager.settings, "model_profile": "qwen3.8-27b"}
        settings.pop("context_key", None)
        self.assertEqual(module.voice_context(settings), 8192)

        config = await self.client.get("/local/voice/config")
        body = config.json()
        self.assertEqual(body["profile"], "qwen3.8-27b")
        self.assertTrue(body["supported"])
        self.assertFalse(body["locked"], "档位不再固定，锁定会让客户端把语音标签变灰")
        self.assertTrue(body["context_locked"])
        self.assertEqual(body["context_size"], 8192)

        # A scratch ledger, so this test cannot write a conversation into the owner's history.
        with tempfile.TemporaryDirectory(prefix="qwen-voice-window-") as stage:
            scratch = ConversationLedger(Path(stage) / "history.json", context_size=8192)
            with patch.object(module, "ledger", scratch):
                serving = (await self.client.get("/local/conversation",
                            params={"conversation_id": "窗口-1"})).json()["meter"]
                voice = (await self.client.get("/local/conversation",
                           params={"conversation_id": "窗口-1",
                                   "window_kind": "voice"})).json()["meter"]
        # Both are honest, and they are different numbers: one describes the backend in front of
        # the owner right now, the other the window a spoken turn is locked to.
        self.assertEqual(serving["window_kind"], "serving")
        self.assertEqual(serving["context_size"], module.ledger_window())
        self.assertEqual(voice["window_kind"], "voice")
        self.assertEqual(voice["context_size"], 8192)
        self.assertNotEqual(voice["context_size"], serving["context_size"])

    async def test_context_assets_are_served_and_injected_in_cached_shell(self):
        with tempfile.TemporaryDirectory(prefix="qwen-context-assets-") as directory:
            root = Path(directory)
            (root / "web/native").mkdir(parents=True)
            (root / "web/native/index.html").write_text("<html><head></head><body>chat</body></html>", encoding="utf-8")
            (root / "web/context-control.js").write_text("window.contextReady = true;", encoding="utf-8")
            (root / "web/context-control.css").write_text(".context-control { display: block; }", encoding="utf-8")
            with patch.object(module, "ROOT", root):
                for path in ("/local/context-control.js?v=1", "/local/context-control.css?v=1"):
                    response = await self.client.get(path)
                    self.assertEqual(response.status_code, 200)
                    # Asset URLs stay the same across deployments, so the browser
                    # must revalidate instead of serving a cached copy.
                    self.assertIn("no-store", response.headers["cache-control"])
                self.assertEqual((await self.client.get("/local/context-secret.json")).status_code, 404)
                shell = await self.client.get("/")
                # The cache-busting query is a deployment detail that changes on purpose,
                # so the check is that both assets are injected — not which number they
                # carry (pinning it made an unrelated version bump look like a regression).
                self.assertIn('/local/context-control.js?v=', shell.text)
                self.assertIn('/local/context-control.css?v=', shell.text)
                self.assertEqual(shell.headers["cache-control"], "no-store")
        self.assertEqual(self.requests, [])


class ConnectedOperationTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_cancels_inflight_work_and_returns_499(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        request = Mock()

        async def receive():
            await entered.wait()
            return {"type": "http.disconnect"}

        request.receive = receive

        async def pending():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with self.assertRaises(HTTPException) as raised:
            await asyncio.wait_for(module.await_while_connected(request, pending()), 2)
        self.assertEqual(raised.exception.status_code, 499)
        self.assertTrue(cancelled.is_set())

    async def test_success_and_failure_are_returned_without_live_monitor_tasks(self):
        monitor_entered, monitor_cancelled = asyncio.Event(), asyncio.Event()
        baseline = asyncio.all_tasks()

        async def receive():
            monitor_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                monitor_cancelled.set()

        request = Mock()
        request.receive = receive

        async def work():
            await monitor_entered.wait()
            return "finished"

        self.assertEqual(await module.await_while_connected(request, work()), "finished")
        self.assertTrue(monitor_cancelled.is_set())
        self.assertEqual(asyncio.all_tasks() - baseline, set())

        async def failed():
            raise ValueError("synthetic failure")

        with self.assertRaisesRegex(ValueError, "synthetic failure"):
            await module.await_while_connected(request, failed())
        self.assertEqual(asyncio.all_tasks() - baseline, set())

    async def test_external_cancellation_awaits_work_and_monitor_cleanup(self):
        work_entered, monitor_entered = asyncio.Event(), asyncio.Event()
        work_closed, monitor_closed = asyncio.Event(), asyncio.Event()
        baseline = asyncio.all_tasks()

        async def work():
            work_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                work_closed.set()

        async def receive():
            monitor_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                monitor_closed.set()

        request = Mock()
        request.receive = receive
        operation = asyncio.create_task(module.await_while_connected(request, work()))
        await asyncio.wait_for(work_entered.wait(), 2)
        await asyncio.wait_for(monitor_entered.wait(), 2)
        operation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await operation
        self.assertTrue(work_closed.is_set())
        self.assertTrue(monitor_closed.is_set())
        self.assertEqual(asyncio.all_tasks() - baseline, set())


if __name__ == "__main__":
    unittest.main()
