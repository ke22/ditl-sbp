"""One server per data root.

The server holds an exclusive ``flock`` on ``<data root>/.ditl_sbp.lock`` for as
long as it runs. The operating system releases the lock when the process ends
(however it ends), so no stale lock can exist. The file holds only the process
id and the port, so a second launcher can find the running server.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

LOCK_NAME = ".ditl_sbp.lock"


class AlreadyRunning(Exception):
    def __init__(self, pid: int | None, port: int | None) -> None:
        super().__init__(f"another server holds this data root (pid={pid}, port={port})")
        self.pid = pid
        self.port = port


def lock_path(data_root: str | Path) -> Path:
    return Path(data_root) / LOCK_NAME


def read_info(data_root: str | Path) -> dict | None:
    """The recorded ``{"pid", "port"}``, or None when the file is missing, half
    written, or not what this module writes."""
    try:
        raw = lock_path(data_root).read_text(encoding="utf-8")
        info = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or not isinstance(info.get("pid"), int):
        return None
    port = info.get("port")
    if port is not None and not isinstance(port, int):
        return None
    return {"pid": info["pid"], "port": port}


class InstanceLock:
    def __init__(self, data_root: str | Path) -> None:
        self.data_root = Path(data_root)
        self._fd: int | None = None

    def acquire(self) -> "InstanceLock":
        # os.open fds are non-inheritable, so AI child processes never hold the lock.
        fd = os.open(lock_path(self.data_root), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            info = read_info(self.data_root) or {}
            raise AlreadyRunning(info.get("pid"), info.get("port")) from None
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        self._write(None)
        return self

    def _write(self, port: int | None) -> None:
        payload = json.dumps({"pid": os.getpid(), "port": port}).encode("utf-8")
        os.ftruncate(self._fd, 0)
        os.pwrite(self._fd, payload, 0)

    def publish_port(self, port: int) -> None:
        if self._fd is None:
            raise RuntimeError("lock not held")
        self._write(port)

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "InstanceLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
