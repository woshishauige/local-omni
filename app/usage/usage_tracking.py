"""Fail-open request accounting for the local gateway; never persists message text."""
import asyncio
import json
import logging
import time
import uuid

import anyio
from starlette.requests import ClientDisconnect

from app.usage.usage_capture import UsageCapture

LOGGER = logging.getLogger("qwen.usage")
GENERATION_PATHS = {
    "/completion", "/completions", "/v1/completions", "/infill",
    "/chat/completions", "/v1/chat/completions", "/responses", "/v1/responses",
}
OPENAI_STREAM_PATHS = {"/completions", "/v1/completions", "/chat/completions", "/v1/chat/completions"}
MAX_RESPONSE_JSON = 8 * 1024 * 1024
MAX_REQUEST_INSPECTION = 8 * 1024 * 1024


async def database_call(store, method, *args):
    """Accounting failures must never fail a model request or leak its payload."""
    try:
        return await asyncio.to_thread(getattr(store, method), *args)
    except Exception:
        LOGGER.exception("Usage database operation failed: %s", method)
        return None


class UsageMiddleware:
    def __init__(self, app, get_store, get_mode, get_model=None, observe=None):
        self.app = app
        self.get_store = get_store
        self.get_mode = get_mode
        self.get_model = get_model
        # Called once per generation request with what was asked and what came back, for the
        # shared conversation history. Optional and separate from accounting: the usage table
        # must keep working even when nothing is listening.
        self.observe = observe

    def _model_name(self, requested):
        """The name to store for this request.

        A caller that names a model gets that name recorded — it is what the client asked
        for. Everything else (the bundled web UI posts no model at all) is recorded as the
        model the gateway is *actually* running, resolved live from the selected profile.
        The old hardcoded fallback made every such row read "qwen3.8-27b-local" even while
        the 9B was loaded, so the usage table could not answer "which model served this".
        """
        if isinstance(requested, str) and requested.strip():
            return requested[:200]
        if self.get_model is not None:
            try:
                resolved = self.get_model()
            except Exception:
                LOGGER.exception("Could not resolve the active model for usage accounting")
                resolved = None
            if isinstance(resolved, str) and resolved.strip():
                return resolved[:200]
        return "unknown-local"

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "").rstrip("/")
        store = self.get_store()
        if (scope["type"] != "http" or scope.get("method") != "POST"
                or path not in GENERATION_PATHS or store is None):
            await self.app(scope, receive, send)
            return
        started_at = time.time()
        started_monotonic = time.monotonic()
        messages = []
        raw_body = bytearray()
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.disconnect":
                # No submitted request exists until its body has arrived.
                async def disconnected():
                    return {"type": "http.disconnect"}
                await self.app(scope, disconnected, send)
                return
            chunk = message.get("body", b"")
            if len(raw_body) + len(chunk) > MAX_REQUEST_INSPECTION:
                # Large image requests are forwarded without an additional full
                # JSON copy. Response usage is still observed authoritatively.
                raw_body = None
                break
            raw_body.extend(chunk)
            if not message.get("more_body", False):
                break
        payload = {}
        parsed = None
        options = None
        headers = dict(scope.get("headers", []))
        content_type = headers.get(b"content-type", b"").decode("latin-1").partition(";")[0].strip().lower()
        is_json = content_type == "application/json" or content_type.endswith("+json")
        if is_json and raw_body is not None:
            try:
                parsed = json.loads(raw_body)
                if isinstance(parsed, dict):
                    payload = parsed
            except (ValueError, UnicodeDecodeError, RecursionError):
                pass
        stream = payload.get("stream") is True
        if stream and is_json and path in OPENAI_STREAM_PATHS:
            options = payload.get("stream_options")
            if options is None or isinstance(options, dict):
                payload["stream_options"] = {**(options or {}), "include_usage": True}
                raw_body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
                scope = dict(scope)
                scope["headers"] = [(key, value) for key, value in scope.get("headers", []) if key.lower() != b"content-length"]
                scope["headers"].append((b"content-length", str(len(raw_body)).encode("ascii")))
                messages = [{"type": "http.request", "body": raw_body, "more_body": False}]
        model = payload.get("model")
        record = {
            "id": uuid.uuid4().hex,
            "started_at": started_at,
            "model": self._model_name(model),
            "mode": self.get_mode(),
            "source": "Web" if b"mozilla/" in headers.get(b"user-agent", b"").lower() else "API",
            "stream": stream,
            "agent": isinstance(payload.get("tools"), list) and bool(payload["tools"]),
            "endpoint": path,
        }
        # Retain only the ASGI body for forwarding; recording contains no messages.
        del raw_body, parsed, options, chunk, message
        begin_task = asyncio.create_task(database_call(store, "begin", record))
        try:
            with anyio.CancelScope(shield=True):
                request_id = await asyncio.shield(begin_task)
        except asyncio.CancelledError:
            # to_thread can finish its INSERT after cancellation. Wait for that
            # insertion and close it, rather than leave a phantom running row.
            with anyio.CancelScope(shield=True):
                request_id = await begin_task
                if request_id is not None:
                    await database_call(store, "finish", request_id, {
                        "status": "cancelled", "finished_at": time.time(),
                        "duration_ms": (time.monotonic() - started_monotonic) * 1000,
                        "error_message": "请求在开始生成前取消。",
                    })
            raise
        if request_id is None:
            LOGGER.warning("This request could not be recorded; inference continues")
        capture = UsageCapture(start_monotonic=started_monotonic)
        http_status = 500
        response_type = ""
        response_json = bytearray()
        response_complete = False
        cancelled = False
        failed = False
        capture_failed = False
        metrics = {}

        async def replay():
            if messages:
                return messages.pop(0)
            return await receive()

        async def observe(message):
            nonlocal http_status, response_type, response_complete, capture_failed
            if message["type"] == "http.response.start":
                http_status = message["status"]
                response_type = dict(message.get("headers", [])).get(b"content-type", b"").decode("latin-1").lower()
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                try:
                    if "text/event-stream" in response_type and not capture_failed:
                        capture.feed(chunk)
                    elif "json" in response_type and not capture_failed:
                        if len(response_json) + len(chunk) <= MAX_RESPONSE_JSON:
                            response_json.extend(chunk)
                        else:
                            response_json.clear()
                            capture_failed = True
                except Exception:
                    capture_failed = True
                    LOGGER.exception("Usage response parsing failed; response forwarding continues")
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                response_complete = True

        try:
            await self.app(scope, replay, observe)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except (BrokenPipeError, ConnectionResetError, ClientDisconnect):
            cancelled = True
            raise
        except BaseException:
            failed = True
            raise
        finally:
            if request_id is not None:
                try:
                    if response_json and not capture_failed:
                        capture.consume_json(json.loads(response_json))
                    metrics = capture.finish_metrics()
                except Exception:
                    LOGGER.exception("Usage summary parsing failed")
                    capture_failed = True
                    metrics = {}
                metrics["duration_ms"] = max(0, (time.monotonic() - started_monotonic) * 1000)
                metrics["finished_at"] = time.time()
                metrics["http_status"] = http_status
                if "text/event-stream" in response_type:
                    metrics["stream"] = True
                terminal_status = metrics.get("terminal_status")
                if cancelled or terminal_status == "cancelled" or (not response_complete and not failed):
                    status = "cancelled"
                    detail = "客户端中断连接；未返回的 Token 用量记为未知。"
                elif http_status >= 400 or failed or terminal_status == "error" or (metrics.get("stream_error") and not metrics.get("capture_error")):
                    status = "error"
                    detail = f"请求失败（HTTP {http_status}）" if http_status >= 400 else "生成流中断或模型报告错误。"
                elif terminal_status == "incomplete" or capture_failed or metrics.get("capture_error") or ("text/event-stream" in response_type and not metrics.get("completed")):
                    status = "incomplete"
                    detail = "响应或用量统计不完整；未返回的 Token 用量记为未知。"
                else:
                    status = "success"
                    detail = None
                metrics["status"] = status
                metrics["error_message"] = detail
                with anyio.CancelScope(shield=True):
                    await database_call(store, "finish", request_id, metrics)
            if self.observe is not None:
                # Never allowed to affect the request: this is a transcript, not accounting.
                #
                # The mode and the conversation key are the ones **this request was served
                # under**, taken from the scope where the gateway resolved them at delivery.
                # Reading ``self.get_mode()`` and the raw header here instead reads the mode at
                # *write* time, which is one response late: measured live, a 纯文本 turn whose
                # write landed next to a switch to 识图 was filed into the *vision* line, and the
                # prompt filter then counted the typed question as the vision line's own and let
                # its answer — the passphrase — through. The header fallback keeps any request
                # that never went through the gateway's resolver behaving as before.
                try:
                    await self.observe({
                        "path": path, "stream": stream,
                        "mode": scope.get("qwen.mode") or self.get_mode(),
                        "payload": payload, "status": http_status,
                        "conversation_id": (scope.get("qwen.conversation")
                                            or headers.get(b"x-conversation-id", b"").decode("latin-1").strip()),
                        "prompt_tokens": metrics.get("prompt_tokens") if request_id is not None else None,
                        "reply": self.reply_text(response_json),
                    })
                except Exception:
                    LOGGER.exception("Conversation history could not be updated")

    @staticmethod
    def reply_text(response_json):
        """The assistant's text from a non-streaming response body, or None."""
        if not response_json:
            return None
        try:
            body = json.loads(response_json)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return None
        if not isinstance(body, dict):
            return None
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return None
        first = choices[0]
        message = first.get("message")
        if isinstance(message, dict):
            content = message.get("content") or message.get("reasoning_content")
            if isinstance(content, str):
                return content
        text = first.get("text")
        return text if isinstance(text, str) else None
