"""Guest uploads: the switch, the isolation, and the limits.

Option A of the sharing work: a shared visitor may put a clip (or a shot reference image)
on the machine and fetch its own material back, while the save-location picker and the
machine's own output tree stay owner-only. Generation itself was opened to visitors later
at the owner's request, and the reference images came with it — so what these tests pin is
that "open" still means *per session*: a guest's uploads are recorded against its session,
it cannot use another session's reference image, and the storage switch still closes it all.

These run through the real ASGI app, so they exercise the deployed route table and the
per-route guard rather than a copy of its rules. ``use_peer`` supplies the peer address
uvicorn would report, which is what separates the owner from a tunnel visitor; the
session cookie is a real session from the isolated store.
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import guest_policy
import guest_uploads
import mode_server as module
import qwen_config
import video_routes
from test_access_gate import GateCase

CLIP = b"\x00\x00\x00\x18ftypmp42" + b"x" * 200
# 参考图：上传路由按后缀与体积判定（内容检查在 ComfyUI 那边），这里给一段带 PNG 魔数的负载。
REFERENCE = b"\x89PNG\r\n\x1a\n" + b"x" * 120


class GuestUploadCase(GateCase):
    """Harness: isolated clip folders, an isolated policy store, guest uploads on."""

    def setUp(self):
        super().setUp()
        self.media = Path(self.directory.name) / "media"
        self.input_root = self.media / "input"
        self.output_root = self.media / "output"
        self.input_root.mkdir(parents=True)
        self.output_root.mkdir(parents=True)
        for name, value in (("INPUT_ROOT", self.input_root), ("OUTPUT_ROOT", self.output_root),
                            ("DEFAULT_OUTPUT_ROOT", self.output_root / "video")):
            for target in (video_routes,):
                patcher = patch.object(target, name, value)
                patcher.start()
                self.addCleanup(patcher.stop)
        # A per-test store in the temporary directory, so one test's switch cannot leak
        # into the next and nothing is written to the machine's real records.
        previous = module.video_routes._POLICY_STORE
        self.store = guest_uploads.GuestUploads(directory=self.media)
        module.video_routes.set_policy_store(self.store)
        self.addCleanup(module.video_routes.set_policy_store, previous)
        self.addCleanup(guest_uploads._SOURCES.clear)
        # The capability switches are a per-test document too: the machine's own guest policy
        # must not decide what these tests can see, and a test that closes a feature must not
        # leave it closed for the next one.
        previous_policy = guest_policy.policy()
        self.policy = guest_policy.GuestPolicy(directory=self.media)
        guest_policy.set_policy(self.policy)
        self.addCleanup(guest_policy.set_policy, previous_policy)

    # ---------------------------------------------------------------- helpers
    def open_uploads(self, policy="guests"):
        """Open (or close) the storage capability the way the sharing panel does."""
        self.policy.set_feature("storage", policy == "guests")

    def close(self, feature):
        self.policy.set_feature(feature, False)

    async def upload(self, cookies, name="clip.mp4", payload=CLIP, **kwargs):
        return await self.post(f"/local/video/upload?name={name}", cookies=cookies,
                               content=payload, **kwargs)

    def stored_clips(self):
        return sorted(path.name for path in self.input_root.glob("*") if path.is_file())


class SwitchTests(GuestUploadCase):
    """上传/下载是所有者可以随时收回的一项能力（默认开，按所有者这次的要求）。"""

    async def test_guest_upload_is_refused_when_storage_is_closed(self):
        self.close("storage")
        cookies = self.device_cookies(self.session())
        self.use_peer("203.0.113.9")
        response = await self.upload(cookies)
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("上传与下载", response.text)
        self.assertEqual(self.stored_clips(), [])

    async def test_guest_upload_is_accepted_once_the_owner_opens_it(self):
        self.open_uploads()
        cookies = self.device_cookies(self.session())
        self.use_peer("203.0.113.9")
        response = await self.upload(cookies)
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["name"].startswith("qwen-video-"))
        self.assertEqual(payload["bytes"], len(CLIP))
        self.assertEqual(self.stored_clips(), [payload["name"]])

    async def test_owner_upload_does_not_need_the_switch(self):
        cookies = self.device_cookies(self.session())
        self.use_peer("127.0.0.1")
        response = await self.upload(cookies)
        self.assertEqual(response.status_code, 200, response.text)

    async def test_guests_cannot_flip_the_switch_themselves(self):
        cookies = self.device_cookies(self.session())
        self.use_peer("203.0.113.9")
        read = await self.get("/local/share/uploads", cookies=cookies)
        write = await self.post("/local/share/uploads", cookies=cookies, json={"policy": "guests"})
        self.assertEqual(read.status_code, 403, read.text)
        self.assertEqual(write.status_code, 403, write.text)
        self.assertEqual(self.store.get_policy(), "off")

    async def test_owner_can_flip_the_switch_and_an_unknown_value_is_rejected(self):
        cookies = self.device_cookies(self.session())
        self.use_peer("127.0.0.1")
        opened = await self.post("/local/share/uploads", cookies=cookies, json={"policy": "guests"})
        self.assertEqual(opened.status_code, 200, opened.text)
        self.assertEqual(opened.json()["policy"], "guests")
        self.assertEqual(self.store.get_policy(), "guests")
        bad = await self.post("/local/share/uploads", cookies=cookies, json={"policy": "everyone"})
        self.assertEqual(bad.status_code, 400, bad.text)
        self.assertEqual(self.store.get_policy(), "guests")

    async def test_the_sharing_panel_status_reports_the_switches(self):
        cookies = self.device_cookies(self.session())
        self.use_peer("127.0.0.1")
        self.open_uploads()
        payload = (await self.get("/local/share/status", cookies=cookies)).json()
        self.assertTrue(payload["guest_policy"]["features"]["storage"])
        self.assertIn("guest_bytes", payload["guest_uploads"]["usage"])

    async def test_the_switch_survives_a_restart(self):
        self.open_uploads()
        reopened = guest_policy.GuestPolicy(directory=self.media)
        self.assertTrue(reopened.allows("storage"))
        # ...and so does closing one.
        self.close("media")
        self.assertFalse(guest_policy.GuestPolicy(directory=self.media).allows("media"))
        self.assertTrue(guest_policy.GuestPolicy(directory=self.media).allows("switch"))


class IsolationTests(GuestUploadCase):
    """One visitor must not see, list or fetch another visitor's clip."""

    async def test_a_visitor_sees_only_its_own_uploads(self):
        self.open_uploads()
        first = self.device_cookies(self.session(label="first"))
        second = self.device_cookies(self.session(label="second"))
        self.use_peer("203.0.113.9")
        mine = (await self.upload(first)).json()["name"]
        theirs = (await self.upload(second)).json()["name"]

        listed = (await self.get("/local/video/source", cookies=first)).json()
        self.assertEqual(listed["videos"], [mine])
        self.assertNotIn(theirs, listed["videos"])

        owner = self.device_cookies(self.session(label="owner"))
        self.use_peer("127.0.0.1")
        everything = (await self.get("/local/video/source", cookies=owner)).json()
        self.assertEqual(sorted(everything["videos"]), sorted([mine, theirs]))

    async def test_a_visitor_cannot_download_another_visitors_clip(self):
        self.open_uploads()
        first = self.device_cookies(self.session(label="first"))
        second = self.device_cookies(self.session(label="second"))
        self.use_peer("203.0.113.9")
        mine = (await self.upload(first)).json()["name"]
        theirs = (await self.upload(second)).json()["name"]
        inside = str(self.input_root / mine)
        outside = str(self.input_root / theirs)

        own = await self.get(f"/local/video/file?path={inside}", cookies=first)
        self.assertEqual(own.status_code, 200, own.text)
        self.assertEqual(own.content, CLIP)

        stranger = await self.get(f"/local/video/file?path={outside}", cookies=second)
        self.assertEqual(stranger.status_code, 200, stranger.text)
        cross = await self.get(f"/local/video/file?path={outside}", cookies=first)
        # 404 rather than 403: the input folder *is* inside a visitor's fence (it must be, or it
        # could never fetch back its own upload), so the refusal here is the ownership half — and
        # a 404 must not confirm that the other visitor's name exists.
        self.assertEqual(cross.status_code, 404, cross.text)

    async def test_a_visitor_cannot_download_a_file_in_the_output_tree(self):
        self.open_uploads()
        cookies = self.device_cookies(self.session())
        film = self.output_root / "video" / "someone-elses-film.mp4"
        film.parent.mkdir(parents=True, exist_ok=True)
        film.write_bytes(CLIP)
        self.use_peer("203.0.113.9")
        response = await self.get(f"/local/video/file?path={film}", cookies=cookies)
        # A visitor's film output folder is its own, so a file in the owner's tree is not merely
        # "not owned" but outside the roots this caller may read at all.
        self.assertEqual(response.status_code, 403, response.text)

    async def test_a_visitor_reads_its_own_output_folder(self):
        """访客自己的产出目录就是"他自己的存储"：路径围栏放行，别人的不放行。"""
        self.open_uploads()
        first = self.device_cookies(self.session(label="first"))
        second = self.device_cookies(self.session(label="second"))
        self.use_peer("203.0.113.9")
        token = first[module.ACCESS_COOKIE]
        mine = video_routes.guest_root(video_routes.Identity(False, token)) / "my-film.mp4"
        mine.parent.mkdir(parents=True, exist_ok=True)
        mine.write_bytes(CLIP)
        own = await self.get(f"/local/video/file?path={mine}", cookies=first)
        self.assertEqual(own.status_code, 200, own.text)
        self.assertEqual(own.content, CLIP)
        # The same file is outside the second visitor's fence.
        stranger = await self.get(f"/local/video/file?path={mine}", cookies=second)
        self.assertEqual(stranger.status_code, 403, stranger.text)

    async def test_selecting_a_clip_does_not_retarget_another_visitor(self):
        self.open_uploads()
        first = self.device_cookies(self.session(label="first"))
        second = self.device_cookies(self.session(label="second"))
        self.use_peer("203.0.113.9")
        mine = (await self.upload(first)).json()["name"]
        theirs = (await self.upload(second)).json()["name"]

        chosen = await self.post("/local/video/source", cookies=first, json={"path": mine})
        self.assertEqual(chosen.status_code, 200, chosen.text)
        self.assertEqual((await self.get("/local/video/source", cookies=first)).json()["selected"], mine)
        # The second visitor never selected anything, and cannot select the first's clip.
        self.assertIsNone((await self.get("/local/video/source", cookies=second)).json()["selected"])
        refused = await self.post("/local/video/source", cookies=second, json={"path": mine})
        self.assertEqual(refused.status_code, 404, refused.text)
        self.assertEqual(self.store.owner_of(theirs), self.store.owner_of(theirs))

    async def test_a_guest_can_upload_a_reference_image(self):
        """参考图跟着生成一起开放给访客（所有者要求）。

        以前这里是 403「参考图上传仅限本机管理员」，理由是"参考图只用来驱动生成，而生成本身
        仅限机主"——那条理由在访客能自己生成之后就不成立了。现在按会话记账，谁传的只有谁能用。
        """
        self.open_uploads()
        cookies = self.device_cookies(self.session())
        self.use_peer("203.0.113.9")
        response = await self.upload(cookies, name="ref.png", payload=REFERENCE)
        self.assertEqual(response.status_code, 200, response.text)
        name = response.json()["name"]
        self.assertTrue(name.startswith("qwen-image-ref-"), name)
        self.assertEqual(self.store.owner_of(name), cookies[module.ACCESS_COOKIE])

    async def test_the_storage_switch_still_closes_reference_uploads(self):
        """存储那一项关掉时，访客连参考图也传不上来——开放不等于没有开关。"""
        self.close("storage")
        cookies = self.device_cookies(self.session())
        self.use_peer("203.0.113.9")
        response = await self.upload(cookies, name="ref.png", payload=REFERENCE)
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.stored_clips(), [])

    async def test_a_guest_cannot_use_someone_elses_reference_image(self):
        """放开的只是"自己传的"：别人的参考图当不存在，而且拒绝发生在建任务之前。

        这里发的是 generate：围栏在 `check_references()` 里，位置在 `monitor.start()` **之前**，
        所以既不会真的排一次渲染，也不会把别人的素材用进图里。
        """
        self.open_uploads()
        mine = self.device_cookies(self.session(label="mine"))
        theirs = self.device_cookies(self.session(label="theirs"))
        self.use_peer("203.0.113.9")
        stolen = (await self.upload(theirs, name="ref.png", payload=REFERENCE)).json()["name"]

        refused = await self.post("/local/video/generate", cookies=mine,
                                  json={"prompt": "x", "shots": [], "shot_references": [[stolen]]})
        self.assertEqual(refused.status_code, 404, refused.text)
        self.assertIn("参考图", refused.text)
        # 自己的那张就放行到"提交"这一步（测试进程不会真的渲染，见 VideoMonitor.start 的闸）。
        allowed = await self.post("/local/video/generate", cookies=theirs,
                                  json={"prompt": "x", "shots": [], "shot_references": [[stolen]]})
        self.assertEqual(allowed.status_code, 409, allowed.text)

    async def test_the_owners_reference_image_stays_the_owners(self):
        """机主上传的参考图不会被访客借用（上传记录挂在机主那份上）。"""
        self.open_uploads()
        owner = await self.upload({}, name="ref.png", payload=REFERENCE)
        self.assertEqual(owner.status_code, 200, owner.text)
        name = owner.json()["name"]
        self.assertEqual(self.store.owner_of(name), guest_uploads.OWNER_SESSION)

        guest = self.device_cookies(self.session())
        self.use_peer("203.0.113.9")
        refused = await self.post("/local/video/generate", cookies=guest,
                                  json={"prompt": "x", "shots": [], "shot_references": [[name]]})
        self.assertEqual(refused.status_code, 404, refused.text)


class GuestGenerationTests(GuestUploadCase):
    """所有者要求访客能自己生成：生成路由对访客开放，机器自身的布局不开放。"""

    async def test_a_guest_can_reach_every_generating_route(self):
        self.open_uploads()
        cookies = self.device_cookies(self.session())
        self.use_peer("203.0.113.9")
        # Paths are inside the guest's own folder on purpose: what this test is about is the
        # permission gate, and a path outside the fence is refused by the handler's own check.
        own = video_routes.guest_root(
            video_routes.Identity(False, cookies[module.ACCESS_COOKIE]))
        routes = (
            ("POST", "/local/video/generate", {"prompt": "x", "shots": []}),
            ("POST", "/local/video/interpolate", {"path": str(own / "a.mp4"), "multiplier": 2}),
            ("POST", "/local/video/concat", {"clips": [str(own / "a.mp4"), str(own / "b.mp4")]}),
            ("POST", "/local/video/denoise", {"path": str(own / "a.mp4")}),
            ("POST", "/local/video/denoise-source", {"name": "x.mp4"}),
            ("POST", "/local/video/cancel", None),
            ("GET", "/local/video/progress", None),
            ("GET", "/local/video/history", None),
            ("GET", "/local/video/output", None),
            ("GET", "/local/video/comfy", None),
            ("GET", "/local/video/recipes", None),
        )
        for method, path, body in routes:
            response = await self.request(method, path, cookies=cookies, json=body)
            # Not 403: what the handler then answers (404 for a missing path, 409 for a busy
            # monitor) is its own business; the permission gate must let the call through.
            self.assertNotEqual(response.status_code, 403, f"{method} {path}: {response.text}")

    async def test_a_guest_is_told_its_own_output_folder(self):
        self.open_uploads()
        cookies = self.device_cookies(self.session())
        self.use_peer("203.0.113.9")
        payload = (await self.get("/local/video/output", cookies=cookies)).json()
        self.assertIn("guests", payload["current"])
        self.assertEqual(payload["default"], payload["current"])

    async def test_closing_media_refuses_the_generating_routes(self):
        self.close("media")
        cookies = self.device_cookies(self.session())
        self.use_peer("203.0.113.9")
        for method, path, body in (("POST", "/local/video/generate", {"prompt": "x", "shots": []}),
                                   ("GET", "/local/video/progress", None),
                                   ("GET", "/local/video/recipes", None),
                                   ("POST", "/local/image/cancel", None)):
            response = await self.request(method, path, cookies=cookies, json=body)
            self.assertEqual(response.status_code, 403, f"{method} {path}")
            self.assertIn("生图与生成视频", response.text)

    async def test_the_save_location_picker_is_not_reachable_by_a_guest(self):
        self.open_uploads()
        cookies = self.device_cookies(self.session())
        self.use_peer("203.0.113.9")
        response = await self.get("/local/video/folders?path=C:\\", cookies=cookies)
        self.assertEqual(response.status_code, 403, response.text)

    async def test_the_owner_keeps_every_route(self):
        cookies = self.device_cookies(self.session())
        self.use_peer("127.0.0.1")
        for path in ("/local/video/source", "/local/video/output", "/local/video/recipes",
                     "/local/video/history", "/local/video/progress", "/local/video/folders"):
            response = await self.get(path, cookies=cookies)
            self.assertNotEqual(response.status_code, 403, f"{path}: {response.text}")


class LimitTests(GuestUploadCase):
    """Quotas and the free-space floor, checked before a single byte is written."""

    async def test_a_visitor_cannot_exceed_its_own_quota(self):
        self.open_uploads()
        cookies = self.device_cookies(self.session())
        self.use_peer("203.0.113.9")
        with patch.object(guest_uploads, "GUEST_SESSION_BYTES", len(CLIP) + 1):
            first = await self.upload(cookies)
            second = await self.upload(cookies)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 413, second.text)
        self.assertEqual(len(self.stored_clips()), 1)

    async def test_the_total_guest_quota_is_enforced(self):
        self.open_uploads()
        first = self.device_cookies(self.session(label="first"))
        self.use_peer("203.0.113.9")
        with patch.object(guest_uploads, "GUEST_SESSION_BYTES", 10 ** 9), \
                patch.object(guest_uploads, "GUEST_TOTAL_BYTES", len(CLIP) + 1):
            accepted = await self.upload(first)
            refused = await self.upload(self.device_cookies(self.session(label="second")))
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(refused.status_code, 413, refused.text)
        self.assertEqual(len(self.stored_clips()), 1)

    async def test_upload_is_refused_when_the_disk_is_almost_full(self):
        self.open_uploads()
        cookies = self.device_cookies(self.session())
        self.use_peer("203.0.113.9")
        with patch.object(guest_uploads, "RESERVE_BYTES", 1 << 60):
            response = await self.upload(cookies)
        self.assertEqual(response.status_code, 507, response.text)
        self.assertEqual(self.stored_clips(), [])

    async def test_the_size_ceiling_still_applies(self):
        self.open_uploads()
        cookies = self.device_cookies(self.session())
        self.use_peer("203.0.113.9")
        with patch.object(video_routes, "VIDEO_MAX_BYTES", 8):
            response = await self.upload(cookies)
        self.assertEqual(response.status_code, 413, response.text)

    async def test_an_unsupported_extension_is_still_rejected(self):
        self.open_uploads()
        cookies = self.device_cookies(self.session())
        self.use_peer("203.0.113.9")
        response = await self.upload(cookies, name="notes.txt")
        self.assertEqual(response.status_code, 400, response.text)

    async def test_kicking_a_device_drops_its_clip_records(self):
        self.open_uploads()
        token = self.session(label="visitor")
        cookies = self.device_cookies(token)
        self.use_peer("203.0.113.9")
        name = (await self.upload(cookies)).json()["name"]
        self.assertEqual(self.store.owner_of(name), token)

        owner = self.device_cookies(self.session(label="owner"))
        self.use_peer("127.0.0.1")
        handle = module.share_store.handle(token)
        revoked = await self.post(f"/local/share/sessions/{handle}/revoke", cookies=owner)
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertIsNone(self.store.owner_of(name))
        # The clip itself is the owner's to keep or delete; the record is what grants access.
        self.assertEqual(len(self.stored_clips()), 1)


class RecordShapeTests(unittest.TestCase):
    """The record this feature keeps must stay content-free."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = guest_uploads.GuestUploads(directory=self.directory.name)

    def test_records_hold_only_name_session_size_and_time(self):
        self.store.note_upload("qwen-video-abc.mp4", "session-token", 1234)
        text = (Path(self.directory.name) / guest_uploads.FILE_NAME).read_text(encoding="utf-8")
        entry = self.store.records["qwen-video-abc.mp4"]
        self.assertEqual(set(entry), {"session", "bytes", "uploaded_at"})
        self.assertNotIn("prompt", text.lower())

    def test_an_unknown_policy_is_refused(self):
        with self.assertRaises(ValueError):
            self.store.set_policy("everyone")

    def test_a_damaged_record_file_does_not_break_the_panel(self):
        (Path(self.directory.name) / guest_uploads.FILE_NAME).write_text("{not json", encoding="utf-8")
        store = guest_uploads.GuestUploads(directory=self.directory.name)
        self.assertEqual(store.get_policy(), "off")
        self.assertEqual(store.usage()["files"], 0)

    def test_source_selection_is_per_session(self):
        guest_uploads.select_source("a", "one.mp4")
        guest_uploads.select_source("b", "two.mp4")
        self.addCleanup(guest_uploads._SOURCES.clear)
        self.assertEqual(guest_uploads.selected_source("a"), "one.mp4")
        self.assertEqual(guest_uploads.selected_source("b"), "two.mp4")
        guest_uploads.prune_sources({"a"})
        self.assertIsNone(guest_uploads.selected_source("b"))


if __name__ == "__main__":
    unittest.main()
