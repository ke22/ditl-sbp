"""Read-only Local V1 ``customer_journey`` delivery adapter.

Journey delivery is resolved from the selected run's lifecycle-current
``journey.yaml`` plus its referenced evidence.  The older authored ``case.yaml``
step list is used only for case identity/project metadata and never replaces
the confirmed Journey axis.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

import yaml

from . import lifecycle, stages
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


CUSTOMER_JOURNEY = "customer_journey"


def _required_file(run_dir: Path, filename: str, artifact: str) -> Path:
    path = run_dir / filename
    if not path.is_file():
        raise DeliveryError("required_artifact_missing", artifact, filename)
    return path


def _check_case_id(data: Mapping[str, Any], case_id: str, artifact: str) -> None:
    if data.get("case_id") != case_id:
        raise DeliveryError("artifact_case_identity_mismatch", artifact, data.get("case_id"))


def _current_lifecycle_version(
    run_dir: Path,
    run_state: Mapping[str, Any],
    case_id: str,
    artifact_type: str,
    data: Any,
) -> str | None:
    """Validate an explicitly recorded lifecycle pointer when present.

    Older run fixtures without lifecycle metadata remain run-scoped and are
    accepted as the selected run's current file.  A declared registry is
    always checked for exact type, current status, and content digest.
    """
    registry_value = run_state.get("artifact_registry")
    versions = run_state.get("artifact_versions")
    if registry_value is None and versions is None:
        return None
    if not isinstance(registry_value, str) or not registry_value.strip() or not isinstance(versions, dict):
        raise DeliveryError("lifecycle_metadata_incomplete", artifact_type)
    version_id = versions.get(artifact_type)
    if not isinstance(version_id, str) or not version_id:
        raise DeliveryError("lifecycle_version_missing", artifact_type)
    registry_path = _resolve_registry_path(run_dir, registry_value)
    registry = _read_yaml(registry_path, "artifact_registry")
    errors = lifecycle.lifecycle_errors(registry)
    if errors:
        raise DeliveryError("lifecycle_registry_invalid", artifact_type, "; ".join(errors))
    if registry.get("case_id") != case_id:
        raise DeliveryError("lifecycle_case_mismatch", artifact_type)
    records = {
        item["version_id"]: item
        for item in registry.get("versions", [])
        if isinstance(item, dict) and isinstance(item.get("version_id"), str)
    }
    record = records.get(version_id)
    if not isinstance(record, dict) or record.get("artifact_type") != artifact_type:
        raise DeliveryError("lifecycle_version_mismatch", artifact_type)
    if lifecycle.resolve_lifecycle(registry).get(version_id) != "current":
        raise DeliveryError("lifecycle_version_not_current", artifact_type)
    content_digest = _digest(data, artifact_type)
    if record.get("content_digest") != content_digest:
        raise DeliveryError("lifecycle_content_digest_mismatch", artifact_type)
    return version_id


def _source_ref(
    artifact_type: str,
    filename: str,
    data: Any,
    *,
    run_state: Mapping[str, Any],
    version_ref: str | None = None,
    status: str = "ready",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ref: dict[str, Any] = {
        "artifact_type": artifact_type,
        "source_ref": filename,
        "status": status,
        "version_ref": version_ref,
        "content_digest": _digest(data, artifact_type) if data is not None else None,
        "versioning": "lifecycle_current" if version_ref else "run_scoped_current_file",
    }
    recorded_state = run_state.get(f"{artifact_type}_state")
    if recorded_state is not None:
        ref["recorded_state"] = recorded_state
    if extra:
        ref.update(copy.deepcopy(dict(extra)))
    return ref


def _conflict_records(value: Any, evidence_ids: set[str]) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise DeliveryError("optional_artifact_invalid", "conflicts", "expected array")
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise DeliveryError("optional_artifact_invalid", "conflicts", "id is required")
        if item["id"] in seen:
            raise DeliveryError("duplicate_record_id", "conflicts", "id")
        seen.add(item["id"])
        refs = item.get("evidence_refs") or []
        missing = sorted(set(refs) - evidence_ids)
        if missing:
            raise DeliveryError("delivery_evidence_reference_missing", "conflicts", ",".join(missing))
        records.append(copy.deepcopy(item))
    return records


def _step_view(
    step: Mapping[str, Any],
    order: int,
    *,
    preserve_content_provenance: bool = False,
) -> dict[str, Any]:
    refs = list(step.get("evidence_refs") or [])
    provenance = {
        "status": "researcher_confirmed",
        "source_ref": "journey.yaml",
        "evidence_refs": refs,
    }
    if preserve_content_provenance:
        # v3 records an optional content provenance value on each step.  Keep
        # its absence explicit rather than upgrading the step to an observed
        # or confirmed research claim.
        provenance = {
            "content_status": step.get("provenance") if step.get("provenance") is not None else "unavailable",
            "source_ref": "journey.yaml",
            "evidence_refs": refs,
        }
    return {
        "step_id": step.get("id"),
        "order": order,
        "label": step.get("label"),
        "description": step.get("description"),
        "evidence_refs": refs,
        "evidence_count": len(refs),
        "goal": {
            "status": "unavailable",
            "reason": "step_goal_not_recorded_in_confirmed_journey",
        },
        "touchpoint_channel": {
            "status": "unresolved",
            "value": None,
            "reason": "channel_not_recorded_in_confirmed_journey",
            "blocking": False,
        },
        "pain_friction": {
            "status": "unavailable",
            "reason": "not_separately_classified_in_confirmed_journey",
        },
        "provenance": provenance,
    }


def _branch_view(branch: Mapping[str, Any]) -> dict[str, Any]:
    """Project a v3 Branch without treating it as a scenario or a flat path."""
    return {
        "branch_id": branch.get("branch_id"),
        "label": branch.get("label"),
        "divergence_step_id": branch.get("divergence_step_id"),
        "merge_step_id": branch.get("merge_step_id"),
        "topology_provenance": branch.get("topology_provenance"),
        "steps": [
            _step_view(step, index, preserve_content_provenance=True)
            for index, step in enumerate(branch.get("steps") or [], 1)
            if isinstance(step, Mapping)
        ],
    }


def build_customer_journey_view(run_dir: str | Path) -> DeliveryViewModel:
    """Build a normalized Customer Journey projection from one selected run."""
    run_path = Path(run_dir).resolve()
    if not run_path.is_dir():
        raise DeliveryError("run_directory_missing", "run", run_path.name)

    run_state = _read_json(_required_file(run_path, "run.json", "run.json"), "run.json")
    if not isinstance(run_state, dict):
        raise DeliveryError("run_metadata_invalid", "run.json", "expected object")
    case_doc = _read_yaml(_required_file(run_path, "case.yaml", "case"), "case")
    journey = _read_yaml(_required_file(run_path, "journey.yaml", "journey"), "journey")
    evidence = _read_yaml(_required_file(run_path, "evidence.yaml", "evidence"), "evidence")
    if not isinstance(case_doc, dict):
        raise DeliveryError("case_identity_invalid", "case", "expected mapping")
    if not isinstance(run_state.get("case_id"), str) or not run_state["case_id"]:
        raise DeliveryError("run_identity_invalid", "run.json", "case_id is required")
    case_id = run_state["case_id"]
    _check_case_id(case_doc, case_id, "case")
    if run_state.get("journey_state") != "complete":
        raise DeliveryError("required_artifact_not_current", "journey", run_state.get("journey_state", "state_missing"))
    if not isinstance(journey, dict):
        raise DeliveryError("required_artifact_invalid", "journey", "expected mapping")
    if not isinstance(evidence, list):
        raise DeliveryError("required_artifact_invalid", "evidence", "expected array")
    _check_case_id(journey, case_id, "journey")
    _require_valid(_schema_errors(journey, "journey"), "journey")
    evidence_records = [item for item in evidence if isinstance(item, dict)]
    if len(evidence_records) != len(evidence):
        raise DeliveryError("required_artifact_invalid", "evidence", "expected object records")
    evidence_ids = {item.get("id") for item in evidence_records if isinstance(item.get("id"), str)}
    if len(evidence_ids) != len(evidence_records):
        raise DeliveryError("duplicate_record_id", "evidence", "id")
    _require_valid(stages._journey_errors(journey, evidence_ids, require_confirmed=True), "journey")
    evidence_version_id = _evidence_version(run_path, run_state, case_id, evidence_records)
    journey_version_id = _current_lifecycle_version(
        run_path, run_state, case_id, "confirmed_journey", journey
    )

    schema_version = journey.get("schema_version")
    scenarios = journey.get("scenarios") or []
    normalized_scenarios = []
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            raise DeliveryError("required_artifact_invalid", "journey", "scenario must be object")
        steps = scenario.get("steps") or []
        normalized_scenarios.append({
            "scenario_id": scenario.get("id"),
            "label": scenario.get("label"),
            "status": scenario.get("status"),
            "is_primary": scenario.get("id") == journey.get("primary_scenario"),
            "branch_relation": "primary_path" if scenario.get("id") == journey.get("primary_scenario") else "alternative_path",
            "customer_goal": scenario.get("customer_goal"),
            "description": scenario.get("description"),
            "object_types": list(scenario.get("object_types") or []),
            "steps": [
                _step_view(step, index, preserve_content_provenance=schema_version == 3)
                for index, step in enumerate(steps, 1)
            ],
        })

    # v2 has no Branch read surface by contract.  v3 keeps Branches as a
    # separate collection, including an explicit empty collection when the
    # canonical document has no Branches.
    normalized_branches = [
        _branch_view(branch)
        for branch in (journey.get("branches") or [])
        if isinstance(branch, Mapping)
    ] if schema_version == 3 else None

    conflicts_path = run_path / "conflicts.yaml"
    if conflicts_path.is_file():
        conflicts_data = _read_yaml(conflicts_path, "conflicts")
        conflicts = _conflict_records(conflicts_data, evidence_ids)
        conflicts_state = {"status": "ready", "reason": None}
        conflicts_ref = _source_ref("conflicts", "conflicts.yaml", conflicts_data, run_state=run_state)
    else:
        conflicts = []
        conflicts_state = {"status": "unavailable", "reason": "optional_source_missing"}
        conflicts_ref = _source_ref("conflicts", "conflicts.yaml", None, run_state=run_state, status="unavailable")

    referenced_evidence_ids = {
        ref
        for scenario in normalized_scenarios
        for step in scenario["steps"]
        for ref in step["evidence_refs"]
    }
    if normalized_branches is not None:
        for branch in normalized_branches:
            for step in branch["steps"]:
                referenced_evidence_ids.update(step.get("evidence_refs") or [])
    for conflict in conflicts:
        referenced_evidence_ids.update(conflict.get("evidence_refs") or [])
    evidence_by_id = {record["id"]: record for record in evidence_records}
    evidence_sources = [
        {
            "evidence_id": evidence_id,
            "source": evidence_by_id[evidence_id].get("source"),
            "locator": evidence_by_id[evidence_id].get("locator"),
            "role": evidence_by_id[evidence_id].get("role"),
            "tier": evidence_by_id[evidence_id].get("tier"),
        }
        for evidence_id in sorted(referenced_evidence_ids)
        if evidence_id in evidence_by_id
    ]

    snapshot = [
        _source_ref("run", "run.json", run_state, run_state=run_state),
        _source_ref("case", "case.yaml", case_doc, run_state=run_state),
        _source_ref("evidence", "evidence.yaml", evidence_records, run_state=run_state, version_ref=evidence_version_id, extra={"resolution_method": "run_local"}),
        _source_ref("confirmed_journey", "journey.yaml", journey, run_state=run_state, version_ref=journey_version_id, extra={"resolution_method": "lifecycle_current"}),
        conflicts_ref,
    ]
    availability = {
        "journey": {"status": "ready", "reason": None},
        "evidence": {"status": "ready", "reason": None},
        "touchpoint_channel": {
            "status": "unavailable",
            "classification": "unresolved",
            "reason": "channel_not_recorded_in_confirmed_journey",
            "blocking": False,
        },
        "conflicts": conflicts_state,
        "emotion": {"status": "unsupported", "reason": "not_recorded_in_confirmed_journey"},
        "nps": {"status": "unsupported", "reason": "not_recorded_in_confirmed_journey"},
        "opportunity": {"status": "unsupported", "reason": "not_recorded_in_confirmed_journey"},
    }
    payload = {
        "view_model_schema_version": 1,
        "delivery_type": CUSTOMER_JOURNEY,
        "export_id": CUSTOMER_JOURNEY,
        "status": "ready",
        "metadata": {
            "run_ref": run_path.name,
            "project_id": case_doc.get("project_id") or run_state.get("project_id"),
            "case_id": case_id,
            "journey_state": run_state.get("journey_state"),
            "primary_scenario": journey.get("primary_scenario"),
            "scenario_count": len(normalized_scenarios),
            "confirmed_step_counts": {scenario["scenario_id"]: len(scenario["steps"]) for scenario in normalized_scenarios},
        },
        "source_snapshot": snapshot,
        "scenarios": normalized_scenarios,
        "unresolved_mappings": {
            "channel": {
                "status": "unresolved",
                "blocking": False,
                "count": sum(len(scenario["steps"]) for scenario in normalized_scenarios),
                "reason": "confirmed_journey schema does not record channel assignments",
            },
        },
        "conflicts": conflicts,
        "provenance": {
            "journey": {"source_ref": "journey.yaml", "state": "confirmed", "version_ref": journey_version_id},
            "evidence_sources": evidence_sources,
            "conflicts": {"source_ref": "conflicts.yaml", "status": conflicts_state["status"]},
        },
        "availability": availability,
        "notices": [
            {
                "code": "unresolved_channel_non_blocking",
                "detail": "確認過的 Journey 未記錄渠道分類；這不阻擋交付或繼續下游工作。",
            },
            {
                "code": "unsupported_optional_fields",
                "detail": "本交付不補寫情緒、NPS、機會或方案欄位。",
            },
        ],
    }
    if normalized_branches is not None:
        payload["branches"] = normalized_branches
    return DeliveryViewModel.from_payload(payload)
