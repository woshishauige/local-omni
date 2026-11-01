"""Stopping the service must actually give the card back.

The requirement is that closing the service leaves nothing of it on the GPU. Three
processes can hold video memory at that moment — the chat backend, the voice recogniser
and voice mode's own copy of the 9B — plus a synthesis worker that holds ~1.1 GB of
weights. The one that used to survive a stop unnoticed is a recogniser orphaned by a
killed parent, which is exactly what `release_card` reaps.

Classification is tested against a fake process, because it is pure logic over a command
line. Termination is tested against real processes, with `is_ours` spied on rather than
faked: the port decision is the whole point of that function, so the spy asserts it was
consulted and what it answered for each candidate.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import os
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

import psutil

from app import qwen_service as service

STUB_PORT = 65001
OTHER_PORT = 65002


def make_stub(folder, port):
    """A real, living process to terminate: cmd.exe copied to the name llama-server.exe.

    Two dead ends are recorded here because both looked reasonable and neither worked.
    A copied python.exe cannot find its own DLLs outside its installation and dies at
    once; and cmd.exe rejects an extra `--port` argument outright ("选择 --port 不正确"),
    so the fake command line for the *name* has to come from the fake process below while
    the real one only has to stay alive.
    """
    home = Path(folder) / f"stub-{port}"
    home.mkdir(exist_ok=True)
    target = home / "llama-server.exe"
    shutil.copyfile(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe", target)
    # A plain `ping` is what stays alive here: `timeout` and `choice` demand an interactive
    # console and exited immediately, leaving the assertions with nothing to look at. The count
    # is generous (90 ≈ 89 s) because the full suite can be slow enough to outlive a shorter
    # one, which showed up as a rare "the stub died on its own" failure. Teardown kills it, so
    # a long life costs nothing.
    process = subprocess.Popen(
        [str(target), "/c", "ping -n 90 127.0.0.1"],
        cwd=str(home), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return process, target


class FakeProcess:
    """The slice of psutil's process interface that classification reads."""

    def __init__(self, pid, image, command):
        self.pid = pid
        self.info = {"name": Path(image).name, "exe": str(image), "cmdline": list(command)}
        self._command = list(command)

    def cmdline(self):
        return self._command

    def exe(self):
        return self.info["exe"]


def model_command(port, image=r"D:\somewhere\llama-server.exe"):
    return [str(image), "-m", "model.gguf", "--host", "127.0.0.1", "--port", str(port)]


class ProcessClassificationTests(unittest.TestCase):
    """Which running processes this service may terminate, decided without side effects."""

    def ours(self, port):
        return service.is_ours(FakeProcess(4242, r"D:\AI\Qwen-Local\runtime\llama-server.exe",
                                           model_command(port)))

    def test_a_model_process_on_one_of_our_ports_is_ours(self):
        with patch.object(service, "model_ports", return_value=(8082, 8097, 8098)):
            for port in (8082, 8097, 8098):
                with self.subTest(port=port):
                    self.assertTrue(self.ours(port))

    def test_a_model_process_on_another_port_is_left_alone(self):
        with patch.object(service, "model_ports", return_value=(8082, 8097, 8098)):
            # Someone else's llama-server is not this service's to kill, even though it
            # has the same image name and would otherwise look identical.
            self.assertFalse(self.ours(65002))

    def test_only_the_ports_that_are_ours_are_claimed(self):
        with patch.object(service, "model_ports", return_value=(8097,)):
            self.assertFalse(self.ours(8082))   # the chat backend, not voice's
            self.assertTrue(self.ours(8097))    # the recogniser, which is voice's

    def test_something_that_is_not_a_model_server_is_never_ours(self):
        with patch.object(service, "model_ports", return_value=(8082,)):
            python = FakeProcess(1, r"D:\AI\ComfyUI-BSAI_v010\python\python.exe",
                                 ["python.exe", "main.py", "--port", "8082"])
            self.assertFalse(service.is_ours(python))

    def test_a_model_image_without_the_flag_is_not_ours(self):
        with patch.object(service, "model_ports", return_value=(8082,)):
            missing = FakeProcess(2, r"D:\somewhere\llama-server.exe",
                                  [r"D:\somewhere\llama-server.exe", "-m", "model.gguf"])
            self.assertFalse(service.is_ours(missing))


class ReleaseCardTests(unittest.TestCase):
    """Termination, on real processes, with the machine's own servers kept out of it.

    `llama_servers` is patched to return exactly the stubs this test started, and `is_ours`
    is replaced by the one thing this level is about — "is this one of ours?" — answered by
    the port each stub was created for. The command-line classifier itself is covered
    against a fake process in `ProcessClassificationTests`, where it can be asserted
    precisely instead of through the accident of what a stub's own command line looks like.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qwen-release-")
        self.addCleanup(self.drop_temporary)
        self.stubs = {}
        self.readings = [(8500, 3400)]
        self._install("model_ports", return_value=(STUB_PORT,))
        # Patched before any test body runs, not inside `start()`: a test that starts no stub
        # would otherwise see the machine's real servers, and one of these tests promptly
        # terminates whatever it is shown — which is a very bad thing for a test to do.
        self._install("llama_servers", side_effect=self._live_stubs)
        self._install("card_state", side_effect=lambda: self.readings[-1])
        self._install("is_ours", side_effect=self._is_ours)

    def _install(self, name, **kwargs):
        patcher = patch.object(service, name, **kwargs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _is_ours(self, process):
        return self.stubs.get(process.pid) == STUB_PORT

    def drop_temporary(self):
        for process in list(self.stubs):
            try:
                target = psutil.Process(process)
            except psutil.Error:
                continue
            if target.is_running():
                with suppress(psutil.Error):
                    target.kill()
        try:
            self.temporary.cleanup()
        except (PermissionError, OSError):
            # The stub image is a running executable, which Windows refuses to delete while
            # it is alive; a leftover copy in the temp folder is not worth failing a green
            # test over, and the processes themselves are ended above.
            pass

    def start(self, port):
        process, _ = make_stub(self.temporary.name, port)
        self.stubs[process.pid] = port
        return process

    def _live_stubs(self):
        """The stubs still alive, as psutil objects, and nothing else."""
        live = []
        for pid in list(self.stubs):
            try:
                process = psutil.Process(pid)
            except psutil.Error:
                continue
            if process.is_running():
                live.append(process)
        return live

    def readings_are(self, *values):
        """A card reading sequence that repeats its last value for the rest of the call.

        A plain list would run out — release_card reads the card once per second while it
        waits — and the test would die of StopIteration instead of an assertion.
        """
        self.readings = list(values)
        index = getattr(self, "_reading_index", 0)
        value = self.readings[min(index, len(self.readings) - 1)]
        self._reading_index = index + 1
        return value

    def test_release_terminates_our_processes_and_reports_the_card(self):
        process = self.start(STUB_PORT)
        self._install("card_state",
                      side_effect=lambda: self.readings_are((8500, 3400), (1200, 10700)))
        report = service.release_card(wait_seconds=1)

        self.assertTrue(report["card_free"], report)
        self.assertEqual(report["remaining"], [])
        self.assertTrue(any(f"pid={process.pid}" in item for item in report["killed"]), report)
        self.assertEqual(report["free_mib_before"], 3400)
        self.assertEqual(report["free_mib_after"], 10700)
        process.wait(timeout=10)
        self.assertIsNotNone(process.poll())

    def test_release_leaves_another_application_model_running(self):
        mine = self.start(STUB_PORT)
        theirs = self.start(OTHER_PORT)
        report = service.release_card(wait_seconds=0)

        # A model server that is not ours — same image name, a port this service never
        # serves — must survive untouched and must not be reported as released.
        self.assertTrue(report["card_free"], report)
        self.assertNotIn(str(OTHER_PORT), " ".join(report["killed"]))
        self.assertIsNone(theirs.poll())
        self.assertIsNotNone(mine.poll())

    def test_a_process_that_will_not_die_is_reported_not_hidden(self):
        mine = self.start(STUB_PORT)
        with patch.object(service, "terminate_tree", return_value=False):
            report = service.release_card(wait_seconds=0)

        # "Stopped" and "the card is free" are different claims; the report keeps them apart
        # so a stop that leaves a model resident cannot look like a clean one. `terminate_tree`
        # is the one thing faked here, and it is faked to fail.
        self.assertFalse(report["card_free"])
        self.assertTrue(any(f"pid={mine.pid}" in item for item in report["remaining"]), report)
        self.assertIsNone(mine.poll())

    def test_release_is_idempotent(self):
        self.start(STUB_PORT)
        first = service.release_card(wait_seconds=0)
        second = service.release_card(wait_seconds=0)

        self.assertTrue(first["card_free"])
        self.assertTrue(second["card_free"])
        self.assertEqual(second["killed"], [])  # nothing left to release the second time


class CardStateTests(unittest.TestCase):
    def test_a_readable_reading_is_parsed(self):
        completed = subprocess.CompletedProcess([], 0, stdout="8502, 3443\n", stderr="")
        with patch.object(service.subprocess, "run", return_value=completed):
            self.assertEqual(service.card_state(), (8502, 3443))

    def test_an_unreadable_reading_is_none_rather_than_a_guess(self):
        for stdout in ("", "not a reading\n", "8502\n"):
            with self.subTest(stdout=stdout):
                completed = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
                with patch.object(service.subprocess, "run", return_value=completed):
                    self.assertIsNone(service.card_state())
        with patch.object(service.subprocess, "run", side_effect=OSError("no nvidia-smi")):
            self.assertIsNone(service.card_state())


class StopCommandTests(unittest.TestCase):
    """`stop()` must release the card and report it, without becoming harder to trust."""

    def run_stop(self, released):
        with patch.object(service, "file_lock"), \
                patch.object(service, "gateway_status", return_value=None), \
                patch.object(service, "adopt_manager", return_value=None), \
                patch.object(service, "owned_process", return_value=None), \
                patch.object(service, "owned_backend", return_value=None), \
                patch.object(service, "release_card", return_value=released), \
                patch.object(service.Path, "write_text"), \
                patch.object(service.Path, "unlink"), \
                patch.object(service, "logging") as fake_log:
            result = service.stop()
        return result, fake_log

    def test_stop_reports_what_it_released(self):
        released = {"killed": ["llama-server pid=1 port=8097"], "remaining": [],
                    "free_mib_before": 8000, "free_mib_after": 11000, "card_free": True}
        result, fake_log = self.run_stop(released)

        self.assertEqual(result["state"], "stopped")
        self.assertTrue(result["released"]["card_free"])
        self.assertIn("pid=1", result["released"]["killed"][0])
        self.assertFalse(fake_log.warning.called)

    def test_stop_does_not_claim_the_card_while_a_process_will_not_die(self):
        released = {"killed": [], "remaining": ["llama-server pid=9 port=8082"],
                    "free_mib_before": 8000, "free_mib_after": 8000, "card_free": False}
        result, fake_log = self.run_stop(released)

        self.assertFalse(result["released"]["card_free"])
        self.assertTrue(fake_log.warning.called)


if __name__ == "__main__":
    unittest.main()
