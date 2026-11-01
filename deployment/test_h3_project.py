import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

path = Path('D:/AI/ComfyUI-BSAI_v010/ComfyUI/custom_nodes/ComfyUI-H3-LocalDirector/project.py')
spec = importlib.util.spec_from_file_location('h3_project', path)
project = importlib.util.module_from_spec(spec)
spec.loader.exec_module(project)


class ProjectTests(unittest.TestCase):
    def test_frame_grid_and_per_shot_duration(self):
        plan = project.parse_storyboard(json.dumps({'shots': [
            {'prompt': 'first'}, {'prompt': 'second', 'seconds': 10},
        ]}), 8, 'shared')
        self.assertEqual([shot['frames'] for shot in plan], [192, 243])
        self.assertEqual(plan[1]['prompt'], 'shared\nsecond')

    def test_generated_and_plain_storyboards(self):
        plain = project.parse_storyboard('first\n---\nsecond', 8, '')
        generated = project.parse_storyboard('{"prompts":["first","second"]}', 8, '')
        self.assertEqual(plain, generated)

    def test_invalid_storyboards_fail_before_render(self):
        for text in ('[]', '{"shots":[{}]}', '{"shots":[{"prompt":"x","seconds":30}]}',
                     '{"shots":[{"prompt":"x","references":[true]}]}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                project.parse_storyboard(text, 8, '')

    def test_qwen_shot_headings_and_fenced_json(self):
        plan = project.parse_storyboard('[Shot 1] Car moves.\n\n[Shot 2] Car stops.\n\n[Shot 3] Car moves again.', 8, '')
        self.assertEqual(len(plan), 3)
        self.assertEqual(plan[1]['prompt'], 'integrated_multimodal_description: [Shot 2] Car stops.')
        self.assertEqual(project.parse_storyboard('```json\n{"shots":[{"prompt":"car"}]}\n```', 8, '')[0]['prompt'], 'car')
        with self.assertRaises(json.JSONDecodeError):
            project.parse_storyboard('{"shots":[', 8, '')

    def test_project_stays_inside_output(self):
        root = Path(tempfile.gettempdir()) / 'h3-project-test'
        for name in ('../other', 'a/b', 'a\\b', '.', 'name.', ' name'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                project.project_path(root, name)
        self.assertEqual(project.project_path(root, 'episode-1'), root.resolve() / 'episode-1')

    def test_resume_append_and_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            record = {'signature': 'first'}
            for kind in ('video', 'audio', 'latent'):
                file = run / kind
                file.write_bytes(kind.encode())
                record.update({kind: kind, kind + '_sha256': project.digest_file(file)})
            project.check_completed(run, [record], ['first', 'second'])
            with self.assertRaisesRegex(ValueError, 'changed'):
                project.check_completed(run, [record], ['different'])
            (run / 'audio').write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError, 'audio'):
                project.check_completed(run, [record], ['first'])


unittest.main()
