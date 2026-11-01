"""Shared plumbing for the live verification scripts in this directory.

Every `verify_*.py` here drives the same gateway over HTTP: wait until the model
is idle, submit something, then poll a progress endpoint until the job settles.
Those loops used to be copy-pasted per script with slightly different retry and
printing behaviour, which made a fix in one script miss the others. This module
is the single copy:

    wait_ready()            - block until /local/status reports ready and idle
    get_json()              - GET with a timeout and a clear failure message
    poll_image_job()        - poll /local/image/progress until the job settles

The functions are deliberately small and dependency-free beyond `requests`, so a
script can still be read top-to-bottom without following imports. Import example:

    from verify_common import GATEWAY, wait_ready, get_json, poll_image_job
"""

from __future__ import annotations

import time

import requests

GATEWAY = "http://127.0.0.1:8081"
# The gateway proxies browser traffic: requests without the panel's Origin still
# work for the JSON routes, but sending it keeps these scripts on the same path
# the browser takes.
IMAGE_HEADERS = {"Origin": GATEWAY}


def get_json(path: str, *, timeout: float = 20, headers: dict | None = None) -> dict:
    """GET a gateway JSON route, raising with the URL and body on failure."""
    response = requests.get(f"{GATEWAY}{path}", headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def wait_ready(seconds: float = 300, *, interval: float = 3) -> dict:
    """Wait until /local/status says the model is ready and has no active request.

    Raises RuntimeError after `seconds`, reporting the last status seen.
    """
    deadline = time.time() + seconds
    last: dict = {}
    while time.time() < deadline:
        try:
            last = get_json("/local/status", timeout=15)
        except requests.RequestException as exc:  # gateway still starting up
            last = {"error": str(exc)}
            time.sleep(interval)
            continue
        if last.get("state") == "ready" and not last.get("active_requests"):
            return last
        time.sleep(interval)
    raise RuntimeError(f"gateway never became ready: {last}")


def poll_image_job(
    endpoint: str,
    job_id: str,
    *,
    timeout: float = 1800,
    interval: float = 2,
    retry: bool = True,
    done_states: tuple[str, ...] = ("completed", "failed", "cancelled"),
    on_phase=None,
) -> dict:
    """Poll an image progress endpoint until the job with `job_id` settles.

    `on_phase(job)` is called whenever `phase`/`state` changes, which is where
    each script prints its own progress line. `retry` keeps the loop alive across
    transient request failures (all shipped scripts want that); a persistent
    failure still ends in RuntimeError rather than hanging.
    """
    deadline = time.time() + timeout
    last_phase = None
    last_error: str | None = None
    while time.time() < deadline:
        try:
            payload = get_json(endpoint, timeout=15)
        except requests.RequestException as exc:
            if not retry:
                raise
            last_error = str(exc)
            time.sleep(interval)
            continue
        job = payload.get("job") or {}
        state, phase = job.get("state"), job.get("phase")
        if on_phase and (state, phase) != last_phase:
            last_phase = (state, phase)
            on_phase(job)
        if job.get("id") == job_id and not payload.get("busy") and state in done_states:
            return job
        time.sleep(interval)
    detail = f" (last request error: {last_error})" if last_error else ""
    raise RuntimeError(f"timeout waiting for job {job_id} on {endpoint}{detail}")
