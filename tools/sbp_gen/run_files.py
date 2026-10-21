"""The one writer of a run's state files (run-state-write-integrity).

`run.json` and `journey_proposal.yaml` are written only here:

- atomically: a temporary file in the same directory, flushed to disk, then one
  `os.replace`, so a concurrent reader never sees half a file;
- only by the owner of the project lock (`run_lock`), under the project's
  in-process transaction lock; this module never takes the project lock
  itself (`transaction()` is the read-modify-write form);
- never over a malformed existing file: if the target exists but cannot be
  parsed, the write is refused whatever the caller is trying to write (a caller
  that swallowed the read error and passes `{}` is refused too). Nothing here
  repairs, truncates or overwrites malformed research state.

Other files may use the same atomic writers without the ownership check.
"""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import yaml

from . import run_lock

STATE_FILE_NAMES = frozenset({"run.json", "journey_proposal.yaml"})


class RunStateUnreadable(Exception):
    """A state file exists but cannot be parsed; nothing was written."""

    def __init__(self, path: Path, detail: str = ""):
        self.path = Path(path)
        super().__init__(f"專案狀態檔無法讀取，沒有寫入：{self.path.name}" + (f"（{detail}）" if detail else ""))


def is_state_file(path: str | os.PathLike) -> bool:
    return Path(path).name in STATE_FILE_NAMES


def _parse(path: Path, text: str) -> dict:
    if not text.strip():
        raise RunStateUnreadable(path, "檔案是空的")
    try:
        value = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError) as exc:
        raise RunStateUnreadable(path, type(exc).__name__) from None
    if not isinstance(value, dict):
        raise RunStateUnreadable(path, "最外層不是物件")
    return value


def read_state(path: str | os.PathLike) -> dict:
    """Missing file -> empty state. Present but unparsable -> RunStateUnreadable."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise RunStateUnreadable(path, type(exc).__name__) from None
    return _parse(path, text)


def _ensure_replaceable(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RunStateUnreadable(path, type(exc).__name__) from None
    _parse(path, text)


def _atomic_write(path: Path, write: Callable[[Any], None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=path.name + ".", suffix=".tmp", delete=False,
        ) as temporary:
            temporary_name = temporary.name
            write(temporary)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def _guarded(path: Path, write: Callable[[Any], None]) -> None:
    if not is_state_file(path):
        _atomic_write(path, write)
        return
    run_dir = path.parent
    run_lock.require_owned(run_dir)
    with run_lock.transaction_lock(run_dir):
        _ensure_replaceable(path)
        _atomic_write(path, write)


def write_json_atomic(path: str | os.PathLike, value: Any, *, trailing_newline: bool = True) -> None:
    path = Path(path)

    def write(handle) -> None:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        if trailing_newline:
            handle.write("\n")

    _guarded(path, write)


def write_yaml_atomic(path: str | os.PathLike, value: Any, *, safe: bool = True) -> None:
    path = Path(path)

    def write(handle) -> None:
        if safe:
            yaml.safe_dump(value, handle, allow_unicode=True, sort_keys=False)
        else:
            yaml.dump(value, handle, allow_unicode=True, sort_keys=False)

    _guarded(path, write)


def restore(path: str | os.PathLike, before: bytes | None) -> None:
    """Roll a state file back to bytes the owner read earlier in the same
    transaction (or remove it when it did not exist). Owner-only, atomic."""
    path = Path(path)
    run_dir = path.parent
    if is_state_file(path):
        run_lock.require_owned(run_dir)
    with run_lock.transaction_lock(run_dir):
        if before is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_write_bytes(path, before)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as temporary:
            temporary_name = temporary.name
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


@contextmanager
def transaction(path: str | os.PathLike) -> Iterator[dict]:
    """One read-modify-write of a state file. The caller must already own the
    project lock; the whole read, change and write runs under the project's
    in-process transaction lock. Raising inside the block writes nothing."""
    path = Path(path)
    run_dir = path.parent
    run_lock.require_owned(run_dir)
    with run_lock.transaction_lock(run_dir):
        state = read_state(path)
        yield state
        if path.suffix == ".json":
            write_json_atomic(path, state)
        else:
            write_yaml_atomic(path, state)
