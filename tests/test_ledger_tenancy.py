"""One history per mode *and* one history per caller.

The mode half is guarded by test_mode_isolation / test_context_gateway; this file guards the
caller half, which exists because of a leak measured over the LAN: a device holding nothing but
the share passphrase listed the owner's conversations and read one back in full by handing over
the id it had just been given.

Run: python -X utf8 -m pytest test_ledger_tenancy.py -q
"""


from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import unittest
from types import SimpleNamespace

from app import mode_server as module


class FakeRequest:
    """Enough of a Starlette request for the identity helpers, and nothing more."""

    def __init__(self, host="10.36.22.5", cookies=None, query=None):
        self.client = SimpleNamespace(host=host)
        self.cookies = dict(cookies or {})
        self.query_params = dict(query or {})
        self.headers = {}


def guest_request(token="guest-session-token-0123456789abcdef"):
    return FakeRequest(cookies={module.ACCESS_COOKIE: token})


def owner_request():
    return FakeRequest(host="127.0.0.1")


class NamespaceTests(unittest.TestCase):
    def test_owner_has_no_namespace(self):
        self.assertEqual(module.guest_namespace(owner_request()), "")
        self.assertEqual(module.tenant_conversation(owner_request(), "abc"), "text·abc")
        self.assertEqual(module.tenant_filter(owner_request(), "voice"), "voice·")

    def test_guest_key_carries_its_session(self):
        request = guest_request()
        self.assertEqual(module.guest_namespace(request), "guest-guest-session-token-0123·")
        self.assertEqual(module.tenant_conversation(request, "abc"),
                         "text·guest-guest-session-token-0123·abc")

    def test_two_guests_cannot_share_a_key(self):
        one = module.tenant_conversation(guest_request("token-aaaaaaaaaaaaaaaaaaaa"), "abc")
        two = module.tenant_conversation(guest_request("token-bbbbbbbbbbbbbbbbbbbb"), "abc")
        self.assertNotEqual(one, two)

    def test_a_guest_cannot_name_the_owners_thread(self):
        """The owner's own id is nested inside the guest's namespace, so it names nothing."""
        request = guest_request("token-aaaaaaaaaaaaaaaaaaaa")
        key = module.tenant_conversation(request, "text·owner-thread")
        self.assertEqual(key, "text·guest-token-aaaaaaaaaaaaaaaaaa·owner-thread")
        self.assertNotEqual(key, "text·owner-thread")

    def test_another_guests_namespace_is_not_stripped_but_nested(self):
        """Handing over a foreign key must not resolve to that tenant's thread."""
        mine = guest_request("token-aaaaaaaaaaaaaaaaaaaa")
        key = module.tenant_conversation(mine, "text·guest-token-bbbbbbbbbbbbbbbbbbbb·secret")
        self.assertEqual(
            key, "text·guest-token-aaaaaaaaaaaaaaaaaa·guest-token-bbbbbbbbbbbbbbbbbbbb·secret")
        self.assertNotEqual(key, "text·guest-token-bbbbbbbbbbbbbbbbbbbb·secret")

    def test_a_row_from_the_own_listing_is_usable_as_it_is(self):
        """Rename/delete read the id out of a listing and hand it straight back."""
        request = guest_request("token-aaaaaaaaaaaaaaaaaaaa")
        listed = module.tenant_conversation(request, "abc", "voice")
        self.assertEqual(module.tenant_conversation(request, listed, "voice"), listed)
        # …with or without the mode repeated by the caller.
        self.assertEqual(module.tenant_conversation(request, listed, "text"), listed)

    def test_the_mode_segment_stays_first(self):
        """Every existing prefix filter keeps working: the tenant goes inside the id."""
        request = guest_request("token-aaaaaaaaaaaaaaaaaaaa")
        key = module.tenant_conversation(request, "abc", "vision")
        self.assertTrue(key.startswith("vision·"))
        self.assertTrue(key.startswith(module.tenant_filter(request, "vision")))

    def test_listing_prefix_is_the_callers_own(self):
        request = guest_request("token-aaaaaaaaaaaaaaaaaaaa")
        prefix = module.tenant_filter(request, "voice")
        self.assertEqual(prefix, "voice·guest-token-aaaaaaaaaaaaaaaaaa·")
        own = module.tenant_conversation(request, "mine", "voice")
        theirs = module.tenant_conversation(guest_request("token-bbbbbbbbbbbbbbbbbbbb"), "theirs",
                                           "voice")
        self.assertTrue(own.startswith(prefix))
        self.assertFalse(theirs.startswith(prefix))

    def test_a_session_less_caller_gets_an_empty_namespace_not_the_owners(self):
        request = FakeRequest(cookies={})
        self.assertEqual(module.guest_namespace(request), "guest-anonymous·")
        self.assertEqual(module.tenant_conversation(request, "abc"), "text·guest-anonymous·abc")

    def test_no_request_is_the_owners_path(self):
        """Calls from owner tooling without a request object keep the historical keys."""
        self.assertEqual(module.tenant_conversation(None, "abc", "voice"), "voice·abc")
        self.assertEqual(module.tenant_filter(None), "text·")


class ListingTests(unittest.TestCase):
    def rows(self):
        return [{"id": "text·owner-secret", "title": "私人口令"},
                {"id": "text·guest-token-aaaaaaaaaaaaaaaa·theirs", "title": "访客的"}]

    def test_owner_listing_does_not_show_guest_rows(self):
        self.assertEqual([row["id"] for row in module.caller_rows(owner_request(), self.rows())],
                         ["text·owner-secret"])

    def test_guest_rows_are_already_fenced_so_nothing_is_stripped(self):
        request = guest_request("token-aaaaaaaaaaaaaaaaaaaa")
        rows = [{"id": "text·guest-token-aaaaaaaaaaaaaaaa·mine"}]
        self.assertEqual(module.caller_rows(request, rows), rows)

    def test_guest_marker_is_read_from_the_id_half(self):
        self.assertTrue(module.is_guest_key("voice·guest-abc·x"))
        self.assertFalse(module.is_guest_key("voice·owner-thread"))
        self.assertFalse(module.is_guest_key(""))


if __name__ == "__main__":
    unittest.main()
