"""Read-only ``SBP Evaluation Report`` delivery adapter (new workflow only).

New workflow: CJM + researcher-selected Solutions -> Service Blueprint ->
SBP Evaluation.  This report is the research evaluation record of whether the
confirmed SBP can actually work.  It is not a To-be proposal: it reads the
confirmed SBP, the evaluation questions that research was asked, the external
research catalog that research produced, and the researcher's item-level
review -- and nothing else.  It never reads or synthesizes an Improvement
Goal, never proposes a design, and never decides whether the SBP is acceptable.

Legacy runs keep ``to_be_report`` (To-be Service Report); this adapter refuses
them rather than reinterpreting their artifacts.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Mapping

import yaml

import validate_sbp as V

from . import as_is_research, lifecycle, research_catalog_review, stages
from .delivery import DeliveryError, DeliveryViewModel, _digest
from .delivery_as_is_blueprint import _node_view

SBP_EVALUATION_REPORT = "sbp_evaluation_report"
SBP_EVALUATION_REPORT_TITLE = "SBP Evaluation Report"

# Service layers named in the report; physical evidence is the physical /
# digital elements lane of the canonical graph.
_MODEL_LAYERS = ("顧客行動", "前台行動", "後台行動", "支援流程", "實體證據")


def _read_yaml(path: Path, artifact: str) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise DeliveryError("required_artifact_unreadable", artifact, type(exc).__name__) from exc


def _current_research(run_path: Path, baseline: as_is_research.Baseline) -> tuple[dict[str, Any], dict[str, Any]]:
    """The research operation and its catalog, only while both are current."""
    operation = as_is_research.load_current_operation(run_path)
    if operation is None:
        raise DeliveryError("research_not_started", "research_operation")
    if operation.get("state") != "done" or operation.get("phase") != "research_ready":
        raise DeliveryError("research_not_ready", "research_operation", str(operation.get("phase")))
    expected = as_is_research.request_digest(baseline, model_policy=as_is_research.DEFAULT_MODEL_POLICY)
    if operation.get("request_digest") != expected:
        raise DeliveryError("research_not_current", "research_operation", "SBP changed after research")
    result_ids = set((operation.get("result") or {}).values())
    registry = _read_yaml(baseline.registry_path, "artifact_registry")
    states = lifecycle.resolve_lifecycle(registry)
    if not result_ids or any(states.get(version_id) != "current" for version_id in result_ids):
        raise DeliveryError("research_not_current", "research_catalog")
    stage_dir = as_is_research.operation_path(run_path, str(operation["request_digest"])).parent / "staged"
    catalog = _read_yaml(stage_dir / "research_catalog.yaml", "research_catalog")
    if not isinstance(catalog, dict):
        raise DeliveryError("required_artifact_invalid", "research_catalog")
    if lifecycle.canonical_content_digest(catalog) != (operation.get("staged_digests") or {}).get("research_catalog"):
        raise DeliveryError("research_catalog_integrity_mismatch", "research_catalog")
    return operation, catalog


def _review(run_path: Path, catalog: Mapping[str, Any], operation: Mapping[str, Any],
            baseline: as_is_research.Baseline) -> tuple[str, dict[str, Any] | None]:
    """The researcher's item-level review, used only when it covers this catalog."""
    try:
        review = research_catalog_review.load(run_path)
    except research_catalog_review.ReviewError as exc:
        return ("not_reviewed" if exc.code == research_catalog_review.MISSING else "invalid"), None
    if V.structural_errors(review, V.load_schema("research-catalog-review")):
        return "invalid", None
    if research_catalog_review.binding_errors(
        review, dict(catalog),
        catalog_request_digest=str(operation.get("request_digest") or ""),
        case_id=baseline.case_id, scenario_id=baseline.scenario_id,
    ):
        return "stale", None
    if research_catalog_review.semantic_errors(review, dict(catalog)):
        return "invalid", None
    return "current", review


def build_sbp_evaluation_report_view(run_dir: str | Path) -> DeliveryViewModel:
    run_path = Path(run_dir).resolve()
    if not run_path.is_dir():
        raise DeliveryError("run_directory_missing", "run", run_path.name)
    journey_path = run_path / "journey.yaml"
    if not journey_path.is_file():
        raise DeliveryError("required_artifact_missing", "confirmed_journey", "journey.yaml")
    journey = _read_yaml(journey_path, "confirmed_journey")
    if not stages.journey_uses_solution_selection(journey):
        raise DeliveryError("legacy_workflow_uses_to_be_report", "confirmed_journey")
    try:
        baseline = as_is_research.resolve_baseline(run_path, require_reviewed=True)
    except as_is_research.ResearchOperationError as exc:
        raise DeliveryError("sbp_not_confirmed", "service_graph", exc.code) from exc

    operation, catalog = _current_research(run_path, baseline)
    review_state, review = _review(run_path, catalog, operation, baseline)

    evidence_ids = {item.get("id") for item in baseline.evidence if isinstance(item, Mapping)}
    nodes = [_node_view(node, evidence_ids) for node in baseline.graph.get("nodes") or [] if isinstance(node, Mapping)]
    handoff_steps = [step for step in stages.journey_sbp_handoff(journey).get("steps") or [] if isinstance(step, Mapping)]
    solution_text = {
        str(solution.get("id")): solution.get("text")
        for step in handoff_steps for solution in step.get("selected_solutions") or []
    }
    for node in nodes:
        if node.get("selected_solution_ref"):
            node["selected_solution_text"] = solution_text.get(str(node["selected_solution_ref"]))

    questions = as_is_research.sbp_evaluation_questions(run_path, baseline.graph)
    node_layer = {str(node.get("node_id")): node.get("layer") for node in nodes}
    for question in questions:
        if question.get("kind") == "service_assumption":
            question["layer"] = node_layer.get(str(question.get("node_id")))

    decisions = {
        str(entry.get("claim_ref")): {"decision": entry.get("decision"), "researcher_rationale": entry.get("researcher_rationale")}
        for entry in (review or {}).get("claim_decisions") or [] if isinstance(entry, Mapping)
    }
    sources = [copy.deepcopy(item) for item in catalog.get("sources") or [] if isinstance(item, Mapping)]
    claims = []
    for claim in catalog.get("claims") or []:
        if not isinstance(claim, Mapping):
            continue
        record = copy.deepcopy(dict(claim))
        record["researcher_review"] = decisions.get(str(claim.get("id")))
        claims.append(record)
    queries = [copy.deepcopy(item) for item in catalog.get("query_audit") or [] if isinstance(item, Mapping)]

    directives = {"selected_mechanisms": [], "design_conditions": [], "design_questions": []}
    if review is not None:
        directives = research_catalog_review._directives(review)

    unreviewed = [claim["id"] for claim in claims if claim.get("researcher_review") is None]
    held = [claim["id"] for claim in claims if (claim.get("researcher_review") or {}).get("decision") == "hold"]
    uncertain = [claim["id"] for claim in claims if (claim.get("uncertainty") or {}).get("state") in {"uncertain", "disputed"}]
    empty_queries = [query for query in queries if not query.get("returned_source_refs")]
    inferred_assumptions = [
        node for node in nodes
        if node.get("layer") in {"後台行動", "支援流程"} and node.get("provenance") in {"inferred", "mixed"}
    ]

    payload = {
        "view_model_schema_version": 1,
        "delivery_type": SBP_EVALUATION_REPORT,
        "export_id": SBP_EVALUATION_REPORT,
        "status": "ready",
        "metadata": {
            "title": SBP_EVALUATION_REPORT_TITLE,
            "case_id": baseline.case_id,
            "scenario_id": baseline.scenario_id,
            "run_ref": run_path.name,
            "sbp_version_ref": baseline.as_is_version_id,
            "research_request_digest": operation.get("request_digest"),
            "research_review_state": review_state,
        },
        "scope": {
            "steps": [copy.deepcopy(dict(step)) for step in handoff_steps],
        },
        "service_model": {
            "layers": {layer: [node for node in nodes if node.get("layer") == layer] for layer in _MODEL_LAYERS},
        },
        "evaluation_questions": questions,
        "findings": claims,
        "research_evidence": {"sources": sources, "queries": queries},
        "researcher_review": {
            "state": review_state,
            "directives": directives,
        },
        "unresolved": {
            "unreviewed_claim_refs": unreviewed,
            "held_claim_refs": held,
            "uncertain_claim_refs": uncertain,
            "queries_without_admitted_sources": empty_queries,
            "inferred_service_assumptions": inferred_assumptions,
            "hold_conditions": directives["design_conditions"],
            "hold_questions": directives["design_questions"],
        },
        "provenance_rule": {
            "participant_evidence": "evidence refs on the confirmed SBP and CJM",
            "synthesis": "CJM Solution candidates and their Channels (AI-drafted, researcher-selected)",
            "service_system_inference": "SBP nodes and relations whose claims are inference",
            "external_research": "research_catalog sources and claims; design-support evidence, not participant evidence",
            "researcher_decision": f"Solution selection in the CJM and {research_catalog_review.REVIEW_FILENAME} decisions",
        },
        "availability": {
            "improvement_goal": {"status": "not_applicable", "reason": "new workflow has no Improvement Goal"},
            "to_be": {"status": "not_applicable", "reason": "SBP evaluation does not generate a To-be proposal"},
        },
        "source_snapshot": [
            {"artifact_type": "as_is_graph", "source_ref": "service_graph.yaml", "status": "ready",
             "version_ref": baseline.as_is_version_id, "content_digest": baseline.as_is_digest,
             "versioning": "lifecycle_current"},
            {"artifact_type": "confirmed_journey", "source_ref": "journey.yaml", "status": "ready",
             "version_ref": None, "content_digest": _digest(journey, "confirmed_journey"),
             "versioning": "run_scoped_current_file"},
            {"artifact_type": "research_catalog", "source_ref": "lifecycle-current:research_catalog", "status": "ready",
             "version_ref": (operation.get("result") or {}).get("research_version_id"),
             "content_digest": lifecycle.canonical_content_digest(catalog), "versioning": "lifecycle_current"},
            {"artifact_type": "research_catalog_review", "source_ref": research_catalog_review.REVIEW_FILENAME,
             "status": "ready" if review is not None else "unavailable", "version_ref": None,
             "content_digest": _digest(review, "research_catalog_review") if review is not None else None,
             "versioning": "run_scoped_current_file"},
        ],
    }
    return DeliveryViewModel.from_payload(payload)
