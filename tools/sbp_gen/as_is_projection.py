"""Deterministic Journey input projection consumed by As-is analysis.

This module deliberately knows only the canonical Journey structure.  It does
not read evidence, Service Graph data, or UI state, and it does not attempt to
understand free text.  The projection is an input boundary, not a second
research model.
"""
from __future__ import annotations

import hashlib
import json
from typing import Iterable, Mapping, Any


def _step_record(step: Mapping[str, Any], presentation_only_step_ids: set[str]) -> dict[str, Any]:
    step_id = step.get("id")
    return {
        "step_id": step_id,
        # An explicitly declared presentation-only rename is not a consumed
        # service-action edit.  Without that operation marker, the label stays
        # in the projection and any uncertain free-text change fails closed.
        "label": None if step_id in presentation_only_step_ids else step.get("label"),
        "description": step.get("description"),
    }


def project_as_is_input(
    journey: Mapping[str, Any],
    *,
    presentation_only_step_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Project only Journey-owned service-action/path inputs.

    Array order, scenario/branch labels, evidence references, provenance
    annotations, and Branch anchors are intentionally excluded.  Stable IDs,
    step content, and path membership remain.  The optional operation marker
    is required to treat a step-label edit as presentation-only; otherwise a
    label/description change changes the projection and requires recheck.
    """
    if not isinstance(journey, Mapping):
        raise TypeError("journey must be a mapping")
    version = journey.get("schema_version")
    if version not in (2, 3):
        raise ValueError(f"unsupported Journey schema_version={version!r}")
    renamed = {item for item in presentation_only_step_ids if isinstance(item, str)}
    primary_id = journey.get("primary_scenario")
    paths: list[dict[str, Any]] = []
    scenarios = [item for item in journey.get("scenarios") or [] if isinstance(item, Mapping)]
    for scenario in sorted(scenarios, key=lambda item: str(item.get("id", ""))):
        scenario_id = scenario.get("id")
        path_kind = "primary" if scenario_id == primary_id else "alternative_scenario"
        steps = [
            _step_record(step, renamed)
            for step in scenario.get("steps") or []
            if isinstance(step, Mapping)
        ]
        paths.append({
            "path_id": scenario_id,
            "path_kind": path_kind,
            "steps": sorted(steps, key=lambda item: str(item.get("step_id", ""))),
        })
    if version == 3:
        branches = [item for item in journey.get("branches") or [] if isinstance(item, Mapping)]
        for branch in sorted(branches, key=lambda item: str(item.get("branch_id", ""))):
            steps = [
                _step_record(step, renamed)
                for step in branch.get("steps") or []
                if isinstance(step, Mapping)
            ]
            paths.append({
                "path_id": branch.get("branch_id"),
                "path_kind": "branch",
                "steps": sorted(steps, key=lambda item: str(item.get("step_id", ""))),
            })
    return {
        "schema_version": version,
        "case_id": journey.get("case_id"),
        "primary_scenario": primary_id,
        "paths": paths,
    }


def as_is_input_projection_digest(
    journey: Mapping[str, Any],
    *,
    presentation_only_step_ids: Iterable[str] = (),
) -> str:
    """Return the stable SHA-256 fingerprint for the projection."""
    projection = project_as_is_input(
        journey,
        presentation_only_step_ids=presentation_only_step_ids,
    )
    encoded = json.dumps(
        projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def as_is_input_changed(
    baseline_journey: Mapping[str, Any],
    edited_journey: Mapping[str, Any],
    *,
    presentation_only_step_ids: Iterable[str] = (),
) -> bool:
    """Return whether the consumed Journey projection changed."""
    return as_is_input_projection_digest(
        baseline_journey,
        presentation_only_step_ids=presentation_only_step_ids,
    ) != as_is_input_projection_digest(
        edited_journey,
        presentation_only_step_ids=presentation_only_step_ids,
    )
