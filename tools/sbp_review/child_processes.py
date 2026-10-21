"""Child processes the review server starts, tracked so they can be ended.

Every `sbp_gen` command the server runs starts in its own session (so its
process group is its own) and is registered here until it and everything in its
group has exited. Closing the server, or cancelling a job, ends the whole group
instead of leaving paid AI work running in the background
(desktop-runtime-foundation).

The shipped CLI wrappers start their executor in yet another session, so a
group signal alone does not reach it; the wrappers forward SIGTERM themselves.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from typing import Any

GRACE_SECONDS = 5.0

_lock = threading.Lock()
_registry: dict[int, subprocess.Popen] = {}


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(pgid: int, sig: int) -> bool:
    """Signal a tracked group; False when it is already gone (or is our own)."""
    if pgid == os.getpgrp():
        return False
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _prune() -> None:
    with _lock:
        for pgid, proc in list(_registry.items()):
            proc.poll()
            if proc.returncode is not None and not _group_alive(pgid):
                del _registry[pgid]


def active() -> list[subprocess.Popen]:
    """Processes still registered: the leader or something in its group is alive."""
    _prune()
    with _lock:
        return list(_registry.values())


def spawn(cmd: Any, *, run_lock: Any = None, **popen_kwargs: Any) -> subprocess.Popen:
    """`subprocess.Popen`, always in its own session, and registered.

    `run_lock` (a `sbp_gen.run_lock.Lease`, run-state-write-integrity): the
    child inherits the same open lock description (`pass_fds` plus the two
    environment variables), so it keeps the project lock even if this server
    dies. Until the child has exited, nobody in this process owns the lock."""
    popen_kwargs["start_new_session"] = True
    if run_lock is not None:
        given = run_lock.delegate()
        popen_kwargs["pass_fds"] = tuple(popen_kwargs.get("pass_fds") or ()) + tuple(given["pass_fds"])
        env = dict(popen_kwargs["env"] if popen_kwargs.get("env") is not None else os.environ)
        env.update(given["env"])
        popen_kwargs["env"] = env
    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)
    except BaseException:
        if run_lock is not None:
            run_lock.reclaim()
        raise
    if run_lock is not None:
        if callable(getattr(proc, "poll", None)):
            run_lock.bind_child(proc)
        else:  # a test double that never really started
            run_lock.reclaim()
    _prune()
    pid = getattr(proc, "pid", None)
    # A stand-in object (a test double) has no real pid; there is nothing to track.
    if isinstance(pid, int) and not isinstance(pid, bool):
        with _lock:
            _registry[pid] = proc
    return proc


def release(proc: subprocess.Popen) -> None:
    """Stop tracking `proc` once it and its group are gone (a no-op while alive)."""
    with _lock:
        if proc.poll() is not None and not _group_alive(proc.pid):
            _registry.pop(proc.pid, None)


def run(cmd: Any, *, input: Any = None, timeout: float | None = None, capture_output: bool = False,
        check: bool = False, **kwargs: Any) -> subprocess.CompletedProcess:
    """`subprocess.run` for tracked processes. A timeout ends the whole group."""
    if capture_output:
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
    if input is not None:
        kwargs["stdin"] = subprocess.PIPE
    proc = spawn(cmd, **kwargs)
    try:
        try:
            stdout, stderr = proc.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            _signal_group(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise
        except BaseException:
            _signal_group(proc.pid, signal.SIGKILL)
            proc.wait()
            raise
        result = subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
        if check and result.returncode:
            raise subprocess.CalledProcessError(result.returncode, proc.args, stdout, stderr)
        return result
    finally:
        release(proc)


def terminate_group(proc: subprocess.Popen, grace: float = GRACE_SECONDS) -> None:
    """Ask `proc`'s whole group to stop; kill what is still alive after `grace`.

    Returns at once, so a request handler is never held up by the grace period."""
    if not _signal_group(proc.pid, signal.SIGTERM):
        return

    def _escalate() -> None:
        proc.poll()
        if _group_alive(proc.pid):
            _signal_group(proc.pid, signal.SIGKILL)

    timer = threading.Timer(grace, _escalate)
    timer.daemon = True
    timer.start()


def terminate_all(grace: float = GRACE_SECONDS) -> None:
    """End every tracked group. Blocks until they are gone or `grace` has passed
    (then SIGKILL); returns immediately when nothing is tracked."""
    procs = active()
    if not procs:
        return
    for proc in procs:
        _signal_group(proc.pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not active():
            return
        time.sleep(0.05)
    for proc in active():
        _signal_group(proc.pid, signal.SIGKILL)
    kill_deadline = time.monotonic() + 2.0
    while time.monotonic() < kill_deadline and active():
        time.sleep(0.05)
