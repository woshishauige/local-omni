"""No model, production files, or ports: context semantics and failure boundaries."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx

import context_compaction as module
from context_compaction import CompactionError, ContextCompactor, SUMMARY_MARKER
from usage_store import UsageStore


def history(turns=7, size=900):
    messages = [{"role": "system", "content": "Keep exact identifiers; original system."}]
    for turn in range(turns):
        messages.append({"role": "user", "content": f"turn-{turn}:" + "x" * size})
        if turn != turns - 1:
            messages.append({"role": "assistant", "content": f"done-{turn}"})
    return {"model": "test-model", "messages": messages, "max_tokens": 512,
            "temperature": 0.8, "top_p": 0.91, "stream": True,
            "chat_template_kwargs": {"enable_thinking": True}}


class FakeBackend:
    def __init__(self):
        self.requests = []
        self.generations = []
        self.summary_text = "目标保持；编号 12345；已完成前述工作，保留后续约束。"
        self.finish_reason = "stop"
        self.truncate_generations = 0   # 前 N 次摘要按 length 返回：模拟被 max_tokens 截断
        self.fail_count = False
        self.fail_generate = False
        self.fail_generation_number = None
        self.generate_started = asyncio.Event()
        self.paused = asyncio.Event()
        self.release = None
        self.pause_generation_number = None

    async def handle(self, request):
        body = json.loads(request.content)
        self.requests.append((request.url.path, body))
        if request.url.path == "/apply-template":
            if self.fail_count:
                return httpx.Response(500, json={"error": "no template"})
            # One synthetic token per character, with roles/tools/generation prompt
            # charged too: test decisions, not a substitute for live token tests.
            prompt = "".join(str(message.get("content") or "") + str(message.get("tool_calls") or "")
                             + message["role"] + "|" for message in body["messages"])
            prompt += json.dumps(body.get("tools", [])) + "assistant|"
            return httpx.Response(200, json={"prompt": prompt})
        if request.url.path == "/tokenize":
            if body.get("add_special") is not False or body.get("parse_special") is not True:
                raise AssertionError("Template special tokens must not be added twice")
            return httpx.Response(200, json={"tokens": [1] * len(body["content"])})
        if request.url.path == "/v1/chat/completions":
            self.generations.append(body)
            self.generate_started.set()
            if self.release and (self.pause_generation_number is None
                                 or len(self.generations) == self.pause_generation_number):
                self.paused.set()
                await self.release.wait()
            if self.fail_generate or len(self.generations) == self.fail_generation_number:
                return httpx.Response(503, json={"error": "sensitive raw error"})
            reason = self.finish_reason
            if len(self.generations) <= self.truncate_generations:
                reason = "length"
            return httpx.Response(200, json={"choices": [{"finish_reason": reason,
                "message": {"role": "assistant", "content": self.summary_text}}],
                "usage": {"prompt_tokens": 250, "completion_tokens": 20, "total_tokens": 270}})
        raise AssertionError("Unexpected endpoint " + request.url.path)


class ContextCompactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="qwen-context-unit-")
        self.store = UsageStore(Path(self.directory.name) / "usage.sqlite3")
        self.backend = FakeBackend()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.backend.handle))
        self.compactor = ContextCompactor("http://127.0.0.1:8082", lambda: self.store)

    async def asyncTearDown(self):
        await self.client.aclose()
        self.directory.cleanup()

    async def prepare(self, payload=None, **kwargs):
        return await self.compactor.prepare(payload or history(), self.client, 8192, **kwargs)

    async def test_single_turn_no_network_and_disabled_no_changes(self):
        payload = history(turns=1)
        result, status = await self.prepare(payload)
        self.assertIs(result, payload)
        self.assertEqual(status["state"], "unchanged")
        result, status = await self.prepare(enabled=False)
        self.assertEqual(status["state"], "disabled")
        self.assertFalse(status["enabled"])
        self.assertEqual(self.backend.requests, [])

    async def test_short_history_not_compacted_and_parameters_unchanged(self):
        payload = history(turns=2, size=10)
        original = deepcopy(payload)
        result, status = await self.prepare(payload)
        self.assertIs(result, payload)
        self.assertEqual(payload, original)
        self.assertEqual(status["state"], "unchanged")
        self.assertFalse(self.backend.generations)
        self.assertEqual([path for path, _ in self.backend.requests], ["/apply-template", "/tokenize"])

    async def test_actual_rewrite_preserves_tail_and_all_sampling(self):
        payload = history()
        original = deepcopy(payload)
        result, status = await self.prepare(payload, scope="private-session")
        self.assertEqual(status["state"], "compressed")
        self.assertGreater(status["saved_tokens"], 0)
        self.assertGreaterEqual(status["preserved_turns"], 1)
        self.assertLessEqual(status["preserved_turns"], 4)
        self.assertEqual(payload, original)
        self.assertEqual({k: v for k, v in result.items() if k != "messages"},
                         {k: v for k, v in payload.items() if k != "messages"})
        self.assertEqual(result["messages"][0], original["messages"][0])
        self.assertEqual(result["messages"][1]["role"], "assistant")
        self.assertTrue(result["messages"][1]["content"].startswith(SUMMARY_MARKER))
        retained = status["preserved_turns"] * 2 - 1
        self.assertEqual(result["messages"][-retained:], original["messages"][-retained:])
        summary_request = self.backend.generations[0]
        self.assertFalse(summary_request["stream"])
        self.assertEqual(summary_request["temperature"], 0)
        self.assertFalse(summary_request["chat_template_kwargs"]["enable_thinking"])
        self.assertTrue(payload["chat_template_kwargs"]["enable_thinking"])
        self.assertNotIn("tools", summary_request)
        serialized = json.dumps(self.compactor.get_status("private-session"), ensure_ascii=False)
        self.assertNotIn("turn-", serialized)
        self.assertNotIn(self.backend.summary_text, serialized)
        self.assertNotIn("private-session", serialized)

    async def test_summary_budget_scales_with_the_serving_window(self):
        """摘要输出预算必须跟着**正在服务的窗口**走，而且要给够。

        两段真机教训叠在这里：
        1. 窗口若被误传成 settings.json 的 8192，`min(1536, max(256, 8192//32))` = 256，
           摘要写不完就被截断，记账行留下 `out=256` 与"未完整结束，未替换原消息"；
        2. 换成真实窗口 32768 后 `32768//32` 仍然只有 1024——25,845 token 的历史摘要还是写不完，
           于是**最需要压缩的那一次** saved_tokens=0。现在按 //16 给（32k → 2048），
           真截断时 `_generate_summary` 还会加长篇幅重试一次（天花板 3072）。
        """
        groups = [[{"role": "user", "content": "早先的问题"}], [{"role": "user", "content": "继续"}]]
        for window, expected in ((8192, 512), (32768, 2048), (40960, 2048), (131072, 2048),
                                 (262144, 2048)):
            request = self.compactor._summary_payload({"messages": []}, window, groups, "")
            self.assertEqual(request["max_tokens"], expected,
                             f"窗口 {window} 的摘要预算应为 {expected}")
            self.assertIn("篇幅上限约 %d 个汉字" % expected, request["messages"][0]["content"])

    async def test_truncated_summary_is_retried_with_more_room(self):
        """摘要被 max_tokens 截断时，先加长篇幅重试，而不是立刻丢掉整次压缩。

        真机：2026-10-28 那次 400 之前，压缩器压过一次、摘要写不完、原样退回 69 条消息，
        saved_tokens=0，然后在 37,679 token 上撞窗口。这里让假后端第一次回 length、
        第二次回正常摘要，要求最终压缩成功且只重新生成一次。
        """
        self.backend.truncate_generations = 1
        payload = history()
        result, status = await self.prepare(payload)
        self.assertEqual(status["state"], "compressed")
        self.assertNotEqual(result, payload)
        self.assertEqual(len(self.backend.generations), 2)
        self.assertGreater(self.backend.generations[1]["max_tokens"],
                           self.backend.generations[0]["max_tokens"])

    async def test_usage_row_names_the_serving_model_not_a_hardcoded_27b(self):
        """"上下文压缩"的记账模型名来自调用方（正在服务的那份权重），不是写死的 27B。

        真机踩到：跑 9B 时压缩记录显示 `qwen3.8-27b-local`，看起来像用错了模型。
        """
        compactor = ContextCompactor("http://127.0.0.1:8082", lambda: self.store,
                                     label_getter=lambda: "Qwen3.5-9B-Uncensored-Q6_K")
        # force：窗口是 131072 时阈值很高，小历史默认不会触发压缩，也就没有记账行。
        # 去掉 payload 自带的 model（生产里聊天应用不发这个字段），走的就是"谁的标签"这条兜底路径。
        payload = history()
        payload.pop("model", None)
        await compactor.prepare(payload, self.client, 131072, force=True)
        rows = self.store.query({})["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "Qwen3.5-9B-Uncensored-Q6_K")
        self.assertNotIn("27b", rows[0]["model"].lower())

    async def test_summary_usage_is_independent_and_content_free(self):
        await self.prepare()
        data = self.store.query({})
        self.assertEqual(data["total"], 1)
        row = data["rows"][0]
        self.assertEqual(row["source"], "上下文压缩")
        self.assertEqual(row["endpoint"], "/local/context/compact")
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["input_tokens"], 250)
        self.assertEqual(row["output_tokens"], 20)
        self.assertNotIn("turn-", json.dumps(data))
        self.assertNotIn(self.backend.summary_text, json.dumps(data, ensure_ascii=False))

    async def test_exact_prefix_cache_reuses_and_newest_message_can_change(self):
        payload = history()
        first, _ = await self.prepare(payload, scope="A")
        again, status = await self.prepare(deepcopy(payload), scope="A")
        self.assertEqual(status["state"], "cached")
        self.assertEqual(again, first)
        self.assertEqual(len(self.backend.generations), 1)
        payload["messages"][-1]["content"] = "Different latest request"
        changed, status = await self.prepare(payload, scope="A")
        self.assertEqual(status["state"], "cached")
        self.assertEqual(changed["messages"][-1], payload["messages"][-1])
        self.assertEqual(len(self.backend.generations), 1)

    async def test_scope_history_and_instruction_changes_never_share_summary(self):
        payload = history()
        await self.prepare(payload, scope="A")
        await self.prepare(payload, scope="B")
        self.assertEqual(len(self.backend.generations), 2)
        payload["messages"][1]["content"] += "corrected early fact"
        await self.prepare(payload, scope="A")
        self.assertEqual(len(self.backend.generations), 3)
        payload["messages"][0]["content"] += "new instruction"
        await self.prepare(payload, scope="A")
        self.assertEqual(len(self.backend.generations), 4)
        payload["tools"] = [{"type": "function", "function": {"name": "probe"}}]
        await self.prepare(payload, scope="A")
        self.assertEqual(len(self.backend.generations), 5)

    async def test_cache_can_merge_new_old_turns_and_bounds_memory(self):
        payload = history()
        await self.prepare(payload, scope="A")
        payload["messages"].append({"role": "assistant", "content": "previous response"})
        for index in range(5):
            payload["messages"].extend([{"role": "user", "content": "new-" + "z" * 1000},
                                         {"role": "assistant", "content": "new done"}])
        result, status = await self.prepare(payload, scope="A")
        self.assertEqual(status["state"], "compressed")
        self.assertTrue(status["cache_hit"])
        self.assertEqual(len(self.backend.generations), 2)
        summary_input = json.loads(self.backend.generations[-1]["messages"][-1]["content"])
        self.assertEqual(summary_input["previous_summary"], self.backend.summary_text)
        self.assertFalse(any("turn-0:" in str(item) for item in summary_input["history"]))
        self.assertLess(status["prompt_tokens"], 8192 - 512)
        with patch.object(module, "MAX_CACHE_ENTRIES", 1):
            self.compactor._prune()
            self.assertEqual(len(self.compactor._cache), 1)

    async def test_force_compacts_older_turn_below_threshold(self):
        result, status = await self.prepare(history(turns=2, size=700), force=True)
        self.assertEqual(status["state"], "compressed")
        self.assertEqual(status["compressed_turns"], 1)
        self.assertEqual(status["preserved_turns"], 1)

    async def test_usable_cached_summary_retained_when_latest_turn_nears_limit(self):
        payload = history(turns=2, size=700)
        await self.prepare(payload, scope="large-tail", force=True)
        payload["messages"][-1]["content"] = "t" * 6500
        result, status = await self.prepare(payload, scope="large-tail")
        self.assertEqual(status["state"], "cached")
        self.assertGreater(status["original_tokens"], 8192 - 512 - 512)
        self.assertGreater(status["prompt_tokens"], status["threshold_tokens"])
        self.assertLess(status["prompt_tokens"], 8192 - 512 - 512)
        self.assertEqual(result["messages"][-1], payload["messages"][-1])
        self.assertEqual(len(self.backend.generations), 1)

    async def test_chinese_summary_input_stays_readable_and_cache_expires(self):
        payload = history()
        payload["messages"][1]["content"] += "编号准确保留"
        await self.prepare(payload, scope="Chinese")
        self.assertIn("编号准确保留", self.backend.generations[-1]["messages"][-1]["content"])
        self.assertNotIn("\\u7f16", self.backend.generations[-1]["messages"][-1]["content"])
        with patch.object(module, "CACHE_TTL_SECONDS", 0):
            await self.prepare(payload, scope="Chinese")
        self.assertEqual(len(self.backend.generations), 2)

    async def test_media_and_mid_system_skipped_without_token_requests(self):
        for content in ([{"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}],
                        [{"type": "input_audio", "input_audio": {"data": "secret"}}]):
            payload = history()
            payload["messages"][1]["content"] = content
            result, status = await self.prepare(payload)
            self.assertIs(result, payload)
            self.assertEqual(status["state"], "skipped")
        payload = history()
        payload["messages"].insert(3, {"role": "developer", "content": "later constraint"})
        _, status = await self.prepare(payload)
        self.assertEqual(status["state"], "skipped")
        self.assertFalse(self.backend.requests)

    async def test_unhashable_role_and_content_types_skip_without_500(self):
        for invalid in ({"type": "user"}, ["user"], None, 1):
            payload = history()
            payload["messages"][1]["role"] = invalid
            result, status = await self.prepare(payload)
            self.assertIs(result, payload)
            self.assertEqual(status["state"], "skipped")
            payload = history()
            payload["messages"][1]["content"] = [{"type": invalid, "text": "sensitive"}]
            result, status = await self.prepare(payload)
            self.assertIs(result, payload)
            self.assertEqual(status["state"], "skipped")
        self.assertFalse(self.backend.requests)

    async def test_tool_chain_is_summarized_or_kept_whole(self):
        payload = history()
        payload["messages"][2:3] = [{"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function", "function": {"name": "probe", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "code 321"},
            {"role": "assistant", "content": "confirmed 321"}]
        result, status = await self.prepare(payload)
        self.assertEqual(status["state"], "compressed")
        input_history = json.loads(self.backend.generations[0]["messages"][-1]["content"])["history"]
        tool = next(item for item in input_history if item["role"] == "tool")
        self.assertEqual(tool["tool_call_id"], "call_1")
        self.assertTrue(any(item.get("tool_calls", [{}])[0].get("id") == "call_1" for item in input_history))
        self.assertFalse(any(item["role"] == "tool" for item in result["messages"]))
        payload["messages"].append({"role": "assistant", "content": None, "tool_calls": [{
            "id": "pending", "type": "function", "function": {"name": "last", "arguments": "{}"}}]})
        result, status = await self.prepare(payload)
        self.assertEqual(result["messages"][-1], payload["messages"][-1])

    async def test_orphaned_tool_result_and_open_old_chain_skip(self):
        payload = history()
        payload["messages"][2] = {"role": "tool", "tool_call_id": "orphan", "content": "data"}
        _, status = await self.prepare(payload)
        self.assertEqual(status["state"], "skipped")
        payload = history()
        payload["messages"][2] = {"role": "assistant", "tool_calls": [{"id": "open"}], "content": None}
        _, status = await self.prepare(payload)
        self.assertEqual(status["state"], "skipped")
        self.assertFalse(self.backend.requests)

    async def test_failed_summary_falls_back_without_losing_original(self):
        self.backend.fail_generate = True
        payload = history()
        result, status = await self.prepare(payload)
        self.assertIs(result, payload)
        self.assertEqual(status["state"], "fallback")
        self.assertEqual(self.store.query({})["rows"][0]["status"], "error")
        self.assertNotIn("sensitive", json.dumps(status))
        self.assertFalse(self.compactor._cache)

    async def test_confirmed_overflow_failure_is_clear_400(self):
        self.backend.fail_generate = True
        payload = history(turns=8, size=1000)
        with self.assertRaises(CompactionError) as raised:
            await self.prepare(payload)
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("原历史未删除", raised.exception.message)
        self.assertEqual(self.compactor.get_status()["state"], "error")

    async def test_truncated_summary_never_replaces_history(self):
        self.backend.finish_reason = "length"
        payload = history()
        result, status = await self.prepare(payload)
        self.assertIs(result, payload)
        self.assertEqual(status["state"], "fallback")
        self.assertEqual(self.store.query({})["rows"][0]["status"], "incomplete")

    async def test_tokenizer_failure_is_fail_open(self):
        self.backend.fail_count = True
        payload = history()
        result, status = await self.prepare(payload)
        self.assertIs(result, payload)
        self.assertEqual(status["state"], "skipped")
        self.assertFalse(self.backend.generations)

    async def test_latest_turn_too_large_not_truncated_or_sent_to_summary(self):
        payload = history()
        payload["messages"][-1]["content"] = "z" * 9000
        with self.assertRaises(CompactionError):
            await self.prepare(payload)
        self.assertFalse(self.backend.generations)
        self.assertEqual(len(payload["messages"][-1]["content"]), 9000)

    async def test_old_prefix_larger_than_summary_window_not_sent(self):
        payload = history(turns=3, size=20)
        payload["messages"][1]["content"] = "z" * 9000
        with self.assertRaises(CompactionError):
            await self.prepare(payload)
        self.assertFalse(self.backend.generations)

    async def test_long_history_compacts_in_bounded_complete_turn_chunks(self):
        payload = history(turns=20, size=1100)
        original = deepcopy(payload)
        result, status = await self.prepare(payload)
        self.assertEqual(status["state"], "compressed")
        self.assertEqual(payload, original)
        self.assertGreater(status["compression_calls"], 1)
        self.assertLessEqual(status["compression_calls"], 8)
        summarized = []
        for index, generation in enumerate(self.backend.generations):
            data = json.loads(generation["messages"][-1]["content"])
            self.assertEqual(data["previous_summary"], self.backend.summary_text if index else "")
            self.assertEqual(data["history"][0]["role"], "user")
            self.assertEqual(data["history"][-1]["role"], "assistant")
            summarized.extend(data["history"])
            # Every actual generation payload must be independently proven fit.
            self.assertLessEqual(await self.compactor._count(generation, self.client)
                                 + generation["max_tokens"] + 128, 8192)
        self.assertEqual(summarized, original["messages"][1:1 + status["compressed_turns"] * 2])
        self.assertEqual(result["messages"][-1], original["messages"][-1])
        self.assertEqual(self.store.query({})["total"], status["compression_calls"])
        self.assertEqual(self.store.query({})["summary"]["input_tokens"], 250 * status["compression_calls"])

    async def test_late_chunk_failure_does_not_publish_partial_summary(self):
        payload = history(turns=20, size=1100)
        original = deepcopy(payload)
        self.backend.fail_generation_number = 2
        with self.assertRaises(CompactionError):
            await self.prepare(payload)
        self.assertEqual(payload, original)
        self.assertFalse(self.compactor._cache)
        self.assertEqual(len(self.backend.generations), 2)
        self.assertEqual(sorted(row["status"] for row in self.store.query({})["rows"]), ["error", "success"])

    async def test_more_than_eight_chunks_stops_without_partial_commit(self):
        payload = history(turns=11, size=4200)
        with self.assertRaises(CompactionError) as raised:
            await self.prepare(payload)
        self.assertIn("8 批", raised.exception.message)
        self.assertEqual(len(self.backend.generations), 8)
        self.assertFalse(self.compactor._cache)
        self.assertEqual(self.store.query({})["total"], 8)

    async def test_cancel_later_chunk_finishes_usage_without_partial_cache(self):
        self.backend.release = asyncio.Event()
        self.backend.pause_generation_number = 2
        task = asyncio.create_task(self.prepare(history(turns=20, size=1100), scope="chunk-cancel"))
        await asyncio.wait_for(self.backend.paused.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.compactor._cache)
        self.assertEqual(self.compactor.get_status("chunk-cancel")["state"], "cancelled")
        self.assertEqual(sorted(row["status"] for row in self.store.query({})["rows"]), ["cancelled", "success"])

    async def test_output_reserve_respects_requested_limits_without_mutation(self):
        for field in ("max_completion_tokens", "max_tokens", "n_predict"):
            payload = history(turns=2, size=10)
            payload.pop("max_tokens")
            payload[field] = 1000
            _, status = await self.prepare(payload)
            self.assertEqual(status["output_reserve"], 1000)
            self.assertEqual(payload[field], 1000)
        for value in (None, -1, 0, True):
            payload = history(turns=2, size=10)
            payload["max_tokens"] = value
            _, status = await self.prepare(payload)
            self.assertEqual(status["output_reserve"], 4096)
            self.assertEqual(payload["max_tokens"], value)
        payload = history(turns=2, size=10)
        payload.update(max_completion_tokens=1000, max_tokens=2000, n_predict=3000)
        _, status = await self.prepare(payload)
        self.assertEqual(status["output_reserve"], 3000)

    async def test_same_scope_concurrency_only_generates_once(self):
        self.backend.release = asyncio.Event()
        first = asyncio.create_task(self.prepare(scope="shared"))
        await asyncio.wait_for(self.backend.generate_started.wait(), 1)
        second = asyncio.create_task(self.prepare(scope="shared"))
        await asyncio.sleep(0)
        self.backend.release.set()
        results = await asyncio.gather(first, second)
        self.assertEqual(len(self.backend.generations), 1)
        self.assertEqual([status["state"] for _, status in results], ["compressed", "cached"])

    async def test_generation_cancel_closes_usage_and_unlocks_scope(self):
        self.backend.release = asyncio.Event()
        task = asyncio.create_task(self.prepare(scope="cancel"))
        await asyncio.wait_for(self.backend.generate_started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.compactor.get_status("cancel")["state"], "cancelled")
        self.assertEqual(self.store.query({})["rows"][0]["status"], "cancelled")
        self.assertFalse(self.compactor._cache)
        self.backend.release.set()
        _, status = await self.prepare(scope="cancel")
        self.assertEqual(status["state"], "compressed")

    async def test_cancel_while_sqlite_begin_pending_leaves_no_running_row(self):
        original_begin = self.store.begin
        thread_started, thread_release = threading.Event(), threading.Event()

        def delayed(record):
            thread_started.set()
            thread_release.wait(2)
            return original_begin(record)

        with patch.object(self.store, "begin", delayed):
            task = asyncio.create_task(self.prepare(scope="cancel-begin"))
            await asyncio.to_thread(thread_started.wait, 1)
            task.cancel()
            thread_release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.store.query({})["rows"][0]["status"], "cancelled")
        self.assertFalse(self.backend.generations)

    async def test_telemetry_failure_does_not_fail_compaction(self):
        with patch.object(self.store, "begin", side_effect=OSError("database locked")):
            _, status = await self.prepare()
        self.assertEqual(status["state"], "compressed")


if __name__ == "__main__":
    unittest.main()
