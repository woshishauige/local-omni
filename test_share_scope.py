"""Sharing settings must not disappear out from under a running gateway.

实测：手工启动的网关（没有 QWEN_SHARE_CONFIG）在运行期被某个库 `import unittest` 之后，
`tests_are_running()` 由 False 变 True，整个共享覆盖层随之消失——允许的访问名、监听地址、
端口全部回落到本机默认值，于是共享地址一律收到
`400 This address is not shared by this Qwen service`。启动日志里明明写着
`0.0.0.0, *.trycloudflare.com, 192.168.1.10`，同一个调用在 import 之后只剩回环名。

修法：这个判断在 import 时定一次，之后不再随 `sys.modules` 漂移。

Run: python -X utf8 -m pytest test_share_scope.py -q
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import qwen_config


class TestsAreRunningIsFixedAtImportTests(unittest.TestCase):
    def test_a_runtime_unittest_import_does_not_flip_it(self):
        """The predicate is a fact about how this process started, not about now."""
        before = qwen_config.tests_are_running()
        if "unittest" not in sys.modules:          # pragma: no cover - pytest imports it
            importlib.import_module("unittest")
        self.assertEqual(qwen_config.tests_are_running(), before)

    def test_the_decision_is_recorded_at_import(self):
        self.assertIsInstance(qwen_config._TESTS_AT_IMPORT, bool)
        self.assertTrue(qwen_config.tests_are_running(),
                        "pytest 进程必须认出自己是测试进程，否则会套用机器上的真实共享配置")

    def test_the_escape_hatch_still_works_at_runtime(self):
        """QWEN_TEST_ISOLATION=0 仍然可以在运行期打开常规路径，供测试自己验收它。"""
        with patch.dict("os.environ", {"QWEN_TEST_ISOLATION": "0"}):
            self.assertFalse(qwen_config.tests_are_running())

    def test_a_process_without_pytest_or_unittest_in_argv_is_not_a_test_run(self):
        """反过来的方向也要成立：真正的服务进程在 import 时必须判成“不是测试”。"""
        script = (
            "import sys, qwen_config\n"
            "print('unittest' in sys.modules, qwen_config.tests_are_running())\n"
        )
        completed = subprocess.run([sys.executable, "-X", "utf8", "-c", script],
                                   cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stderr[-400:])
        loaded, verdict = completed.stdout.split()
        self.assertEqual(loaded, "False")
        self.assertEqual(verdict, "False", "服务进程被判成测试进程，共享配置会被整体忽略")

    def test_the_overlay_survives_a_late_unittest_import(self):
        """端到端：一个像服务那样启动的进程，晚到的 import 之后允许名不变。"""
        script = (
            "import json, sys\n"
            "import qwen_config as c\n"
            "before = c.share_setting('allowed_hosts')\n"
            "import unittest\n"                      # 晚到的 import，正是实测里发生的事
            "after = c.share_setting('allowed_hosts')\n"
            "print(json.dumps({'before': before, 'after': after,\n"
            "                  'off': c.tests_are_running()}, ensure_ascii=False))\n"
        )
        completed = subprocess.run([sys.executable, "-X", "utf8", "-c", script],
                                   cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stderr[-400:])
        verdict = json.loads(completed.stdout.strip().splitlines()[-1])
        if not verdict["before"]:                    # pragma: no cover - 未配置共享
            self.skipTest("这台机器没有 share.config.json")
        self.assertEqual(verdict["after"], verdict["before"],
                         "晚到的 import 把共享覆盖层弄丢了")
        self.assertFalse(verdict["off"], "服务进程被判成测试进程，共享配置会被整体忽略")


if __name__ == "__main__":
    unittest.main()
