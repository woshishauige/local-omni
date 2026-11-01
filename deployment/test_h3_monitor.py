import base64
import importlib.util
from io import BytesIO
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path('D:/AI/ComfyUI-BSAI_v010/ComfyUI')
sys.path.insert(0, str(ROOT))
import comfy.cli_args
comfy.cli_args.args.cpu = True
import torch
from PIL import Image
from aiohttp import web
from comfy.latent_formats import MiniMaxH3AV
from comfy.nested_tensor import NestedTensor
from comfy_execution.utils import CurrentNodeContext
from server import PromptServer

PromptServer.instance = Mock(routes=web.RouteTableDef())
spec = importlib.util.spec_from_file_location('h3_monitor', ROOT / 'custom_nodes/ComfyUI-H3-LocalDirector/monitor.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.monitor = module.H3Monitor()
        with CurrentNodeContext('test-prompt', '50'):
            self.monitor.begin('project', 1, 4, 6, 'same-render-settings')

    def test_nested_video_preview_covers_start_middle_end(self):
        model = Mock()
        model.model.latent_format = MiniMaxH3AV()
        video = torch.zeros((1, 24, 3, 4, 6))
        video[:, 9, 0] = -1
        video[:, 9, 2] = 1
        x0 = NestedTensor([video, torch.zeros((1, 32, 10))])
        before = video.clone()
        callback = self.monitor.callback(model, 6)
        with patch.object(module.time, 'monotonic', side_effect=[10, 10, 12, 12, 16, 16]):
            for step in range(3):
                callback(step, x0, None, 6)
        state = self.monitor.state
        self.assertEqual(state['prompt_id'], 'test-prompt')
        self.assertEqual(state['step'], 3)
        self.assertEqual(state['sampling_eta_seconds'], 9)
        self.assertIsNone(state['eta_seconds'])
        image = Image.open(BytesIO(base64.b64decode(state['preview'].split(',')[1])))
        self.assertEqual(image.size, (72, 16))
        self.assertLess(image.getpixel((10, 8))[0], image.getpixel((60, 8))[0])
        self.assertTrue(torch.equal(video, before))

    def test_phase_resets_eta_and_completed_timing_is_bounded(self):
        self.monitor.publish(sampling_eta_seconds=20, eta_seconds=30)
        self.monitor.phase('decoding')
        self.assertIsNone(self.monitor.state['sampling_eta_seconds'])
        self.monitor.finish('completed')
        self.assertIn('same-render-settings', self.monitor.completed_timings)
        for key in range(15):
            self.monitor.begin('project', 1, 4, 6, str(key))
            self.monitor.finish('completed')
        self.assertEqual(len(self.monitor.completed_timings), 12)

    def test_cancel_marks_terminal_and_preserves_preview(self):
        self.monitor.publish(preview='last-frame')
        self.monitor.finish('interrupted')
        self.assertEqual(self.monitor.state['phase'], 'interrupted')
        self.assertEqual(self.monitor.state['preview'], 'last-frame')

    def test_sampler_preserves_noise_mask_and_output_contract(self):
        samples = torch.zeros((1, 24, 3, 4, 6))
        result = samples + 2
        guider = Mock()
        guider.sample.return_value = result
        noise = Mock(seed=42)
        latent = {'samples': samples, 'noise_mask': 'mask', 'batch_index': [0],
                  'downscale_ratio_spacial': 16, 'downscale_ratio_temporal': 4}
        with patch.object(module.comfy.sample, 'fix_empty_latent_channels', return_value=samples), \
             patch.object(module.monitor, 'callback', return_value='callback'):
            output = module.sample_monitored(noise, guider, 'euler', torch.tensor([1., .5, 0.]), latent)
        self.assertTrue(torch.equal(output['samples'], result))
        self.assertNotIn('downscale_ratio_spacial', output)
        self.assertIn('downscale_ratio_spacial', latent)
        self.assertEqual(guider.sample.call_args.kwargs['seed'], 42)
        self.assertEqual(guider.sample.call_args.kwargs['denoise_mask'], 'mask')
        self.assertEqual(output['batch_index'], [0])


if __name__ == '__main__':
    unittest.main()
