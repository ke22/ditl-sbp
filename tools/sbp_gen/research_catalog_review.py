"""Researcher gate over external research claims, resolved for To-be input.

`research_catalog_review.yaml` records the researcher's select/hold/reject
ruling per research_catalog claim, plus mechanisms (from SELECT claims) and
hold-only design conditions/questions (from HOLD claims).  This module
validates that ruling against the exact catalog being consumed and projects
it into (1) a driver catalog holding only SELECT claims and the sources they
link to, and (2) directives made of refs and researcher-authored text only.

External research is inspiration evidence, not user evidence: nothing here
touches MUJI evidence, and no claim or source text is copied into the review.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
import validate_sbp as V

from . import lifecycle

REVIEW_FILENAME = "research_catalog_review.yaml"

MISSING = "missing_research_review"
INVALID = "research_review_invalid"
STALE = "research_review_stale"


class ReviewError(Exception):
    def __init__(self, code: str, errors: list[str] | None = None):
        self.code = code
        self.errors = list(errors or [])
        super().__init__(f"{code}: {'; '.join(self.errors)}" if self.errors else code)


@dataclass(frozen=True)
class ResolvedReview:
    driver_catalog: dict[str, Any]
    directives: dict[str, list[dict[str, Any]]]
    review_digest: str


def load(run_dir: str | Path, *, filename: str = REVIEW_FILENAME) -> dict[str, Any]:
    """Absent file is `missing_research_review`; an unreadable or malformed
    file is `research_review_invalid`. Neither degrades to "no review".
    `filename` lets the pre-blueprint implementation research keep its own
    review file under the same contract."""
    path = Path(run_dir) / filename
    if not path.exists():
        raise ReviewError(MISSING)
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
        raise ReviewError(INVALID, [f"[review] {filename} cannot be read: {exc}"]) from exc
    if not isinstance(doc, dict):
        raise ReviewError(INVALID, [f"[review] {filename} must be a mapping"])
    return doc


def binding_errors(
    review: dict[str, Any],
    catalog: dict[str, Any],
    *,
    catalog_request_digest: str | None,
    case_id: str,
    scenario_id: str,
) -> list[str]:
    """Freshness: the review must cover this operation, this exact catalog
    content, and this case/scenario. `catalog_path` is never consulted."""
    errors: list[str] = []
    reviewed = review.get("reviewed_catalog") or {}
    if not catalog_request_digest:
        errors.append("[binding] producing operation request_digest is unknown")
    elif reviewed.get("request_digest") != catalog_request_digest:
        errors.append("[binding] reviewed_catalog.request_digest does not match the producing operation")
    if reviewed.get("catalog_digest") != lifecycle.canonical_content_digest(catalog):
        errors.append("[binding] reviewed_catalog.catalog_digest does not match the catalog content")
    if review.get("case_id") != case_id:
        errors.append("[binding] case_id does not match the baseline")
    if review.get("scenario_id") != scenario_id:
        errors.append("[binding] scenario_id does not match the baseline")
    return errors


def semantic_errors(review: dict[str, Any], catalog: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    catalog_ids = [
        claim.get("id") for claim in catalog.get("claims") or [] if isinstance(claim, dict)
    ]
    known = set(catalog_ids)

    decisions: dict[str, str] = {}
    for entry in review.get("claim_decisions") or []:
        ref = entry.get("claim_ref")
        if ref not in known:
            errors.append(f"[review] claim_decisions.claim_ref={ref!r} is not in the catalog")
        elif ref in decisions:
            errors.append(f"[review] claim {ref!r} has more than one decision")
        else:
            decisions[ref] = entry.get("decision")
    for ref in catalog_ids:
        if ref not in decisions:
            errors.append(f"[review] catalog claim {ref!r} has no decision")

    def check_derivation(kind: str, items: list, allowed: str) -> None:
        for item in items:
            for ref in item.get("derived_from") or []:
                if ref not in known:
                    errors.append(f"[review] {kind} {item.get('id')!r} derives from unknown claim {ref!r}")
                    continue
                decision = decisions.get(ref)
                if decision == "reject":
                    errors.append(f"[review] {kind} {item.get('id')!r} derives from rejected claim {ref!r}")
                elif decision != allowed:
                    errors.append(
                        f"[review] {kind} {item.get('id')!r} derives from {decision or 'undecided'} "
                        f"claim {ref!r}; only {allowed} claims are allowed"
                    )

    mechanisms = review.get("design_mechanisms") or []
    conditions = review.get("design_conditions") or []
    questions = review.get("design_questions") or []
    check_derivation("mechanism", mechanisms, "select")
    check_derivation("design_condition", conditions, "hold")
    check_derivation("design_question", questions, "hold")

    seen: set[str] = set()
    for item in [*mechanisms, *conditions, *questions]:
        item_id = item.get("id")
        if item_id in seen:
            errors.append(f"[review] id {item_id!r} is used more than once")
        seen.add(item_id)
    return errors


def build_driver_catalog(catalog: dict[str, Any], select_refs: set[str]) -> dict[str, Any]:
    view = copy.deepcopy(catalog)
    view["claims"] = [
        claim for claim in view.get("claims") or []
        if isinstance(claim, dict) and claim.get("id") in select_refs
    ]
    kept_sources = {
        link.get("source_ref")
        for claim in view["claims"]
        for link in claim.get("source_links") or []
        if isinstance(link, dict)
    }
    view["sources"] = [
        source for source in view.get("sources") or []
        if isinstance(source, dict) and source.get("id") in kept_sources
    ]
    for query in view.get("query_audit") or []:
        if isinstance(query, dict):
            query["returned_source_refs"] = [
                ref for ref in query.get("returned_source_refs") or [] if ref in kept_sources
            ]
    return view


def _directives(review: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        "selected_mechanisms": [
            {
                "id": item["id"],
                "name": item["name"],
                "description": item["description"],
                "derived_from_claim_refs": list(item["derived_from"]),
            }
            for item in review.get("design_mechanisms") or []
        ],
        "design_conditions": [
            {
                "id": item["id"],
                "text": item["text"],
                "derived_from_claim_refs": list(item["derived_from"]),
                "status": "hold_only",
            }
            for item in review.get("design_conditions") or []
        ],
        "design_questions": [
            {
                "id": item["id"],
                "text": item["text"],
                "derived_from_claim_refs": list(item["derived_from"]),
                "status": "hold_only",
            }
            for item in review.get("design_questions") or []
        ],
    }


def resolve(
    run_dir: str | Path,
    catalog: dict[str, Any],
    *,
    catalog_request_digest: str | None,
    case_id: str,
    scenario_id: str,
) -> ResolvedReview:
    review = load(run_dir)
    structural = V.structural_errors(review, V.load_schema("research-catalog-review"))
    if structural:
        raise ReviewError(INVALID, structural)
    stale = binding_errors(
        review,
        catalog,
        catalog_request_digest=catalog_request_digest,
        case_id=case_id,
        scenario_id=scenario_id,
    )
    if stale:
        raise ReviewError(STALE, stale)
    invalid = semantic_errors(review, catalog)
    if invalid:
        raise ReviewError(INVALID, invalid)
    select_refs = {
        entry["claim_ref"] for entry in review["claim_decisions"] if entry["decision"] == "select"
    }
    return ResolvedReview(
        driver_catalog=build_driver_catalog(catalog, select_refs),
        directives=_directives(review),
        review_digest=lifecycle.canonical_content_digest(review),
    )
