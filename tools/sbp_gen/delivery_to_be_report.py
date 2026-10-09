"""Read-only Local V1 ``to_be_report`` delivery adapter.

The adapter resolves the lifecycle-current To-be bundle through the existing
To-be baseline resolver.  It carries proposal, validation, external-research,
and blueprint lineage into a normalized delivery view without changing any
canonical artifact or treating a proposal as an observed result.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Mapping

from . import research_catalog_review, to_be
from .delivery import DeliveryError, DeliveryViewModel, _digest


TO_BE_REPORT = "to_be_report"


def _as_delivery_error(exc: Exception, artifact: str) -> DeliveryError:
    if isinstance(exc, DeliveryError):
        return exc
    code = getattr(exc, "code", None)
    return DeliveryError(
        "to_be_baseline_unavailable" if not isinstance(code, str) else code,
        artifact,
        getattr(exc, "detail_digest", None) or type(exc).__name__,
    )


def _source_ref(
    artifact_type: str,
    source_ref: str,
    data: Any,
    *,
    status: str = "ready",
    version_ref: str | None = None,
    reason: str | None = None,
    content_digest: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "artifact_type": artifact_type,
        "source_ref": source_ref,
        "status": status,
        "version_ref": version_ref,
        "content_digest": content_digest if content_digest is not None else (_digest(data, artifact_type) if data is not None else None),
        "versioning": "lifecycle_current" if version_ref else "run_scoped_current_file",
    }
    if reason:
        result["reason"] = reason
    return result


def _review_decisions(review: Mapping[str, Any]) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for entry in review.get("claim_decisions") or []:
        if not isinstance(entry, Mapping):
            continue
        decisions.append({
            "claim_ref": entry.get("claim_ref"),
            "decision": entry.get("decision"),
            "researcher_rationale": entry.get("researcher_rationale"),
        })
    return decisions


def _external_research_view(
    baseline: to_be.ToBeBaseline,
    review: Mapping[str, Any],
) -> dict[str, Any]:
    decisions = _review_decisions(review)
    by_decision: dict[str, list[str]] = {"select": [], "hold": [], "reject": []}
    for item in decisions:
        decision = item.get("decision")
        if decision in by_decision:
            by_decision[decision].append(str(item.get("claim_ref")))
    return {
        "status": "ready",
        "review_digest": _digest(review, "research_catalog_review"),
        "decisions": decisions,
        "decision_refs": by_decision,
        "selected_claims": copy.deepcopy(baseline.research_catalog.get("claims") or []),
        "selected_sources": copy.deepcopy(baseline.research_catalog.get("sources") or []),
        "selected_mechanisms": copy.deepcopy(
            baseline.research_directives.get("selected_mechanisms") or []
        ),
        "hold_conditions": copy.deepcopy(
            baseline.research_directives.get("design_conditions") or []
        ),
        "hold_questions": copy.deepcopy(
            baseline.research_directives.get("design_questions") or []
        ),
        "transfer_rule": (
            "SELECT 可支持 To-be 機制與提案；HOLD 只保留為條件／問題；"
            "REJECT 不得支持 To-be。外部研究不是 MUJI 使用者證據。"
        ),
    }


def _proposal_view(proposal: Mapping[str, Any]) -> dict[str, Any]:
    # Preserve the canonical proposal fields and their refs exactly.  The
    # renderer may arrange these fields, but the adapter does not rewrite them.
    return copy.deepcopy(dict(proposal))


def _issue_coverage_view(
    issue_coverage: Any,
    issue_package: Mapping[str, Any],
) -> list[dict[str, Any]]:
    issues_by_id = {
        item.get("id"): item
        for item in issue_package.get("issues") or []
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    records: list[dict[str, Any]] = []
    for item in issue_coverage or []:
        if not isinstance(item, Mapping):
            continue
        record = copy.deepcopy(dict(item))
        issue = issues_by_id.get(record.get("issue_ref"))
        if issue is not None:
            record["issue"] = copy.deepcopy(dict(issue))
        records.append(record)
    return records


def _source_snapshot(
    baseline: to_be.ToBeBaseline,
    current: to_be.CurrentToBe,
    review: Mapping[str, Any],
) -> list[dict[str, Any]]:
    return [
        _source_ref(
            "to_be_bundle",
            f".to_be_generation/{current.version_id}/staged/to_be_bundle.yaml",
            current.bundle,
            version_ref=current.version_id,
        ),
        _source_ref(
            "to_be_review",
            f".to_be_reviews/{current.version_id}.json",
            review,
            version_ref=current.version_id,
        ),
        _source_ref(
            "improvement_goal_review",
            "improvement_goal_review.yaml",
            {"operative_goal": baseline.improvement_goal},
        ),
        _source_ref(
            "as_is_graph",
            "service_graph.yaml",
            baseline.graph,
            version_ref=baseline.as_is_version_id,
        ),
        _source_ref(
            "issue_package",
            "lifecycle-current:issue_package",
            baseline.issue_package,
            version_ref=baseline.issue_version_id,
        ),
        _source_ref(
            "research_catalog",
            "lifecycle-current:research_catalog",
            None,
            version_ref=baseline.research_version_id,
            content_digest=baseline.research_digest,
        ),
        _source_ref(
            "research_catalog_review",
            research_catalog_review.REVIEW_FILENAME,
            review.get("reviewed_catalog") and review,
        ),
    ]


def build_to_be_report_view(run_dir: str | Path) -> DeliveryViewModel:
    """Build a normalized To-be projection from the current reviewed chain."""
    run_path = Path(run_dir).resolve()
    if not run_path.is_dir():
        raise DeliveryError("run_directory_missing", "run", run_path.name)

    try:
        baseline = to_be.resolve_baseline(run_path)
        current, review = to_be.load_current_review(baseline)
        research_review = research_catalog_review.load(run_path)
    except Exception as exc:
        raise _as_delivery_error(exc, "to_be_report") from exc

    bundle = current.bundle
    errors = to_be._bundle_errors(bundle, baseline)
    if errors:
        raise DeliveryError("required_artifact_invalid", "to_be_bundle", "; ".join(errors))
    if bundle.get("case_id") != baseline.case_id:
        raise DeliveryError("artifact_case_identity_mismatch", "to_be_bundle", bundle.get("case_id"))
    if bundle.get("scenario_id") != baseline.scenario_id:
        raise DeliveryError("artifact_scenario_identity_mismatch", "to_be_bundle", bundle.get("scenario_id"))
    if bundle.get("source_as_is_ref") != baseline.as_is_version_id:
        raise DeliveryError("to_be_baseline_not_current", "to_be_bundle", "As-is dependency mismatch")
    if review.get("version_id") != current.version_id or review.get("fingerprint") != current.fingerprint:
        raise DeliveryError("stale_to_be_fingerprint", "to_be_review")

    external = _external_research_view(baseline, research_review)
    issue_coverage = _issue_coverage_view(bundle.get("issue_coverage"), baseline.issue_package)
    proposals = [_proposal_view(item) for item in bundle.get("proposals") or [] if isinstance(item, Mapping)]
    blueprint_changes = [
        copy.deepcopy(dict(item))
        for item in bundle.get("blueprint_changes") or []
        if isinstance(item, Mapping)
    ]
    review_state = review.get("state")
    proposal_state = bundle.get("direction", {}).get("claim_state")

    payload = {
        "view_model_schema_version": 1,
        "delivery_type": TO_BE_REPORT,
        "export_id": TO_BE_REPORT,
        "status": "ready",
        "metadata": {
            "case_id": baseline.case_id,
            "scenario_id": baseline.scenario_id,
            "run_ref": run_path.name,
            "to_be_version_ref": current.version_id,
            "to_be_fingerprint": current.fingerprint,
            "to_be_review_state": review_state,
            "proposal_state": proposal_state,
            "reviewed_as_is_fingerprint": baseline.reviewed_fingerprint,
        },
        "operative_goal": {
            "text": baseline.improvement_goal,
            "status": "ready",
            "provenance": "researcher_confirmed_operational_goal",
            "source_ref": "improvement_goal_review.yaml",
        },
        "direction": copy.deepcopy(bundle.get("direction") or {}),
        "issue_coverage": issue_coverage,
        "proposals": proposals,
        "blueprint_changes": blueprint_changes,
        "revision_context": copy.deepcopy(bundle.get("revision_context") or {}),
        "external_research": external,
        "provenance": {
            "to_be_bundle": {"source_ref": "to_be_bundle", "version_ref": current.version_id, "status": "current"},
            "to_be_review": {"source_ref": f".to_be_reviews/{current.version_id}.json", "state": review_state},
            "as_is": {"source_ref": "service_graph.yaml", "version_ref": baseline.as_is_version_id, "status": "current"},
            "issue_package": {"source_ref": "lifecycle-current:issue_package", "version_ref": baseline.issue_version_id},
            "research_review": {"source_ref": research_catalog_review.REVIEW_FILENAME, "digest": external["review_digest"]},
        },
        "availability": {
            "to_be_bundle": {"status": "ready", "reason": None},
            "to_be_review": {"status": "ready", "reason": None},
            "operative_goal": {"status": "ready", "reason": None},
            "as_is_graph": {"status": "ready", "reason": None},
            "issue_package": {"status": "ready", "reason": None},
            "research_catalog": {"status": "ready", "reason": None},
            "research_catalog_review": {"status": "ready", "reason": None},
            "validation": {"status": "ready", "reason": "proposal_validation_plans_are_not_results"},
        },
        "notices": [
            {
                "code": "proposal_not_approved_result",
                "detail": f"目前 To-be review state 為 {review_state}；提案與 validation plan 不是已觀察結果或已核准成效。",
            },
            {
                "code": "external_research_transfer_limited",
                "detail": "SELECT 僅作為可轉移機制支持；HOLD 為條件／問題；REJECT 不進入提案支持，外部研究不等於 MUJI 使用者證據。",
            },
            {
                "code": "unsupported_issues_preserved",
                "detail": "沒有足夠 SELECT 支持的 issue 維持 not_addressed，不為完整性新增提案。",
            },
        ],
        "source_snapshot": _source_snapshot(baseline, current, research_review),
    }
    return DeliveryViewModel.from_payload(payload)
