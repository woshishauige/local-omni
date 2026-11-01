"""Regression tests for the video layer.

The report parser is the one that bit us: the director returns a JSON document
whose "video" field names the assembled file, and an earlier version read the
counts instead, so a finished film was reported as a failure.

The other lesson is that the director renders exactly one shot per submission, so
a film is a chain of submissions and each shot carries its own settings.

The film's save location is a folder the panel chooses, which may sit outside
ComfyUI's output tree — so the file route and the chooser are covered here too.
"""
import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import HTTPException

import guest_uploads
import prompt_enhance
import video_context
import video_denoise
import video_interpolate
import video_jobs
import video_routes


class DenoiseTests(unittest.TestCase):
    """The denoise step that finishes a film: naming, modes and the reported numbers."""

    def test_target_sits_beside_the_film(self):
        self.assertEqual(video_denoise.target_path(Path("D:/films/a/master.mp4")).name,
                         "master-denoised.mp4")

    def test_unknown_mode_is_refused(self):
        with self.assertRaises(video_denoise.DenoiseError):
            video_denoise.run(Path("D:/films/a/master.mp4"), "sharpen")

    def test_missing_file_is_refused(self):
        with self.assertRaises(video_denoise.DenoiseError):
            video_denoise.run(Path("D:/films/does-not-exist.mp4"))

    def test_the_note_reports_both_sides(self):
        note = video_denoise.describe({
            "mode": "nlmeans",
            "before": {"noise": 4.0, "detail": 1000.0},
            "after": {"noise": 3.0, "detail": 970.0},
        })
        self.assertIn("nlmeans", note)
        self.assertIn("-25%", note)
        self.assertIn("-3%", note)


class ReportParserTests(unittest.TestCase):
    def test_video_path_slot_is_used_when_present(self):
        outputs = {"50": {"video_path": [r"D:\out\video\H3-30s\proj\shot_01.mp4"]}}
        video, _ = video_routes._read_report(outputs)
        self.assertTrue(video.endswith("shot_01.mp4"))

    def test_the_report_json_supplies_the_path(self):
        outputs = {"50": {"checkpoint_report": ['{"video": "X:/out/movie.mp4", "next_shot": 2}']}}
        video, report = video_routes._read_report(outputs)
        self.assertTrue(video.endswith("movie.mp4"))
        self.assertIn("next_shot", report)

    def test_counts_are_not_mistaken_for_a_path(self):
        # completed_shots arrives as an int list; it must not become the report.
        outputs = {"50": {"checkpoint_report": ['{"video": "X:/out/master.mp4", "completed_shots": [1]}']}}
        video, _ = video_routes._read_report(outputs)
        self.assertTrue(video.endswith("master.mp4"))

    def test_a_report_without_a_path_invents_none(self):
        video, report = video_routes._read_report({"50": {"checkpoint_report": ["shots=2"]}})
        self.assertIsNone(video)
        self.assertEqual(report, "shots=2")


class VideoUrlTests(unittest.TestCase):
    def job(self, path):
        job = video_routes.VideoJob("v", "video_distill12", "", {})
        job.video_path = str(path)
        return job

    def test_a_film_inside_the_output_tree_becomes_a_url(self):
        target = video_routes.OUTPUT_ROOT / "video" / "p" / "master.mp4"
        self.assertEqual(self.job(target).video_url(),
                         "/local/video/file?path=" + quote(str(target.resolve())))

    def test_a_path_outside_the_output_tree_is_not_served(self):
        self.assertIsNone(self.job(r"C:\Windows\not-ours.mp4").video_url())

    def test_the_save_location_the_panel_picked_may_be_served(self):
        with tempfile.TemporaryDirectory() as folder:
            chosen = Path(folder).resolve() / "成片"
            chosen.mkdir()
            previous = video_routes._OUTPUT["root"]
            video_routes._OUTPUT["root"] = str(chosen)
            try:
                target = chosen / "proj" / "master.mp4"
                self.assertEqual(self.job(target).video_url(),
                                 "/local/video/file?path=" + quote(str(target.resolve())))
            finally:
                video_routes._OUTPUT["root"] = previous


class SaveLocationTests(unittest.TestCase):
    def test_an_empty_choice_means_the_default_folder(self):
        self.assertEqual(video_routes._resolve_output_root(None),
                         str(video_routes.DEFAULT_OUTPUT_ROOT))
        self.assertEqual(video_routes._resolve_output_root("   "),
                         str(video_routes.DEFAULT_OUTPUT_ROOT))

    def test_a_relative_value_is_read_from_the_output_directory(self):
        self.assertEqual(video_routes._resolve_output_root("video"),
                         str(video_routes.OUTPUT_ROOT / "video"))

    def test_an_absolute_value_is_used_as_is(self):
        with tempfile.TemporaryDirectory() as folder:
            chosen = str(Path(folder).resolve())
            self.assertEqual(video_routes._resolve_output_root(chosen), chosen)

    def test_a_file_is_not_a_save_location(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4") as handle:
            with self.assertRaises(HTTPException) as caught:
                video_routes._resolve_output_root(handle.name)
        self.assertEqual(caught.exception.status_code, 400)

    def test_the_project_folder_is_created_before_the_render_is_submitted(self):
        """A folder that cannot be written must cost a second, not a finished shot."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            created = video_routes._prepare_project_folder(root, "my-film")
            self.assertEqual(created, root / "my-film")
            self.assertTrue(created.is_dir())
            # Asking twice is what every re-run does, and it must not fail.
            self.assertTrue(video_routes._prepare_project_folder(root, "my-film").is_dir())

    def test_windows_rejected_names_are_refused_with_a_readable_message(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            for name in ("bad:name", "bad/name", "trailing.", "trailing ", "q?mark"):
                with self.assertRaises(HTTPException) as caught:
                    video_routes._prepare_project_folder(root, name)
                self.assertEqual(caught.exception.status_code, 400, name)
                self.assertIn("保存名称不能包含", caught.exception.detail)
            self.assertEqual(video_routes._prepare_project_folder(root, ""), root)

    def test_a_file_in_the_way_is_reported_as_an_unwritable_location(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / "taken").write_text("not a folder", encoding="utf-8")
            with self.assertRaises(HTTPException) as caught:
                video_routes._prepare_project_folder(root, "taken")
            self.assertEqual(caught.exception.status_code, 400)
            self.assertIn("保存位置写不进去", caught.exception.detail)


class FolderBrowseTests(unittest.TestCase):
    def test_one_directory_lists_its_subfolders_and_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / "b-成片").mkdir()
            (root / "a-素材").mkdir()
            (root / "note.txt").write_text("x", encoding="utf-8")
            listing = video_routes._folder_browse(str(root))
        self.assertEqual([entry["name"] for entry in listing["folders"]], ["a-素材", "b-成片"])
        self.assertEqual(listing["path"], str(root))
        self.assertEqual(listing["parent"], str(root.parent))
        self.assertTrue(listing["roots"], "the picker needs somewhere to start from")

    def test_no_path_returns_only_the_drives(self):
        listing = video_routes._folder_browse(None)
        self.assertIsNone(listing["path"])
        self.assertEqual(listing["folders"], [])
        self.assertTrue(listing["roots"])

    def test_a_missing_folder_is_reported(self):
        with self.assertRaises(HTTPException) as caught:
            video_routes._folder_browse(str(Path(tempfile.gettempdir()) / "no-such-folder-xyz"))
        self.assertEqual(caught.exception.status_code, 404)

    def test_a_relative_path_is_rejected(self):
        with self.assertRaises(HTTPException) as caught:
            video_routes._folder_browse("video")
        self.assertEqual(caught.exception.status_code, 400)


class DirectorInputTests(unittest.TestCase):
    """The save folder is an optional input, so the workflow export still maps."""

    def test_the_graph_carries_the_save_folder(self):
        graph = video_jobs.build_prompt("video_distill12", prompt="p", output_root=r"D:\films")
        self.assertEqual(graph["50"]["inputs"]["output_root"], r"D:\films")

    def test_an_absent_save_folder_keeps_the_nodes_own_default(self):
        graph = video_jobs.build_prompt("video_distill12", prompt="p")
        self.assertNotIn("output_root", graph["50"]["inputs"])

    def test_the_declared_inputs_still_line_up_with_the_exported_widgets(self):
        # Read from the running server: an optional input that were declared among
        # the required ones would slide every exported widget value onto the wrong
        # name, and the prompt would silently lose its project name and storyboard.
        try:
            schema = video_jobs.schema_for("H3MP4ShotEditor")
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"ComfyUI is not answering /object_info: {exc}")
        names = video_jobs.widget_input_names(schema)
        self.assertIn("output_root", names)
        self.assertGreater(names.index("output_root"), names.index("use_first_shot_reference"))
        workflow = video_jobs.load_template("video_distill12")
        director = next(node for node in workflow["nodes"] if node["type"] == "H3MP4ShotEditor")
        named = video_jobs.named_widgets("H3MP4ShotEditor", director["widgets_values"])
        self.assertEqual(named["project_name"], "tianmen-duel-30s")
        self.assertEqual(named["action"], "next_shot")
        self.assertEqual((named["width"], named["height"], named["fps"]), (768, 512, 24))
        self.assertEqual(named["steps"], 12, "the shipped file must carry its own route's step count")
        self.assertIs(named["use_first_shot_reference"], True)


class StructureGuardTests(unittest.TestCase):
    """The structure switch rides along with the prompts and touches nothing else."""

    def shot_text(self, graph, node_id="40"):
        return graph[node_id]["inputs"][video_jobs.SHOT_TEXT_INPUT]

    def test_off_by_default(self):
        graph = video_jobs.build_prompt("video_distill12", prompt="p", shot_count=1,
                                        shot_seconds=[5.0])
        self.assertNotIn(video_jobs.STRUCTURE_CONSTRAINT, self.shot_text(graph))

    def test_it_appends_the_constraint_to_every_shot(self):
        graph = video_jobs.build_prompt("video_distill12", prompt="film text",
                                        shots=["shot one", "shot two"], shot_count=2,
                                        shot_seconds=[5.0, 5.0], structure_guard=True)
        first = self.shot_text(graph)
        second = self.shot_text(graph, "41")
        self.assertIn("shot one", first)
        self.assertIn(video_jobs.STRUCTURE_CONSTRAINT, first)
        self.assertIn(video_jobs.STRUCTURE_CONSTRAINT, second)

    def storyboard(self, graph):
        return json.loads(graph[str(video_jobs.DIRECTOR)]["inputs"]["storyboard"])

    def test_it_reaches_the_storyboard_the_director_reads(self):
        graph = video_jobs.build_prompt("video_distill12", prompt="film text",
                                        shots=["shot one"], shot_count=1,
                                        shot_seconds=[5.0], structure_guard=True)
        self.assertIn(video_jobs.STRUCTURE_CONSTRAINT,
                      self.storyboard(graph)["shots"][0]["prompt"])

    def test_a_shot_without_its_own_text_uses_the_film_description(self):
        graph = video_jobs.build_prompt("video_distill12", prompt="film text",
                                        shots=[""], shot_count=1, shot_seconds=[5.0],
                                        structure_guard=True)
        prompt = self.storyboard(graph)["shots"][0]["prompt"]
        self.assertIn("film text", prompt)
        self.assertIn(video_jobs.STRUCTURE_CONSTRAINT, prompt)

    def test_the_structure_switch_never_touches_the_cache_node(self):
        def cache_node(guard):
            workflow = video_jobs.load_template("video_distill12")
            graph = video_jobs.build_prompt("video_distill12", prompt="p", shot_count=1,
                                            shot_seconds=[5.0], structure_guard=guard)
            shipped = next(node for node in workflow["nodes"] if node["id"] == video_jobs.CACHE_NODE)
            return shipped["mode"], "80" in graph
        # The shipped graph keeps its cache node in bypass, and no prompt carries it.
        self.assertEqual(cache_node(True), cache_node(False))
        self.assertEqual(cache_node(True), (4, False))

    def test_no_recipe_reaches_comfyui_with_a_cache_node(self):
        for entry in video_jobs.recipes():
            graph = video_jobs.build_prompt(entry.key, prompt="p", shot_count=1,
                                            shot_seconds=[5.0])
            self.assertNotIn("80", graph, entry.key)


class FirstShotContextTests(unittest.TestCase):
    """Shot 1 can be given the predecessor it does not have.

    The director continues a shot from the file before it; for shot 1 that file comes
    from the caller, so the gateway only has to pass the name through and make sure the
    clip is in the shape the continuation path reads (size, 24 fps, audio).
    """

    def director_inputs(self, **kwargs):
        return video_jobs.build_prompt("video_distill12", prompt="p", shots=["s"],
                                       shot_count=1, shot_seconds=[5.0], **kwargs)["50"]["inputs"]

    def test_the_clip_reaches_the_director(self):
        inputs = self.director_inputs(shot_01_context="qwen-h3-context-abc-768x512.mp4")
        self.assertEqual(inputs["shot_01_context"], "qwen-h3-context-abc-768x512.mp4")

    def test_without_a_clip_the_widget_stays_empty(self):
        self.assertIsNone(self.director_inputs().get("shot_01_context"))

    def test_no_clip_means_no_conversion(self):
        self.assertIsNone(video_routes._prepare_context(None, 768, 512))
        self.assertIsNone(video_routes._prepare_context("   ", 768, 512))

    def test_a_missing_clip_is_rejected_with_400(self):
        with self.assertRaises(HTTPException) as caught:
            video_routes._prepare_context("nope-not-here.mp4", 768, 512)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("前文视频", caught.exception.detail)

    def test_conversion_resizes_retimes_and_keeps_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            source = folder / "raw.mp4"
            subprocess.run([video_context._ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=2",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)],
                           check=True, timeout=180)
            name = video_context.normalize(source, 768, 512, folder)
            self.assertTrue((folder / name).is_file())
            info = video_context.probe(folder / name)
            self.assertEqual((info["width"], info["height"]), (768, 512))
            self.assertEqual(round(info["fps"]), 24)
            self.assertTrue(info["audio"])
            # Content-addressed: the second call reuses the converted file.
            stamp = (folder / name).stat().st_mtime_ns
            self.assertEqual(video_context.normalize(source, 768, 512, folder), name)
            self.assertEqual((folder / name).stat().st_mtime_ns, stamp)

    def test_a_clip_without_audio_still_gets_a_track(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            source = folder / "silent.mp4"
            subprocess.run([video_context._ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=2",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)],
                           check=True, timeout=180)
            self.assertFalse(video_context.probe(source)["audio"])
            info = video_context.probe(folder / video_context.normalize(source, 768, 512, folder))
            self.assertTrue(info["audio"])

    def test_a_too_short_clip_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            source = folder / "short.mp4"
            subprocess.run([video_context._ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=0.4",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)],
                           check=True, timeout=180)
            with self.assertRaises(ValueError) as caught:
                video_context.normalize(source, 768, 512, folder)
            self.assertIn("太短", str(caught.exception))


class InheritedWorkflowContentTests(unittest.TestCase):
    """Nothing the workflow file was last saved with may leak into a new film.

    The export carries a 923-character global prompt — the demo project's own film
    description — and the director prefixes it to every shot, while the reference
    slots keep whatever image the file was saved with. Both turned up in a finished
    film that the panel had submitted without any of it.
    """

    def slots(self, graph):
        return {node_id: node["inputs"] for node_id, node in graph.items()
                if node["class_type"] == "H3OptionalReferenceImage"}

    def test_the_shipped_film_description_is_not_prepended(self):
        shipped = video_jobs.load_template("video_distill12")
        director = next(node for node in shipped["nodes"] if node["type"] == "H3MP4ShotEditor")
        self.assertTrue(video_jobs.named_widgets("H3MP4ShotEditor",
                                                 director["widgets_values"])["global_prompt"])
        for key in ("video_distill12", "video_distill3", "video_balance6"):
            graph = video_jobs.build_prompt(key, prompt="夜晚雨后的城市街道")
            self.assertEqual(graph["50"]["inputs"]["global_prompt"], "", key)

    def test_the_storyboard_keeps_only_the_callers_own_text(self):
        graph = video_jobs.build_prompt("video_distill12", prompt="夜晚雨后的城市街道",
                                        shots=["第一段：远景"], shot_count=1, shot_seconds=[5.0])
        storyboard = json.loads(graph["50"]["inputs"]["storyboard"])
        self.assertEqual([shot["prompt"] for shot in storyboard["shots"]], ["第一段：远景"])

    def test_every_unfilled_reference_slot_is_switched_off(self):
        for key in ("video_distill12", "video_distill3", "video_balance6"):
            slots = self.slots(video_jobs.build_prompt(key, prompt="p"))
            self.assertEqual(len(slots), 20, key)
            self.assertTrue(all(inputs["enabled"] is False for inputs in slots.values()), key)

    def test_only_the_references_the_caller_sent_are_enabled(self):
        graph = video_jobs.build_prompt("video_distill12", prompt="p",
                                        shot_references=[["a.png"], [], ["c1.png", "c2.png"]])
        slots = self.slots(graph)
        self.assertEqual(sum(1 for inputs in slots.values() if inputs["enabled"]), 3)
        self.assertEqual({inputs.get("image") for inputs in slots.values() if inputs["enabled"]},
                         {"a.png", "c1.png", "c2.png"})
        self.assertTrue(all(inputs.get("image") == "__none__" for inputs in slots.values()
                            if not inputs["enabled"]))

    def test_the_shipped_workflows_have_no_armed_slot(self):
        for key in ("video_distill12", "video_distill3", "video_balance6"):
            workflow = video_jobs.load_template(key)
            nodes = [node for node in workflow["nodes"] if node["type"] == "H3OptionalReferenceImage"]
            self.assertEqual(len(nodes), 20, key)
            for node in nodes:
                self.assertEqual(node["widgets_values"], ["__none__", False],
                                 f"{key} node {node['id']}")


class UpscaleDenoiseTests(unittest.TestCase):
    """The upscale recipe that denoises after SeedVR2, and the pass it drives."""

    def test_no_upscale_recipe_bundles_the_denoise_pass(self):
        """Denoising is its own module on both sides: no upscale recipe may smuggle one in.

        Video gets 「超分重建」+「降噪」, the image panel 「超分」+「降噪」, so the bundled
        pass is gone everywhere and the order stays an explicit two-step the user runs.
        """
        import image_pipelines

        for entry in image_pipelines.workflows_for_all():
            if entry.kind != "upscale":
                continue
            self.assertIsNone(entry.params.get("video_denoise"), entry.key)

    def test_the_denoise_recipe_is_a_module_of_its_own(self):
        import image_pipelines

        entry = image_pipelines.workflow("video_denoise")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.pipeline, "seedvr2_video")
        self.assertEqual(entry.kind, "denoise")
        self.assertEqual(entry.mode, "video")
        self.assertEqual(entry.params["video_denoise"], 2.0)
        # The panel posts it to /local/video/denoise-source, so no graph is built at all;
        # skip_upscale is what refuses it if a caller aims /local/image/generate at it.
        self.assertTrue(entry.params.get("skip_upscale"))

    def test_strength_becomes_the_nlmeans_filter(self):
        self.assertEqual(video_denoise.filter_for(2.0), "nlmeans=s=2.00:p=7:r=15")
        # Clamped to the dial's range rather than handed to ffmpeg unchanged.
        self.assertIn("s=1.00", video_denoise.filter_for(0.2))
        self.assertIn("s=3.50", video_denoise.filter_for(9.0))

    def test_a_strength_run_writes_its_own_file(self):
        import imageio_ffmpeg

        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "clip.mp4"
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel",
                            "error", "-y", "-f", "lavfi", "-i",
                            "testsrc=size=64x64:rate=8:duration=1", "-pix_fmt", "yuv420p",
                            str(source)], check=True)
            result = video_denoise.run(source, strength=2.0)
            self.assertTrue(Path(result["target"]).is_file())
            self.assertIn("s=2.00", result["mode"])


    def test_the_job_layer_denoises_what_comfyui_published(self):
        import asyncio
        import imageio_ffmpeg
        import image_comfy
        import image_jobs

        class History:
            def raise_for_status(self):
                return None

            def json(self):
                return {job.prompt_id: {"outputs": {
                    "12": {"gifs": [{"filename": clip.name, "subfolder": folder,
                                     "type": "output"}]}}}}

        class Client:
            async def get(self, url, timeout=None):
                return History()

        folder = "denoise-test"
        root = image_comfy.COMFY_ROOT / "ComfyUI" / "output" / folder
        root.mkdir(parents=True, exist_ok=True)
        try:
            clip = root / "upscaled.mp4"
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel",
                            "error", "-y", "-f", "lavfi", "-i",
                            "testsrc=size=64x64:rate=8:duration=1", "-pix_fmt", "yuv420p",
                            str(clip)], check=True)
            monitor = image_jobs.ImageJobMonitor.__new__(image_jobs.ImageJobMonitor)
            monitor.client = Client()
            job = image_jobs.ImageJob("t", "", "", {"video_denoise": 2.0}, [])
            job.prompt_id = "prompt-1"
            asyncio.run(monitor._denoise_result(job))
            self.assertTrue(Path(job.denoise["target"]).is_file())
            self.assertIn("nlmeans", job.denoise["note"])
            self.assertEqual(job.images, [job.denoise["video"]])
        finally:
            for path in root.glob("*"):
                path.unlink()
            root.rmdir()


class ConcatTests(unittest.TestCase):
    """Stitching separately processed segments back into one film."""

    def make_clip(self, folder: Path, name: str, colour: str, seconds: int) -> Path:
        import imageio_ffmpeg

        target = folder / name
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
                        "-y", "-f", "lavfi", "-i",
                        f"color=c={colour}:size=64x64:rate=8:duration={seconds}",
                        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                        "-pix_fmt", "yuv420p", "-c:v", "libx264", "-c:a", "aac", "-shortest",
                        str(target)], check=True)
        return target

    def first_frame_colour(self, path: Path) -> tuple[float, float, float]:
        import cv2

        capture = cv2.VideoCapture(str(path))
        ok, frame = capture.read()
        capture.release()
        self.assertTrue(ok, path)
        return tuple(float(value) for value in frame.mean(axis=(0, 1))[::-1])

    def test_the_front_slot_plays_first(self):
        import asyncio
        import image_comfy
        import video_denoise

        # The clips live in the served output tree, which is what the route accepts: a
        # caller-named path outside it is refused on purpose (the containment check).
        folder = image_comfy.COMFY_ROOT / "ComfyUI" / "output" / "stitch-test"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            front = self.make_clip(folder, "front.mp4", "red", 1)
            back = self.make_clip(folder, "back.mp4", "blue", 1)
            result = asyncio.run(video_routes.concat_clips(video_routes.ConcatRequest(
                clips=[str(front), str(back)], name="order-check")))
            payload = json.loads(result.body)
            target = Path(payload["target"])
            self.assertTrue(target.is_file())
            self.assertAlmostEqual(payload["seconds"], 2.0, delta=0.4)
            # 16 frames of 8 fps: the first half is the front clip's colour, the second the
            # back clip's, which is what "前段先播" has to mean.
            info = video_denoise.probe(target)
            self.assertEqual(info["frames"], 16)
            head = self.first_frame_colour(target)
            self.assertGreater(head[0], head[2], "首帧应当是红色的前段")
            target.unlink()
        finally:
            for path in folder.glob("*"):
                path.unlink()
            folder.rmdir()

    def test_one_clip_is_refused(self):
        import asyncio

        with self.assertRaises(HTTPException):
            asyncio.run(video_routes.concat_clips(
                video_routes.ConcatRequest(clips=[r"D:\single.mp4"])))


class ContinuityAndNormalizeTests(unittest.TestCase):
    """Per-shot 续接/切镜, and the stitching brightness match."""

    def storyboard(self, graph):
        return json.loads(graph[str(video_jobs.DIRECTOR)]["inputs"]["storyboard"])

    def test_cut_is_written_into_the_storyboard(self):
        graph = video_jobs.build_prompt("video_distill3", prompt="p", shots=["a", "b", "c"],
                                        shot_count=3, shot_seconds=[5.0, 5.0, 5.0],
                                        shot_continuity=["continue", "cut", "cut"])
        shots = self.storyboard(graph)["shots"]
        self.assertNotIn("continuity", shots[0])
        self.assertEqual(shots[1]["continuity"], "cut")
        self.assertEqual(shots[2]["continuity"], "cut")

    def test_continue_shots_carry_no_field(self):
        graph = video_jobs.build_prompt("video_distill3", prompt="p", shots=["a", "b"],
                                        shot_count=2, shot_seconds=[5.0, 5.0],
                                        shot_continuity=["continue", "continue"])
        for shot in self.storyboard(graph)["shots"]:
            self.assertNotIn("continuity", shot)

    def test_the_shot_plan_normalises_continuity(self):
        body = video_routes.VideoGenerateRequest(shots=["a", "b"], shot_plan=[
            {"seconds": 5.0, "steps": 3, "seed": 1, "continuity": "CUT"},
            {"seconds": 5.0, "steps": 3, "seed": 2},
        ])
        plan = video_routes._shot_plan(body)
        self.assertEqual(plan[0]["continuity"], "cut")
        self.assertEqual(plan[1]["continuity"], "continue")

    def test_normalize_moves_a_dark_segment_towards_the_first(self):
        import asyncio
        import cv2
        import image_comfy
        import imageio_ffmpeg

        folder = image_comfy.COMFY_ROOT / "ComfyUI" / "output" / "stitch-test"
        folder.mkdir(parents=True, exist_ok=True)

        def clip(name, filters):
            target = folder / name
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel",
                            "error", "-y", "-f", "lavfi", "-i",
                            "color=c=gray:size=64x64:rate=8:duration=1",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                            "-vf", filters, "-pix_fmt", "yuv420p", "-c:v", "libx264",
                            "-c:a", "aac", "-shortest", str(target)], check=True)
            return target

        def luma(path):
            capture = cv2.VideoCapture(str(path))
            ok, frame = capture.read()
            capture.release()
            self.assertTrue(ok, path)
            return float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean())

        try:
            bright = clip("bright.mp4", "null")
            dark = clip("dark.mp4", "eq=brightness=-0.30")
            plain = asyncio.run(video_routes.concat_clips(video_routes.ConcatRequest(
                clips=[str(bright), str(dark)], name="plain")))
            fixed = asyncio.run(video_routes.concat_clips(video_routes.ConcatRequest(
                clips=[str(bright), str(dark)], name="fixed", normalize=True)))
            # The second half of each film is the dark clip; after normalisation its luma
            # has to land closer to the first clip's than it was.
            def second_half_luma(payload):
                target = Path(json.loads(payload.body)["target"])
                capture = cv2.VideoCapture(str(target))
                frames = []
                while len(frames) < 16:
                    ok, frame = capture.read()
                    if not ok:
                        break
                    frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean())
                capture.release()
                target.unlink()
                return sum(frames[8:]) / max(len(frames[8:]), 1)

            self.assertLess(abs(second_half_luma(fixed) - luma(bright)),
                            abs(second_half_luma(plain) - luma(bright)))
            gains = json.loads(fixed.body)["brightness"]
            self.assertEqual(len(gains), 2)
            self.assertAlmostEqual(gains[0], 0.0, places=3)
            self.assertGreater(gains[1], 0.1)
        finally:
            for path in folder.glob("*"):
                path.unlink()
            folder.rmdir()


class LoraStrengthTests(unittest.TestCase):
    """The strength dial the panel offers for the routes that ship a distill LoRA."""

    def graph_lora(self, key, **kwargs):
        graph = video_jobs.build_prompt(key, prompt="p", shot_count=1, shot_seconds=[5.0], **kwargs)
        return [node["inputs"] for node in graph.values()
                if node["class_type"] == "LoraLoaderModelOnly"][0]

    def test_the_workflow_files_ship_the_measured_strength(self):
        for key in ("video_distill3", "video_balance6"):
            entry = video_jobs.recipe(key)
            self.assertEqual(video_jobs.lora_in_template(entry.file)["strength"], 0.85, key)
            self.assertEqual(self.graph_lora(key)["strength_model"], 0.85, key)

    def test_a_request_strength_reaches_the_graph(self):
        node = self.graph_lora("video_distill3", lora_strength=0.7)
        self.assertEqual(node["strength_model"], 0.7)

    def test_a_request_strength_is_clamped(self):
        self.assertEqual(self.graph_lora("video_distill3", lora_strength=3.0)["strength_model"],
                         1.2)
        self.assertEqual(self.graph_lora("video_distill3", lora_strength=0.01)["strength_model"],
                         0.4)

    def test_a_strength_on_a_graph_without_a_lora_is_refused(self):
        # No shipped route is LoRA-less any more, so the guard is exercised against the
        # retired native workflow, registered only for the duration of this test.
        entry = video_jobs.Recipe(key="video_probe_native", label="探针",
                                  file="视频生成/01_视频_质量_20步原生.json", summary="",
                                  detail="")
        video_jobs.RECIPE_INDEX[entry.key] = entry
        try:
            with self.assertRaises(RuntimeError):
                video_jobs.build_prompt(entry.key, prompt="p", shot_count=1,
                                        shot_seconds=[5.0], lora_strength=0.85)
        finally:
            video_jobs.RECIPE_INDEX.pop(entry.key, None)

    def test_every_route_reports_its_lora(self):
        for entry in video_jobs.recipes():
            described = entry.describe()["lora"]
            self.assertIsNotNone(described, entry.key)
            # The three distillation routes carry TaoMate at its measured 0.85; the two
            # adapted routes carry the LoRA they were built around, at its own strength.
            if "TaoMate" in described["name"]:
                self.assertIn("TaoMate-H3-3step", described["name"], entry.key)
                self.assertEqual(described["strength"], 0.85, entry.key)
            else:
                self.assertIn(described["strength"], (0.85, 1.0), entry.key)


class RecipeTests(unittest.TestCase):
    def test_recipes_exist_and_point_at_shipped_files(self):
        keys = {entry.key for entry in video_jobs.recipes()}
        self.assertEqual(keys, {"video_distill12", "video_balance6", "video_balance6_nsfw",
                                "video_distill3"})
        for entry in video_jobs.recipes():
            self.assertTrue((video_jobs.WORKFLOW_ROOT / entry.file).exists(), entry.file)

    def test_every_distill_workflow_carries_a_lora_wired_into_the_director(self):
        for key, steps in (("video_distill12", 12), ("video_balance6", 6), ("video_distill3", 3)):
            entry = video_jobs.recipe(key)
            template = video_jobs.load_template(key)
            loras = [node for node in template["nodes"]
                     if node["type"] == "LoraLoaderModelOnly"]
            self.assertEqual(len(loras), 1, key)
            self.assertIn("TaoMate-H3-3step", loras[0]["widgets_values"][0], key)
            # The director must sample a model that has been through the LoRA. The 2-step route
            # puts MiniMaxH3SigmaShift (the AV clock) in between, so walk the chain back.
            graph = video_jobs.build_prompt(key, prompt="a test film", seed=1)
            node_id = graph[str(video_jobs.DIRECTOR)]["inputs"]["model"][0]
            for _ in range(8):
                if node_id == str(loras[0]["id"]):
                    break
                node_id = graph[node_id]["inputs"]["model"][0]
            self.assertEqual(node_id, str(loras[0]["id"]), key)
            self.assertIn(graph[node_id]["inputs"]["model"][0], graph, key)
            self.assertEqual(graph[str(video_jobs.DIRECTOR)]["inputs"]["steps"], steps, key)

    def test_the_quality_recipe_drops_the_cache_node(self):
        graph = video_jobs.build_prompt("video_distill12", prompt="a test film", seed=1)
        present = any(node["class_type"] == "ApplyMiniMaxH3FirstBlockCache"
                      for node in graph.values())
        self.assertFalse(present)

    def test_every_link_points_at_a_node_in_the_prompt(self):
        for key in ("video_distill12", "video_balance6", "video_distill3"):
            graph = video_jobs.build_prompt(key, prompt="a test film", seed=1)
            for node_id, node in graph.items():
                for name, value in node["inputs"].items():
                    if isinstance(value, list) and value:
                        self.assertIn(str(value[0]), graph, f"{key}: {node_id}.{name}")


class ShotPlanTests(unittest.TestCase):
    """One settings entry per shot, because the director renders one shot per run."""

    def plan(self, shots, entries=()):
        body = video_routes.VideoGenerateRequest(
            prompt="p", shots=shots,
            shot_plan=[video_routes.ShotPlan(**entry) for entry in entries])
        return video_routes._shot_plan(body)

    def test_one_entry_per_shot_with_the_panels_values(self):
        plan = self.plan(["a", "b"], [{"seconds": 6.0, "steps": 8, "seed": 7}])
        self.assertEqual(len(plan), 2)
        self.assertEqual((plan[0]["seconds"], plan[0]["steps"], plan[0]["seed"]), (6.0, 8, 7))
        # The shot the panel said nothing about still gets usable settings.
        self.assertEqual(plan[1]["seconds"], 7.5)
        self.assertEqual(plan[1]["steps"], 20)

    def test_a_missing_or_negative_seed_becomes_a_random_one(self):
        plan = self.plan(["a", "b", "c"], [{"seed": -1}, {"seed": None}, {}])
        for entry in plan:
            self.assertIsInstance(entry["seed"], int)
            self.assertGreaterEqual(entry["seed"], 0)
        # The workflow file ships one fixed seed; reusing it would repeat the film.
        self.assertEqual(len({entry["seed"] for entry in plan}), 3)

    def test_settings_outside_the_nodes_range_are_clamped(self):
        plan = self.plan(["a"], [{"seconds": 99.0, "steps": 500, "seed": 1}])
        self.assertEqual(plan[0]["seconds"], 13.75)
        self.assertEqual(plan[0]["steps"], 50)
        plan = self.plan(["a"], [{"seconds": 1.0, "steps": 0, "seed": 1}])
        self.assertEqual(plan[0]["seconds"], 5.0)
        self.assertEqual(plan[0]["steps"], 1)

    def test_a_caller_that_only_names_shots_still_gets_a_plan(self):
        plan = self.plan(["a", "b", "c"])
        self.assertEqual(len(plan), 3)


class ShotSelectionTests(unittest.TestCase):
    """The chain renders each shot by index, so the graph has to say which one."""

    def director(self, **kwargs):
        graph = video_jobs.build_prompt("video_distill12", prompt="a test film",
                                        shot_count=2, shot_seconds=[7.5, 8.0], **kwargs)
        return graph["50"]["inputs"]

    def test_the_first_submission_renders_shot_one_by_itself(self):
        inputs = self.director(shot_index=1, action="redo_shot", steps=9, seed=11)
        self.assertEqual(inputs["shot_index"], 1)
        self.assertEqual(inputs["action"], "redo_shot")
        self.assertEqual(inputs["steps"], 9)
        self.assertEqual(inputs["seed"], 11)

    def test_the_second_submission_carries_its_own_settings(self):
        inputs = self.director(shot_index=2, action="redo_shot", steps=24, seed=99)
        self.assertEqual(inputs["shot_index"], 2)
        self.assertEqual(inputs["steps"], 24)
        self.assertEqual(inputs["seed"], 99)

    def test_the_storyboard_carries_every_shot_and_its_own_duration(self):
        inputs = self.director(shot_index=2, action="redo_shot")
        storyboard = json.loads(inputs["storyboard"])
        self.assertEqual([shot["seconds"] for shot in storyboard["shots"]], [7.5, 8.0])

    def test_the_shipped_action_is_kept_when_the_caller_says_nothing(self):
        self.assertEqual(self.director()["action"], "next_shot")


class InterpolateTests(unittest.TestCase):
    """The RIFE post-process: graph and naming, without touching the GPU."""

    def graph(self, multiplier=2, fps=24.0):
        return video_interpolate.build_prompt("film.mp4", "qwen-interp/abc", fps, multiplier)

    def test_the_graph_loads_the_film_runs_rife_and_saves_a_video(self):
        graph = self.graph()
        self.assertEqual(graph["1"]["class_type"], "LoadVideo")
        self.assertEqual(graph["1"]["inputs"]["file"], "film.mp4")
        self.assertEqual(graph["2"]["class_type"], "GetVideoComponents")
        self.assertEqual(graph["3"]["class_type"], "RIFE VFI")
        self.assertEqual(graph["3"]["inputs"]["frames"], ["2", 0])
        self.assertEqual(graph["4"]["class_type"], "CreateVideo")
        self.assertEqual(graph["5"]["class_type"], "SaveVideo")

    def test_every_link_points_at_a_node_in_the_graph(self):
        for node_id, node in self.graph().items():
            for name, value in node["inputs"].items():
                if isinstance(value, list):
                    self.assertIn(str(value[0]), self.graph(), f"{node_id}.{name}")

    def test_the_multiplier_sets_the_output_frame_rate(self):
        for multiplier, expected in ((2, 48.0), (4, 96.0)):
            self.assertEqual(self.graph(multiplier)["4"]["inputs"]["fps"], expected)

    def test_the_audio_is_carried_through(self):
        self.assertEqual(self.graph()["4"]["inputs"]["audio"], ["2", 1])

    def test_the_published_file_sits_next_to_the_film(self):
        self.assertEqual(video_interpolate.published_name(Path(r"D:\out\shot_01.mp4"), 48),
                         "shot_01-48fps.mp4")

    def test_the_job_has_no_url_before_it_finishes(self):
        job = video_interpolate.InterpolateJob("itp-1", Path(r"D:\out\shot_01.mp4"), 2)
        self.assertIsNone(job.video_url())
        job.target = Path(r"D:\out\shot_01-48fps.mp4")
        self.assertIn("shot_01-48fps.mp4", job.video_url())


class VideoPromptEnhancementTests(unittest.IsolatedAsyncioTestCase):
    """生成视频的提示词优化：用当前选中的模型，写完就把显卡交还给 ComfyUI。

    The video queue used to have no prompt step at all, so the request field and the ordering
    (rewrite first, release the language model before the render) are what these pin.
    """

    def setUp(self):
        previous = (video_routes._ENHANCER, video_routes._RELEASE_FOR_RENDER)
        self.addCleanup(lambda: video_routes.set_prompt_enhancer(*previous))

    def job(self, **overrides):
        params = {"shot_plan": [{"seconds": 5.0, "steps": 3, "seed": 1}]} | overrides
        return video_routes.VideoJob("vid-test", "video_distill3", "一只猫", params)

    async def asyncTearDown(self):
        for client in getattr(self, "clients", []):
            await client.aclose()

    def enhancer(self, reply="夜晚雨后的街道，霓虹在水面反光，镜头缓推。", loader=None,
                 settings=None, comfy=None, queue_wait=None):
        recorded = []
        self.requests = recorded
        queued = list(comfy or [])

        async def handler(request):
            recorded.append(str(request.url))
            if request.url.path == "/queue":
                body = queued.pop(0) if queued else {"queue_running": [], "queue_pending": []}
                return httpx.Response(200, json=body)
            return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.clients = getattr(self, "clients", []) + [client]
        extra = {"comfy_base": "http://comfy.test"} if comfy is not None else {}
        if queue_wait is not None:
            extra["queue_wait"] = queue_wait
            extra["queue_interval"] = 0.01
        return prompt_enhance.PromptEnhancer(
            client, loader,
            settings_getter=lambda: settings or {"model_profile": "qwen3.5-9b-uncensored"},
            **extra)

    async def test_the_film_description_is_rewritten_by_the_selected_model(self):
        loader = AsyncMock()
        release = AsyncMock()
        video_routes.set_prompt_enhancer(self.enhancer(loader=loader), release=release)
        job = self.job(enhance_prompt=True)
        await video_routes.VideoMonitor(self.clients[0]).prepare_prompt(job)
        self.assertEqual(job.prompt, "夜晚雨后的街道，霓虹在水面反光，镜头缓推。")
        self.assertEqual(job.params["prompt"], job.prompt, "the storyboard reads params")
        self.assertEqual(job.enhancer["label"], "9B 无审查")
        self.assertEqual(job.enhancer["original"], "一只猫")
        self.assertTrue(job.enhancer["used"])
        # The model is loaded through the gateway's own loader (the selected profile), and the
        # card goes straight back to ComfyUI: H3 renders next.
        loader.assert_awaited_once_with("text")
        release.assert_awaited_once()
        self.assertIn("enhanced", [note["phase"] for note in job.notes])

    async def test_a_new_film_is_not_optimised_unless_the_owner_asks(self):
        loader = AsyncMock()
        release = AsyncMock()
        video_routes.set_prompt_enhancer(self.enhancer(loader=loader), release=release)
        job = self.job()
        await video_routes.VideoMonitor(self.clients[0]).prepare_prompt(job)
        self.assertEqual(job.prompt, "一只猫")
        self.assertIsNone(job.enhancer)
        self.assertEqual(self.requests, [], "no model is asked for anything")
        loader.assert_not_awaited()
        release.assert_not_awaited()

    async def test_without_an_enhancer_bound_the_request_still_works(self):
        # The router is importable on its own (check_video_routes.py does exactly that), so the
        # step has to be a no-op when nothing has been injected rather than an AttributeError.
        video_routes.set_prompt_enhancer(None, None)
        job = self.job(enhance_prompt=True)
        await video_routes.VideoMonitor(httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))).prepare_prompt(job)
        self.assertEqual(job.prompt, "一只猫")
        self.assertIsNone(job.enhancer)

    async def test_a_busy_comfyui_is_waited_out_instead_of_reported(self):
        """The old message promised a load after the queue drained; now that is what happens."""
        loader = AsyncMock()
        video_routes.set_prompt_enhancer(
            self.enhancer(loader=loader, queue_wait=30,
                          comfy=[{"queue_running": [["1", "shot"]], "queue_pending": []},
                                 {"queue_running": [], "queue_pending": [["2", "shot"]]}]),
            release=AsyncMock())
        job = self.job(enhance_prompt=True)
        await video_routes.VideoMonitor(self.clients[0]).prepare_prompt(job)
        self.assertTrue(job.enhancer["used"], "the prompt is optimised once the queue drains")
        # Three polls for two busy answers: check, sleep, check, sleep, check.
        self.assertEqual([url for url in self.requests if url.endswith("/queue")],
                         ["http://comfy.test/queue"] * 3)
        loader.assert_awaited_once_with("text")
        notes = " ".join(note["message"] for note in job.notes)
        self.assertNotIn("将在任务结束后自动加载", notes)

    async def test_a_queue_that_never_drains_leaves_the_words_and_says_so_briefly(self):
        loader = AsyncMock()
        video_routes.set_prompt_enhancer(
            self.enhancer(loader=loader, queue_wait=0,
                          comfy=[{"queue_running": [["1", "shot"]], "queue_pending": []}]),
            release=AsyncMock())
        job = self.job(enhance_prompt=True)
        await video_routes.VideoMonitor(self.clients[0]).prepare_prompt(job)
        self.assertEqual(job.prompt, "一只猫")
        self.assertFalse(job.enhancer["used"])
        self.assertEqual(job.enhancer["reason"], "ComfyUI 忙，未优化")
        loader.assert_not_awaited()
        self.assertIn("ComfyUI 忙，未优化",
                      " ".join(note["message"] for note in job.notes))

    async def test_the_measured_enhancement_time_and_the_stage_reach_the_panel(self):
        video_routes.set_prompt_enhancer(self.enhancer(queue_wait=5,
                                                       comfy=[{"queue_running": [], "queue_pending": []}]),
                                         release=AsyncMock())
        job = self.job(enhance_prompt=True)
        await video_routes.VideoMonitor(self.clients[0]).prepare_prompt(job)
        snapshot = job.snapshot()
        self.assertIsInstance(snapshot["enhance_seconds"], float)
        self.assertGreaterEqual(snapshot["enhance_seconds"], 0.0)
        self.assertEqual(job.enhancer["seconds"], snapshot["enhance_seconds"])
        # The rail shows both texts: "已优化" alone says nothing about what the film inherits.
        self.assertEqual(job.enhancer["original"], "一只猫")
        self.assertEqual(job.enhancer["rewritten"], job.prompt)
        self.assertEqual(snapshot["enhancer"]["rewritten"], job.prompt)
        # The panel draws the 提示词优化 stage from the job, not from the switch's current state.
        self.assertTrue(snapshot["settings"]["enhance_prompt"])
        self.assertFalse(self.job().snapshot()["settings"]["enhance_prompt"])

    async def test_the_job_reports_which_model_wrote_the_prompt(self):
        video_routes.set_prompt_enhancer(self.enhancer(), release=AsyncMock())
        job = self.job(enhance_prompt=True)
        await video_routes.VideoMonitor(self.clients[0]).prepare_prompt(job)
        self.assertEqual(job.snapshot()["enhancer"]["profile"], "qwen3.5-9b-uncensored")
        # The request field exists and defaults to off, so an existing caller keeps its behaviour.
        self.assertFalse(video_routes.VideoGenerateRequest(prompt="p").enhance_prompt)
        self.assertTrue(video_routes.VideoGenerateRequest(prompt="p", enhance_prompt=True).enhance_prompt)


class TestProcessNeverRendersTests(unittest.TestCase):
    """测试进程不许真的占用显卡——这是 2026-09-21 15:03 的教训。

    生图侧一直用假传输，视频侧的集成测试却打的是**真路由**：套件跑过之后，所有者机器上的
    ComfyUI 队列里多了 **5 条 20 步 H3 渲染**（storyboard 正是测试自己的 `{"prompt": "x"}`），
    白白占了 **约 32 分钟**显卡，而且网关根本不知道这些任务存在（它自己的任务表是空的）。
    """

    def setUp(self):
        self.previous = video_routes.os.environ.get(video_routes.TEST_RENDER_ENV)
        self.addCleanup(self._restore)

    def _restore(self):
        if self.previous is None:
            video_routes.os.environ.pop(video_routes.TEST_RENDER_ENV, None)
        else:
            video_routes.os.environ[video_routes.TEST_RENDER_ENV] = self.previous

    def test_a_test_process_is_refused_by_default(self):
        video_routes.os.environ.pop(video_routes.TEST_RENDER_ENV, None)
        self.assertTrue(video_routes.renders_are_refused())

    def test_a_deliberate_probe_can_opt_in(self):
        video_routes.os.environ[video_routes.TEST_RENDER_ENV] = "1"
        self.assertFalse(video_routes.renders_are_refused())

    def test_the_monitor_refuses_before_it_builds_any_graph(self):
        """拒绝发生在建图之前：连 ComfyUI 都不该被碰一下。"""
        video_routes.os.environ.pop(video_routes.TEST_RENDER_ENV, None)
        monitor = video_routes.VideoMonitor(_StubClient())
        with self.assertRaises(HTTPException) as caught:
            monitor.start(video_routes.VideoGenerateRequest(prompt="x", shots=[]))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("测试进程", caught.exception.detail)
        self.assertIsNone(monitor.job, "被拒绝的请求不能留下任务")
        self.assertIsNone(monitor.task, "被拒绝的请求不能起后台渲染")


class _StubClient:
    """Nothing may reach it; if it does, the test is the thing that failed."""

    async def post(self, *args, **kwargs):                     # pragma: no cover
        raise AssertionError("测试进程不该向 ComfyUI 提交任何东西")


class LoopBoundClientTests(unittest.TestCase):
    """缓存下来的兜底客户端必须**属于当前这个事件循环**。

    真机踩到（`test_guest_uploads` 里紧跟着的那条测试）：`/local/video/comfy` 让
    `_fallback_client()` 在**上一个测试的循环**里建了客户端并缓存，下一个测试（新循环）拿到同一个
    对象，`httpx` 在建连接池时抛 `RuntimeError: Event loop is closed`——同一条测试单独跑能过、
    跟在别人后面就失败。网关只有一个循环，所以生产上只会"顺手认领"；两个循环真的并存时换掉旧的。
    """

    def setUp(self):
        self.previous = (video_routes._LOCAL_CLIENT, video_routes._LOCAL_CLIENT_LOOP,
                         video_routes._CLIENT_PROVIDER)
        video_routes._LOCAL_CLIENT = None
        video_routes._LOCAL_CLIENT_LOOP = None
        video_routes._CLIENT_PROVIDER = lambda: None
        self.addCleanup(self._restore)

    def _restore(self):
        (video_routes._LOCAL_CLIENT, video_routes._LOCAL_CLIENT_LOOP,
         video_routes._CLIENT_PROVIDER) = self.previous

    def test_a_second_loop_gets_a_client_of_its_own(self):
        async def use():
            return video_routes._client()

        first = asyncio.run(use())
        self.assertTrue(first is not None)
        second = asyncio.run(use())
        self.assertIsNot(second, first, "换了一个事件循环就必须换客户端")
        self.assertIs(second, video_routes._LOCAL_CLIENT)

    def test_the_same_loop_keeps_reusing_one_client(self):
        async def twice():
            return video_routes._client(), video_routes._client()

        one, two = asyncio.run(twice())
        self.assertIs(one, two, "同一个循环里不要每次请求都新建客户端")


class MissingSharedClientTests(unittest.TestCase):
    """画面没有"网关尚未就绪"这一条：只跟 ComfyUI 说话的路由不能依赖语言模型那个 HTTP 客户端。

    真机踩到：所有者生成视频时报 `失败：网关尚未就绪，请稍后重试（HTTP 503）`，而片子其实在渲染
    ——是每步遥测那条路由（`/local/video/comfy`）每次调用都去要共享客户端，而共享客户端由网关的
    lifespan 创建、随进程状态可能缺席，于是面板把这条失败显示成整次生成的失败。
    """

    def setUp(self):
        self.previous_provider = video_routes._CLIENT_PROVIDER
        self.previous_client = getattr(video_routes, "_LOCAL_CLIENT", None)
        # Another suite may have left a stub in place; this test is about what happens when there
        # is no client at all, so start from a clean slate.
        video_routes._LOCAL_CLIENT = None
        self.addCleanup(setattr, video_routes, "_CLIENT_PROVIDER", self.previous_provider)
        self.addCleanup(setattr, video_routes, "_LOCAL_CLIENT", self.previous_client)

    def test_a_missing_shared_client_falls_back_instead_of_refusing(self):
        video_routes._CLIENT_PROVIDER = lambda: None
        client = video_routes._client()
        self.assertIsNotNone(client)
        self.assertIsInstance(client, httpx.AsyncClient)

    def test_the_route_answers_even_when_comfyui_is_unreachable(self):
        video_routes._CLIENT_PROVIDER = lambda: None
        previous_comfy = video_routes.COMFY
        video_routes.COMFY = "http://127.0.0.1:1"          # 一定连不上：那时必须回 null，而不是报错
        self.addCleanup(setattr, video_routes, "COMFY", previous_comfy)
        response = asyncio.run(video_routes.video_comfy())
        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(payload["monitor"])
        self.assertIsNone(payload["queue"])

    def test_a_present_shared_client_is_still_preferred(self):
        sentinel = object()
        video_routes._CLIENT_PROVIDER = lambda: sentinel
        self.assertIs(video_routes._client(), sentinel)


class SupervisorBindingTests(unittest.IsolatedAsyncioTestCase):
    """视频任务必须能自己把 ComfyUI 拉起来，而不是站在关着的门口报"没有启动器"。

    真机踩到：终止一条渲染时 ComfyUI 在卸载 GGUF 模型时崩了（`access violation`），随后两条
    视频任务在 2 秒内失败，报 `ComfyUI 未运行，且没有可用的启动器。`——因为 `_ensure_comfy`
    去 `import image_jobs` 拿的是**模块**，而 `supervisor` 挂在监控对象上，模块里从来没有这个名字
    （网关把监控对象发布成了 mode_server 里的 `image_jobs` 名字，把模块名遮蔽了）。
    """

    def setUp(self):
        self.previous = video_routes._SUPERVISOR_PROVIDER
        self.addCleanup(setattr, video_routes, "_SUPERVISOR_PROVIDER", self.previous)

    @staticmethod
    def _dead_client():
        client = AsyncMock()
        client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        return client

    async def test_bound_supervisor_starts_comfyui(self):
        supervisor = Mock()
        supervisor.start_timeout = 300
        supervisor.ensure_running = AsyncMock(return_value={"reachable": True})
        video_routes.set_supervisor_provider(lambda: supervisor)
        await video_routes._ensure_comfy(self._dead_client())
        supervisor.ensure_running.assert_awaited_once_with(timeout=300)

    async def test_a_reachable_comfyui_is_not_restarted(self):
        supervisor = Mock()
        supervisor.start_timeout = 300
        supervisor.ensure_running = AsyncMock()
        video_routes.set_supervisor_provider(lambda: supervisor)
        client = AsyncMock()
        client.get = AsyncMock(return_value=Mock(status_code=200))
        await video_routes._ensure_comfy(client)
        supervisor.ensure_running.assert_not_awaited()

    async def test_without_any_supervisor_the_failure_says_so(self):
        video_routes.set_supervisor_provider(lambda: None)
        with patch("image_jobs.supervisor", None, create=True):
            with self.assertRaises(RuntimeError) as caught:
                await video_routes._ensure_comfy(self._dead_client())
        self.assertIn("没有可用的启动器", str(caught.exception))

    async def test_provider_is_read_at_call_time(self):
        """绑定时监控对象还不存在（lifespan 里才创建），所以 provider 必须每次调用时才取。"""
        box = {"supervisor": None}
        video_routes.set_supervisor_provider(lambda: box["supervisor"])
        with self.assertRaises(RuntimeError):
            await video_routes._ensure_comfy(self._dead_client())
        supervisor = Mock()
        supervisor.start_timeout = 300
        supervisor.ensure_running = AsyncMock(return_value={"reachable": True})
        box["supervisor"] = supervisor
        await video_routes._ensure_comfy(self._dead_client())
        supervisor.ensure_running.assert_awaited_once()


class CancelOwnershipTests(unittest.TestCase):
    """取消只能取消自己的那一个任务（审查发现：访客能停机主正在渲染的片子）。"""

    def setUp(self):
        self.previous = video_routes._MONITOR
        self.addCleanup(setattr, video_routes, "_MONITOR", self.previous)

    def monitor(self, submitted_by):
        job = video_routes.VideoJob("vid-cancel", "video_distill3", "p",
                                    {"shot_plan": [{"seconds": 5.0, "steps": 3, "seed": 1}]})
        job.submitted_by = submitted_by
        fake = Mock()
        fake.job = job
        fake.busy = True
        fake.cancel = AsyncMock(return_value={"state": "cancelled"})
        video_routes._MONITOR = fake
        return fake

    def test_a_guest_cannot_cancel_the_owners_film(self):
        # The owner's jobs carry no session, so a visitor can never match one.
        monitor = self.monitor(submitted_by="")
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(video_routes.video_cancel(
                identity=video_routes.Identity(False, "guest-token")))
        self.assertEqual(caught.exception.status_code, 403)
        self.assertIn("不是你的任务", caught.exception.detail)
        monitor.cancel.assert_not_awaited()

    def test_a_guest_can_cancel_its_own_film(self):
        monitor = self.monitor(submitted_by="guest-token")
        asyncio.run(video_routes.video_cancel(identity=video_routes.Identity(False, "guest-token")))
        monitor.cancel.assert_awaited_once()

    def test_the_owner_can_cancel_any_film(self):
        monitor = self.monitor(submitted_by="guest-token")
        asyncio.run(video_routes.video_cancel(identity=video_routes.Identity(True, "owner")))
        monitor.cancel.assert_awaited_once()

    def test_nothing_running_still_answers_rather_than_refusing(self):
        monitor = self.monitor(submitted_by="guest-token")
        monitor.busy = False
        asyncio.run(video_routes.video_cancel(identity=video_routes.Identity(False, "guest-token")))
        monitor.cancel.assert_awaited_once()


class DenoiseSourceFenceTests(unittest.TestCase):
    """只降噪：读谁的素材、写到谁的目录（审查发现：兜底读机主选中片段 + 产物写机主目录）。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        media = Path(self.directory.name)
        self.input_root = media / "input"
        self.output_root = media / "output"
        self.input_root.mkdir(parents=True)
        self.output_root.mkdir(parents=True)
        for name, value in (("INPUT_ROOT", self.input_root), ("OUTPUT_ROOT", self.output_root),
                            ("DEFAULT_OUTPUT_ROOT", self.output_root / "video")):
            patcher = patch.object(video_routes, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        previous_store = video_routes._POLICY_STORE
        self.store = guest_uploads.GuestUploads(directory=media)
        video_routes.set_policy_store(self.store)
        self.addCleanup(video_routes.set_policy_store, previous_store)
        self.addCleanup(guest_uploads._SOURCES.clear)
        self.addCleanup(video_routes._SOURCE.update, {"path": video_routes._SOURCE.get("path")})
        # ffmpeg 不在这里跑：这一步只关心"读谁的、写哪儿"，但产物要真的落盘，
        # 因为断言的就是它的位置。
        def fake_run(path, mode, strength):
            target = Path(path).with_name("denoised.mp4")
            target.write_bytes(b"denoised")
            return {"target": str(target), "mode": mode, "seconds": 1.0, "note": "",
                    "before": {"noise": 1.0, "detail": 1.0},
                    "after": {"noise": 0.7, "detail": 0.95}}

        runner = patch.object(video_denoise, "run", side_effect=fake_run)
        runner.start()
        self.addCleanup(runner.stop)

    def test_a_guest_without_its_own_selection_is_not_given_the_owners_clip(self):
        owner_clip = self.input_root / "qwen-video-owner.mp4"
        owner_clip.write_bytes(b"owner")
        guest_uploads.select_source(guest_uploads.OWNER_SESSION, str(owner_clip))
        video_routes._SOURCE["path"] = str(owner_clip)
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(video_routes.denoise_source(
                video_routes.DenoiseSourceRequest(), identity=video_routes.Identity(False, "guest")))
        self.assertEqual(caught.exception.status_code, 400)

    def test_a_guest_cannot_denoise_somebody_elses_upload(self):
        theirs = self.input_root / "qwen-video-theirs.mp4"
        theirs.write_bytes(b"theirs")
        self.store.note_upload(theirs.name, "other-guest", 6)
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(video_routes.denoise_source(
                video_routes.DenoiseSourceRequest(name=theirs.name),
                identity=video_routes.Identity(False, "guest")))
        self.assertEqual(caught.exception.status_code, 404)

    def test_a_guest_denoises_its_own_clip_into_its_own_folder(self):
        mine = self.input_root / "qwen-video-mine.mp4"
        mine.write_bytes(b"mine")
        self.store.note_upload(mine.name, "guest", 4)
        response = asyncio.run(video_routes.denoise_source(
            video_routes.DenoiseSourceRequest(name=mine.name),
            identity=video_routes.Identity(False, "guest")))
        payload = json.loads(response.body)
        staged = Path(payload["target"])
        self.assertTrue(staged.is_file(), payload)
        # 落在访客自己的目录里，而不是机主的输出树。
        self.assertTrue(staged.is_relative_to(self.output_root / "guests"), str(staged))
        self.assertFalse((self.output_root / "video").exists(), "机主的输出树不该被访客写入")

    def test_the_owner_still_uses_the_panel_selection(self):
        owner_clip = self.input_root / "qwen-video-owner.mp4"
        owner_clip.write_bytes(b"owner")
        video_routes._SOURCE["path"] = str(owner_clip)
        response = asyncio.run(video_routes.denoise_source(
            video_routes.DenoiseSourceRequest(), identity=video_routes.Identity(True, "owner")))
        payload = json.loads(response.body)
        self.assertTrue(Path(payload["target"]).is_file())
        self.assertTrue(Path(payload["target"]).is_relative_to(self.output_root / "video"))


if __name__ == "__main__":
    unittest.main()
