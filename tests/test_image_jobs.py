"""Regression tests for the image job runner, progress API and local history.

The generation cycle reaches ComfyUI through ``manager.client``; these tests
replace that transport, so no model, GPU or ComfyUI process is required.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import asyncio
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx

from app.image import image_comfy
from app.image import image_jobs
from app.image import image_pipelines
from app import mode_server as module
from app import qwen_config


_TEST_WORKFLOW_DIR: "tempfile.TemporaryDirectory | None" = None
_TEST_WORKFLOW_PATCH = None


def setUpModule():
    """Never let a test touch the machine's real logs/image-workflow.json."""
    global _TEST_WORKFLOW_DIR, _TEST_WORKFLOW_PATCH
    _TEST_WORKFLOW_DIR = tempfile.TemporaryDirectory()
    _TEST_WORKFLOW_PATCH = patch.object(
        image_comfy, "WORKFLOW_PATH", Path(_TEST_WORKFLOW_DIR.name) / "image-workflow.json")
    _TEST_WORKFLOW_PATCH.start()


def tearDownModule():
    if _TEST_WORKFLOW_PATCH is not None:
        _TEST_WORKFLOW_PATCH.stop()
    if _TEST_WORKFLOW_DIR is not None:
        _TEST_WORKFLOW_DIR.cleanup()


def default_model(pipeline_key: str, input_name: str) -> str:
    """The file the gateway would pick for a pipeline input on this machine."""
    return image_comfy._default_model(pipeline_key, input_name)


def accepts_input(info: dict, class_type: str, name: str) -> bool:
    """Whether ``name`` is a valid input for ``class_type``.

    ``/object_info`` lists a dynamic combo as one entry and keeps the option's own
    children nested under it, while the API prompt addresses those children by
    their dotted path (``resize_type.multiplier``). Accept both spellings so a
    pipeline may expose a dynamic-combo child as an editable field.
    """
    spec = info[class_type]["input"]
    known = set(spec.get("required", {})) | set(spec.get("optional", {}))
    if name in known:
        return True
    parent, _, child = name.rpartition(".")
    if not parent or parent not in known:
        return False
    entry = spec["required"].get(parent) or spec["optional"].get(parent) or []
    if not entry or entry[0] != "COMFY_DYNAMICCOMBO_V3":
        return False
    options = (entry[1] if len(entry) > 1 and isinstance(entry[1], dict) else {}).get("options") or []
    return any(child in (option.get("inputs", {}).get("required") or {}) for option in options)


def make_monitor(handler, loader=None):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monitor = image_jobs.ImageJobMonitor(client, loader or AsyncMock())
    return monitor, client


class ComfyStopTests(unittest.TestCase):
    """`ComfySupervisor.stop()` 必须真的能停，而且不能靠一个没导入的模块。

    真机踩到两次：`image_comfy.py` 里用了 `psutil` 却没导入它——`restart()`（每轮生图后的清理
    路径）和新的 `stop()`（关页面收工的最后一步）都会抛 NameError。第一次表现为网关卡在 error
    （"网站没用"）；第二次表现为"关页面后 ComfyUI 还占着 8188"。这里用一个假进程把三条路都跑一遍。
    """

    def supervisor(self):
        return image_comfy.ComfySupervisor(Mock())

    def test_stop_terminates_its_own_child(self):
        supervisor = self.supervisor()
        child = Mock()
        child.poll.return_value = None
        supervisor.process = child
        with patch.object(image_comfy.psutil, "process_iter", return_value=[]):
            self.assertTrue(supervisor.stop())
        child.terminate.assert_called_once()
        self.assertIsNone(supervisor.process)
        self.assertIsNone(supervisor.last_error, "收工不该留下「已重启」的提示")

    def test_stop_sweeps_a_comfyui_that_is_not_our_child(self):
        supervisor = self.supervisor()
        stray = Mock()
        stray.info = {"pid": 4242, "name": "python.exe",
                      "cmdline": ["python", str(image_comfy.COMFY_MAIN)]}
        with patch.object(image_comfy.psutil, "process_iter", return_value=[stray]):
            self.assertTrue(supervisor.stop())
        stray.terminate.assert_called_once()

    def test_stop_reports_nothing_when_there_is_nothing_to_stop(self):
        supervisor = self.supervisor()
        with patch.object(image_comfy.psutil, "process_iter", return_value=[]):
            self.assertFalse(supervisor.stop())


class ImageJobTests(unittest.TestCase):
    def make_job(self):
        return image_jobs.ImageJob("img-1", "a cat", "", {"steps": 4}, [])

    def test_queue_load_and_sampling_are_measured_separately(self):
        """A cold run must not report the model swap as sampling time."""
        job = self.make_job()
        clock = {"now": 10.0}
        with patch.object(image_jobs.time, "monotonic", side_effect=lambda: clock["now"]):
            job.note_queue_started()          # the language model is gone
            clock["now"] = 40.0
            job.note_execution_started()      # ComfyUI picked the prompt up -> 30 s queue
            clock["now"] = 61.0
            job.note_sampling_begun()         # first sampler step -> 21 s of weight loading
            clock["now"] = 63.0
            job.note_sampling_finished()      # last step -> 2 s of sampling
        self.assertEqual(job.timings["queue"], 30.0)
        self.assertEqual(job.timings["load"], 21.0)
        self.assertEqual(job.timings["sampling"], 2.0)
        self.assertNotIn("queue", job.marks, "the mark is consumed, so nothing leaks")
        self.assertIsNotNone(job.sampling_started)

    def test_a_job_without_progress_reports_no_sampling_time(self):
        job = self.make_job()
        clock = {"now": 5.0}
        with patch.object(image_jobs.time, "monotonic", side_effect=lambda: clock["now"]):
            job.mark("queue")
            clock["now"] = 5.5
            job.finish_mark("queue")
            job.finish_mark("load")
            job.finish_mark("sampling")
        self.assertEqual(job.timings["queue"], 0.5)
        self.assertNotIn("sampling", job.timings,
                         "a job that never reported a step has no sampling time")
        self.assertNotIn("load", job.timings)

    def test_step_progress_speed_and_eta(self):
        job = self.make_job()
        with patch.object(image_jobs.time, "monotonic", side_effect=[10.0, 12.0, 14.0]):
            job.set_step(0, 4)
            job.set_step(1, 4)
            job.set_step(2, 4)
        self.assertEqual(job.step, 2)
        self.assertEqual(job.max_step, 4)
        self.assertEqual(job.progress, 0.5)
        self.assertEqual(job.step_seconds(), 2.0)
        self.assertEqual(job.eta_seconds(), 4.0)
        snapshot = job.snapshot()
        self.assertEqual(snapshot["steps_per_second"], 0.5)
        self.assertEqual(snapshot["eta_seconds"], 4.0)
        self.assertFalse(snapshot["estimate_calibrated"])

    def test_progress_is_monotonic_when_comfyui_reports_sparsely(self):
        job = self.make_job()
        job.max_step = 4
        job.state = "running"
        job.phase = "sampling"
        job.note_sampling_begun()
        with patch.object(image_jobs.time, "monotonic", return_value=10.0):
            job.set_step(1, 4)
        # ComfyUI reports the first step only, ~10 s in; the projection keeps the
        # bar moving while a lower real value must never move it back.
        with patch.object(image_jobs.time, "monotonic", return_value=30.0):
            job.project_step()
        self.assertGreater(job.step, 1)
        projected = job.step
        with patch.object(image_jobs.time, "monotonic", return_value=31.0):
            job.set_step(1, 4)
        self.assertEqual(job.step, projected, "a sparse lower report never rewinds the bar")
        with patch.object(image_jobs.time, "monotonic", return_value=32.0):
            job.set_step(4, 4)
        self.assertEqual(job.step, 4)
        self.assertEqual(job.progress, 1.0)

    def test_eta_is_uncalibrated_before_the_first_completed_step(self):
        job = self.make_job()
        job.set_step(0, 4)
        self.assertIsNone(job.eta_seconds())
        self.assertIsNone(job.snapshot()["steps_per_second"])

    def test_snapshot_reports_queue_and_model_metadata(self):
        job = self.make_job()
        job.queue_position = 2
        job.queue_pending = 3
        job.phase = "queued"
        snapshot = job.snapshot()
        self.assertEqual(snapshot["queue_position"], 2)
        self.assertEqual(snapshot["queue_pending"], 3)
        self.assertEqual(snapshot["model"]["pipeline"], image_pipelines.DEFAULT_PIPELINE)
        self.assertEqual(snapshot["model"]["pipeline_name"],
                         image_pipelines.pipeline(image_pipelines.DEFAULT_PIPELINE).name)
        self.assertEqual(snapshot["phase"], "queued")

    def test_event_log_is_bounded(self):
        job = self.make_job()
        for index in range(image_jobs.EVENT_LOG_LIMIT + 12):
            job.note("step", str(index))
        self.assertEqual(len(job.snapshot()["events"]), image_jobs.EVENT_LOG_LIMIT)

    def test_binary_preview_accepts_png_and_rejects_junk(self):
        png = b"\x89PNG\r\n\x1a\n" + b"payload"
        preview = image_jobs.ImageJobMonitor._binary_preview(b"\x00\x00\x00\x01\x00\x00\x00\x02" + png)
        self.assertTrue(preview.startswith("data:image/png;base64,"))
        self.assertIsNone(image_jobs.ImageJobMonitor._binary_preview(b"short"))
        self.assertIsNone(image_jobs.ImageJobMonitor._binary_preview(b"\x00" * 8 + b"not-an-image"))

    def test_comfy_error_prefers_the_reported_message(self):
        status = {"messages": [["execution_error", {"exception_message": "CUDA out of memory"}]]}
        self.assertIn("CUDA out of memory", image_jobs.ImageJobMonitor._comfy_error(status))
        self.assertIn("失败", image_jobs.ImageJobMonitor._comfy_error({"messages": []}))


class UpscaleTests(unittest.TestCase):
    """The 超分 recipes must really change the multiplier they promise."""

    def test_the_multiplier_is_a_composer_parameter(self):
        self.assertIn(("3", "resize_type.multiplier"), image_pipelines.linked_inputs("seedvr2"))
        self.assertEqual(image_comfy.workflow_labels("seedvr2")["scale"]["input"], "resize_type.multiplier")
        # denoise is hardcoded to 1.0 in the graph, so the composer must not claim it.
        self.assertNotIn(("10", "denoise"), image_pipelines.linked_inputs("seedvr2"))

    def test_the_request_overrides_the_saved_multiplier(self):
        built = image_comfy.build_graph("seedvr2", "", references=["ref.png"], steps=3, cfg=1.0,
                                        scale=1.25, seed=1, overrides={"3": {"resize_type.multiplier": 1.5}})
        self.assertEqual(built["prompt"]["3"]["inputs"]["resize_type.multiplier"], 1.25)
        self.assertEqual(built["parameters"]["scale"], 1.25)

    def test_the_redraw_ratio_mixes_the_restored_frames_back_with_the_resize(self):
        # 1.0 is the untouched SeedVR2 output: no blend node anywhere.
        plain = image_comfy.build_graph("seedvr2_video", "", references=["clip.mp4"], steps=3,
                                        cfg=1.0, seed=1, scale=1.25, overrides={})["prompt"]
        self.assertFalse(any(node["class_type"] == "ImageBlend" for node in plain.values()))
        self.assertEqual(plain["14"]["inputs"]["images"], ["13", 0])
        # Below 1.0 the restored frames are mixed with node 3, the plain resize.
        dialed = image_comfy.build_graph("seedvr2_video", "", references=["clip.mp4"], steps=3,
                                         cfg=1.0, seed=1, scale=1.25, overrides={},
                                         upscale_strength=0.7)["prompt"]
        blend = [node for node in dialed.values() if node["class_type"] == "ImageBlend"]
        self.assertEqual(len(blend), 1)
        self.assertEqual(blend[0]["inputs"]["blend_factor"], 0.7)
        self.assertEqual(blend[0]["inputs"]["image2"], ["3", 0])
        self.assertEqual(dialed["14"]["inputs"]["images"], ["16", 0])
        # The still-image builder takes the same path into SaveImage.
        still = image_comfy.build_graph("seedvr2", "", references=["ref.png"], steps=3, cfg=1.0,
                                        seed=1, overrides={}, upscale_strength=0.5)["prompt"]
        self.assertEqual(still["15"]["inputs"]["images"], ["16", 0])

    def test_without_a_request_scale_the_saved_value_wins(self):
        built = image_comfy.build_graph("seedvr2", "", references=["ref.png"], steps=2, cfg=1.0,
                                        seed=1, overrides={"3": {"resize_type.multiplier": 1.5}})
        self.assertEqual(built["prompt"]["3"]["inputs"]["resize_type.multiplier"], 1.5)
        # A pipeline that does not declare a multiplier never reports one.
        sdxl = image_comfy.build_graph("sdxl", "cat", steps=8, cfg=1.5, seed=1, scale=2.0, overrides={})
        self.assertNotIn("scale", sdxl["parameters"])

    def test_an_upscale_recipe_switches_pipeline_and_reports_its_multiplier(self):
        monitor = image_jobs.ImageJobMonitor(None, None)
        payload = monitor.apply_workflow("upscale_sharp")
        self.assertEqual(payload["pipeline"], "seedvr2")
        self.assertEqual(payload["params"]["scale"], 1.25)
        self.assertEqual(monitor.pipeline, "seedvr2")
        self.assertEqual(payload["workflow"]["mode"], "image")

    def test_the_sub_modules_own_disjoint_halves_of_the_generate_view(self):
        entries = [entry for entry in image_pipelines.workflows_for_all() if entry.mode == "image"]
        generate = {entry.key for entry in entries if entry.kind == "generate"}
        upscale = {entry.key for entry in entries if entry.kind == "upscale"}
        self.assertEqual(upscale, {"upscale_sharp", "upscale_clean", "upscale_balanced"})
        self.assertTrue(generate and not (generate & upscale), "a recipe belongs to one sub-module")
        for entry in entries:
            if entry.kind == "upscale":
                self.assertEqual(entry.pipeline, "seedvr2")
                self.assertTrue(entry.references, "an upscale run needs an image")
        # The video recipes split across two sub-modules: rebuilding resolution and
        # denoising, which no longer share a card.
        self.assertEqual({entry.kind for entry in image_pipelines.workflows_for_all() if entry.mode == "video"},
                         {"upscale", "denoise"})

    def test_only_the_image_recipes_belong_to_the_generate_view(self):
        modes = {entry.key: entry.mode for entry in image_pipelines.workflows_for_all()}
        self.assertEqual(modes["upscale_balanced"], "image")
        self.assertEqual(modes["video_upscale_balanced"], "video")
        self.assertEqual({key for key, mode in modes.items() if mode == "video"},
                         {"video_upscale_clean", "video_upscale_sharp", "video_upscale_balanced",
                          "video_denoise"})
        for entry in image_pipelines.workflows_for_all():
            if entry.pipeline in {"seedvr2", "seedvr2_video"}:
                self.assertTrue(entry.references, f"{entry.key} starts from an image")

    def test_the_payload_tells_the_panel_to_hide_the_prompt(self):
        monitor = image_jobs.ImageJobMonitor(None, None)
        upscaler = monitor.workflow({"prompt": ""}, "seedvr2")
        self.assertTrue(upscaler["references_required"])
        self.assertIn("summary", upscaler)
        self.assertEqual(upscaler["defaults"]["scale"], 1.5)
        painter = monitor.workflow({"prompt": "cat"}, "sdxl")
        self.assertFalse(painter["references_required"])

    def test_the_request_accepts_a_multiplier_and_rejects_a_silly_one(self):
        self.assertIsNone(image_jobs.ImageGenerateRequest(prompt="").scale)
        self.assertEqual(image_jobs.ImageGenerateRequest(prompt="", scale=1.5).scale, 1.5)
        self.assertIsNone(image_jobs.ImageGenerateRequest(prompt="").denoise,
                          "a recipe that sets no denoise may leave it out or send null")


class ComfyQueueRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """A dead queue entry must be dropped; someone else's live render must not."""

    def test_the_guard_only_claims_death_after_a_long_idle_stretch(self):
        guard = image_jobs.ComfyQueueGuard(idle_seconds=10, utilization=15)
        guard.observe({"utilization": 60}, now=0.0)
        self.assertFalse(guard.looks_dead(now=60.0), "a busy card is never 'dead'")
        guard.observe({"utilization": 4}, now=100.0)
        self.assertFalse(guard.looks_dead(now=105.0))
        self.assertTrue(guard.looks_dead(now=115.0), "10 s of idle with work queued is the verdict")
        guard.observe({"utilization": 40}, now=120.0)
        self.assertFalse(guard.looks_dead(now=200.0), "a new sample resets the verdict")

    def test_unknown_utilisation_never_claims_death(self):
        guard = image_jobs.ComfyQueueGuard(idle_seconds=1)
        guard.observe(None, now=0.0)
        guard.observe({}, now=1.0)
        self.assertFalse(guard.looks_dead(now=100.0))

    def _client(self, running, pending=(), history=(), calls=None):
        async def handler(request):
            if calls is not None:
                calls.append(f"{request.method} {request.url.path}")
            if request.url.path == "/queue" and request.method == "GET":
                return httpx.Response(200, json={
                    "queue_running": [["1", pid] for pid in running["value"]],
                    "queue_pending": [["2", pid] for pid in pending],
                })
            if request.url.path.startswith("/history/"):
                pid = request.url.path.rsplit("/", 1)[-1]
                return httpx.Response(200, json={pid: {}} if pid in history else {})
            if request.url.path == "/queue" and request.method == "POST":
                # The real server only forgets a running entry once it is interrupted.
                return httpx.Response(200, json={})
            if request.url.path == "/interrupt":
                running["value"] = []
                return httpx.Response(200, json={})
            return httpx.Response(404)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_live_work_is_never_touched(self):
        running = {"value": ["someone-else"]}
        calls: list[str] = []
        client = self._client(running, calls=calls)
        try:
            guard = image_jobs.ComfyQueueGuard(idle_seconds=10, utilization=15)
            guard.observe({"utilization": 80}, now=0.0)
            outcome = await image_jobs.clear_dead_comfy_work(client, guard, own_prompt_ids=("ours",))
        finally:
            await client.aclose()
        self.assertEqual(outcome["cleared"], [])
        self.assertIn("GPU 仍在工作", outcome["reason"])
        self.assertNotIn("POST /interrupt", calls)

    async def test_a_finished_entry_is_treated_as_bookkeeping_lag(self):
        running = {"value": ["done-elsewhere"]}
        calls: list[str] = []
        client = self._client(running, history=("done-elsewhere",), calls=calls)
        try:
            guard = image_jobs.ComfyQueueGuard(idle_seconds=1, utilization=15)
            guard.observe({"utilization": 0}, now=0.0)
            outcome = await image_jobs.clear_dead_comfy_work(client, guard, own_prompt_ids=())
        finally:
            await client.aclose()
        self.assertEqual(outcome["cleared"], [])
        self.assertIn("记账滞后", outcome["reason"])
        self.assertNotIn("POST /interrupt", calls)

    async def test_a_dead_entry_is_interrupted_and_deleted(self):
        running = {"value": ["leftover"]}
        calls: list[str] = []
        client = self._client(running, calls=calls)
        try:
            guard = image_jobs.ComfyQueueGuard(idle_seconds=5, utilization=15)
            guard.observe({"utilization": 1}, now=0.0)
            outcome = await image_jobs.clear_dead_comfy_work(
                client, guard, own_prompt_ids=("ours",), supervisor=None)
        finally:
            await client.aclose()
        self.assertEqual(outcome["cleared"], ["leftover"])
        self.assertIn("POST /interrupt", calls)
        self.assertIn("POST /queue", calls)

    async def test_a_stubborn_entry_restarts_comfyui(self):
        running = {"value": ["stuck"]}
        restarts: list[bool] = []

        async def handler(request):
            if request.url.path == "/queue" and request.method == "GET":
                return httpx.Response(200, json={"queue_running": [["1", "stuck"]], "queue_pending": []})
            if request.url.path.startswith("/history/"):
                return httpx.Response(200, json={})
            return httpx.Response(200, json={})

        class Supervisor:
            def restart(self):
                restarts.append(True)
                running["value"] = []
                return True

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            guard = image_jobs.ComfyQueueGuard(idle_seconds=1, utilization=15)
            guard.observe({"utilization": 0}, now=0.0)
            outcome = await image_jobs.clear_dead_comfy_work(
                client, guard, own_prompt_ids=(), supervisor=Supervisor())
        finally:
            await client.aclose()
        self.assertTrue(restarts)
        self.assertTrue(outcome["restarted"])

    async def test_our_own_running_prompt_is_left_alone(self):
        running = {"value": ["ours"]}
        calls: list[str] = []
        client = self._client(running, calls=calls)
        try:
            guard = image_jobs.ComfyQueueGuard(idle_seconds=1, utilization=15)
            guard.observe({"utilization": 0}, now=0.0)
            outcome = await image_jobs.clear_dead_comfy_work(client, guard, own_prompt_ids=("ours",))
        finally:
            await client.aclose()
        self.assertEqual(outcome["cleared"], [])
        self.assertNotIn("POST /interrupt", calls)


@unittest.skip("SDXL/LoRA 预设栈已按用户要求移除")
class WorkflowPresetTests(unittest.IsolatedAsyncioTestCase):
    """One-click recipes: pipeline + LoRAs + parameters in a single call."""

    LORAS = sorted(image_pipelines.PRESET_FILES)

    async def asyncSetUp(self):
        self.previous_monitor = module.image_jobs
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workflow_patch = patch.object(image_comfy, "WORKFLOW_PATH", self.root / "image-workflow.json")
        self.workflow_patch.start()
        # Only the LoRA list is faked: the pipelines must still resolve their real
        # checkpoint, otherwise every recipe would look unavailable.
        real_list_models = image_comfy.list_models
        self.loras_patch = patch.object(
            image_comfy, "list_models",
            side_effect=lambda kind: self.LORAS if kind == "loras" else real_list_models(kind))
        self.loras_patch.start()
        monitor, client = make_monitor(lambda request: httpx.Response(500))
        self.monitor, self.client = monitor, client
        self.monitor.pipeline = "qwen21"
        module.image_jobs = monitor
        module.manager = module.ModeManager()
        module.manager.state = "ready"
        self.api = httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),
                                     base_url="http://127.0.0.1:8081")

    async def asyncTearDown(self):
        await self.api.aclose()
        await self.client.aclose()
        self.loras_patch.stop()
        self.workflow_patch.stop()
        self.temporary.cleanup()
        module.image_jobs = self.previous_monitor

    def test_every_recipe_names_a_registered_pipeline_and_presets(self):
        for entry in image_pipelines.workflows_for_all():
            self.assertIn(entry.pipeline, image_pipelines.PIPELINES, entry.key)
            for preset in entry.presets:
                found = image_pipelines.lora_preset(preset)
                self.assertIsNotNone(found, f"{entry.key}: unknown preset {preset}")
                self.assertIn(entry.pipeline, found.families, f"{entry.key}: {preset} does not fit")
            self.assertIn("steps", entry.params, entry.key)
            self.assertIn("cfg", entry.params, entry.key)
            self.assertTrue(entry.summary, f"{entry.key} needs a description for the panel")
            self.assertLessEqual(len(entry.presets), 2, "only two LoRA slots exist")

    def test_the_recommended_recipe_is_the_registered_default(self):
        entry = image_pipelines.workflow("portrait")
        self.assertEqual(entry.pipeline, image_pipelines.DEFAULT_PIPELINE)
        self.assertEqual(entry.presets, ("lcm", "photoreal"))
        self.assertEqual(image_pipelines.pipeline(entry.pipeline).defaults["steps"], entry.params["steps"])
        self.assertEqual(image_pipelines.pipeline(entry.pipeline).defaults["cfg"], entry.params["cfg"])

    def test_upscale_recipes_pin_a_scale_and_their_pipeline_needs_an_image(self):
        """An upscaler runs from an image, so every recipe must carry a scale."""
        recipes = [entry for entry in image_pipelines.workflows_for_all()
                   if image_pipelines.pipeline(entry.pipeline).references_required]
        self.assertTrue(recipes, "the upscale pipeline should offer at least one recipe")
        for entry in recipes:
            self.assertIn("scale", entry.params, f"{entry.key} needs a scale")
            self.assertGreater(entry.params["scale"], 0, entry.key)
            self.assertNotIn("width", entry.params, f"{entry.key}: an upscaler takes its size from the image")
            self.assertNotIn("height", entry.params, entry.key)

    async def test_upscaling_without_a_reference_is_refused(self):
        """The panel's prompt field cannot stand in for the missing image."""
        monitor, client = make_monitor(lambda request: httpx.Response(200, json={}))
        self.addAsyncCleanup(client.aclose)
        body = image_jobs.ImageGenerateRequest(prompt="", pipeline="seedvr2")
        with self.assertRaises(module.HTTPException) as caught:
            monitor.start(body, "text", [])
        self.assertIn("参考图", str(caught.exception.detail))

    async def test_listing_reports_availability_and_the_active_recipe(self):
        payload = self.monitor.workflows()
        by_key = {entry["key"]: entry for entry in payload}
        self.assertEqual(set(by_key), {entry.key for entry in image_pipelines.workflows_for_all()})
        self.assertTrue(all(entry["available"] for entry in payload if entry["pipeline"] != "chroma"))
        self.assertIn("summary", by_key["portrait"])

    async def test_applying_a_recipe_switches_pipeline_loras_and_parameters(self):
        payload = self.monitor.apply_workflow("draft")
        self.assertEqual(payload["pipeline"], "sdxl")
        self.assertEqual(payload["presets"], ["dmd2"])
        self.assertEqual(payload["params"], {"steps": 4, "cfg": 1.0, "denoise": 1.0,
                                              "width": 832, "height": 1216},
                         "the recipe carries every parameter the composer owns")
        self.assertEqual(self.monitor.pipeline, "sdxl", "the pipeline switch is remembered")
        stored = json.loads((self.root / "image-workflow.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["sdxl"]["11"]["lora_name"], "dmd2_sdxl_4step_lora_fp16.safetensors")
        self.assertNotIn("12", stored["sdxl"], "a recipe without a quality LoRA clears that slot")

    async def test_switching_recipes_replaces_the_previous_loras(self):
        self.monitor.apply_workflow("portrait")
        stored = json.loads((self.root / "image-workflow.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["sdxl"]["12"]["lora_name"], "sdxl_photorealistic_slider_v1-0.safetensors")
        payload = self.monitor.apply_workflow("quality")
        stored = json.loads((self.root / "image-workflow.json").read_text(encoding="utf-8"))
        self.assertEqual(stored, {}, "全量精修 uses no LoRA at all")
        self.assertEqual(payload["params"]["steps"], 28)

    async def test_a_missing_model_blocks_the_switch(self):
        with patch.object(image_comfy, "_choose_model", return_value=None):
            with self.assertRaises(module.HTTPException) as caught:
                self.monitor.apply_workflow("qwen21_real")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("还缺少模型文件", caught.exception.detail)

    async def test_unknown_recipe_is_rejected(self):
        with self.assertRaises(module.HTTPException) as caught:
            self.monitor.apply_workflow("nope")
        self.assertEqual(caught.exception.status_code, 404)

    async def test_the_endpoint_lists_and_applies_recipes(self):
        listed = await self.api.get("/local/image/workflows")
        self.assertEqual(listed.status_code, 200)
        keys = [entry["key"] for entry in listed.json()["workflows"]]
        self.assertIn("portrait", keys)

        applied = await self.api.post("/local/image/workflows", json={"key": "img2img"})
        self.assertEqual(applied.status_code, 200)
        body = applied.json()
        self.assertEqual(body["params"]["denoise"], 0.55)
        self.assertEqual(body["pipeline"], "sdxl")
        self.assertEqual(body["workflow"]["key"], "img2img")
        self.assertEqual(body["workflow_payload"]["overrides"]["11"]["lora_name"], "lcm-lora-sdxl.safetensors")

        missing_key = await self.api.post("/local/image/workflows", json={})
        self.assertEqual(missing_key.status_code, 400)
        unknown = await self.api.post("/local/image/workflows", json={"key": "nope"})
        self.assertEqual(unknown.status_code, 404)

    async def test_the_endpoint_refuses_to_switch_while_generating(self):
        self.monitor.task = asyncio.create_task(asyncio.sleep(30))
        try:
            response = await self.api.post("/local/image/workflows", json={"key": "draft"})
        finally:
            self.monitor.task.cancel()
            self.monitor.task = None
        self.assertEqual(response.status_code, 409)


class PipelineContractTests(unittest.IsolatedAsyncioTestCase):
    """Every pipeline's nodes must exist in ComfyUI, with the inputs we set.

    This is the check that keeps the pipelines whose weights are not on this
    machine honest: Chroma, the fp16 checkpoint and any future pipeline are
    validated against ComfyUI's own ``/object_info`` instead of a live run.
    Skipped when ComfyUI is not running.
    """

    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(timeout=10)
        try:
            response = await self.client.get(f"{image_comfy.COMFY_URL}/object_info")
            response.raise_for_status()
            self.info = response.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            await self.client.aclose()
            self.skipTest(f"ComfyUI is not reachable: {exc}")

    async def asyncTearDown(self):
        await self.client.aclose()

    def test_every_pipeline_node_class_exists(self):
        for key, pipeline in image_pipelines.PIPELINES.items():
            for node in pipeline.nodes:
                self.assertIn(node.class_type, self.info, f"{key}: {node.class_type} is not installed")
            for required in pipeline.requires:
                self.assertIn(required, self.info, f"{key}: required class {required} is missing")

    def test_every_editable_input_is_a_real_input(self):
        for key, pipeline in image_pipelines.PIPELINES.items():
            for node in pipeline.nodes:
                for field in node.fields:
                    self.assertTrue(accepts_input(self.info, node.class_type, field.input),
                                    f"{key}: {node.class_type} has no input {field.input}")

    def test_enum_inputs_use_values_comfyui_offers(self):
        """Loader ``type`` strings (T5 for Chroma, SDXL for the dual CLIP) drift."""
        checked = 0
        for key in image_pipelines.PIPELINES:
            graph = image_comfy.build_graph(key, "a portrait", "", [], 832, 1216, 8, 1.5, 1.0, 7,
                                            "qwen/generated", None)["prompt"]
            for node in graph.values():
                spec = self.info[node["class_type"]]["input"]
                for name, value in node["inputs"].items():
                    if name not in {"type", "weight_dtype"} or not isinstance(value, str):
                        continue
                    options = spec.get("required", {}).get(name) or spec.get("optional", {}).get(name)
                    if not options or not isinstance(options[0], list):
                        continue
                    self.assertIn(value, options[0], f"{key}: {node['class_type']}.{name} = {value!r}")
                    checked += 1
        self.assertGreater(checked, 0, "no loader type/weight_dtype value was checked")

    def test_sampler_and_scheduler_presets_are_offered_by_comfyui(self):
        spec = self.info["KSampler"]["input"]
        samplers = set(spec["required"]["sampler_name"][0])
        schedulers = set(spec["required"]["scheduler"][0])
        for preset in image_pipelines.LORA_PRESETS:
            if preset.params.get("sampler"):
                self.assertIn(preset.params["sampler"], samplers, preset.key)
            if preset.params.get("scheduler"):
                self.assertIn(preset.params["scheduler"], schedulers, preset.key)

    def test_graph_builders_produce_nodes_comfyui_knows(self):
        for key in image_pipelines.PIPELINES:
            graph = image_comfy.build_graph(key, "a portrait", "", [], 832, 1216, 8, 1.5, 1.0, 7,
                                            "qwen/generated", None)["prompt"]
            for node in graph.values():
                self.assertIn(node["class_type"], self.info, f"{key}: {node['class_type']}")
                for name in node["inputs"]:
                    self.assertTrue(accepts_input(self.info, node["class_type"], name),
                                    f"{key}: {node['class_type']} has no input {name}")


class SdxlLoraWiringTests(unittest.TestCase):
    """The two SDXL builders must wire their LoRA slots the same way.

    Both share ``image_comfy._apply_sdxl_loras`` (slot 11 = accelerator, slot 12 =
    quality), so the invariant here is about the chain: an empty slot emits no
    node at all, a filled slot feeds the next one, and whatever comes last feeds
    the sampler and the text encoders.
    """

    PARAMETERS = {"width": 832, "height": 1216, "steps": 8, "cfg": 1.5, "denoise": 1.0, "seed": 7}

    def build(self, builder, loras):
        overrides = {str(slot): values for slot, values in loras.items()}
        with patch.object(image_comfy, "_model_ref", lambda key, name, ov: (f"{key}-{name}.safetensors", True)):
            return builder("a portrait", "", None, dict(self.PARAMETERS), "qwen/generated", overrides)

    def test_no_loras_leaves_the_loaders_untouched(self):
        for builder in (image_comfy._build_sdxl, image_comfy._build_sdxl_gguf):
            graph = self.build(builder, {})
            self.assertNotIn("11", graph)
            self.assertNotIn("12", graph)
            self.assertEqual(graph["4"]["inputs"]["model"], ["10", 0], "the sampler reads the base model")
            # CheckpointLoaderSimple exposes CLIP on slot 1; the GGUF builder has
            # a dedicated DualCLIPLoader on node 13.
            self.assertEqual(graph["1"]["inputs"]["clip"],
                             ["10", 1] if builder is image_comfy._build_sdxl else ["13", 0])

    def test_two_selected_loras_chain_in_order(self):
        loras = {11: {"lora_name": "lcm-lora-sdxl.safetensors", "strength_model": 1.0, "strength_clip": 1.0},
                 12: {"lora_name": "sdxl_photorealistic_slider_v1-0.safetensors",
                      "strength_model": 1.5, "strength_clip": 1.5}}
        for builder in (image_comfy._build_sdxl, image_comfy._build_sdxl_gguf):
            graph = self.build(builder, loras)
            self.assertEqual(graph["11"]["class_type"], "LoraLoader")
            self.assertEqual(graph["12"]["class_type"], "LoraLoader")
            self.assertEqual(graph["11"]["inputs"]["model"], ["10", 0])
            self.assertEqual(graph["12"]["inputs"]["model"], ["11", 0], "slot 12 must chain onto slot 11")
            self.assertEqual(graph["12"]["inputs"]["strength_model"], 1.5)
            self.assertEqual(graph["4"]["inputs"]["model"], ["12", 0], "the sampler reads the last loader")
            self.assertEqual(graph["1"]["inputs"]["clip"], ["12", 1], "the prompt encoder reads the last loader")

    def test_a_disabled_slot_is_skipped_not_emitted(self):
        loras = {11: {"lora_name": "none"}, 12: {"lora_name": "detail.safetensors"}}
        for builder in (image_comfy._build_sdxl, image_comfy._build_sdxl_gguf):
            graph = self.build(builder, loras)
            self.assertNotIn("11", graph, "a 'none' lora_name would make ComfyUI reject the graph")
            self.assertEqual(graph["12"]["inputs"]["model"], ["10", 0])
            self.assertEqual(graph["4"]["inputs"]["model"], ["12", 0])

    def test_one_selected_lora_in_the_first_slot(self):
        loras = {11: {"lora_name": "lcm-lora-sdxl.safetensors"}}
        for builder in (image_comfy._build_sdxl, image_comfy._build_sdxl_gguf):
            graph = self.build(builder, loras)
            self.assertIn("11", graph)
            self.assertNotIn("12", graph)
            self.assertEqual(graph["4"]["inputs"]["model"], ["11", 0])
            self.assertEqual(graph["1"]["inputs"]["clip"], ["11", 1])


class ImageApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous_manager = module.manager
        self.previous_monitor = module.image_jobs
        module.manager = module.ModeManager()
        module.manager.state = "ready"
        module.manager.mode = "text"
        self.requests = []
        self.temporary = tempfile.TemporaryDirectory()
        self.history_patch = patch.object(image_jobs, "HISTORY_PATH",
                                          Path(self.temporary.name) / "image-history.json")
        self.history_patch.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),
                                        base_url="http://127.0.0.1:8081")

    async def asyncTearDown(self):
        if module.image_jobs and module.image_jobs.busy:
            module.image_jobs.task.cancel()
        if module.manager.client:
            await module.manager.client.aclose()
        await self.client.aclose()
        module.manager = self.previous_manager
        module.image_jobs = self.previous_monitor
        self.history_patch.stop()
        self.temporary.cleanup()

    def install(self, handler, loader=None):
        monitor, client = make_monitor(handler, loader)
        # Never spawn a real ComfyUI or call nvidia-smi from a unit test.
        monitor.supervisor.ensure_running = AsyncMock(return_value={"reachable": True, "device": "test-gpu"})
        monitor.supervisor.status = AsyncMock(return_value={"reachable": False, "managed": False, "error": None})
        monitor.gpu.sample = AsyncMock(return_value=None)
        monitor.gpu.processes = AsyncMock(return_value=[])
        module.manager.client = client
        monitor.client = client
        module.image_jobs = monitor
        return monitor

    def generation_handler(self, queue=None, history=None):
        async def handler(request):
            self.requests.append(str(request.url))
            path = request.url.path
            if path == "/v1/chat/completions":
                return httpx.Response(200, json={"choices": [{"message": {"content": "final prompt"}}]})
            if path == "/queue" and request.method == "GET":
                return httpx.Response(200, json=queue or {"queue_running": [], "queue_pending": []})
            if path.startswith("/history/"):
                prompt_id = path.rsplit("/", 1)[-1]
                # Completed by default so a job reaches its terminal state; a
                # test that needs "still running" passes its own history body.
                body = history if history is not None else {"status": {"completed": True}, "outputs": {}}
                return httpx.Response(200, json={prompt_id: body})
            if path == "/prompt":
                return httpx.Response(200, json={"prompt_id": "prompt-1"})
            raise AssertionError(f"unexpected request {request.method} {path}")
        return handler

    async def submit(self, **overrides):
        body = {"prompt": "a cat", "steps": 4, "enhance_prompt": True} | overrides
        return await self.client.post("/local/image/generate", json=body)

    async def test_submit_returns_a_job_and_keeps_the_cycle_off_the_request(self):
        self.install(self.generation_handler())
        response = await self.submit()
        self.assertEqual(response.status_code, 202)
        job = response.json()["job"]
        self.assertTrue(job["id"].startswith("img-"))
        self.assertEqual(job["prompt"], "a cat")
        self.assertEqual(job["params"]["steps"], 4)
        self.assertIn(job["phase"], {"queued", "enhancing", "loading_model"})
        # The HTTP request must not have waited for the generation cycle.
        self.assertNotIn("http://127.0.0.1:8188/prompt", self.requests)
        module.image_jobs.task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await module.image_jobs.task

    async def test_second_submission_waits_instead_of_being_rejected(self):
        """一次只能画一张，但第二次必须被收下排队，而不是回 409。

        实测（真机访客）：一轮 60-80 秒里有 36-46 秒只是把语言模型装回来，图片已经在屏幕上；
        这段窗口里以前的实现一律 409，访客连点几次就读成"卡死"。
        """
        loader = AsyncMock()
        self.install(self.generation_handler(), loader)
        module.image_jobs.task = asyncio.get_running_loop().create_future()
        response = await self.submit()
        self.assertEqual(response.status_code, 202)
        waiting = response.json()["job"]
        self.assertEqual(waiting["state"], "queued_behind")
        self.assertEqual(waiting["queue_ahead"], 1)
        self.assertIsNotNone(module.image_jobs.pending)
        # 排队位只有一格：第三张必须被拒，否则队列会无上限地涨。
        again = await self.submit()
        self.assertEqual(again.status_code, 409)
        self.assertIn("排在队列里", again.json()["detail"])
        module.image_jobs.pending = None
        module.image_jobs.task.cancel()

    async def test_queued_job_starts_when_the_running_one_settles(self):
        """排队位里的任务要在前一张收尾后自己开工，并且能被它的提交者看到。"""
        loader = AsyncMock()
        monitor = self.install(self.generation_handler(), loader)
        module.image_jobs.task = asyncio.get_running_loop().create_future()
        await self.submit()
        queued = image_jobs.ImageJob("img-waiting", "a dog", "", {"steps": 4, "pipeline": "sdxl"}, [])
        queued.state = "queued_behind"
        queued.session = "guest-token"
        module.image_jobs.pending = {"job": queued, "body": image_jobs.ImageGenerateRequest(prompt="a dog"),
                                     "previous_mode": "text"}
        self.assertEqual(monitor.pending_for("guest-token", False)["id"], "img-waiting")
        self.assertIsNone(monitor.pending_for("someone-else", False))
        monitor._start_pending()
        self.assertIs(module.image_jobs.job, queued)
        self.assertEqual(queued.state, "running")
        self.assertIsNone(module.image_jobs.pending)
        module.image_jobs.task.cancel()

    async def test_waiting_job_is_what_its_own_caller_sees(self):
        """访客的进度里要看得到自己那张排队的任务，而不是机主正在画的那张。"""
        self.install(self.generation_handler())
        running = image_jobs.ImageJob("img-owner", "owner words", "", {"steps": 4}, [])
        module.image_jobs.job = running
        module.image_jobs.task = asyncio.get_running_loop().create_future()
        queued = image_jobs.ImageJob("img-mine", "my words", "", {"steps": 4}, [])
        queued.state = "queued_behind"
        queued.session = "guest-token"
        module.image_jobs.pending = {"job": queued, "body": image_jobs.ImageGenerateRequest(prompt="my words"),
                                     "previous_mode": "text"}
        response = await self.client.get("/local/image/progress", headers={"cookie": "qwen_access=guest-token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["job"]["id"], "img-mine")
        module.image_jobs.pending = None
        module.image_jobs.task.cancel()

    async def test_active_request_blocks_submission(self):
        self.install(self.generation_handler())
        module.manager.active_requests = 1
        response = await self.submit()
        self.assertEqual(response.status_code, 409)

    async def test_progress_reports_job_queue_and_events(self):
        monitor = self.install(self.generation_handler())
        module.image_jobs.task = asyncio.get_running_loop().create_future()
        job = image_jobs.ImageJob("img-live", "a cat", "", {"steps": 8}, [])
        job.phase = "queued"
        job.queue_position = 2
        job.queue_pending = 4
        job.prompt_id = "prompt-1"
        job.note("queued")
        monitor.job = job
        response = await self.client.get("/local/image/progress")
        payload = response.json()
        self.assertTrue(payload["busy"])
        self.assertEqual(payload["job"]["queue_position"], 2)
        self.assertEqual(payload["job"]["queue_pending"], 4)
        self.assertEqual(payload["job"]["max_step"], 8)
        self.assertEqual([event["key"] for event in payload["events"]], ["queued"])
        self.assertEqual(payload["model"]["pipeline"], image_pipelines.DEFAULT_PIPELINE)
        module.image_jobs.task.cancel()

    async def test_preview_is_sent_once_per_version(self):
        """采样预览只发给"要看图且版本对不上"的调用方。

        实测：这个接口每次回 85,745 字节，其中预览图 79,157 字节，而面板忙时 0.8 秒拉一次、
        空闲 1.5 秒一次——一张图要往手机上灌 6-8 MB。默认（不带参数）就是"我不要图"，
        悬浮任务窗和任何普通轮询都走这条路；面板带着自己已有的版本号来问，也不会再拿一遍。
        """
        monitor = self.install(self.generation_handler())
        module.image_jobs.task = asyncio.get_running_loop().create_future()
        job = image_jobs.ImageJob("img-live", "a cat", "", {"steps": 4}, [])
        job.preview = "data:image/png;base64,AAAA"
        job.preview_version = 7
        monitor.job = job

        default = (await self.client.get("/local/image/progress")).json()["job"]
        self.assertIsNone(default["preview"], "默认不该把 79 KB 的图发过来")
        self.assertEqual(default["preview_version"], 7)

        fresh = (await self.client.get("/local/image/progress?preview=0")).json()["job"]
        self.assertEqual(fresh["preview"], job.preview, "没见过的版本必须发过来")

        same = (await self.client.get("/local/image/progress?preview=7")).json()["job"]
        self.assertIsNone(same["preview"])
        self.assertEqual(same["preview_version"], 7)

        off = (await self.client.get("/local/image/progress?preview=-1")).json()["job"]
        self.assertIsNone(off["preview"], "-1 表示调用方不需要图片")
        module.image_jobs.task.cancel()

    async def test_status_reports_switching_while_a_job_owns_the_backend(self):
        monitor = self.install(self.generation_handler())
        module.image_jobs.task = asyncio.get_running_loop().create_future()
        monitor.job = image_jobs.ImageJob("img-live", "a cat", "", {"steps": 4}, [])
        self.assertEqual(module.manager.status()["state"], "switching")
        self.assertEqual(module.manager.status()["image"]["job_id"], "img-live")
        module.image_jobs.task.cancel()
        self.assertEqual(module.manager.status()["state"], "ready")

    async def test_a_stalled_job_stops_claiming_the_backend(self):
        monitor = self.install(self.generation_handler())
        module.image_jobs.task = asyncio.create_task(asyncio.sleep(60))
        job = image_jobs.ImageJob("img-stalled", "a cat", "", {"steps": 4}, [])
        job.phase = "sampling"
        job.updated = job.started
        monitor.job = job
        self.assertTrue(monitor.progress()["busy"])
        self.assertEqual(module.manager.status()["state"], "switching")
        with patch.object(image_jobs.time, "time", return_value=job.started + image_jobs.STALL_SECONDS + 1):
            progress = monitor.progress()
            self.assertFalse(progress["busy"], "a stalled job must not keep the backend busy")
            self.assertTrue(progress["monitor_busy"], "the task state stays visible for diagnostics")
            self.assertTrue(progress["stalled"])
            self.assertTrue(progress["job"]["stalled"])
            self.assertEqual(module.manager.status()["state"], "ready", "mode buttons unblock after a stall")
        module.image_jobs.task.cancel()

    async def test_cancel_interrupts_comfy_and_clears_the_slot(self):
        async def handler(request):
            self.requests.append(f"{request.method} {request.url.path}")
            return httpx.Response(200, json={})
        monitor = self.install(handler)
        module.image_jobs.task = asyncio.create_task(asyncio.sleep(60))
        monitor.job = image_jobs.ImageJob("img-live", "a cat", "", {"steps": 4}, [])
        monitor.job.prompt_id = "prompt-1"
        response = await self.client.post("/local/image/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["job"]["state"], "cancelled")
        self.assertIn("POST /interrupt", self.requests)
        self.assertIn("POST /queue", self.requests)
        self.assertFalse(monitor.busy)

    async def test_cancel_without_a_job_is_rejected(self):
        self.install(self.generation_handler())
        self.assertEqual((await self.client.post("/local/image/cancel")).status_code, 409)

    async def test_history_round_trip_and_clear(self):
        monitor = self.install(self.generation_handler())
        job = image_jobs.ImageJob("img-done", "a cat", "", {"steps": 4, "seed": 7}, [])
        job.state = "completed"
        job.finished = job.created + 12
        job.timings = {"sampling": 8.0, "total": 12.0}
        await monitor._persist(job)
        listed = (await self.client.get("/local/image/history")).json()["entries"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["params"]["seed"], 7)
        self.assertEqual(listed[0]["duration"]["sampling"], 8.0)
        removed = await self.client.delete("/local/image/history/img-done")
        self.assertEqual(removed.status_code, 200)
        self.assertEqual((await self.client.get("/local/image/history")).json()["entries"], [])
        self.assertEqual((await self.client.delete("/local/image/history/img-done")).status_code, 404)

    async def test_history_survives_a_reload_and_is_capped(self):
        monitor = self.install(self.generation_handler())
        for index in range(image_jobs.HISTORY_LIMIT + 3):
            job = image_jobs.ImageJob(f"img-{index}", f"prompt {index}", "", {"steps": 4}, [])
            job.state = "completed"
            await monitor._persist(job)
        stored = json.loads(image_jobs.HISTORY_PATH.read_text(encoding="utf-8"))
        self.assertEqual(len(stored), image_jobs.HISTORY_LIMIT)
        self.assertEqual(stored[0]["prompt"], f"prompt {image_jobs.HISTORY_LIMIT + 2}")
        reloaded = image_jobs.ImageJobMonitor(monitor.client, AsyncMock())
        self.assertEqual(len(reloaded.history), image_jobs.HISTORY_LIMIT)

    async def test_clear_history_reports_the_removed_count(self):
        monitor = self.install(self.generation_handler())
        job = image_jobs.ImageJob("img-done", "a cat", "", {"steps": 4}, [])
        await monitor._persist(job)
        response = await self.client.delete("/local/image/history")
        self.assertEqual(response.json()["removed"], 1)
        self.assertEqual(json.loads(image_jobs.HISTORY_PATH.read_text(encoding="utf-8")), [])

    async def test_the_denoise_module_takes_a_still_and_writes_a_denoised_copy(self):
        """The image side's denoise module: one posted picture in, one file out, no graph."""
        import base64

        import cv2
        import numpy as np

        self.install(self.generation_handler())
        noise = np.random.default_rng(7).integers(0, 255, (48, 48), dtype=np.uint8)
        encoded, buffer = cv2.imencode(".png", noise)
        self.assertTrue(encoded)
        payload = "data:image/png;base64," + base64.b64encode(buffer.tobytes()).decode("ascii")
        with patch.object(image_comfy, "COMFY_ROOT", Path(self.temporary.name)):
            response = await self.client.post("/local/image/denoise",
                                              json={"image": payload, "strength": 2.0})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("nlmeans s=2.00", body["note"])
        self.assertIn("denoised", body["image"])
        target = Path(body["target"])
        self.assertTrue(target.is_file(), "the denoised still must exist")
        self.assertEqual(target.parent.name, "denoised")
        # No ComfyUI call at all: the module is ffmpeg on the CPU.
        self.assertEqual(self.requests, [])

    async def test_the_denoise_module_refuses_anything_that_is_not_a_picture(self):
        self.install(self.generation_handler())
        for image in ("data:text/plain;base64,QQ==", "", "data:image/png;base64,not-base64!"):
            response = await self.client.post("/local/image/denoise", json={"image": image})
            self.assertEqual(response.status_code, 400, image[:32])

    async def test_meta_reports_pipelines_models_and_comfyui_state(self):
        async def handler(request):
            if request.url.path == "/queue":
                return httpx.Response(200, json={"queue_running": [["1", "a"]], "queue_pending": [["2", "b"]]})
            raise AssertionError(request.url)
        self.install(handler)
        payload = (await self.client.get("/local/image/meta")).json()
        self.assertEqual({key: payload["comfy"][key] for key in ("reachable", "running", "pending")},
                         {"reachable": True, "running": 1, "pending": 1})
        self.assertIn("managed", payload["comfy"], "the panel knows whether the gateway owns the server")
        self.assertEqual([pipeline["key"] for pipeline in payload["pipelines"]],
                         list(image_pipelines.PIPELINES), "every registered pipeline is offered to the panel")
        self.assertTrue(all("ready" in pipeline for pipeline in payload["pipelines"]),
                        "the panel is told whether each pipeline can run yet")
        self.assertIn("checkpoints", payload["models"], "model files come from the local folders")
        self.assertEqual(payload["current"]["pipeline"], image_pipelines.DEFAULT_PIPELINE)
        # Named from the selection, not a fixed string: with the 9B selected the panel used to
        # claim the 27B had optimised the prompt.
        self.assertEqual(payload["current"]["enhancer"],
                         qwen_config.active_profile_label() + " · 本地")

    async def test_meta_reports_comfyui_unreachable_without_failing(self):
        def handler(request):
            raise httpx.ConnectError("refused", request=request)
        self.install(handler)
        payload = (await self.client.get("/local/image/meta")).json()
        self.assertFalse(payload["comfy"]["reachable"])
        self.assertEqual(payload["models"]["checkpoints"], [n for n in payload["models"]["checkpoints"] if n])

    async def test_reference_data_urls_are_uploaded_before_queueing(self):
        async def handler(request):
            self.requests.append(f"{request.method} {request.url.path}")
            if request.url.path == "/queue":
                return httpx.Response(200, json={"queue_running": [], "queue_pending": []})
            if request.url.path == "/upload/image":
                return httpx.Response(200, json={"name": "uploaded.png"})
            if request.url.path == "/v1/chat/completions":
                return httpx.Response(200, json={"choices": [{"message": {"content": "p"}}]})
            if request.url.path.startswith("/history/"):
                return httpx.Response(200, json={"prompt-1": {"status": {"completed": True}, "outputs": {}}})
            if request.url.path == "/prompt":
                return httpx.Response(200, json={"prompt_id": "prompt-1"})
            raise AssertionError(request.url)
        monitor = self.install(handler)
        with patch.object(monitor, "_listen", new=AsyncMock()):
            response = await self.submit(prompt="cat", image_1="data:image/png;base64,aGk=", steps=1)
            self.assertEqual(response.status_code, 202)
            await monitor.task
        self.assertIn("POST /upload/image", self.requests)
        self.assertIn("POST /prompt", self.requests)

    async def test_comfyui_is_started_on_demand_before_the_model_swap(self):
        monitor = self.install(self.generation_handler())
        starts = []

        async def ensure(on_note=None):
            starts.append(True)
            if on_note:
                on_note("starting_comfy")
            return {"reachable": True, "device": "test-gpu"}

        monitor.supervisor.ensure_running = AsyncMock(side_effect=ensure)
        loader = AsyncMock()
        monitor.mode_loader = loader
        response = await self.submit(prompt="cat", enhance_prompt=False)
        self.assertEqual(response.status_code, 202)
        await monitor.task
        self.assertTrue(starts, "ComfyUI is started on demand")
        self.assertEqual(monitor.snapshot()["timings"].get("comfy_start") is not None, True,
                         "the start-up phase is timed for the monitoring panel")
        phases = [event["key"] for event in monitor.snapshot()["events"]]
        self.assertLess(phases.index("starting_comfy"), phases.index("loading_model"),
                        "ComfyUI starts before the language model is unloaded")

    async def test_autostart_disabled_reports_an_actionable_error(self):
        monitor = self.install(self.generation_handler())
        # Use the real guard: install() stubs ensure_running for hermeticity.
        monitor.supervisor.ensure_running = image_comfy.ComfySupervisor.ensure_running.__get__(monitor.supervisor)
        monitor.supervisor.autostart = False
        monitor.supervisor.status = AsyncMock(return_value={"reachable": False, "managed": False, "error": None})
        spawns = []
        monitor.supervisor.spawn = lambda: spawns.append(True)
        response = await self.submit(prompt="cat")
        self.assertEqual(response.status_code, 202)
        await monitor.task
        self.assertEqual(spawns, [], "autostart disabled must not spawn a server")
        self.assertIn("自动启动已在 settings.json 中关闭", monitor.snapshot()["error"])

    async def test_the_prompt_optimiser_loads_the_selected_model(self):
        """提示词优化跟着当前模型：先把它加载起来，再让它写提示词。

        The old code called the language-model port directly and only reloaded it when
        ``previous_mode == "image"`` — a value the route can never produce (it maps everything
        that is not text/vision to "text"). From the 生图 view the language model has been
        released to ComfyUI, so that call reached a closed port and the job quietly used the raw
        prompt. Loading through the gateway's own loader is what makes it the *selected* model.
        """
        monitor = self.install(self.generation_handler())
        loader = AsyncMock()
        monitor.mode_loader = loader
        response = await self.submit(prompt="a cat")
        self.assertEqual(response.status_code, 202)
        await monitor.task
        self.assertIn(("text",), [call.args for call in loader.await_args_list],
                      "the current model must be loaded for the prompt work")
        snapshot = monitor.snapshot()
        self.assertTrue(snapshot["enhanced"])
        self.assertEqual(snapshot["final_prompt"], "final prompt")
        self.assertTrue(snapshot["enhancer"]["used"])
        self.assertEqual(snapshot["enhancer"]["label"], qwen_config.active_profile_label())

    async def test_turning_the_optimiser_off_never_touches_the_model(self):
        monitor = self.install(self.generation_handler())
        response = await self.submit(prompt="a cat", enhance_prompt=False)
        self.assertEqual(response.status_code, 202)
        await monitor.task
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot["final_prompt"], "a cat")
        self.assertEqual(snapshot["enhancer"]["used"], False)
        self.assertIn("已关闭", snapshot["enhancer"]["reason"])
        self.assertFalse(any(url.endswith("/v1/chat/completions") for url in self.requests))

    async def test_a_failed_optimisation_still_runs_the_generation(self):
        def handler(request):
            self.requests.append(str(request.url))
            path = request.url.path
            if path == "/v1/chat/completions":
                return httpx.Response(503, json={"error": "model not loaded"})
            if path == "/queue" and request.method == "GET":
                return httpx.Response(200, json={"queue_running": [], "queue_pending": []})
            if path.startswith("/history/"):
                return httpx.Response(200, json={path.rsplit("/", 1)[-1]:
                                                 {"status": {"completed": True}, "outputs": {}}})
            if path == "/prompt":
                return httpx.Response(200, json={"prompt_id": "prompt-1"})
            raise AssertionError(f"unexpected request {request.method} {path}")

        monitor = self.install(handler)
        await self.submit(prompt="a cat")
        await monitor.task
        snapshot = monitor.snapshot()
        # Fail-open, exactly as before: the owner's own words are used and the job still runs.
        self.assertEqual(snapshot["final_prompt"], "a cat")
        self.assertEqual(snapshot["enhancer"]["used"], False)
        self.assertNotEqual(snapshot["state"], "error")

    async def test_comfyui_start_failure_keeps_the_language_model_loaded(self):
        monitor = self.install(self.generation_handler())
        monitor.supervisor.ensure_running = AsyncMock(side_effect=RuntimeError("ComfyUI 进程已退出（代码 1）"))
        loader = AsyncMock()
        monitor.mode_loader = loader
        await self.submit(prompt="cat")
        await monitor.task
        # Only the restore call happens; the model is never swapped for a queue
        # that cannot accept work.
        loader.assert_awaited_once_with("text")
        self.assertIn("ComfyUI 进程已退出", monitor.snapshot()["error"])

    async def test_workflow_overrides_are_validated_and_applied(self):
        monitor = self.install(self.generation_handler())
        response = await self.client.put("/local/image/workflow", json={
            "pipeline": "qwen21",
            "overrides": {"6": {"steps": "12", "sampler_name": "euler_ancestral"},
                          "5": {"width": "1536"},
                          "99": {"nope": 1}, "1": {"unet_name": "  custom.safetensors  "}},
            "params": {"width": 768, "height": 512, "steps": 4, "cfg": 1, "seed": 7},
        })
        self.assertEqual(response.status_code, 200)
        stored = response.json()["overrides"]
        self.assertEqual(stored["6"], {"sampler_name": "euler_ancestral"})
        self.assertEqual(stored["1"], {"unet_name": "custom.safetensors"})
        self.assertNotIn("steps", stored["6"], "the step count belongs to the request parameters")
        self.assertNotIn("5", stored, "the canvas size belongs to the request parameters")
        self.assertNotIn("99", stored, "unknown nodes are ignored")
        graph = {node["id"]: node["inputs"] for node in response.json()["workflow"]["graph"]}
        self.assertEqual(graph["6"]["steps"], 4, "the request step count is used, not a stale edit")
        self.assertEqual(graph["5"]["width"], 768, "the request width is used")
        self.assertEqual(graph["6"]["sampler_name"], "euler_ancestral", "a sampler edit still applies")
        self.assertEqual(graph["1"]["unet_name"], "custom.safetensors", "the saved model file is applied")
        image_comfy.save_overrides({})

    async def test_a_persisted_linked_parameter_cannot_override_the_request(self):
        # A flat file written by an older version still holds width/steps/seed.
        # It migrates to the default pipeline (SDXL), whose sampler node is 4.
        image_comfy.WORKFLOW_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Per-pipeline map (what save_pipeline_overrides writes now).
        image_comfy.WORKFLOW_PATH.write_text(json.dumps({
            "qwen21": {
                "5": {"width": 1536, "height": 1536},
                "6": {"seed": 342949938, "steps": 20, "sampler_name": "euler_ancestral"},
                "1": {"unet_name": "stale.safetensors"},
            }
        }), encoding="utf-8")
        monitor = self.install(self.generation_handler())
        workflow = (await self.client.get("/local/image/workflow?pipeline=qwen21&width=768&height=512&steps=4&seed=11")).json()
        graph = {node["id"]: node["inputs"] for node in workflow["graph"]}
        self.assertEqual(graph["5"]["width"], 768, "the request width wins over the stale file")
        self.assertEqual(graph["6"]["seed"], 11, "the freshly rolled seed is used")
        self.assertEqual(graph["6"]["steps"], 4)
        self.assertEqual(graph["6"]["sampler_name"], "euler_ancestral", "non-linked edits still apply")
        self.assertEqual(graph["1"]["unet_name"], "stale.safetensors", "a saved model file still applies")
        self.assertNotIn("seed", workflow["overrides"].get("6", {}),
                         "the stale linked keys are not reported as edits")
        # The unified run path uses the same resolution.
        module.image_jobs.task = asyncio.get_running_loop().create_future()
        job = image_jobs.ImageJob("img-live", "a cat", "", {"steps": 4}, [])
        monitor.job = job
        self.assertEqual(job.params.get("width"), None)
        module.image_jobs.task.cancel()
        image_comfy.save_overrides({})

    async def test_generation_uses_the_request_seed_not_a_stale_override(self):
        image_comfy.WORKFLOW_PATH.parent.mkdir(parents=True, exist_ok=True)
        image_comfy.WORKFLOW_PATH.write_text(json.dumps({"6": {"seed": 424242}}), encoding="utf-8")
        monitor = self.install(self.generation_handler())
        with patch.object(monitor, "_listen", new=AsyncMock()):
            response = await self.submit(prompt="cat", pipeline="qwen21", seed=987654, steps=2, enhance_prompt=False)
            self.assertEqual(response.status_code, 202)
            await monitor.task
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot["params"]["seed"], 987654, "the submitted seed is what runs")
        graph = {node["id"]: node["inputs"] for node in snapshot["graph"]}
        self.assertEqual(graph["6"]["seed"], 987654)
        image_comfy.save_overrides({})

    async def test_invalid_workflow_override_is_rejected(self):
        self.install(self.generation_handler())
        # Linked parameters are ignored rather than rejected; invalid values for
        # a real editable node still fail loudly with the field name.
        response = await self.client.put("/local/image/workflow", json={"overrides": {"6": {"steps": "abc"}}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["overrides"], {})
        response = await self.client.put("/local/image/workflow", json={"overrides": {"1": {"unet_name": "   "}}})
        self.assertEqual(response.status_code, 400)
        self.assertIn("模型文件", response.json()["detail"])
        response = await self.client.put("/local/image/workflow", json={"overrides": "nope"})
        self.assertEqual(response.status_code, 400)

    async def test_workflow_can_be_reset_to_defaults(self):
        self.install(self.generation_handler())
        await self.client.put("/local/image/workflow", json={"overrides": {"1": {"unet_name": "custom.safetensors"}}})
        reset = await self.client.delete("/local/image/workflow")
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(reset.json()["overrides"], {})
        model_node = [node for node in reset.json()["workflow"]["graph"] if node["id"] == "1"][0]
        expected = default_model(image_pipelines.DEFAULT_PIPELINE, "unet_name")
        self.assertEqual(model_node["inputs"]["unet_name"], expected,
                         "the default model is used again after the reset")

    async def test_incomplete_pipeline_fails_before_queueing(self):
        """A pipeline without its files must not reach ComfyUI at all."""
        monitor = self.install(self.generation_handler())
        # Pretend every candidate file for this pipeline is absent, so the test
        # does not depend on what happens to be downloaded on this machine.
        with patch.object(image_comfy, "_choose_model", return_value=None):
            previous, module.image_jobs.pipeline = module.image_jobs.pipeline, "qwen21"
            try:
                response = await self.submit(prompt="cat", enhance_prompt=False)
                self.assertEqual(response.status_code, 202)
                await monitor.task
            finally:
                module.image_jobs.pipeline = previous
        error = monitor.snapshot()["error"]
        self.assertIn("还缺少模型文件", error)
        self.assertIn("diffusion_models", error)
        self.assertEqual(monitor.snapshot()["state"], "error")
        self.assertNotIn("http://127.0.0.1:8188/prompt", self.requests, "nothing was queued")

    async def test_workflow_preview_follows_the_request_parameters(self):
        self.install(self.generation_handler())
        payload = (await self.client.get(
            "/local/image/workflow?pipeline=qwen21&width=768&height=512&steps=6&cfg=1.5&seed=99")).json()
        graph = {node["id"]: node["inputs"] for node in payload["graph"]}
        self.assertEqual(graph["5"]["width"], 768)
        self.assertEqual(graph["6"]["steps"], 6)
        self.assertEqual(graph["6"]["cfg"], 1.5)
        self.assertEqual(graph["6"]["seed"], 99)
        self.assertEqual(payload["sync"]["width"], {"node": "5", "input": "width", "title": "画布尺寸", "label": "宽度"})

    async def test_workflow_template_shows_the_values_that_will_run(self):
        self.install(self.generation_handler())
        payload = (await self.client.get("/local/image/workflow?pipeline=qwen21&width=768&steps=6&seed=99")).json()
        values = {(node["id"], field["input"]): field["value"] for node in payload["template"] for field in node["fields"]}
        self.assertEqual(values[("1", "unet_name")], default_model("qwen21", "unet_name"), "the editor shows the model that will load")
        self.assertEqual(values[("2", "clip_name")], default_model("qwen21", "clip_name"))
        self.assertEqual(values[("3", "vae_name")], default_model("qwen21", "vae_name"))
        self.assertEqual(values[("6", "sampler_name")], "euler")
        self.assertEqual(values[("8", "filename_prefix")], "qwen/generated")
        self.assertEqual(values[("5", "width")], 768, "linked fields show the request values")
        self.assertEqual(values[("6", "steps")], 6)
        self.assertEqual(values[("6", "seed")], 99)

    async def test_gpu_monitor_reports_totals_and_the_job_footprint(self):
        monitor = self.install(self.generation_handler())
        samples = [
            {"total_mib": 12227, "used_mib": 1000, "free_mib": 11227, "utilization": 5},
            {"total_mib": 12227, "used_mib": 9600, "free_mib": 2627, "utilization": 88},
            {"total_mib": 12227, "used_mib": 4100, "free_mib": 8127, "utilization": 20},
        ]
        # Repeat the last reading if the endpoint samples more often than the list.
        monitor.gpu.sample = AsyncMock(side_effect=lambda: samples.pop(0) if len(samples) > 1 else samples[0])
        monitor.gpu.begin(await monitor.gpu.sample())
        monitor.gpu.observe(await monitor.gpu.sample())
        monitor.gpu.observe(await monitor.gpu.sample())
        snapshot = monitor.gpu.snapshot()
        self.assertEqual(snapshot["total_mib"], 12227)
        self.assertEqual(snapshot["used_mib"], 4100, "the latest reading is current")
        self.assertEqual(snapshot["job_peak_mib"], 8600, "the peak above the baseline is the job footprint")
        response = await self.client.get("/local/image/gpu")
        self.assertTrue(response.json()["available"])
        self.assertEqual(response.json()["gpu"]["job_mib"], 3100)

    async def test_gpu_monitor_degrades_without_nvidia_smi(self):
        monitor = self.install(self.generation_handler())
        monitor.gpu.sample = AsyncMock(return_value=None)
        response = await self.client.get("/local/image/gpu")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["available"])
        self.assertIsNone(response.json()["gpu"])

    def test_gpu_monitor_keeps_the_measured_peak_after_the_models_are_freed(self):
        monitor = image_jobs.ImageJobMonitor(None, None)
        monitor.gpu.begin({"total_mib": 12227, "used_mib": 10000, "free_mib": 2227, "utilization": 5})
        monitor.gpu.release({"total_mib": 12227, "used_mib": 400, "free_mib": 11827, "utilization": 0})
        monitor.gpu.observe({"total_mib": 12227, "used_mib": 10500, "free_mib": 1727, "utilization": 90})
        live = monitor.gpu.snapshot()
        self.assertEqual(live["image_mib"], 10100, "the image job's own allocation is measured after the swap")
        self.assertEqual(live["image_peak_mib"], 10100)
        # Once the job ends, ComfyUI frees its models; the report must keep the peak.
        monitor.gpu.freeze({"total_mib": 12227, "used_mib": 300, "free_mib": 11927, "utilization": 0})
        final = monitor.gpu.snapshot()
        self.assertEqual(final["used_mib"], 300, "the live reading still updates")
        self.assertEqual(final["image_peak_mib"], 10200, "the measured peak survives the cleanup")
        self.assertEqual(final["job_peak_mib"], 500, "the whole-job impact is also kept")

    async def test_job_snapshot_carries_the_graph_and_vram(self):
        monitor = self.install(self.generation_handler())
        module.image_jobs.task = asyncio.get_running_loop().create_future()
        job = image_jobs.ImageJob("img-live", "a cat", "", {"steps": 4}, [])
        job.graph = [{"id": "6", "class_type": "KSampler", "title": "步数", "inputs": {"steps": 25}}]
        job.gpu = {"total_mib": 12227, "used_mib": 9000, "job_mib": 8000, "job_peak_mib": 8600}
        monitor.job = job
        payload = (await self.client.get("/local/image/progress")).json()
        self.assertEqual(payload["job"]["graph"][0]["id"], "6", "the panel receives the running workflow")
        self.assertEqual(payload["job"]["gpu"]["job_mib"], 8000)
        module.image_jobs.task.cancel()

    async def test_connection_loss_after_queueing_reports_a_clear_error(self):
        async def handler(request):
            if request.url.path == "/queue":
                return httpx.Response(200, json={"queue_running": [], "queue_pending": []})
            if request.url.path == "/v1/chat/completions":
                return httpx.Response(200, json={"choices": [{"message": {"content": "p"}}]})
            raise httpx.ConnectError("refused", request=request)
        monitor = self.install(handler, AsyncMock())
        await self.submit(prompt="cat")
        await monitor.task
        self.assertIn("ComfyUI", monitor.snapshot()["error"])
        self.assertIn("连接中断", monitor.snapshot()["error"])

    async def test_bad_reference_shape_is_rejected(self):
        self.install(self.generation_handler())
        response = await self.submit(image_1="../../etc/passwd")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(module.image_jobs.busy)


@unittest.skip("SDXL/LoRA 预设栈已按用户要求移除")
class LoraPresetTests(unittest.IsolatedAsyncioTestCase):
    """One-click LoRAs: accelerators set the sampler, quality LoRAs stack."""

    LORAS = sorted(image_pipelines.PRESET_FILES)

    async def asyncSetUp(self):
        self.previous_monitor = module.image_jobs
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workflow_patch = patch.object(image_comfy, "WORKFLOW_PATH", self.root / "image-workflow.json")
        self.workflow_patch.start()
        self.loras_patch = patch.object(image_comfy, "list_models", side_effect=self.models)
        self.loras_patch.start()
        monitor, client = make_monitor(lambda request: httpx.Response(500))
        self.monitor, self.client = monitor, client
        self.monitor.pipeline = "sdxl"
        module.image_jobs = monitor
        module.manager = module.ModeManager()
        module.manager.state = "ready"
        self.api = httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),
                                     base_url="http://127.0.0.1:8081")

    async def asyncTearDown(self):
        await self.api.aclose()
        await self.client.aclose()
        self.loras_patch.stop()
        self.workflow_patch.stop()
        self.temporary.cleanup()
        module.image_jobs = self.previous_monitor

    def models(self, kind):
        return self.LORAS if kind == "loras" else []

    def test_every_pipeline_offers_the_presets_it_can_run(self):
        self.assertEqual(len(image_pipelines.presets_for("sdxl")), len(image_pipelines.LORA_PRESETS))
        self.assertEqual([preset.key for preset in image_pipelines.presets_for("sdxl")],
                         [preset.key for preset in image_pipelines.LORA_PRESETS])
        self.assertEqual(image_pipelines.presets_for("qwen21"), [])
        self.assertEqual(image_pipelines.preset_slots("sdxl"), {"speed": "11", "quality": "12"})
        self.assertEqual(image_pipelines.preset_slots("qwen21"), {})

    def test_accelerators_carry_sampling_settings(self):
        for preset in image_pipelines.presets_for("sdxl", role="speed"):
            self.assertIn("steps", preset.params, preset.key)
            self.assertIn("sampler", preset.params, preset.key)
            self.assertLessEqual(preset.params["steps"], 8, preset.key)
            self.assertLessEqual(preset.params["cfg"], 6.0, preset.key)
        for preset in image_pipelines.presets_for("sdxl", role="quality"):
            self.assertEqual(preset.params, {}, preset.key)

    async def test_apply_writes_the_lora_and_the_sampler_it_needs(self):
        payload = self.monitor.apply_preset("sdxl", "lcm")
        overrides = payload["overrides"]
        self.assertEqual(overrides["11"]["lora_name"], "lcm-lora-sdxl.safetensors")
        self.assertEqual(overrides["11"]["strength_model"], 1.0)
        self.assertEqual(overrides["4"]["sampler_name"], "lcm")
        self.assertEqual(overrides["4"]["scheduler"], "sgm_uniform")
        self.assertEqual(payload["params"]["steps"], 8)
        self.assertEqual(payload["params"]["cfg"], 1.5)
        active = [preset["key"] for preset in payload["presets"] if preset["active"]]
        self.assertEqual(active, ["lcm"])
        # The saved file is what the next generation reads.
        stored = json.loads((self.root / "image-workflow.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["sdxl"]["11"]["lora_name"], "lcm-lora-sdxl.safetensors")

    async def test_quality_preset_stacks_into_the_second_slot(self):
        self.monitor.apply_preset("sdxl", "lcm")
        payload = self.monitor.apply_preset("sdxl", "face")
        self.assertEqual(payload["overrides"]["11"]["lora_name"], "lcm-lora-sdxl.safetensors")
        self.assertEqual(payload["overrides"]["12"]["lora_name"], "face_xl_v0_1.safetensors")
        self.assertEqual(payload["params"], {}, "a quality LoRA never changes the step count")
        active = {preset["key"] for preset in payload["presets"] if preset["active"]}
        self.assertEqual(active, {"lcm", "face"})

    async def test_a_second_accelerator_replaces_the_first(self):
        self.monitor.apply_preset("sdxl", "lcm")
        payload = self.monitor.apply_preset("sdxl", "hyper8")
        self.assertEqual(payload["overrides"]["11"]["lora_name"], "Hyper-SDXL-8steps-CFG-lora.safetensors")
        self.assertNotIn("12", payload["overrides"])
        self.assertEqual(payload["overrides"]["4"]["sampler_name"], "dpmpp_2m")
        self.assertEqual(payload["overrides"]["4"]["scheduler"], "karras")

    async def test_clearing_restores_the_pipeline_defaults(self):
        self.monitor.apply_preset("sdxl", "dmd2")
        payload = self.monitor.clear_presets("sdxl")
        self.assertEqual(payload["overrides"], {})
        self.assertEqual(payload["params"], dict(image_pipelines.pipeline("sdxl").defaults))
        self.assertEqual([preset["key"] for preset in payload["presets"] if preset["active"]], [])
        self.assertEqual(json.loads((self.root / "image-workflow.json").read_text(encoding="utf-8")), {})

    async def test_a_missing_lora_file_is_reported_before_queueing(self):
        with patch.object(image_comfy, "list_models", side_effect=lambda kind: []):
            with self.assertRaises(module.HTTPException) as caught:
                self.monitor.apply_preset("sdxl", "lcm")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("lcm-lora-sdxl.safetensors", caught.exception.detail)

    async def test_pipelines_without_lora_nodes_refuse_presets(self):
        with self.assertRaises(module.HTTPException) as caught:
            self.monitor.apply_preset("qwen21", "lcm")
        self.assertEqual(caught.exception.status_code, 404)

    async def test_workflow_payload_lists_presets_with_availability(self):
        payload = self.monitor.workflow({"prompt": ""}, "sdxl")
        self.assertEqual(payload["preset_slots"], {"speed": "11", "quality": "12"})
        self.assertTrue(all(preset["available"] for preset in payload["presets"]))
        self.assertEqual(payload["presets"][0]["role"], "speed")
        self.assertFalse(any(preset["active"] for preset in payload["presets"]))

    async def test_only_a_running_job_is_shown_as_the_running_workflow(self):
        job = image_jobs.ImageJob("img-1", "a cat", "", {"steps": 8}, [])
        job.graph = [{"id": "11", "class_title": "LoraLoader"}]
        self.monitor.job = job
        self.assertIsNone(self.monitor.task)
        self.assertEqual(self.monitor.workflow({"prompt": ""}, "sdxl")["running"], [],
                         "a finished job leaves the card on the next-run graph")
        self.monitor.task = asyncio.create_task(asyncio.sleep(30))
        try:
            self.assertEqual(self.monitor.workflow({"prompt": ""}, "sdxl")["running"], job.graph)
        finally:
            self.monitor.task.cancel()
            self.monitor.task = None
        self.monitor.job = None

    async def test_preset_endpoints_apply_and_clear(self):
        applied = await self.api.post("/local/image/presets", json={"pipeline": "sdxl", "key": "lcm"})
        self.assertEqual(applied.status_code, 200)
        body = applied.json()
        self.assertEqual(body["applied"]["key"], "lcm")
        self.assertEqual(body["workflow"]["overrides"]["11"]["lora_name"], "lcm-lora-sdxl.safetensors")

        cleared = await self.api.delete("/local/image/presets", params={"pipeline": "sdxl"})
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.json()["cleared"], "all")
        self.assertEqual(cleared.json()["overrides"], {})

        unknown = await self.api.post("/local/image/presets", json={"pipeline": "sdxl", "key": "nope"})
        self.assertEqual(unknown.status_code, 404)
        bad_pipeline = await self.api.post("/local/image/presets", json={"pipeline": "nope", "key": "lcm"})
        self.assertEqual(bad_pipeline.status_code, 404)
        without_key = await self.api.post("/local/image/presets", json={"pipeline": "sdxl"})
        self.assertEqual(without_key.status_code, 400)

    async def test_presets_are_refused_while_a_job_runs(self):
        self.monitor.task = asyncio.create_task(asyncio.sleep(30))
        try:
            response = await self.api.post("/local/image/presets", json={"pipeline": "sdxl", "key": "lcm"})
        finally:
            self.monitor.task.cancel()
            self.monitor.task = None
        self.assertEqual(response.status_code, 409)

    async def test_generation_uses_the_saved_accelerator(self):
        self.monitor.apply_preset("sdxl", "lcm")
        graph = image_comfy.build_graph("sdxl", "a portrait", "", [], 832, 1216, 8, 1.5, 1.0, 7,
                                        "qwen/generated", image_comfy.pipeline_overrides("sdxl"))["prompt"]
        self.assertEqual(graph["11"]["inputs"]["lora_name"], "lcm-lora-sdxl.safetensors")
        self.assertEqual(graph["11"]["inputs"]["model"][0], "10", "the accelerator chains onto the checkpoint")
        self.assertEqual(graph["4"]["inputs"]["sampler_name"], "lcm")
        self.assertEqual(graph["4"]["inputs"]["steps"], 8)
        self.assertEqual(graph["4"]["inputs"]["cfg"], 1.5)


class InjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_html_injection_carries_the_generate_panel_assets(self):
        module.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"content-type": "text/html"},
                                           text='<html><head></head><body></body></html>')
        ))
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),
                                   base_url="http://127.0.0.1:8081")
        try:
            text = (await client.get("/test-native-route")).text
        finally:
            await client.aclose()
            await module.manager.client.aclose()
        # Any version query is fine; the assets must be cache-busted so a new
        # panel build reaches the browser without a manual cache clear.
        for asset in ("image-panel.js", "image-panel.css", "mode-control.js",
                      "video-panel.js", "video-panel.css"):
            self.assertRegex(text, rf"/local/{re.escape(asset)}\?v=\d+")

    async def test_panel_assets_are_served_and_unknown_ones_are_not(self):
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),
                                   base_url="http://127.0.0.1:8081")
        try:
            served = await client.get("/local/image-panel.js")
            missing = await client.get("/local/not-a-panel.js")
        finally:
            await client.aclose()
        self.assertEqual(served.status_code, 200)
        self.assertIn("javascript", served.headers["content-type"])
        self.assertEqual(missing.status_code, 404)
