"""Deterministic de-identification boundary for external As-is research."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any


PROJECTION_VERSION = "research-projection-v2"
MIN_KNOWN_VALUE_LENGTH = 2
MIN_EVIDENCE_SPAN_LENGTH = 8

FORBIDDEN_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("email", "contact_detail", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)),
    ("phone", "contact_detail", re.compile(r"(?<!\d)(?:\+?886[-\s]?)?0?9\d{2}[-\s]?\d{3}[-\s]?\d{3}(?!\d)")),
    ("taiwan_id", "government_identifier", re.compile(r"\b[A-Z][12]\d{8}\b", re.I)),
    ("url", "case_url", re.compile(r"\b(?:https?|file)://[^\s]+", re.I)),
    ("windows_path", "local_path", re.compile(r"\b[A-Z]:\\[^\s]+", re.I)),
    ("unix_path", "local_path", re.compile(r"(?<!:)\/(?:Users|home|private|var|tmp|Volumes)\/[^\s]+")),
    ("filename", "filename", re.compile(r"\b[^\s/\\]+\.(?:txt|pdf|docx?|xlsx?|csv|yaml|json)\b", re.I)),
    ("canonical_id", "artifact_identifier", re.compile(r"\b(?:CASE|SC|AV|E|A|P|I|R|RC|T|TB)-[A-Za-z0-9:_-]+\b")),
)


class PrivacyBoundaryError(ValueError):
    code = "privacy_boundary_rejected"

    def __init__(self, audit: list[dict[str, str]]):
        super().__init__("research payload failed privacy boundary")
        self.audit = audit


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def case_value_replacements(case: Mapping[str, Any]) -> dict[str, str]:
    """Build role-preserving replacements without exposing case metadata."""
    replacements: dict[str, str] = {}
    case_id = case.get("case_id")
    if isinstance(case_id, str):
        replacements[case_id] = "案例"
    provider = case.get("primary_provider")
    if isinstance(provider, str):
        replacements[provider] = "服務提供者"
    for customer in case.get("customers") or []:
        if isinstance(customer, str):
            replacements[customer] = "顧客"
    for partner in case.get("external_partners") or []:
        role = partner.get("role") if isinstance(partner, dict) else None
        if isinstance(role, str):
            replacements[role] = "外部合作角色"
    return {
        value: replacement
        for value, replacement in replacements.items()
        if len(_normalize(value)) >= MIN_KNOWN_VALUE_LENGTH
    }


def _replacement_map(
    known_values: Mapping[str, str] | Iterable[str] | None,
) -> dict[str, str]:
    if known_values is None:
        return {}
    if isinstance(known_values, Mapping):
        source = known_values.items()
    else:
        source = ((value, "服務對象") for value in known_values)
    return {
        str(value): str(replacement)
        for value, replacement in source
        if isinstance(value, str)
        and isinstance(replacement, str)
        and len(_normalize(value)) >= MIN_KNOWN_VALUE_LENGTH
    }


def replace_known_values(
    text: str,
    known_values: Mapping[str, str] | Iterable[str] | None,
) -> str:
    result = _normalize(text)
    for value, replacement in sorted(
        _replacement_map(known_values).items(), key=lambda item: len(item[0]), reverse=True
    ):
        result = re.sub(re.escape(value), replacement, result, flags=re.I)
    return result


def generalize_improvement_goal(
    improvement_goal: str,
    known_values: Mapping[str, str] | Iterable[str] | None = None,
) -> str:
    """Map an exact local brief to a stable purpose that cannot reproduce it."""
    goal = replace_known_values(improvement_goal, known_values).casefold()
    categories = (
        (("等待", "時間", "延遲", "速度", "完成"), "縮短服務等待與完成時間"),
        (("進度", "狀態", "催問", "查詢", "通知"), "降低服務狀態不透明造成的查詢負擔"),
        (("錯誤", "失誤", "重工", "補件", "遺漏"), "降低服務失誤與重工"),
        (("理解", "易懂", "可讀", "識字", "可近"), "提升服務資訊的可理解性與可近性"),
        (("滿意", "體驗", "信任", "焦慮"), "改善主要服務體驗與信任"),
    )
    for keywords, generalized in categories:
        if any(keyword in goal for keyword in keywords):
            return generalized
    return "改善主要服務結果與使用者體驗"


def _string_leaves(value: Any, prefix: str = "payload") -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield prefix, value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _string_leaves(child, f"{prefix}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _string_leaves(child, f"{prefix}[{index}]")


def _audit(field: str, rule: str, category: str, value: str) -> dict[str, str]:
    return {
        "field": field,
        "rule": rule,
        "category": category,
        "value_digest": _digest(_normalize(value)),
    }


def validate_external_payload(
    payload: Any,
    *,
    known_values: Mapping[str, str] | Iterable[str] | None = None,
    evidence_spans: Iterable[str] = (),
    field_prefix: str = "payload",
) -> None:
    """Reject private values without returning their plaintext in diagnostics."""
    replacements = _replacement_map(known_values)
    spans = [
        _normalize(span)
        for span in evidence_spans
        if isinstance(span, str) and len(_normalize(span)) >= MIN_EVIDENCE_SPAN_LENGTH
    ]
    findings: list[dict[str, str]] = []
    for field, raw in _string_leaves(payload, field_prefix):
        text = _normalize(raw)
        if not text:
            continue
        for value in replacements:
            if re.search(re.escape(value), text, re.I):
                findings.append(_audit(field, "known_case_value", "case_identifier", value))
        for rule, category, pattern in FORBIDDEN_PATTERNS:
            match = pattern.search(text)
            if match:
                findings.append(_audit(field, rule, category, match.group(0)))
        for span in spans:
            if span in text or (len(text) >= 12 and text in span):
                findings.append(_audit(field, "verbatim_evidence", "raw_evidence", span))
    if findings:
        unique = {
            (item["field"], item["rule"], item["category"], item["value_digest"]): item
            for item in findings
        }
        raise PrivacyBoundaryError([unique[key] for key in sorted(unique)])


QUESTION_FRAMES = {
    "sbp_evaluation": (
        "Evaluate feasibility and weakly supported service assumptions in the reviewed blueprint.",
        "Find transferable evidence, operational requirements, resource needs, costs and lower-complexity alternatives.",
    ),
    "implementation": (
        "Inform how an adopted design direction could be implemented; findings are design reference, not case evidence.",
        "Find how comparable services implemented this kind of practice, the conditions it required, and the limits they met.",
    ),
}


def build_research_projection(
    issue_package: Mapping[str, Any],
    *,
    service_context: str,
    improvement_goal: str,
    known_values: Mapping[str, str] | Iterable[str] | None = None,
    evidence_spans: Iterable[str] = (),
    sbp_questions: Iterable[Mapping[str, str]] = (),
    mode: str = "sbp_evaluation",
) -> dict[str, Any]:
    """Project canonical issues into the only shape visible to research tools.

    `mode` frames the questions: "sbp_evaluation" (default) evaluates a
    reviewed blueprint; "implementation" asks how adopted Solutions are made
    to work elsewhere, before the blueprint exists
    (pre-blueprint-implementation-research)."""
    if mode not in QUESTION_FRAMES:
        raise ValueError(f"unknown research projection mode: {mode}")
    question_impact, question_purpose = QUESTION_FRAMES[mode]
    replacements = _replacement_map(known_values)
    context = replace_known_values(service_context, replacements)
    generalized_goal = generalize_improvement_goal(improvement_goal, replacements)
    projected = []
    for index, issue in enumerate(issue_package.get("issues") or [], 1):
        if not isinstance(issue, dict):
            continue
        statement = replace_known_values(str(issue.get("statement") or ""), replacements)
        impact = replace_known_values(str(issue.get("impact") or ""), replacements)
        projected.append({
            "alias": f"Q-{index:03d}",
            "statement": statement,
            "impact": impact,
            "research_purpose": (
                "Find transferable evidence, constraints, and first-party cases about "
                + statement
            ),
        })
    questions = list(sbp_questions)
    for item in questions:
        question = replace_known_values(str(item.get("question") or ""), replacements)
        if not question:
            continue
        projected.append({
            "alias": f"Q-{len(projected) + 1:03d}",
            "statement": question,
            "impact": question_impact,
            "research_purpose": question_purpose,
        })
    projection = {
        "projection_version": PROJECTION_VERSION,
        "service_context": context,
        "improvement_goal": generalized_goal,
        "issues": projected,
    }
    if questions:
        projection["research_mode"] = mode
    validate_external_payload(
        projection,
        known_values=replacements,
        evidence_spans=evidence_spans,
        field_prefix="research_projection",
    )
    return projection


def build_supplemental_research_projection(
    existing_projection: Mapping[str, Any],
    *,
    generalized_purpose: str,
    known_values: Mapping[str, str] | Iterable[str] | None = None,
    evidence_spans: Iterable[str] = (),
) -> dict[str, Any]:
    """Add one already-generalized revision purpose without exposing lineage.

    The exact revision instruction and To-be artifact never enter this
    function.  The complete projected payload is checked again because a
    caller-provided "generalized" purpose is not trusted by name alone.
    """
    allowed_projection = {
        "projection_version": existing_projection.get("projection_version"),
        "service_context": existing_projection.get("service_context"),
        "improvement_goal": existing_projection.get("improvement_goal"),
        "issues": existing_projection.get("issues"),
        "supplemental_research_purpose": " ".join(generalized_purpose.split()),
    }
    if not allowed_projection["supplemental_research_purpose"]:
        raise PrivacyBoundaryError([{
            "field": "supplemental_research_purpose",
            "rule": "empty_generalized_purpose",
            "category": "revision_purpose",
            "value_digest": _digest(""),
        }])
    validate_external_payload(
        allowed_projection,
        known_values=known_values,
        evidence_spans=evidence_spans,
        field_prefix="supplemental_research_projection",
    )
    return allowed_projection


def projection_json(projection: Mapping[str, Any]) -> str:
    """Stable serialization used as the Claude research prompt payload."""
    return json.dumps(
        projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
