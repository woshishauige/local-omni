"""Regressions for the two defects behind "switching to the 9B fails".

Both were the same mistake in different places: a value that belongs to the *model*
being asserted from a global switch.

1. The startup validator computed its expectation from `mtp_enabled` alone, so the 9B —
   whose profile ships no draft head and therefore reads `speculative.types=none` — could
   never satisfy it. Every retry died with "Backend MTP status differs from validated
   configuration." and the model never loaded.
2. Usage accounting fell back to a hardcoded model name, so the 9B's requests were filed
   under the 27B's name and "which model served this" was unanswerable from the log.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import asyncio
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, Mock, patch

import httpx

from app import mode_server as module
from app.qwen_config import DEFAULTS, PROFILES, expected_mtp, mtp_capable, source_switch
from app.usage.usage_tracking import UsageMiddleware

NINE_B = "qwen3.5-9b-uncensored"
TWENTY_SEVEN_B = "qwen3.8-27b"

# 2026-09-23 换血后，随包发布的两个 profile 都没有 MTP 草稿头（三元 27B 不带草稿头）。
# 能力判定的**逻辑**仍然必须被测到，所以"有草稿头"那一支改用一个注入的合成 profile 覆盖，
# 而不是删掉这些断言——否则将来重新引入带草稿头的模型时就没有回归网了。
CAPABLE = "synthetic-mtp-capable"
CAPABLE_PROFILE = {
    "model": "models/synthetic.gguf", "mmproj": "models/synthetic-mmproj.gguf",
    "alias": "synthetic-mtp", "gpu_layers": 1, "vision_gpu_layers": 1,
    "mtp": True, "context_size": 4096,
}


def settings_for(profile, **overrides):
    return DEFAULTS | {"model_profile": profile, "mtp_enabled": True} | overrides


class MtpCapabilityTests(unittest.TestCase):
    def test_no_shipped_profile_ships_a_draft_head_after_the_swap(self):
        """换血后的事实：两个 profile 都读不到草稿头，开关也变不出一个。"""
        for name in PROFILES:
            with self.subTest(profile=name):
                self.assertFalse(mtp_capable(settings_for(name)))
                self.assertFalse(mtp_capable(settings_for(name, mtp_enabled=False)))

    def test_only_the_profile_that_ships_a_draft_head_is_capable(self):
        with patch.dict(PROFILES, {CAPABLE: CAPABLE_PROFILE}):
            self.assertFalse(mtp_capable(settings_for(NINE_B)))
            self.assertFalse(mtp_capable(settings_for(TWENTY_SEVEN_B)))
            self.assertTrue(mtp_capable(settings_for(CAPABLE)))
            # The capability is the weights' property, not the switch's: turning MTP off does
            # not make a model that has a draft head unable to run one.
            self.assertTrue(mtp_capable(settings_for(CAPABLE, mtp_enabled=False)))

    def test_a_profile_without_a_draft_head_can_only_ever_expect_false(self):
        # This is the exact value the old expression produced as True, which is what made
        # the 9B's load fail. It must stay False for every mode and every switch position.
        for mode in ("text", "vision"):
            for unified in (True, False):
                for enabled in (True, False):
                    with self.subTest(mode=mode, unified=unified, enabled=enabled):
                        settings = settings_for(NINE_B, mtp_enabled=enabled)
                        self.assertFalse(expected_mtp(settings, mode, unified=unified))

    def test_a_capable_profile_still_follows_the_switch_and_the_mode(self):
        with patch.dict(PROFILES, {CAPABLE: CAPABLE_PROFILE}):
            settings = settings_for(CAPABLE)
            self.assertTrue(expected_mtp(settings, "text", unified=False))
            self.assertFalse(expected_mtp(settings, "vision", unified=False))
            # Unified backend: one process serves both slices, so text mode keeps the head.
            self.assertTrue(expected_mtp(settings, "vision", unified=True))
            self.assertFalse(expected_mtp(settings_for(CAPABLE, mtp_enabled=False), "text"))

    def test_the_launch_command_and_the_expectation_agree_for_every_profile(self):
        """The two halves of this bug drifted apart; this is the invariant between them."""
        with patch.dict(PROFILES, {CAPABLE: CAPABLE_PROFILE}):
            for name in list(PROFILES):
                for mode in ("text", "vision"):
                    for unified in (True, False):
                        with self.subTest(profile=name, mode=mode, unified=unified):
                            settings = settings_for(name)
                            command = module.server_command(
                                mode, settings=settings,
                                mtp=expected_mtp(settings, mode, unified=unified), mmproj=True,
                            )
                            launched_mtp = "draft-mtp" in command
                            self.assertEqual(launched_mtp, expected_mtp(settings, mode, unified=unified))


class ActiveModelNameTests(unittest.TestCase):
    """`active_model_name` is what the usage log writes when the caller names no model.

    It reads the manager's live settings rather than the file: the backend that is
    answering was launched from those, so they are what the row should say even if
    settings.json has since been edited by hand.
    """

    def resolve_with(self, profile):
        with patch.object(module.manager, "settings", settings_for(profile)):
            return module.active_model_name()

    def test_it_follows_the_selected_profile(self):
        self.assertEqual(self.resolve_with(NINE_B), PROFILES[NINE_B]["model"].stem)
        self.assertEqual(self.resolve_with(TWENTY_SEVEN_B), PROFILES[TWENTY_SEVEN_B]["model"].stem)

    def test_an_unusable_selection_yields_no_name_rather_than_a_failed_request(self):
        """Accounting is fail-open: this must never raise into a model request."""
        with patch.object(module, "active_paths", side_effect=KeyError("model")):
            with patch.object(module, "LOGGER"):
                self.assertIsNone(module.active_model_name())

    def test_it_reports_the_fallback_profile_when_no_profile_is_selected(self):
        """The old fallback named the 27B regardless, so the 9B was never visible."""
        settings = DEFAULTS | {"model_profile": TWENTY_SEVEN_B, "mtp_enabled": True}
        with patch.object(module.manager, "settings", settings):
            self.assertEqual(module.active_model_name(), PROFILES[TWENTY_SEVEN_B]["model"].stem)


class UsageModelFallbackTests(unittest.TestCase):
    def middleware(self, get_model=None):
        return UsageMiddleware(app=None, get_store=lambda: None, get_mode=lambda: "text",
                               get_model=get_model)

    def test_a_named_model_is_recorded_as_asked(self):
        self.assertEqual(self.middleware(lambda: "ignored")._model_name("gpt-6-astra"), "gpt-6-astra")

    def test_an_unnamed_request_is_recorded_as_the_active_model(self):
        # The old fallback was the literal "qwen3.8-27b-local", which is why no row with
        # a 9B name existed even while the 9B was answering.
        self.assertEqual(self.middleware(lambda: "Qwen3.5-9B-Uncensored")._model_name(None),
                         "Qwen3.5-9B-Uncensored")
        self.assertEqual(self.middleware(lambda: "Qwen3.5-9B-Uncensored")._model_name("  "),
                         "Qwen3.5-9B-Uncensored")

    def test_a_resolver_that_raises_does_not_break_accounting(self):
        def broken():
            raise RuntimeError("manager not ready")

        with patch("app.usage.usage_tracking.LOGGER"):
            self.assertEqual(self.middleware(broken)._model_name(None), "unknown-local")

    def test_no_resolver_at_all_is_still_honest_about_not_knowing(self):
        self.assertEqual(self.middleware()._model_name(None), "unknown-local")


class SourceSwitchTests(unittest.TestCase):
    """Same weights = plugin change; different weights = whole-model switch.

    This is the rule the owner stated for every mode, so it lives in one function and is
    asserted directly: a mode that reuses the resident profile must not reload anything,
    and a mode that needs another profile must reload *everything*. Settings are injected
    rather than read from disk, so re-pointing the machine's default cannot change what
    these assert.
    """

    def test_text_and_vision_reuse_the_selected_profile(self):
        """两种模式都用当前选中的档位，**识图不自动换档**。

        2026-09-23 实测：三元 27B 读不了视频（三条短片全部 400），9B 能读。但"识图跑 9B"
        这条规则目前只能靠选档实现——这条映射只被决策层读，`ModeManager.load()` 用的是
        `active_paths(self.settings)`（实机验证：切到识图后 profile 仍是 27B、视频仍 400）。
        所以这里如实断言现状；哪天加载路径改成认 mode 的档位，这条测试要一起改。
        """
        settings = settings_for(NINE_B)
        for mode in ("text", "vision"):
            with self.subTest(mode=mode):
                decision = source_switch(mode, launched=NINE_B, settings=settings)
                self.assertTrue(decision["same_source"])
                self.assertFalse(decision["reload"])
                self.assertEqual(decision["profile"], NINE_B)

    def test_text_reports_a_switch_when_the_resident_model_is_the_other_one(self):
        settings = settings_for(NINE_B)
        decision = source_switch("text", launched=TWENTY_SEVEN_B, settings=settings)
        self.assertFalse(decision["same_source"])
        self.assertTrue(decision["reload"])
        self.assertEqual(decision["profile"], NINE_B)

    def test_voice_cost_nothing_extra_while_the_27b_is_resident(self):
        """语音档位就是默认档位，所以它不再要求换权重——只换窗口与插件。

        2026-09-23 语音从 9B 换到三元 27B 之后，"进语音"这件事从"换一套权重"变成了"同一个
        底座、只换 8192 的窗口 + 加识别器"，这正是同一个来源判定的价值。
        """
        settings = settings_for(TWENTY_SEVEN_B)
        resident = source_switch("voice", launched=TWENTY_SEVEN_B, settings=settings)
        self.assertTrue(resident["same_source"])
        self.assertEqual(resident["profile"], TWENTY_SEVEN_B)
        # 9B 上说话不再是允许的配对：它现在是"另一个来源"，要说清是要换权重。
        foreign = source_switch("voice", launched=NINE_B, settings=settings)
        self.assertFalse(foreign["same_source"])
        self.assertTrue(foreign["reload"])
        self.assertEqual(foreign["profile"], TWENTY_SEVEN_B)
        self.assertIn(NINE_B, foreign["reason"])

    def test_comfyui_modes_always_replace_the_weights(self):
        # image/video run on ComfyUI's own models, so "same source" is never true for them
        # even though they also need no question answered.
        settings = settings_for(NINE_B)
        for mode in ("image", "video"):
            with self.subTest(mode=mode):
                decision = source_switch(mode, launched=NINE_B, settings=settings)
                self.assertFalse(decision["same_source"])
                self.assertTrue(decision["reload"])
                self.assertIsNone(decision["profile"])

    def test_the_resident_profile_wins_over_the_one_settings_names(self):
        # `launched` is what the backend was actually started for. A file that names the
        # selected profile cannot make a resident backend of the other one pass for it.
        settings = settings_for(TWENTY_SEVEN_B)
        decision = source_switch("text", launched=NINE_B, settings=settings)
        self.assertEqual(decision["resident"], NINE_B)
        self.assertFalse(decision["same_source"])


class VoiceModeSwitchTests(unittest.IsolatedAsyncioTestCase):
    """`/local/voice/mode` performs the switch itself instead of asking the owner."""

    async def asyncSetUp(self):
        self.previous_manager = module.manager
        module.manager = module.ModeManager()
        module.manager.state = "ready"
        module.manager.mode = "text"
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),
                                        base_url="http://127.0.0.1:8081")
        self.voice = module.voice_mode.VOICE
        self.saved = {name: getattr(self.voice, name)
                      for name in ("shared_brain_port", "brain_ready", "asr_ready", "last_error")}

    async def asyncTearDown(self):
        for name, value in self.saved.items():
            setattr(self.voice, name, value)
        if module.manager.client:
            await module.manager.client.aclose()
        await self.client.aclose()
        module.manager = self.previous_manager

    async def start_voice(self, **patches):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(self.voice, "ensure_ready", new_callable=AsyncMock,
                                         return_value=patches.get("ensure_ready", True)))
        # share_brain/release_brain are synchronous by design (they only repoint an
        # attribute), so AsyncMock would make them return a coroutine the route never
        # awaits, and the assertions would be against the wrong call.
        for name in ("share_brain", "release_brain"):
            stack.enter_context(patch.object(self.voice, name, new_callable=Mock))
        stack.enter_context(patch.object(self.voice, "stop", new_callable=AsyncMock))
        stack.enter_context(patch.object(module, "_warm_tts", new_callable=AsyncMock))

        loop = asyncio.get_running_loop()

        async def no_op():
            return None

        def schedule(coro):
            # Anything the route backgrounds (the synthesiser warm-up) still has to look
            # like a task to it, so hand back a real one for a no-op instead of None.
            coro.close()
            return loop.create_task(no_op())

        stack.enter_context(patch.object(module.asyncio, "create_task", side_effect=schedule))
        return await self.client.post("/local/voice/mode", json={"profile": TWENTY_SEVEN_B})

    async def test_a_foreign_model_is_switched_without_asking(self):
        module.manager.launched_profile = NINE_B
        seen = []

        async def fake_load(mode):
            seen.append(mode)
            module.manager.launched_profile = TWENTY_SEVEN_B

        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(module.manager, "load", side_effect=fake_load))
        stack.enter_context(patch.object(self.voice, "ensure_ready", new_callable=AsyncMock,
                                         return_value=True))
        for name in ("share_brain", "release_brain"):
            stack.enter_context(patch.object(self.voice, name, new_callable=Mock))
        stack.enter_context(patch.object(module, "_warm_tts", new_callable=AsyncMock))

        response = await self.client.post("/local/voice/mode", json={"profile": TWENTY_SEVEN_B})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["started"])
        self.assertTrue(body["switch"]["reload"])
        # The backend was reloaded for the voice profile itself — no 409, no prompt — and
        # the route waited for it instead of answering first.
        self.assertEqual(seen, ["voice"])
        self.voice.share_brain.assert_called_once()
        self.assertFalse(module.manager.voice_holds_card)
        self.assertEqual(module.manager.mode, "voice")

    async def test_a_borrowed_27b_is_reloaded_when_its_window_is_the_wrong_one(self):
        """Same weights, different window: still a reload.

        The 27B serves chat at 32768 and voice at 8192, because voice always adds the
        recogniser: measured with both up, 8192 left 1220 MiB free while the text window would
        not have fitted beside it. Deciding "same source" from the profile alone therefore
        ships a pairing that cannot generate — the context is part of the switch.
        """
        module.manager.launched_profile = TWENTY_SEVEN_B
        module.manager.metadata = {"props": {"default_generation_settings": {"n_ctx": 32768}}}
        seen = []

        async def fake_load(mode):
            seen.append(mode)
            module.manager.metadata = {"props": {"default_generation_settings": {"n_ctx": 8192}}}

        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(module, "voice_context", return_value=8192))
        stack.enter_context(patch.object(module.manager, "load", side_effect=fake_load))
        stack.enter_context(patch.object(self.voice, "ensure_ready", new_callable=AsyncMock,
                                         return_value=True))
        for name in ("share_brain", "release_brain"):
            stack.enter_context(patch.object(self.voice, name, new_callable=Mock))
        stack.enter_context(patch.object(module, "_warm_tts", new_callable=AsyncMock))

        response = await self.client.post("/local/voice/mode", json={"profile": TWENTY_SEVEN_B})
        body = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen, ["voice"], "the backend should have been reloaded at the voice window")
        switch = body.get("switch") or {}
        self.assertFalse(switch.get("same_source"))
        self.assertEqual(switch.get("context_to"), 8192)
        # Borrowed once, after the reload — not twice, and not before it.
        self.voice.share_brain.assert_called_once()

    async def test_a_resident_27b_is_reused_without_a_reload(self):
        module.manager.launched_profile = TWENTY_SEVEN_B
        load = AsyncMock()
        with patch.object(module.manager, "load", load):
            response = await self.start_voice()

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(body["switch"]["same_source"])
        self.assertFalse(body["switch"]["reload"])
        load.assert_not_awaited()
        self.voice.share_brain.assert_called_once()
        self.assertEqual(module.manager.mode, "voice")

    async def test_a_failed_voice_startup_returns_the_reason_and_the_card(self):
        module.manager.launched_profile = TWENTY_SEVEN_B
        response = await self.start_voice(ensure_ready=False)

        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertFalse(body["started"])
        self.assertIn("switch", body)
        self.voice.release_brain.assert_called_once()
        self.assertFalse(module.manager.voice_holds_card)


if __name__ == "__main__":
    unittest.main()
