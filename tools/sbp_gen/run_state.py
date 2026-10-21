"""Serialized read-modify-write of a run's `run.json`.

Steps that run concurrently in one process (the HMW formation lenses) each
record their own state and usage in `run.json`. Writing a whole state dict
read at the start of a step would drop whatever another step wrote in the
meantime, so every such write goes through `update`: take the lock, reread
the file, apply only this writer's keys, and replace the file atomically.

run-state-write-integrity: `update` is a thin wrapper over
`run_files.transaction` -- the caller must own the project lock, the whole
read-modify-write runs under the project's transaction lock, and a malformed
`run.json` raises `run_files.RunStateUnreadable` instead of becoming `{}`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from . import run_files


def read(path: Path) -> dict:
    """Missing -> {}; present but malformed -> run_files.RunStateUnreadable."""
    return run_files.read_state(path)


def update(path: Path, mutate: Callable[[dict], None]) -> dict:
    """Apply `mutate` to the current on-disk state under the lock and save it."""
    with run_files.transaction(Path(path)) as state:
        mutate(state)
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
