"""Read-only adapter for the Local V1 ``jtbd_persona`` delivery.

All research sources are resolved from the explicitly selected run. Persona
selection gates are checked through the existing read-only gate resolver; no
other run, case-level index, HMW, Narrative, Journey, or Blueprint artifact is
used as a substitute or Persona evidence filter.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

import yaml

from . import gates, lifecycle, stages
from .delivery import (
    DeliveryError,
    DeliveryViewModel,
    _digest,
    _evidence_version,
    _read_json,
    _read_yaml,
    _require_valid,
    _resolve_registry_path,
    _schema_errors,
)


JTBD_PERSONA = "jtbd_persona"
_REQUIRED_RUN_STATES = (
    "jtbd",
    "segmentation_axis_selection",
    "target_segmentation_selection",
    "persona_matrix",
    "persona_content",
)
_GATED_SELECTION_STATES = frozenset({"segmentation_axis_selection", "target_segmentation_selection"})
_DELIVERY_SOURCE_FILES = (
    "case.yaml",
    "evidence.yaml",
    "axial_coding.yaml",
    "segmentation_axis_selection.yaml",
    "target_segmentation_selection.yaml",
    "persona_matrix.yaml",
    "persona_content.yaml",
)


def _required_file(run_dir: Path, filename: str, artifact: str) -> Path:
    path = run_dir / filename
    if not path.is_file():
        raise DeliveryError("required_artifact_missing", artifact, filename)
    return path


def _read_required_yaml(run_dir: Path, filename: str, artifact: str) -> Any:
    return _read_yaml(_required_file(run_dir, filename, artifact), artifact)


def _check_case_id(data: Mapping[str, Any], case_id: str, artifact: str) -> None:
    if data.get("case_id") != case_id:
        raise DeliveryError("artifact_case_identity_mismatch", artifact, data.get("case_id"))


def _require_complete_state(run_state: Mapping[str, Any], state_name: str) -> None:
    recorded = run_state.get(f"{state_name}_state")
    if recorded != "complete":
        raise DeliveryError("required_artifact_not_current", state_name, recorded or "state_missing")


def _source_ref(
    artifact_type: str,
    filename: str,
    data: Any,
    *,
    run_state: Mapping[str, Any],
    version_ref: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ref: dict[str, Any] = {
        "artifact_type": artifact_type,
        "source_ref": filename,
        "status": "ready",
        "version_ref": version_ref,
        "content_digest": _digest(data, artifact_type),
        "versioning": "lifecycle_current" if version_ref else "run_scoped_current_file",
    }
    recorded_state = run_state.get(f"{artifact_type}_state")
    if recorded_state is not None:
        ref["recorded_state"] = recorded_state
    if extra:
        ref.update(copy.deepcopy(dict(extra)))
    return ref


def _persona_outcome(
    persona_content: Mapping[str, Any], primary_quadrant_id: str,
) -> dict[str, Any] | None:
    matches = [
        entry for entry in persona_content.get("quadrant_outcomes", [])
        if isinstance(entry, dict) and entry.get("quadrant_id") == primary_quadrant_id
    ]
    if len(matches) > 1:
        raise DeliveryError("duplicate_persona_quadrant_outcome", "persona_content", primary_quadrant_id)
    return copy.deepcopy(matches[0]) if matches else None


def _gate_confirmation(
    run_dir: Path,
    gate_name: str,
    now: str,
) -> tuple[str, str | None]:
    """Return resolved gate state and its recorded/imported provenance.

    ``resolve_gate_state`` is a pure read: for a legacy run it synthesizes an
    in-memory import only. It never materializes ``.gates/`` during a read.
    """
    try:
        state = gates.resolve_gate_state(run_dir, gate_name, now=now)
        ledger = gates._ledger_or_legacy_import(run_dir, gates._read_ledger(run_dir), now)
        selected = gates._selected_confirmation(ledger, gate_name)
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
        raise DeliveryError("human_gate_resolution_failed", gate_name, type(exc).__name__) from exc
    origin = selected.get("recorded_by") if isinstance(selected, dict) else None
    return state, origin


def _validate_candidate_quadrants(
    candidate_doc: Mapping[str, Any], quadrants: list[dict[str, Any]],
) -> None:
    expected_by_id = {item["id"]: item for item in quadrants}
    candidate_items = candidate_doc.get("quadrants") or []
    found_by_id = {
        item.get("id"): item for item in candidate_items
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if set(found_by_id) != set(expected_by_id):
        raise DeliveryError("target_quadrant_lineage_mismatch", "target_segmentation_candidates")
    for quadrant_id, expected in expected_by_id.items():
        candidate = found_by_id[quadrant_id]
        if (
            candidate.get("axis_1_pole") != expected.get("axis_1_pole")
            or candidate.get("axis_2_pole") != expected.get("axis_2_pole")
        ):
            raise DeliveryError("target_quadrant_lineage_mismatch", "target_segmentation_candidates", quadrant_id)


def _persona_snapshot_state(run_dir: Path) -> str:
    """`current` when persona_confirmed.yaml holds the digest of the present
    persona_content.yaml; otherwise why not."""
    confirmed_path = run_dir / "persona_confirmed.yaml"
    content_path = run_dir / "persona_content.yaml"
    if not confirmed_path.is_file() or not content_path.is_file():
        return "absent"
    snapshot = _read_yaml(confirmed_path, "persona_confirmed")
    content = _read_yaml(content_path, "persona_content")
    if not isinstance(snapshot, dict) or not isinstance(content, dict) or not content.get("personas"):
        return "absent"
    return "current" if snapshot.get("persona_digest") == lifecycle.canonical_content_digest(content) else "needs_confirmation"


def build_jtbd_persona_view(run_dir: str | Path) -> DeliveryViewModel:
    """Build a normalized JTBD + Persona projection from one current run.

    The adapter reads only run-local required artifacts, validates their
    identities and cross-references, and uses the repository's current
    segmentation/target gate resolver. It performs no writes, generation, or
    model/API calls. The returned model records absent required rationale as
    ``unsupported`` rather than manufacturing a replacement.
    """
    run_path = Path(run_dir).resolve()
    if not run_path.is_dir():
        raise DeliveryError("run_directory_missing", "run", run_path.name)
    run_json_path = _required_file(run_path, "run.json", "run.json")
    run_state = _read_json(run_json_path, "run.json")
    if not isinstance(run_state, dict):
        raise DeliveryError("run_metadata_invalid", "run.json", "expected object")

    loaded: dict[str, Any] = {
        "run.json": run_state,
    }
    for filename in _DELIVERY_SOURCE_FILES:
        artifact = filename.removesuffix(".yaml")
        loaded[filename] = _read_required_yaml(run_path, filename, artifact)
    # The current workflow (merge-persona-matrix-selection-into-one-screen)
    # computes quadrants from the matrix and the confirmed axes and writes no
    # separate candidates file; an older run that has one is still checked.
    # multi-jtbd-single-job-source (2026-10-03): the JTBDs are the researcher's
    # selected list (one JTBD source). jtbd.yaml is read only for older runs
    # that never recorded a list selection.
    selection_path = run_path / "narrative_job_selection.yaml"
    listed = _read_yaml(selection_path, "narrative_job_selection") if selection_path.is_file() else None
    jtbd_from_selection = isinstance(listed, dict) and listed.get("schema_version") == 2
    if jtbd_from_selection:
        loaded["jtbd.yaml"] = {"case_id": listed.get("case_id"), "jobs": [
            {"id": f"JTBD-{index}", "label": item.get("progress"), "situation": item.get("situation"),
             "progress": item.get("progress"), "motivation": item.get("motivation"),
             "outcome": item.get("outcome"), "grounded_in": list(item.get("grounded_in") or [])}
            for index, item in enumerate(listed.get("selections") or [], start=1) if isinstance(item, dict)]}
    else:
        loaded["jtbd.yaml"] = _read_required_yaml(run_path, "jtbd.yaml", "jtbd")
    candidates_path = run_path / "target_segmentation_candidates.yaml"
    loaded["target_segmentation_candidates.yaml"] = (
        _read_yaml(candidates_path, "target_segmentation_candidates") if candidates_path.is_file() else None)

    case_doc = loaded["case.yaml"]
    if not isinstance(case_doc, dict):
        raise DeliveryError("case_identity_invalid", "case.yaml", "expected mapping")
    case_id = run_state.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise DeliveryError("run_identity_invalid", "run.json", "case_id is required")
    _check_case_id(case_doc, case_id, "case")

    for state_name in _REQUIRED_RUN_STATES:
        # The merged Persona screen records the two selections as human gates
        # (checked below), not as run states; a recorded state must still be complete.
        if state_name in _GATED_SELECTION_STATES and f"{state_name}_state" not in run_state:
            continue
        if state_name == "jtbd" and jtbd_from_selection:
            continue
        _require_complete_state(run_state, state_name)

    evidence = loaded["evidence.yaml"]
    if not isinstance(evidence, list):
        raise DeliveryError("required_artifact_invalid", "evidence", "expected array")
    _require_valid(_schema_errors(evidence, "evidence"), "evidence")
    evidence_records = [item for item in evidence if isinstance(item, dict)]
    if len(evidence_records) != len(evidence):
        raise DeliveryError("required_artifact_invalid", "evidence", "expected object records")
    evidence_ids = {item.get("id") for item in evidence_records if isinstance(item.get("id"), str)}
    if len(evidence_ids) != len(evidence_records):
        raise DeliveryError("duplicate_record_id", "evidence", "id")

    axial = loaded["axial_coding.yaml"]
    if not isinstance(axial, dict):
        raise DeliveryError("required_artifact_invalid", "axial_coding", "expected mapping")
    _require_complete_state(run_state, "axial_coding")
    _require_valid(_schema_errors(axial, "axial-coding"), "axial_coding")
    _require_valid(stages._axial_coding_errors(axial, evidence_ids), "axial_coding")
    _check_case_id(axial, case_id, "axial_coding")
    categories = [item for item in axial.get("categories", []) if isinstance(item, dict)]
    category_ids = {item.get("id") for item in categories if isinstance(item.get("id"), str)}

    jtbd = loaded["jtbd.yaml"]
    if not isinstance(jtbd, dict):
        raise DeliveryError("required_artifact_invalid", "jtbd", "expected mapping")
    _check_case_id(jtbd, case_id, "jtbd")
    if not jtbd_from_selection:
        _require_valid(stages._jtbd_errors(jtbd, category_ids, evidence_ids), "jtbd")

    matrix = loaded["persona_matrix.yaml"]
    if not isinstance(matrix, dict):
        raise DeliveryError("required_artifact_invalid", "persona_matrix", "expected mapping")
    _check_case_id(matrix, case_id, "persona_matrix")
    axes = [item for item in matrix.get("axes", []) if isinstance(item, dict)]
    subjects = [item for item in matrix.get("subjects", []) if isinstance(item, dict)]
    axis_by_id = {item.get("id"): item for item in axes if isinstance(item.get("id"), str)}
    subject_by_id = {item.get("id"): item for item in subjects if isinstance(item.get("id"), str)}
    if len(axis_by_id) != len(axes) or len(subject_by_id) != len(subjects):
        raise DeliveryError("duplicate_record_id", "persona_matrix", "axis or subject id")

    axis_selection = loaded["segmentation_axis_selection.yaml"]
    if not isinstance(axis_selection, dict):
        raise DeliveryError("required_artifact_invalid", "segmentation_axis_selection", "expected mapping")
    _check_case_id(axis_selection, case_id, "segmentation_axis_selection")
    axis_ids = axis_selection.get("axis_ids") or []
    selected_axes = [axis_by_id[axis_id] for axis_id in axis_ids if axis_id in axis_by_id]
    _require_valid(stages._segmentation_axis_selection_errors(axis_selection, set(axis_by_id)), "segmentation_axis_selection")
    # Matrix axes are a broader candidate set. The separately confirmed
    # segmentation selection intentionally narrows that set to two axes only
    # for quadrant construction; it must not be used to validate or filter the
    # upstream Persona Matrix itself.
    _require_valid(stages._persona_matrix_errors(matrix, category_ids, evidence_ids), "persona_matrix")

    candidate_doc = loaded["target_segmentation_candidates.yaml"]
    quadrants, unclassified_subject_ids = stages.assign_target_segmentation_quadrants(matrix, axis_ids)
    quadrant_ids = {item["id"] for item in quadrants}
    if candidate_doc is not None:
        if not isinstance(candidate_doc, dict):
            raise DeliveryError("required_artifact_invalid", "target_segmentation_candidates", "expected mapping")
        _check_case_id(candidate_doc, case_id, "target_segmentation_candidates")
        _require_complete_state(run_state, "target_segmentation_candidates")
        _require_valid(_schema_errors(candidate_doc, "target-segmentation"), "target_segmentation_candidates")
        if candidate_doc.get("axis_ids") != axis_ids:
            raise DeliveryError("target_quadrant_lineage_mismatch", "target_segmentation_candidates", "axis_ids")
        _validate_candidate_quadrants(candidate_doc, quadrants)

    target_selection = loaded["target_segmentation_selection.yaml"]
    if not isinstance(target_selection, dict):
        raise DeliveryError("required_artifact_invalid", "target_segmentation_selection", "expected mapping")
    _check_case_id(target_selection, case_id, "target_segmentation_selection")
    _require_valid(
        stages._target_segmentation_selection_errors(
            target_selection,
            quadrant_ids,
            set(subject_by_id),
            set(unclassified_subject_ids),
            {q["id"] for q in quadrants if q.get("suggested_subject_ids")},
        ),
        "target_segmentation_selection",
    )

    now = "delivery-read-only"
    axis_gate_state, axis_gate_origin = _gate_confirmation(run_path, "segmentation_axes", now)
    target_gate_state, target_gate_origin = _gate_confirmation(run_path, "target_segmentation", now)
    if axis_gate_state != "confirmed":
        raise DeliveryError("required_human_gate_not_confirmed", "segmentation_axes", axis_gate_state)
    if target_gate_state != "confirmed":
        raise DeliveryError("required_human_gate_not_confirmed", "target_segmentation", target_gate_state)
    try:
        ledger = gates._ledger_or_legacy_import(run_path, gates._read_ledger(run_path), now)
        persona_derivation_state = gates.resolve_selected_status(
            run_path,
            ledger,
            gates.PERSONA_DEPENDENCY_MAP,
            gates.derivation("persona_content"),
        )
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
        raise DeliveryError("persona_derivation_resolution_failed", "persona_content", type(exc).__name__) from exc
    if persona_derivation_state == "absent":
        # The current workflow confirms a Persona by snapshotting its content
        # digest in persona_confirmed.yaml rather than recording a ledger
        # derivation; it is current only while that digest still matches.
        persona_derivation_state = _persona_snapshot_state(run_path)
    if persona_derivation_state != "current":
        raise DeliveryError("required_artifact_not_current", "persona_content", persona_derivation_state)

    target_id = target_selection.get("primary_quadrant_id")
    confirmed_members_by_quadrant = {
        item.get("quadrant_id"): list(item.get("subject_ids") or [])
        for item in target_selection.get("quadrant_members", [])
        if isinstance(item, dict) and isinstance(item.get("quadrant_id"), str)
    }
    confirmed_member_ids = confirmed_members_by_quadrant.get(target_id, [])
    if not confirmed_member_ids:
        raise DeliveryError("primary_target_has_no_confirmed_members", "target_segmentation_selection", target_id)
    unassigned_subject_ids = sorted(set(subject_by_id) - {
        subject_id for member_ids in confirmed_members_by_quadrant.values() for subject_id in member_ids
    })

    persona_content = loaded["persona_content.yaml"]
    if not isinstance(persona_content, dict):
        raise DeliveryError("required_artifact_invalid", "persona_content", "expected mapping")
    _check_case_id(persona_content, case_id, "persona_content")
    personas = persona_content.get("personas") or []
    evidence_refs_by_subject = {
        subject_id: set(subject.get("evidence_refs") or [])
        for subject_id, subject in subject_by_id.items()
    }
    _require_valid(
        stages._persona_content_errors(
            persona_content,
            set(subject_by_id),
            target_id if personas else None,
            evidence_refs_by_subject,
            # The delivered artifact may carry the researcher's own card edits.
            allow_researcher=True,
        ),
        "persona_content",
    )
    if personas:
        if len(personas) != 1:
            raise DeliveryError("persona_target_cardinality_mismatch", "persona_content", len(personas))
        persona = personas[0]
        if set(persona.get("member_subject_ids") or []) != set(confirmed_member_ids):
            raise DeliveryError("persona_target_membership_mismatch", "persona_content", target_id)
    else:
        outcome = _persona_outcome(persona_content, target_id)
        if not isinstance(outcome, dict) or outcome.get("status") not in {
            "insufficient_evidence", "researcher_deferred",
        }:
            raise DeliveryError("persona_empty_outcome_missing", "persona_content", target_id)

    # Validate all canonical references carried by Persona provenance. The
    # stage validator checks membership; this extra pass ensures evidence IDs
    # resolve in the selected run's exact evidence artifact.
    for persona in personas:
        for entry in persona.get("field_provenance", []) or []:
            for evidence_id in entry.get("evidence_refs", []) or []:
                if evidence_id not in evidence_ids:
                    raise DeliveryError("persona_evidence_reference_missing", "persona_content", evidence_id)

    evidence_version_id = _evidence_version(run_path, run_state, case_id, evidence_records)
    project_id = case_doc.get("project_id") or run_state.get("project_id")
    identity_fields = {"person_name", "avatar"}
    identity_available = any(
        isinstance(persona, dict) and any(persona.get(field) for field in identity_fields)
        for persona in personas
    )
    primary_rationale = target_selection.get("primary_rationale")
    rationale_recorded = isinstance(primary_rationale, str) and bool(primary_rationale.strip())

    source_snapshot: list[dict[str, Any]] = [
        _source_ref("run", "run.json", run_state, run_state=run_state),
        _source_ref("case", "case.yaml", case_doc, run_state=run_state),
        _source_ref("evidence", "evidence.yaml", evidence_records, run_state=run_state,
                    version_ref=evidence_version_id,
                    extra={"resolution_method": "run_local"}),
        _source_ref("axial_coding", "axial_coding.yaml", axial, run_state=run_state),
        _source_ref("jtbd", "narrative_job_selection.yaml" if jtbd_from_selection else "jtbd.yaml", jtbd, run_state=run_state),
        _source_ref("segmentation_axis_selection", "segmentation_axis_selection.yaml", axis_selection,
                    run_state=run_state,
                    extra={"gate_state": axis_gate_state, "confirmation_origin": axis_gate_origin}),
        *([_source_ref("target_segmentation_candidates", "target_segmentation_candidates.yaml", candidate_doc,
                       run_state=run_state)] if candidate_doc is not None else []),
        _source_ref("target_segmentation_selection", "target_segmentation_selection.yaml", target_selection,
                    run_state=run_state,
                    extra={"gate_state": target_gate_state, "confirmation_origin": target_gate_origin}),
        _source_ref("persona_matrix", "persona_matrix.yaml", matrix, run_state=run_state),
        _source_ref("persona_content", "persona_content.yaml", persona_content, run_state=run_state,
                    extra={"derivation_state": persona_derivation_state}),
    ]
    registry_value = run_state.get("artifact_registry")
    if isinstance(registry_value, str) and registry_value.strip():
        registry_path = _resolve_registry_path(run_path, registry_value)
        registry_data = _read_yaml(registry_path, "artifact_registry")
        source_snapshot.append(_source_ref(
            "artifact_registry",
            "run.json:artifact_registry",
            registry_data,
            run_state=run_state,
            extra={"resolved_path": str(registry_path)},
        ))

    availability: dict[str, dict[str, Any]] = {
        "jtbd": {"status": "ready", "reason": None},
        "segmentation_axes": {"status": "ready", "reason": None},
        "target_selection": {"status": "ready", "reason": None},
        "persona_matrix": {"status": "ready", "reason": None},
        "persona": {"status": "ready", "reason": None},
        "target_rationale": {
            "status": "ready" if rationale_recorded else "unavailable",
            "reason": None if rationale_recorded else "target_rationale_not_recorded",
        },
        "illustrative_identity": {
            "status": "ready" if identity_available else "unavailable",
            "reason": None if identity_available else "optional_identity_fields_not_provided",
        },
    }
    if not personas:
        target_outcome = _persona_outcome(persona_content, target_id) or {}
        outcome_status = target_outcome.get("status")
        availability["persona"] = {
            "status": "insufficient_evidence" if outcome_status == "insufficient_evidence" else "unavailable",
            "reason": outcome_status,
        }

    notices: list[dict[str, str]] = []
    if not rationale_recorded:
        notices.append({
            "code": "target_rationale_not_recorded",
            "artifact": "target_segmentation_selection",
            "detail": "研究者確認的 primary_rationale 為空；交付不得代填。",
        })
    unreferenced_direct_fields = sorted({
        entry.get("field")
        for persona in personas
        for entry in (persona.get("field_provenance") or [])
        if isinstance(entry, dict)
        and entry.get("type") == "research_evidence"
        and not entry.get("evidence_refs")
        and isinstance(entry.get("field"), str)
    })
    if unreferenced_direct_fields:
        notices.append({
            "code": "persona_direct_evidence_refs_not_recorded",
            "artifact": "persona_content",
            "detail": ",".join(unreferenced_direct_fields),
        })

    referenced_evidence_ids: set[str] = {
        evidence_id
        for job in jtbd.get("jobs", []) if isinstance(job, dict)
        for evidence_id in job.get("evidence_refs", []) or []
    }
    for axis in selected_axes:
        for category_id in axis.get("grounded_in", []) or []:
            category = next((item for item in categories if item.get("id") == category_id), None)
            if category:
                referenced_evidence_ids.update(category.get("evidence_refs") or [])
    for member_ids in confirmed_members_by_quadrant.values():
        for subject_id in member_ids:
            referenced_evidence_ids.update(evidence_refs_by_subject.get(subject_id, set()))
    for persona in personas:
        quote = persona.get("representative_quote")
        if isinstance(quote, dict) and quote.get("evidence_ref"):
            referenced_evidence_ids.add(quote["evidence_ref"])
        for entry in persona.get("field_provenance", []) or []:
            if isinstance(entry, dict):
                referenced_evidence_ids.update(entry.get("evidence_refs") or [])
    evidence_by_id = {record["id"]: record for record in evidence_records}
    evidence_sources = []
    for evidence_record in evidence_records:
        evidence_id = evidence_record["id"]
        if evidence_id not in referenced_evidence_ids:
            continue
        evidence_sources.append({
            "evidence_id": evidence_id,
            "source": evidence_record.get("source"),
            "locator": evidence_record.get("locator"),
            "role": evidence_record.get("role"),
            "tier": evidence_record.get("tier"),
        })
    unresolved_evidence_refs = sorted(referenced_evidence_ids - set(evidence_by_id))
    if unresolved_evidence_refs:
        raise DeliveryError("delivery_evidence_reference_missing", "evidence", ",".join(unresolved_evidence_refs))

    normalized_quadrants = []
    for quadrant in quadrants:
        quadrant_id = quadrant["id"]
        member_ids = list(confirmed_members_by_quadrant.get(quadrant_id, []))
        refs = sorted({
            evidence_ref
            for subject_id in member_ids
            for evidence_ref in evidence_refs_by_subject.get(subject_id, set())
        })
        normalized_quadrants.append({
            "id": quadrant_id,
            "axis_1_pole": quadrant["axis_1_pole"],
            "axis_2_pole": quadrant["axis_2_pole"],
            "member_subject_ids": member_ids,
            "member_count": len(member_ids),
            "evidence_refs": refs,
            "evidence_count": len(refs),
            "membership_source": "target_segmentation_selection.yaml",
        })

    # 2026-10-02 researcher ruling: the export reflects the research record. A
    # target chosen without a recorded rationale is exported as such
    # (研究者未記錄選擇理由); no rationale is inferred or written for the researcher,
    # and no Persona-screen field is added to satisfy the export.
    status = "ready"
    payload = {
        "view_model_schema_version": 1,
        "delivery_type": JTBD_PERSONA,
        "export_id": JTBD_PERSONA,
        "status": status,
        "metadata": {
            "run_ref": run_path.name,
            "project_id": project_id if isinstance(project_id, str) else None,
            "case_id": case_id,
            "run_states": {name: run_state.get(f"{name}_state") for name in _REQUIRED_RUN_STATES},
        },
        "source_snapshot": source_snapshot,
        "jtbd": copy.deepcopy(jtbd),
        "segmentation": {
            "confirmed_axis_ids": list(axis_ids),
        "confirmed_axes": [
            {
                **copy.deepcopy(axis),
                "grounded_categories": [
                    {
                        "id": category_id,
                        "label": next((item.get("label") for item in categories if item.get("id") == category_id), None),
                    }
                    for category_id in axis.get("grounded_in", []) or []
                    if any(item.get("id") == category_id for item in categories)
                ],
            }
            for axis in selected_axes
        ],
            "quadrants": normalized_quadrants,
            "confirmed_membership_subject_count": sum(len(ids) for ids in confirmed_members_by_quadrant.values()),
            "matrix_subject_count": len(subject_by_id),
            "subjects_without_confirmed_quadrant": unassigned_subject_ids,
            "target_quadrant_id": target_id,
            "target_member_subject_ids": list(confirmed_member_ids),
            "target_rationale": primary_rationale if rationale_recorded else None,
            "target_confirmation": {
                "state": target_gate_state,
                "origin": target_gate_origin,
                "source_ref": "target_segmentation_selection.yaml",
            },
        },
        "persona_content": copy.deepcopy(persona_content),
        "provenance": {
            "jtbd": {
                "source_ref": "narrative_job_selection.yaml" if jtbd_from_selection else "jtbd.yaml",
                "run_state": run_state.get("narrative_job_selection_state" if jtbd_from_selection else "jtbd_state"),
                "confirmation_state": "researcher_selected" if jtbd_from_selection else "not_recorded",
            },
            "segmentation_axes": {
                "source_ref": "segmentation_axis_selection.yaml",
                "confirmation_state": axis_gate_state,
                "confirmation_origin": axis_gate_origin,
            },
            "target": {
                "source_ref": "target_segmentation_selection.yaml",
                "confirmation_state": target_gate_state,
                "confirmation_origin": target_gate_origin,
            },
            "evidence_sources": evidence_sources,
            "persona_content": {
                "source_ref": "persona_content.yaml",
                "run_state": run_state.get("persona_content_state"),
                "derivation_state": persona_derivation_state,
            },
        },
        "availability": availability,
        "notices": notices,
    }
    return DeliveryViewModel.from_payload(payload)
