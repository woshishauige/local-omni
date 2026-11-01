"""记账必须用**这个请求被服务时**的模式与键，而不是写完响应时的模式。

实测到的真问题：中间件在响应发完之后才写历史，那一刻 `manager.mode` 可能已经是切过去的
新模式了，于是纯文本那一轮被记进**识图**那条线；提示词过滤器随后把这条打字的问题当成
"识图线自己问过的"，它的回答（里面有机主只在文字线给过的暗号）就一起进了识图的提示词——
正是"切换模式数据不能互传"要挡的那件事。

现在网关在**收到请求时**就把模式与作用域键写进 ASGI scope，中间件只读它。

Run: python -X utf8 -m pytest test_history_attribution.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from usage_tracking import UsageMiddleware


class FakeStore:
    """The accounting half is not what this test is about: begin() answers "no row"."""

    def begin(self, record):
        return None

    def finish(self, request_id, metrics):
        return None


def reply_body():
    return json.dumps({
        "choices": [{"message": {"role": "assistant", "content": "收到了"}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3},
    }).encode()


class Scenario:
    """One generation request through the middleware, with a scope the gateway would have set."""

    def __init__(self, stash=None, live_mode="text", header="chat-1"):
        self.stash = stash or {}
        self.live_mode = live_mode
        self.header = header
        self.seen = []

    async def app(self, scope, receive, send):
        # Standing in for the gateway: it resolves the request-time mode/key onto the scope
        # before doing any work (mode_server.proxy), and later answers with one completion.
        scope.update(self.stash)
        await receive()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": reply_body()})

    async def observe(self, info):
        self.seen.append(info)

    def run(self):
        middleware = UsageMiddleware(self.app, get_store=lambda: FakeStore(),
                                     get_mode=lambda: self.live_mode, observe=self.observe)
        scope = {
            "type": "http", "method": "POST", "path": "/v1/chat/completions",
            "headers": [(b"content-type", b"application/json"),
                        (b"x-conversation-id", self.header.encode("ascii"))],
            "query_string": b"", "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", 8081),
            "scheme": "http", "root_path": "", "http_version": "1.1",
        }
        body = json.dumps({"model": "local", "stream": False,
                           "messages": [{"role": "user", "content": "你好"}]}).encode()
        inbox = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            return inbox.pop(0) if inbox else {"type": "http.disconnect"}

        async def send(message):
            return None

        asyncio.run(middleware(scope, receive, send))
        return self.seen[-1]


class AttributionTests(unittest.TestCase):
    def test_the_request_time_mode_wins_over_the_mode_at_write_time(self):
        """响应发完才写历史；那一刻的模式可能已经切走了。"""
        seen = Scenario(stash={"qwen.mode": "vision", "qwen.conversation": "vision·chat-1"},
                        live_mode="text").run()
        self.assertEqual(seen["mode"], "vision",
                         "记账读了写完响应时的模式，切模式时会把上一轮记进新那条线")
        self.assertEqual(seen["conversation_id"], "vision·chat-1")

    def test_a_guest_turn_is_filed_under_the_callers_own_namespace(self):
        """网关解出的键带访客命名空间，中间件必须原样用它，而不是重新看请求头。"""
        key = "text·guest-token-aaaaaaaaaaaaaaaaaa·chat-1"
        seen = Scenario(stash={"qwen.mode": "text", "qwen.conversation": key}).run()
        self.assertEqual(seen["conversation_id"], key,
                         "访客的回合会被记进机主的命名空间")

    def test_without_the_stash_the_old_behaviour_is_kept(self):
        """没走网关解算器的请求（例如测试里的直连）照旧读请求头与当前模式。"""
        seen = Scenario(live_mode="vision", header="plain-9").run()
        self.assertEqual(seen["mode"], "vision")
        self.assertEqual(seen["conversation_id"], "plain-9")

    def test_the_scope_stash_is_what_the_gateway_sets(self):
        """网关那一侧确实把两样东西放进了 scope（改名/删掉会在这里挂掉）。"""
        import mode_server as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        self.assertIn('request.scope["qwen.mode"] = request_mode', source)
        self.assertIn('request.scope["qwen.conversation"] = conversation_id', source)


if __name__ == "__main__":
    unittest.main()
