"""The project lock (run-state-write-integrity).

Every writer of a run directory's `run.json` / `journey_proposal.yaml` owns this
lock from before it reads the state it will write back until its last write.
The lock is an exclusive BSD `flock()` on `<run dir>/.run-state.lock`. Such a
lock belongs to an *open file description*, which has two consequences this
module is built around:

- When the server hands the description to an `sbp_gen` worker (`pass_fds`),
  both hold the same lock. If the server is killed, the worker still holds it,
  so a stale snapshot can never be written back after a later researcher save.
- An explicit `LOCK_UN` on a shared description would release it for every
  holder. Nothing here ever calls `LOCK_UN`: a holder releases only by closing
  its own descriptor, and the lock ends when the last descriptor is closed (or
  its process dies).

Two holders of one description do not exclude each other, so ownership inside
a process is tracked here, independently of the descriptor:

- server: thread-scoped. Only the request/job thread that acquired the lock
  owns it; any other thread asking for the same project is refused as busy.
- worker / command line: process-scoped. The command process owns it, and its
  threads serialize their read-modify-write through the transaction lock.

While a lease is delegated to a live child process, nobody in this process
owns it (the server must not write while the worker can).

Waiting happens only on the in-process transaction lock; the project lock is
always taken without waiting. There is deliberately no switch that disables
any of this.
"""
from __future__ import annotations

import errno
import fcntl
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

LOCK_NAME = ".run-state.lock"
FD_ENV = "SBP_RUN_LOCK_FD"
PATH_ENV = "SBP_RUN_LOCK_PATH"
BUSY_MESSAGE = "目前無法儲存。此專案仍有其他工作正在執行，可能是先前未結束的背景分析。請稍後再試。"
# An `sbp_gen` command that cannot take (or verify an inherited) project lock
# reads and writes nothing and exits with this code (EX_TEMPFAIL).
RUN_LOCK_BUSY_EXIT = 75

THREAD_SCOPE = "thread"
PROCESS_SCOPE = "process"


class RunLockBusy(Exception):
    """The project lock is held elsewhere (another process, another server
    thread, or a delegated worker)."""

    def __init__(self, message: str = BUSY_MESSAGE):
        super().__init__(message)


class RunLockNotHeld(Exception):
    """A state file was about to be written by code that does not own the lock."""


def _key(run_dir: str | os.PathLike) -> str:
    return os.path.realpath(os.fspath(run_dir))


def lock_path(run_dir: str | os.PathLike) -> Path:
    return Path(_key(run_dir)) / LOCK_NAME


class _Entry:
    __slots__ = ("key", "fd", "scope", "owner", "depth", "delegated", "child")

    def __init__(self, key: str, fd: int, scope: str):
        self.key = key
        self.fd = fd
        self.scope = scope
        self.owner = threading.get_ident()
        self.depth = 0
        self.delegated = False
        self.child = None

    def owned_by_current(self) -> bool:
        if self.delegated:
            # Delegation ends by itself once the bound child process has exited
            # (it has written everything it will ever write).
            if self.child is None or self.child.poll() is None:
                return False
            self.delegated = False
            self.child = None
        return self.scope == PROCESS_SCOPE or self.owner == threading.get_ident()


_registry_lock = threading.Lock()
_entries: dict[str, _Entry] = {}
_tx_locks: dict[str, threading.RLock] = {}


def _open_lock_file(key: str) -> int:
    Path(key).mkdir(parents=True, exist_ok=True)
    return os.open(os.path.join(key, LOCK_NAME), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)


def _try_flock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return False
        raise
    return True


class Lease:
    """One owner's claim on a project lock. Release closes the owner's own
    descriptor; it never unlocks a description another process may share."""

    def __init__(self, entry: _Entry):
        self._entry = entry
        self._released = False
        self.adopted = False

    @property
    def run_dir(self) -> str:
        return self._entry.key

    def held_by_current_thread(self) -> bool:
        """Still held, not delegated, and owned by the calling thread (a job
        thread registering its own follow-up job on the same project)."""
        with _registry_lock:
            entry = self._entry
            return (not self._released and _entries.get(entry.key) is entry
                    and entry.owned_by_current())

    def adopt_current_thread(self) -> None:
        """Hand a thread-scoped lease to the thread that will use it (a job
        registered by a request thread runs in its own job thread)."""
        with _registry_lock:
            self._entry.owner = threading.get_ident()
        self.adopted = True

    def delegate(self) -> dict:
        """Mark the lease as handed to a child process and return what to pass
        to it. Until `reclaim()`, nobody in this process owns the lock."""
        with _registry_lock:
            self._entry.delegated = True
            fd = self._entry.fd
        return {"pass_fds": (fd,), "env": {FD_ENV: str(fd), PATH_ENV: str(Path(self._entry.key) / LOCK_NAME)}}

    def bind_child(self, child) -> None:
        """The delegated descriptor now lives in `child` (a `subprocess.Popen`);
        ownership comes back to this process as soon as `child` has exited."""
        with _registry_lock:
            self._entry.child = child

    def reclaim(self) -> None:
        with _registry_lock:
            self._entry.delegated = False
            self._entry.child = None

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        with _registry_lock:
            entry = self._entry
            entry.depth -= 1
            if entry.depth > 0:
                return
            if _entries.get(entry.key) is entry:
                del _entries[entry.key]
            fd = entry.fd
        os.close(fd)  # never LOCK_UN: the description may be shared with a worker


def _acquire(run_dir, scope: str) -> Lease:
    key = _key(run_dir)
    with _registry_lock:
        entry = _entries.get(key)
        if entry is not None:
            if entry.owned_by_current():
                entry.depth += 1
                return Lease(entry)
            raise RunLockBusy()
        fd = _open_lock_file(key)
        if not _try_flock(fd):
            os.close(fd)
            raise RunLockBusy()
        entry = _Entry(key, fd, scope)
        entry.depth = 1
        _entries[key] = entry
        return Lease(entry)


def acquire(run_dir) -> Lease:
    """Server-side, thread-scoped, never waits."""
    return _acquire(run_dir, THREAD_SCOPE)


@contextmanager
def hold(run_dir) -> Iterator[Lease]:
    lease = acquire(run_dir)
    try:
        yield lease
    finally:
        lease.release()


def adopt_or_acquire(run_dir) -> Lease:
    """Worker / command-line side, process-scoped. Adopt the descriptor the
    server handed over for exactly this run directory, or take the lock without
    waiting. A mismatching inheritance is refused as busy."""
    key = _key(run_dir)
    inherited_fd = os.environ.get(FD_ENV)
    inherited_path = os.environ.get(PATH_ENV)
    with _registry_lock:
        entry = _entries.get(key)
        if entry is not None:
            if entry.owned_by_current():
                entry.depth += 1
                return Lease(entry)
            raise RunLockBusy()
        if inherited_fd is not None and inherited_path is not None:
            expected = os.path.join(key, LOCK_NAME)
            if os.path.realpath(inherited_path) != expected:
                raise RunLockBusy()
            try:
                fd = int(inherited_fd)
                held = os.fstat(fd)
                on_disk = os.stat(expected)
            except (ValueError, OSError):
                raise RunLockBusy() from None
            if (held.st_ino, held.st_dev) != (on_disk.st_ino, on_disk.st_dev) or not _try_flock(fd):
                raise RunLockBusy()
            os.set_inheritable(fd, False)
            for name in (FD_ENV, PATH_ENV):
                os.environ.pop(name, None)
        else:
            fd = _open_lock_file(key)
            if not _try_flock(fd):
                os.close(fd)
                raise RunLockBusy()
        entry = _Entry(key, fd, PROCESS_SCOPE)
        entry.depth = 1
        _entries[key] = entry
        return Lease(entry)


@contextmanager
def command_lock(run_dir) -> Iterator[Lease]:
    lease = adopt_or_acquire(run_dir)
    try:
        yield lease
    finally:
        lease.release()


@contextmanager
def handoff(run_dir) -> Iterator[dict]:
    """For a short synchronous `sbp_gen` call made by the current owner: yield
    `subprocess` keyword arguments (`pass_fds`, `env`) that hand the child this
    project's lock, and take ownership back when the call returns."""
    lease = acquire(run_dir)
    try:
        given = lease.delegate()
        env = dict(os.environ)
        env.update(given["env"])
        try:
            yield {"pass_fds": given["pass_fds"], "env": env}
        finally:
            lease.reclaim()
    finally:
        lease.release()


def content_entries(directory) -> list[Path]:
    """A directory's entries other than the project lock file: taking the lock
    for a not-yet-written output location must not make it look occupied."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    return [entry for entry in directory.iterdir() if entry.name != LOCK_NAME]


def is_owned(run_dir) -> bool:
    with _registry_lock:
        entry = _entries.get(_key(run_dir))
        return entry is not None and entry.owned_by_current()


def require_owned(run_dir) -> None:
    if not is_owned(run_dir):
        raise RunLockNotHeld(f"沒有持有專案鎖，拒絕寫入：{run_dir}")


def transaction_lock(run_dir) -> threading.RLock:
    """The in-process lock that makes one read-modify-write at a time. Hold it
    only around file reads/writes: never while calling a model, starting a
    child process, or waiting on another lock."""
    key = _key(run_dir)
    with _registry_lock:
        lock = _tx_locks.get(key)
        if lock is None:
            lock = _tx_locks[key] = threading.RLock()
        return lock
