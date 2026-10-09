"""Runtime-owned locations for canonical filesystem artifacts.

This module intentionally contains only path rules.  It does not create,
rewrite, or migrate any research data.  Callers provide the active ``runs``
root when they have it; the fallback derived from a run path is only for the
legacy call sites that already have a selected run and no server context.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml


class RegistryPointerError(ValueError):
    """A stored registry pointer has no supported, deterministic shape."""

    code = "registry_pointer_unrecognized"
    retryable = False

    def __init__(self, pointer: object, expected_name: str | None = None):
        self.pointer = str(pointer)
        self.expected_name = expected_name
        expected = f"; expected filename {expected_name!r}" if expected_name else ""
        super().__init__(
            f"{self.code}: unsupported artifact_registry pointer {self.pointer!r}{expected}"
        )


def runs_root_for_run(run_dir: str | Path) -> Path:
    """Return the enclosing ``runs`` directory without consulting CWD.

    Archived runs are nested below ``runs/_archive`` and therefore need the
    ancestor search rather than simply using ``run_dir.parent``.  Small test
    fixtures whose root is not literally named ``runs`` retain the old
    parent-based layout as a deterministic fallback.
    """
    run_path = Path(run_dir).expanduser()
    for candidate in (run_path, *run_path.parents):
        if candidate.name == "runs":
            return candidate
    return run_path.parent


def derive_registry_path(case_id: str, runs_root: str | Path) -> Path:
    """Derive the existing case registry location; the hash rule is unchanged."""
    key = hashlib.sha256(case_id.encode("utf-8")).hexdigest()
    return Path(runs_root).expanduser() / ".artifact-registry" / f"{key}.yaml"


def normalized_registry_pointer(
    registry_path: str | Path, run_dir: str | Path, runs_root: str | Path | None = None
) -> str:
    """Return the one representation used for newly written pointers.

    The approved representation is ``runs/.artifact-registry/<name>`` relative
    to the active data root.  A registry outside that root is kept absolute,
    because it cannot survive a data-root move truthfully.
    """
    registry = Path(registry_path).expanduser()
    root = Path(runs_root).expanduser() if runs_root is not None else runs_root_for_run(run_dir)
    if root.name != "runs":
        return str(registry if registry.is_absolute() else registry.resolve())
    data_root = root.parent
    if not registry.is_absolute():
        registry = registry.resolve()
    try:
        relative = registry.relative_to(data_root)
    except ValueError:
        return str(registry)
    return relative.as_posix()


def _case_id_for_run(run_dir: str | Path) -> str | None:
    """Read the run identity needed to validate a stored registry filename."""
    path = Path(run_dir).expanduser()
    run_json = path / "run.json"
    try:
        data = json.loads(run_json.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict) and isinstance(data.get("case_id"), str) and data["case_id"]:
        return data["case_id"]
    case_yaml = path / "case.yaml"
    try:
        data = yaml.safe_load(case_yaml.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        data = None
    if isinstance(data, dict) and isinstance(data.get("case_id"), str) and data["case_id"]:
        return data["case_id"]
    return None


def resolve_registry_pointer(
    pointer: str | Path,
    run_dir: str | Path,
    runs_root: str | Path | None = None,
    *,
    case_id: str | None = None,
) -> Path:
    """Resolve a stored pointer under the active registry root.

    Legacy prefixes are deliberately ignored.  The only trusted part of a
    stored value is its basename, which must equal the hash-derived registry
    filename for the run's case identity.  This makes absolute legacy values
    safe to read after the data root moves and makes every legacy shape
    independent of process CWD.
    """
    if not isinstance(pointer, (str, Path)):
        raise RegistryPointerError(pointer)
    raw = str(pointer)
    if not raw.strip():
        raise RegistryPointerError(pointer)
    identity = case_id or _case_id_for_run(run_dir)
    if not identity:
        raise RegistryPointerError(pointer)
    expected_name = Path(derive_registry_path(identity, runs_root or runs_root_for_run(run_dir))).name
    if Path(raw).name != expected_name:
        raise RegistryPointerError(pointer, expected_name)
    active_runs = Path(runs_root).expanduser() if runs_root is not None else runs_root_for_run(run_dir)
    return derive_registry_path(identity, active_runs)


def protected_canonical_data_kind(
    output_path: str | Path, data_root: str | Path
) -> str | None:
    """Return ``cases``/``runs`` when an output is inside active canonical data."""
    target = Path(output_path).expanduser().resolve()
    root = Path(data_root).expanduser().resolve()
    for name in ("cases", "runs"):
        protected = (root / name).resolve()
        try:
            target.relative_to(protected)
        except ValueError:
            continue
        return name
    return None
