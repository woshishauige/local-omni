"""agent_briefing 的回归测试（第 1 档：纯静态、零显存）。

钉住三件事：
1. 只有在"这一轮要动手、而且应用没写自己的 system 消息"时才注入——纯聊天、识图、语音不能被污染；
2. 注入后消息顺序不变，已有的空 system 占位就地替换，不会出现两条 system；
3. 规程里必须留着这次踩出来的那几条具体处置（Windows 命令、输出被吞改走脚本、3 次停手、
   一轮 8 次、没有搜索工具、上下文窗口数字），否则等于把教训丢了。
"""
import unittest

import agent_briefing


def call(name="read_file", args='{"path":"D:/a.txt"}', identifier="c1"):
    return {"id": identifier, "type": "function",
            "function": {"name": name, "arguments": args}}


class ShouldBriefTest(unittest.TestCase):
    def payload(self, messages=None, tools=None):
        body = {"messages": messages if messages is not None else [{"role": "user", "content": "看下目录"}]}
        if tools is not None:
            body["tools"] = tools
        return body

    def test_plain_chat_is_left_alone(self):
        self.assertFalse(agent_briefing.should_brief(self.payload()))

    def test_tool_turn_is_briefed(self):
        self.assertTrue(agent_briefing.should_brief(self.payload(tools=[{"type": "function"}])))

    def test_history_with_tool_calls_counts_as_acting(self):
        messages = [{"role": "user", "content": "继续"},
                    {"role": "assistant", "content": None, "tool_calls": [call()]},
                    {"role": "tool", "tool_call_id": "c1", "content": "ok"}]
        self.assertTrue(agent_briefing.should_brief(self.payload(messages=messages)))

    def test_app_system_message_wins(self):
        messages = [{"role": "system", "content": "你是识图助手。"}, {"role": "user", "content": "这是什么"}]
        self.assertFalse(agent_briefing.should_brief(self.payload(messages=messages, tools=[{}])))

    def test_empty_system_placeholder_is_taken_over(self):
        messages = [{"role": "system", "content": ""}, {"role": "user", "content": "看下目录"}]
        self.assertTrue(agent_briefing.should_brief(self.payload(messages=messages, tools=[{}])))

    def test_switch_off(self):
        import os
        os.environ["QWEN_AGENT_BRIEFING"] = "off"
        try:
            self.assertFalse(agent_briefing.should_brief(self.payload(tools=[{}])))
        finally:
            del os.environ["QWEN_AGENT_BRIEFING"]

    def test_bad_shapes(self):
        self.assertFalse(agent_briefing.should_brief(None))
        self.assertFalse(agent_briefing.should_brief({"messages": []}))
        self.assertFalse(agent_briefing.should_brief({"messages": "nope", "tools": [{}]}))


class ApplyTest(unittest.TestCase):
    def test_prepends_when_no_system(self):
        original = {"messages": [{"role": "user", "content": "看下目录"}], "tools": [{"type": "function"}]}
        data, added = agent_briefing.apply(original, window=32768)
        self.assertGreater(added, 500)
        self.assertEqual(data["messages"][0]["role"], "system")
        self.assertIn("Windows", data["messages"][0]["content"])
        self.assertIn("32768", data["messages"][0]["content"])
        self.assertEqual(data["messages"][1], original["messages"][0])
        # 原 payload 不被改动：网关可能还要拿它做别的判断（压缩、记账）。
        self.assertEqual(original["messages"], [{"role": "user", "content": "看下目录"}])

    def test_replaces_empty_placeholder_in_place(self):
        original = {"tools": [{}], "messages": [{"role": "system", "content": "  "},
                                                {"role": "user", "content": "hi"}]}
        data, _ = agent_briefing.apply(original)
        self.assertEqual(len(data["messages"]), 2)
        self.assertEqual(data["messages"][1]["content"], "hi")
        self.assertTrue(data["messages"][0]["content"].startswith("【运行环境】"))

    def test_untouched_when_not_applicable(self):
        original = {"messages": [{"role": "user", "content": "你好"}]}
        data, added = agent_briefing.apply(original)
        self.assertIs(data, original)
        self.assertEqual(added, 0)


class LessonTest(unittest.TestCase):
    """这些条目是那次真机对话的直接教训，少一条就等于复发。"""

    REQUIRED = [
        "cmd.exe",
        "dir",
        "get_info",
        "一次只发一个工具调用",
        "write_file",
        "read_file",
        "拒绝访问",
        "连续失败 3 次",
        "不超过 8 次",
        "没有搜索工具",
        "400",
        "违法操作",
    ]

    def test_lessons_are_present(self):
        text = agent_briefing.BRIEFING
        missing = [item for item in self.REQUIRED if item not in text]
        self.assertEqual(missing, [])

    def test_briefing_stays_small(self):
        # 系统消息吃的是同一个窗口：规程再长就该拆成工具/文档，而不是硬塞。
        self.assertLess(len(agent_briefing.BRIEFING.format(window=32768)), 2900)

    def test_measured_tool_facts_are_stated(self):
        """这几条是直接对着 /tools 测出来的：带管道/引号的命令会坏，写脚本再跑才可靠。"""
        text = agent_briefing.BRIEFING
        self.assertIn("管道", text)
        self.assertIn("python 脚本.py", text)
        self.assertIn("-o NUL", text)


if __name__ == "__main__":
    unittest.main()
