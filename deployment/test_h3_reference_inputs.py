import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, 'D:/AI/ComfyUI-BSAI_v010/ComfyUI')
os.environ.setdefault('TRITON_CACHE_DIR', 'D:/AI/ComfyUI-BSAI_v010/.cache/triton')
import comfy.cli_args
comfy.cli_args.args.cpu = True
import torch
from PIL import Image
from comfy_extras.nodes_minimax_h3 import MiniMaxH3ReferenceToVideo
from aiohttp import web
from server import PromptServer

PromptServer.instance = mock.Mock(routes=web.RouteTableDef())

root = Path('D:/AI/ComfyUI-BSAI_v010/ComfyUI/custom_nodes/ComfyUI-H3-LocalDirector')
spec = importlib.util.spec_from_file_location('h3_reference_nodes', root / '__init__.py', submodule_search_locations=[str(root)])
director = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = director
spec.loader.exec_module(director)


class ReferenceTests(unittest.TestCase):
    def test_empty_disabled_and_uploaded_images(self):
        loader = director.H3OptionalReferenceImage()
        self.assertEqual(loader.load('__none__'), (None,))
        self.assertTrue(loader.VALIDATE_INPUTS('__none__'))
        self.assertEqual(loader.load('missing.png', enabled=False), (None,))
        self.assertTrue(loader.VALIDATE_INPUTS('missing.png', enabled=False))
        with tempfile.TemporaryDirectory(prefix='h3-ref-loader-', dir='D:/AI/deployment/logs') as temporary:
            path = Path(temporary) / 'reference.png'
            Image.new('RGBA', (2000, 1000), (120, 80, 40, 255)).save(path)
            with mock.patch.object(director.folder_paths, 'get_annotated_filepath', return_value=str(path)):
                pixels = loader.load('reference.png')[0]
                self.assertEqual(tuple(pixels.shape), (1, 384, 768, 3))
                first = loader.IS_CHANGED('reference.png')
                Image.new('RGB', (2000, 1000), (30, 50, 70)).save(path)
                self.assertNotEqual(first, loader.IS_CHANGED('reference.png'))

    def test_native_h3_encodes_all_five_references(self):
        images = {f'shot_01_ref_{index:02d}': torch.full((1, 32, 32, 3), index / 10) for index in range(1, 6)}
        refs, _ = director.collect_reference_images(images, 1)
        vae = mock.Mock()
        vae.encode.return_value = torch.zeros((1, 24, 2, 2, 2))
        clip = mock.Mock()
        clip.encode_from_tokens_scheduled.return_value = [[torch.zeros((1, 2, 4)), {}]]
        output = MiniMaxH3ReferenceToVideo.execute(clip, vae, None, 'Use the five pictures.', 32, 32, 124, ref_images=refs)
        self.assertEqual(vae.encode.call_count, 5)
        self.assertEqual(len(clip.tokenize.call_args.kwargs['minimax_ref_items']), 5)
        self.assertEqual(len(output[0][0][1]['minimax_refs']), 5)

    def test_first_shot_uses_uploaded_images_without_existing_checkpoint(self):
        class StopBeforeSampling(Exception):
            pass
        with tempfile.TemporaryDirectory(prefix='h3-ref-first-shot-', dir='D:/AI/deployment/logs') as temporary:
            root = Path(temporary)
            run, scratch = root / 'film', root / 'temp'
            run.mkdir()
            scratch.mkdir()
            plan = [{'prompt': 'Follow <Picture 1>.', 'frames': 180, 'continuity': 'continue', 'references': []}]
            refs = {f'shot_01_ref_{index:02d}': torch.zeros((1, 32, 32, 3)) for index in range(1, 6)}
            with mock.patch.object(director.mm, 'unload_all_models'), mock.patch.object(
                    director.MiniMaxH3ReferenceToVideo, 'execute', side_effect=StopBeforeSampling) as encode:
                with self.assertRaises(StopBeforeSampling):
                    director.H3MP4ShotEditor()._render(run, scratch, plan, None, None, None, None,
                        'next_shot', 1, 768, 512, 24, 1, True, {}, {}, '50', refs)
            self.assertEqual(len(encode.call_args.kwargs['ref_images']), 5)
            self.assertIn('<Picture 5>', encode.call_args.args[3])
            self.assertEqual(list(run.iterdir()), [])


unittest.main()
