import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import unittest
from unittest.mock import patch

from app.usage import usage_capture as module


def frame(value, event=None, newline="\n"):
    body = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    prefix = f"event: {event}{newline}" if event else ""
    return (prefix + "data: " + body + newline * 2).encode("utf-8")


class UsageCaptureTests(unittest.TestCase):
    def setUp(self):
        self.clock_patch = patch.object(module.time, "monotonic", return_value=100.0)
        self.clock = self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.capture = module.UsageCapture(start_monotonic=100.0)

    def finish(self, at=102.0):
        self.clock.return_value = at
        return self.capture.finish_metrics()

    def test_native_nonstream_uses_reported_counts_and_decode_speed(self):
        self.capture.consume_json({
            "content": "generated text", "stop": True,
            "tokens_evaluated": 41, "tokens_predicted": 128, "tokens_cached": 168,
            "timings": {"cache_n": 0, "prompt_n": 41, "predicted_n": 128,
                        "predicted_ms": 18686.406, "predicted_per_second": 6.796384494696305},
        })
        metrics = self.finish(at=120)
        self.assertEqual(metrics["input_tokens"], 41)
        self.assertEqual(metrics["output_tokens"], 128)
        self.assertEqual(metrics["cache_read_tokens"], 0)
        self.assertIsNone(metrics["cache_write_tokens"])
        self.assertEqual(metrics["tokens_per_second"], 6.796)
        self.assertIsNone(metrics["ttft_ms"])
        self.assertEqual(metrics["duration_ms"], 20000)
        self.assertTrue(metrics["usage_known"])
        self.assertTrue(metrics["had_output"])
        self.assertTrue(metrics["completed"])

    def test_native_timings_add_cached_and_evaluated_prompt_tokens(self):
        self.capture.consume_json({"stop": True, "timings": {
            "prompt_n": 12, "cache_n": 88, "predicted_n": 3, "predicted_ms": 200,
        }})
        metrics = self.finish()
        self.assertEqual(metrics["input_tokens"], 100)
        self.assertEqual(metrics["cache_read_tokens"], 88)
        self.assertEqual(metrics["output_tokens"], 3)
        self.assertEqual(metrics["tokens_per_second"], 10)

    def test_prompt_evaluation_alone_does_not_guess_total_or_cache_usage(self):
        self.capture.consume_json({"tokens_cached": 900, "timings": {"prompt_n": 12, "predicted_n": 3}})
        metrics = self.finish()
        self.assertIsNone(metrics["input_tokens"])
        self.assertIsNone(metrics["cache_read_tokens"])
        self.assertIsNone(metrics["cache_write_tokens"])
        self.assertFalse(metrics["usage_known"])

    def test_usage_is_authoritative_over_native_counts_and_timings(self):
        self.capture.consume_json({
            "tokens_evaluated": 99, "tokens_predicted": 7,
            "timings": {"prompt_n": 20, "cache_n": 30, "predicted_n": 6},
            "usage": {"prompt_tokens": 100, "completion_tokens": 8,
                      "prompt_tokens_details": {"cached_tokens": 40}},
        })
        self.capture.consume_json({"timings": {"prompt_n": 1, "cache_n": 0, "predicted_n": 0}})
        metrics = self.finish()
        self.assertEqual((metrics["input_tokens"], metrics["output_tokens"], metrics["cache_read_tokens"]),
                         (100, 8, 40))

    def test_chat_sse_usage_only_final_chunk_and_done(self):
        self.clock.return_value = 100.25
        self.capture.feed(frame({"choices": [{"index": 0, "delta": {"role": "assistant"}}]}))
        self.clock.return_value = 100.5
        self.capture.feed(frame({"choices": [{"index": 0, "delta": {"content": "你好"}}]}))
        self.capture.feed(frame({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}))
        final = {"choices": [], "usage": {"prompt_tokens": 15, "completion_tokens": 2,
                 "prompt_tokens_details": {"cached_tokens": 0}},
                 "timings": {"cache_n": 0, "prompt_n": 15, "predicted_n": 2,
                             "predicted_ms": 214.775, "predicted_per_second": 4.656}}
        self.capture.feed(frame(final))
        self.capture.feed(frame(final))
        self.capture.feed(frame("[DONE]"))
        metrics = self.finish()
        self.assertEqual(metrics["ttft_ms"], 500)
        self.assertEqual(metrics["input_tokens"], 15)
        self.assertEqual(metrics["output_tokens"], 2)
        self.assertEqual(metrics["tokens_per_second"], 4.656)
        self.assertTrue(metrics["completed"])
        self.assertFalse(metrics["stream_error"])

    def test_cumulative_usage_snapshots_replace_instead_of_add(self):
        for count in (0, 1, 5, 5):
            self.capture.feed(frame({"usage": {"prompt_tokens": 20, "completion_tokens": count}}))
        self.capture.feed(frame("[DONE]"))
        self.assertEqual(self.finish()["output_tokens"], 5)

    def test_reasoning_and_content_do_not_increment_token_counts(self):
        self.clock.return_value = 100.2
        self.capture.feed(frame({"choices": [{"delta": {"reasoning_content": "思考"}}]}))
        self.clock.return_value = 100.6
        self.capture.feed(frame({"choices": [{"delta": {"content": "答案"}}]}))
        self.capture.feed(frame({"usage": {"prompt_tokens": 10, "completion_tokens": 7,
                                          "completion_tokens_details": {"reasoning_tokens": 4}}}))
        self.capture.feed(frame("[DONE]"))
        metrics = self.finish()
        self.assertEqual(metrics["ttft_ms"], 200)
        self.assertEqual(metrics["output_tokens"], 7)

    def test_tool_metadata_and_empty_deltas_do_not_start_ttft(self):
        self.clock.return_value = 100.1
        for delta in ({"role": "assistant"}, {"content": ""}, {"reasoning_content": ""},
                      {"tool_calls": [{"index": 0, "id": "call1", "function": {"name": "run"}}]}):
            self.capture.feed(frame({"choices": [{"delta": delta}]}))
        self.clock.return_value = 100.7
        self.capture.feed(frame({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "{"}},
        ]}}]}))
        self.capture.feed(frame("[DONE]"))
        metrics = self.finish()
        self.assertEqual(metrics["ttft_ms"], 700)
        self.assertIsNone(metrics["output_tokens"])

    def test_legacy_function_call_arguments_start_ttft(self):
        self.clock.return_value = 100.4
        self.capture.feed(frame({"choices": [{"delta": {"function_call": {"arguments": "{}"}}}]}))
        self.assertEqual(self.finish()["ttft_ms"], 400)

    def test_openai_text_completion_stream_and_nonstream(self):
        self.clock.return_value = 100.3
        self.capture.feed(frame({"choices": [{"text": "answer", "finish_reason": "length"}],
                                 "usage": {"prompt_tokens": 5, "completion_tokens": 2}}))
        metrics = self.finish()
        self.assertEqual(metrics["ttft_ms"], 300)
        self.assertTrue(metrics["completed"])
        capture = module.UsageCapture(100)
        capture.consume_json({"choices": [{"text": "answer", "finish_reason": "stop"}],
                              "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
        self.assertIsNone(capture.finish_metrics()["ttft_ms"])
        self.assertTrue(capture.finish_metrics()["completed"])

    def test_responses_delta_and_nested_final_usage(self):
        self.capture.feed(frame({"type": "response.created", "response": {"status": "in_progress"}}))
        self.clock.return_value = 100.4
        self.capture.feed(frame({"type": "response.output_text.delta", "delta": "answer"}))
        self.capture.feed(frame({"type": "response.completed", "response": {
            "status": "completed", "usage": {"input_tokens": 30, "output_tokens": 4,
                                              "input_tokens_details": {"cached_tokens": 12}},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "answer"}]}],
        }}))
        metrics = self.finish()
        self.assertEqual((metrics["input_tokens"], metrics["output_tokens"], metrics["cache_read_tokens"]),
                         (30, 4, 12))
        self.assertEqual(metrics["ttft_ms"], 400)
        self.assertTrue(metrics["completed"])

    def test_responses_tool_arguments_and_sse_event_type(self):
        self.capture.feed(frame({"item": {"type": "function_call", "name": "run"}},
                                event="response.output_item.added"))
        self.clock.return_value = 100.8
        self.capture.feed(frame({"delta": "{}"}, event="response.function_call_arguments.delta"))
        self.capture.feed(frame({"response": {"status": "completed"}}, event="response.completed"))
        metrics = self.finish()
        self.assertEqual(metrics["ttft_ms"], 800)
        self.assertTrue(metrics["completed"])

    def test_responses_nonstream_ttft_is_unknown(self):
        self.capture.consume_json({"status": "completed", "output": [
            {"type": "function_call", "name": "run", "arguments": "{}"},
        ], "usage": {"input_tokens": 4, "output_tokens": 1}})
        metrics = self.finish()
        self.assertTrue(metrics["had_output"])
        self.assertIsNone(metrics["ttft_ms"])
        self.assertTrue(metrics["completed"])

    def test_nonstream_responses_terminal_outcomes_are_explicit(self):
        for status, expected in (("incomplete", "incomplete"), ("cancelled", "cancelled"), ("failed", "error")):
            for wrapped in (False, True):
                with self.subTest(status=status, wrapped=wrapped):
                    capture = module.UsageCapture(100)
                    response = {"status": status, "usage": {"input_tokens": 3, "output_tokens": 2}}
                    capture.consume_json({"response": response} if wrapped else response)
                    metrics = capture.finish_metrics()
                    self.assertEqual(metrics["terminal_status"], expected)
                    self.assertFalse(metrics["completed"])
                    self.assertEqual(metrics["stream_error"], expected == "error")
                    self.assertFalse(metrics["capture_error"])

    def test_backend_error_retains_priority_over_later_terminal_events(self):
        self.capture.consume_json({"error": {"message": "failure"}})
        self.capture.consume_json({"status": "incomplete"})
        self.capture.consume_json({"status": "completed"})
        metrics = self.finish()
        self.assertEqual(metrics["terminal_status"], "error")
        self.assertFalse(metrics["completed"])

    def test_native_stream_stop_is_completion_without_done_marker(self):
        self.clock.return_value = 100.3
        self.capture.feed(frame({"content": "answer", "stop": False}))
        self.capture.feed(frame({"content": "", "stop": True, "tokens_evaluated": 5, "tokens_predicted": 2}))
        self.assertTrue(self.finish()["completed"])

    def test_explicit_zero_is_known_but_missing_usage_is_not(self):
        empty = self.finish()
        self.assertIsNone(empty["input_tokens"])
        self.assertIsNone(empty["output_tokens"])
        self.assertIsNone(empty["tokens_per_second"])
        self.assertFalse(empty["usage_known"])
        capture = module.UsageCapture(100)
        capture.consume_json({"usage": {"input_tokens": 0, "output_tokens": 0}})
        metrics = capture.finish_metrics()
        self.assertTrue(metrics["usage_known"])
        self.assertEqual(metrics["output_tokens"], 0)

    def test_cache_writes_require_an_explicit_reported_counter(self):
        self.capture.consume_json({"usage": {"input_tokens": 20, "output_tokens": 3,
            "cache_read_input_tokens": 5, "cache_creation_input_tokens": 10}})
        metrics = self.finish()
        self.assertEqual(metrics["cache_read_tokens"], 5)
        self.assertEqual(metrics["cache_write_tokens"], 10)

    def test_bad_counter_types_are_ignored(self):
        for invalid in (True, -1, 1.5, "3", {}, [], None, 2**63):
            self.capture.consume_json({"usage": {"input_tokens": invalid, "output_tokens": invalid}})
        metrics = self.finish()
        self.assertFalse(metrics["usage_known"])
        self.assertIsNone(metrics["tokens_per_second"])

    def test_invalid_and_nonfinite_server_speeds_do_not_escape(self):
        for invalid in (float("nan"), float("inf"), -1, 10**500):
            self.capture.consume_json({"timings": {"predicted_per_second": invalid}})
        self.assertIsNone(self.finish()["tokens_per_second"])

    def test_role_only_stream_never_estimates_output_tokens_or_ttft(self):
        self.capture.feed(frame({"choices": [{"delta": {"role": "assistant"}}]}))
        self.capture.feed(frame("[DONE]"))
        metrics = self.finish()
        self.assertTrue(metrics["completed"])
        self.assertFalse(metrics["had_output"])
        self.assertIsNone(metrics["ttft_ms"])
        self.assertIsNone(metrics["output_tokens"])

    def test_multi_choice_finish_waits_for_all_observed_choices(self):
        self.capture.feed(frame({"choices": [{"index": 0, "delta": {"content": "a"}},
                                              {"index": 1, "delta": {"content": "b"}}]}))
        self.capture.feed(frame({"choices": [{"index": 0, "finish_reason": "stop"}]}))
        self.assertFalse(self.capture.completed)
        self.capture.feed(frame({"choices": [{"index": 1, "finish_reason": "stop"}]}))
        self.assertTrue(self.capture.completed)

    def test_finish_is_idempotent_and_future_chunks_are_ignored(self):
        self.capture.feed(frame("[DONE]"))
        first = self.finish()
        self.clock.return_value = 200
        self.capture.feed(frame({"error": {"message": "late"}}))
        first["completed"] = False
        self.assertTrue(self.capture.finish_metrics()["completed"])
        self.assertEqual(self.capture.finish_metrics()["duration_ms"], 2000)

    def test_clock_regression_does_not_produce_negative_metrics(self):
        self.clock.return_value = 99
        self.capture.feed(frame({"content": "text", "stop": True}))
        metrics = self.finish(at=99)
        self.assertEqual(metrics["ttft_ms"], 0)
        self.assertEqual(metrics["duration_ms"], 0)


class IncrementalSseTests(unittest.TestCase):
    def test_every_byte_boundary_in_utf8_and_crlf_is_supported(self):
        payload = frame({"choices": [{"delta": {"content": "你好🌍"}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 6, "completion_tokens": 3}}, newline="\r\n") + frame("[DONE]", newline="\r\n")
        for cut in range(len(payload) + 1):
            with self.subTest(cut=cut):
                capture = module.UsageCapture()
                capture.feed(payload[:cut])
                capture.feed(payload[cut:])
                metrics = capture.finish_metrics()
                self.assertTrue(metrics["completed"])
                self.assertFalse(metrics["stream_error"])
                self.assertEqual(metrics["output_tokens"], 3)
        capture = module.UsageCapture()
        for value in payload:
            capture.feed(bytes([value]))
        self.assertTrue(capture.finish_metrics()["completed"])

    def test_multiline_data_comments_and_standalone_cr(self):
        capture = module.UsageCapture()
        capture.feed(b': heartbeat\rid: ignored\revent: message\rdata: {"usage":\r')
        capture.feed(b'data: {"prompt_tokens": 5, "completion_tokens": 2}}\r\rdata: [DONE]\r\r')
        metrics = capture.finish_metrics()
        self.assertEqual(metrics["input_tokens"], 5)
        self.assertTrue(metrics["completed"])
        self.assertFalse(metrics["stream_error"])

    def test_malformed_json_is_flagged_but_later_usage_is_still_captured(self):
        capture = module.UsageCapture()
        capture.feed(b'data: {broken}\n\n')
        capture.feed(frame({"usage": {"prompt_tokens": 3, "completion_tokens": 1}}))
        capture.feed(frame("[DONE]"))
        metrics = capture.finish_metrics()
        self.assertTrue(metrics["stream_error"])
        self.assertFalse(metrics["completed"])
        self.assertEqual(metrics["output_tokens"], 1)

    def test_truncated_json_event_or_utf8_never_marks_success(self):
        for tail in (b'data: {"usage":', b'data: [DONE]', b'data: {"content":"\xe4\xbd',
                     b'data: {"stop":true}\n'):
            with self.subTest(tail=tail):
                capture = module.UsageCapture()
                capture.feed(tail)
                metrics = capture.finish_metrics()
                self.assertTrue(metrics["stream_error"])
                self.assertFalse(metrics["completed"])

    def test_invalid_utf8_and_overdeep_json_do_not_raise(self):
        for payload in (b'data: "\xff"\n\n', b'data: ' + b'[' * 2000 + b']' * 2000 + b'\n\n'):
            capture = module.UsageCapture()
            capture.feed(payload)
            self.assertTrue(capture.finish_metrics()["stream_error"])

    def test_oversized_single_line_is_dropped_with_bounded_storage(self):
        capture = module.UsageCapture()
        capture.feed(b'data: "' + b'x' * (module.MAX_EVENT_BYTES * 3))
        self.assertLessEqual(len(capture._line) + len(capture._data), module.MAX_EVENT_BYTES)
        capture.feed(b'"\n\n')
        capture.feed(frame({"usage": {"input_tokens": 2, "output_tokens": 1}}))
        metrics = capture.finish_metrics()
        self.assertTrue(metrics["stream_error"])
        self.assertTrue(metrics["capture_error"])
        self.assertIsNone(metrics["terminal_status"])
        self.assertEqual(metrics["input_tokens"], 2)

    def test_oversized_multiline_event_is_dropped_with_bounded_storage(self):
        capture = module.UsageCapture()
        for _ in range(300):
            capture.feed(b'data: ' + b'x' * 1024 + b'\n')
            self.assertLessEqual(len(capture._line) + len(capture._data), module.MAX_EVENT_BYTES)
        capture.feed(b'\n')
        capture.feed(frame("[DONE]"))
        self.assertTrue(capture.finish_metrics()["stream_error"])

    def test_error_frame_overrides_finish_and_done(self):
        for error in ({"type": "error", "error": {"message": "secret failure"}},
                      {"type": "server_error", "message": "secret failure"},
                      {"type": "response.failed", "response": {"status": "failed"}}):
            capture = module.UsageCapture()
            capture.feed(frame({"choices": [{"finish_reason": "stop"}]}))
            capture.feed(frame(error, event="error"))
            capture.feed(frame("[DONE]"))
            metrics = capture.finish_metrics()
            self.assertTrue(metrics["stream_error"])
            self.assertFalse(metrics["completed"])
            self.assertNotIn("secret failure", repr(vars(capture)))

    def test_malformed_object_field_types_never_crash_the_observer(self):
        capture = module.UsageCapture()
        for value in (None, True, 123, [], {}, ["bad"]):
            capture.feed(frame({"type": value, "status": value, "usage": value, "timings": value,
                "choices": [{"index": value, "delta": value, "finish_reason": value}],
                "response": {"status": value, "output": value}}))
        metrics = capture.finish_metrics()
        self.assertIsNone(metrics["input_tokens"])
        self.assertIsNone(metrics["output_tokens"])

    def test_responses_incomplete_is_not_success_even_with_done(self):
        capture = module.UsageCapture()
        capture.feed(frame({"type": "response.incomplete", "response": {"status": "incomplete",
            "usage": {"input_tokens": 2, "output_tokens": 1}}}))
        capture.feed(frame("[DONE]"))
        metrics = capture.finish_metrics()
        self.assertFalse(metrics["completed"])
        self.assertFalse(metrics["stream_error"])
        self.assertTrue(metrics["usage_known"])
        self.assertEqual(metrics["terminal_status"], "incomplete")
        self.assertFalse(metrics["capture_error"])

    def test_stream_responses_terminal_events_work_without_status_wrapper(self):
        for event, expected in (("response.incomplete", "incomplete"), ("response.cancelled", "cancelled"),
                                ("response.failed", "error")):
            for named_event in (False, True):
                with self.subTest(event=event, named_event=named_event):
                    capture = module.UsageCapture()
                    capture.feed(frame({} if named_event else {"type": event},
                                       event=event if named_event else None))
                    capture.feed(frame("[DONE]"))
                    metrics = capture.finish_metrics()
                    self.assertEqual(metrics["terminal_status"], expected)
                    self.assertFalse(metrics["completed"])
                    self.assertFalse(metrics["capture_error"])

    def test_malformed_capture_is_distinct_from_explicit_backend_error(self):
        capture = module.UsageCapture()
        capture.feed(b'data: not-json\n\n')
        capture.feed(frame("[DONE]"))
        metrics = capture.finish_metrics()
        self.assertTrue(metrics["capture_error"])
        self.assertTrue(metrics["stream_error"])
        self.assertIsNone(metrics["terminal_status"])
        capture = module.UsageCapture()
        capture.feed(b'data: not-json\n\n')
        capture.feed(frame({"error": {"message": "backend failure"}}))
        metrics = capture.finish_metrics()
        self.assertTrue(metrics["capture_error"])
        self.assertEqual(metrics["terminal_status"], "error")

    def test_no_generated_content_is_retained_after_event_or_finish(self):
        capture = module.UsageCapture()
        secret = "private generated text and private tool argument"
        capture.feed(frame({"content": secret, "stop": True}))
        self.assertNotIn(secret, repr(vars(capture)))
        capture.consume_json({"choices": [{"message": {"tool_calls": [
            {"function": {"arguments": secret}},
        ]}}]})
        self.assertNotIn(secret, repr(vars(capture)))
        capture.feed(b'data: {"content":"unfinished private fragment')
        capture.finish_metrics()
        self.assertEqual(capture._line, b"")
        self.assertEqual(capture._data, b"")


if __name__ == "__main__":
    unittest.main()
