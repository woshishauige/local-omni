"""Tests for the runtime updater's policy — the part that decides whether to touch production.

The network, GPU and config-editing paths cannot run in a unit test; the *decision* can, and it is
the part where a mistake does real damage. These cases are the ones that matter: a faster build
that answers with garbage must be rejected, a slower-but-correct build must be kept, and a missing
measurement must never be read as "no harm".
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import runtime_updater as updater  # noqa: E402


def probe(correct=13, unparsed=0, thinking=0, garbage=0, items=20):
    return {"correct": correct, "unparsed": unparsed, "thinking": thinking,
            "garbage": garbage, "items": items}


def bench(pp, tg, rounds=None):
    return {"pp": pp, "tg": tg, "rounds": rounds or {"pp": [pp], "tg": [tg]}}


class DecideTests(unittest.TestCase):
    def test_prompt_gain_alone_is_enough(self):
        adopt, reason = updater.decide(probe(), probe(), bench(2465, 48.0), bench(2247, 48.2))
        self.assertTrue(adopt, reason)
        self.assertIn("pp", reason)

    def test_generation_gain_alone_is_enough(self):
        adopt, reason = updater.decide(probe(), probe(), bench(2200, 54.0), bench(2200, 48.0))
        self.assertTrue(adopt, reason)

    def test_no_gain_keeps_the_installed_build(self):
        adopt, reason = updater.decide(probe(), probe(), bench(2250, 48.1), bench(2247, 48.2))
        self.assertFalse(adopt)
        self.assertIn("提升不足", reason)

    def test_garbage_output_is_rejected_even_when_faster(self):
        adopt, reason = updater.decide(probe(unparsed=3, garbage=9, correct=6), probe(),
                                       bench(2600, 55.0), bench(2247, 48.2))
        self.assertFalse(adopt)
        self.assertIn("异常", reason)

    def test_correctness_drop_beyond_budget_is_rejected(self):
        adopt, reason = updater.decide(probe(correct=10), probe(correct=13),
                                       bench(2600, 55.0), bench(2247, 48.2))
        self.assertFalse(adopt)
        self.assertIn("正确率下降", reason)

    def test_small_correctness_drop_is_tolerated_when_faster(self):
        adopt, _ = updater.decide(probe(correct=11), probe(correct=13),
                                  bench(2600, 55.0), bench(2247, 48.2))
        self.assertTrue(adopt)

    def test_missing_benchmark_is_never_read_as_no_harm(self):
        adopt, reason = updater.decide(probe(), probe(), {}, bench(2247, 48.2))
        self.assertFalse(adopt)
        self.assertIn("基准数据缺失", reason)

    def test_failed_probe_is_rejected(self):
        adopt, reason = updater.decide({"error": "load timeout"}, probe(),
                                       bench(2600, 55.0), bench(2247, 48.2))
        self.assertFalse(adopt)
        self.assertIn("探针失败", reason)


class VersionAndAssetTests(unittest.TestCase):
    def test_build_number_is_read_from_the_binary_banner(self):
        # both builds print "version: x (build NNNNN, commit ...)" on stderr
        import re
        match = re.search(r"build (\d+)", "version: 0.6.0-dev (build 11935, commit f2918cabb)")
        self.assertEqual(int(match.group(1)), 11935)

    def test_stock_asset_filter_prefers_the_installed_cuda_generation(self):
        pattern = updater.TRACKS["stock"]["asset"]
        names = ["llama-b11935-bin-win-cuda-13.4-x64.zip",
                 "llama-b11935-bin-win-cuda-12.4-x64.zip",
                 "llama-b11935-bin-ubuntu-cuda-13.4-x64.tar.gz",
                 "cudart-llama-bin-win-cuda-13.4-x64.zip"]
        matched = [name for name in names if pattern.search(name)]
        self.assertEqual(matched, ["llama-b11935-bin-win-cuda-13.4-x64.zip",
                                   "llama-b11935-bin-win-cuda-12.4-x64.zip"])

    def test_fork_asset_filter_matches_the_prism_naming(self):
        pattern = updater.TRACKS["fork"]["asset"]
        self.assertTrue(pattern.search(
            "llama-prism-b10826-c9c34bc-bin-win-cuda-13.3-x64.zip"))
        self.assertFalse(pattern.search("llama-b11935-bin-win-cuda-13.4-x64.zip"))


if __name__ == "__main__":
    unittest.main()
