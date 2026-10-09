"""Read-only Evidence & Decision Traceability delivery projection.

This adapter composes the already validated Local V1 delivery views into the
machine-oriented ``evidence_appendix`` view.  It does not resolve a second
source of truth: each constituent adapter remains responsible for resolving
its current canonical artifacts and validating their references.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

import yaml

from .delivery import (
    DeliveryError,
    DeliveryViewModel,
    _digest,
    _read_json,
    _read_yaml,
    _resolve_registry_path,
    build_coding_synthesis_view,
)
from .delivery_as_is_blueprint import build_as_is_blueprint_view
from .delivery_customer_journey import build_customer_journey_view
from .delivery_jtbd_persona import build_jtbd_persona_view
from .delivery_to_be_report import build_to_be_report_view
from . import research_catalog_review


EVIDENCE_APPENDIX = "evidence_appendix"
_DELIVERY_BUILDERS = (
    ("coding_synthesis", build_coding_synthesis_view),
    ("jtbd_persona", build_jtbd_persona_view),
    ("customer_journey", build_customer_journey_view),
    ("as_is_blueprint", build_as_is_blueprint_view),
    ("to_be_report", build_to_be_report_view),
)


def _records(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value or [] if isinstance(item, Mapping)]


def _context(value: Mapping[str, Any], path: str) -> dict[str, Any]:
    """Keep only recorded context; this helper never creates a claim."""
    keys = (
        "id", "node_id", "step_id", "proposal_ref", "issue_ref", "category_id",
        "open_code_ref", "axial_category_ref", "field", "label", "kind", "layer",
        "branch_id", "divergence_step_id", "merge_step_id", "topology_provenance",
        "provenance", "claim_state", "status", "source_ref", "version_ref", "relationship",
    )
    result = {key: value[key] for key in keys if key in value}
    result["path"] = path
    return result


def _explicit_evidence_rows(
    value: Any,
    *,
    delivery_type: str,
    path: str = "",
    parent: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Extract only explicit ``evidence_ref(s)`` keys from a delivery view.

    The output records the source path and parent fields instead of assigning a
    new relationship.  This is deliberately conservative: IDs such as
    research claims and external source IDs are not treated as user evidence.
    """
    rows: list[dict[str, Any]] = []
    current_parent = value if isinstance(value, Mapping) else (parent if isinstance(parent, Mapping) else {})
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if key == "evidence_ref" and isinstance(child, str) and child:
                rows.append({
                    "record_type": "explicit_evidence_ref",
                    "delivery_type": delivery_type,
                    "finding_ref": current_parent.get("id") or current_parent.get("node_id") or child_path.rsplit(".", 1)[0],
                    "finding_path": child_path,
                    "evidence_id": child,
                    "evidence_refs": [child],
                    "provenance": current_parent.get("provenance"),
                    "claim_state": current_parent.get("claim_state"),
                    "status": current_parent.get("status"),
                    "source_ref": current_parent.get("source_ref"),
                    "version_ref": current_parent.get("version_ref"),
                    "details": _context(current_parent, path),
                })
                continue
            if key == "evidence_refs" and isinstance(child, list):
                refs = [item for item in child if isinstance(item, str) and item]
                for evidence_id in refs:
                    rows.append({
                        "record_type": "explicit_evidence_ref",
                        "delivery_type": delivery_type,
                        "finding_ref": current_parent.get("id") or current_parent.get("node_id") or path.rsplit(".", 1)[-1],
                        "finding_path": child_path,
                        "evidence_id": evidence_id,
                        "evidence_refs": refs,
                        "provenance": current_parent.get("provenance"),
                        "claim_state": current_parent.get("claim_state"),
                        "status": current_parent.get("status"),
                        "source_ref": current_parent.get("source_ref"),
                        "version_ref": current_parent.get("version_ref"),
                        "details": _context(current_parent, path),
                    })
                continue
            if isinstance(child, Mapping):
                rows.extend(_explicit_evidence_rows(
                    child,
                    delivery_type=delivery_type,
                    path=child_path,
                    parent=child,
                ))
            elif isinstance(child, list):
                rows.extend(_explicit_evidence_rows(
                    child,
                    delivery_type=delivery_type,
                    path=child_path,
                    parent=current_parent,
                ))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]"
            if isinstance(child, (Mapping, list)):
                rows.extend(_explicit_evidence_rows(
                    child,
                    delivery_type=delivery_type,
                    path=child_path,
                    parent=parent,
                ))
    return rows


def _view_evidence_rows(delivery_type: str, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    roots: dict[str, tuple[str, ...]] = {
        "coding_synthesis": ("open_coding", "axial_coding", "selective_coding", "optional_context"),
        "jtbd_persona": ("jtbd", "segmentation", "persona_content"),
        # ``branches`` is present only for v3 views, so adding the optional
        # root leaves v2 traversal/output unchanged while preserving explicit
        # Branch step evidence when it exists.
        "customer_journey": ("scenarios", "branches", "conflicts"),
        "as_is_blueprint": ("journey_scope", "layers", "nodes", "relations", "issues_gaps", "scenarios", "branches", "conflicts"),
        "to_be_report": ("issue_coverage", "proposals", "blueprint_changes", "revision_context", "direction"),
    }
    rows: list[dict[str, Any]] = []
    for key in roots.get(delivery_type, ()):
        rows.extend(_explicit_evidence_rows(payload.get(key), delivery_type=delivery_type, path=key))
    if delivery_type == "coding_synthesis":
        for link in _records((payload.get("provenance") or {}).get("open_code_to_axial_category")):
            rows.extend(_explicit_evidence_rows(
                link,
                delivery_type=delivery_type,
                path="provenance.open_code_to_axial_category",
            ))
    return rows


def _finding_rows(views: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    coding = views["coding_synthesis"]
    for mapping in _records(coding.get("evidence_mapping")):
        evidence_id = mapping.get("evidence_id")
        rows.append({
            "record_type": "coding_evidence_mapping",
            "delivery_type": "coding_synthesis",
            "finding_ref": evidence_id,
            "finding_path": "evidence_mapping",
            "evidence_id": evidence_id,
            "evidence_refs": [evidence_id] if isinstance(evidence_id, str) else [],
            "provenance": "canonical_mapping",
            "claim_state": None,
            "status": "ready",
            "source_ref": "evidence_mapping",
            "version_ref": None,
            "details": mapping,
        })
    for delivery_type, payload in views.items():
        rows.extend(_view_evidence_rows(delivery_type, payload))
    return rows


def _decision_row(
    decision_type: str,
    decision_ref: str | None,
    decision: str | None,
    *,
    state: str | None = None,
    rationale: str | None = None,
    source_ref: str | None = None,
    version_ref: str | None = None,
    provenance: str | None = None,
    claim_state: str | None = None,
    details: Any = None,
) -> dict[str, Any]:
    return {
        "decision_type": decision_type,
        "decision_ref": decision_ref,
        "decision": decision,
        "state": state,
        "rationale": rationale,
        "source_ref": source_ref,
        "version_ref": version_ref,
        "provenance": provenance,
        "claim_state": claim_state,
        "details": details if details is not None else {},
    }


def _decision_rows(views: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    coding = views["coding_synthesis"]
    selective = (coding.get("provenance") or {}).get("selective_core_decision") or {}
    if selective:
        rows.append(_decision_row(
            "selective_coding",
            selective.get("core_category_ref"),
            "core_category_selected",
            state=selective.get("decision_state"),
            rationale=selective.get("rationale"),
            source_ref=selective.get("source_ref"),
            provenance="researcher_confirmed_or_recorded",
            details=selective,
        ))
    optional = coding.get("optional_context") or {}
    hmw_selection = optional.get("primary_hmw_selection")
    if isinstance(hmw_selection, Mapping):
        rows.append(_decision_row(
            "primary_hmw_selection",
            hmw_selection.get("hmw_id"),
            "primary_selected",
            state="recorded",
            rationale=hmw_selection.get("rationale"),
            source_ref="primary_hmw_selection.yaml",
            provenance="researcher_recorded",
            details=hmw_selection,
        ))

    persona = views["jtbd_persona"]
    segmentation = persona.get("segmentation") or {}
    target = segmentation.get("target_confirmation") or {}
    if segmentation.get("confirmed_axis_ids"):
        rows.append(_decision_row(
            "persona_segmentation_axes",
            ",".join(segmentation.get("confirmed_axis_ids") or []),
            "axes_confirmed",
            state="confirmed",
            source_ref="segmentation_axis_selection.yaml",
            provenance="recorded_confirmation",
            details={"axis_ids": segmentation.get("confirmed_axis_ids")},
        ))
    if target:
        rows.append(_decision_row(
            "persona_target_selection",
            segmentation.get("target_quadrant_id"),
            "target_confirmed",
            state=target.get("state"),
            rationale=segmentation.get("target_rationale"),
            source_ref=target.get("source_ref"),
            provenance=target.get("origin"),
            details={
                "target_quadrant_id": segmentation.get("target_quadrant_id"),
                "target_member_subject_ids": segmentation.get("target_member_subject_ids"),
                "target_confirmation": target,
            },
        ))
    pmeta = persona.get("metadata") or {}
    for key, state in sorted((pmeta.get("run_states") or {}).items()):
        rows.append(_decision_row(
            "lifecycle_state", key, None, state=state, source_ref="run.json", provenance="run_record",
        ))

    journey = views["customer_journey"]
    jmeta = journey.get("metadata") or {}
    rows.append(_decision_row(
        "journey_confirmation", jmeta.get("primary_scenario"), "confirmed",
        state=jmeta.get("journey_state"), source_ref="journey.yaml", provenance="researcher_confirmed",
        details={"scenario_count": jmeta.get("scenario_count"), "confirmed_step_counts": jmeta.get("confirmed_step_counts")},
    ))

    as_is = views["as_is_blueprint"]
    ameta = as_is.get("metadata") or {}
    rows.append(_decision_row(
        "as_is_blueprint", ameta.get("graph_version_ref"), "confirmed" if ameta.get("as_is_confirmed") else "recorded",
        state=ameta.get("review_state"), source_ref="service_graph.yaml", version_ref=ameta.get("graph_version_ref"),
        provenance="researcher_confirmed" if ameta.get("as_is_confirmed") else None,
    ))
    goal = as_is.get("operative_goal") or {}
    if goal:
        rows.append(_decision_row(
            "operative_improvement_goal", "improvement_goal_review.yaml", "operative",
            state=goal.get("status"), rationale=goal.get("text"), source_ref=goal.get("source_ref"),
            provenance=goal.get("provenance"),
        ))

    to_be = views["to_be_report"]
    tmeta = to_be.get("metadata") or {}
    rows.append(_decision_row(
        "to_be_review", tmeta.get("to_be_version_ref"), tmeta.get("to_be_review_state"),
        state=tmeta.get("to_be_review_state"), source_ref=".to_be_reviews", version_ref=tmeta.get("to_be_version_ref"),
        provenance="researcher_review", claim_state=tmeta.get("proposal_state"),
        details={"to_be_fingerprint": tmeta.get("to_be_fingerprint")},
    ))
    for item in _records((to_be.get("revision_context") or {}).get("decisions")):
        rows.append(_decision_row(
            "to_be_revision", item.get("proposal_ref"), item.get("decision"),
            rationale=item.get("instruction"), source_ref="to_be_bundle", provenance="researcher_decision", details=item,
        ))

    external = to_be.get("external_research") or {}
    support = {"select": "to_be_support", "hold": "condition_or_question_only", "reject": "excluded_from_to_be_support"}
    for item in _records(external.get("decisions")):
        decision = item.get("decision")
        rows.append(_decision_row(
            "external_research_claim", item.get("claim_ref"), decision.upper() if isinstance(decision, str) else decision,
            state="researcher_confirmed", rationale=item.get("researcher_rationale"), source_ref=research_catalog_review.REVIEW_FILENAME,
            provenance="researcher", claim_state=support.get(decision), details=item,
        ))
    return rows


def _artifact_rows(views: Mapping[str, Mapping[str, Any]], run_state: Mapping[str, Any], registry: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for delivery_type, payload in views.items():
        for source in _records(payload.get("source_snapshot")):
            row = dict(source)
            row.update({"record_type": "source_snapshot", "delivery_type": delivery_type, "details": source})
            rows.append(row)
        for key, raw in sorted((payload.get("availability") or {}).items()):
            state = raw if isinstance(raw, Mapping) else {}
            rows.append({
                "record_type": "availability", "delivery_type": delivery_type, "artifact_type": key,
                "source_ref": None, "status": state.get("status"), "version_ref": None,
                "content_digest": None, "versioning": None, "reason": state.get("reason"),
                "key": key, "details": state,
            })
        for notice in _records(payload.get("notices")):
            rows.append({
                "record_type": "notice", "delivery_type": delivery_type, "artifact_type": notice.get("artifact"),
                "source_ref": None, "status": "ready", "version_ref": None, "content_digest": None,
                "versioning": None, "reason": notice.get("code"), "detail": notice.get("detail"), "details": notice,
            })
    registry_ref = run_state.get("artifact_registry")
    if registry_ref:
        rows.append({
            "record_type": "registry_pointer", "delivery_type": "evidence_appendix", "artifact_type": "artifact_registry",
            "source_ref": "run.json:artifact_registry", "status": "ready", "version_ref": None,
            "content_digest": _digest(registry, "artifact_registry"), "versioning": "lifecycle_current",
            "resolved_path": registry_ref, "details": {"pointer": registry_ref},
        })
    return rows


def _external_rows(to_be: Mapping[str, Any]) -> list[dict[str, Any]]:
    external = to_be.get("external_research") or {}
    decisions = {item.get("claim_ref"): item for item in _records(external.get("decisions"))}
    eligibility = {"select": "to_be_support", "hold": "condition_or_question_only", "reject": "excluded_from_to_be_support"}
    rows: list[dict[str, Any]] = []
    for item in _records(external.get("decisions")):
        decision = item.get("decision")
        rows.append({
            "record_type": "claim_decision", "ref": item.get("claim_ref"), "decision": decision.upper() if isinstance(decision, str) else decision,
            "support_eligibility": eligibility.get(decision), "text": None,
            "researcher_rationale": item.get("researcher_rationale"), "source_ref": research_catalog_review.REVIEW_FILENAME,
            "external_not_user_evidence": True, "status": "researcher_confirmed", "derived_from": None, "details": item,
        })
    for claim in _records(external.get("selected_claims")):
        decision = decisions.get(claim.get("id"), {}).get("decision", "select")
        rows.append({
            "record_type": "selected_claim", "ref": claim.get("id"), "decision": decision.upper(),
            "support_eligibility": eligibility.get(decision), "text": claim.get("text"),
            "researcher_rationale": decisions.get(claim.get("id"), {}).get("researcher_rationale"),
            "source_ref": "research_catalog", "external_not_user_evidence": True,
            "status": claim.get("claim_state"), "derived_from": claim.get("source_links"), "details": claim,
        })
    for source in _records(external.get("selected_sources")):
        rows.append({
            "record_type": "external_source", "ref": source.get("id"), "decision": "SELECT",
            "support_eligibility": "external_reference_only", "text": source.get("title"),
            "researcher_rationale": source.get("admission_reason"), "source_ref": source.get("url"),
            "external_not_user_evidence": True, "status": source.get("admission"), "derived_from": None, "details": source,
        })
    for mechanism in _records(external.get("selected_mechanisms")):
        rows.append({
            "record_type": "selected_mechanism", "ref": mechanism.get("id"), "decision": "SELECT",
            "support_eligibility": "to_be_support_mechanism", "text": mechanism.get("description"),
            "researcher_rationale": None, "source_ref": research_catalog_review.REVIEW_FILENAME,
            "external_not_user_evidence": True, "status": "researcher_confirmed",
            "derived_from": mechanism.get("derived_from_claim_refs"), "details": mechanism,
        })
    for key, eligibility_value in (("hold_conditions", "condition_or_question_only"), ("hold_questions", "condition_or_question_only")):
        for item in _records(external.get(key)):
            rows.append({
                "record_type": "hold_condition" if key == "hold_conditions" else "hold_question",
                "ref": item.get("id"), "decision": "HOLD", "support_eligibility": eligibility_value,
                "text": item.get("text"), "researcher_rationale": None, "source_ref": research_catalog_review.REVIEW_FILENAME,
                "external_not_user_evidence": True, "status": item.get("status", "hold_only"),
                "derived_from": item.get("derived_from_claim_refs"), "details": item,
            })
    rows.append({
        "record_type": "transfer_rule", "ref": "external_research_transfer", "decision": None,
        "support_eligibility": "explicit", "text": external.get("transfer_rule"), "researcher_rationale": None,
        "source_ref": research_catalog_review.REVIEW_FILENAME, "external_not_user_evidence": True,
        "status": external.get("status"), "derived_from": None, "details": {"transfer_rule": external.get("transfer_rule")},
    })
    return rows


def build_evidence_appendix_view(run_dir: str | Path) -> DeliveryViewModel:
    """Build the current five-sheet traceability projection without writes."""
    run_path = Path(run_dir).resolve()
    if not run_path.is_dir():
        raise DeliveryError("run_directory_missing", "run", run_path.name)
    run_state = _read_json(run_path / "run.json", "run")
    if not isinstance(run_state, Mapping):
        raise DeliveryError("required_metadata_invalid", "run", "expected object")

    built: dict[str, dict[str, Any]] = {}
    for delivery_type, builder in _DELIVERY_BUILDERS:
        view = builder(run_path).to_dict()
        built[delivery_type] = view
    case_ids = {((payload.get("metadata") or {}).get("case_id")) for payload in built.values()}
    case_ids.discard(None)
    if len(case_ids) != 1 or run_state.get("case_id") not in case_ids:
        raise DeliveryError("delivery_case_identity_mismatch", "delivery_chain", ",".join(sorted(map(str, case_ids))))

    registry_value = run_state.get("artifact_registry")
    registry = None
    if registry_value:
        registry_path = _resolve_registry_path(run_path, registry_value)
        registry = _read_yaml(registry_path, "artifact_registry")

    evidence = copy.deepcopy(_records(built["coding_synthesis"].get("evidence")))
    metadata = {
        "case_id": next(iter(case_ids), run_state.get("case_id")),
        "project_id": (built["coding_synthesis"].get("metadata") or {}).get("project_id"),
        "run_ref": run_path.name,
        "delivery_sources": list(built),
        "evidence_count": len(evidence),
        "artifact_registry_ref": registry_value,
    }
    snapshots = []
    for payload in built.values():
        snapshots.extend(copy.deepcopy(_records(payload.get("source_snapshot"))))
    if registry_value and registry is not None:
        snapshots.append({
            "artifact_type": "artifact_registry", "source_ref": "run.json:artifact_registry", "status": "ready",
            "version_ref": None, "content_digest": _digest(registry, "artifact_registry"),
            "versioning": "lifecycle_current", "resolved_path": registry_value,
        })
    payload = {
        "view_model_schema_version": 1,
        "delivery_type": EVIDENCE_APPENDIX,
        "export_id": EVIDENCE_APPENDIX,
        "status": "ready",
        "metadata": metadata,
        "evidence_index": evidence,
        "finding_to_evidence": _finding_rows(built),
        "decision_log": _decision_rows(built),
        "artifact_traceability": _artifact_rows(built, run_state, registry),
        "external_research": _external_rows(built["to_be_report"]),
        "source_snapshot": snapshots,
        "provenance": {"per_delivery": {key: copy.deepcopy(value.get("provenance") or {}) for key, value in built.items()}},
        "availability": {
            key: {"status": value.get("status"), "reason": None}
            for key, value in built.items()
        },
        "notices": [
            {"code": "read_only_projection", "detail": "Evidence & Decision Traceability is a delivery projection; canonical artifacts remain the source of truth."},
            {"code": "external_research_boundary", "detail": "External sources are not MUJI user evidence; SELECT/HOLD/REJECT transfer limits are preserved."},
        ],
    }
    return DeliveryViewModel.from_payload(payload)
