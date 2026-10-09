"""Serialized read-modify-write of a run's `run.json`.

Steps that run concurrently in one process (the HMW formation lenses) each
record their own state and usage in `run.json`. Writing a whole state dict
read at the start of a step would drop whatever another step wrote in the
meantime, so every such write goes through `update`: take the lock, reread
the file, apply only this writer's keys, and replace the file atomically.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Callable

_LOCK = threading.RLock()


def read(path: Path) -> dict:
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def update(path: Path, mutate: Callable[[dict], None]) -> dict:
    """Apply `mutate` to the current on-disk state under the lock and save it."""
    path = Path(path)
    with _LOCK:
        state = read(path)
        mutate(state)
        descriptor, temporary = tempfile.mkstemp(prefix=".run.", suffix=".json", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(state, ensure_ascii=False, indent=2))
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return state


def record_stage(path: Path, stage: str, *, state_key: str, failure_key: str,
                 usage: dict | None = None, failure: str | None = None) -> dict:
    """One step's outcome: its own state, failure and usage keys only."""
    def mutate(state: dict) -> None:
        if failure is None:
            state[state_key] = "complete"
            state.pop(failure_key, None)
        else:
            state[state_key] = "failed"
            state[failure_key] = failure
        if usage is not None:
            state.setdefault("usage", {})[stage] = usage
    return update(path, mutate)
