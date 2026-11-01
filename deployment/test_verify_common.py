"""Tests for verify_common: the wait_ready / poll_image_job loops the live scripts use.

The loops matter because every live verification script depends on them and a bug
here looks like "the gateway is broken" instead of "the helper is broken". These
tests drive them against a real local HTTP server on an ephemeral port, so request
handling, retry and phase reporting are exercised for real.

Each test owns the server's script through a fresh ``_State`` object, so no test
can be affected by how many requests an earlier one made. (An earlier revision
shared call counters and only reset them in ``setUp``; a test that ran first could
then decide what the next one saw.)
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

import verify_common


class _State:
    """The fake gateway's script for exactly one test."""

    def __init__(self) -> None:
        # wait_ready: report "loading and busy" until ready_after, then ready.
        self.ready_after = 0.0
        self.status: dict = {"state": "ready", "active_requests": 0}
        self.status_calls = 0
        # poll_image_job: canned answers, popped one per poll.
        self.progress_sequence: list = []
        self.progress_sequence_default: dict = {"busy": False, "job": {"id": "job-1", "state": "running"}}
        self.progress_calls = 0
        self.fail_progress_until = 0


HOLDER = {"state": _State()}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:  # keep the test output clean
        pass

    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        state = HOLDER["state"]
        if self.path.startswith("/local/status"):
            state.status_calls += 1
            if time.monotonic() < state.ready_after:
                self._send({"state": "loading", "active_requests": 1})
            else:
                self._send(state.status)
            return
        if self.path.startswith("/local/image/progress"):
            state.progress_calls += 1
            if state.progress_calls <= state.fail_progress_until:
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if state.progress_sequence:
                self._send(state.progress_sequence.pop(0))
                return
            self._send(state.progress_sequence_default)
            return
        self._send({"error": "not found"}, status=404)


class VerifyCommonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.original = verify_common.GATEWAY
        verify_common.GATEWAY = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        verify_common.GATEWAY = cls.original
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        HOLDER["state"] = _State()

    @property
    def state(self) -> _State:
        return HOLDER["state"]

    def test_wait_ready_waits_out_a_loading_and_busy_backend(self) -> None:
        self.state.ready_after = time.monotonic() + 0.15
        status = verify_common.wait_ready(seconds=10, interval=0.01)
        self.assertEqual(status["state"], "ready")
        # The gate cannot be satisfied on the first request, so the loop really
        # had to poll instead of returning whatever it found.
        self.assertGreater(self.state.status_calls, 1)

    def test_wait_ready_reports_the_last_status_on_timeout(self) -> None:
        self.state.ready_after = time.monotonic() + 600
        with self.assertRaises(RuntimeError) as caught:
            verify_common.wait_ready(seconds=0.15, interval=0.01)
        self.assertIn("never became ready", str(caught.exception))
        self.assertIn("loading", str(caught.exception))

    def test_wait_ready_treats_an_active_request_as_not_ready(self) -> None:
        self.state.status = {"state": "ready", "active_requests": 1}
        with self.assertRaises(RuntimeError):
            verify_common.wait_ready(seconds=0.15, interval=0.01)

    def test_poll_image_job_returns_the_settled_job_and_reports_phases(self) -> None:
        seen: list = []
        # First poll: still running. Second poll: the job settles, which also
        # changes phase - the callback must fire for both transitions.
        self.state.progress_sequence = [
            {"busy": False, "job": {"id": "job-1", "state": "running"}},
            {"busy": False, "job": {"id": "job-1", "state": "completed", "phase": "done"}},
        ]
        job = verify_common.poll_image_job("/local/image/progress", "job-1", timeout=5,
                                           interval=0.01, on_phase=seen.append)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(sorted(j["state"] for j in seen), ["completed", "running"])

    def test_poll_image_job_ignores_a_busy_queue_and_another_job(self) -> None:
        self.state.progress_sequence_default = {"busy": True, "job": {"id": "job-1", "state": "completed"}}
        with self.assertRaises(RuntimeError):
            verify_common.poll_image_job("/local/image/progress", "job-1", timeout=0.1, interval=0.01)
        self.state.progress_sequence_default = {"busy": False, "job": {"id": "job-2", "state": "completed"}}
        with self.assertRaises(RuntimeError):
            verify_common.poll_image_job("/local/image/progress", "job-1", timeout=0.1, interval=0.01)

    def test_poll_image_job_retries_transient_request_failures(self) -> None:
        self.state.fail_progress_until = 2
        self.state.progress_sequence_default = {"busy": False, "job": {"id": "job-1", "state": "completed"}}
        job = verify_common.poll_image_job("/local/image/progress", "job-1", timeout=5, interval=0.01)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(self.state.progress_calls, 3, "two failures then the answer")

    def test_poll_image_job_timeout_mentions_the_request_error(self) -> None:
        self.state.fail_progress_until = 10_000
        with self.assertRaises(RuntimeError) as caught:
            verify_common.poll_image_job("/local/image/progress", "job-1", timeout=0.15, interval=0.01)
        self.assertIn("last request error", str(caught.exception))

    def test_poll_image_job_without_retry_raises_immediately(self) -> None:
        self.state.fail_progress_until = 1
        with self.assertRaises(requests.HTTPError):
            verify_common.poll_image_job("/local/image/progress", "job-1", timeout=1, interval=0.01, retry=False)

    def test_get_json_reports_status_errors(self) -> None:
        with self.assertRaises(requests.HTTPError):
            verify_common.get_json("/local/missing", timeout=2)


if __name__ == "__main__":
    unittest.main()
