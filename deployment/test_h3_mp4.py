from fractions import Fraction
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, 'D:/AI/ComfyUI-BSAI_v010/ComfyUI')
import comfy.cli_args
comfy.cli_args.args.cpu = True
import av
import numpy as np
import torch
from comfy_api.latest import InputImpl, Types

path = Path('D:/AI/ComfyUI-BSAI_v010/ComfyUI/custom_nodes/ComfyUI-H3-LocalDirector/mp4_project.py')
spec = importlib.util.spec_from_file_location('h3_mp4_project', path)
project = importlib.util.module_from_spec(spec)
spec.loader.exec_module(project)


class MP4Tests(unittest.TestCase):
    def test_optional_reference_slots_skip_empty_and_keep_five_per_shot(self):
        identity = torch.zeros((1, 8, 8, 3))
        images = {f'shot_02_ref_{slot:02d}': torch.full((1, 8, 8, 3), slot / 10)
                  for slot in range(1, 6)}
        images['shot_01_ref_01'] = torch.ones((1, 8, 8, 3))
        references, descriptors = project.collect_reference_images(images, 2, identity)
        self.assertEqual(list(references), [f'ref_image_{slot}' for slot in range(6)])
        self.assertEqual(len(descriptors), 6)
        self.assertEqual(descriptors[-1]['slot'], 'continuity_identity')
        self.assertEqual([row['slot'] for row in descriptors[:-1]],
                         [f'shot_02_ref_{slot:02d}' for slot in range(1, 6)])
        references, descriptors = project.collect_reference_images({}, 3)
        self.assertEqual(references, {})
        self.assertEqual(descriptors, [])
        references, descriptors = project.collect_reference_images(
            {'shot_04_ref_01': None, 'shot_04_ref_05': identity.to(torch.bfloat16)}, 4)
        self.assertEqual(list(references), ['ref_image_0'])
        self.assertEqual(descriptors[0]['slot'], 'shot_04_ref_05')
        self.assertEqual(descriptors[0]['picture'], 1)

    @unittest.skipUnless(os.name == 'nt', 'Windows file permission regression')
    def test_publish_inherits_destination_permissions_and_preserves_failed_redo(self):
        import win32security as security

        def descriptor(path):
            return security.GetFileSecurity(str(path), security.DACL_SECURITY_INFORMATION)

        with tempfile.TemporaryDirectory(prefix='h3-permission-test-', dir='D:/AI/deployment/logs') as directory:
            root = Path(directory)
            security.SetNamedSecurityInfo(str(root), security.SE_FILE_OBJECT,
                security.DACL_SECURITY_INFORMATION | security.UNPROTECTED_DACL_SECURITY_INFORMATION,
                None, None, descriptor(root).GetSecurityDescriptorDacl(), None)
            source, target, control = root / 'private.mp4', root / 'published.mp4', root / 'control'
            source.write_bytes(b'new video bytes')
            target.write_bytes(b'previous video bytes')
            control.write_bytes(b'ordinary inherited permissions')
            security.SetNamedSecurityInfo(str(source), security.SE_FILE_OBJECT,
                security.DACL_SECURITY_INFORMATION | security.PROTECTED_DACL_SECURITY_INFORMATION,
                None, None, descriptor(source).GetSecurityDescriptorDacl(), None)
            self.assertTrue(descriptor(source).GetSecurityDescriptorControl()[0] & security.SE_DACL_PROTECTED)
            with mock.patch.object(project.shutil, 'copyfileobj', side_effect=OSError('test write failure')):
                with self.assertRaises(OSError):
                    project.publish_mp4(source, target)
            self.assertEqual(target.read_bytes(), b'previous video bytes')
            self.assertEqual(list(root.glob('.h3-publish-*')), [])
            project.publish_mp4(source, target)
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertFalse(descriptor(target).GetSecurityDescriptorControl()[0] & security.SE_DACL_PROTECTED)
            def dacl_text(path):
                return security.ConvertSecurityDescriptorToStringSecurityDescriptor(
                    descriptor(path), security.SDDL_REVISION_1, security.DACL_SECURITY_INFORMATION)
            self.assertEqual(dacl_text(target), dacl_text(control))
            self.assertEqual(list(root.glob('.h3-publish-*')), [])

    def test_frame_budget(self):
        self.assertEqual(project.generation_frames(180, False), 192)
        self.assertEqual(project.generation_frames(180, True), 209)
        with self.assertRaises(ValueError):
            project.generation_frames(500, True)

    def test_mp4_checkpoint_context_and_clean_30_second_assembly(self):
        with tempfile.TemporaryDirectory(prefix='h3-mp4-test-') as root:
            root = Path(root)
            run, scratch = root / "project's film", root / 'temp'
            run.mkdir()
            scratch.mkdir()
            rate = 32000
            images = torch.zeros((180, 32, 32, 3))
            images[:, :, :, 0] = torch.linspace(0.1, 0.9, 180)[:, None, None]
            wave = torch.from_numpy((0.05 * np.sin(2 * np.pi * 440 * np.arange(240000) / rate)).astype(np.float32))
            audio = {'waveform': wave.repeat(2, 1).unsqueeze(0), 'sample_rate': rate}
            components = Types.VideoComponents(images=images, audio=audio, frame_rate=Fraction(24))
            clips, checkpoints = [], []
            for index in range(1, 5):
                cp = {'format': project.FORMAT, 'shot_index': index, 'width': 32, 'height': 32,
                      'frames': 180, 'sample_rate': rate, 'channels': 2}
                clip = run / f'shot_{index:02d}.mp4'
                InputImpl.VideoFromComponents(components).save_to(str(clip), codec='h264', crf=19,
                    metadata={'h3_checkpoint': cp, 'workflow': {'nodes': []}})
                self.assertEqual(project.read_checkpoint(clip, index), cp)
                clips.append(clip)
                checkpoints.append(cp)
            before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in clips]
            pixels, tail = project.video_context(clips[0], 32, 32)
            self.assertEqual(tuple(pixels.shape), (22, 32, 32, 3))
            self.assertEqual(tuple(tail['waveform'].shape), (1, 2, 32000))
            movie = project.assemble_mp4s(run, clips, checkpoints, {'workflow': {'nodes': []}}, scratch)
            self.assertEqual([hashlib.sha256(p.read_bytes()).hexdigest() for p in clips], before)
            self.assertEqual(list(scratch.iterdir()), [])
            self.assertEqual(sorted(p.name for p in run.iterdir()), ['master.mp4'] + [f'shot_{i:02d}.mp4' for i in range(1, 5)])
            with av.open(str(movie)) as source:
                count = 0
                for count, frame in enumerate(source.decode(video=0), 1):
                    self.assertAlmostEqual(float(frame.pts * frame.time_base), (count - 1) / 24, places=4)
                self.assertEqual(count, 720)
                self.assertEqual(len(json.loads(source.metadata['h3_film'])), 4)
            with av.open(str(movie)) as source:
                audio_stream = source.streams.audio[0]
                self.assertAlmostEqual(float(audio_stream.duration * audio_stream.time_base), 30, places=2)
            with self.assertRaises(ValueError):
                project.read_checkpoint(clips[0], 2)


unittest.main()
