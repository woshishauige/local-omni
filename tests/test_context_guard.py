"""context_guard 的回归测试（第 1 档：零显存）。

钉住三件事：
1. 只有"量出来确实装不下"才拦；量不了（None）和刚好压线都不拦，不能误伤正常请求；
2. 给界面的话必须是中文、说清"没发出去 + 为什么 + 怎么办"，并带真实的两个数字；
3. 流式那一路必须按界面真正渲染的形状发（`choices[0].delta`），否则等于又静默一次。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import unittest

from app.context import context_guard


class BudgetTest(unittest.TestCase):
    def test_reserve_is_kept_for_generation(self):
        self.assertEqual(context_guard.budget(32768), 32768 - context_guard.RESERVE_TOKENS)

    def test_tiny_window_never_goes_negative(self):
        self.assertGreaterEqual(context_guard.budget(256), 1024)

    def test_overflows_edges(self):
        window = 32768
        limit = context_guard.budget(window)
        self.assertFalse(context_guard.overflows(limit, window))
        self.assertTrue(context_guard.overflows(limit + 1, window))
        self.assertFalse(context_guard.overflows(None, window))

    def test_real_incident_numbers(self):
        # 真机那次：37,679 token 撞上 32,768 的窗口。
        self.assertTrue(context_guard.overflows(37679, 32768))
        # 一次正常的带工具短回合不该被拦。
        self.assertFalse(context_guard.overflows(2443, 32768))


class MessageTest(unittest.TestCase):
    def test_message_is_actionable(self):
        text = context_guard.message(37679, 32768)
        for expected in ("37679", "32768", "没有发给模型", "新开一个对话", "拆小", "context_size"):
            self.assertIn(expected, text)
        self.assertGreater(len(text), 120)

    def test_reply_frame_is_the_shape_the_ui_renders(self):
        """界面只读 `data:` 里的 `choices[0].delta`，所以护栏的话必须按助手回复的形状发。"""
        text = context_guard.message(40000, 32768)
        frame = context_guard.reply_frame(text).decode("utf-8")
        data_lines = [line[6:] for line in frame.split("\n") if line.startswith("data: ")]
        self.assertEqual(data_lines[-1], "[DONE]")
        chunks = [json.loads(line) for line in data_lines[:-1]]
        self.assertEqual(chunks[0]["choices"][0]["delta"]["role"], "assistant")
        self.assertIn("40000", chunks[0]["choices"][0]["delta"]["content"])
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")
        self.assertNotIn("event: error", frame)

    def test_legacy_name_still_points_at_the_reply_frame(self):
        """mode_server.py 现在跑的是 22:10 的字节码，里面调的还是旧名字 `sse_frame`。"""
        self.assertIs(context_guard.sse_frame, context_guard.reply_frame)

    def test_json_error_shape(self):
        body = context_guard.json_error("x")
        self.assertEqual(body["error"]["type"], "context_overflow_error")


class WireTest(unittest.TestCase):
    def test_count_threshold_is_not_zero(self):
        # 每次请求都量一遍会白白多两次上游往返：只有大请求才量。
        self.assertGreaterEqual(context_guard.COUNT_ABOVE_BYTES, 8000)


if __name__ == "__main__":
    unittest.main()
