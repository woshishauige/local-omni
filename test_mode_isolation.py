"""切换模式不传数据：进模型的提示词只保留"这个模式自己的那一轮"。

The packaged app keeps one conversation on screen while the toolbar switches which slice of the
backend answers, so every request carries the whole thread — including the turns typed in 纯文本.
The gateway's per-mode history alone does not stop that; this filter does, and these tests pin the
rule it implements: keep what this mode recorded (plus the current turn), drop the rest.
"""

import json
import unittest
from unittest.mock import patch

import mode_isolation


def message(role, content, **extra):
    return {"role": role, "content": content, **extra}


class IsolationTests(unittest.TestCase):
    def test_the_other_modes_turns_are_dropped(self):
        payload = {"messages": [
            message("system", "你是助手"),
            message("user", "这是文字模式问的"),
            message("assistant", "文字模式的回答"),
            message("user", {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}}),
        ]}
        isolated, dropped = mode_isolation.isolate(payload, {"这是识图模式问的"})
        roles = [entry["role"] for entry in isolated["messages"]]
        # The system prompt and the current turn survive; the typed exchange does not.
        self.assertEqual(roles, ["system", "user"])
        self.assertEqual(dropped, 2)
        self.assertIs(isolated["messages"][1], payload["messages"][3], "the current turn is kept as it is")

    def test_this_modes_own_history_is_kept_across_a_switch_back(self):
        """切回文字模式时，文字那条线自己的历史要留着（被丢掉的只是识图那几轮）。"""
        payload = {"messages": [
            message("system", "你是助手"),
            message("user", "第一句文字"),
            message("assistant", "第一句回答"),
            message("user", {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}}),
            message("assistant", "这张图里有一只猫"),
            message("user", "第二句文字"),
        ]}
        isolated, dropped = mode_isolation.isolate(payload, {"第一句文字"})
        self.assertEqual([entry["role"] for entry in isolated["messages"]],
                         ["system", "user", "assistant", "user"])
        self.assertEqual([entry.get("content") for entry in isolated["messages"][1:]],
                         ["第一句文字", "第一句回答", "第二句文字"])
        self.assertEqual(dropped, 2)

    def test_the_first_turn_of_a_mode_carries_nothing_but_itself(self):
        payload = {"messages": [
            message("system", "你是助手"),
            message("user", "文字第一句"),
            message("assistant", "回答"),
            message("user", "刚切到识图问的第一句"),
        ]}
        isolated, dropped = mode_isolation.isolate(payload, set())
        self.assertEqual([entry["role"] for entry in isolated["messages"]], ["system", "user"])
        self.assertEqual(dropped, 2)

    def test_nothing_recorded_or_nothing_to_drop_returns_the_same_object(self):
        payload = {"messages": [message("user", "只有这一句")]}
        isolated, dropped = mode_isolation.isolate(payload, set())
        self.assertIs(isolated, payload)
        self.assertEqual(dropped, 0)

    def test_a_payload_without_messages_is_left_alone(self):
        payload = {"prompt": "补全点什么"}
        isolated, dropped = mode_isolation.isolate(payload, {"x"})
        self.assertIs(isolated, payload)
        self.assertEqual(dropped, 0)

    def test_a_string_and_a_structured_question_compare_the_way_the_ledger_stores_them(self):
        structured = {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}}
        payload = {"messages": [message("user", structured), message("user", "当前这一句")]}
        # The ledger stores a non-string content as JSON; the filter has to match that form.
        isolated, dropped = mode_isolation.isolate(
            payload, {json.dumps(structured, ensure_ascii=False)})
        self.assertEqual([entry["role"] for entry in isolated["messages"]], ["user", "user"])
        self.assertEqual(dropped, 0)

    def test_a_repeated_question_only_licenses_one_turn(self):
        """同一句话在别的模式里也出现过时，不能把那一轮一起放进来（审查发现的缺口）。

        The set-based matcher legitimised *every* copy of a recorded question, so a typed
        "你好 / A1 / 你好 / A2 / 新问题" thread kept the other mode's copy of 你好 and its answer.
        Matching as a count from the end keeps the newest copy only.
        """
        payload = {"messages": [
            message("user", "你好"),
            message("assistant", "A1（文字模式的回答）"),
            message("user", "你好"),
            message("assistant", "A2（本模式的回答）"),
            message("user", "新问题"),
        ]}
        isolated, dropped = mode_isolation.isolate(payload, {"你好"})
        self.assertEqual([entry.get("content") for entry in isolated["messages"]],
                         ["你好", "A2（本模式的回答）", "新问题"])
        self.assertEqual(dropped, 2)

    def test_the_current_turn_does_not_consume_a_recorded_occurrence(self):
        """当前这一轮总是留下，而且不占用"本模式记过的次数"。

        三次"收到"、本模式只记过一次：最新的两份（当前这一轮 + 被那次记录许可的一份）留下，
        最早那份属于别的模式，丢掉。文字完全相同的两轮无法只靠文本区分，这一点在模块文档里写明。
        """
        payload = {"messages": [
            message("user", "收到"),
            message("assistant", "旧回答（别的模式）"),
            message("user", "收到"),
            message("assistant", "本模式的回答"),
            message("user", "收到"),
        ]}
        isolated, dropped = mode_isolation.isolate(payload, {"收到"})
        self.assertEqual([entry.get("content") for entry in isolated["messages"]],
                         ["收到", "本模式的回答", "收到"])
        self.assertEqual(dropped, 2)

    def test_two_recorded_occurrences_keep_two_turns(self):
        """本模式真的问过两次，那就留两轮（按次数消费，不是一刀切）。"""
        payload = {"messages": [
            message("user", "继续"),
            message("assistant", "第一次回答"),
            message("user", "继续"),
            message("assistant", "第二次回答"),
            message("user", "新的"),
        ]}
        isolated, dropped = mode_isolation.isolate(payload, ["继续", "继续"])
        self.assertEqual([entry["role"] for entry in isolated["messages"]],
                         ["user", "assistant", "user", "assistant", "user"])
        self.assertEqual(dropped, 0)

    def test_tool_messages_follow_the_run_they_belong_to(self):
        payload = {"messages": [
            message("system", "你是助手"),
            message("user", "旧的文字问题"),
            message("tool", "旧工具结果"),
            message("user", "识图这一句"),
        ]}
        isolated, dropped = mode_isolation.isolate(payload, set())
        self.assertEqual([entry["role"] for entry in isolated["messages"]], ["system", "user"])
        self.assertEqual(dropped, 2)


class WiringTests(unittest.IsolatedAsyncioTestCase):
    """The gateway asks the module — and does so before compressing, not after."""

    async def test_the_proxy_filters_the_prompt_for_the_serving_mode(self):
        import mode_server as module

        seen = {}

        async def capture(payload, *_args, **_kwargs):
            seen["messages"] = list(payload.get("messages") or [])
            return payload, {"state": "unchanged"}

        conversation = module.scoped_conversation("switch-me")
        module.ledger.record_measurement(conversation, 10)
        module.ledger.append_turn(conversation, "user", "识图问过的一句", mode="vision")
        self.assertEqual(module.scope_user_texts(conversation), {"识图问过的一句"})
        with patch.object(module.context_compactor, "prepare", side_effect=capture):
            isolated, dropped = module.mode_isolation.isolate(
                {"messages": [{"role": "user", "content": "文字问的"},
                              {"role": "assistant", "content": "文字回答"},
                              {"role": "user", "content": "识图问过的一句"}]},
                module.scope_user_texts(conversation))
        self.assertEqual([entry["content"] for entry in isolated["messages"]], ["识图问过的一句"])
        self.assertEqual(dropped, 2)

    async def test_a_question_just_forwarded_is_not_treated_as_another_modes(self):
        """账本要等响应结束才写，所以"上一轮"必须由转发时记住的那份兜住。"""
        import mode_server as module

        conversation = module.scoped_conversation("just-asked")
        module.forwarded_user_texts.pop(conversation, None)
        self.addCleanup(module.forwarded_user_texts.pop, conversation, None)
        # Nothing in the ledger yet — exactly the window the live check hit.
        self.assertEqual(module.scope_user_texts(conversation), set())
        module.remember_user_text(conversation,
                                  {"messages": [{"role": "user", "content": "刚问过的一句"}]})
        self.assertIn("刚问过的一句", module.scope_user_texts(conversation))
        isolated, dropped = module.mode_isolation.isolate(
            {"messages": [{"role": "user", "content": "刚问过的一句"},
                          {"role": "assistant", "content": "回答"},
                          {"role": "user", "content": "接着问"}]},
            module.scope_user_texts(conversation))
        self.assertEqual(len(isolated["messages"]), 3)
        self.assertEqual(dropped, 0)

    def test_the_current_question_does_not_license_an_earlier_copy_of_itself(self):
        """当前这一轮不能在过滤之前就进"已知集合"——否则它会许可更早的同文本那一轮。

        实测过的事故：同一句话先在纯文本问、再在识图问，识图那轮把文字那轮当成"本模式的"
        留下，连带它的回答（里面有只在文字线里给过的暗号）一起进模型，于是识图答出了暗号。
        顺序必须是"先过滤、后记住"。
        """
        payload = {"messages": [
            message("user", "刚才让你记住的暗号是什么？"),
            message("assistant", "紫色河马 741"),
            message("user", "刚才让你记住的暗号是什么？"),
        ]}
        # 先按"还没记住当前这一轮"计算（这是修好之后的顺序），视这条线为空。
        isolated, dropped = mode_isolation.isolate(payload, set())
        self.assertEqual([entry["role"] for entry in isolated["messages"]], ["user"])
        self.assertEqual(dropped, 2)
        # 反过来的顺序（先记住、再过滤）会许可更早那一轮 —— 这个用例把差别钉住。
        isolated_wrong, dropped_wrong = mode_isolation.isolate(payload, {"刚才让你记住的暗号是什么？"})
        self.assertEqual(len(isolated_wrong["messages"]), 3)
        self.assertEqual(dropped_wrong, 0)


if __name__ == "__main__":
    unittest.main()
