"""循环防护的回归测试（第 1 档：纯静态、零显存）。

所有者反馈：9B/27B 碰到"查文档"或一条报错命令时会**无限循环**——反复用同一参数调用同一工具，
既不退出也不分析原因（打包界面里没有任何步数上限）。所有者接着明确要求：
**不能只会打断**，打断之后要让它反思错误、给出另一条可行方案并**继续**。

所以这些用例同时钉住三件事：
1. 打转要能判出来，正常的多步任务不能误判；
2. 第 1 级**不动工具**，只要求"换方案并继续"；只有复发才逐级收紧（摘一个工具 → 摘全部）；
3. 判据只看最近一条防护指令之后的调用——否则老历史会让这段对话**永久**被摘工具，模型再也干不了活。
"""
import unittest

import loop_guard


def tool_call(identifier: str, name: str, args: str) -> dict:
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": identifier, "type": "function",
                            "function": {"name": name, "arguments": args}}]}


def tool_result(identifier: str, text: str) -> dict:
    return {"role": "tool", "tool_call_id": identifier, "content": text}


def error_history(times: int, *, name: str = "read_file", args: str = '{"path":"D:/docs/手册.md"}',
                  error: str = "Error: 找不到文件 D:/docs/手册.md（No such file）") -> list[dict]:
    messages = [{"role": "user", "content": "帮我查一下这份文档"}]
    for index in range(times):
        messages.append(tool_call(f"call-{index}", name, args))
        messages.append(tool_result(f"call-{index}", error))
    return messages


def busy_history(times: int) -> list[dict]:
    """一轮对话里做了很多次**各不相同且都成功**的工具调用。"""
    messages = [{"role": "user", "content": "把项目里所有模块都过一遍"}]
    for index in range(times):
        name = "read_file" if index % 2 else "grep"
        messages.append(tool_call(f"c{index}", name, f'{{"n":{index}}}'))
        messages.append(tool_result(f"c{index}", f"第 {index} 项内容正常"))
    return messages


def loopy() -> list[dict]:
    """真机那种形态：同一份文档、同一个参数，读了三次都是同一个错误。"""
    return error_history(3)


def after_guard(messages: list[dict], tools: list[dict] | None = None) -> tuple[dict, str]:
    """跑一遍网关的处置，返回（转发出去的 payload，追加的指令）。"""
    verdict = loop_guard.inspect(messages)
    return loop_guard.break_loop({"messages": messages, "tools": tools or DEFAULT_TOOLS()}, verdict)


def DEFAULT_TOOLS() -> list[dict]:
    return [{"type": "function", "function": {"name": name}}
            for name in ("read_file", "grep", "pwsh")]


def tool_names(payload: dict) -> list[str]:
    return [entry["function"]["name"] for entry in payload.get("tools") or []]


class LoopDetectionTests(unittest.TestCase):
    def test_three_identical_failures_are_a_loop(self):
        verdict = loop_guard.inspect(error_history(3))
        self.assertTrue(verdict["loop"])
        self.assertEqual(verdict["reason"], "repeat-identical")
        self.assertEqual(verdict["tool"], "read_file")
        self.assertEqual(verdict["count"], 3)
        self.assertEqual(verdict["level"], 1, "第一次判定是第 1 级：先教它换方案，不摘工具")
        self.assertIn("找不到文件", verdict["failure"])
        self.assertIn("手册.md", verdict["blocked"])

    def test_two_failures_are_not_a_loop_yet(self):
        self.assertFalse(loop_guard.inspect(error_history(2))["loop"])

    def test_identical_successful_retries_also_loop(self):
        """成功的调用原地重复同样没有进展，也要刹住。"""
        messages = [{"role": "user", "content": "查文档"}]
        for index in range(3):
            messages.append(tool_call(f"c{index}", "grep", '{"pattern":"张量"}'))
            messages.append(tool_result(f"c{index}", "没有匹配"))
        verdict = loop_guard.inspect(messages)
        self.assertTrue(verdict["loop"])
        self.assertEqual(verdict["reason"], "repeat-identical")

    def test_many_failing_calls_to_one_tool_loop_even_with_different_args(self):
        """换着参数反复试同一个工具、次次失败，同样是循环。"""
        messages = [{"role": "user", "content": "跑一下命令"}]
        verbs = ["dir", "ls", "type", "cat", "head", "tail"]
        for index, verb in enumerate(verbs):
            messages.append(tool_call(f"c{index}", "pwsh", f'{{"command":"{verb} missing.txt"}}'))
            messages.append(tool_result(f"c{index}", "Error: CommandNotFoundException"))
        verdict = loop_guard.inspect(messages)
        self.assertTrue(verdict["loop"])
        self.assertEqual(verdict["reason"], "same-tool-failing")
        self.assertEqual(verdict["count"], 6)

    def test_varied_successful_work_is_never_a_loop(self):
        messages = [{"role": "user", "content": "整理文档"}]
        for index in range(8):
            messages.append(tool_call(f"c{index}", "read_file" if index % 2 else "grep",
                                      f'{{"file":"doc{index}.md"}}'))
            messages.append(tool_result(f"c{index}", f"第 {index} 份文档的内容……"))
        verdict = loop_guard.inspect(messages)
        self.assertFalse(verdict["loop"], verdict)

    def test_partly_failing_but_varied_work_is_not_a_loop(self):
        messages = [{"role": "user", "content": "做几件事"}]
        for index in range(6):
            name = "read_file" if index % 2 == 0 else "pwsh"
            messages.append(tool_call(f"c{index}", name, f'{{"n":{index}}}'))
            messages.append(tool_result(f"c{index}", "Error: 偶发失败" if index % 3 else "成功了"))
        self.assertFalse(loop_guard.inspect(messages)["loop"])

    def test_no_tool_calls_is_not_a_loop(self):
        messages = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好"}]
        verdict = loop_guard.inspect(messages)
        self.assertFalse(verdict["loop"])
        self.assertEqual(verdict["calls"], 0)


class LevelOneTests(unittest.TestCase):
    """第 1 级：**打断但不缴械**——工具还在，要求它反思、给新方案、继续干。"""

    def test_the_first_interruption_keeps_every_tool(self):
        fixed, note = after_guard(loopy())
        self.assertEqual(tool_names(fixed), ["read_file", "grep", "pwsh"],
                         "第 1 级不能摘工具，否则这一轮就没法继续了")

    def test_the_instruction_demands_a_new_plan_and_continuation(self):
        _, note = after_guard(loopy())
        self.assertIn("循环防护", note)
        self.assertIn("可行方案", note)
        self.assertIn("继续执行", note)
        self.assertIn("read_file", note)
        self.assertIn("手册.md", note)
        self.assertIn("找不到文件", note)
        self.assertIn("level=1", note)

    def test_the_marker_says_what_was_blocked_and_why(self):
        verdict = loop_guard.inspect(loopy())
        note = loop_guard.break_loop({"messages": loopy()}, verdict)[1]
        marker = loop_guard.BLOCK_RE.search(note)
        self.assertIsNotNone(marker, note)
        self.assertEqual(marker.group(1), "read_file")
        self.assertEqual(len(marker.group(2)), 8)
        self.assertEqual(marker.group(3), "1")
        self.assertEqual(marker.group(4), "repeat-identical")

    def test_the_same_call_is_recognised_by_its_signature(self):
        """参数不同就不算"同一次调用"：换参数试探有它自己的判据（判据二）。"""
        other = error_history(3, args='{"path":"D:/docs/另一份.md"}')
        verdict = loop_guard.inspect(other)
        self.assertNotEqual(verdict["signature"], loop_guard.inspect(loopy())["signature"])

    def test_the_original_payload_is_never_mutated(self):
        payload = {"messages": loopy(), "tools": DEFAULT_TOOLS()}
        before = len(payload["messages"])
        loop_guard.break_loop(payload, loop_guard.inspect(payload["messages"]))
        self.assertIn("tools", payload)
        self.assertEqual(len(payload["messages"]), before)

    def test_the_instruction_stays_small(self):
        verdict = loop_guard.inspect(error_history(3, error="x" * 5000))
        _, note = loop_guard.break_loop({"messages": []}, verdict)
        self.assertLess(len(note.encode("utf-8")), 1400)


class SelfHealingTests(unittest.TestCase):
    """判据只看最近一条防护指令**之后**的调用，所以中断一次不会把整段对话废掉。"""

    def test_the_old_repetition_does_not_fire_again_for_ever(self):
        messages = loopy()
        _, note = after_guard(messages)
        messages = messages + [{"role": "user", "content": note}]
        verdict = loop_guard.inspect(messages)
        self.assertFalse(verdict["loop"], "老历史里的重复不该每一轮都再判一次")
        self.assertEqual(tool_names(loop_guard.break_loop({"messages": messages,
                                                           "tools": DEFAULT_TOOLS()}, verdict)[0]),
                         ["read_file", "grep", "pwsh"])

    def test_a_different_approach_after_the_interruption_is_fine(self):
        messages = loopy()
        _, note = after_guard(messages)
        messages = messages + [{"role": "user", "content": note}]
        messages.append(tool_call("c-new", "grep", '{"pattern":"手册","path":"D:/docs"}'))
        messages.append(tool_result("c-new", "D:/docs/手册-v2.md:3: 目录"))
        messages.append(tool_call("c-new2", "read_file", '{"path":"D:/docs/手册-v2.md"}'))
        messages.append(tool_result("c-new2", "第 1 章 ……"))
        self.assertFalse(loop_guard.inspect(messages)["loop"])

    def test_a_new_owner_message_still_counts_calls_from_scratch(self):
        messages = loopy()
        _, note = after_guard(messages)
        messages = messages + [{"role": "user", "content": note},
                               {"role": "assistant", "content": "我改走 grep。"},
                               {"role": "user", "content": "好，继续"}]
        messages.append(tool_call("c-new", "grep", '{"pattern":"手册"}'))
        messages.append(tool_result("c-new", "找到 2 处"))
        self.assertFalse(loop_guard.inspect(messages)["loop"])


class EscalationTests(unittest.TestCase):
    """复发才收紧：先摘掉打转的那一个工具，再犯就摘全部。"""

    def relapse(self, times: int = 1) -> list[dict]:
        messages = loopy()
        _, note = after_guard(messages)
        messages = messages + [{"role": "user", "content": note}]
        for index in range(times):
            messages.append(tool_call(f"again-{index}", "read_file", '{"path":"D:/docs/手册.md"}'))
            messages.append(tool_result(f"again-{index}", "Error: 找不到文件（No such file）"))
        return messages

    def test_repeating_the_blocked_call_escalates_to_level_two(self):
        verdict = loop_guard.inspect(self.relapse())
        self.assertTrue(verdict["loop"])
        self.assertEqual(verdict["reason"], "repeat-blocked")
        self.assertEqual(verdict["level"], 2)

    def test_level_two_removes_only_the_tool_that_kept_failing(self):
        messages = self.relapse()
        fixed, note = loop_guard.break_loop({"messages": messages, "tools": DEFAULT_TOOLS()},
                                            loop_guard.inspect(messages))
        self.assertEqual(tool_names(fixed), ["grep", "pwsh"], "其余工具要留着，好让它换个做法")
        self.assertIn("移除", note)
        self.assertIn("level=2", note)
        self.assertIn("别的工具", note)

    def test_a_third_attempt_removes_every_tool(self):
        messages = self.relapse()
        _, note = after_guard(messages)
        messages = messages + [{"role": "user", "content": note}]
        messages.append(tool_call("again-2", "read_file", '{"path":"D:/docs/手册.md"}'))
        messages.append(tool_result("again-2", "Error: 找不到文件（No such file）"))
        verdict = loop_guard.inspect(messages)
        self.assertEqual(verdict["level"], 3)
        fixed, note = loop_guard.break_loop({"messages": messages, "tools": DEFAULT_TOOLS()}, verdict)
        self.assertNotIn("tools", fixed)
        self.assertIn("根本原因", note)
        self.assertIn("下一步的具体方案", note)

    def test_the_legacy_functions_shape_is_filtered_too(self):
        messages = self.relapse()
        payload = {"messages": messages,
                   "functions": [{"name": "read_file"}, {"name": "grep"}],
                   "function_call": {"name": "read_file"}}
        fixed, _ = loop_guard.break_loop(payload, loop_guard.inspect(messages))
        self.assertEqual([entry["name"] for entry in fixed["functions"]], ["grep"])
        self.assertNotIn("function_call", fixed)
        self.assertEqual(len(payload["functions"]), 2, "原 payload 的旧字段不能被删掉")
        self.assertIn("function_call", payload)

    def test_a_tool_only_block_relapses_when_it_keeps_failing(self):
        """判据二拦下的是"这个工具一直失败"，不是某一次参数：它再失败 3 次才算复发。"""
        messages = [{"role": "user", "content": "跑一下命令"}]
        verbs = ["dir", "ls", "type", "cat", "head", "tail"]
        for index, verb in enumerate(verbs):
            messages.append(tool_call(f"c{index}", "pwsh", f'{{"command":"{verb} missing.txt"}}'))
            messages.append(tool_result(f"c{index}", "Error: CommandNotFoundException"))
        _, note = after_guard(messages)
        messages = messages + [{"role": "user", "content": note}]
        for index, verb in enumerate(("del", "rm", "erase")):
            messages.append(tool_call(f"n{index}", "pwsh", f'{{"command":"{verb} missing.txt"}}'))
            messages.append(tool_result(f"n{index}", "Error: CommandNotFoundException"))
        verdict = loop_guard.inspect(messages)
        self.assertEqual(verdict["reason"], "repeat-blocked")
        self.assertEqual(verdict["level"], 2)
        self.assertEqual(verdict["tool"], "pwsh")


class StepBudgetTests(unittest.TestCase):
    """界面里没有任何步数上限（`bundle.zh-CN.js` 里 `max_steps` 出现 0 次），
    所以"一直在干活、但永远不收尾"也要有最后一道刹车——这一档是硬停，要它总结并给下一步方案。"""

    def test_a_turn_that_exceeds_the_budget_is_stopped(self):
        verdict = loop_guard.inspect(busy_history(loop_guard.STEP_BUDGET))
        self.assertTrue(verdict["loop"])
        self.assertEqual(verdict["reason"], "step-budget")
        self.assertEqual(verdict["count"], loop_guard.STEP_BUDGET)
        self.assertEqual(verdict["level"], loop_guard.LEVEL_LIMIT)
        self.assertIn(verdict["tool"], {"grep", "read_file"})

    def test_one_call_under_the_budget_is_left_alone(self):
        verdict = loop_guard.inspect(busy_history(loop_guard.STEP_BUDGET - 1))
        self.assertFalse(verdict["loop"], verdict)

    def test_the_budget_instruction_asks_for_a_summary_and_the_next_plan(self):
        messages = busy_history(loop_guard.STEP_BUDGET)
        payload = {"messages": messages,
                   "tools": [{"type": "function", "function": {"name": "grep"}}]}
        fixed, note = loop_guard.break_loop(payload, loop_guard.inspect(messages))
        self.assertNotIn("tools", fixed)
        self.assertIn(str(loop_guard.STEP_BUDGET), note)
        self.assertIn("可行方案", note)
        self.assertIn("level=3", note)
        self.assertNotIn("{budget}", note)
        self.assertNotIn("失败信息是", note)

    def test_a_new_user_turn_resets_the_budget(self):
        """预算是"每一轮"的：所有者再问一句，计数重新开始。"""
        messages = busy_history(loop_guard.STEP_BUDGET - 1)
        messages.append({"role": "assistant", "content": "先到这，等你确认。"})
        messages.append({"role": "user", "content": "继续，把剩下的也看完"})
        messages.append(tool_call("c-new", "read_file", '{"n":"new"}'))
        messages.append(tool_result("c-new", "新一轮的第一项，正常"))
        self.assertFalse(loop_guard.inspect(messages)["loop"])

    def test_our_own_instruction_does_not_start_a_fresh_budget(self):
        """防护指令自己也是一条 user 消息（客户端会照原样回传），它不算新的一轮。

        否则"提醒一次 → 计数清零 → 再放 24 次"就是绕过刹车的路。这里模拟真机形态：
        24 次调用 → 网关追加指令 → 模型又想调工具，仍然必须是超预算。
        """
        messages = busy_history(loop_guard.STEP_BUDGET)
        _, note = after_guard(messages)
        messages.append({"role": "user", "content": note})
        messages.append(tool_call("c-next", "read_file", '{"n":"after-guard"}'))
        messages.append(tool_result("c-next", "第 25 项内容正常"))
        verdict = loop_guard.inspect(messages)
        self.assertTrue(verdict["loop"], verdict)
        self.assertEqual(verdict["reason"], "step-budget")
        self.assertEqual(verdict["count"], loop_guard.STEP_BUDGET + 1)
        self.assertTrue(note.startswith(loop_guard.GUARD_MARK))


if __name__ == "__main__":
    unittest.main()
