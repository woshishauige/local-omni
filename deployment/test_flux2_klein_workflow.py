import unittest

from flux2_klein_workflow import build_flux2_klein_prompt


class Flux2KleinWorkflowTests(unittest.TestCase):
    def test_text_to_image_has_no_reference_branch(self):
        prompt = build_flux2_klein_prompt("a red fox in snow")['prompt']
        self.assertNotIn("LoadImage", {node['class_type'] for node in prompt.values()})
        self.assertEqual(prompt['10']['inputs']['positive'], ['4', 0])

    def test_one_reference_has_one_complete_branch(self):
        prompt = build_flux2_klein_prompt("edit the subject", reference_images=['one.png'])['prompt']
        classes = [node['class_type'] for node in prompt.values()]
        self.assertEqual(classes.count('LoadImage'), 1)
        self.assertEqual(classes.count('ReferenceLatent'), 1)
        self.assertEqual(prompt['10']['inputs']['positive'], ['23', 0])
        self.assertEqual(prompt['23']['inputs']['conditioning'], ['4', 0])

    def test_two_references_are_chained_in_order(self):
        prompt = build_flux2_klein_prompt("combine both", reference_images=['one.png', 'two.png'])['prompt']
        self.assertEqual([prompt['20']['inputs']['image'], prompt['24']['inputs']['image']], ['one.png', 'two.png'])
        self.assertEqual(prompt['27']['inputs']['conditioning'], ['23', 0])
        self.assertEqual(prompt['10']['inputs']['positive'], ['27', 0])

    def test_invalid_reference_count_and_parameters(self):
        with self.assertRaises(ValueError):
            build_flux2_klein_prompt('x', reference_images=['a', 'b', 'c'])
        with self.assertRaises(ValueError):
            build_flux2_klein_prompt('x', width=513)
        with self.assertRaises(ValueError):
            build_flux2_klein_prompt('x', cfg=21)
        with self.assertRaises(ValueError):
            build_flux2_klein_prompt('x', megapixels=0.001)

    def test_negative_seed_is_resolved_to_comfyui_compatible_random_seed(self):
        prompt = build_flux2_klein_prompt('x', seed=-1)['prompt']
        seed = prompt['7']['inputs']['noise_seed']
        self.assertIsInstance(seed, int)
        self.assertGreaterEqual(seed, 0)

    def test_seed_upper_bound_is_validated(self):
        with self.assertRaises(ValueError):
            build_flux2_klein_prompt('x', seed=18446744073709551616)


if __name__ == '__main__':
    unittest.main()
