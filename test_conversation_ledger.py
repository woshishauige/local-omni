"""One history and one context meter across typing, pictures and speech.

The three modes used to keep three separate ideas of the conversation: the chat app owned the
transcript and its meter, voice mode kept an array of its own turns, and a spoken turn was
therefore invisible to the written thread and never counted against the window. These tests
pin the ledger that replaces all three, and the gateway routes that expose it.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import conversation_ledger as ledger_module
import mode_server as module
from conversation_ledger import ConversationLedger, estimate_tokens


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qwen-ledger-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "conversation-history.json"
        self.ledger = ConversationLedger(self.path, context_size=8192, reserve_tokens=512)

    def test_a_conversation_identifier_is_reusable_and_never_empty(self):
        self.assertEqual(self.ledger.normalize_id(" 会话-1 "), "会话-1")
        # A caller with no identifier at all still gets a usable key for this turn, and two
        # such callers do not share one: filing them together would merge unrelated threads.
        self.assertTrue(self.ledger.normalize_id(""))
        self.assertNotEqual(self.ledger.normalize_id(None), self.ledger.normalize_id(""))
        self.assertEqual(len(self.ledger.normalize_id("x" * 500)), 200)

    def test_typing_and_speaking_land_in_the_same_thread(self):
        key = "conv-1"
        self.ledger.append_turn(key, "user", "把这张图片放大两倍", mode="text")
        self.ledger.append_turn(key, "assistant", "好的。", mode="text")
        # A spoken turn goes into the very same conversation, tagged with how it arrived.
        self.ledger.append_turn(key, "user", "刚才那句再说一遍", mode="voice")

        history = self.ledger.history(key)
        self.assertEqual([turn["role"] for turn in history], ["user", "assistant", "user"])
        self.assertEqual([turn["mode"] for turn in history], ["text", "text", "voice"])
        self.assertEqual(self.ledger.messages(key)[-1],
                         {"role": "user", "content": "刚才那句再说一遍"})

    def test_the_meter_counts_the_turns_when_nothing_was_measured(self):
        key = "conv-1"
        self.ledger.append_turn(key, "user", "一二三四五六七八九十", mode="text", tokens=40)
        self.ledger.append_turn(key, "assistant", "好的", mode="text", tokens=10)
        meter = self.ledger.meter(key)

        self.assertEqual(meter["used_tokens"], 50)
        self.assertEqual(meter["source"], "estimated")
        self.assertEqual(meter["usable_tokens"], 8192 - 512)
        self.assertEqual(meter["remaining_tokens"], 8192 - 512 - 50)
        self.assertEqual(meter["level"], "ok")

    def test_a_measured_prompt_wins_over_the_estimate(self):
        key = "conv-2"
        self.ledger.append_turn(key, "user", "短问题", mode="text")
        meter = self.ledger.meter(key, used_tokens=7000)
        self.assertEqual(meter["source"], "measured")
        self.assertEqual(meter["used_tokens"], 7000)
        self.assertEqual(meter["level"], "critical")

    def test_a_window_can_be_narrowed_for_a_mode_that_has_less_room(self):
        key = "conv-3"
        # Voice mode's own backend runs a small window; reporting the borrowed profile's
        # window would promise room the spoken model does not have.
        wide = self.ledger.meter(key, used_tokens=4000, window=32768)
        narrow = self.ledger.meter(key, used_tokens=5500, window=8192)
        self.assertEqual(wide["level"], "ok")
        self.assertEqual(narrow["level"], "tight")
        self.assertLess(narrow["remaining_tokens"], wide["remaining_tokens"])
        self.assertGreater(wide["used_ratio"], 0)
        self.assertLess(wide["used_ratio"], narrow["used_ratio"])

    def test_a_full_window_is_reported_as_full_not_as_negative_room(self):
        meter = self.ledger.meter("conv-4", used_tokens=99999)
        self.assertEqual(meter["remaining_tokens"], 0)
        self.assertEqual(meter["level"], "full")

    def test_observe_request_records_both_sides_of_a_written_turn(self):
        key = "conv-5"
        meter = self.ledger.observe_request(
            key,
            {"messages": [{"role": "system", "content": "sys"},
                          {"role": "user", "content": "问题"}], "max_tokens": 100},
            "/v1/chat/completions", mode="text", reply="回答", prompt_tokens=1234)

        history = self.ledger.history(key)
        self.assertEqual([turn["content"] for turn in history], ["问题", "回答"])
        self.assertEqual(meter["used_tokens"], 1234)
        self.assertEqual(meter["source"], "measured")

    def test_a_prompt_style_request_is_recorded_too(self):
        key = "conv-6"
        self.ledger.observe_request(key, {"prompt": "续写这一段", "max_tokens": 50},
                                    "/v1/completions", mode="text")
        self.assertEqual(self.ledger.history(key)[0]["content"], "续写这一段")

    def test_history_is_bounded_by_count_and_by_size(self):
        key = "conv-7"
        for index in range(30):
            self.ledger.append_turn(key, "user", f"第{index}句" + "字" * 400, mode="text")
        history = self.ledger.history(key, limit=8, max_chars=1000)
        self.assertLessEqual(len(history), 8)
        self.assertLessEqual(sum(len(turn["content"]) for turn in history), 1000 + 400)

    def test_the_thread_survives_a_restart(self):
        key = "conv-8"
        self.ledger.append_turn(key, "user", "关机前说的话", mode="text")
        self.ledger.append_turn(key, "assistant", "关机前的回答", mode="voice")
        self.ledger.save(force=True)

        reopened = ConversationLedger(self.path, context_size=8192, reserve_tokens=512)
        self.assertEqual([turn["content"] for turn in reopened.history(key)],
                         ["关机前说的话", "关机前的回答"])

    def test_a_corrupt_history_file_does_not_stop_the_service(self):
        self.path.write_text("{ this is not json", encoding="utf-8")
        reopened = ConversationLedger(self.path, context_size=8192, reserve_tokens=512)
        self.assertEqual(reopened.history("anything"), [])
        reopened.append_turn("anything", "user", "还是能用", mode="text")
        self.assertEqual(len(reopened.history("anything")), 1)

    def test_an_unwritable_history_is_dropped_rather_than_raised(self):
        # A directory where the file should be: the write fails with a real OSError, which is
        # what an unwritable or vanished logs directory looks like on this machine.
        self.ledger.path = Path(self.temporary.name) / "in-the-way"
        self.ledger.path.mkdir()
        with patch.object(ledger_module, "LOGGER"):
            self.ledger._dirty = True
            self.ledger.save(force=True)  # must not raise
        self.ledger.path = Path(self.temporary.name) / "conversation-history.json"
        self.ledger.append_turn("conv-9", "user", "仍然记录", mode="text")
        self.assertEqual(len(self.ledger.history("conv-9")), 1)

    def test_the_conversation_list_is_newest_first(self):
        self.ledger.append_turn("old", "user", "先说的", mode="text")
        self.ledger.append_turn("new", "user", "后说的", mode="voice")
        # The most recently used conversation leads, whichever order they were created in:
        # the earlier one is touched again to prove the order follows use, not creation.
        self.ledger.append_turn("old", "user", "又说了一句", mode="text")
        listing = self.ledger.conversations()
        self.assertEqual(listing[0]["id"], "old")
        self.assertEqual(listing[0]["last_mode"], "text")
        self.assertEqual(listing[0]["title"], "先说的")
        self.assertEqual(listing[1]["id"], "new")
        self.assertEqual(listing[1]["last_mode"], "voice")

    def test_an_empty_turn_is_not_a_turn(self):
        self.assertIsNone(self.ledger.append_turn("conv-10", "user", "   ", mode="text"))
        self.assertIsNone(self.ledger.append_turn("conv-10", "system", "x", mode="text"))
        self.assertEqual(self.ledger.history("conv-10"), [])

    def test_a_new_conversation_is_called_new_until_it_is_spoken_into(self):
        """「新对话」就暂时先叫新对话：标识符不该出现在界面上。"""
        self.ledger.adopt("voice-x4amc05qmu5djpzp5")
        item = self.ledger.conversations()[0]
        self.assertEqual(item["title"], "")
        self.assertEqual(item["display_title"], "新对话")
        self.assertFalse(item["renamed"])

    def test_the_first_words_become_the_title_cut_to_ten_characters(self):
        self.ledger.append_turn("conv-11", "user", "帮我把这周的周报润色一下，别太官腔", mode="voice")
        item = self.ledger.conversations()[0]
        self.assertEqual(item["title"], "帮我把这周的周报润色")
        self.assertEqual(len(item["title"]), 10)
        self.assertEqual(item["display_title"], item["title"])
        # A second turn does not rename the thread: the title belongs to the opening line.
        self.ledger.append_turn("conv-11", "user", "换个话题：明天天气", mode="voice")
        self.assertEqual(self.ledger.conversations()[0]["title"], "帮我把这周的周报润色")
        # Whitespace is collapsed before cutting, so a pasted paragraph cannot spend the whole
        # title on indentation.
        self.ledger.append_turn("conv-12", "user", "   多行\n文本\n   测试   ", mode="text")
        self.assertEqual(self.ledger.conversations()[0]["title"], "多行 文本 测试")

    def test_renaming_is_final_and_does_not_reorder_the_rail(self):
        self.ledger.append_turn("conv-13", "user", "今天几号", mode="text")
        self.ledger.append_turn("conv-14", "user", "帮我写一段脚本", mode="text")
        before = [item["id"] for item in self.ledger.conversations()]
        self.assertEqual(before[0], "conv-14")

        self.assertEqual(self.ledger.rename("conv-13", "  日期问题  "), "日期问题")
        item = next(entry for entry in self.ledger.conversations() if entry["id"] == "conv-13")
        self.assertEqual((item["title"], item["display_title"], item["renamed"]),
                         ("日期问题", "日期问题", True))
        # Renaming is not activity, so the rail order is exactly what it was.
        self.assertEqual([e["id"] for e in self.ledger.conversations()], before)
        # A manual name survives later turns — and a later turn *does* move the thread up,
        # because "most recently used" is what that order means.
        self.ledger.append_turn("conv-13", "user", "再问一句", mode="text")
        listing = self.ledger.conversations()
        self.assertEqual(next(e for e in listing if e["id"] == "conv-13")["title"], "日期问题")
        self.assertEqual([e["id"] for e in listing], ["conv-13", "conv-14"])
        # A title typed by hand may be longer than the ten characters taken from a first line,
        # but it is still bounded, and it cannot be blanked.
        self.assertEqual(self.ledger.rename("conv-14", "x" * 40), "x" * 40)
        with self.assertRaises(ValueError):
            self.ledger.rename("conv-14", "x" * 41)
        with self.assertRaises(ValueError):
            self.ledger.rename("conv-14", "   ")
        self.assertIsNone(self.ledger.rename("never-existed", "名字"))

    def test_the_estimate_is_a_sane_order_of_magnitude(self):
        self.assertEqual(estimate_tokens(""), 0)
        # Chinese is near one token per character; Latin nearer one per four. A single divisor
        # for both under-counted Chinese by about half, which is what this pins down.
        chinese = estimate_tokens("这是一句中文" * 10)
        english = estimate_tokens("this is english " * 10)
        self.assertGreater(chinese, 50)
        self.assertLess(chinese, 70)
        self.assertGreater(english, 25)
        self.assertLess(english, 60)
        self.assertGreater(chinese, english)


class ConversationRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous = module.ledger
        self.temporary = tempfile.TemporaryDirectory(prefix="qwen-ledger-route-")
        self.addCleanup(self.temporary.cleanup)
        module.ledger = ConversationLedger(Path(self.temporary.name) / "history.json",
                                           context_size=8192, reserve_tokens=512)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),
                                        base_url="http://127.0.0.1:8081")

    async def asyncTearDown(self):
        await self.client.aclose()
        module.ledger = self.previous

    async def test_a_conversation_reports_its_meter_and_recent_turns(self):
        # A thread belongs to one mode: the two turns below are filed under the text scope, which
        # is what the route reads when the serving mode is text.
        key = module.scoped_conversation("conv-a")
        module.ledger.append_turn(key, "user", "打字说的话", mode="text")
        module.ledger.append_turn(key, "assistant", "语音的回答", mode="text")

        response = await self.client.get("/local/conversation",
                                         params={"conversation_id": "conv-a"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["conversation_id"], key)
        self.assertEqual([turn["content"] for turn in body["history"]],
                         ["打字说的话", "语音的回答"])
        self.assertIn("remaining_tokens", body["meter"])
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_an_unknown_conversation_gets_an_empty_but_valid_answer(self):
        response = await self.client.get("/local/conversation",
                                         params={"conversation_id": "never-used"})
        body = response.json()
        self.assertEqual(body["history"], [])
        # Asking about a conversation is what brings it into existence, so its meter reads
        # zero turns from that moment rather than "unknown" for ever.
        self.assertEqual(body["meter"]["turns"], 0)
        self.assertEqual(body["meter"]["used_tokens"], 0)
        self.assertIn(body["meter"]["level"], ("ok", "unknown"))

    async def test_no_identifier_lists_the_conversations(self):
        module.ledger.append_turn(module.scoped_conversation("conv-b"), "user", "第一句",
                                  mode="text")
        response = await self.client.get("/local/conversation")
        body = response.json()
        self.assertEqual([item["id"] for item in body["conversations"]],
                         [module.scoped_conversation("conv-b")])
        self.assertIn("context_size", body)
        self.assertEqual(body["scope"], "text")

    async def test_each_mode_keeps_its_own_thread(self):
        """切换模式不传数据：同一个应用会话标识，在文字与识图下是两条线。"""
        module.ledger.append_turn(module.scoped_conversation("shared-id", "text"),
                                  "user", "这是一句打字说的话", mode="text")
        module.ledger.append_turn(module.scoped_conversation("shared-id", "vision"),
                                  "user", "这是一张图的问题", mode="vision")

        typed = (await self.client.get("/local/conversation",
                                       params={"conversation_id": "shared-id",
                                               "scope": "text"})).json()
        scene = (await self.client.get("/local/conversation",
                                       params={"conversation_id": "shared-id",
                                               "scope": "vision"})).json()
        self.assertEqual([turn["content"] for turn in typed["history"]], ["这是一句打字说的话"])
        self.assertEqual([turn["content"] for turn in scene["history"]], ["这是一张图的问题"])
        self.assertNotEqual(typed["conversation_id"], scene["conversation_id"])
        # The meter counts only its own mode's turns, and a listing shows only its own scope.
        self.assertEqual(typed["meter"]["turns"], 1)
        self.assertEqual(scene["meter"]["turns"], 1)
        names = [item["id"] for item in (await self.client.get(
            "/local/conversation", params={"scope": "vision"})).json()["conversations"]]
        self.assertEqual(names, [module.scoped_conversation("shared-id", "vision")])

    async def test_an_over_long_identifier_is_refused(self):
        response = await self.client.get("/local/conversation",
                                         params={"conversation_id": "x" * 513})
        self.assertEqual(response.status_code, 400)

    async def test_an_opened_but_unused_conversation_can_be_forgotten(self):
        # Entering voice mode opens a fresh conversation; leaving without saying anything must
        # not leave an empty row behind for every visit.
        module.ledger.adopt(module.scoped_conversation("voice-visit-1", "voice"))
        response = await self.client.delete("/local/conversation",
                                            params={"conversation_id": "voice-visit-1",
                                                    "scope": "voice"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["forgotten"])
        self.assertEqual([item["id"] for item in module.ledger.conversations()], [])

    async def test_a_conversation_can_be_renamed_by_the_owner(self):
        module.ledger.append_turn(module.scoped_conversation("conv-rename", "voice"),
                                  "user", "帮我把这周的周报润色一下", mode="voice")
        response = await self.client.patch("/local/conversation",
                                           params={"conversation_id": "conv-rename",
                                                   "scope": "voice"},
                                           json={"title": "周报润色"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], "周报润色")
        self.assertTrue(response.json()["renamed"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        item = module.ledger.conversations()[0]
        self.assertEqual((item["title"], item["display_title"]), ("周报润色", "周报润色"))

    async def test_a_rename_that_cannot_be_used_is_refused_with_a_reason(self):
        module.ledger.append_turn(module.scoped_conversation("conv-rename-2"),
                                  "user", "一句话", mode="text")
        blank = await self.client.patch("/local/conversation",
                                        params={"conversation_id": "conv-rename-2"},
                                        json={"title": "   "})
        self.assertEqual(blank.status_code, 400)
        too_long = await self.client.patch("/local/conversation",
                                           params={"conversation_id": "conv-rename-2"},
                                           json={"title": "字" * 41})
        self.assertEqual(too_long.status_code, 400)
        unknown = await self.client.patch("/local/conversation",
                                          params={"conversation_id": "never-existed"},
                                          json={"title": "名字"})
        self.assertEqual(unknown.status_code, 404)
        no_id = await self.client.patch("/local/conversation", json={"title": "名字"})
        self.assertEqual(no_id.status_code, 400)
        # Nothing was changed by any of the refusals.
        self.assertEqual(module.ledger.conversations()[0]["title"], "一句话")

    async def test_a_conversation_with_turns_can_be_deleted_by_this_route(self):
        # The owner asked for a delete control that removes a thread; a route that silently
        # refused a thread with content would be a button that does nothing. The panel confirms
        # before calling this, which is where the "are you sure" belongs.
        module.ledger.append_turn(module.scoped_conversation("real-thread", "voice"),
                                  "user", "删掉我", mode="voice")
        module.ledger.append_turn(module.scoped_conversation("real-thread", "voice"),
                                  "assistant", "好", mode="voice")
        response = await self.client.delete("/local/conversation",
                                            params={"conversation_id": "real-thread",
                                                    "scope": "voice"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["forgotten"])
        self.assertEqual([item["id"] for item in module.ledger.conversations()], [])

    async def test_clearing_removes_every_conversation_and_reports_the_count(self):
        for name in ("one", "two", "three"):
            module.ledger.append_turn(module.scoped_conversation(name), "user",
                                      f"第 {name} 条", mode="text")
        response = await self.client.delete("/local/conversation/all")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cleared"], 3)
        self.assertEqual(module.ledger.conversations(), [])

    async def test_clearing_an_empty_history_is_not_an_error(self):
        response = await self.client.delete("/local/conversation/all")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cleared"], 0)

    async def test_forgetting_without_an_identifier_is_refused(self):
        response = await self.client.delete("/local/conversation")
        self.assertEqual(response.status_code, 400)

    async def test_deleting_a_conversation_that_is_not_there_reports_false(self):
        response = await self.client.delete("/local/conversation",
                                            params={"conversation_id": "never-existed"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["forgotten"])

    async def test_the_history_limit_is_clamped_rather_than_trusted(self):
        for index in range(80):
            module.ledger.append_turn("conv-c", "user", f"第{index}句", mode="text")
        body = (await self.client.get("/local/conversation",
                                      params={"conversation_id": "conv-c", "limit": 5000})).json()
        self.assertLessEqual(len(body["history"]), 60)


if __name__ == "__main__":
    unittest.main()
