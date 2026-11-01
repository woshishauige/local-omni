"""语音档位规则的测试（第 1/2 档：纯静态 + 真实路由，零显存、不排任何渲染）。

真机踩到（所有者反馈）：**选中 9B 时语音对话用不了**。原因不是能力不够，而是规则被写死成
"语音只服务三元 27B"（`voice_mode.profile_allowed` 与 `/local/voice/config` 的 `locked: true`），
于是工具栏把语音标签变灰、路由也会 409 拒绝。可三份档案里**只有旧版 IQ4_XS 真的装不下**
（14.25 GB + 识别器 > 12 GB），9B 的语音窗口一直是 40960、三元 27B 是 8192。

这些用例钉住新规则：**跟当前选中的档位走，能不能用由那份档案自己的 `voice_context_size` 决定**，
而且拒绝时必须说清是哪一条不满足。
"""
import unittest
from unittest.mock import patch

import mode_server as module
import qwen_config
import voice_mode
from test_access_gate import GateCase


class VoiceProfileRuleTests(unittest.TestCase):
    def test_the_9b_is_allowed_and_keeps_its_own_voice_window(self):
        self.assertTrue(voice_mode.VoiceMode.profile_allowed("qwen3.5-9b-uncensored"))
        self.assertEqual(voice_mode.brain_context("qwen3.5-9b-uncensored"), 40960)

    def test_the_ternary_27b_is_allowed_with_its_smaller_window(self):
        self.assertTrue(voice_mode.VoiceMode.profile_allowed("qwen3.8-27b"))
        self.assertEqual(voice_mode.brain_context("qwen3.8-27b"), 8192)

    def test_the_old_27b_is_refused_because_it_has_no_voice_window(self):
        self.assertFalse(voice_mode.VoiceMode.profile_allowed("qwen3.8-27b-iq4xs"))
        reason = voice_mode.VoiceMode.refusal_message("qwen3.8-27b-iq4xs")
        self.assertIn("没有语音窗口", reason)
        self.assertIn("9B", reason, "拒绝话里要给出可行的替代")

    def test_an_unknown_profile_is_refused_with_its_own_reason(self):
        self.assertFalse(voice_mode.VoiceMode.profile_allowed("nope"))
        self.assertIn("不认识", voice_mode.VoiceMode.refusal_message("nope"))

    def test_a_profile_whose_weights_are_missing_is_refused(self):
        missing = "qwen3.5-9b-uncensored"
        with patch.dict(qwen_config.PROFILES, {missing: {**qwen_config.PROFILES[missing],
                                                          "model": qwen_config.ROOT / "nope.gguf"}}):
            self.assertFalse(voice_mode.VoiceMode.profile_allowed(missing))
            self.assertIn("不在本机", voice_mode.VoiceMode.refusal_message(missing))

    def test_voice_follows_the_selected_profile(self):
        """`MODE_PROFILE_SOURCE` 说语音用"当前选中档位"——写回固定档位就是这次那个 bug。"""
        self.assertEqual(qwen_config.MODE_PROFILE_SOURCE["voice"], "active")

    def test_the_brain_is_taken_from_the_profile_not_from_a_constant(self):
        """脑的三件套必须按档位现取：三元 27B 只在 PrismML fork 上加载。"""
        nine = voice_mode.brain_llama("qwen3.5-9b-uncensored")
        ternary = voice_mode.brain_llama("qwen3.8-27b")
        self.assertTrue(str(nine).endswith("llama-server.exe"))
        self.assertTrue(str(ternary).endswith("llama-server.exe"))
        model, projector = voice_mode.brain_paths("qwen3.5-9b-uncensored")
        self.assertTrue(model.is_file(), model)
        self.assertTrue(str(projector).endswith((".gguf", ".mmproj")))


class VoiceConfigRouteTests(GateCase):
    """`/local/voice/config` 是客户端唯一的依据：它必须说"支持 9B"。"""

    def setUp(self):
        super().setUp()
        self.previous = module.manager.settings
        self.addCleanup(setattr, module.manager, "settings", self.previous)

    def select(self, profile):
        module.manager.settings = {**self.previous, "model_profile": profile}

    async def test_the_config_reports_the_selected_profile_as_supported(self):
        self.select("qwen3.5-9b-uncensored")
        payload = (await self.get("/local/voice/config")).json()
        self.assertEqual(payload["profile"], "qwen3.5-9b-uncensored")
        self.assertTrue(payload["supported"], payload)
        self.assertFalse(payload["locked"], "档位不再锁死，客户端就是靠这个放行语音标签的")
        self.assertEqual(payload["context_size"], 40960)
        self.assertEqual(payload["reason"], "")

    async def test_the_config_refuses_the_profile_without_a_voice_window(self):
        self.select("qwen3.8-27b-iq4xs")
        payload = (await self.get("/local/voice/config")).json()
        self.assertFalse(payload["supported"])
        self.assertIn("没有语音窗口", payload["reason"])
        self.assertIsNone(payload["context_size"])


if __name__ == "__main__":
    unittest.main()
