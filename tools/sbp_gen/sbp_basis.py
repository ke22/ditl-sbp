"""What a service blueprint was built from (change track-sbp-upstream-freshness).

When the blueprint is generated, ``run.json`` records the fingerprint of the
upstream content actually sent to generation (``built_from_fingerprint``). If
that content later changes, the researcher either keeps the blueprint -- the
newer fingerprint is recorded as ``retained_against_fingerprint`` and
``built_from`` is left alone -- or regenerates it on purpose. Nothing here
starts a model call.

The fingerprint is the digest of ``stages.structure_input_view``, the same
view the structure message is assembled from -- including the external
references the researcher adopted before the blueprint, when there are any.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import lifecycle, stages

STATES = ("none", "untracked", "current", "retained", "stale")


def fingerprint(view: dict) -> str:
    return lifecycle.canonical_content_digest(view)


def _load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None


def current_fingerprint(run_dir: str | Path) -> str | None:
    """The fingerprint the blueprint would be built from now, from the same
    files `sbp_gen structure` reads; None when they cannot be read."""
    run = Path(run_dir)
    evidence = _load_yaml(run / "evidence.yaml")
    case = _load_yaml(run / "case.yaml")
    journey = _load_yaml(run / "journey.yaml")
    if not isinstance(evidence, list) or not isinstance(case, dict) or not isinstance(journey, dict):
        return None
    axial = _load_yaml(run / "axial_coding.yaml") or {}
    try:
        runtime_case = stages.case_with_journey(case, journey)
    except (ValueError, KeyError, StopIteration):
        return None
    from . import implementation_research
    references = implementation_research.accepted_references(run) if stages.journey_has_solutions(journey) else []
    return fingerprint(stages.structure_input_view(evidence, runtime_case, journey, axial.get("categories"), references))


def _state(run: Path) -> dict:
    path = run / "run.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        return {}


def _write_state(run: Path, state: dict) -> None:
    path = run / "run.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def basis_after_build(view: dict) -> dict:
    """The `sbp_basis` a fresh build records; any earlier retention is dropped."""
    return {"built_from_fingerprint": fingerprint(view), "built_at": _now()}


def sbp_freshness(run_dir: str | Path) -> dict:
    run = Path(run_dir)
    if not (run / "sbp_draft.yaml").is_file():
        return {"state": "none"}
    basis = _state(run).get("sbp_basis")
    if not isinstance(basis, dict) or not basis.get("built_from_fingerprint"):
        return {"state": "untracked"}
    now = current_fingerprint(run)
    if now is None:
        return {"state": "untracked"}
    if now == basis["built_from_fingerprint"]:
        return {"state": "current"}
    if now == basis.get("retained_against_fingerprint"):
        return {"state": "retained", "retained_at": basis.get("retained_at")}
    return {"state": "stale"}


class NotStale(ValueError):
    pass


def retain(run_dir: str | Path) -> dict:
    """The researcher keeps the blueprint against the current upstream."""
    run = Path(run_dir)
    if sbp_freshness(run)["state"] != "stale":
        raise NotStale("服務藍圖目前沒有需要決定沿用或重新產生的變更。")
    state = _state(run)
    basis = dict(state.get("sbp_basis") or {})
    basis["retained_against_fingerprint"] = current_fingerprint(run)
    basis["retained_at"] = _now()
    state["sbp_basis"] = basis
    _write_state(run, state)
    return sbp_freshness(run)


def backup_before_regenerate(run_dir: str | Path) -> list[Path]:
    """Copy the current blueprint aside before a researcher-requested rebuild."""
    run = Path(run_dir)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    copies = []
    for name in ("sbp_draft.yaml", "service_graph.yaml"):
        source = run / name
        if source.is_file():
            target = run / f"{name}.bak-{stamp}-before-regenerate"
            target.write_bytes(source.read_bytes())
            copies.append(target)
    return copies
