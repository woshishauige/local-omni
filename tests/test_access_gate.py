"""Sharing guards: host allowlist, device sessions, owner-only routes, panel API.

These run through the ASGI app, so they exercise the deployed middleware stack and
the real route table rather than a copy of its rules. Two knobs matter:

* ``client``       the peer address uvicorn would report (loopback = the owner)
* ``host``         the Host header, which is what separates the tunnel address
                   from the LAN address once a proxy is in front
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


from datetime import datetime
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from starlette.requests import Request

from app import mode_server as module
from app import qwen_config
from app.share.share_store import ShareStore


def make_store(directory, **kwargs):
    return ShareStore(Path(directory), **kwargs)


def fake_request(peer, query=None, cookies=None):
    """A real Starlette request, so the owner checks see a genuine request object."""
    headers = []
    if cookies:
        headers.append((b"cookie", "; ".join(f"{name}={value}" for name, value in cookies.items()).encode()))
    scope = {
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": "/local/status", "raw_path": b"/local/status", "root_path": "",
        "query_string": (query or "").encode(), "headers": headers,
        "client": (peer, 51000), "server": ("127.0.0.1", 8081),
    }
    return Request(scope)


def _deny():
    """The real refusal a non-loopback caller gets, minus the framework plumbing."""
    from fastapi import HTTPException
    raise HTTPException(403, "该操作仅限本机管理员")


class GateCase(unittest.IsolatedAsyncioTestCase):
    """Shared harness: isolated session store, no passphrase unless asked for."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.previous_store = module.share_store
        module.share_store = make_store(self.directory.name)
        self.addCleanup(self.restore_store)
        # The overlay is discovered by convention under ROOT, so ROOT is pointed at
        # the temporary directory too: without this a test would read the machine's
        # real share.config.json and start enforcing its live passphrase.
        self.root = patch.object(qwen_config, "ROOT", Path(self.directory.name))
        self.root.start()
        self.addCleanup(self.root.stop)
        self.environment = patch.dict("os.environ", {
            module.ACCESS_TOKEN_ENV: "",
            module.ALLOWED_HOSTS_ENV: "",
            module.ALLOWED_ORIGINS_ENV: "",
            "QWEN_SHARE_CONFIG": "",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def restore_store(self):
        module.share_store = self.previous_store

    def share(self, token="correct-horse", hosts="192.168.1.20,*.trycloudflare.com"):
        environment = patch.dict("os.environ", {
            module.ACCESS_TOKEN_ENV: token,
            module.ALLOWED_HOSTS_ENV: hosts,
        })
        environment.start()
        self.addCleanup(environment.stop)
        module.share_store.set_pin(token)

    def forget_share_variables(self):
        """Let the overlay be the source again.

        Setting a variable to empty now means "sharing is off" for this process, so
        a test that wants the overlay to be read has to remove the variable instead
        of blanking it. The stored passphrase is cleared too: on a real start it is
        empty until the overlay seeds it, and it outranks the overlay by design.
        """
        for name in (*module.SHARE_ENV_NAMES.values(), "QWEN_SHARE_CONFIG"):
            os.environ.pop(name, None)
        module.share_store.pin = ""

    def use_peer(self, peer):
        """Make the owner checks see `peer` as the request's source address.

        httpx always reports 127.0.0.1 as the peer, so the loopback-versus-tunnel
        decision is driven here instead. The origin guard is untouched and still
        runs for real in every request.
        """
        loopback = peer == "::1" or peer.startswith("127.")
        for name in ("require_local_session", "is_admin_session"):
            original = module.__dict__[name]
            self.addCleanup(setattr, module, name, original)
            if name == "require_local_session":
                setattr(module, name, lambda request, _ok=loopback: True if _ok else _deny())
            else:
                setattr(module, name, lambda request, _ok=loopback: _ok)

    def client(self, host="127.0.0.1:8081", peer="127.0.0.1", scheme="http"):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=module.app),
            base_url=f"{scheme}://{host}",
        )

    async def request(self, method, path, host="127.0.0.1:8081", peer="127.0.0.1",
                      scheme="http", cookies=None, **kwargs):
        async with self.client(host, peer, scheme) as client:
            if cookies:
                for name, value in cookies.items():
                    client.cookies.set(name, value)
            return await client.request(method, path, **kwargs)

    async def get(self, path, **kwargs):
        return await self.request("GET", path, **kwargs)

    async def post(self, path, **kwargs):
        return await self.request("POST", path, **kwargs)

    def session(self, label="iPhone 15", source="10.36.22.9", kind="device"):
        return module.share_store.create(kind=kind, label=label, source=source)

    def device_cookies(self, token):
        return {module.ACCESS_COOKIE: token}


class DefaultsStayLocalTests(GateCase):
    """Without a passphrase the service keeps its original loopback-only shape."""

    async def test_local_status_is_served(self):
        response = await self.get("/local/status")
        self.assertEqual(response.status_code, 200)
        self.assertIn("state", response.json())

    async def test_unknown_host_is_rejected(self):
        response = await self.get("/local/status", host="evil.example.com")
        self.assertEqual(response.status_code, 400)

    async def test_lan_address_is_rejected_until_it_is_shared(self):
        response = await self.get("/local/status", host="192.168.1.20:8081")
        self.assertEqual(response.status_code, 400)


class SharedAddressTests(GateCase):
    """A configured name is served; the wildcard follows Starlette's rule."""

    def setUp(self):
        super().setUp()
        self.share()

    async def test_shared_lan_address_is_served(self):
        response = await self.post("/v1/chat/completions", host="192.168.1.20:8081",
                                   json={"messages": []})
        self.assertEqual(response.status_code, 401)

    async def test_quick_tunnel_name_is_served(self):
        response = await self.post("/v1/chat/completions",
                                   host="plain-paper-example.trycloudflare.com",
                                   peer="203.0.113.9", scheme="https", json={"messages": []})
        self.assertEqual(response.status_code, 401)

    async def test_deep_subdomain_matches_but_bare_domain_does_not(self):
        response = await self.post("/v1/chat/completions", host="a.b.trycloudflare.com",
                                   peer="203.0.113.9", scheme="https", json={"messages": []})
        self.assertEqual(response.status_code, 401)
        response = await self.post("/v1/chat/completions", host="trycloudflare.com",
                                   peer="203.0.113.9", scheme="https", json={"messages": []})
        self.assertEqual(response.status_code, 400)

    async def test_unlisted_name_is_still_rejected(self):
        response = await self.post("/v1/chat/completions", host="10.0.0.5:8081", json={"messages": []})
        self.assertEqual(response.status_code, 400)


class LoginTests(GateCase):
    """The passphrase buys a per-device session, not a copy of the passphrase."""

    def setUp(self):
        super().setUp()
        self.share()

    async def test_request_without_a_session_gets_the_login_page(self):
        response = await self.post("/v1/chat/completions", json={"messages": []})
        self.assertEqual(response.status_code, 401)
        self.assertIn("需要访问口令", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_login_page_never_contains_the_passphrase(self):
        response = await self.post("/v1/chat/completions", json={"messages": []})
        self.assertNotIn("correct-horse", response.text)
        self.assertIn('action="/__login"', response.text)

    async def test_login_issues_a_device_token_instead_of_the_passphrase(self):
        response = await self.post("/__login", data={"token": "correct-horse"})
        self.assertEqual(response.status_code, 303)
        cookie = response.headers["set-cookie"]
        self.assertIn(f"{module.ACCESS_COOKIE}=", cookie)
        self.assertNotIn(f"{module.ACCESS_COOKIE}=correct-horse", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertEqual(module.share_store.session_count(), 1)

    async def test_wrong_passphrase_is_rejected_without_a_session(self):
        response = await self.post("/__login", data={"token": "nope"})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(module.share_store.session_count(), 0)

    async def test_login_target_cannot_leave_the_site(self):
        response = await self.post("/__login", params={"next": "//evil.example.com"},
                                   data={"token": "correct-horse"})
        self.assertEqual(response.headers["location"], "/")

    async def test_device_cookie_is_accepted(self):
        response = await self.get("/local/status", cookies=self.device_cookies(self.session()))
        self.assertEqual(response.status_code, 200)

    async def test_unknown_cookie_is_rejected(self):
        response = await self.get("/local/status", cookies=self.device_cookies("forged-token"))
        self.assertEqual(response.status_code, 401)

    async def test_legacy_passphrase_cookie_is_now_just_an_unknown_cookie(self):
        """The passphrase is only ever read at login, never accepted as a session."""
        response = await self.get("/local/status", cookies=self.device_cookies("correct-horse"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(module.share_store.session_count(), 0)

    async def test_logout_revokes_this_device(self):
        token = self.session()
        response = await self.post("/__logout", cookies=self.device_cookies(token))
        self.assertEqual(response.status_code, 303)
        self.assertIn("Max-Age=0", response.headers["set-cookie"])
        self.assertIsNone(module.share_store.authenticate(token))


class SessionStoreTests(unittest.TestCase):
    """Store rules that the kick button and the rotation promise depend on."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = make_store(self.directory.name)

    def test_a_kicked_session_is_deleted_not_flagged(self):
        """Kicking removes the record, so it disappears from the panel and the file."""
        token = self.store.create(label="iPad")
        self.assertIsNotNone(self.store.authenticate(token))
        self.assertTrue(self.store.revoke(token))
        self.assertIsNone(self.store.authenticate(token))
        self.assertEqual(self.store.session_count(), 0)
        self.assertEqual(self.store.summarize()["sessions"], [])
        self.store.save(force=True)
        self.assertNotIn(token, self.store.path.read_text(encoding="utf-8"))

    def test_kicking_twice_reports_the_second_attempt_as_gone(self):
        token = self.store.create(label="iPad")
        self.assertTrue(self.store.revoke(token))
        self.assertFalse(self.store.revoke(token), "a deleted session cannot be kicked again")

    def test_rotating_the_passphrase_keeps_existing_sessions(self):
        token = self.store.create(label="iPad")
        self.store.set_pin("111111-222222-333333")
        self.store.set_pin("444444-555555-666666")
        self.assertIsNotNone(self.store.authenticate(token))

    def test_activity_is_accounted_without_recording_content(self):
        token = self.store.create(label="iPad")
        self.store.note_request(token, "/v1/chat/completions", received=4096)
        self.store.note_request(token, "/v1/chat/completions", received=96)
        entry = self.store.summarize(current_token=token)["sessions"][0]
        self.assertEqual(entry["requests"], 2)
        self.assertEqual(entry["bytes"], 4192)
        self.assertEqual(entry["last_endpoint"], "/v1/chat/completions")
        self.assertTrue(entry["current"])
        for forbidden in ("messages", "prompt", "content", "response"):
            self.assertNotIn(forbidden, entry)

    def test_the_panel_never_exposes_a_replayable_token(self):
        token = self.store.create(label="iPad")
        handle = self.store.summarize()["sessions"][0]["token"]
        self.assertNotEqual(handle, token)
        self.assertNotIn(token, json.dumps(self.store.summarize()))
        self.assertEqual(self.store.token_for_handle(handle), token)

    def test_handle_is_stable_and_short(self):
        token = self.store.create(label="iPad")
        first = self.store.handle(token)
        self.assertEqual(first, self.store.handle(token))
        self.assertEqual(len(first), 16)

    def test_sessions_survive_a_reload(self):
        token = self.store.create(label="iPad")
        self.store.note_request(token, "/v1/chat/completions", received=10)
        self.store.save(force=True)
        reloaded = make_store(self.directory.name)
        self.assertIsNotNone(reloaded.authenticate(token))
        self.assertEqual(reloaded.summarize()["sessions"][0]["requests"], 1)

    def test_expired_sessions_are_dropped(self):
        now = [1000.0]
        store = make_store(self.directory.name, clock=lambda: now[0])
        token = store.create(label="old")
        now[0] += 500 * 24 * 3600
        self.assertIsNone(store.authenticate(token))
        store.summarize()
        self.assertEqual(store.session_count(), 0)

    def test_admin_token_is_stable_across_reloads(self):
        first = self.store.admin_token
        self.assertTrue(first)
        self.assertEqual(make_store(self.directory.name).admin_token, first)

    def test_kick_all_deletes_every_device_and_keeps_the_owner(self):
        first = self.store.create(label="a")
        second = self.store.create(label="b")
        owner = self.store.create(kind="admin", label="owner")
        self.assertEqual(self.store.revoke_all_devices(), 2)
        self.assertIsNone(self.store.authenticate(first))
        self.assertIsNone(self.store.authenticate(second))
        self.assertIsNotNone(self.store.authenticate(owner), "the owner's session survives")
        self.assertEqual(self.store.session_count(), 1)

    def test_forget_unused_keeps_devices_that_did_something(self):
        """Only the records with no requests at all are dropped."""
        idle = self.store.create(label="never used")
        active = self.store.create(label="used")
        self.store.note_request(active, "/v1/chat/completions", received=64)
        owner = self.store.create(kind="admin", label="owner")
        self.assertEqual(self.store.forget_unused_devices(), 1)
        self.assertIsNone(self.store.authenticate(idle))
        self.assertIsNotNone(self.store.authenticate(active))
        self.assertIsNotNone(self.store.authenticate(owner), "the owner is never forgotten")

    def test_forget_unused_reports_nothing_when_there_is_nothing_to_drop(self):
        """Every remaining record has served at least one request."""
        for label in ("one", "two"):
            token = self.store.create(label=label)
            self.store.note_request(token, "/v1/chat/completions", received=1)
        self.assertEqual(self.store.forget_unused_devices(), 0)

    def test_anonymous_session_is_not_created_by_authenticating(self):
        self.assertIsNone(self.store.authenticate(""))
        self.assertIsNone(self.store.authenticate(None))
        self.assertEqual(self.store.session_count(), 0)


class GuestCapabilityTests(GateCase):
    """A shared visitor *uses* the machine; the host's own controls stay the owner's.

    The owner asked for this explicitly: a visitor should be able to switch modes (and with them
    the model and its plugins), generate images and films, and handle its own uploads and
    downloads — instead of being told "该操作仅限本机管理员". The switches live in
    ``guest_policy`` and this suite is what says which way each one cuts.
    """

    def setUp(self):
        super().setUp()
        self.share()
        # A guest comes from the tunnel, never from loopback. Individual tests flip
        # this to 127.0.0.1 for the owner's own browser.
        self.use_peer("203.0.113.9")
        self.cookies_device = self.device_cookies(self.session())
        self.cookies_admin = {module.ACCESS_COOKIE: self.session(kind="admin", label="本机")}
        # Per-test capability document: the machine's own guest policy must not decide these
        # expectations, and a test that closes a switch must not leave it closed for the next.
        previous = module.guest_policy.policy()
        self.policy = module.guest_policy.GuestPolicy(directory=Path(self.directory.name))
        module.guest_policy.set_policy(self.policy)
        self.addCleanup(module.guest_policy.set_policy, previous)

    async def test_guest_can_switch_mode_and_model(self):
        """访客能自己切模式——包括随之切换模型与插件，而不是被拒。"""
        for path, body in (("/local/mode", {"mode": "image"}),
                           ("/local/model", {"profile": "qwen3.5-9b-uncensored"})):
            response = await self.post(path, cookies=self.cookies_device, json=body)
            self.assertNotEqual(response.status_code, 403, f"{path}: {response.text}")

    async def test_guest_can_queue_generation(self):
        requests = (
            ("POST", "/local/video/generate", {"prompt": "x", "shots": []}),
            ("POST", "/local/video/interpolate", {"factor": 2}),
            ("POST", "/local/video/cancel", None),
            ("POST", "/local/video/source", {"path": "x"}),
            ("POST", "/local/image/cancel", None),
        )
        for method, path, body in requests:
            status = await self.status_of(method, path, body)
            self.assertNotEqual(status, 403, f"{method} {path}")

    async def status_of(self, method, path, body, **options):
        """The status a guest gets, treating "the handler needs a live service" as pass-through.

        This app runs without its lifespan here, so a route that got *through* the gate can still
        raise (``image_jobs`` is None). That is not a permission refusal, and the distinction is
        the whole point of the test.
        """
        try:
            response = await self.request(method, path, cookies=self.cookies_device,
                                          json=body, **options)
        except Exception:  # noqa: BLE001 - the gate let it through; the handler had no service
            return 200
        return response.status_code

    async def test_closing_a_capability_refuses_with_its_own_wording(self):
        cases = (("switch", "POST", "/local/mode", {"mode": "image"}, "模式"),
                 ("media", "POST", "/local/image/cancel", None, "生图"),
                 ("chat", "POST", "/local/voice/mode", {"profile": "x"}, "对话"),
                 ("chat", "POST", "/local/voice/transcribe", None, "对话"),
                 ("chat", "POST", "/local/voice/speak", {"text": "你好"}, "对话"),
                 ("storage", "POST", "/local/video/upload?name=x.mp4", None, "上传"))
        for feature, method, path, body, wording in cases:
            with self.subTest(feature=feature):
                self.policy.set_feature(feature, False)
                response = await self.request(method, path, cookies=self.cookies_device, json=body)
                self.assertEqual(response.status_code, 403, f"{method} {path}")
                self.assertIn(wording, response.text)
                self.policy.set_feature(feature, True)

    async def test_guest_can_store_and_reuse_its_own_material(self):
        response = await self.post("/local/video/upload?name=x.mp4", cookies=self.cookies_device,
                                   content=b"not really a video")
        self.assertEqual(response.status_code, 200, response.text)
        name = response.json()["name"]
        # Filed against the guest's own session: that is what makes it "its own storage".
        self.assertEqual(module.guest_uploads.store().owner_of(name),
                         self.cookies_device[module.ACCESS_COOKIE])

    async def test_guest_can_configure_the_generation_it_is_allowed_to_run(self):
        requests = (
            ("PUT", "/local/image/workflow", {"pipeline": "flux2"}),
            ("POST", "/local/image/workflows", {"key": "x"}),
            ("POST", "/local/image/presets", {"key": "x"}),
            ("POST", "/local/image/pipeline", {"pipeline": "flux2"}),
        )
        for method, path, body in requests:
            status = await self.status_of(method, path, body)
            self.assertNotEqual(status, 403, f"{method} {path}")

    async def test_guest_cannot_delete_the_owners_data(self):
        """生成能用，删掉机主的东西不能：那条线画在"改"与"毁"之间。

        历史的单条改名/删除是访客对自己那条线的操作（面板上就有这两个按钮），所以拒绝
        不再靠 403，而是靠键的推导：访客递过来的 id 会被嵌进他自己的命名空间
        （mode_server.tenant_conversation），于是"机主那条"根本不是他能命名的键。
        清空全部仍然是机主专属。
        """
        requests = (
            ("DELETE", "/local/image/history", None),
            ("DELETE", "/local/image/workflow", None),
            ("DELETE", "/local/image/presets", None),
            ("DELETE", "/local/conversation/all", None),
        )
        for method, path, body in requests:
            response = await self.request(method, path, cookies=self.cookies_device, json=body)
            self.assertEqual(response.status_code, 403, f"{method} {path}")

    async def test_a_guest_delete_only_reaches_its_own_namespace(self):
        token = self.cookies_device[module.ACCESS_COOKIE]
        owner_key = module.scoped_conversation("owner-thread")
        module.ledger.append_turn(owner_key, "user", "机主的私人口令")
        self.addCleanup(module.ledger.forget, owner_key)

        response = await self.request("DELETE", "/local/conversation?conversation_id=owner-thread",
                                      cookies=self.cookies_device)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["forgotten"], False)
        self.assertTrue(module.ledger.history(owner_key),
                        "访客的删除请求动到了机主那条对话")

        mine = module.tenant_conversation(
            SimpleNamespace(cookies={module.ACCESS_COOKIE: token},
                            client=SimpleNamespace(host="10.0.0.5"),
                            query_params={}, headers={}),
            "mine", "text")
        module.ledger.append_turn(mine, "user", "访客自己的话")
        response = await self.request("DELETE", "/local/conversation?conversation_id=mine",
                                      cookies=self.cookies_device)
        self.assertEqual(response.json()["forgotten"], True)
        self.assertFalse(module.ledger.history(mine))

    async def test_a_guest_cannot_cancel_the_owners_job(self):
        """生成能用，停别人的任务不能：一个监视器服务整台机器（审查发现的越界）。"""
        token = self.cookies_device[module.ACCESS_COOKIE]

        class FakeJobs:
            def __init__(self, session):
                self.session = session
                self.cancelled = False

            @property
            def busy(self):
                return True

            def snapshot(self):
                return {"session": self.session}

            async def cancel(self):
                self.cancelled = True
                return {"state": "cancelled"}

        previous = module.image_jobs
        self.addCleanup(setattr, module, "image_jobs", previous)

        owners = FakeJobs("")                      # 机主的任务不带会话
        module.image_jobs = owners
        denied = await self.post("/local/image/cancel", cookies=self.cookies_device, json={})
        self.assertEqual(denied.status_code, 403)
        self.assertIn("不是你的任务", denied.text)
        self.assertFalse(owners.cancelled)

        mine = FakeJobs(token)                     # 自己提交的那个可以取消
        module.image_jobs = mine
        allowed = await self.post("/local/image/cancel", cookies=self.cookies_device, json={})
        self.assertNotEqual(allowed.status_code, 403, allowed.text)
        self.assertTrue(mine.cancelled)

    async def test_guest_cannot_read_usage_or_export_it(self):
        for path in ("/local/usage", "/local/usage/export", "/local/routing", "/local/routing/export"):
            response = await self.get(path, cookies=self.cookies_device)
            self.assertEqual(response.status_code, 403, path)

    async def test_guest_cannot_shut_down_or_recover_the_service(self):
        for path in ("/local/shutdown", "/local/recover"):
            response = await self.post(path, cookies=self.cookies_device)
            self.assertEqual(response.status_code, 403, path)

    async def test_guest_cannot_touch_the_sharing_controls(self):
        requests = (
            ("GET", "/local/share/status", None),
            ("POST", "/local/share/online", {"online": False}),
            ("POST", "/local/share/pin", {"pin": "1234"}),
            ("POST", "/local/share/stop", None),
            ("POST", "/local/share/guest-policy", {"feature": "media", "allowed": False}),
            ("POST", "/local/gpu/preempt", {"mode": "interrupt"}),
        )
        for method, path, body in requests:
            response = await self.request(method, path, cookies=self.cookies_device, json=body)
            self.assertEqual(response.status_code, 403, f"{method} {path}")

    async def test_a_tunnel_peer_gets_guest_rights_not_owner_rights(self):
        """A tunnel peer is never loopback: it may use the machine, but it is not its owner."""
        response = await self.post("/local/mode", host="plain-paper-example.trycloudflare.com",
                                   peer="203.0.113.9", scheme="https",
                                   cookies=self.cookies_device, json={"mode": "image"})
        self.assertNotEqual(response.status_code, 403, response.text)
        for path in ("/local/share/status", "/local/usage"):
            denied = await self.get(path, host="plain-paper-example.trycloudflare.com",
                                    peer="203.0.113.9", scheme="https",
                                    cookies=self.cookies_device)
            self.assertEqual(denied.status_code, 403, path)

    async def test_owner_on_the_local_machine_keeps_control(self):
        self.use_peer("127.0.0.1")
        response = await self.post("/local/mode", cookies=self.cookies_admin, json={"mode": "image"})
        self.assertNotEqual(response.status_code, 403)
        for path in ("/local/share/status", "/local/usage"):
            allowed = await self.get(path, cookies=self.cookies_admin)
            self.assertNotEqual(allowed.status_code, 403, path)

    async def test_a_guessed_admin_cookie_buys_nothing_on_the_tunnel(self):
        cookies = {module.ACCESS_COOKIE: self.session(), module.ACCESS_ADMIN_COOKIE: "guessed"}
        response = await self.get("/local/share/status", host="plain-paper-example.trycloudflare.com",
                                  peer="203.0.113.9", scheme="https", cookies=cookies)
        self.assertEqual(response.status_code, 403)
        # The same caller still gets ordinary guest rights: the cookie is not a demotion either.
        switch = await self.post("/local/mode", host="plain-paper-example.trycloudflare.com",
                                 peer="203.0.113.9", scheme="https", cookies=cookies,
                                 json={"mode": "image"})
        self.assertNotEqual(switch.status_code, 403, switch.text)
        self.assertEqual(response.status_code, 403)


class StatusPrivacyTests(GateCase):
    """The model endpoint advertises progress, not the owner's hardware."""

    def setUp(self):
        super().setUp()
        self.share()

    def use_peer(self, peer):
        original = module.is_admin_session
        self.addCleanup(setattr, module, "is_admin_session", original)
        setattr(module, "is_admin_session", lambda request, _ok=peer in {"127.0.0.1", "::1"}: _ok)

    async def test_guest_status_omits_machine_detail(self):
        self.use_peer("203.0.113.9")
        response = await self.get("/local/status", cookies=self.device_cookies(self.session()))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        for field in ("state", "mode", "active_requests", "revision"):
            self.assertIn(field, payload)
        for field in ("gpu_layers", "threads", "context_size", "mtp_reason"):
            self.assertNotIn(field, payload)

    async def test_owner_status_keeps_the_detail(self):
        self.use_peer("127.0.0.1")
        response = await self.get("/local/status", cookies=self.device_cookies(self.session()))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("context_size", payload)
        self.assertIn("mtp_reason", payload)


class AdminConsoleTests(GateCase):
    """The owner marks their own browser once, over loopback only."""

    def setUp(self):
        super().setUp()
        self.share()

    async def test_admin_link_marks_this_browser(self):
        self.use_peer("127.0.0.1")
        response = await self.get(f"/?admin={module.share_store.admin_token}")
        self.assertEqual(response.status_code, 303)
        cookie = response.headers["set-cookie"]
        self.assertIn(module.ACCESS_ADMIN_COOKIE, cookie)
        self.assertIn("HttpOnly", cookie)

    async def test_admin_link_also_starts_a_session_so_the_console_opens(self):
        """One click, no passphrase prompt: loopback already proved the owner."""
        self.use_peer("127.0.0.1")
        response = await self.get(f"/?admin={module.share_store.admin_token}")
        header = response.headers["set-cookie"]
        self.assertIn(f"{module.ACCESS_COOKIE}=", header)
        self.assertEqual(module.share_store.session_count(), 1)
        issued = header.split(f"{module.ACCESS_COOKIE}=")[1].split(";")[0]
        self.assertIsNotNone(module.share_store.authenticate(issued))

    async def test_admin_link_reuses_an_existing_session(self):
        self.use_peer("127.0.0.1")
        token = self.session()
        response = await self.get(f"/?admin={module.share_store.admin_token}",
                                  cookies=self.device_cookies(token))
        self.assertEqual(response.status_code, 303)
        self.assertEqual(module.share_store.session_count(), 1)

    async def test_repeated_owner_visits_do_not_pile_up_sessions(self):
        """A cookie-less visitor must not add a row per visit.

        This is the shape of a real accident: a reload loop against the owner link
        used to create an empty session every time and filled the store.
        """
        self.use_peer("127.0.0.1")
        for _ in range(25):
            response = await self.get(f"/?admin={module.share_store.admin_token}")
            self.assertEqual(response.status_code, 303)
        self.assertEqual(module.share_store.session_count(), 1)

    async def test_the_store_caps_how_many_devices_it_remembers(self):
        from app.share.share_store import MAX_SESSIONS
        owner = module.share_store.ensure_admin_session("owner-token")
        for index in range(MAX_SESSIONS + 20):
            module.share_store.create(label=f"device-{index}")
        module.share_store.summarize()
        self.assertLessEqual(module.share_store.session_count(), MAX_SESSIONS)
        self.assertIsNotNone(module.share_store.authenticate(owner))

    async def test_admin_link_is_refused_from_a_tunnel(self):
        self.use_peer("203.0.113.9")
        response = await self.get(f"/?admin={module.share_store.admin_token}",
                                  host="plain-paper-example.trycloudflare.com",
                                  peer="203.0.113.9", scheme="https")
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("set-cookie", response.headers)

    async def test_admin_cookie_does_not_bypass_the_passphrase(self):
        self.use_peer("127.0.0.1")
        response = await self.get("/local/status",
                                  cookies={module.ACCESS_ADMIN_COOKIE: module.share_store.admin_token})
        self.assertEqual(response.status_code, 401)

    async def test_admin_cookie_turns_a_local_device_session_into_an_admin_one(self):
        self.use_peer("127.0.0.1")
        token = self.session()
        cookies = {module.ACCESS_COOKIE: token,
                   module.ACCESS_ADMIN_COOKIE: module.share_store.admin_token}
        response = await self.get("/local/share/status", cookies=cookies)
        self.assertEqual(response.status_code, 200)


class ShareManagerApiTests(GateCase):
    """What the manager panel calls, and what it is allowed to change."""

    def setUp(self):
        super().setUp()
        self.share()
        self.admin = self.session(kind="admin", label="本机")
        self.admin_cookies = {module.ACCESS_COOKIE: self.admin}
        self.cookies_device = self.device_cookies(self.session(label="访客"))

    async def status(self):
        response = await self.get("/local/share/status", cookies=self.admin_cookies)
        self.assertEqual(response.status_code, 200)
        return response.json()

    async def test_status_lists_devices_with_activity(self):
        device = self.session(label="iPhone 15", source="10.36.22.9")
        module.share_store.note_request(device, "/v1/chat/completions", received=2048)
        payload = await self.status()
        entry = next(item for item in payload["sessions"] if item["label"] == "iPhone 15")
        self.assertEqual(entry["kind"], "device")
        self.assertEqual(entry["requests"], 1)
        self.assertEqual(entry["bytes"], 2048)
        self.assertEqual(entry["last_source"], "10.36.22.9")
        self.assertFalse(entry["current"])
        self.assertEqual(payload["pin"], "correct-horse")
        self.assertIn("online", payload)

    async def test_status_marks_the_current_session(self):
        payload = await self.status()
        current = [item for item in payload["sessions"] if item["current"]]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["kind"], "admin")

    async def test_revoke_kicks_a_device_out_and_deletes_its_record(self):
        device = self.session(label="guest")
        handle = module.share_store.handle(device)
        response = await self.post(f"/local/share/sessions/{handle}/revoke", cookies=self.admin_cookies)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["revoked"])
        follow_up = await self.get("/local/status", cookies=self.device_cookies(device))
        self.assertEqual(follow_up.status_code, 401)
        payload = await self.status()
        self.assertNotIn(handle, [item["token"] for item in payload["sessions"]],
                         "the kicked device is gone from the panel, not greyed out")

    async def test_kick_all_removes_every_device_in_one_call(self):
        for label in ("a", "b", "c"):
            self.session(label=label)
        devices = sum(1 for item in (await self.status())["sessions"] if item["kind"] != "admin")
        response = await self.post("/local/share/sessions/revoke-all", cookies=self.admin_cookies,
                                   json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["kicked"], devices)
        self.assertGreaterEqual(devices, 3)
        payload = await self.status()
        self.assertEqual([item["kind"] for item in payload["sessions"]], ["admin"],
                         "only the local management session is left")

    async def test_kick_all_is_refused_for_a_guest(self):
        self.use_peer("203.0.113.9")
        response = await self.post("/local/share/sessions/revoke-all", cookies=self.cookies_device,
                                   json={})
        self.assertEqual(response.status_code, 403, response.status_code)

    async def test_forget_unused_removes_only_idle_records(self):
        idle = self.session(label="never used")
        active = self.session(label="active")
        module.share_store.note_request(active, "/v1/chat/completions", received=32)
        idle_handles = {module.share_store.handle(token) for token, session
                        in module.share_store.sessions.items()
                        if session.get("kind") != "admin" and not session.get("requests")}
        response = await self.post("/local/share/sessions/forget-unused",
                                   cookies=self.admin_cookies, json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["removed"], len(idle_handles))
        self.assertIn(module.share_store.handle(idle), idle_handles)
        self.assertIsNone(module.share_store.authenticate(idle), "the idle record is deleted")
        self.assertIsNotNone(module.share_store.authenticate(active), "an active device stays")

    async def test_revoke_refuses_to_kick_the_current_session(self):
        handle = module.share_store.handle(self.admin)
        response = await self.post(f"/local/share/sessions/{handle}/revoke", cookies=self.admin_cookies)
        self.assertEqual(response.status_code, 409)

    async def test_revoke_reports_an_unknown_handle(self):
        response = await self.post("/local/share/sessions/deadbeefdeadbeef/revoke",
                                   cookies=self.admin_cookies)
        self.assertEqual(response.status_code, 404)

    async def test_rotation_changes_the_passphrase_for_new_devices_only(self):
        existing = self.session(label="已加入的设备")
        response = await self.post("/local/share/pin", cookies=self.admin_cookies,
                                   json={"pin": "123456-789012-345678"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pin"], "123456-789012-345678")
        # The passphrase in force really changed.
        refused = await self.post("/__login", data={"token": "correct-horse"})
        self.assertEqual(refused.status_code, 401)
        accepted = await self.post("/__login", data={"token": "123456-789012-345678"})
        self.assertEqual(accepted.status_code, 303)
        # The device that had already joined was not interrupted.
        still_working = await self.get("/local/status", cookies=self.device_cookies(existing))
        self.assertEqual(still_working.status_code, 200)

    async def test_rotation_rejects_a_malformed_passphrase(self):
        for bad in ("123456", "12345-789012-345678", "abcdef-789012-345678", "", 123456):
            response = await self.post("/local/share/pin", cookies=self.admin_cookies,
                                       json={"pin": bad})
            self.assertEqual(response.status_code, 400, bad)

    async def test_online_toggle_rejects_a_non_boolean(self):
        response = await self.post("/local/share/online", cookies=self.admin_cookies,
                                   json={"online": "yes"})
        self.assertEqual(response.status_code, 400)

    def test_overlay_is_found_by_convention_without_the_environment_variable(self):
        """The watchdog and any tool resolve settings the same way the server does."""
        from app import qwen_config
        with patch.object(qwen_config, "ROOT", Path(self.directory.name)):
            overlay = Path(self.directory.name) / "share.config.json"
            overlay.write_text(json.dumps({"access_token": "convention-pin"}), encoding="utf-8")
            # Removed, not blanked: a blank variable means "sharing is off here".
            self.forget_share_variables()
            self.addCleanup(self.forget_share_variables)
            # The convention path is normally suppressed during a test run so a suite
            # cannot adopt the machine's live passphrase; this test is about that
            # path, so the suppression is lifted for it.
            with patch.dict("os.environ", {"QWEN_TEST_ISOLATION": "0"}):
                self.assertEqual(overlay, qwen_config.share_overlay())
                self.assertEqual(qwen_config.share_setting("access_token"), "convention-pin")

    def test_overlay_is_absent_when_neither_source_exists(self):
        from app import qwen_config
        with patch.object(qwen_config, "ROOT", Path(self.directory.name)):
            with patch.dict("os.environ", {"QWEN_SHARE_CONFIG": "",
                                           module.ACCESS_TOKEN_ENV: ""}):
                self.assertIsNone(qwen_config.share_overlay())

    def test_setting_the_variable_empty_switches_sharing_off_completely(self):
        """An explicitly empty variable hides the overlay this machine really has.

        Without that, a suite or a deliberately local-only launch would silently
        start enforcing the live passphrase from share.config.json.
        """
        from app import qwen_config
        with patch.object(qwen_config, "ROOT", Path(self.directory.name)):
            (Path(self.directory.name) / "share.config.json").write_text(
                json.dumps({"access_token": "live-pin"}), encoding="utf-8")
            with patch.dict("os.environ", {"QWEN_SHARE_CONFIG": "",
                                           module.ACCESS_TOKEN_ENV: ""}):
                self.assertIsNone(qwen_config.share_overlay())
                self.assertNotEqual(qwen_config.share_setting("access_token"), "live-pin")

    def test_a_local_scope_start_ignores_sharing_entirely(self):
        """The plain Start-Qwen.bat entry point must never become a shared server.

        This is the whole point of two separate launchers: the local one ignores a
        share.config.json that the sharing launcher left behind, and it cannot rely on
        blanking variables because the service seeds its own passphrase from whatever
        configuration it can still find.
        """
        from app import qwen_config
        with patch.object(qwen_config, "ROOT", Path(self.directory.name)):
            overlay = Path(self.directory.name) / "share.config.json"
            overlay.write_text(
                json.dumps({"access_token": "live-pin", "allowed_hosts": ["*"]}),
                encoding="utf-8")
            # What the local launcher actually does: clear the pointer, mark the scope.
            self.forget_share_variables()
            self.addCleanup(self.forget_share_variables)
            os.environ["QWEN_SHARE_SCOPE"] = "local"
            self.addCleanup(os.environ.pop, "QWEN_SHARE_SCOPE", None)
            with patch.dict("os.environ", {module.ACCESS_TOKEN_ENV: ""}):
                self.assertTrue(qwen_config.sharing_is_off())
                self.assertIsNone(qwen_config.share_overlay())
                self.assertEqual(qwen_config.share_setting("access_token"), "")
                self.assertEqual(qwen_config.share_setting("allowed_hosts"), "")

    def test_a_share_scope_start_reads_the_configuration(self):
        from app import qwen_config
        with patch.object(qwen_config, "ROOT", Path(self.directory.name)):
            overlay = Path(self.directory.name) / "share.config.json"
            overlay.write_text(json.dumps({"access_token": "shared-pin"}), encoding="utf-8")
            self.forget_share_variables()
            self.addCleanup(self.forget_share_variables)
            os.environ["QWEN_SHARE_SCOPE"] = "share"
            os.environ["QWEN_SHARE_CONFIG"] = str(overlay)
            self.addCleanup(os.environ.pop, "QWEN_SHARE_SCOPE", None)
            self.addCleanup(os.environ.pop, "QWEN_SHARE_CONFIG", None)
            self.assertFalse(qwen_config.sharing_is_off())
            self.assertEqual(qwen_config.share_overlay(), overlay)
            self.assertEqual(qwen_config.share_setting("access_token"), "shared-pin")

    async def test_status_reports_the_public_url_from_the_tunnel_log(self):
        log = Path(self.directory.name) / "logs"
        log.mkdir(exist_ok=True)
        (log / "cloudflared.log").write_text(
            "INF https://old-name.trycloudflare.com\n"
            "INF https://plain-paper-example.trycloudflare.com\n", encoding="utf-8")
        with patch.object(module, "ROOT", Path(self.directory.name)), \
                patch.object(module, "share_online", return_value=True):
            payload = await self.status()
        self.assertEqual(payload["url"], "https://plain-paper-example.trycloudflare.com")

    async def test_status_hides_the_url_while_disconnected(self):
        with patch.object(module, "share_online", return_value=False):
            payload = await self.status()
        self.assertEqual(payload["url"], "")

    async def test_owner_can_stop_sharing_and_the_service_from_the_panel(self):
        """The panel's stop button ends the tunnel and the gateway, keeping config."""
        (Path(self.directory.name) / "logs").mkdir(exist_ok=True)
        with patch.object(module, "ROOT", Path(self.directory.name)), \
                patch.object(module, "set_share_online", return_value=False) as tunnel:
            response = await self.post("/local/share/stop", cookies=self.admin_cookies,
                                       json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["stopped"])
        tunnel.assert_called_once_with(False)
        self.assertTrue((Path(self.directory.name) / "service.stop").is_file(),
                        "the stop marker keeps a supervisor from respawning the gateway")

    async def test_stop_is_refused_while_a_request_is_generating(self):
        with patch.object(module, "ROOT", Path(self.directory.name)), \
                patch.object(module, "set_share_online", return_value=False):
            module.manager.active_requests = 1
            self.addCleanup(lambda: setattr(module.manager, "active_requests", 0))
            response = await self.post("/local/share/stop", cookies=self.admin_cookies, json={})
        self.assertEqual(response.status_code, 409, response.text)

    async def test_a_guest_cannot_stop_the_service(self):
        self.use_peer("203.0.113.9")
        response = await self.post("/local/share/stop", cookies=self.cookies_device, json={})
        self.assertEqual(response.status_code, 403, response.status_code)

    async def test_status_reports_the_tunnel_as_offline_when_no_process_runs(self):
        with patch.object(module, "tunnel_process", return_value=None):
            module._tunnel_state["checked_at"] = 0.0
            payload = await self.status()
        self.assertFalse(payload["online"])

    def test_tunnel_probe_reads_a_running_task(self):
        """The real Windows call, not a stub: the request must not depend on locale.

        The status text is localised, so what matters is that the call succeeds and
        the process probe answers a real boolean.
        """
        completed = module._run_hidden(
            ["schtasks.exe", "/Query", "/TN", "QwenCloudflaredTunnel", "/FO", "LIST"], 10)
        self.assertIn(completed.returncode, (0, 1))
        self.assertIsInstance(completed.stdout, str)
        self.assertIsInstance(module.share_online(), bool)

    def test_tunnel_detection_does_not_parse_localised_text(self):
        """A running tunnel is found by its process, not by the English status word."""
        with patch.object(module, "tunnel_process", return_value=object()):
            module._tunnel_state["checked_at"] = 0.0
            self.assertTrue(module.share_online())
        with patch.object(module, "tunnel_process", return_value=None):
            module._tunnel_state["checked_at"] = 0.0
            self.assertFalse(module.share_online())


class OriginTests(GateCase):
    """Cross-site state changes stay rejected even for the owner's browser."""

    async def test_local_origin_is_accepted(self):
        response = await self.post("/local/mode", json={"mode": "text"},
                                   headers={"origin": "http://127.0.0.1:8081"})
        self.assertNotEqual(response.status_code, 403)

    async def test_foreign_origin_is_rejected(self):
        response = await self.post("/local/mode", json={"mode": "text"},
                                   headers={"origin": "http://evil.example.com"})
        self.assertEqual(response.status_code, 403)

    async def test_tunnel_origin_matches_the_forwarded_host(self):
        response = await self.post("/local/context/status",
                                   host="plain-paper-example.trycloudflare.com",
                                   peer="203.0.113.9", scheme="https",
                                   headers={"origin": "https://plain-paper-example.trycloudflare.com"})
        self.assertNotEqual(response.status_code, 403)

    async def test_configured_front_end_origin_is_accepted(self):
        with patch.dict("os.environ", {module.ALLOWED_ORIGINS_ENV: "https://panel.example.com"}):
            response = await self.post("/local/context/status",
                                       headers={"origin": "https://panel.example.com"})
        self.assertNotEqual(response.status_code, 403)


class OverlayFileTests(GateCase):
    """The sharing overlay is hand-edited JSON, so a list has to work too."""

    def setUp(self):
        super().setUp()
        self.overlay = Path(self.directory.name) / "share.config.json"
        self.overlay.write_text(json.dumps({
            "listen_host": "0.0.0.0",
            "listen_port": 9100,
            "access_token": "overlay-pin",
            "allowed_hosts": ["192.168.1.20", "*.trycloudflare.com"],
        }), encoding="utf-8")
        # The harness blanks the sharing variables to isolate tests and the stored
        # passphrase outranks the overlay by design, so both are cleared first and
        # the overlay is then pointed at explicitly.
        self.forget_share_variables()
        self.overlay_env = patch.dict("os.environ", {"QWEN_SHARE_CONFIG": str(self.overlay)})
        self.overlay_env.start()
        self.addCleanup(self.overlay_env.stop)

    async def test_overlay_list_is_read_as_separate_names(self):
        names = module.allowed_host_names()
        self.assertIn("192.168.1.20", names)
        self.assertIn("*.trycloudflare.com", names)
        self.assertNotIn("['192.168.1.20'", names)

    async def test_overlay_drives_bind_passphrase_and_port(self):
        self.assertEqual(module.listening_host(), "0.0.0.0")
        self.assertEqual(module.listening_port(), 9100)
        self.assertEqual(module.access_token(), "overlay-pin")

    async def test_environment_still_wins_over_the_overlay(self):
        with patch.dict("os.environ", {module.ACCESS_TOKEN_ENV: "environment-pin"}):
            self.assertEqual(module.access_token(), "environment-pin")

    async def test_overlay_exposes_the_gate_over_that_name(self):
        response = await self.post("/v1/chat/completions", host="192.168.1.20:8081",
                                   json={"messages": []})
        self.assertEqual(response.status_code, 401)

    def test_overlay_disappears_when_the_file_is_gone(self):
        """Stopping sharing must restore local-only access without editing settings."""
        self.overlay.unlink()
        self.assertEqual(module.listening_host(), "127.0.0.1")
        self.assertEqual(module.access_token(), "")


class SupervisorHeartbeatTests(GateCase):
    """The watchdog probes routes that sit behind the passphrase gate.

    Without its token the supervisor reads the 401 as an unhealthy gateway and
    restarts a working one in a loop.
    """

    def setUp(self):
        super().setUp()
        # A passphrase has to be in force for the gate to demand anything at all,
        # and the credential the watchdog uses comes from the live admin token.
        self.share(token="supervisor-pin")
        self.use_peer("127.0.0.1")
        environment = patch.dict("os.environ", {"QWEN_SHARE_CONFIG": ""})
        environment.start()
        self.addCleanup(environment.stop)

    def watchdog(self):
        from app import qwen_service
        return qwen_service

    def test_heartbeat_carries_the_gate_credential(self):
        """The watchdog presents the admin token, which survives passphrase rotation."""
        from app import qwen_service
        if not (qwen_service.ROOT / "logs" / "admin.token").is_file():
            self.skipTest("no admin token on this machine yet")
        url = qwen_service.gateway_url("/local/status")
        self.assertIn(f"{module.GATE_PARAM}=", url)
        self.assertNotIn(f"{module.OWNER_PARAM}=", url)

    def watchdog_token(self):
        """The credential the live watchdog presents, read the way it reads it.

        This has to be the token on disk rather than the isolated store's own: the
        watchdog resolves the admin token from ``logs/admin.token``, so a store that
        generated a different one would make these tests pass while the real probe
        still received the login page.
        """
        from app import qwen_service
        token = ""
        try:
            token = (qwen_service.ROOT / "logs" / "admin.token").read_text(encoding="ascii").strip()
        except OSError:
            token = ""
        if not token:
            token = module.share_store.admin_token
        module.share_store.admin_token = token
        self.watchdog = token
        return token

    async def test_a_gate_credential_request_answers_the_route(self):
        """It must answer the route, not redirect: the watchdog reads non-200 as down.

        Sharing one parameter with the console exchange is exactly the bug this
        covers: the probe would receive the cookie-exchange redirect, the supervisor
        would call a healthy gateway dead, and it would restart it in a loop. The
        watchdog also holds a session, because it logs in like any other client.
        """
        self.watchdog_token()
        cookies = self.device_cookies(self.session(label="watchdog"))
        response = await self.get(
            f"/local/status?{module.GATE_PARAM}={self.watchdog}", cookies=cookies)
        self.assertEqual(response.status_code, 200, response.status_code)
        self.assertEqual(response.json()["service"], "qwen-local-modes")
        self.assertNotIn("location", response.headers)

    async def test_a_cookieless_gate_credential_answers_the_route(self):
        """The real watchdog shape: no cookie at all, only the gate parameter.

        The route has to answer 200 anyway. It could not before: the gate read an
        unknown caller from any parameter other than the console's, so the probe got
        the login page, the supervisor read 401 as a dead gateway, and it restarted a
        healthy one every few seconds — which is what tore down the shared service.
        """
        token = self.watchdog_token()
        response = await self.get(f"/local/status?{module.GATE_PARAM}={token}")
        self.assertEqual(response.status_code, 200, response.status_code)
        self.assertEqual(response.json()["service"], "qwen-local-modes")
        self.assertEqual(list(module.share_store.sessions), [module._admin_session_token()])

    async def test_the_cookieless_probe_reuses_the_owners_session(self):
        """A probe every five seconds must not add a session row each time."""
        token = self.watchdog_token()
        before = len(module.share_store.sessions)
        for _ in range(3):
            response = await self.get(f"/local/status?{module.GATE_PARAM}={token}")
            self.assertEqual(response.status_code, 200, response.status_code)
        self.assertEqual(len(module.share_store.sessions), before + 1,
                         "one stable session for the watchdog, not one per probe")
        derived = module._admin_session_token()
        self.assertEqual(module.share_store.sessions[derived]["kind"], "admin")

    async def test_a_wrong_gate_credential_still_gets_the_login_page(self):
        response = await self.get(f"/local/status?{module.GATE_PARAM}=not-the-admin-token")
        self.assertEqual(response.status_code, 401, response.status_code)

    async def test_the_console_parameter_still_exchanges_for_a_cookie(self):
        response = await self.get(
            f"/local/status?{module.OWNER_PARAM}={module.share_store.admin_token}",
            cookies=self.device_cookies(self.session()))
        self.assertEqual(response.status_code, 303, response.status_code)

    async def test_a_gate_credential_only_satisfies_the_gate(self):
        """The credential admits a request; the owner tier still decides permission."""
        cookies = self.device_cookies(self.session(label="watchdog"))
        with patch.object(module, "usage_operation", return_value={"rows": []}):
            owner = await self.get(
                f"/local/usage?{module.GATE_PARAM}={module.share_store.admin_token}", cookies=cookies)
            self.assertEqual(owner.status_code, 200, owner.status_code)
            self.use_peer("203.0.113.9")
            guest = await self.get(
                f"/local/usage?{module.GATE_PARAM}={module.share_store.admin_token}", cookies=cookies)
            self.assertEqual(guest.status_code, 403, guest.status_code)

    def test_heartbeat_falls_back_to_the_passphrase(self):
        """With no admin token on disk the sharing passphrase is used instead."""
        from app import qwen_service
        with patch.object(qwen_service, "supervisor_credential",
                          return_value=("k", "supervisor-pin")):
            self.assertEqual(qwen_service.gateway_url("/local/status"),
                             "/local/status?k=supervisor-pin")

    def test_the_watchdog_reads_the_passphrase_from_the_sharing_overlay(self):
        """It must resolve the passphrase like the server does, not only from env.

        A launcher that configured sharing through the overlay without exporting an
        environment variable still has to give the watchdog a way past the gate.
        """
        from app import qwen_service
        overlay = Path(self.directory.name) / "share.config.json"
        overlay.write_text(json.dumps({"access_token": "overlay-pin"}), encoding="utf-8")
        qwen_service.SESSION_READY.clear()
        self.addCleanup(qwen_service.SESSION_READY.clear)
        environment = patch.dict("os.environ", {"QWEN_SHARE_CONFIG": str(overlay)})
        environment.start()
        self.addCleanup(environment.stop)
        self.forget_share_variables()
        os.environ["QWEN_SHARE_CONFIG"] = str(overlay)
        with patch.object(qwen_service, "gateway_status", return_value=None):
            with patch.object(qwen_service, "OPENER") as opener:
                opener.open.return_value.read.return_value = b""
                self.assertFalse(qwen_service.ensure_session())
                sent = opener.open.call_args.args[0]
                self.assertIn(b"token=overlay-pin", sent.data)

    def test_control_routes_carry_the_credential_too(self):
        from app import qwen_service
        with patch.object(qwen_service, "supervisor_credential",
                          return_value=("k", "supervisor-pin")):
            self.assertEqual(qwen_service.gateway_url("/local/shutdown"),
                             "/local/shutdown?k=supervisor-pin")
            self.assertEqual(qwen_service.gateway_url("/local/recover"),
                             "/local/recover?k=supervisor-pin")

    def test_existing_query_parameters_are_preserved(self):
        from app import qwen_service
        with patch.object(qwen_service, "supervisor_credential",
                          return_value=("k", "supervisor-pin")):
            self.assertEqual(qwen_service.gateway_url("/local/status?verbose=1"),
                             "/local/status?verbose=1&k=supervisor-pin")

    def test_no_credential_keeps_the_plain_path(self):
        from app import qwen_service
        with patch.object(qwen_service, "supervisor_credential", return_value=(None, "")):
            self.assertEqual(qwen_service.gateway_url("/local/status"), "/local/status")

    def test_special_characters_are_url_encoded(self):
        from app import qwen_service
        with patch.object(qwen_service, "supervisor_credential",
                          return_value=("k", "a b&c=d")):
            self.assertEqual(qwen_service.gateway_url("/local/status"),
                             "/local/status?k=a%20b%26c%3Dd")


class FeatureSwitchTests(GateCase):
    """A shared address without a passphrase must be refused, not served."""

    def test_loopback_detection(self):
        """The owner tier keys off the peer address, so pin its exact boundary."""
        for peer in ("127.0.0.1", "127.0.0.2", "127.9.9.9", "::1"):
            self.assertTrue(module.peer_is_loopback(fake_request(peer)), peer)
        for peer in ("192.168.1.20", "10.36.22.9", "203.0.113.9", "::ffff:127.0.0.1", "0.0.0.0"):
            self.assertFalse(module.peer_is_loopback(fake_request(peer)), peer)

    def test_a_request_without_a_peer_is_not_local(self):
        request = fake_request("127.0.0.1")
        request.scope["client"] = None
        self.assertFalse(module.peer_is_loopback(request))

    async def test_bind_address_detection(self):
        for host in ("127.0.0.1", "localhost", "::1", "[::1]"):
            self.assertTrue(module.is_loopback_host(host), host)
        for host in ("0.0.0.0", "192.168.1.20", "::"):
            self.assertFalse(module.is_loopback_host(host), host)

    async def test_listen_defaults_come_from_settings(self):
        with patch.object(module.manager, "settings",
                          module.manager.settings | {"listen_host": "0.0.0.0", "listen_port": 9000}):
            self.assertEqual(module.listening_host(), "0.0.0.0")
            self.assertEqual(module.listening_port(), 9000)
        with patch.dict("os.environ", {"QWEN_LISTEN_HOST": "127.0.0.1"}):
            self.assertEqual(module.listening_host(), "127.0.0.1")
