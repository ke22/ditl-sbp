"""Read-only research delivery view models and Local V1 adapters.

The first adapter projects run-scoped coding artifacts into the normalized
``coding_synthesis`` delivery shape. It never writes to the run or source data.
"""
from __future__ import annotations

import copy
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import evidence_locator
import storage_locations
import validate_sbp as V
import yaml

from . import lifecycle, stages


DELIVERY_VIEW_SCHEMA_VERSION = 1
CODING_SYNTHESIS = "coding_synthesis"
VIEW_STATUSES = frozenset({
    "ready",
    "unsupported",
    "insufficient_evidence",
    "unavailable",
    "not_applicable",
    "error",
})

OPTIONAL_CODING_CONTEXT: dict[str, tuple[str, str]] = {
    "narrative_job_candidates": ("narrative_job_candidates.yaml", "narrative-job-candidates"),
    "narrative_job_selection": ("narrative_job_selection.yaml", "narrative-job-selection"),
    "jtbd": ("jtbd.yaml", "jtbd"),
    "primary_hmw_selection": ("primary_hmw_selection.yaml", "primary-hmw-selection"),
}


class DeliveryError(ValueError):
    """Stable, structured failure raised when a delivery source cannot be used."""

    def __init__(self, code: str, artifact: str, detail: object | None = None):
        self.code = code
        self.artifact = artifact
        self.detail = str(detail) if detail is not None else None
        message = f"{code}: {artifact}"
        if self.detail:
            message += f" ({self.detail})"
        super().__init__(message)

    def as_dict(self) -> dict[str, str | None]:
        return {"code": self.code, "artifact": self.artifact, "detail": self.detail}


def _resolve_registry_path(
    run_dir: Path, value: str | Path, artifact: str = "artifact_registry"
) -> Path:
    try:
        return storage_locations.resolve_registry_pointer(
            value, run_dir, storage_locations.runs_root_for_run(run_dir)
        )
    except storage_locations.RegistryPointerError as exc:
        raise DeliveryError(
            "artifact_registry_pointer_unrecognized", artifact, exc.pointer
        ) from exc


@dataclass(frozen=True, slots=True)
class DeliveryViewModel:
    """Immutable serialized delivery projection.

    The internal canonical JSON string cannot be mutated by callers. ``to_dict``
    returns a fresh object each time, so later renderer work cannot mutate this
    snapshot or any source artifact through a shared nested reference.
    """

    _payload_json: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "DeliveryViewModel":
        if not isinstance(payload, Mapping):
            raise DeliveryError("view_model_invalid", "delivery_view", "expected object")
        if not isinstance(payload.get("export_id"), str) or not payload.get("export_id"):
            raise DeliveryError("view_model_invalid", "delivery_view", "export_id is required")
        if payload.get("status") not in VIEW_STATUSES:
            raise DeliveryError("view_model_invalid", "delivery_view", "unknown status")
        source_snapshot = payload.get("source_snapshot", [])
        if not isinstance(source_snapshot, list):
            raise DeliveryError("view_model_invalid", "delivery_view", "source_snapshot must be an array")
        for source in source_snapshot:
            if not isinstance(source, Mapping) or source.get("status") not in VIEW_STATUSES:
                raise DeliveryError("view_model_invalid", "delivery_view", "invalid source status")
        availability = payload.get("availability", {})
        if not isinstance(availability, Mapping) or any(
            not isinstance(item, Mapping) or item.get("status") not in VIEW_STATUSES
            for item in availability.values()
        ):
            raise DeliveryError("view_model_invalid", "delivery_view", "invalid availability state")
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise DeliveryError("view_model_not_json_serializable", "delivery_view", exc) from exc
        return cls(encoded)

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._payload_json)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=indent)

    @property
    def export_id(self) -> str:
        return str(json.loads(self._payload_json)["export_id"])

    @property
    def delivery_type(self) -> str:
        return str(json.loads(self._payload_json)["delivery_type"])

    @property
    def status(self) -> str:
        return str(json.loads(self._payload_json)["status"])


def _read_yaml(path: Path, artifact: str) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise DeliveryError("required_artifact_unreadable", artifact, type(exc).__name__) from exc


def _read_json(path: Path, artifact: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeliveryError("required_metadata_unreadable", artifact, type(exc).__name__) from exc


def _digest(data: Any, artifact: str) -> str:
    try:
        return lifecycle.canonical_content_digest(data)
    except (TypeError, ValueError) as exc:
        raise DeliveryError("artifact_content_not_canonical_json", artifact, type(exc).__name__) from exc


def _schema_errors(data: Any, schema_name: str) -> list[str]:
    return V.structural_errors(data, V.load_schema(schema_name))


def _require_valid(errors: list[str], artifact: str) -> None:
    if errors:
        raise DeliveryError("required_artifact_invalid", artifact, "; ".join(errors))


def _unique_ids(records: list[dict[str, Any]], field: str, artifact: str) -> None:
    ids = [record.get(field) for record in records if isinstance(record, dict)]
    if len(ids) != len(set(ids)):
        raise DeliveryError("duplicate_record_id", artifact, field)


def _evidence_source_ref(resolution: str, run_state: Mapping[str, Any], case_id: str) -> str:
    if resolution == "run_local":
        return "evidence.yaml"
    if resolution == "run_json_pointer":
        for path in evidence_locator.EVIDENCE_POINTER_KEYS:
            cursor: Any = run_state
            for key in path:
                cursor = cursor.get(key) if isinstance(cursor, dict) else None
            if isinstance(cursor, str) and cursor.strip():
                return "run.json:" + ".".join(path)
        return "run.json:evidence_pointer"
    if resolution == "case_unique":
        return f"case_id:{case_id}:unique-evidence-source"
    return f"evidence_locator:{resolution}"


def _has_recorded_evidence_pointer(run_state: Mapping[str, Any]) -> bool:
    for path in evidence_locator.EVIDENCE_POINTER_KEYS:
        cursor: Any = run_state
        for key in path:
            cursor = cursor.get(key) if isinstance(cursor, dict) else None
        if isinstance(cursor, str) and cursor.strip():
            return True
    return False


def _optional_source(
    run_dir: Path,
    name: str,
    filename: str,
    schema_name: str,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    path = run_dir / filename
    if not path.is_file():
        state = {"status": "unavailable", "reason": "optional_source_missing"}
        ref = {
            "artifact_type": name,
            "source_ref": filename,
            "status": "unavailable",
            "version_ref": None,
            "content_digest": None,
        }
        return state, None, ref
    try:
        data = _read_yaml(path, name)
    except DeliveryError:
        state = {"status": "unsupported", "reason": "optional_source_unreadable"}
        ref = {
            "artifact_type": name,
            "source_ref": filename,
            "status": "unsupported",
            "version_ref": None,
            "content_digest": None,
        }
        return state, None, ref
    errors = _schema_errors(data, schema_name)
    if errors:
        state = {"status": "unsupported", "reason": "optional_source_invalid"}
        ref = {
            "artifact_type": name,
            "source_ref": filename,
            "status": "unsupported",
            "version_ref": None,
            "content_digest": None,
        }
        return state, None, ref
    digest = _digest(data, name)
    state = {"status": "ready", "reason": None}
    ref = {
        "artifact_type": name,
        "source_ref": filename,
        "status": "ready",
        "version_ref": None,
        "content_digest": digest,
    }
    return state, copy.deepcopy(data), ref


def _evidence_version(
    run_dir: Path,
    run_state: Mapping[str, Any],
    case_id: str,
    evidence: list[dict[str, Any]],
) -> str | None:
    """Verify lifecycle identity when a run declares it; support older runs
    with run-scoped files but never fall back to a different run/version."""
    registry_value = run_state.get("artifact_registry")
    versions = run_state.get("artifact_versions")
    if registry_value is None and versions is None:
        return None
    if not isinstance(registry_value, str) or not registry_value.strip() or not isinstance(versions, dict):
        raise DeliveryError("lifecycle_metadata_incomplete", "evidence")
    version_id = versions.get("evidence")
    if not isinstance(version_id, str) or not version_id:
        raise DeliveryError("lifecycle_version_missing", "evidence")
    registry_path = _resolve_registry_path(run_dir, registry_value)
    registry = _read_yaml(registry_path, "artifact_registry")
    errors = lifecycle.lifecycle_errors(registry)
    if errors:
        raise DeliveryError("lifecycle_registry_invalid", "evidence", "; ".join(errors))
    if registry.get("case_id") != case_id:
        raise DeliveryError("lifecycle_case_mismatch", "evidence")
    records = {
        item["version_id"]: item
        for item in registry.get("versions", [])
        if isinstance(item, dict) and isinstance(item.get("version_id"), str)
    }
    record = records.get(version_id)
    if not isinstance(record, dict) or record.get("artifact_type") != "evidence":
        raise DeliveryError("lifecycle_version_mismatch", "evidence")
    if lifecycle.resolve_lifecycle(registry).get(version_id) != "current":
        raise DeliveryError("lifecycle_version_not_current", "evidence")
    content_digest = _digest(evidence, "evidence")
    if record.get("content_digest") != content_digest:
        raise DeliveryError("lifecycle_content_digest_mismatch", "evidence")
    return version_id


def _coding_artifact(
    run_dir: Path,
    filename: str,
    artifact: str,
    schema_name: str,
    run_state: Mapping[str, Any],
    evidence_ids: set[str],
    category_ids: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    path = run_dir / filename
    if not path.is_file():
        raise DeliveryError("required_artifact_missing", artifact, filename)
    data = _read_yaml(path, artifact)
    _require_valid(_schema_errors(data, schema_name), artifact)
    recorded_state = run_state.get(f"{artifact}_state")
    if recorded_state is not None and recorded_state != "complete":
        raise DeliveryError("required_artifact_not_current", artifact, recorded_state)
    if not isinstance(data, dict):
        raise DeliveryError("required_artifact_invalid", artifact, "expected mapping")
    if artifact == "open_coding":
        _require_valid(V.open_coding_errors(data, evidence_ids), artifact)
        records = data.get("concepts") or []
        _unique_ids(records, "id", artifact)
    elif artifact == "axial_coding":
        _require_valid(stages._axial_coding_errors(data, evidence_ids), artifact)
        records = data.get("categories") or []
        _unique_ids(records, "id", artifact)
    elif artifact == "selective_coding":
        _require_valid(stages._selective_coding_errors(data, category_ids or set()), artifact)
        _require_valid(V.selective_coding_narrative_errors(data, evidence_ids), artifact)
    return copy.deepcopy(data), {
        "artifact_type": artifact,
        "source_ref": filename,
        "status": "ready",
        "version_ref": None,
        "content_digest": _digest(data, artifact),
        "recorded_state": recorded_state,
        "versioning": "run_scoped_current_file",
    }


def _selected_job_synthesis(run_path: Path, category_ids: set[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The selective-coding equivalent of the current workflow: the
    researcher's selected JTBD, its core phenomenon and grounding categories.
    Read as recorded; nothing is regenerated or reworded."""
    path = run_path / "narrative_job_selection.yaml"
    selection = _read_yaml(path, "narrative_job_selection")
    if not isinstance(selection, dict):
        raise DeliveryError("required_artifact_invalid", "narrative_job_selection", "expected mapping")
    grounded = [ref for ref in selection.get("grounded_in") or [] if isinstance(ref, str)]
    unknown = [ref for ref in grounded if ref not in category_ids]
    if unknown:
        raise DeliveryError("required_artifact_invalid", "narrative_job_selection", f"unknown categories {unknown}")
    synthesis = {
        "schema_version": 1,
        "case_id": selection.get("case_id"),
        "core_category_id": None,
        "grounded_in": grounded,
        "selected_job": {key: selection.get(key) for key in ("situation", "progress", "motivation", "outcome")},
        "narrative": selection.get("narrative"),
        "narrative_composed_by": selection.get("narrative_composed_by"),
        "rationale": selection.get("rationale"),
        "key_insights": [],
        "source": "narrative_job_selection",
    }
    reference = {
        "artifact_type": "narrative_job_selection",
        "source_ref": "narrative_job_selection.yaml",
        "status": "ready",
        "version_ref": None,
        "content_digest": _digest(selection, "narrative_job_selection"),
        "versioning": "run_scoped_current_file",
    }
    return synthesis, reference


def build_coding_synthesis_view(run_dir: str | Path) -> DeliveryViewModel:
    """Build a normalized coding/evidence delivery view from one run directory.

    Required coding files are resolved only from this run. Evidence uses the
    repository's shared ``evidence_locator`` contract (run-local, recorded
    pointer, or unique case source); ambiguous and missing results fail closed.
    If a run declares an evidence lifecycle version, its exact current version
    and content digest are verified before a view is returned.
    """
    run_path = Path(run_dir).resolve()
    if not run_path.is_dir():
        raise DeliveryError("run_directory_missing", "run", run_path.name)

    run_json_path = run_path / "run.json"
    run_state: dict[str, Any] = {}
    if run_json_path.exists():
        loaded_state = _read_json(run_json_path, "run.json")
        if not isinstance(loaded_state, dict):
            raise DeliveryError("run_metadata_invalid", "run.json", "expected object")
        run_state = loaded_state

    evidence, evidence_resolution = evidence_locator.locate_evidence(run_path)
    if evidence is None:
        raise DeliveryError("required_artifact_missing", "evidence", evidence_resolution)
    if (run_path / "evidence.yaml").is_file() and evidence_resolution != "run_local":
        raise DeliveryError("run_local_evidence_unreadable", "evidence", evidence_resolution)
    if evidence_resolution == "case_unique" and _has_recorded_evidence_pointer(run_state):
        raise DeliveryError("recorded_evidence_source_unavailable", "evidence", evidence_resolution)
    if not isinstance(evidence, list):
        raise DeliveryError("required_artifact_invalid", "evidence", "expected array")
    _require_valid(_schema_errors(evidence, "evidence"), "evidence")
    _unique_ids(evidence, "id", "evidence")
    evidence_ids = {record["id"] for record in evidence if isinstance(record, dict)}

    open_coding, open_ref = _coding_artifact(
        run_path, "open_coding.yaml", "open_coding", "open-coding", run_state, evidence_ids
    )
    axial_coding, axial_ref = _coding_artifact(
        run_path, "axial_coding.yaml", "axial_coding", "axial-coding", run_state, evidence_ids
    )
    category_ids = {
        category["id"]
        for category in axial_coding.get("categories", [])
        if isinstance(category, dict) and isinstance(category.get("id"), str)
    }
    # The current workflow has no selective-coding gate (2026-10-01): the
    # researcher's selected JTBD (narrative_job_selection.yaml) is the
    # synthesis decision. Older runs keep reading selective_coding.yaml.
    if (run_path / "selective_coding.yaml").is_file() or not (run_path / "narrative_job_selection.yaml").is_file():
        selective_coding, selective_ref = _coding_artifact(
            run_path,
            "selective_coding.yaml",
            "selective_coding",
            "selective-coding",
            run_state,
            evidence_ids,
            category_ids,
        )
        synthesis_source = "selective_coding.yaml"
    else:
        selective_coding, selective_ref = _selected_job_synthesis(run_path, category_ids)
        synthesis_source = "narrative_job_selection.yaml"

    artifact_case_ids = {
        data.get("case_id")
        for data in (open_coding, axial_coding, selective_coding)
        if isinstance(data.get("case_id"), str) and data.get("case_id")
    }
    if len(artifact_case_ids) != 1:
        raise DeliveryError("coding_case_identity_mismatch", "coding_artifacts")
    coding_case_id = next(iter(artifact_case_ids))

    case_path = run_path / "case.yaml"
    case_data: dict[str, Any] = {}
    if case_path.exists():
        loaded_case = _read_yaml(case_path, "case")
        if not isinstance(loaded_case, dict):
            raise DeliveryError("case_identity_invalid", "case.yaml", "expected mapping")
        case_data = loaded_case
    declared_case_ids = {
        value for value in (
            run_state.get("case_id"),
            case_data.get("case_id"),
            coding_case_id,
        ) if isinstance(value, str) and value
    }
    if len(declared_case_ids) != 1:
        raise DeliveryError("coding_case_identity_mismatch", "run_identity")
    case_id = next(iter(declared_case_ids))

    evidence_version_id = _evidence_version(run_path, run_state, case_id, evidence)
    evidence_ref = {
        "artifact_type": "evidence",
        "source_ref": _evidence_source_ref(evidence_resolution, run_state, case_id),
        "resolution_method": evidence_resolution,
        "status": "ready",
        "version_ref": evidence_version_id,
        "content_digest": _digest(evidence, "evidence"),
        "versioning": "lifecycle_current" if evidence_version_id else "content_digest_only",
    }

    optional_context: dict[str, Any] = {}
    project_id = case_data.get("project_id") or run_state.get("project_id")
    project_id_available = isinstance(project_id, str) and bool(project_id.strip())
    availability: dict[str, Any] = {
        "evidence": {"status": "ready", "reason": None},
        "open_coding": {"status": "ready", "reason": None},
        "axial_coding": {"status": "ready", "reason": None},
        "selective_coding": {"status": "ready", "reason": None},
        "project_identity": {
            "status": "ready" if project_id_available else "unavailable",
            "reason": None if project_id_available else "project_id_not_recorded",
        },
    }
    optional_refs: list[dict[str, Any]] = []
    notices: list[dict[str, str]] = []
    for name, (filename, schema_name) in OPTIONAL_CODING_CONTEXT.items():
        state, data, reference = _optional_source(run_path, name, filename, schema_name)
        availability[name] = state
        optional_refs.append(reference)
        if data is not None:
            if isinstance(data, dict) and data.get("case_id") not in (None, case_id):
                availability[name] = {"status": "unsupported", "reason": "optional_source_case_mismatch"}
                reference["status"] = "unsupported"
                data = None
            else:
                optional_context[name] = data
        if availability[name]["status"] in {"unavailable", "unsupported"}:
            notices.append({"code": availability[name]["reason"], "artifact": name})

    open_concepts = open_coding.get("concepts") or []
    axial_categories = axial_coding.get("categories") or []
    open_by_evidence: dict[str, list[str]] = defaultdict(list)
    category_by_evidence: dict[str, list[str]] = defaultdict(list)
    concept_refs = {concept["id"]: set(concept.get("evidence_refs") or []) for concept in open_concepts}
    category_refs = {category["id"]: set(category.get("evidence_refs") or []) for category in axial_categories}
    for concept in open_concepts:
        for evidence_id in concept.get("evidence_refs") or []:
            open_by_evidence[evidence_id].append(concept["id"])
    for category in axial_categories:
        for evidence_id in category.get("evidence_refs") or []:
            category_by_evidence[evidence_id].append(category["id"])

    core_category_id = selective_coding.get("core_category_id")
    grounded_category_ids = set(selective_coding.get("grounded_in") or ([core_category_id] if core_category_id else []))
    insights = selective_coding.get("key_insights") or []
    insight_by_evidence: dict[str, list[str]] = defaultdict(list)
    for index, insight in enumerate(insights):
        for evidence_id in insight.get("evidence_refs") or []:
            insight_by_evidence[evidence_id].append(f"key_insights[{index}]")

    evidence_mapping = []
    for record in evidence:
        evidence_id = record["id"]
        core_refs = [
            category_id for category_id in category_by_evidence.get(evidence_id, [])
            if category_id in grounded_category_ids
        ]
        evidence_mapping.append({
            "evidence_id": evidence_id,
            "open_code_refs": list(open_by_evidence.get(evidence_id, [])),
            "axial_category_refs": list(category_by_evidence.get(evidence_id, [])),
            "selective_core_category_refs": core_refs,
            "selective_insight_refs": list(insight_by_evidence.get(evidence_id, [])),
        })

    open_to_axial = []
    for concept in open_concepts:
        concept_id = concept["id"]
        for category in axial_categories:
            category_id = category["id"]
            shared_refs = sorted(concept_refs[concept_id] & category_refs[category_id])
            if shared_refs:
                open_to_axial.append({
                    "open_code_ref": concept_id,
                    "axial_category_ref": category_id,
                    "relationship": "shared_evidence",
                    "evidence_refs": shared_refs,
                })

    source_snapshot = [
        evidence_ref,
        open_ref,
        axial_ref,
        selective_ref,
        *optional_refs,
    ]
    payload = {
        "view_model_schema_version": DELIVERY_VIEW_SCHEMA_VERSION,
        "delivery_type": CODING_SYNTHESIS,
        "export_id": CODING_SYNTHESIS,
        "status": "ready",
        "metadata": {
            "run_ref": run_path.name,
            "project_id": project_id if project_id_available else None,
            "case_id": case_id,
            "coding_states": {
                key: run_state.get(f"{key}_state")
                for key in ("open_coding", "axial_coding", "selective_coding")
                if run_state.get(f"{key}_state") is not None
            },
        },
        "source_snapshot": source_snapshot,
        "evidence": copy.deepcopy(evidence),
        "open_coding": open_coding,
        "axial_coding": axial_coding,
        "selective_coding": selective_coding,
        "optional_context": optional_context,
        "evidence_mapping": evidence_mapping,
        "provenance": {
            "evidence_sources": [
                {
                    "evidence_id": record["id"],
                    "source": record.get("source"),
                    "locator": record.get("locator"),
                    "role": record.get("role"),
                    "tier": record.get("tier"),
                    "content_type": record.get("content_type"),
                }
                for record in evidence
            ],
            "open_code_to_axial_category": open_to_axial,
            "selective_core_decision": {
                "source_ref": synthesis_source,
                "core_category_ref": core_category_id,
                "grounded_category_refs": sorted(grounded_category_ids),
                "rationale": selective_coding.get("rationale"),
                "decision_state": run_state.get("selective_coding_state", "recorded_in_source_artifact"),
            },
            "selective_narrative_composed_by": selective_coding.get("narrative_composed_by"),
        },
        "availability": availability,
        "notices": notices,
    }
    return DeliveryViewModel.from_payload(payload)
