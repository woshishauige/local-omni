"""Bounded, content-free usage accounting for llama.cpp generation responses.

Only reported token counters are used. ``tokens_cached`` in a native completion
is the resulting slot occupancy, not a cache hit or cache write count; it is
deliberately ignored. Native ``timings.prompt_n`` counts evaluated prompt tokens,
so it needs ``cache_n`` before it can supply a total input count.
"""

import json
import math
import re
import time


MAX_EVENT_BYTES = 256 * 1024
MAX_OUTPUT_ITEMS = 256
_LINE_END = re.compile(rb"\r\n|\r|\n")
_OUTPUT_EVENTS = {
    "response.output_text.delta", "response.output_text.done",
    "response.reasoning_text.delta", "response.reasoning_text.done",
    "response.reasoning_summary_text.delta", "response.reasoning_summary_text.done",
    "response.function_call_arguments.delta", "response.function_call_arguments.done",
    "response.refusal.delta", "response.refusal.done",
}


def _counter(value):
    # SQLite stores signed 64-bit integers. Booleans, negatives, and estimates are
    # not valid token counts even though Python accepts bool as a subclass of int.
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _number(value):
    if type(value) not in (int, float):
        return None
    try:
        return float(value) if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def _text_present(value):
    if isinstance(value, str):
        return bool(value)
    if isinstance(value, list):
        for item in value[:MAX_OUTPUT_ITEMS]:
            if isinstance(item, str) and item:
                return True
            if isinstance(item, dict) and any(
                isinstance(item.get(key), str) and item[key]
                for key in ("text", "refusal", "reasoning", "reasoning_content")
            ):
                return True
    return False


def _output_present(value):
    if not isinstance(value, dict):
        return False
    if any(_text_present(value.get(key)) for key in
           ("content", "text", "reasoning_content", "reasoning", "refusal", "arguments")):
        return True
    function = value.get("function_call")
    if isinstance(function, dict) and _text_present(function.get("arguments")):
        return True
    calls = value.get("tool_calls")
    if isinstance(calls, list):
        for call in calls[:MAX_OUTPUT_ITEMS]:
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict) and _text_present(function.get("arguments")):
                return True
    return False


class UsageCapture:
    """Observe one request without keeping its prompt, output text, or tool args.

    ``feed`` accepts SSE bytes. ``consume_json`` accepts an already decoded,
    bounded non-streaming JSON body. Call ``finish_metrics`` once delivery ends;
    it is idempotent. A caller may set ``stream_error = True`` for a transport
    failure that occurs outside this parser. Non-streaming TTFT remains unknown.
    """

    def __init__(self, start_monotonic=None):
        self.start_monotonic = time.monotonic() if start_monotonic is None else start_monotonic
        if type(self.start_monotonic) not in (int, float) or not math.isfinite(self.start_monotonic):
            raise ValueError("start_monotonic must be a finite monotonic timestamp")
        self.completed = False
        self.stream_error = False
        self.capture_error = False
        self.terminal_status = None
        self.had_output = False
        self._terminal_failure = False
        self._ttft_ms = None
        self._counts = {key: None for key in
                        ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")}
        self._priorities = dict.fromkeys(self._counts, -1)
        self._server_speed = None
        self._predicted_ms = None
        self._predicted_n = None
        self._choices_seen = set()
        self._choices_finished = set()
        self._line = bytearray()
        self._line_nonempty = False
        self._data = bytearray()
        self._has_data = False
        self._event_name = ""
        self._event_bytes = 0
        self._dropping = False
        self._skip_lf = False
        self._final_metrics = None

    def _capture_failed(self):
        # Accounting may be incomplete even when the underlying generation is
        # healthy (for example, a valid but oversized output-text SSE event).
        self.capture_error = True
        self.stream_error = True

    def _mark_terminal(self, status):
        priorities = {None: 0, "incomplete": 1, "cancelled": 2, "error": 3}
        if priorities[status] >= priorities[self.terminal_status]:
            self.terminal_status = status
        self._terminal_failure = True
        self.completed = False
        if status == "error":
            self.stream_error = True

    def _set_count(self, name, value, priority):
        value = _counter(value)
        if value is not None and priority >= self._priorities[name]:
            # Usage events are cumulative snapshots, not increments. In
            # particular, repeated final chunks must never be added together.
            self._counts[name] = value
            self._priorities[name] = priority

    def _observe_output(self, present, stream_event):
        if present:
            self.had_output = True
            if stream_event and self._ttft_ms is None:
                self._ttft_ms = max(0, (time.monotonic() - self.start_monotonic) * 1000)

    def _read_usage(self, usage):
        if not isinstance(usage, dict):
            return
        for name, aliases in (
            ("input_tokens", ("input_tokens", "prompt_tokens")),
            ("output_tokens", ("output_tokens", "completion_tokens")),
            ("cache_read_tokens", ("cache_read_input_tokens", "cache_read_tokens")),
            ("cache_write_tokens", ("cache_creation_input_tokens", "cache_write_tokens")),
        ):
            for alias in aliases:
                if _counter(usage.get(alias)) is not None:
                    self._set_count(name, usage[alias], 30)
                    break
        for name in ("input_tokens_details", "prompt_tokens_details"):
            details = usage.get(name)
            if isinstance(details, dict):
                self._set_count("cache_read_tokens", details.get("cached_tokens"), 30)
                self._set_count("cache_write_tokens", details.get("cache_write_tokens"), 30)

    def _read_timings(self, timings):
        if not isinstance(timings, dict):
            return
        cache_n = _counter(timings.get("cache_n"))
        prompt_n = _counter(timings.get("prompt_n"))
        if cache_n is not None and prompt_n is not None:
            self._set_count("input_tokens", cache_n + prompt_n, 10)
        self._set_count("cache_read_tokens", cache_n, 20)
        self._set_count("output_tokens", timings.get("predicted_n"), 10)
        speed = _number(timings.get("predicted_per_second"))
        if speed is not None:
            self._server_speed = speed
        predicted_ms = _number(timings.get("predicted_ms"))
        predicted_n = _counter(timings.get("predicted_n"))
        if predicted_ms is not None:
            self._predicted_ms = predicted_ms
        if predicted_n is not None:
            self._predicted_n = predicted_n

    def consume_json(self, obj, stream_event=False):
        if self._final_metrics is not None or not isinstance(obj, dict):
            return
        self._consume_object(obj, stream_event, "")

    def _consume_object(self, obj, stream_event, event_name):
        kind = obj.get("type")
        kind = kind if isinstance(kind, str) else event_name
        if obj.get("error") is not None or event_name == "error" or kind in {"error", "response.failed"}:
            self._mark_terminal("error")
        self._read_timings(obj.get("timings"))
        self._set_count("input_tokens", obj.get("tokens_evaluated"), 20)
        self._set_count("output_tokens", obj.get("tokens_predicted"), 20)
        self._read_usage(obj.get("usage"))
        self._observe_output(_output_present(obj), stream_event)

        choices = obj.get("choices")
        if isinstance(choices, list):
            for position, choice in enumerate(choices[:MAX_OUTPUT_ITEMS]):
                if not isinstance(choice, dict):
                    continue
                index = choice.get("index", position)
                if type(index) is int and 0 <= index < MAX_OUTPUT_ITEMS:
                    self._choices_seen.add(index)
                    reason = choice.get("finish_reason")
                    if isinstance(reason, str) and reason:
                        self._choices_finished.add(index)
                self._observe_output(
                    _text_present(choice.get("text")) or _output_present(choice.get("delta"))
                    or _output_present(choice.get("message")), stream_event,
                )
            if self._choices_seen:
                self.completed = self._choices_seen <= self._choices_finished

        if kind in _OUTPUT_EVENTS:
            self._observe_output(_text_present(obj.get("delta")), stream_event)
        if kind in {"response.output_item.added", "response.output_item.done"}:
            self._observe_output(_output_present(obj.get("item")), stream_event)
        if kind in {"response.content_part.added", "response.content_part.done"}:
            self._observe_output(_output_present(obj.get("part")), stream_event)

        response = obj.get("response")
        if isinstance(response, dict):
            # Inspect one known wrapper only, never recursively walk arbitrary
            # generated JSON, metadata, or echoed request instructions.
            self._read_timings(response.get("timings"))
            self._read_usage(response.get("usage"))
            self._read_response_status(response.get("status"))
            self._read_response_output(response.get("output"), stream_event)
            if response.get("error") is not None:
                self._mark_terminal("error")
        self._read_response_output(obj.get("output"), stream_event)
        self._read_response_status(obj.get("status"))
        if obj.get("stop") is True or kind == "response.completed":
            self.completed = True
        if kind in {"response.incomplete", "response.cancelled"}:
            self._mark_terminal(kind.removeprefix("response."))
        if self._terminal_failure or self.stream_error:
            self.completed = False

    def _read_response_status(self, status):
        if status == "completed":
            self.completed = True
        elif status in ("failed", "cancelled", "incomplete"):
            self._mark_terminal("error" if status == "failed" else status)

    def _read_response_output(self, output, stream_event):
        if isinstance(output, list):
            self._observe_output(any(_output_present(item) for item in output[:MAX_OUTPUT_ITEMS]), stream_event)

    def feed(self, data):
        if self._final_metrics is not None or not data:
            return
        if not isinstance(data, (bytes, bytearray, memoryview)):
            self._capture_failed()
            return
        if isinstance(data, memoryview):
            data = data.tobytes()
        offset = 0
        if self._skip_lf:
            if data[0] == 10:
                offset = 1
            self._skip_lf = False
        for match in _LINE_END.finditer(data, offset):
            self._append_line(data[offset:match.start()])
            self._end_line(len(match.group()))
            offset = match.end()
            if match.group() == b"\r" and offset == len(data):
                self._skip_lf = True
        self._append_line(data[offset:])

    def _append_line(self, value):
        self._line_nonempty = self._line_nonempty or bool(value)
        if self._dropping:
            return
        self._event_bytes += len(value)
        if self._event_bytes > MAX_EVENT_BYTES:
            self._capture_failed()
            self._dropping = True
            self._line.clear()
            self._data.clear()
            self._event_name = ""
            return
        self._line.extend(value)

    def _end_line(self, delimiter_size):
        if not self._line_nonempty:
            self._dispatch_event()
        elif not self._dropping:
            self._event_bytes += delimiter_size
            if self._event_bytes > MAX_EVENT_BYTES:
                self._capture_failed()
                self._dropping = True
                self._line.clear()
                self._data.clear()
                self._event_name = ""
                self._line_nonempty = False
                return
            line = bytes(self._line)
            field, _, value = line.partition(b":")
            if value.startswith(b" "):
                value = value[1:]
            if field == b"data":
                if self._has_data:
                    self._data.extend(b"\n")
                self._data.extend(value)
                self._has_data = True
            elif field == b"event":
                self._event_name = value[:128].decode("utf-8", errors="replace")
        self._line.clear()
        self._line_nonempty = False

    def _dispatch_event(self):
        if self._has_data and not self._dropping:
            try:
                raw = bytes(self._data).decode("utf-8")
                if raw.strip() == "[DONE]":
                    self.completed = not (self.stream_error or self._terminal_failure)
                else:
                    obj = json.loads(raw)
                    if isinstance(obj, dict):
                        self._consume_object(obj, True, self._event_name)
                    else:
                        self._capture_failed()
            except (ValueError, UnicodeError, RecursionError):
                self._capture_failed()
        self._data.clear()
        self._has_data = False
        self._event_name = ""
        self._event_bytes = 0
        self._dropping = False

    def finish_metrics(self):
        if self._final_metrics is not None:
            return dict(self._final_metrics)
        if self._has_data or (self._line_nonempty and not self._line.startswith(b":")):
            # EOF is not an SSE event boundary. Do not accept a truncated JSON
            # object, UTF-8 sequence, or unterminated final usage frame.
            self._capture_failed()
        self._line.clear()
        self._data.clear()
        self._event_name = ""
        duration_ms = max(0, (time.monotonic() - self.start_monotonic) * 1000)
        speed = self._server_speed
        if speed is None and self._predicted_ms and self._predicted_n is not None and self._predicted_n > 1:
            # llama.cpp's decode timing excludes the first output token, which
            # comes from prompt evaluation (predicted_per_second uses n - 1).
            speed = (self._predicted_n - 1) * 1000 / self._predicted_ms
        if speed is None and self._counts["output_tokens"] is not None and duration_ms > 0:
            # Fallback is observed whole-request throughput, including prefill.
            speed = self._counts["output_tokens"] * 1000 / duration_ms
        self._final_metrics = self._counts | {
            "ttft_ms": round(self._ttft_ms, 3) if self._ttft_ms is not None else None,
            "duration_ms": round(duration_ms, 3),
            "tokens_per_second": round(speed, 3) if speed is not None else None,
            "usage_known": self._counts["input_tokens"] is not None and self._counts["output_tokens"] is not None,
            "completed": self.completed and not (self.stream_error or self._terminal_failure),
            "stream_error": self.stream_error,
            "capture_error": self.capture_error,
            "terminal_status": self.terminal_status,
            "had_output": self.had_output,
        }
        return dict(self._final_metrics)
