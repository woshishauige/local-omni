"""生图 / 生成视频的提示词优化必须跟着**当前选中的模型**走，而不是钉死在某一个模型上。

The rule used to be implicit and wrong in three ways at once: the image job called the
language-model port directly while claiming "Qwen is still loaded in the user's last mode" (never
true from the 生图 view, where ComfyUI holds the card, so the call failed and the job silently used
the raw prompt), the video queue had no such step at all, and the enhancer's name was the literal
string "Qwen3.8-27B 本地" even while the 9B was selected.

These tests pin the corrected contract: the model is read from the settings, loaded through the
caller's own mode loader, named honestly to the UI, and a model problem degrades to the owner's own
words instead of failing the job.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import unittest
from unittest.mock import AsyncMock

import httpx

from app import prompt_enhance


def make_client(reply="一只猫坐在窗台上", status=200, record=None):
    async def handler(request):
        if record is not None:
            record.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": "boom"})
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def make_enhancer(settings, client=None, loader=None, record=None):
    return prompt_enhance.PromptEnhancer(
        client or make_client(record=record),
        loader,
        settings_getter=lambda: settings,
    )


class EnhancerFollowsTheSelectedModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_it_names_the_selected_model_not_a_fixed_one(self):
        nine = make_enhancer({"model_profile": "qwen3.5-9b-uncensored"})
        heavy = make_enhancer({"model_profile": "qwen3.8-27b"})
        self.assertEqual(nine.describe()["label"], "9B 无审查")
        self.assertEqual(heavy.describe()["label"], "27B 三元去审查")
        self.assertEqual(nine.describe()["profile"], "qwen3.5-9b-uncensored")
        self.assertTrue(nine.describe()["dynamic"])

    async def test_it_loads_the_current_model_before_asking_it(self):
        settings = {"model_profile": "qwen3.5-9b-uncensored"}
        loader = AsyncMock()
        enhancer = make_enhancer(settings, loader=loader)
        text, info = await enhancer.enhance("一只猫", kind="video")
        # The loader is the gateway's own mode loader, called for the *language* mode: that is the
        # same path the mode buttons use, so the profile it brings up is the selected one.
        loader.assert_awaited_once_with("text")
        self.assertEqual(text, "一只猫坐在窗台上")
        self.assertTrue(info["used"])
        self.assertEqual(info["label"], "9B 无审查")

    async def test_each_kind_asks_for_the_document_it_needs(self):
        settings = {"model_profile": "qwen3.5-9b-uncensored"}
        record = []
        enhancer = make_enhancer(settings, record=record)
        await enhancer.enhance("一只猫", kind="video")
        await enhancer.enhance("一只猫", kind="image")
        import json
        first = json.loads(record[0].content.decode("utf-8"))
        second = json.loads(record[1].content.decode("utf-8"))
        self.assertEqual(first["messages"][0]["content"], prompt_enhance.VIDEO_PROMPT_SYSTEM)
        # The image queue still wants the English Klein prompt its workflow was trained for; the
        # dynamic part is which model writes it, not what the workflow needs.
        self.assertNotEqual(second["messages"][0]["content"], prompt_enhance.VIDEO_PROMPT_SYSTEM)

    async def test_a_model_that_cannot_be_loaded_leaves_the_words_alone(self):
        settings = {"model_profile": "qwen3.5-9b-uncensored"}
        loader = AsyncMock(side_effect=RuntimeError("显卡被占用"))
        enhancer = make_enhancer(settings, loader=loader)
        text, info = await enhancer.enhance("一只猫", kind="image")
        self.assertEqual(text, "一只猫")
        self.assertFalse(info["used"])
        self.assertIn("显卡被占用", info["reason"])
        # The name is still reported: the panel says which model *would* have done it.
        self.assertEqual(info["label"], "9B 无审查")

    async def test_an_unreachable_model_degrades_to_the_owners_words(self):
        settings = {"model_profile": "qwen3.5-9b-uncensored"}
        enhancer = make_enhancer(settings, client=make_client(status=500))
        text, info = await enhancer.enhance("一只猫", kind="video")
        self.assertEqual(text, "一只猫")
        self.assertFalse(info["used"])
        self.assertTrue(info["reason"])

    async def test_an_empty_prompt_is_not_sent_anywhere(self):
        settings = {"model_profile": "qwen3.5-9b-uncensored"}
        record = []
        loader = AsyncMock()
        enhancer = make_enhancer(settings, loader=loader, record=record)
        text, info = await enhancer.enhance("   ", kind="image")
        self.assertEqual(text, "")
        self.assertEqual(record, [])
        loader.assert_not_awaited()
        self.assertFalse(info["used"])


if __name__ == "__main__":
    unittest.main()
