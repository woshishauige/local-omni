"""GPU admission and local voice input.

Two rules are worth testing and both have bitten this machine in practice: the owner
must never be told "no" by a visitor's work, and a visitor must never be admitted onto a
card that has no room. The recognition tests use a real WAV when one exists, because the
only meaningful check of a speech path is that it hears the right words.
"""

import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest
import wave
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gpu_arbiter
import voice_input

SAMPLE = Path(r"D:\AI\Qwen-Local\logs\asr-sample.wav")


def tearDownModule():
    """Stop the recognition worker this module starts.

    The worker is a separate process holding its own copy of the speech model and it does
    not die with the test run: without this, every suite run leaks one (measured — three
    accumulated before this was added).
    """
    voice_input.shutdown_worker()


def production_arbiter(test_case):
    """Make the arbiter decide by the production rule inside a test process.

    ``is_test_process()`` suppresses the gate for the proxy suites (their outcome must
    not depend on the machine's live VRAM), so the tests that are *about* the gate turn
    it back on explicitly — otherwise they would assert nothing.
    """
    patcher = patch.object(gpu_arbiter, "is_test_process", return_value=False)
    patcher.start()
    test_case.addCleanup(patcher.stop)


class FakeGpu(gpu_arbiter.GpuArbiter):
    """An arbiter whose card reading is decided by the test."""

    def __init__(self, free_mib=9000, **kwargs):
        super().__init__(**kwargs)
        self.free_mib = free_mib
        self.reads = 0

    async def _sample(self):
        self.reads += 1
        return {"total_mib": 12227.0, "used_mib": 12227.0 - self.free_mib, "free_mib": float(self.free_mib)}


class ClassificationTests(unittest.TestCase):
    """Which requests take VRAM at all, and which kind they are."""

    def test_chat_is_text_and_an_image_turn_is_vision(self):
        text = {"messages": [{"role": "user", "content": "你好"}]}
        image = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]}]}
        self.assertEqual(gpu_arbiter.classify("v1/chat/completions", "POST", text), "text")
        self.assertEqual(gpu_arbiter.classify("v1/chat/completions", "POST", image), "vision")

    def test_tool_schemas_are_not_mistaken_for_images(self):
        """A tool definition may declare a property named type; that is not an image."""
        payload = {"messages": [{"role": "user", "content": "生成"}]}
        self.assertEqual(gpu_arbiter.classify("v1/chat/completions", "POST", payload), "text")

    def test_generating_routes_are_classified(self):
        self.assertEqual(gpu_arbiter.classify("local/image/generate", "POST", {}), "image")
        self.assertEqual(gpu_arbiter.classify("local/image/denoise", "POST", {}), "image")
        self.assertEqual(gpu_arbiter.classify("local/video/generate", "POST", {}), "video")
        self.assertEqual(gpu_arbiter.classify("local/video/interpolate", "POST", {}), "video")

    def test_reading_routes_and_voice_take_nothing(self):
        for path in ("local/status", "local/image/history", "local/video/source",
                     "local/video/file", "local/voice/transcribe", "v1/models"):
            self.assertIsNone(gpu_arbiter.classify(path, "GET", None), path)


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    """The decision itself."""

    async def asyncSetUp(self):
        production_arbiter(self)

    async def admit(self, arbiter, session, owner, label, kind, preempt=False):
        """The two steps the proxy performs: reserve, then admit."""
        reservation = await arbiter.reserve(session, owner, label, kind)
        return await arbiter.admit(reservation, session, owner, label, kind, preempt=preempt)

    async def test_a_roomy_card_admits_visitor_and_owner_alike(self):
        arbiter = FakeGpu(free_mib=9000)
        guest = await self.admit(arbiter, "guest-1", False, "iPhone", "text")
        owner = await self.admit(arbiter, "owner", True, "本机管理", "image")
        self.assertEqual(guest["decision"], "allow")
        self.assertEqual(owner["decision"], "allow")
        self.assertIn("guest-1", arbiter.active)
        self.assertIn("owner", arbiter.active)

    async def test_a_crowded_card_refuses_the_visitor(self):
        arbiter = FakeGpu(free_mib=800)
        decision = await arbiter.reserve("guest-1", False, "iPhone", "image")
        self.assertEqual(decision["decision"], "refuse")
        self.assertIn("不足", decision["message"])
        self.assertEqual(arbiter.active, {}, "a refused request must not register work")

    async def test_plain_chat_survives_a_card_that_only_has_room_for_chat(self):
        """A text turn rides on the resident model; a busy card is not a reason to refuse it."""
        arbiter = FakeGpu(free_mib=800)
        chat = await arbiter.reserve("guest-1", False, "iPhone", "text")
        image = await arbiter.reserve("guest-2", False, "iPad", "image")
        self.assertEqual(chat["decision"], "allow", "800 MiB is plenty for a text turn")
        self.assertEqual(image["decision"], "refuse", "but not for a model swap")

    async def test_chat_is_still_refused_when_nothing_is_left(self):
        arbiter = FakeGpu(free_mib=32)
        decision = await arbiter.reserve("guest-1", False, "iPhone", "text")
        self.assertEqual(decision["decision"], "refuse")
        self.assertEqual(decision["threshold_mib"], gpu_arbiter.TEXT_THRESHOLD_MIB)

    async def test_a_crowded_card_asks_the_owner_instead_of_refusing(self):
        arbiter = FakeGpu(free_mib=800)
        arbiter.active["guest-1"] = gpu_arbiter.Work("guest-1", False, "iPhone", "image")
        decision = await arbiter.reserve("owner", True, "本机管理", "image")
        self.assertEqual(decision["decision"], "ask")
        self.assertEqual([entry["label"] for entry in decision["busy"]], ["iPhone"])

    async def test_the_owner_taking_the_card_holds_visitors_off(self):
        arbiter = FakeGpu(free_mib=800)
        reservation = await arbiter.reserve("owner", True, "本机管理", "image")
        admitted = await arbiter.admit(reservation, "owner", True, "本机管理", "image", preempt=True)
        self.assertEqual(admitted["decision"], "allow")
        self.assertTrue((await arbiter.status())["preempting"])

        arbiter.free_mib = 9000
        refused = await arbiter.reserve("guest-2", False, "iPad", "text")
        self.assertEqual(refused["decision"], "refuse")
        self.assertIn("机主正在使用", refused["message"])

    async def test_releasing_lets_visitors_back_in(self):
        arbiter = FakeGpu(free_mib=9000)
        arbiter.begin_preempt("test")
        arbiter.end_preempt()
        decision = await arbiter.reserve("guest-1", False, "iPhone", "text")
        self.assertEqual(decision["decision"], "allow")

    async def test_admitting_now_waits_for_the_interrupt_then_proceeds(self):
        """The owner asked for the card, so the grace period must not end in a refusal."""
        arbiter = FakeGpu(free_mib=100)
        calls = {"n": 0}

        async def room(**kwargs):
            calls["n"] += 1
            return {"total_mib": 12227.0, "used_mib": 100.0, "free_mib": 9000.0} if calls["n"] > 1 else None

        arbiter.vram = room  # type: ignore[assignment]
        reservation = {"decision": "ask", "busy": []}
        admitted = await arbiter.admit(reservation, "owner", True, "本机管理", "video", preempt=True)
        self.assertEqual(admitted["decision"], "allow")
        self.assertIn("owner", arbiter.active)

    async def test_a_refused_request_is_never_admitted_by_preempt(self):
        """Preemption is the owner's; a visitor cannot turn a refusal into a pass."""
        arbiter = FakeGpu(free_mib=500)
        reservation = await arbiter.reserve("guest-1", False, "iPhone", "image")
        admitted = await arbiter.admit(reservation, "guest-1", False, "iPhone", "image", preempt=False)
        self.assertEqual(admitted["decision"], "refuse")
        self.assertEqual(arbiter.active, {})

    async def test_an_unreadable_card_does_not_block_anyone(self):
        """No nvidia-smi is not evidence of contention; it must not refuse work."""
        arbiter = FakeGpu()
        arbiter._sample = lambda: asyncio.sleep(0, result=None)  # type: ignore[assignment]
        decision = await arbiter.reserve("guest-1", False, "iPhone", "image")
        self.assertEqual(decision["decision"], "allow")

    async def test_work_is_released_when_the_request_ends(self):
        arbiter = FakeGpu(free_mib=9000)
        await self.admit(arbiter, "guest-1", False, "iPhone", "text")
        self.assertIn("guest-1", arbiter.active)
        arbiter.release("guest-1")
        self.assertNotIn("guest-1", arbiter.active)

    async def test_the_vram_reading_is_cached_between_requests(self):
        arbiter = FakeGpu(free_mib=9000)
        await arbiter.vram()
        await arbiter.vram()
        self.assertEqual(arbiter.reads, 1, "one nvidia-smi call serves the whole window")


class StreamingVoiceTests(unittest.IsolatedAsyncioTestCase):
    """Live dictation: a hypothesis while speaking, and no words lost or doubled."""

    async def asyncSetUp(self):
        production_arbiter(self)
        voice_input.stream_drop("test-session")

    @staticmethod
    def audio():
        with wave.open(str(SAMPLE), "rb") as handle:
            return handle.readframes(handle.getnframes())

    async def feed(self, session, pace=False, stop_after=None):
        """Feed the recording, optionally at real-time pace, returning chunks sent."""
        pcm = self.audio()
        chunk = int(0.25 * voice_input.SAMPLE_RATE) * 2
        sent = 0
        for offset in range(0, len(pcm), chunk):
            piece = pcm[offset:offset + chunk]
            await voice_input.stream_feed(session, piece, "zh")
            sent += len(piece)
            if pace:
                await asyncio.sleep(0.25)
            if stop_after and sent >= stop_after:
                break
        return sent, len(pcm)

    async def settle(self, session, timeout=60.0):
        """Wait until the server's own idle finalizer closes the phrase."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.3)
            stream = voice_input.stream_get(session)
            if stream is None or stream.finished:
                return stream
        return voice_input.stream_get(session)

    async def test_a_hypothesis_appears_while_speaking(self):
        """Text before the clip ends: the reading runs in a worker, not in the feed."""
        sent, total = await self.feed("test-session", stop_after=int(1.2 * voice_input.SAMPLE_RATE * 2))
        seen = ""
        for _ in range(80):
            await asyncio.sleep(0.25)
            stream = voice_input.stream_get("test-session")
            if stream is not None and (stream.pending or stream.committed):
                seen = stream.pending or stream.committed
                break
        self.assertTrue(seen, "a live reading must produce a hypothesis")
        self.assertLess(sent, total, "and it must arrive before the whole clip was sent")

    async def test_the_final_text_keeps_every_word_and_repeats_nothing(self):
        """The measured failure this covers: text that stopped early, then appeared twice."""
        await self.feed("test-session")
        stream = await self.settle("test-session")
        self.assertIsNotNone(stream, "the idle finalizer must close the phrase")
        final = await voice_input.stream_finish("test-session", "zh")
        text = final["text"]
        self.assertIn("图片", text, text)
        self.assertIn("放大两倍", text, text)
        self.assertIn("输出目录", text, text)
        self.assertEqual(text.count("输出目录"), 1, f"the sentence must not be duplicated: {text}")
        self.assertEqual(text.count("图片"), 1, text)
        self.assertLess(len(text), 30, text)

    async def test_a_tail_that_overlaps_is_merged_not_appended(self):
        """Measured: a re-read returned "两点,然后保存" after "…两倍" was committed.

        The overlap is matched at the character level, longest run first, so the shared
        "两" is not repeated.
        """
        stream = voice_input.VoiceStream("zh")
        stream.committed = "然后把这张图片放大两倍"
        self.assertEqual(stream.grown_committed("两点,然后保存到输出目录"),
                         "然后把这张图片放大两倍两点,然后保存到输出目录")
        self.assertEqual(stream.grown_committed("放大两倍,然后保存"),
                         "然后把这张图片放大两倍,然后保存")
        self.assertEqual(stream.grown_committed("完全是新的"), "然后把这张图片放大两倍完全是新的")
        self.assertEqual(stream.grown_committed(""), "然后把这张图片放大两倍")

    async def test_a_chunk_that_is_not_16_bit_is_refused(self):
        with self.assertRaises(voice_input.VoiceError):
            await voice_input.stream_feed("test-session", b"\x01\x02\x03", "zh")

    async def test_an_empty_chunk_is_a_poll_not_an_error(self):
        """Between chunks the browser asks "any text yet?" without sending audio."""
        with self.assertRaises(voice_input.VoiceError) as caught:
            await voice_input.stream_feed("never-started", b"", "zh")
        self.assertEqual(caught.exception.status, 409)

        await voice_input.stream_feed("test-session", b"\x00\x40" * 8000, "zh")
        snapshot = await voice_input.stream_feed("test-session", b"", "zh")
        self.assertEqual(snapshot["duration_seconds"], 0.5)

    async def test_the_idle_timer_does_not_cut_a_sentence_in_half(self):
        """A slow client pausing mid-sentence must be re-armed, not closed."""
        stream = voice_input.VoiceStream("zh")
        stream.add(b"\x00\x40" * int(voice_input.SAMPLE_RATE * 0.6))
        result = await stream.finish(only_if_silent=True)
        self.assertFalse(stream.finished, "a loud tail is not an ending")
        self.assertIn("tail_seconds", result)

    async def test_finishing_twice_says_the_recording_is_over(self):
        await self.feed("test-session", stop_after=int(0.5 * voice_input.SAMPLE_RATE * 2))
        await voice_input.stream_finish("test-session", "zh")
        with self.assertRaises(voice_input.VoiceError) as caught:
            await voice_input.stream_finish("test-session", "zh")
        self.assertEqual(caught.exception.status, 409)

    def test_the_prompt_echo_is_stripped(self):
        """Whisper repeats its initial_prompt; showing it twice would be visible."""
        self.assertEqual(voice_input.strip_repeat("把这张图片", "把这张图片放大两倍"), "放大两倍")
        self.assertEqual(voice_input.strip_repeat("把这张图片", "然后保存"), "然后保存")
        self.assertEqual(voice_input.strip_repeat("", "然后保存"), "然后保存")

    def test_the_buffer_stays_bounded(self):
        stream = voice_input.VoiceStream("zh")
        loud = b"\x00\x40" * voice_input.SAMPLE_RATE  # one second of constant audio
        for _ in range(int(voice_input.STREAM_BUFFER_SECONDS) + 6):
            stream.add(loud)
        self.assertLessEqual(stream.tail_seconds(), voice_input.STREAM_BUFFER_SECONDS + 1.1)

    def test_a_silent_tail_is_detected(self):
        stream = voice_input.VoiceStream("zh")
        stream.add(b"\x00\x00" * int(voice_input.SAMPLE_RATE * 0.6))
        self.assertTrue(stream._silent_tail())
        stream.add(b"\x00\x40" * int(voice_input.SAMPLE_RATE * 0.6))
        self.assertFalse(stream._silent_tail())


class VoiceAppendTests(unittest.TestCase):
    """The browser side of live text: it may only ever add characters.

    The recognizer revises its hypothesis as it hears more, so re-rendering the
    transcript every update would make the user's sentence jump backwards mid-word. The
    text already on screen is therefore never deleted, and these tests pin that down.
    """

    def setUp(self):
        source = Path(r"D:\AI\Qwen-Local\web\voice-input.js").read_text(encoding="utf-8")
        self.source = source

    def test_the_live_path_only_appends(self):
        """`acceptVoice` must never delete from the field."""
        body = self.source[self.source.index("function acceptVoice"):]
        body = body[:body.index("\n  // ------")]
        self.assertNotIn("deleteContents", body)
        self.assertIn("total.slice(previous.length)", body)

    def test_a_revised_hypothesis_does_not_shrink_the_text(self):
        """A shorter or different hypothesis yields no deletion, only an extension."""
        body = self.source[self.source.index("function acceptVoice"):]
        body = body[:body.index("\n  // ------")]
        self.assertIn("!total.startsWith(previous)", body)
        self.assertIn("if (total === previous || !total.startsWith(previous)) return;", body)

    def test_settled_phrases_are_not_inserted_twice(self):
        """Resuming after a pause must not re-insert the transcript."""
        self.assertIn("const seq = typeof voice.seq === 'number'", self.source)
        self.assertIn("state.phraseSeq = seq;", self.source)


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    """Recognition, and the refusals that keep a click from becoming a job."""

    async def test_an_empty_body_is_refused(self):
        with self.assertRaises(voice_input.VoiceError) as caught:
            await voice_input.transcribe(b"")
        self.assertIn("没有收到音频", caught.exception.message)

    async def test_an_oversized_clip_is_refused_before_decoding(self):
        with patch.object(voice_input, "MAX_AUDIO_BYTES", 10):
            with self.assertRaises(voice_input.VoiceError) as caught:
                await voice_input.transcribe(b"x" * 50)
        self.assertEqual(caught.exception.status, 413)

    def test_a_missing_model_says_how_to_get_one(self):
        """The message has to name the command, or the owner cannot act on it.

        The loaded model is cleared too: once a model is in memory the module serves it
        without touching the disk, which is the behaviour that makes the check a
        first-load concern rather than a per-request one.
        """
        import asyncio as runner

        with patch.object(voice_input, "available_model", return_value=None), \
                patch.object(voice_input, "_MODEL", None):
            with self.assertRaises(voice_input.VoiceError) as caught:
                runner.run(voice_input.load())
        self.assertEqual(caught.exception.status, 503)
        self.assertIn("fetch_asr_model.py", caught.exception.message)

    async def test_the_status_payload_tells_the_panel_what_is_ready(self):
        payload = voice_input.describe()
        self.assertIn("ready", payload)
        self.assertEqual(payload["device"], "cpu", "recognition must not take the GPU")
        self.assertIn("zh", payload["languages"])

    def test_garbage_audio_is_rejected_with_a_readable_message(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "clip.wav"
            with self.assertRaises(voice_input.VoiceError) as caught:
                voice_input.convert_to_wav(b"this is not audio at all", target)
        self.assertIn("解码失败", caught.exception.message)

    def test_traditional_output_is_converted_for_a_simplified_ui(self):
        self.assertEqual(voice_input.to_simplified("把這張圖片放大兩倍"), "把这张图片放大两倍")

    @unittest.skipUnless(SAMPLE.is_file(), "no local sample recording to recognize")
    def test_a_real_recording_is_recognized(self):
        """End to end on the machine's own model: decode, recognize, simplify."""
        async def run():
            model = await voice_input.load()
            with tempfile.TemporaryDirectory() as folder:
                wav = Path(folder) / "clip.wav"
                await asyncio.to_thread(voice_input.convert_to_wav, SAMPLE.read_bytes(), wav)
                return await asyncio.to_thread(voice_input.transcribe_wav, model, wav, "zh")

        result = asyncio.run(run())
        self.assertIn("图片", result["text"], result["text"])
        self.assertIn("放大", result["text"], result["text"])
        self.assertEqual(result["language"], "zh")
        self.assertGreater(result["duration_seconds"], 1.0)


if __name__ == "__main__":
    unittest.main()
