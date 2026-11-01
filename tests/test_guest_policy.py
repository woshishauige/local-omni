"""访客能力开关：一处决策，默认按所有者这次的要求全开，随时可单独收回。

The permission model used to be a hard owner/guest split: a shared visitor could chat and nothing
else. The owner asked for a shared session that can *use* the machine — switch modes (and with
them the model and its plugins), generate, and handle its own files — while the host's own
controls stay the owner's. These tests pin the four switches, their wording, their persistence,
and the migration from the single upload switch that came before them.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.share import guest_policy


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.policy = guest_policy.GuestPolicy(directory=Path(self.directory.name))

    def test_every_feature_is_open_by_default(self):
        """所有者的要求：访客能用整套功能，不需要先逐个打开。"""
        snapshot = self.policy.snapshot()
        self.assertEqual(set(snapshot["features"]), set(guest_policy.FEATURES))
        self.assertTrue(all(snapshot["features"].values()))
        for name in guest_policy.FEATURES:
            self.assertTrue(self.policy.allows(name))
            self.assertTrue(snapshot["labels"][name])
            self.assertTrue(snapshot["descriptions"][name])

    def test_a_feature_can_be_closed_and_reopened_on_its_own(self):
        self.policy.set_feature("media", False)
        self.assertFalse(self.policy.allows("media"))
        self.assertTrue(self.policy.allows("chat"))
        self.assertTrue(self.policy.allows("switch"))
        self.policy.set_feature("media", True)
        self.assertTrue(self.policy.allows("media"))

    def test_the_choice_survives_a_restart(self):
        self.policy.set_feature("switch", False)
        self.policy.set_feature("storage", False)
        reopened = guest_policy.GuestPolicy(directory=Path(self.directory.name))
        self.assertFalse(reopened.allows("switch"))
        self.assertFalse(reopened.allows("storage"))
        self.assertTrue(reopened.allows("chat"))
        stored = json.loads((Path(self.directory.name) / guest_policy.FILE_NAME).read_text("utf-8"))
        self.assertEqual(stored["features"]["switch"], False)

    def test_set_all_flips_everything(self):
        self.policy.set_all(False)
        self.assertEqual(set(self.policy.snapshot()["features"].values()), {False})
        self.policy.set_all(True)
        self.assertEqual(set(self.policy.snapshot()["features"].values()), {True})

    def test_an_unknown_feature_is_refused_rather_than_ignored(self):
        with self.assertRaises(ValueError):
            self.policy.allows("teleport")
        with self.assertRaises(ValueError):
            self.policy.set_feature("teleport", True)

    def test_each_refusal_names_what_would_fix_it(self):
        """"该操作仅限本机管理员"读起来像"你不该存在"，所以每个开关都有自己的说法。"""
        for name in guest_policy.FEATURES:
            wording = self.policy.refusal(name)
            self.assertIn("机主", wording)
            self.assertTrue(wording.endswith("。"))

    def test_the_old_upload_switch_is_honoured(self):
        """上一个开关是所有者对"上传"这件事的明确决定，开或关都照办（只读一次）。"""
        legacy = Path(self.directory.name) / guest_policy.LEGACY_FILE_NAME
        legacy.write_text(json.dumps({"policy": "off", "policy_changed_at": 12.0}), encoding="utf-8")
        closed = guest_policy.GuestPolicy(directory=Path(self.directory.name))
        self.assertFalse(closed.allows("storage"))
        # ...while the other three start open, because that is what the owner just asked for.
        self.assertTrue(closed.allows("media"))
        self.assertTrue(closed.allows("switch"))
        # Reading it wrote the new document, and from then on that is the authority: an edited
        # legacy file no longer changes anything.
        self.assertTrue((Path(self.directory.name) / guest_policy.FILE_NAME).is_file())

        with tempfile.TemporaryDirectory() as other:
            (Path(other) / guest_policy.LEGACY_FILE_NAME).write_text(
                json.dumps({"policy": "guests"}), encoding="utf-8")
            reopened = guest_policy.GuestPolicy(directory=Path(other))
            self.assertTrue(reopened.allows("storage"))

    def test_the_live_policy_can_be_rebound_for_a_test(self):
        with patch.object(guest_policy, "_POLICY", self.policy):
            guest_policy.policy().set_feature("chat", False)
            self.assertFalse(guest_policy.allows("chat"))
            self.assertTrue(guest_policy.allows_any("chat", "media"))


class FeatureSetTests(unittest.TestCase):
    """The four names are an interface: routes, the panel and the docs all use them."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_the_feature_names_are_stable(self):
        self.assertEqual(guest_policy.FEATURES, ("chat", "media", "switch", "storage"))

    def test_allows_any_covers_a_route_that_serves_two_kinds_of_file(self):
        policy = guest_policy.GuestPolicy(directory=Path(self.directory.name))
        policy.set_feature("media", False)
        policy.set_feature("storage", False)
        self.assertFalse(policy.allows_any("media", "storage"))
        policy.set_feature("storage", True)
        self.assertTrue(policy.allows_any("media", "storage"))


if __name__ == "__main__":
    unittest.main()
