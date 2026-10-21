#!/usr/bin/env python3
"""
sbp validate — 最小可行的契約檢查器（對應 pipeline.md §0.5 CLI wireframe 的 `sbp validate`）。

只做兩層檢查，故意不做 gate/ground/diff/score（那些留給之後的正式 CLI，§3.2 待辦）：
  1. 結構層：對照 schema/*.schema.json（JSON Schema）
  2. referential 層：evidence_ref 指到的 id 是否真的存在於 evidence.yaml；
     relations[].claim_refs 是否指到真的存在的 (step, layer, claim_idx)

用法：
  python3 validate_sbp.py evidence  path/to/evidence.yaml
  python3 validate_sbp.py sbp-draft path/to/sbp-draft.yaml  [--evidence path/to/evidence.yaml]
  python3 validate_sbp.py gt        path/to/gt.yaml         [--evidence path/to/evidence.yaml]
  python3 validate_sbp.py issue-package issues.yaml --evidence evidence.yaml --service-graph service_graph.yaml
  python3 validate_sbp.py research-catalog research_catalog.yaml
  python3 validate_sbp.py to-be-bundle to_be.yaml --evidence evidence.yaml --service-graph service_graph.yaml --issue-package issues.yaml --research-catalog research_catalog.yaml

不需要 evidence.yaml 也能跑（只做結構檢查），加上 --evidence 才會做 referential 檢查。
"""
import argparse
import copy
import json
import math
import re
import sys
import unicodedata
from pathlib import Path

from collections import Counter

import yaml
from yaml_loader import safe_load as load_safe_yaml
from jsonschema import Draft7Validator, RefResolver

from sbp_gen import lifecycle

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"

SCHEMA_FILES = {
    "chart-guide-draft": "chart-guide-draft.schema.json",
    "research-purpose": "research-purpose.schema.json",
    "narrative-job-candidates-draft": "narrative-job-candidates-draft.schema.json",
    "selective-themes": "selective-themes.schema.json",
    "selective-themes-draft": "selective-themes-draft.schema.json",
    "semantic-segments": "semantic-segments.schema.json",
    "semantic-segments-draft": "semantic-segments-draft.schema.json",
    "chart-guides": "chart-guides.schema.json",
    "journey-annotation-draft": "journey-annotation-draft.schema.json",
    "journey-annotations": "journey-annotations.schema.json",
    "artifact-lifecycle": "artifact-lifecycle.schema.json",
    "content-provenance": "content-provenance.schema.json",
    "issue-package": "issue-package.schema.json",
    "research-catalog": "research-catalog.schema.json",
    "implementation-research-operation": "implementation-research-operation.schema.json",
    "to-be-bundle": "to-be-bundle.schema.json",
    "pilot-record": "pilot-record.schema.json",
    "validation-outcome": "validation-outcome.schema.json",
    "case": "case.schema.json",
    "evidence": "evidence.schema.json",
    "service-graph": "service-action-graph.schema.json",
    "reference-hypotheses": "reference-hypotheses.schema.json",
    "journey": "journey.schema.json",
    "journey-solutions": "journey-solutions.schema.json",
    "journey-recheck": "journey-recheck.schema.json",
    "experience-annotations": "experience-annotations.schema.json",
    "event-dispositions": "event-dispositions.schema.json",
    "source-manifest": "source-manifest.schema.json",
    "sbp-draft": "sbp-draft.schema.json",
    "gt": "gt.schema.json",
    "briefing": "briefing.schema.json",
    "reviewed-blueprint-version": "reviewed-blueprint-version.schema.json",
    "axial-coding": "axial-coding.schema.json",
    "axial-coding-adjustments": "axial-coding-adjustments.schema.json",
    "open-coding": "open-coding.schema.json",
    "category-properties": "category-properties.schema.json",
    "selective-coding": "selective-coding.schema.json",
    "core-category-candidates": "core-category-candidates.schema.json",
    "persona-matrix": "persona-matrix.schema.json",
    "persona-content": "persona-content.schema.json",
    "persona-dimension-candidates": "persona-dimension-candidates.schema.json",
    "persona-dimensions-selection": "persona-dimensions-selection.schema.json",
    "primary-job-candidates": "primary-job-candidates.schema.json",
    "narrative-job-candidates": "narrative-job-candidates.schema.json",
    "narrative-job-selection": "narrative-job-selection.schema.json",
    "primary-job-selection": "primary-job-selection.schema.json",
    "primary-hmw-candidates": "primary-hmw-candidates.schema.json",
    "primary-hmw-selection": "primary-hmw-selection.schema.json",
    "segmentation-axis-candidates": "segmentation-axis-candidates.schema.json",
    "segmentation-axis-selection": "segmentation-axis-selection.schema.json",
    "target-segmentation": "target-segmentation.schema.json",
    "target-segmentation-selection": "target-segmentation-selection.schema.json",
    "decisions": "decisions.schema.json",
    "jtbd": "jtbd.schema.json",
    "job-map": "job-map.schema.json",
    "hmw": "hmw.schema.json",
    "improvement-goal-review": "improvement-goal-review.schema.json",
    "research-catalog-review": "research-catalog-review.schema.json",
}


def load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return load_safe_yaml(f)


def selective_coding_narrative_errors(data: object, evidence_ids: set[str]) -> list[str]:
    """selective-coding-produces-a-narrative: schema-level bounds (150-200
    character narrative, 3-5 key insights) are already in
    schema/selective-coding.schema.json and caught by structural_errors();
    this adds the one check the schema cannot express -- that every key
    insight's evidence_refs actually exist in this run's evidence."""
    errors = structural_errors(data, load_schema("selective-coding"))
    if not isinstance(data, dict):
        return errors
    for insight in data.get("key_insights") or []:
        if not isinstance(insight, dict):
            continue
        for ref in insight.get("evidence_refs") or []:
            if ref not in evidence_ids:
                errors.append(f"[selective_coding] key_insights 引用不存在的證據：{ref}")
    return errors


PROPERTY_NOMINALIZATION_SUFFIXES = ("度", "性", "感")
PROPERTY_NOMINALIZATION_MAX_LENGTH = 5

PROPERTY_PERSONALITY_ADJECTIVES = frozenset({
    "焦慮", "冷靜", "被動", "積極", "內向", "外向",
})


def category_properties_errors(data: object, category_ids: set[str], evidence_ids: set[str]) -> list[str]:
    """derive-axis-candidates-from-properties: the schema (required +
    minLength: 1 on pole_low_design/pole_high_design) already rejects an
    empty design consequence; this adds the checks the schema cannot
    express -- a pole stated as a personality adjective rather than an
    observable behaviour, a property name that is a nominalisation (ends in
    度/性/感 and is short -- the shape task-4.1 tests, not a judgement of
    whether a permitted name is a good one), a category_id absent from this
    run's axial coding, and an evidence_refs entry absent from this run's
    evidence."""
    errors = structural_errors(data, load_schema("category-properties"))
    if not isinstance(data, dict):
        return errors
    for prop in data.get("properties") or []:
        if not isinstance(prop, dict):
            continue
        pid = prop.get("id")
        name = prop.get("name")
        if (
            isinstance(name, str)
            and len(name) <= PROPERTY_NOMINALIZATION_MAX_LENGTH
            and name.endswith(PROPERTY_NOMINALIZATION_SUFFIXES)
        ):
            errors.append(
                f"[category_properties] {pid} 的名稱「{name}」是名詞化寫法"
                f"（結尾「度／性／感」且 {PROPERTY_NOMINALIZATION_MAX_LENGTH} 字以內），"
                f"改寫成講行為的說法（例如「對這套動線的熟悉程度」而不是「動線清晰度」）"
            )
        for pole_field in ("pole_low", "pole_high"):
            pole_value = prop.get(pole_field)
            if not isinstance(pole_value, str):
                continue
            hit = next((adj for adj in PROPERTY_PERSONALITY_ADJECTIVES if adj in pole_value), None)
            if hit:
                errors.append(
                    f"[category_properties] {pid} 的 {pole_field}「{pole_value}」"
                    f"用了人格形容詞「{hit}」，改寫成可觀察的行為狀態"
                )
        category_id = prop.get("category_id")
        if isinstance(category_id, str) and category_id not in category_ids:
            errors.append(f"[category_properties] {pid} 引用不存在的類別：{category_id}")
        for ref in prop.get("evidence_refs") or []:
            if ref not in evidence_ids:
                errors.append(f"[category_properties] {pid} 引用不存在的證據：{ref}")
    return errors


OPEN_CODING_MAX_REFS_PER_EVIDENCE = 2


def open_coding_errors(data: object, evidence_ids: set[str]) -> list[str]:
    """add-open-coding-stage: the fifteen/thirty-character name/description
    limits are schema-level (schema/open-coding.schema.json) and caught by
    structural_errors(); this adds the two checks the schema cannot
    express -- every evidence_refs entry actually exists in this run's
    evidence, and no single evidence record is claimed by more than two
    concepts (one line of transcript yields at most two concepts, so no
    evidence unit should end up doing more work than that)."""
    errors = structural_errors(data, load_schema("open-coding"))
    if not isinstance(data, dict):
        return errors
    concepts = data.get("concepts")
    if not isinstance(concepts, list):
        return errors
    reference_counts: dict[str, int] = {}
    for concept in concepts:
        if not isinstance(concept, dict):
            continue
        concept_id = concept.get("id")
        for ref in concept.get("evidence_refs") or []:
            if ref not in evidence_ids:
                errors.append(f"[open_coding] concept {concept_id} 引用不存在的證據：{ref}")
            reference_counts[ref] = reference_counts.get(ref, 0) + 1
    # open-coding-on-semantic-segments: a document coded from semantic
    # segments is capped per segment (see stages); the per-evidence cap stays
    # only for older documents without segment_refs.
    segment_based = any(isinstance(c, dict) and c.get("segment_refs") for c in concepts)
    for ref, count in ({} if segment_based else reference_counts).items():
        if count > OPEN_CODING_MAX_REFS_PER_EVIDENCE:
            errors.append(
                f"[open_coding] 證據 {ref} 被 {count} 個概念引用，超過上限 {OPEN_CODING_MAX_REFS_PER_EVIDENCE}"
            )
    return errors


def load_schema(kind):
    schema_path = SCHEMA_DIR / SCHEMA_FILES[kind]
    with open(schema_path, encoding="utf-8") as f:
        return json.load(f)


# 生成時必填、讀取時不必填。這兩件事真的不同：一份新產出如果少了節點說明就是
# 沒做完，該退回重寫；但磁碟上早就存著的 run 少了說明，是當初沒要求，不該因此
# 打不開。把差別寫成 schema 裡的 `x-generation-required`，而不是第二份欄位清單
# ——schema_contract 的存在理由就是不要有第二份，這裡沿用同一個來源。
# jsonschema 會忽略不認得的 `x-` 關鍵字，所以讀取路徑完全不受影響。
GENERATION_REQUIRED_KEYWORD = "x-generation-required"


def generation_schema(schema):
    """Same schema, with `x-generation-required` folded into `required`.

    Used only where a stage validates a model's fresh output. Read paths keep
    using `load_schema()` so historical artifacts stay openable.
    """
    def fold(node):
        if isinstance(node, list):
            return [fold(item) for item in node]
        if not isinstance(node, dict):
            return node
        folded = {key: fold(value) for key, value in node.items()}
        extra = folded.pop(GENERATION_REQUIRED_KEYWORD, None)
        if extra:
            required = list(folded.get("required") or [])
            required.extend(key for key in extra if key not in required)
            folded["required"] = required
        return folded

    return fold(schema)


def case_errors(data):
    """Cross-field rules that JSON Schema draft-07 cannot express portably."""
    if not isinstance(data, dict):
        return []
    errors = []
    steps = data.get("steps") or []
    for field in ("journey_start", "journey_end"):
        if data.get(field) and data[field] not in steps:
            errors.append(f"[case] {field}={data[field]!r} 必須出現在 steps")
    if len(set(steps)) != len(steps):
        errors.append("[case] steps 不得重複")
    roles = [p.get("role") for p in data.get("external_partners") or [] if isinstance(p, dict)]
    if len(set(roles)) != len(roles):
        errors.append("[case] external_partners.role 不得重複")
    return errors


def journey_recheck_errors(
    data,
    *,
    source_manifest=None,
    source_units=None,
    prior_evidence_ids=None,
):
    """Validate the semantic boundary of a journey-recheck result.

    ``source_units`` is a server-resolved ``{(filename, locator): text}``
    mapping.  It deliberately contains extracted case-local units rather than
    paths supplied by a client.  When ``prior_evidence_ids`` is supplied, only
    newly admitted evidence must be re-proven against those units; existing
    evidence keeps the validation performed when its canonical version was
    created.
    """
    if not isinstance(data, dict):
        return []
    errors = []
    evidence = [item for item in data.get("reconciled_evidence") or [] if isinstance(item, dict)]
    evidence_ids = [item.get("id") for item in evidence]
    evidence_index = {item.get("id"): item for item in evidence if item.get("id")}
    for duplicate in sorted({item for item in evidence_ids if item and evidence_ids.count(item) > 1}):
        errors.append(f"[journey-recheck] evidence id 重複：{duplicate}")

    journey = data.get("reconciled_journey") or {}
    errors.extend(journey_identity_topology_errors(journey))
    if journey.get("case_id") != data.get("case_id"):
        errors.append(
            f"[journey-recheck] reconciled_journey.case_id={journey.get('case_id')!r} "
            f"與 result.case_id={data.get('case_id')!r} 不一致"
        )
    scenarios = [item for item in journey.get("scenarios") or [] if isinstance(item, dict)]
    scenario_index = {item.get("id"): item for item in scenarios if item.get("id")}
    primary = scenario_index.get(journey.get("primary_scenario"))
    if primary is None:
        errors.append("[journey-recheck] primary_scenario 不存在")
        primary_steps = []
    else:
        primary_steps = [item for item in primary.get("steps") or [] if isinstance(item, dict)]
    for scenario in scenarios:
        if scenario.get("status") != "proposed":
            errors.append(
                f"[journey-recheck] scenario {scenario.get('id')} 必須維持 proposed"
            )

    step_ids = [item.get("id") for item in primary_steps]
    assessments = [item for item in data.get("step_assessments") or [] if isinstance(item, dict)]
    assessment_ids = [item.get("step_ref") for item in assessments]
    for duplicate in sorted({item for item in assessment_ids if item and assessment_ids.count(item) > 1}):
        errors.append(f"[journey-recheck] step assessment 重複：{duplicate}")
    missing = sorted(set(step_ids) - set(assessment_ids))
    unknown = sorted(set(assessment_ids) - set(step_ids))
    if missing:
        errors.append(f"[journey-recheck] 缺少 step assessments：{', '.join(missing)}")
    if unknown:
        errors.append(f"[journey-recheck] assessment 指到不存在 steps：{', '.join(unknown)}")

    assessment_index = {item.get("step_ref"): item for item in assessments if item.get("step_ref")}
    for step in primary_steps:
        step_id = step.get("id")
        assessment = assessment_index.get(step_id)
        if assessment is None:
            continue
        journey_refs = set(step.get("evidence_refs") or [])
        assessment_refs = set(assessment.get("evidence_refs") or [])
        if journey_refs != assessment_refs:
            errors.append(
                f"[journey-recheck] step {step_id} evidence_refs 與 assessment 不一致"
            )
        for ref in sorted(assessment_refs):
            if ref not in evidence_index:
                errors.append(
                    f"[journey-recheck] step {step_id} evidence_ref={ref!r} 不存在"
                )

    finding_ids = []
    conflict_steps = set()
    for finding in data.get("journey_findings") or []:
        if not isinstance(finding, dict):
            continue
        finding_ids.append(finding.get("id"))
        affected = set(finding.get("affected_step_refs") or [])
        for ref in sorted(affected - set(step_ids)):
            errors.append(
                f"[journey-recheck] finding {finding.get('id')} step_ref={ref!r} 不存在"
            )
        for ref in finding.get("evidence_refs") or []:
            if ref not in evidence_index:
                errors.append(
                    f"[journey-recheck] finding {finding.get('id')} evidence_ref={ref!r} 不存在"
                )
        if finding.get("kind") == "conflict":
            conflict_steps.update(affected)
    for duplicate in sorted({item for item in finding_ids if item and finding_ids.count(item) > 1}):
        errors.append(f"[journey-recheck] journey finding id 重複：{duplicate}")
    assessed_conflicts = {
        item.get("step_ref") for item in assessments if item.get("state") == "conflict"
    }
    for ref in sorted(assessed_conflicts - conflict_steps):
        errors.append(f"[journey-recheck] conflict step {ref} 缺少 journey-level finding")

    attempts = [item for item in data.get("attempt_trace") or [] if isinstance(item, dict)]
    accepted = [item for item in attempts if item.get("outcome") == "accepted"]
    if len(accepted) != 1 or not attempts or attempts[-1].get("outcome") != "accepted":
        errors.append("[journey-recheck] attempt_trace 必須且只能以一個 accepted attempt 結束")
    attempt_numbers = [item.get("attempt") for item in attempts]
    if attempt_numbers != list(range(1, len(attempts) + 1)):
        errors.append("[journey-recheck] attempt_trace.attempt 必須從 1 連續遞增")

    if source_manifest is not None or source_units is not None:
        admitted_sources = {
            item.get("filename")
            for item in (source_manifest or {}).get("sources") or []
            if isinstance(item, dict) and item.get("status") in ("extracted", "partial")
        }
        units = source_units or {}
        prior = set(prior_evidence_ids or [])
        for item in evidence:
            if item.get("id") in prior:
                continue
            source, locator, excerpt = item.get("source"), item.get("locator"), item.get("text", "")
            if source not in admitted_sources:
                errors.append(
                    f"[journey-recheck] evidence {item.get('id')} source={source!r} 未由 manifest admitted"
                )
                continue
            unit = units.get((source, locator))
            if unit is None:
                whole_source = units.get((source, "*"))
                if whole_source is not None and locator and locator in whole_source:
                    unit = whole_source
            if unit is None:
                errors.append(
                    f"[journey-recheck] evidence {item.get('id')} locator={locator!r} 無法解析"
                )
            elif excerpt not in unit:
                errors.append(
                    f"[journey-recheck] evidence {item.get('id')} text 不存在於指定 raw-source unit"
                )
    return errors


def editable_journey_errors(journey):
    """Return errors that must be resolved before edit classification."""
    errors = structural_errors(journey, load_schema("journey"))
    if not isinstance(journey, dict):
        return errors
    scenarios = [item for item in journey.get("scenarios") or [] if isinstance(item, dict)]
    scenario_ids = [item.get("id") for item in scenarios]
    for duplicate in sorted({item for item in scenario_ids if item and scenario_ids.count(item) > 1}):
        errors.append(f"[journey-edit] scenario id 重複：{duplicate}")
    if journey.get("primary_scenario") not in scenario_ids:
        errors.append("[journey-edit] primary_scenario 不存在")
    for scenario in scenarios:
        step_ids = [item.get("id") for item in scenario.get("steps") or [] if isinstance(item, dict)]
        for duplicate in sorted({item for item in step_ids if item and step_ids.count(item) > 1}):
            errors.append(
                f"[journey-edit] scenario {scenario.get('id')} step id 重複：{duplicate}"
            )
    errors.extend(journey_identity_topology_errors(journey))
    return errors


def _editable_string(value):
    return unicodedata.normalize("NFC", value).strip()


def normalize_editable_journey(journey):
    """Project one journey onto researcher-editable semantics only.

    Evidence references and scenario status deliberately do not participate:
    recheck enriches those fields and final confirmation changes status without
    constituting another researcher edit.
    """
    errors = editable_journey_errors(journey)
    if errors:
        raise ValueError("歷程無法比較：\n" + "\n".join(errors))
    normalized = {
        "primary_scenario": _editable_string(journey["primary_scenario"]),
        "scenarios": [
            {
                "id": _editable_string(scenario["id"]),
                "label": _editable_string(scenario["label"]),
                "description": _editable_string(scenario.get("description", "")),
                "steps": [
                    {
                        "id": _editable_string(step["id"]),
                        "label": _editable_string(step["label"]),
                        "description": _editable_string(step.get("description", "")),
                    }
                    for step in scenario["steps"]
                ],
            }
            for scenario in journey["scenarios"]
        ],
    }
    if journey.get("schema_version") == 3:
        topology_normalized = normalize_journey_topology(journey)
        normalized["schema_version"] = 3
        normalized["branches"] = [
            {
                "branch_id": _editable_string(branch["branch_id"]),
                "label": _editable_string(branch["label"]),
                "divergence_step_id": _editable_string(branch["divergence_step_id"]),
                "merge_step_id": (
                    _editable_string(branch["merge_step_id"])
                    if branch.get("merge_step_id") is not None else None
                ),
                "topology_provenance": branch["topology_provenance"],
                "steps": [
                    {
                        "id": _editable_string(step["id"]),
                        "label": _editable_string(step["label"]),
                        "description": _editable_string(step.get("description", "")),
                    }
                    for step in branch["steps"]
                ],
            }
            for branch in topology_normalized.get("branches") or []
        ]
    return normalized


def editable_journey_digest(journey):
    """Return the canonical sha256 digest for editable journey semantics."""
    return lifecycle.canonical_content_digest(normalize_editable_journey(journey))


def normalize_journey_topology(journey):
    """Return a copy with deleted optional merge anchors normalized to null.

    A missing merge target is not a topology error: the branch remains valid
    without a merge.  Divergence anchors are required and are never repaired
    here.  This helper is deliberately pure so validation never mutates a
    canonical Journey document.
    """
    normalized = copy.deepcopy(journey)
    if not isinstance(normalized, dict) or normalized.get("schema_version") != 3:
        return normalized
    step_ids = {
        step.get("id")
        for scenario in normalized.get("scenarios") or []
        if isinstance(scenario, dict)
        for step in scenario.get("steps") or []
        if isinstance(step, dict) and isinstance(step.get("id"), str)
    }
    step_ids.update(
        step.get("id")
        for branch in normalized.get("branches") or []
        if isinstance(branch, dict)
        for step in branch.get("steps") or []
        if isinstance(step, dict) and isinstance(step.get("id"), str)
    )
    for branch in normalized.get("branches") or []:
        if not isinstance(branch, dict):
            continue
        merge_id = branch.get("merge_step_id")
        if isinstance(merge_id, str) and merge_id not in step_ids:
            branch["merge_step_id"] = None
    return normalized


def journey_identity_topology_errors(journey):
    """Validate v3 document-wide identity and computed Branch topology.

    Schema v2 intentionally retains its historical scenario-local identity
    checks.  These rules apply only to v3 and never introduce lifecycle state.
    """
    if not isinstance(journey, dict) or journey.get("schema_version") != 3:
        return []
    normalized = normalize_journey_topology(journey)
    errors = []
    scenarios = [item for item in normalized.get("scenarios") or [] if isinstance(item, dict)]
    branches = [item for item in normalized.get("branches") or [] if isinstance(item, dict)]

    scenario_ids = [item.get("id") for item in scenarios]
    if normalized.get("primary_scenario") not in scenario_ids:
        errors.append("[journey-v3] primary_scenario 不存在")

    branch_ids = [item.get("branch_id") for item in branches]
    for duplicate in sorted({item for item in branch_ids if item and branch_ids.count(item) > 1}):
        errors.append(f"[journey-v3] branch_id 重複：{duplicate}")

    all_step_ids = []
    primary_step_ids = []
    primary = next((item for item in scenarios if item.get("id") == normalized.get("primary_scenario")), None)
    for scenario in scenarios:
        for step in scenario.get("steps") or []:
            if not isinstance(step, dict):
                continue
            step_id = step.get("id")
            if isinstance(step_id, str):
                all_step_ids.append(step_id)
                if scenario is primary:
                    primary_step_ids.append(step_id)
    for branch in branches:
        for step in branch.get("steps") or []:
            if isinstance(step, dict) and isinstance(step.get("id"), str):
                all_step_ids.append(step["id"])
    for duplicate in sorted({item for item in all_step_ids if all_step_ids.count(item) > 1}):
        errors.append(f"[journey-v3] document-wide step_id 重複：{duplicate}")

    primary_set = set(primary_step_ids)
    all_step_set = set(all_step_ids)
    primary_order = {step_id: index for index, step_id in enumerate(primary_step_ids)}
    for branch in branches:
        branch_id = branch.get("branch_id")
        divergence = branch.get("divergence_step_id")
        if divergence not in primary_set:
            if divergence in all_step_set:
                errors.append(
                    f"[journey-v3] Branch {branch_id} divergence_step_id 必須指向 primary step：{divergence}"
                )
            else:
                errors.append(
                    f"[journey-v3] Branch {branch_id} divergence_step_id 不存在：{divergence}"
                )
            continue
        merge = branch.get("merge_step_id")
        if merge is None:
            continue
        if merge not in primary_set:
            if merge in all_step_set:
                errors.append(
                    f"[journey-v3] Branch {branch_id} merge_step_id 必須指向 primary step：{merge}"
                )
            # A missing optional merge target was normalized to null above.
            continue
        if primary_order[merge] < primary_order[divergence]:
            errors.append(
                f"[journey-v3] Branch {branch_id} merge_step_id 不得早於 divergence_step_id"
            )
    return errors


JOURNEY_RECHECK_CORPUS_TAGS = {
    "unchanged_bypass",
    "rename",
    "reorder",
    "supported_addition",
    "unsupported_addition",
    "deletion_cross_step",
    "conflicting_evidence",
    "malformed_output",
    "fallback",
    "repeated_edit",
    "stale_result_submission",
}


def evaluate_journey_recheck_corpus(corpus, observations):
    """Evaluate release observations against the versioned recheck corpus.

    The runner that obtains model observations is intentionally separate from
    this deterministic scorer.  This function defines the release-blocking
    arithmetic so a provider change cannot weaken thresholds in its own test
    harness.
    """
    errors = []
    cases = (corpus or {}).get("cases") or []
    if len(cases) < 5:
        errors.append("[journey-recheck-corpus] 至少需要 5 個完整案例")
    case_ids = [item.get("id") for item in cases if isinstance(item, dict)]
    for duplicate in sorted({item for item in case_ids if item and case_ids.count(item) > 1}):
        errors.append(f"[journey-recheck-corpus] case id 重複：{duplicate}")
    coverage = {
        tag
        for item in cases if isinstance(item, dict)
        for tag in item.get("tags") or []
    }
    missing_tags = sorted(JOURNEY_RECHECK_CORPUS_TAGS - coverage)
    if missing_tags:
        errors.append(
            "[journey-recheck-corpus] 缺少 coverage：" + ", ".join(missing_tags)
        )
    required_fields = {
        "raw_sources",
        "baseline_journey",
        "edited_journey",
        "expected_evidence",
        "forbidden_locators",
        "acceptable_outcomes",
    }
    schema_passes = reference_passes = routing_passes = stale_passes = 0
    fabricated_total = unjustified_total = 0
    recheck_cases = stale_cases = 0
    for case in cases:
        if not isinstance(case, dict):
            errors.append("[journey-recheck-corpus] case 必須是 object")
            continue
        case_id = case.get("id")
        missing = sorted(required_fields - set(case))
        if missing:
            errors.append(
                f"[journey-recheck-corpus] {case_id} 缺少欄位：{', '.join(missing)}"
            )
            continue
        observation = (observations or {}).get(case_id)
        if not isinstance(observation, dict):
            errors.append(f"[journey-recheck-corpus] {case_id} 缺少 observation")
            continue
        try:
            changed = editable_journey_digest(case["baseline_journey"]) != editable_journey_digest(
                case["edited_journey"]
            )
        except ValueError as exc:
            errors.append(f"[journey-recheck-corpus] {case_id} fixture 無效：{exc}")
            continue
        expected_route = "recheck" if changed else "unchanged"
        if observation.get("route") == expected_route:
            routing_passes += 1
        else:
            errors.append(
                f"[journey-recheck-corpus] {case_id} routing 預期 {expected_route}，"
                f"收到 {observation.get('route')!r}"
            )
        if changed:
            recheck_cases += 1
            if observation.get("schema_valid") is True:
                schema_passes += 1
            else:
                errors.append(f"[journey-recheck-corpus] {case_id} schema validity 未通過")
            if observation.get("references_valid") is True:
                reference_passes += 1
            else:
                errors.append(f"[journey-recheck-corpus] {case_id} reference validity 未通過")
            received_locators = set(observation.get("received_locators") or [])
            forbidden = set(case.get("forbidden_locators") or [])
            fabricated = len(received_locators.intersection(forbidden)) + int(
                observation.get("fabricated_excerpt_count") or 0
            )
            fabricated_total += fabricated
            if fabricated:
                errors.append(f"[journey-recheck-corpus] {case_id} 含 {fabricated} 筆 fabricated evidence")
            acceptable = [list(item) for item in case.get("acceptable_outcomes") or []]
            received = list(observation.get("outcome_labels") or [])
            if received not in acceptable:
                unjustified_total += 1
                errors.append(
                    f"[journey-recheck-corpus] {case_id} outcome 不在 acceptable set：{received!r}"
                )
        if "stale_result_submission" in (case.get("tags") or []):
            stale_cases += 1
            if observation.get("stale_rejected") is True:
                stale_passes += 1
            else:
                errors.append(f"[journey-recheck-corpus] {case_id} stale result 未被拒絕")
    metrics = {
        "cases": len(cases),
        "routing_percent": 100 * routing_passes / len(cases) if cases else 0,
        "schema_percent": 100 * schema_passes / recheck_cases if recheck_cases else 100,
        "reference_percent": 100 * reference_passes / recheck_cases if recheck_cases else 100,
        "fabricated_count": fabricated_total,
        "stale_rejection_percent": 100 * stale_passes / stale_cases if stale_cases else 100,
        "unjustified_outcome_count": unjustified_total,
    }
    return {"passed": not errors, "errors": errors, "metrics": metrics}


def structural_errors(data, schema):
    # Schemas use stable project IDs as well as relative sibling refs.  Register
    # both so validation never attempts an accidental network lookup.
    store = {}
    for filename in SCHEMA_FILES.values():
        path = SCHEMA_DIR / filename
        if path.exists():
            candidate = json.loads(path.read_text(encoding="utf-8"))
            project_id = candidate.get("$id", path.as_uri())
            local_candidate = json.loads(json.dumps(candidate))
            local_candidate.pop("$id", None)
            store[project_id] = local_candidate
            store[path.as_uri()] = local_candidate
            # RefResolver treats the project's relative $id as a filesystem
            # subdirectory after entering a referenced schema. Register that
            # normalized alias as well so nested refs never trigger file/network IO.
            store[(SCHEMA_DIR / "ditl_sbp" / "schema" / filename).as_uri()] = local_candidate
    # Project IDs are identifiers, not file-system-relative URLs. Removing the
    # relative $id for this local validation keeps `#/definitions/...` local.
    local_schema = json.loads(json.dumps(schema))
    local_schema.pop("$id", None)
    resolver = RefResolver(SCHEMA_DIR.as_uri() + "/", local_schema, store=store)
    validator = Draft7Validator(local_schema, resolver=resolver)
    errors = []
    for err in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
        loc = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"[結構] {loc}: {err.message}")
    return errors


LAYERS = ["實體證據", "顧客行動", "前台行動", "後台行動", "支援流程"]


# 普遍量詞。ADR-0004 決定三的機械檢查用：claim 宣告 general_pattern，
# 但來源原文找不到這類字，就是可能說得比來源重。純字串比對，不需要 AI 判斷。
# 這條規則原本掛在舊版 build() 的 validate()，pipeline 換成 structure()／action
# graph 之後曾經漏掉，這裡補回來，改成對 node.claims 而不是 cell.claims 做檢查。
UNIVERSAL_QUANTIFIERS = (
    "都", "每", "通常", "一律", "規定", "總是", "一般來講", "一般來說",
    "基本上", "多半", "大部分", "常常", "經常", "往往", "向來", "一直",
)

# distinguish-planned-from-observed-actions：未生效時間詞。跟 UNIVERSAL_
# QUANTIFIERS 同一套機械檢查邏輯——純字串比對，不需要 AI 判斷；命中但節點
# 沒標 status: planned，代表模型把「已宣布但還沒生效的政策」當成已發生的
# 動作生成了。
NOT_YET_EFFECTIVE_MARKERS = ("明年度", "開始實施", "即將", "新制", "尚未")

# flag-unmarked-decision-language：決策語氣詞，直接取自 structure.md 決策點
# 判準本身用的字（人員判斷、核准、選擇、分流）——命中但這個節點沒有任何
# F/W/D 標記，代表模型可能漏標，不是抓「模型判斷了但判斷本身有爭議」這種
# 邊界案例（機械掃描分不出這兩種情況，只能提高警覺）。
# 「分流」拿掉了：新定義明確說規則自動分流不是決策（研究者 2026-09-03 在正征
# 產出的走查上裁定的）。實測拿掉前後候選都是 14 個——它從沒單獨命中過，同一段
# 原文總是還有別的線索詞。留著沒有偵測上的好處，卻讓人以為它還算決策。
DECISION_LANGUAGE_MARKERS = ("判斷", "核准", "選擇", "決定", "視情況")

# ground-structure-stage-action-boundaries：泛稱的店家／人員角色詞。判斷一個
# 前台／後台 action 的 actor 有沒有根據時，不比對模型自己取的角色名稱字面
# 有沒有出現在證據原文裡（會被真實的同義改寫誤判，例如「現場服務人員」對
# 「店員」——恆隆行 measure-henglong run 撞出來的假警報），改成只要引用的
# 證據原文裡出現這些泛稱字之一，就代表原文本來就有提到顧客以外的人，這個
# 動作不是憑空捏造出行為者。
PROVIDER_ROLE_TERMS = (
    "店員", "服務生", "服務人員", "工作人員", "員工", "人員",
    "客服", "收銀", "店家", "老闆", "經理", "主管", "司機", "師傅",
)

# validate-concrete-actors: a subject that cannot be assigned, observed or
# changed is not an actor -- openspec/LANGUAGE.md's「服務」不能當主詞
# entry. structure.md's own actor rule already named 服務流程／指示系統
# (system/policy/process/mechanism) as banned; this list extends it with
# organisations, which the original examples did not name and which real
# data shows the identical defect on: 20 of 59 provider-layer nodes in
# runs/2026-08-21-manual-transport-chongzheng (generated ten days before
# that prompt rule existed) name 機構 as their actor. This tests shape, not
# quality -- whether a permitted name is well chosen stays the researcher's
# judgement.
ABSTRACT_ACTOR_TERMS = frozenset({
    "機構", "單位", "部門", "公司", "政府", "社會局", "醫療院所",
    "系統", "平台", "流程", "服務", "機制", "政策", "團隊", "組織",
})

# Gated on actor_boundary=="provider", not service_role -- frontstage_
# delivery and backstage_delivery both map to actor_boundary="provider"
# (see the role_map below), but service_role is absent entirely on
# runs/2026-08-21-manual-transport-chongzheng's graph (generated before
# that field existed; every one of its 59 nodes has service_role=None).
# actor_boundary is set on every node in both eras, so it is what actually
# lets this check reach the legacy data it was written to catch.
# included_support/external_dependency/customer/out_of_scope are
# deliberately exempt: internal_support/external_dependency routinely ARE
# organisations (the real MUJI graph's 預製食材供應 names 中央廚房/供應商
# as an internal_support actor, actor_boundary="included_support").
_ABSTRACT_ACTOR_BOUNDARY = "provider"

# require-marker-specific-reason-note：tools/sbp_review/ui.html 的 MARKER_HELP
# 三句通用類別定義，這裡是 Python 側的鏡射複本——JS 跟 Python 不能共用同一個
# 字面值，兩邊各自宣告是刻意的權宜做法，靠 tools/tests/test_action_graph_
# pipeline.py 裡一條逐字比對 ui.html 原文的測試守住不漂移，不是漏掉沒共用。
MARKER_GENERIC_NOTE = {
    "失誤點": "F：留意顧客抱怨與服務品質。",
    "等候點": "W：留意顧客或流程等待。",
    "決策點": "D：這是員工判斷或流程分支。",
    "交接點": "H：留意責任在角色間的移交。",
    "放棄點": "A：留意顧客或流程參與者中止未完成。",
}


def _disposition_coverage_errors(graph):
    """Disposition 最終用途稽核（close-sbp-semantic-coverage-gates 決定一）。

    `used`／`annotation` 標了但沒有真的被圖引用、`duplicate`／`out_of_scope` 卻被
    node claim 引用，都是資料誠實度問題——disposition 完整性檢查（每條證據恰好一個
    disposition）已經在 `stages._disposition_and_annotation_errors()` 做了，這裡補的是
    反向查核：標了的東西，圖上真的有對應行為嗎。`dispositions` 缺席（legacy graph）
    整段跳過，跟 schema 「兩個欄位都選填」的相容性原則一致。
    """
    dispositions = [d for d in graph.get("dispositions") or [] if isinstance(d, dict)]
    if not dispositions:
        return []
    nodes = graph.get("nodes") or []
    cited_by_claim = {
        c.get("evidence_ref")
        for n in nodes if isinstance(n, dict)
        for c in n.get("claims") or [] if isinstance(c, dict) and c.get("evidence_ref")
    }
    cited_by_relation = {
        ref for rel in graph.get("relations") or [] if isinstance(rel, dict)
        for ref in rel.get("evidence_refs") or []
    }
    cited_by_marker = {
        m.get("evidence_ref") for m in graph.get("markers") or []
        if isinstance(m, dict) and m.get("evidence_ref")
    }
    cited_by_annotation = {
        ref for a in graph.get("annotations") or [] if isinstance(a, dict)
        for ref in a.get("evidence_refs") or []
    }
    graph_citations = cited_by_claim | cited_by_relation | cited_by_marker
    errors = []
    for d in dispositions:
        ref, status = d.get("evidence_ref"), d.get("status")
        if status == "used" and ref not in graph_citations:
            errors.append(f"[disposition] evidence {ref} 標記 used，但沒有被任何 claim／relation／marker 引用")
        if status == "annotation" and ref not in cited_by_annotation:
            errors.append(f"[disposition] evidence {ref} 標記 annotation，但沒有被任何 annotation 引用")
        if status in ("duplicate", "out_of_scope") and ref in cited_by_claim:
            errors.append(f"[disposition] evidence {ref} 標記 {status}，卻被 node claim 引用")
    return errors


def _orphan_action_errors(graph):
    """孤立 action 檢查（close-sbp-semantic-coverage-gates 決定二）。

    action node 完全沒有任何 relation 碰到它、且不是它所屬 scenario 的第一步或最後一步，
    就報錯。排除 `支援流程` layer（已經有專屬的 outgoing supports 檢查）跟
    `external_dependency`／`out_of_scope` boundary（語意上就是不期待完整納入主線互動）。
    """
    nodes = [n for n in graph.get("nodes") or [] if isinstance(n, dict)]
    touched = set()
    for rel in graph.get("relations") or []:
        if not isinstance(rel, dict):
            continue
        ends = rel.get("endpoints") if rel.get("type") == "same_event" else [rel.get("from"), rel.get("to")]
        touched.update(e for e in (ends or []) if e)
    step_range = {}  # scenario_ref -> (min_step, max_step)
    for n in nodes:
        step = n.get("step")
        if step is None:
            # step 跟 scenario_refs 一樣是選填欄位；缺 step 的節點不能拿來算
            # min/max，硬算會在兩個都缺的節點相遇時炸掉（min(None, None)）。
            continue
        for sref in n.get("scenario_refs") or []:
            lo, hi = step_range.get(sref, (step, step))
            step_range[sref] = (min(lo, step), max(hi, step))
    errors = []
    for n in nodes:
        if n.get("kind") != "action" or n.get("id") in touched:
            continue
        if n.get("layer") == "支援流程" or n.get("actor_boundary") in ("external_dependency", "out_of_scope"):
            continue
        srefs = n.get("scenario_refs") or []
        if not srefs:
            # scenario_refs 是選填欄位；沒有它就沒辦法判斷這個節點是不是它 scenario 的
            # 第一步或最後一步，貿然套用孤立檢查會對沒有 scenario_refs 的舊資料／模擬
            # fixture 產生大量誤判（實作時在既有 fixture 上發現），所以先排除。
            continue
        is_boundary = any(n.get("step") in step_range.get(s, (None, None)) for s in srefs)
        if not is_boundary:
            errors.append(f"[graph] action {n.get('id')} 沒有任何 relation，且不是所屬 scenario 的第一步或最後一步")
    return errors


def step_axis_errors(graph, journey):
    """節點的 step 是否跟已確認 journey 指派給該證據的步驟一致
    （fix-fabricated-research-gaps-from-step-axis-collapse 決定一，
    fix-step-axis-multi-step-evidence 決定一放寬成集合成員判斷）。

    只檢查 primary scenario——非主情境節點混入主藍圖是另一個 change 的範圍
    （proposal.md Non-Goals）。journey 缺 primary scenario、或某個步驟沒有
    `evidence_refs`，就跳過對應檢查，不報錯（比照孤立 action 檢查對缺
    `scenario_refs` 節點的既有處理）。不搬動任何節點，只回報不一致。

    同一條證據可能被 journey 指派給不只一個步驟（例如恆隆行案例的 E-412
    同時在 S-05 與 S-08）——這是 journey 階段的正常行為，不是資料錯誤。
    節點只要落在該證據被指派到的任一步驟就算一致，不要求同時符合所有指派。
    """
    if not isinstance(journey, dict):
        return []
    scenarios = {s.get("id"): s for s in journey.get("scenarios") or [] if isinstance(s, dict)}
    primary = scenarios.get(journey.get("primary_scenario"))
    if not primary:
        return []
    primary_scenario_id = journey.get("primary_scenario")
    nodes = [n for n in (graph or {}).get("nodes") or [] if isinstance(n, dict)]
    # evidence_ref -> {(節點實際 step, 節點 label), ...}：graph 裡誰真的用了這條證據。
    # 只收主情境的節點（含沒填 scenario_refs 的舊資料／模擬 fixture）；只屬於
    # 非主情境的節點不算「混用」，那是 scenario_axis_errors 的檢查範圍。
    usage: dict[str, set] = {}
    for n in nodes:
        srefs = n.get("scenario_refs") or []
        if srefs and primary_scenario_id not in srefs:
            continue
        for cl in n.get("claims") or []:
            if not isinstance(cl, dict):
                continue
            ref = cl.get("evidence_ref")
            if ref:
                usage.setdefault(ref, set()).add((n.get("step"), n.get("label", "")))

    # evidence_ref -> journey 指派給它的所有步驟（可能不只一個）。
    assigned: dict[str, set] = {}
    for step_no, step in enumerate(primary.get("steps") or [], 1):
        if not isinstance(step, dict):
            continue
        for ref in step.get("evidence_refs") or []:
            assigned.setdefault(ref, set()).add(step_no)

    errors = []
    for ref, steps in assigned.items():
        for actual_step, label in usage.get(ref, ()):
            if actual_step not in steps:
                steps_text = "、".join(str(s) for s in sorted(steps))
                errors.append(
                    f"[graph] journey 指派給步驟 {steps_text} 的證據 {ref} 被步驟 {actual_step} 的節點"
                    f"「{label}」claim 使用，不屬於任何指派步驟"
                )
    return errors


def scenario_axis_errors(graph, journey):
    """節點的 step 必須落在自己所屬 scenario 的步驟軸上
    （separate-non-primary-scenarios-in-blueprint 決定一）。

    node 沒有 `scenario_refs`、或引用的 scenario id 在 journey 裡找不到時跳過，
    不報錯。不搬動任何節點、也不改 scenario_refs 或 step，只回報不一致——
    要不要重新編軸是研究決定。

    只檢查「值是否落在 1..len(steps) 內」不夠：node.step 實際上是共用的全案例
    軸（`stages.project()` 用同一個 case-wide 計數器投影所有情境），一個情境
    的節點被誤編到主軸上時，個別數值可能剛好落在自己情境的步數範圍內，逃過
    純粹的成員檢查（例如 3 步的情境裡混進 5 個不同 step 值，其中兩個剛好
    ≤3）。所以同時檢查「同一個情境底下的節點，用到的相異 step 值總數不能
    超過該情境宣告的步數」——超過時無法判斷哪幾個才是對的，全部一併回報。
    """
    if not isinstance(journey, dict):
        return []
    scenarios = {s.get("id"): s for s in journey.get("scenarios") or [] if isinstance(s, dict)}
    nodes = [n for n in (graph or {}).get("nodes") or [] if isinstance(n, dict)]
    by_scenario: dict[str, list] = {}
    for n in nodes:
        for ref in n.get("scenario_refs") or []:
            by_scenario.setdefault(ref, []).append(n)

    errors = []
    for scenario_id, scenario_nodes in by_scenario.items():
        scenario = scenarios.get(scenario_id)
        if not scenario:
            continue
        step_numbers = sorted(range(1, len(scenario.get("steps") or []) + 1))
        distinct_steps = {n.get("step") for n in scenario_nodes}
        overflow = len(distinct_steps) > len(step_numbers)
        for n in scenario_nodes:
            step, label = n.get("step"), n.get("label", "")
            scenario_label = scenario.get("label", scenario_id)
            if step not in step_numbers:
                errors.append(
                    f"[graph] 節點「{label}」屬於情境「{scenario_label}」，"
                    f"但 step={step} 不在該情境的步驟編號 {step_numbers} 內"
                )
            elif overflow:
                errors.append(
                    f"[graph] 節點「{label}」屬於情境「{scenario_label}」的 step={step} 雖在 "
                    f"{step_numbers} 範圍內，但同情境節點共用了 {len(distinct_steps)} 種不同 step 值，"
                    f"超過該情境宣告的 {len(step_numbers)} 步，無法確認實際對應哪一步"
                )
    return errors


def node_in_primary_scenario(node: dict, journey) -> bool:
    """True unless the node is tagged with `scenario_refs` that exclude the
    journey's primary scenario. No journey, or an untagged node, means the
    scenario-separation rule doesn't apply — backward compatible with graphs
    that predate `scenario_refs`."""
    primary = journey.get("primary_scenario") if isinstance(journey, dict) else None
    if not primary:
        return True
    refs = (node or {}).get("scenario_refs") or []
    if not refs:
        return True
    return primary in refs


def _selected_solution_ids_by_position(journey: dict | None) -> dict[int, set[str]]:
    if not isinstance(journey, dict):
        return {}
    scenario = next((item for item in journey.get("scenarios") or []
                     if isinstance(item, dict) and item.get("id") == journey.get("primary_scenario")), {})
    return {position: set(step.get("selected_solution_ids") or [])
            if step.get("develop_solution", True) is True else set()
            for position, step in enumerate(scenario.get("steps") or [], 1) if isinstance(step, dict)}


TO_BE_METHOD = "to_be_v1"
TO_BE_ANCHOR_KINDS = ("selected_solution", "journey_step", "service_node")
TO_BE_FUTURE_STATES = ("maintained", "changed", "added")


def _primary_steps(journey: dict | None) -> list[dict]:
    if not isinstance(journey, dict):
        return []
    scenario = next((item for item in journey.get("scenarios") or []
                     if isinstance(item, dict) and item.get("id") == journey.get("primary_scenario")), {})
    return [step for step in scenario.get("steps") or [] if isinstance(step, dict)]


def to_be_customer_layer(journey: dict | None) -> list[dict]:
    """to-be-blueprint-generation: the customer layer a To-be blueprint must
    have -- one entry per recorded customer action of each confirmed step, or
    one for the step itself when it records none. Generation builds nodes from
    this list and validation compares against it, so the rule lives here once."""
    layer = []
    for position, step in enumerate(_primary_steps(journey), 1):
        actions = [a for a in step.get("customer_actions") or [] if isinstance(a, dict) and str(a.get("text") or "").strip()]
        entries = actions or [{"text": step.get("label") or "", "evidence_refs": step.get("evidence_refs") or []}]
        for action in entries:
            layer.append({"step_ref": str(step.get("id") or ""), "position": position,
                          "text": str(action.get("text") or "").strip(),
                          "evidence_refs": [str(ref) for ref in action.get("evidence_refs") or []]})
    return layer


def to_be_adopted_solutions(journey: dict | None) -> dict[str, str]:
    """Adopted solution id -> step id, for selected moments of the primary scenario."""
    adopted = {}
    for step in _primary_steps(journey):
        if step.get("develop_solution", True) is not True:
            continue
        for solution_id in step.get("selected_solution_ids") or []:
            adopted[str(solution_id)] = str(step.get("id") or "")
    return adopted


def _is_inferred(node: dict) -> bool:
    return any(isinstance(c, dict) and c.get("source") == "inference" for c in node.get("claims") or [])


def _to_be_anchor_analysis(graph: dict, journey: dict | None) -> dict:
    """Which inferred To-be nodes reach an approved root (an adopted, unflagged
    solution or a confirmed step) through their anchors. Service-node anchors
    only pass a root on; a chain of inferred nodes alone, or a cycle, reaches
    none. Shared by `to_be_errors` and the support/planned anchoring rules."""
    nodes = [n for n in graph.get("nodes") or [] if isinstance(n, dict) and n.get("layer") != "顧客行動"]
    by_id = {n.get("id"): n for n in nodes if n.get("id")}
    step_ids = {str(step.get("id")) for step in _primary_steps(journey) if step.get("id")}
    adopted = to_be_adopted_solutions(journey)
    flagged = {str(f.get("solution_ref")) for f in graph.get("topology_flags") or [] if isinstance(f, dict)}

    def anchor_problem(anchor: dict) -> str | None:
        kind, ref = anchor.get("kind"), str(anchor.get("ref") or "")
        if kind == "selected_solution":
            if ref not in adopted:
                return f"指到不是研究者採用的解方 {ref}"
            if ref in flagged:
                return f"指到已標記「需要調整顧客走法」的解方 {ref}，這個解方不展開"
        elif kind == "journey_step":
            if ref not in step_ids:
                return f"指到不存在的步驟 {ref}"
        elif kind == "service_node":
            target = by_id.get(ref)
            if not target or target.get("future_state") not in ("changed", "added"):
                return f"指到的上游節點 {ref} 不是改變或新增的服務機制"
        else:
            return f"錨點種類不明：{kind}"
        return None

    anchored = {n.get("id"): [a for a in n.get("anchors") or [] if isinstance(a, dict)] for n in nodes
                if n.get("future_state") in ("changed", "added") and _is_inferred(n)}
    problems = {}
    for nid, anchors in anchored.items():
        found = [p for p in (anchor_problem(a) for a in anchors) if p]
        if found:
            problems[nid] = found
    rooted = {nid for nid, anchors in anchored.items()
              if any(a.get("kind") in ("selected_solution", "journey_step") and anchor_problem(a) is None
                     for a in anchors)}
    grew = True
    while grew:
        grew = False
        for nid, anchors in anchored.items():
            if nid not in rooted and any(a.get("kind") == "service_node" and str(a.get("ref")) in rooted
                                         for a in anchors):
                rooted.add(nid)
                grew = True
    unrooted = sorted(str(nid) for nid, anchors in anchored.items() if anchors and nid not in rooted)
    # to-be-solution-carry-through: the adopted Solutions each rooted node
    # carries -- its own valid Solution anchors plus those of the rooted
    # service nodes it anchors on. A step anchor carries no Solution.
    solutions = {nid: {str(a.get("ref")) for a in anchored[nid]
                       if a.get("kind") == "selected_solution" and anchor_problem(a) is None}
                 for nid in rooted}
    grew = True
    while grew:
        grew = False
        for nid in rooted:
            for anchor in anchored[nid]:
                upstream = solutions.get(str(anchor.get("ref"))) if anchor.get("kind") == "service_node" else None
                if upstream and not upstream <= solutions[nid]:
                    solutions[nid] |= upstream
                    grew = True
    return {"rooted": rooted, "problems": problems, "unrooted": unrooted, "solutions": solutions}


def to_be_rooted_ids(graph: dict, journey: dict | None) -> set:
    """Inferred nodes of a To-be graph that reach an approved root; empty for
    any other graph."""
    if not isinstance(graph, dict) or (graph.get("meta") or {}).get("blueprint_method") != TO_BE_METHOD:
        return set()
    return _to_be_anchor_analysis(graph, journey)["rooted"]


def to_be_solution_coverage(graph: dict, journey: dict | None) -> dict[str, str]:
    """to-be-solution-carry-through: whether each adopted Solution reached the
    To-be blueprint -- `carried` (a rooted changed/added node's anchor chain
    reaches it), `topology_flagged`, or `not_carried`. A display state for the
    researcher, never a validation error: an error would retry generation and
    push the model to force a node in."""
    if not isinstance(graph, dict) or (graph.get("meta") or {}).get("blueprint_method") != TO_BE_METHOD:
        return {}
    flagged = {str(f.get("solution_ref")) for f in graph.get("topology_flags") or [] if isinstance(f, dict)}
    carried = set().union(*_to_be_anchor_analysis(graph, journey)["solutions"].values())
    return {solution_id: "topology_flagged" if solution_id in flagged
            else "carried" if solution_id in carried else "not_carried"
            for solution_id in to_be_adopted_solutions(journey)}


def to_be_errors(graph: dict, journey: dict | None) -> list[str]:
    """to-be-blueprint-generation: the To-be rules. Only a graph that records
    `blueprint_method: to_be_v1` is checked; any other graph keeps the rules it
    was built under."""
    if not isinstance(graph, dict) or (graph.get("meta") or {}).get("blueprint_method") != TO_BE_METHOD:
        return []
    errors: list[str] = []
    nodes = [n for n in graph.get("nodes") or [] if isinstance(n, dict)]
    step_ids = {str(step.get("id")) for step in _primary_steps(journey) if step.get("id")}
    adopted = to_be_adopted_solutions(journey)
    flags = [f for f in graph.get("topology_flags") or [] if isinstance(f, dict)]
    for flag in flags:
        if str(flag.get("solution_ref")) not in adopted:
            errors.append(f"[to-be] 拓撲標記指到不是研究者採用的解方：{flag.get('solution_ref')}")
        if str(flag.get("step_ref")) not in step_ids:
            errors.append(f"[to-be] 拓撲標記指到不存在的步驟：{flag.get('step_ref')}")

    # The customer layer is inherited, never inferred.
    expected = sorted((entry["step_ref"], entry["text"]) for entry in to_be_customer_layer(journey))
    customer = [n for n in nodes if n.get("layer") == "顧客行動"]
    actual = sorted((str(n.get("journey_step_ref") or ""), str(n.get("description") or "").strip()) for n in customer)
    if actual != expected:
        allowed = set(expected)
        extra = [str(n.get("label")) for n in customer
                 if (str(n.get("journey_step_ref") or ""), str(n.get("description") or "").strip()) not in allowed]
        detail = f"；不屬於已確認旅程的顧客節點：{'、'.join(map(str, extra))}" if extra else ""
        errors.append(f"[to-be] 顧客層要與已確認旅程的步驟與顧客行動一一對應，不能另外推{detail}")

    others = [n for n in nodes if n.get("layer") != "顧客行動"]
    for node in others:
        nid, state = node.get("id"), node.get("future_state")
        if state not in TO_BE_FUTURE_STATES:
            errors.append(f"[to-be] {nid} 要標未來狀態：維持現況、改變或新增")
            continue
        if state == "maintained" and _is_inferred(node):
            errors.append(f"[to-be] {nid} 標成維持現況，但它的內容是推論；維持現況要有現況證據")
        if state in ("changed", "added") and _is_inferred(node):
            if not node.get("anchors"):
                errors.append(f"[to-be] {nid} 是推論出的服務機制，要寫它撐住哪個已採用解方、步驟或上游節點")
            if not str(node.get("necessity") or "").strip():
                errors.append(f"[to-be] {nid} 要寫一句必要性理由：沒有它，哪個解方或步驟無法成立")

    roots = _to_be_anchor_analysis(graph, journey)
    for nid, problems in roots["problems"].items():
        errors.extend(f"[to-be] {nid} {problem}" for problem in problems)
    for nid in roots["unrooted"]:
        errors.append(f"[to-be] {nid} 的錨點追不到研究者採用的解方或已確認步驟，只靠其他推論節點支撐")
    errors.extend(to_be_external_reference_errors(graph))
    return errors


EXTERNAL_REFERENCE_ID = re.compile(r"^XR-\d+$")


def to_be_external_reference_errors(graph: dict) -> list[str]:
    """pre-blueprint-implementation-research: an external reference the
    researcher adopted is design reference only. A node may cite one through
    `external_refs`; it is never evidence, never an anchor, and never makes a
    node exempt from the anchor and necessity rules (those run unchanged)."""
    errors: list[str] = []
    adopted = {str(ref.get("id")) for ref in graph.get("external_references") or [] if isinstance(ref, dict)}
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        nid = node.get("id")
        cited = [str(ref) for ref in node.get("external_refs") or []]
        unknown = [ref for ref in cited if ref not in adopted]
        if unknown:
            errors.append(f"[to-be] {nid} 引用了研究者沒有採用的外部參考：{'、'.join(unknown)}")
        if cited and (node.get("layer") == "顧客行動" or node.get("future_state") == "maintained"):
            errors.append(f"[to-be] {nid} 不能引用外部參考：顧客層照抄已確認旅程，維持現況靠的是現況證據")
        for claim in node.get("claims") or []:
            ref = str((claim or {}).get("evidence_ref") or "") if isinstance(claim, dict) else ""
            if EXTERNAL_REFERENCE_ID.match(ref):
                errors.append(f"[to-be] {nid} 把外部參考 {ref} 當成證據；外部參考只能放在 external_refs")
        for anchor in node.get("anchors") or []:
            ref = str((anchor or {}).get("ref") or "") if isinstance(anchor, dict) else ""
            if EXTERNAL_REFERENCE_ID.match(ref):
                errors.append(f"[to-be] {nid} 把外部參考 {ref} 當成錨點；錨點只能是已採用解方、已確認步驟或上游服務節點")
    for relation in graph.get("relations") or []:
        if not isinstance(relation, dict):
            continue
        for ref in relation.get("evidence_refs") or []:
            if EXTERNAL_REFERENCE_ID.match(str(ref)):
                errors.append(f"[to-be] 關係 {relation.get('id')} 把外部參考 {ref} 當成證據")
    return errors


def journey_anchored_support_ids(graph: dict, journey: dict | None = None) -> set[str]:
    """Return support actions with a directly evidenced link to their stage.

    A support process is not a customer-journey card merely because it is a
    useful operational fact.  It belongs in the primary blueprint only when
    the evidence says it enables a provider action in that same stage.
    """
    nodes = {n.get("id"): n for n in (graph.get("nodes") or []) if isinstance(n, dict) and n.get("id")}
    selected_by_position = _selected_solution_ids_by_position(journey)
    to_be_rooted = to_be_rooted_ids(graph, journey)
    anchored = set()
    for rel in graph.get("relations") or []:
        if not isinstance(rel, dict) or rel.get("type") != "supports":
            continue
        source, target = nodes.get(rel.get("from")), nodes.get(rel.get("to"))
        if not source or not target:
            continue
        directly_evidenced = rel.get("source") == "evidence" and bool(rel.get("evidence_refs"))
        selected_inference = (
            rel.get("source") == "inference"
            and rel.get("selected_solution_ref") in selected_by_position.get(source.get("step"), set())
            and source.get("selected_solution_ref") == rel.get("selected_solution_ref")
            and any(claim.get("source") == "inference" for claim in source.get("claims") or [] if isinstance(claim, dict))
        )
        if (
            source.get("layer") == "支援流程"
            and target.get("kind") == "action"
            and target.get("layer") in ("前台行動", "後台行動")
            and source.get("step") == target.get("step")
            and (directly_evidenced or selected_inference
                 or (rel.get("source") == "inference" and source.get("id") in to_be_rooted))
        ):
            anchored.add(source["id"])
    return anchored


def planned_nodes_without_stage_evidence(graph: dict, journey: dict | None = None) -> set[str]:
    """Return status:planned nodes with no evidence-anchored relation tying
    them to another node at their own journey step.

    cjm-experience-and-moment-solutions: a planned node that realises a
    Solution the researcher adopted at that same Journey step is anchored by
    that adoption (`selected_solution_ref`), not by a quote -- a designed
    intervention has no participant evidence of itself. The real E2E run
    failed five times on exactly this. Plans the evidence mentions keep the
    evidence-relation rule.

    A planned node that exists in the graph at all is, by definition, not
    the kind of ungrounded operational fact structure.md tells the model to
    leave as unlinked evidence (distinguish-planned-from-observed-actions) --
    unlike journey_anchored_support_ids's 支援流程 nodes, there is no
    node-level "this one can skip the check" disposition to fall back to.
    """
    nodes = {n.get("id"): n for n in (graph.get("nodes") or []) if isinstance(n, dict) and n.get("id")}
    selected_by_position = _selected_solution_ids_by_position(journey)
    to_be_rooted = to_be_rooted_ids(graph, journey)
    unanchored = set()
    for node_id, node in nodes.items():
        if node.get("status") != "planned" or node_id in to_be_rooted:
            continue
        if node.get("selected_solution_ref") and \
                node["selected_solution_ref"] in selected_by_position.get(node.get("step"), set()):
            continue
        anchored = False
        for rel in graph.get("relations") or []:
            if not isinstance(rel, dict) or rel.get("source") != "evidence" or not rel.get("evidence_refs"):
                continue
            endpoints = rel.get("endpoints") or [rel.get("from"), rel.get("to")]
            if node_id not in endpoints:
                continue
            for other_id in endpoints:
                if other_id and other_id != node_id and nodes.get(other_id, {}).get("step") == node.get("step"):
                    anchored = True
                    break
            if anchored:
                break
        if not anchored:
            unanchored.add(node_id)
    return unanchored


def action_node_consolidation_warnings(graph):
    """add-action-node-consolidation-criteria：兩個 action node 若屬於同一組
    scenario_refs、同一個 step、同一個 layer、同一個 actor、同一個 channel，
    都沒有掛任何 marker，且 outgoing relation 目標節點集合完全相同且非空，
    代表它們描述的是同一個服務時刻的兩條可互換路徑，不是兩個不同的動作——
    建議合併成一個 node。這是機械、保守的比對（完全比對六個欄位加 outgoing
    目標集合），只抓住最明顯的一類；不成立就不提醒，不代表其他 node 一定沒
    有這個問題。channel 是選填欄位（add-channels-to-journey-map）：兩邊都沒
    填時視為相同，不影響既有 run（沒有 channel 欄位）的比對結果；一填就代表
    「透過不同媒介發生就是不同的節點」，比對時跟其他欄位一視同仁。"""
    if not isinstance(graph, dict):
        return []
    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict) and n.get("kind") == "action"]
    by_id = {n.get("id"): n for n in nodes if n.get("id")}
    marker_types_by_node: dict[str, set] = {}
    for marker in graph.get("markers") or []:
        if isinstance(marker, dict) and marker.get("node_ref"):
            marker_types_by_node.setdefault(marker["node_ref"], set()).add(marker.get("type"))
    outgoing: dict[str, set] = {}
    for rel in graph.get("relations") or []:
        if not isinstance(rel, dict) or rel.get("type") == "same_event":
            continue
        src, dst = rel.get("from"), rel.get("to")
        if src in by_id and dst:
            outgoing.setdefault(src, set()).add(dst)

    def shape(n):
        # add-channels-to-journey-map: two candidates through different
        # channels are different service moments even if everything else
        # matches -- a paper ticket and a QR scan reach the same queue but
        # happen through different media, so channel joins the match tuple.
        return (
            tuple(sorted(n.get("scenario_refs") or [])), n.get("step"), n.get("layer"), n.get("actor"),
            n.get("channel"),
        )

    warnings, seen_pairs, ids = [], set(), sorted(by_id)
    for i, a_id in enumerate(ids):
        a_targets = outgoing.get(a_id) or set()
        if not a_targets or marker_types_by_node.get(a_id):
            continue
        for b_id in ids[i + 1 :]:
            b_targets = outgoing.get(b_id) or set()
            if not b_targets or marker_types_by_node.get(b_id):
                continue
            if shape(by_id[a_id]) != shape(by_id[b_id]) or a_targets != b_targets:
                continue
            pair = (a_id, b_id)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            targets = "、".join(sorted(a_targets))
            _emit_judgment(
                warnings,
                "same_moment_two_paths",
                f"{a_id} 與 {b_id} 在同一條服務路徑、同一步、同一層、同一個行動者身上，"
                f"兩邊都沒有標記，而且後面都接到 {targets}——這是不是同一個服務時刻的"
                f"兩條可互換做法，應該合成一張卡片？",
            )
    return warnings


# define-service-design-judgment-criteria：這五個名字是這份 validator 對外宣告
# 「我會做哪些判斷檢查」。判準來源檔（schema/service-design-judgment.criteria.yaml）
# 必須剛好授權這五個，多一個少一個都在載入時就報錯——validator 檢查一條沒人告訴
# 模型的規則，是 LEARNINGS §8 記了三次的同一個病。

def _checks_bound_to(stage: str) -> set[str]:
    """The machine checks the criteria source binds to a stage (lazy import:
    see `_emit_judgment`)."""
    from sbp_gen import judgment_criteria as JC

    return {criterion.check for criterion in JC.load_default().for_stage(stage) if criterion.check}


def _emit_judgment(warnings: list, check: str, detail: str) -> None:
    """Append a judgment finding, labelled by the criterion authorising the check.

    Imported lazily: `judgment_criteria` reads `SCHEMA_DIR` and
    `JUDGMENT_CHECKS` from this module, so a top-level import here would be a
    cycle. The label is never written at the call site — a hand-typed label is
    a second copy of the criterion's name, which is the class of duplication
    the criteria source exists to remove.

    Withdrawing a rule means deleting its entry from the criteria source *and*
    its name from `JUDGMENT_CHECKS`; both are data, and neither is prompt prose
    or check logic. Doing only the first raises at load time rather than
    quietly disabling the rule. Doing both is what this guard recognises: the
    check no longer has anything authorising it, so it produces nothing, and
    the same deletion has already stopped it reaching the model.
    """
    if check not in JUDGMENT_CHECKS:
        return
    from sbp_gen import judgment_criteria as JC

    warnings.append(JC.finding(check, detail))



# ── 服務設計五原則的機器可判部分 ────────────────────────────────────────
# Lee & Hsien-Hui (2022) 的五原則裡，四條有可以靠讀檔案就settle的部分，一條沒有。
# 每一條的判準敘述、出處、誰裁定，都寫在判準來源檔，不寫在這裡；這裡只有機制。
# 這四條的收窄程度是量出來的，不是猜的：見這個 change 的 tasks.md 4.1。

def principle_continuity(graph, **_):
    """連續：歷程中間有一步完全沒有節點，圖就在那裡斷了而且沒說。"""
    steps = sorted({n.get("step") for n in (graph.get("nodes") or [])
                    if isinstance(n, dict) and isinstance(n.get("step"), int)})
    if not steps:
        return []
    return [f"第 {s} 步在這張圖上完全沒有節點，歷程在這裡斷開，但畫面上沒有任何說明。"
            for s in range(min(steps), max(steps) + 1) if s not in steps]


def principle_stakeholder_centred(graph, **_):
    """利害關係人中心：某一步只有服務方，沒有顧客，等於從內部往外描述。"""
    by_step = {}
    for n in graph.get("nodes") or []:
        if isinstance(n, dict):
            by_step.setdefault(n.get("step"), set()).add(n.get("layer"))
    out = []
    for step, layers in sorted(by_step.items(), key=lambda kv: (kv[0] is None, kv[0])):
        if "顧客行動" not in layers and layers - {"實體證據"}:
            out.append(f"第 {step} 步只有服務方的動作，顧客那一層是空的——"
                       f"這一步是從服務內部描述的，沒有講顧客在做什麼。")
    return out


def principle_whole_system(graph, **_):
    """整體觀：完全沒有可見線以下的內容，代表根本沒往後台看。"""
    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict)]
    # 一張什麼都沒有的圖不是「只畫了一半」，是還沒開始畫。對空圖說它偏向顧客側，
    # 是拿一句聽起來像判斷的話去描述一個沒有內容的檔案。
    if not nodes:
        return []
    if {n.get("layer") for n in nodes} & {"後台行動", "支援流程"}:
        return []
    return ["這張圖完全沒有後台或支援流程的內容，只畫了顧客看得到的那一半。"]


def principle_real(graph, **_):
    """真實：一個節點的每一則 claim 都沒有出處，它就不是從資料裡長出來的。"""
    out = []
    for n in graph.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        claims = n.get("claims") or []
        if claims and not any(isinstance(c, dict) and c.get("evidence_ref") for c in claims):
            out.append(f"{n.get('id')} 的每一則說法都沒有引用任何證據。")
    return out


def journey_step_coverage(graph, journey=None, **_):
    """層的完整性檢查：已確認主要情境的每一步，是不是至少有一張卡片。

    只看主要情境自己的節點有沒有 scenario-axis 問題，不看整個 run 有沒有——
    次要情境的編號錯誤不該蓋掉主要情境自己的覆蓋度發現。兩份崇正案例的 run
    都因為次要情境 zhongba-transfer 的節點誤用主軸編號而在整個 run 層級有
    scenario-axis 錯誤，但主要情境自己的節點編號是對的；如果這裡改成看整個
    run 有沒有 scenario-axis 錯誤，這條檢查就永遠不會在這兩份 run 上發動，
    而它們正是這個 change 的動機案例（declare-analysis-chain-and-journey-
    coverage design.md「Failure modes」）。
    """
    if not isinstance(journey, dict):
        return []
    scenarios = {s.get("id"): s for s in journey.get("scenarios") or [] if isinstance(s, dict)}
    primary_id = journey.get("primary_scenario")
    primary = scenarios.get(primary_id)
    if not primary or primary.get("status") != "confirmed":
        return []
    steps = primary.get("steps") or []
    if not steps:
        return []
    nodes = [n for n in (graph or {}).get("nodes") or [] if isinstance(n, dict)]
    primary_nodes = [n for n in nodes if primary_id in (n.get("scenario_refs") or [])]
    if scenario_axis_errors({"nodes": primary_nodes}, journey):
        return []
    used = {n.get("step") for n in primary_nodes if isinstance(n.get("step"), int)}
    undrawn = [(i, (steps[i - 1] or {}).get("label") or f"第 {i} 步")
               for i in range(1, len(steps) + 1) if i not in used]
    if not undrawn:
        return []
    labels = "、".join(label for _, label in undrawn)
    return [f"已確認的 {len(steps)} 步旅程裡，有 {len(undrawn)} 步完全沒有卡片：{labels}。"]


PRINCIPLE_CHECKS = {
    "principle_continuity": principle_continuity,
    "principle_stakeholder_centred": principle_stakeholder_centred,
    "principle_whole_system": principle_whole_system,
    "principle_real": principle_real,
    "journey_step_coverage": journey_step_coverage,
}

# ── 聚：主軸編碼輸出的判準（add-axial-selective-coding）───────────────────
# 這三條檢查不吃 graph——它們的輸入是 axial_coding.yaml 的 categories 文件，
# 不屬於 PRINCIPLE_CHECKS 那組（那組全部假設輸入是 service_graph），所以另外
# 用 axial_coding_warnings() 這個 orchestrator 收攏，不硬塞進 PRINCIPLE_CHECKS
# 的迴圈（那個迴圈只傳 graph／journey 兩個參數，這裡還需要 evidence）。

# 重疊係數門檻：|A∩B| / min(|A|,|B|)，用 min 而不是聯集，因為一個小類別
# 整個被大類別包住時，用聯集算的 Jaccard 會被大類別的體積稀釋掉。
# 0.6 是手造四組合成資料定出來的暫定值，還沒有真實 axial_coding.yaml 可以
# 驗證——見 add-axial-selective-coding tasks.md 4.1，task 7.2 要回頭校正。
DIMENSION_OVERLAP_THRESHOLD = 0.6


def _dimension_independence_pairs(items):
    """propose-persona-dimension-candidates：`dimension_independence` 的共用
    核心——吃任何一份 `{label, evidence_refs}` 清單(類別或候選軸皆可)，兩兩
    比對證據重疊比例，回傳超過門檻的 (a, b, 重疊比例) 配對。message 措辭留給
    呼叫端自己組，因為類別跟候選軸這兩種呼叫端要講的名詞不一樣（「類別」
    vs 候選軸自己的說法）。"""
    out = []
    for i, a in enumerate(items):
        a_refs = set(a.get("evidence_refs") or [])
        if not a_refs:
            continue
        for b in items[i + 1:]:
            b_refs = set(b.get("evidence_refs") or [])
            if not b_refs:
                continue
            coeff = len(a_refs & b_refs) / min(len(a_refs), len(b_refs))
            if coeff >= DIMENSION_OVERLAP_THRESHOLD:
                out.append((a, b, coeff))
    return out


def _single_role_items(items, evidence):
    """propose-persona-dimension-candidates：`distribution_required` 的共用
    核心——吃任何一份 `{label, evidence_refs}` 清單，回傳證據全部來自同一個
    role 的項目跟那個 role，(item, role) 配對。message 措辭留給呼叫端自己組。"""
    role_by_evidence = {
        e.get("id"): e.get("role") for e in (evidence or []) if isinstance(e, dict) and e.get("id")
    }
    out = []
    for item in items:
        refs = item.get("evidence_refs") or []
        roles = {role_by_evidence[r] for r in refs if role_by_evidence.get(r)}
        if len(roles) == 1:
            out.append((item, next(iter(roles))))
    return out


def dimension_independence(categories_doc, **_):
    """聚：兩個類別是不是同一個構念換句話說——證據重疊比例超過門檻就提醒，
    不斷言一定要合併，那是研究者的判斷。"""
    if not isinstance(categories_doc, dict):
        return []
    cats = [c for c in categories_doc.get("categories") or [] if isinstance(c, dict)]
    return [
        f"類別「{a.get('label')}」與「{b.get('label')}」引用的證據有 "
        f"{coeff:.0%} 重疊，這是不是同一個類別換句話說？"
        for a, b, coeff in _dimension_independence_pairs(cats)
    ]


def distribution_required(categories_doc, evidence=None, **_):
    """聚：這個類別是不是只靠一個人撐起來的，不代表這樣不行，是要讓研究者
    分得出「一個人的強烈說法」跟「好幾個人都這樣講」。"""
    if not isinstance(categories_doc, dict):
        return []
    cats = [c for c in categories_doc.get("categories") or [] if isinstance(c, dict)]
    return [
        f"類別「{item.get('label')}」的證據全部來自「{role}」一人，"
        f"這是單一強烈說法，還是好幾個人都這樣講？"
        for item, role in _single_role_items(cats, evidence)
    ]


def _category_over_generalized_scope(category, evidence_texts):
    """聚：跟 structure 那條 over-generalized-scope 同一條規則、同一個
    criteria 條目，但類別沒有 claim 的 `scope`／`inferred` 欄位可以判斷——
    改成直接看 discriminating_basis 本文有沒有全稱語氣，有的話要求引用的
    證據原文裡至少有一句也用了全稱語氣，否則就是把部分證據講成了全部。"""
    basis = category.get("discriminating_basis") or ""
    if not any(q in basis for q in UNIVERSAL_QUANTIFIERS):
        return None
    refs = category.get("evidence_refs") or []
    texts = [evidence_texts.get(r, "") for r in refs]
    if any(any(q in t for q in UNIVERSAL_QUANTIFIERS) for t in texts if t):
        return None
    return (
        f"類別「{category.get('label')}」的區辨依據用了「都／每／通常」這類全稱語氣，"
        f"但引用的證據原文沒有一句也是全稱語氣。這個類別可能只成立於部分證據，不是全部。"
    )


def axial_coding_warnings(categories_doc: dict, evidence: list) -> list:
    """聚的三條判準的入口：dimension-independence、distribution-required、
    以及重新綁定到這個 stage 的 over-generalized-scope。"""
    warnings: list = []
    if not isinstance(categories_doc, dict):
        return warnings
    for detail in dimension_independence(categories_doc):
        _emit_judgment(warnings, "dimension_independence", detail)
    for detail in distribution_required(categories_doc, evidence=evidence):
        _emit_judgment(warnings, "distribution_required", detail)
    evidence_texts = {e.get("id"): e.get("text", "") for e in (evidence or []) if isinstance(e, dict)}
    for category in categories_doc.get("categories") or []:
        if not isinstance(category, dict):
            continue
        detail = _category_over_generalized_scope(category, evidence_texts)
        if detail:
            _emit_judgment(warnings, "over_generalized_scope", detail)
    return warnings


def persona_dimension_candidate_warnings(candidates_doc: dict, categories_doc: dict, evidence: list) -> list:
    """propose-persona-dimension-candidates：候選階段就能機械檢查的兩條
    篩選標準——Independence（候選軸之間是不是在講同一個構念）跟單一來源
    風險（候選軸賴以存在的證據是不是只來自一個人）。兩者都靠候選軸的
    `grounded_in` 追溯回類別的 `evidence_refs` 聯集起來比對，跟
    `dimension_independence`/`distribution_required` 共用同一套核心比對
    邏輯（`_dimension_independence_pairs`/`_single_role_items`），只是這裡
    的「一項」是候選軸，不是類別。非阻斷——只回報,不擋候選提案成功。"""
    warnings: list = []
    if not isinstance(candidates_doc, dict) or not isinstance(categories_doc, dict):
        return warnings
    evidence_refs_by_category = {
        c.get("id"): set(c.get("evidence_refs") or [])
        for c in (categories_doc.get("categories") or []) if isinstance(c, dict) and c.get("id")
    }
    items = []
    for candidate in candidates_doc.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        for axis in candidate.get("axes") or []:
            if not isinstance(axis, dict):
                continue
            refs: set = set()
            for cat_id in axis.get("grounded_in") or []:
                refs |= evidence_refs_by_category.get(cat_id, set())
            items.append({"label": axis.get("label"), "evidence_refs": sorted(refs)})
    for a, b, coeff in _dimension_independence_pairs(items):
        warnings.append(
            f"候選軸「{a.get('label')}」與「{b.get('label')}」透過所屬類別追溯到的證據有 "
            f"{coeff:.0%} 重疊，這是不是同一個構念換句話說？"
        )
    for item, role in _single_role_items(items, evidence):
        warnings.append(
            f"候選軸「{item.get('label')}」透過所屬類別追溯到的證據全部來自「{role}」一人，"
            f"這是單一強烈說法，還是好幾個人都這樣講？"
        )
    return warnings


def primary_job_candidate_warnings(candidates_doc: dict, categories_doc: dict, evidence: list) -> list:
    """propose-primary-job-candidates：候選 Job 之間的 Independence 檢查——
    3 個候選是不是有 2 個透過 `grounded_in` 追溯回類別的證據重疊,代表其實在
    講同一個 Job 只是換句話說。直接重用
    `propose-persona-dimension-candidates` 已經建好的共用核心
    `_dimension_independence_pairs`,不另外寫比對邏輯。不做單一來源檢查——
    那條規則問的是「這個類別是不是只靠一個人撐起來」,對候選 Job 框架好不好
    不是同一個問題（design.md「Reuse `_dimension_independence_pairs`
    unmodified; no single-source check」）。非阻斷——只回報,不擋候選提案
    成功。"""
    warnings: list = []
    if not isinstance(candidates_doc, dict) or not isinstance(categories_doc, dict):
        return warnings
    evidence_refs_by_category = {
        c.get("id"): set(c.get("evidence_refs") or [])
        for c in (categories_doc.get("categories") or []) if isinstance(c, dict) and c.get("id")
    }
    items = []
    for i, candidate in enumerate(candidates_doc.get("candidates") or [], 1):
        if not isinstance(candidate, dict):
            continue
        refs: set = set()
        for cat_id in candidate.get("grounded_in") or []:
            refs |= evidence_refs_by_category.get(cat_id, set())
        items.append({"label": f"候選 {i}", "evidence_refs": sorted(refs)})
    for a, b, coeff in _dimension_independence_pairs(items):
        warnings.append(
            f"「{a.get('label')}」與「{b.get('label')}」透過所屬類別追溯到的證據有 "
            f"{coeff:.0%} 重疊，這是不是同一則 Job 換句話說？"
        )
    return warnings


# ── 聚：Persona Matrix 輸出的判準（add-persona-matrix）──────────────────────
# 這兩條吃的是 persona_matrix.yaml 文件，不是 graph、不是 categories 文件，
# 所以也另外用 persona_matrix_warnings() 收攏，不進 PRINCIPLE_CHECKS 迴圈。

_SOURCE_NAME_DELIMS = re.compile(r"[、，]")


def _source_name_tokens(source: str) -> list[str]:
    """A Persona Matrix subject's identity comes from `evidence.yaml`'s
    `source` strings, which follow this project's own filename convention:
    `<檔名> / <案名><＿ 或 _><角色1>[、或，<角色2>...]`. Take the role segment
    after the last "/", strip the case-name prefix up to and including the
    first "＿"/"_", then split on "、"/"，" -- the two delimiters
    persona_matrix.md's own counter-example ("崇正＿林督導、吳督導") uses.
    A source with no delimiter (e.g. "崇正＿林督導前三名") yields exactly one
    token, unsplit -- this is deliberate (see subject_identity_overlap_
    across_subjects's substring-containment check, which is what actually
    catches that shape)."""
    if not isinstance(source, str):
        return []
    role_segment = source.rsplit("/", 1)[-1].strip()
    for sep in ("＿", "_"):
        if sep in role_segment:
            role_segment = role_segment.split(sep, 1)[1]
            break
    tokens = [t.strip() for t in _SOURCE_NAME_DELIMS.split(role_segment) if t.strip()]
    return tokens


def _subject_source_tokens(persona_doc: dict, evidence_doc: list) -> dict[str, tuple[list[str], list[str]]]:
    """subject id -> (name tokens across all its evidence, the verbatim
    source strings those tokens came from) -- shared by both new checks so
    neither re-derives the evidence_refs -> source resolution twice."""
    evidence_by_id = {
        e.get("id"): e for e in (evidence_doc or []) if isinstance(e, dict) and e.get("id")
    }
    out: dict[str, tuple[list[str], list[str]]] = {}
    for subject in (persona_doc.get("subjects") or []):
        if not isinstance(subject, dict) or not subject.get("id"):
            continue
        tokens: list[str] = []
        sources: list[str] = []
        for ref in subject.get("evidence_refs") or []:
            evidence = evidence_by_id.get(ref)
            source = evidence.get("source") if isinstance(evidence, dict) else None
            if not isinstance(source, str):
                continue
            if source not in sources:
                sources.append(source)
            for token in _source_name_tokens(source):
                if token not in tokens:
                    tokens.append(token)
        out[subject["id"]] = (tokens, sources)
    return out


def subject_source_names_multiple_people(persona_doc, evidence_doc=None, **_):
    """persona_matrix.md:21-28 already warns the model that one source
    string can mix two speakers ("崇正＿林督導、吳督導" is its own worked
    counter-example) -- that instruction has no code behind it until this
    check. Flags a subject whose own evidence resolves to 2+ distinct name
    tokens, i.e. its own source data already names more than one person."""
    if not isinstance(persona_doc, dict):
        return []
    subject_tokens = _subject_source_tokens(persona_doc, evidence_doc or [])
    out = []
    for subject_id, (tokens, sources) in subject_tokens.items():
        if len(tokens) >= 2:
            quoted = "、".join(f"「{s}」" for s in sources)
            # 「受訪者「{id}」」——跟 subject_unplaced_on_every_axis 同一種引號
            # 慣例，`sbp-gen persona-matrix --show` 的逐受訪者篩選（`__main__.py`
            # 用 `受訪者「{subject_id}」" in f` 字串比對）才找得到這則發現。
            out.append(
                f"受訪者「{subject_id}」的證據來源本身就提到不只一個人（{'、'.join(tokens)}），"
                f"來源：{quoted}——請確認這是不是把兩個人的說法合併成了一個受訪者。"
            )
    return out


def subject_identity_overlap_across_subjects(persona_doc, evidence_doc=None, **_):
    """The subtler half of the same failure: two DIFFERENT subjects whose
    source strings are not identical but name the same person anyway (real
    case: P-3's source names 林督導 via a delimiter-split token; P-4's two
    sources are each an un-delimited "林督導前三名"/"林督導報告" -- exact-token
    matching would miss this pair entirely, so this checks substring
    containment instead, bounded to tokens of 2+ characters to keep a bare
    single-character surname from matching arbitrary role text)."""
    if not isinstance(persona_doc, dict):
        return []
    subject_tokens = _subject_source_tokens(persona_doc, evidence_doc or [])
    subject_ids = sorted(subject_tokens)
    out = []
    for i, subject_a in enumerate(subject_ids):
        tokens_a, sources_a = subject_tokens[subject_a]
        for subject_b in subject_ids[i + 1:]:
            tokens_b, sources_b = subject_tokens[subject_b]
            shared = None
            for token_a in tokens_a:
                if len(token_a) < 2:
                    continue
                for token_b in tokens_b:
                    if len(token_b) < 2:
                        continue
                    if token_a in token_b or token_b in token_a:
                        shared = token_a if len(token_a) <= len(token_b) else token_b
                        break
                if shared:
                    break
            if shared:
                quoted = "、".join(f"「{s}」" for s in sources_a + sources_b)
                # 「受訪者「{a}」」跟「受訪者「{b}」」都各自出現一次，這則發現
                # 才會同時被 `--show` 的兩個受訪者區塊都撈到——這是一則橫跨兩個
                # 受訪者的發現，不該只掛在其中一個底下。
                out.append(
                    f"受訪者「{subject_a}」跟受訪者「{subject_b}」的證據來源都提到「{shared}」，"
                    f"來源：{quoted}——請確認這是同一個人被拆成兩個受訪者，還是巧合。"
                )
    return out


def axis_not_grounded(persona_doc, category_ids, **_):
    """一條軸如果引用了主軸編碼裡不存在的類別 id，代表它沒有真的從任何
    類別讀出來——這是referential-integrity層級的問題，不是要不要合併的疑問。"""
    if not isinstance(persona_doc, dict):
        return []
    out = []
    for axis in persona_doc.get("axes") or []:
        if not isinstance(axis, dict):
            continue
        missing = [c for c in (axis.get("grounded_in") or []) if c not in category_ids]
        if missing:
            out.append(
                f"軸「{axis.get('id')}」的 grounded_in 引用了不存在的類別：{'、'.join(missing)}，"
                f"這條軸沒有真的對應到任何主軸編碼類別。"
            )
    return out


def subject_unplaced_on_every_axis(persona_doc, **_):
    """一個受訪者如果在每一條軸上都沒有真實落點，代表這個受訪者除了
    evidence_refs 分組之外沒有多提供任何區辨資訊——不代表這樣不行，
    是要讓研究者知道這個受訪者可能被分錯了規模，或這批軸對他不適用。

    require-complete-persona-matrix-placements：填滿要求上線之後，
    `placements` 不會再是空陣列——每個受訪者每條軸都會有一筆記錄，真實落點
    或 `no_data`。原本「陣列是空的」這個觸發條件永遠不會再發生，所以改成
    「每一筆都是 no_data」，抓的是同一件事：這個受訪者沒有貢獻任何真實
    區辨資訊。空陣列（不該再出現，但防禦性地）視為全部 no_data 的極端情況，
    一樣算命中。"""
    if not isinstance(persona_doc, dict):
        return []
    out = []
    for subject in persona_doc.get("subjects") or []:
        if not isinstance(subject, dict):
            continue
        placements = subject.get("placements") or []
        # all() over an empty iterable is True -- an empty list is vacuously
        # "every placement is no_data," matching the pre-change behavior for
        # that input shape (an empty array was flagged before this change too).
        if all(isinstance(p, dict) and p.get("position") == "no_data" for p in placements):
            out.append(f"受訪者「{subject.get('id')}」在每一條軸上都沒有落點，這個分組是不是太細或太籠統？")
    return out


def axis_without_placement_spread(persona_doc, **_):
    """`distribution-required` 這條通則宣告的是「一條用來分群的軸，受訪者落點
    必須有分布」——講的是**軸的落點**。判準檔裡掛在同一個 id 底下的既有規則查的是
    「一個類別的證據是不是全來自同一個 role」，那是類別層級、不是軸層級，兩者
    都有用但不是同一件事（enforce-axis-quality-criteria-in-persona-matrix）。

    **分布跟覆蓋是兩件事，不能混。** 落點有沒有分開，講的是這條軸；有幾位受訪者
    被放上去，講的是落點工作做到哪。兩位同意、另外兩位根本沒被放上去，推不出
    「沒有差別」——沒被放的人可能落在另一端。四種結果分開報：

    - **差異不存在**：全部受訪者都有落點，且全部落在同一端。這才是強訊號——
      每個人都被讀過了就是沒分開，這條軸記的是全樣本共通的性質。
    - **資料不足**：兩位以上有落點且一致，但至少一位受訪者沒被放上來。不下
      「沒有差別」的結論，只講放了幾位。
    - **無從判斷**：不到兩位有落點，沒有東西可以比。
    - **有分布**：落點在兩個以上位置，不報。

    **服務對象是單一群體時，分不出群是合理結果，不是分析失敗**，所以措辭一律
    不暗示那些軸不合格（enforce-axis-quality-criteria-in-persona-matrix）。

    require-complete-persona-matrix-placements：填滿要求上線之後，每個受訪者
    每條軸都會有一筆落點，真實位置或 `no_data`。`no_data` 在這支函式眼裡
    跟「這一格根本沒有記錄」是同一件事——一律排除，不算「有放」，不然填滿
    這個動作本身會把每個人都變成「已放上去」，上面這整套分布／覆蓋的區分
    就失效了。
    """
    if not isinstance(persona_doc, dict):
        return []
    subjects = [s for s in (persona_doc.get("subjects") or []) if isinstance(s, dict)]
    total = len(subjects)
    positions: dict = {}
    for subject in subjects:
        for placement in subject.get("placements") or []:
            if not isinstance(placement, dict):
                continue
            # require-complete-persona-matrix-placements: a no_data placement
            # carries exactly the weight a fully-omitted placement carried
            # before every axis required an explicit entry -- exclude it here
            # so filling every cell does not change what this check concludes.
            if placement.get("position") == "no_data":
                continue
            positions.setdefault(placement.get("axis_id"), []).append(
                (subject.get("id"), placement.get("position")))
    out = []
    for axis in persona_doc.get("axes") or []:
        if not isinstance(axis, dict):
            continue
        placed = positions.get(axis.get("id")) or []
        name = f"軸「{axis.get('id')} {axis.get('label')}」"
        if len(placed) < 2:
            out.append(
                f"{name}只有 {len(placed)} 位受訪者有落點，分不分散無從判斷"
                f"——這不是說這條軸站不住，是還沒有東西可以比。"
            )
            continue
        distinct = {p for _, p in placed}
        if len(distinct) > 1:
            continue
        who = "、".join(s for s, _ in placed)
        position = distinct.pop()
        if len(placed) >= total:
            out.append(
                f"{name}上全部 {total} 位受訪者的落點都集中在 {position}"
                f"——每個人都讀過了就是沒有分開，這條軸記的是全樣本共通的性質，"
                f"不是把人分開的變項。共通的性質仍然可能是這個案子最重要的發現，"
                f"留或刪由你判斷。"
            )
        else:
            out.append(
                f"{name}上有落點的 {len(placed)}／{total} 位受訪者（{who}）都在 {position}，"
                f"但還有 {total - len(placed)} 位沒有被放到這條軸上——"
                f"還不能說這條軸分不開人，沒放上來的人可能落在另一端。"
            )
    return out


def axis_independence_in_persona_matrix(persona_doc, categories_doc, **_):
    """`dimension-independence` 通則的軸層級實作：Persona Matrix 自己的軸
    兩兩比對，透過各自的 `grounded_in` 追溯回主軸編碼類別的 evidence_refs
    聯集起來比對重疊比例，重用 `_dimension_independence_pairs` 跟既有的
    `DIMENSION_OVERLAP_THRESHOLD`，不另立第二套門檻——跟
    `persona_dimension_candidate_warnings` 對候選軸做的是同一件事，只是這裡
    的「一項」是 Persona Matrix 已經產出的軸，不是候選軸
    （enforce-axis-quality-criteria-in-persona-matrix）。只提出疑問，不斷言
    一定要合併，那是研究者的判斷。"""
    if not isinstance(persona_doc, dict) or not isinstance(categories_doc, dict):
        return []
    evidence_refs_by_category = {
        c.get("id"): set(c.get("evidence_refs") or [])
        for c in (categories_doc.get("categories") or []) if isinstance(c, dict) and c.get("id")
    }
    items = []
    for axis in persona_doc.get("axes") or []:
        if not isinstance(axis, dict):
            continue
        refs: set = set()
        for cat_id in axis.get("grounded_in") or []:
            refs |= evidence_refs_by_category.get(cat_id, set())
        items.append({
            "name": f"{axis.get('id')} {axis.get('label')}",
            "evidence_refs": sorted(refs),
        })
    return [
        f"軸「{a.get('name')}」與軸「{b.get('name')}」透過所屬類別追溯到的證據有 "
        f"{coeff:.0%} 重疊，這是不是同一條軸換句話說？"
        for a, b, coeff in _dimension_independence_pairs(items)
    ]


def persona_matrix_warnings(persona_doc: dict, categories_doc: dict, evidence_doc: list | None = None) -> list:
    """Persona Matrix 六條判準的入口：axis-grounded-in-category、
    subject-placement-coverage、distribution-required（軸層級）、
    dimension-independence（軸層級）、subject-source-names-multiple-people、
    subject-identity-overlap-across-subjects。`categories_doc` 是完整的主軸編碼
    文件（`{"categories": [...]}`），不是裸的 id 集合——`axis_not_grounded`
    只需要 id 存在與否，`axis_independence_in_persona_matrix` 還要用到每個
    類別的 evidence_refs 才能追溯重疊比例，所以兩者共用同一份輸入。
    `evidence_doc` 是選填的（預設 `None`，等同沒有證據可查）——只有最後兩條
    受訪者身分判準需要它，既有呼叫端（不知道身分判準存在）不用跟著改。"""
    warnings: list = []
    if not isinstance(persona_doc, dict):
        return warnings
    categories_doc = categories_doc if isinstance(categories_doc, dict) else {}
    category_ids = {
        c.get("id") for c in (categories_doc.get("categories") or [])
        if isinstance(c, dict) and c.get("id")
    }
    for detail in axis_not_grounded(persona_doc, category_ids):
        _emit_judgment(warnings, "axis_not_grounded", detail)
    for detail in subject_unplaced_on_every_axis(persona_doc):
        _emit_judgment(warnings, "subject_unplaced_on_every_axis", detail)
    for detail in axis_without_placement_spread(persona_doc):
        _emit_judgment(warnings, "axis_without_placement_spread", detail)
    for detail in axis_independence_in_persona_matrix(persona_doc, categories_doc):
        _emit_judgment(warnings, "axis_independence_in_persona_matrix", detail)
    for detail in subject_source_names_multiple_people(persona_doc, evidence_doc):
        _emit_judgment(warnings, "subject_source_names_multiple_people", detail)
    for detail in subject_identity_overlap_across_subjects(persona_doc, evidence_doc):
        _emit_judgment(warnings, "subject_identity_overlap_across_subjects", detail)
    return warnings


# persona-nine-canonical-fields (2026-10-01): the canonical content fields,
# followed by the older keys a Persona written before that date may carry.
CONTENT_FIELDS = ("context", "motivation_goal", "decision_pattern", "behavior_strategy", "challenge", "need",
                  "motivation", "behavior")
# Evidence-grounded human-understanding fields; synthesis, never character writing.
# The four after user_story are COMPATIBILITY_ONLY (retired for new generation).
NARRATIVE_FIELDS = ("user_story", "worldview", "emotional_tendencies", "behavioral_patterns", "linguistic_style")
IDENTITY_FIELDS = ("person_name", "age", "avatar", "profession", "income", "household_structure")
PROVENANCE_FIELDS = CONTENT_FIELDS + NARRATIVE_FIELDS + IDENTITY_FIELDS

_FIELD_PROVENANCE_CRITERION = "persona-field-provenance-policy"
_POLICY_CACHE: dict | None = None


def persona_field_provenance_policy_table(path=None) -> dict:
    """The field-to-permitted-types table, read from the one file that also
    supplies the sentence the model is sent.

    evidence-aware-persona-provenance: read here rather than imported from
    `sbp_gen.judgment_criteria` because that module imports this one; reading
    the same YAML is one source with two readers, which is the point, while
    restating the rows in Python would be the second authoring this criteria
    file exists to prevent.
    """
    global _POLICY_CACHE
    if path is None and _POLICY_CACHE is not None:
        return _POLICY_CACHE
    source = path or (SCHEMA_DIR / "service-design-judgment.criteria.yaml")
    with open(source, encoding="utf-8") as handle:
        document = load_safe_yaml(handle) or {}
    entry = ((document.get("knowledge") or {}).get(_FIELD_PROVENANCE_CRITERION) or {})
    policy = entry.get("policy")
    if not isinstance(policy, dict) or not policy:
        raise ValueError(
            f"{_FIELD_PROVENANCE_CRITERION} 沒有 policy 表——欄位政策是這條判準的內容本身，"
            f"缺了它這條規則就停止生效，而檢查仍然會宣稱自己在跑"
        )
    if path is None:
        _POLICY_CACHE = policy
    return policy


def _effective_provenance(persona: dict) -> tuple[list[dict], list[str]]:
    """This persona's provenance records, with the superseded field folded in.

    A document written while `inferred_fields` was the only record reads as
    that many `evidence_inference` entries; the spec keeps such documents
    valid rather than stranding the one real run produced under it.
    """
    records = persona.get("field_provenance")
    legacy = persona.get("inferred_fields")
    if records is not None and legacy:
        return [], [
            f"[persona_content] persona {persona.get('id')} 同時寫了 field_provenance 與已被取代的 "
            f"inferred_fields——兩份來源會對同一個欄位給出不同的型別，沒有規則決定該信哪一個。"
        ]
    if records is not None:
        return [r for r in records if isinstance(r, dict)], []
    return [
        {"field": entry.get("field"), "type": "evidence_inference",
         "reasoning_summary": entry.get("derivation"),
         "evidence_refs": entry.get("evidence_refs")}
        for entry in (legacy or []) if isinstance(entry, dict)
    ], []


def persona_field_provenance_errors(data: object,
                                     evidence_refs_by_subject: dict | None = None,
                                     policy: dict | None = None,
                                     allow_researcher: bool = False) -> list[str]:
    """evidence-aware-persona-provenance: every field carrying text declares
    which of four relationships to the evidence it holds, the field policy
    decides which types that field may declare, and each type owes a
    different proof.

    What this deliberately does NOT check: whether a bridging statement is a
    real reading of the evidence it names, whether something typed as a
    synthesis is actually an interpretation, or whether a value is more
    precise than its evidence supports. None is mechanically decidable; all
    three are named in the prompt and left to the researcher.
    """
    if not isinstance(data, dict):
        return []
    policy = policy if policy is not None else persona_field_provenance_policy_table()
    errors: list[str] = []
    for persona in data.get("personas") or []:
        if not isinstance(persona, dict):
            continue
        pid = persona.get("id")
        records, conflict = _effective_provenance(persona)
        errors.extend(conflict)
        if conflict:
            continue
        member_evidence = None
        if evidence_refs_by_subject is not None:
            member_evidence = {
                ref for sid in persona.get("member_subject_ids") or []
                for ref in evidence_refs_by_subject.get(sid, set())
            }
        seen: set[str] = set()
        for entry in records:
            field, ptype = entry.get("field"), entry.get("type")
            if field in seen:
                errors.append(
                    f"[persona_content] persona {pid} 把「{field}」記了不只一筆來源，一個欄位只能有一筆。"
                )
                continue
            if isinstance(field, str):
                seen.add(field)
            if ptype == "researcher":
                # A researcher's own rewrite on the workbench. Only the edit
                # endpoint may record it; generated content never may.
                if not allow_researcher:
                    errors.append(f"[persona_content] persona {pid} 的「{field}」標成 researcher，但只有研究者在工作台上改寫時才能這樣標。")
                elif field in ("person_name", "avatar"):
                    errors.append(f"[persona_content] persona {pid} 的「{field}」是人物化設定，只能標成 illustrative_identity。")
                elif not str(persona.get(field) or "").strip():
                    errors.append(f"[persona_content] persona {pid} 標了「{field}」的來源，但這個欄位是空的。")
                continue
            allowed = policy.get(field)
            if allowed is None:
                errors.append(f"[persona_content] persona {pid} 標了政策沒有規範的欄位：{field}")
                continue
            if ptype not in allowed:
                errors.append(
                    f"[persona_content] persona {pid} 的「{field}」標成 {ptype}，"
                    f"但這個欄位只能標成 {'、'.join(allowed)}。"
                )
                continue
            if not str(persona.get(field) or "").strip():
                errors.append(
                    f"[persona_content] persona {pid} 標了「{field}」的來源，但這個欄位是空的。"
                )
            refs = entry.get("evidence_refs") or []
            bridge = str(entry.get("reasoning_summary") or "").strip()
            if ptype == "illustrative_identity":
                if entry.get("not_a_research_claim") is not True:
                    errors.append(
                        f"[persona_content] persona {pid} 的「{field}」標成人物化設定，"
                        f"但沒有明講它不是研究發現。"
                    )
                if refs or bridge:
                    errors.append(
                        f"[persona_content] persona {pid} 的「{field}」是人物化設定，不該附證據或推導理由——"
                        f"替一個取名硬生一套理由，讀起來跟真的一樣，比沒有理由更糟。"
                    )
                continue
            if entry.get("not_a_research_claim") is not None:
                errors.append(
                    f"[persona_content] persona {pid} 的「{field}」標成 {ptype}，"
                    f"卻宣告自己不是研究發現——這兩件事不能同時成立。"
                )
            if ptype == "evidence_inference" and not bridge:
                errors.append(
                    f"[persona_content] persona {pid} 的「{field}」是推論，但沒有寫推導橋接——"
                    f"研究者無從針對這一步表示不同意。"
                )
            if ptype in ("evidence_inference", "evidence_synthesis") and not refs:
                errors.append(
                    f"[persona_content] persona {pid} 的「{field}」標成 {ptype}，卻沒有指名任何證據。"
                )
            if member_evidence is not None:
                outside = [ref for ref in refs if ref not in member_evidence]
                if outside:
                    errors.append(
                        f"[persona_content] persona {pid} 的「{field}」引用了不屬於這份 persona "
                        f"任何成員的證據：{'、'.join(outside)}"
                    )
        # 只有「這份文件已經在用新的來源區塊」時才要求每個欄位都標到。完全沒有
        # field_provenance 的是這個 change 之前產出的文件，spec 要求它照常通過、
        # 整份讀成 research_evidence——把它跟「有區塊卻漏標一欄」當成同一種，等於
        # 讓既有產出在升級當下全部變成不合法。
        if persona.get("field_provenance") is not None:
            for field in PROVENANCE_FIELDS:
                if str(persona.get(field) or "").strip() and field not in seen:
                    errors.append(
                        f"[persona_content] persona {pid} 的「{field}」有內容卻沒有標明來源——"
                        f"讀的人分不出它是資料、整理、推論還是人物化設定。"
                    )
        quadrant, avatar = persona.get("quadrant_id"), persona.get("avatar")
        if avatar and quadrant:
            expected = f"silhouette-{quadrant.rsplit('-', 1)[-1]}"
            if avatar != expected:
                errors.append(
                    f"[persona_content] persona {pid} 的頭像是 {avatar}，但這一格應該用 {expected}——"
                    f"頭像是依象限決定性地挑的，不是模型選的。"
                )
    return errors


# ── 聚：Persona 內容輸出的判準（add-persona-content）───────────────────
# 吃的是 persona_content.yaml 文件跟同一次 run 的 persona_matrix.yaml 受訪者
# id 集合，不是 graph、不是 categories 文件，所以也另外用
# persona_content_warnings() 收攏，不進 PRINCIPLE_CHECKS 迴圈。跟
# persona-matrix 同一種兩層結構：generation 時擋在 stages.py 的
# `_persona_content_errors`，這裡是審查時可獨立重跑的版本。

def persona_member_not_grounded(persona_content_doc, subject_ids, **_):
    """一個 persona 如果引用了 persona_matrix.yaml 裡不存在的受訪者 id，代表
    它沒有真的從供給的受訪者落點讀出來——這是 referential-integrity 層級的
    問題，不是要不要合併的疑問。"""
    if not isinstance(persona_content_doc, dict):
        return []
    out = []
    for persona in persona_content_doc.get("personas") or []:
        if not isinstance(persona, dict):
            continue
        missing = [s for s in (persona.get("member_subject_ids") or []) if s not in subject_ids]
        if missing:
            out.append(
                f"Persona「{persona.get('id')}」的 member_subject_ids 引用了不存在的受訪者：{'、'.join(missing)}，"
                f"這個 persona 沒有真的對應到 persona_matrix.yaml 裡的任何受訪者。"
            )
    return out


def subject_unassigned_to_any_persona(persona_content_doc, subject_ids, **_):
    """一個受訪者如果沒有出現在任何 persona 的 member_subject_ids 裡，代表這
    個受訪者的分群狀態無法跟「還沒被檢視過」區分——不代表這樣不行，是要讓
    研究者知道這個受訪者可能落點太少或太分散，湊不成有意義的一群。

    generate-persona-for-target-quadrant：`subject_ids` 現在必須是呼叫端先
    篩過的集合——只放落點在兩條選定軸上，跟研究者指定的主要目標象限那個
    兩端組合相符的受訪者（例如 `target_segmentation_candidates.yaml` 那一格
    的 `suggested_subject_ids`），不是這次 run 全部受訪者。一份 run 現在只長
    一份 persona，如果還拿全部受訪者當分母，這個檢查會對絕大多數正常案例
    發出警告（鎖定 run 15 位裡有 11 位在目標象限之外），變成不帶資訊的雜訊；
    這個函式本身的邏輯不變，改變的是呼叫端該傳什麼進來。"""
    if not isinstance(persona_content_doc, dict):
        return []
    assigned = {
        s for persona in (persona_content_doc.get("personas") or []) if isinstance(persona, dict)
        for s in (persona.get("member_subject_ids") or [])
    }
    return [
        f"受訪者「{sid}」符合主要目標象限的落點組合，卻沒有被分進這份 persona。"
        for sid in sorted(subject_ids) if sid not in assigned
    ]


def persona_content_warnings(persona_content_doc: dict, subject_ids: set,
                              matching_subject_ids: set | None = None) -> list:
    """Persona 內容兩條判準的入口：persona-member-grounded-in-subject、
    persona-subject-coverage。

    `subject_ids` 是這次 run 全部受訪者 id 的參照完整性用集合——
    `persona_member_not_grounded` 要靠它判斷一個引用的 id 存不存在。

    `matching_subject_ids` 選填，是縮小過的集合，只放落點符合主要目標象限
    兩端組合的受訪者——`subject_unassigned_to_any_persona` 用它當分母。省略
    時退回用 `subject_ids`（等於沒有縮小），呼叫端應該儘量傳這個縮小過的
    集合，否則覆蓋率檢查會對每一份只長一個象限的 persona 文件發出雜訊。"""
    warnings: list = []
    if not isinstance(persona_content_doc, dict):
        return warnings
    for detail in persona_member_not_grounded(persona_content_doc, subject_ids):
        _emit_judgment(warnings, "persona_member_not_grounded", detail)
    coverage_universe = subject_ids if matching_subject_ids is None else matching_subject_ids
    for detail in subject_unassigned_to_any_persona(persona_content_doc, coverage_universe):
        _emit_judgment(warnings, "subject_unassigned_to_any_persona", detail)
    return warnings


# ── 聚：JTBD 輸出的判準（add-jtbd）───────────────────────────────────────
# 吃的是 jtbd.yaml 文件，不是 graph、不是 categories 文件，所以也另外用
# jtbd_warnings() 收攏，不進 PRINCIPLE_CHECKS 迴圈。只有一條——situation／
# progress／outcome 是不是非空字串、evidence_refs 有沒有至少一筆，schema
# 的 minLength／minItems 已經是結構性必填，不是判準層級的發現；跟
# persona-matrix 湊成兩條同一個數量不是目標，見 design.md「One new judgment
# check, not two」。

def job_not_grounded(jtbd_doc, category_ids, **_):
    """一則 Job 如果引用了主軸編碼裡不存在的類別 id，代表它沒有真的從任何
    類別讀出來——這是 referential-integrity 層級的問題，不是要不要合併的疑問。"""
    if not isinstance(jtbd_doc, dict):
        return []
    out = []
    for job in jtbd_doc.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        missing = [c for c in (job.get("grounded_in") or []) if c not in category_ids]
        if missing:
            out.append(
                f"Job「{job.get('id')}」的 grounded_in 引用了不存在的類別：{'、'.join(missing)}，"
                f"這則 Job 沒有真的對應到任何主軸編碼類別。"
            )
    return out


def jtbd_warnings(jtbd_doc: dict, category_ids: set) -> list:
    """JTBD 唯一一條判準的入口：job-grounded-in-category。"""
    warnings: list = []
    if not isinstance(jtbd_doc, dict):
        return warnings
    for detail in job_not_grounded(jtbd_doc, category_ids):
        _emit_judgment(warnings, "job_not_grounded", detail)
    return warnings


# ── 問：HMW 輸出的判準（add-how-might-we）──────────────────────────────
# 吃的是 hmw.yaml 文件，不是 graph、不是 categories 文件，所以也另外用
# hmw_warnings() 收攏，不進 PRINCIPLE_CHECKS 迴圈。只有一條——理由跟
# jtbd／job_map 一樣：schema 的必填欄位已經是結構性要求，這條抓的是
# grounded_in／evidence_refs 是否真的存在的 referential-integrity 問題。

def hmw_entry_not_grounded(hmw_doc, category_ids, evidence_ids, **_):
    """一則 HMW 如果引用了不存在的類別或證據，代表它沒有真的從供給的資料
    推導出來。"""
    if not isinstance(hmw_doc, dict):
        return []
    out = []
    for entry in hmw_doc.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        entry_id = entry.get("id")
        cat = entry.get("grounded_in")
        if cat not in category_ids:
            out.append(f"HMW「{entry_id}」的 grounded_in 引用了不存在的類別：{cat}。")
        missing = [r for r in (entry.get("evidence_refs") or []) if r not in evidence_ids]
        if missing:
            out.append(f"HMW「{entry_id}」引用了不存在的證據：{'、'.join(missing)}。")
    return out


def hmw_warnings(hmw_doc: dict, category_ids: set, evidence_ids: set) -> list:
    """HMW 唯一一條判準的入口：hmw-entry-grounded。"""
    warnings: list = []
    if not isinstance(hmw_doc, dict):
        return warnings
    for detail in hmw_entry_not_grounded(hmw_doc, category_ids, evidence_ids):
        _emit_judgment(warnings, "hmw_entry_not_grounded", detail)
    return warnings


# ── 聚：Job Map 輸出的判準（add-job-map）─────────────────────────────────
# 吃的是 job_map.yaml 文件（外加 jtbd.yaml 拿每則 Job 自己的證據範圍），不是
# graph、不是 categories 文件，所以也另外用 job_map_warnings() 收攏，不進
# PRINCIPLE_CHECKS 迴圈。只有一條——理由跟 jtbd 的 job-grounded-in-category
# 一樣：schema 的 minLength／minItems 已經是結構性必填，這條抓的是「引用了
# 別則 Job 的證據」這種 schema 表達不了的 referential-integrity 問題。

def step_evidence_outside_job(job_map_doc, jobs_doc, **_):
    """一個階段如果引用了不屬於它自己那則 Job 的證據，代表這個階段不是真的
    從那則 Job 自己的範圍推導出來——可能是抄到別則 Job 的故事，或憑空發明。"""
    if not isinstance(job_map_doc, dict):
        return []
    job_evidence = {
        job["id"]: set(job.get("evidence_refs") or [])
        for job in (jobs_doc.get("jobs") or []) if isinstance(job, dict) and job.get("id")
    }
    out = []
    for entry in job_map_doc.get("job_maps") or []:
        if not isinstance(entry, dict):
            continue
        job_id = entry.get("job_id")
        allowed = job_evidence.get(job_id, set())
        for step in entry.get("steps") or []:
            if not isinstance(step, dict):
                continue
            missing = [r for r in (step.get("evidence_refs") or []) if r not in allowed]
            if missing:
                out.append(
                    f"Job「{job_id}」的階段「{step.get('id')}」引用了不屬於這則 Job 的證據："
                    f"{'、'.join(missing)}，這個階段沒有真的從這則 Job 自己的範圍推導出來。"
                )
    return out


def job_map_warnings(job_map_doc: dict, jobs_doc: dict) -> list:
    """Job Map 唯一一條判準的入口：step-evidence-within-job。"""
    warnings: list = []
    if not isinstance(job_map_doc, dict):
        return warnings
    for detail in step_evidence_outside_job(job_map_doc, jobs_doc):
        _emit_judgment(warnings, "step_evidence_outside_job", detail)
    return warnings


# 過度延伸門檻：pipeline.md §2.3 的 G1b 量到的失敗形態是「把僅有的證據過度延伸
# 去覆蓋沒有證據的格子，並照樣標 inferred: false」——資料齊全時 25%，只剩三分之
# 一時 37%。一條證據撐起三個以上節點就是那個形態的檔案側訊號。門檻 3 是量出來的
# （見 tasks.md 4.2）：六筆真實 run 命中四筆、三筆 fixture 命中零筆。
EVIDENCE_OVER_EXTENSION_THRESHOLD = 3


def evidence_over_extended(graph, **_):
    """這份產出值不值得研究者抽查引用？看有沒有證據被攤太薄。"""
    per_evidence = {}
    for n in graph.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        for ref in {c.get("evidence_ref") for c in (n.get("claims") or [])
                    if isinstance(c, dict) and c.get("evidence_ref")}:
            per_evidence[ref] = per_evidence.get(ref, 0) + 1
    stretched = sorted(r for r, count in per_evidence.items()
                       if count >= EVIDENCE_OVER_EXTENSION_THRESHOLD)
    if not stretched:
        return []
    return [f"有 {len(stretched)} 條證據各自被三個以上的節點引用，"
            f"最多的一條撐起 {max(per_evidence.values())} 個節點。"]



# ── D/F/V 三個透鏡的「什麼時候值得問」──────────────────────────────────
# 這三個是 IDEO 設計思考透鏡，通用文獻，不在本專案語料裡逐字出現，所以它們只能
# 問研究者、不能自己下結論，也不會送給模型。這裡的函式不判斷答案，只判斷「這份
# 產出值不值得拿這個透鏡看」——一個每份產出都問的問題是雜訊，不是判斷。
# 三個門檻都是量出來的，見這個 change 的 tasks.md 4.3。

DESIRABILITY_FRICTION_THRESHOLD = 2


def customer_friction_concentrated(graph, **_):
    """Desirability：顧客側的摩擦多到值得回頭問「這條路徑值得走嗎」。"""
    friction = [m for m in (graph.get("markers") or [])
                if isinstance(m, dict) and m.get("type") in ("失誤點", "等候點", "放棄點")]
    if len(friction) < DESIRABILITY_FRICTION_THRESHOLD:
        return []
    return [f"顧客在這條歷程上有 {len(friction)} 個出錯或等待的地方。"]


def provider_work_outweighs_customer_steps(graph, **_):
    """Feasibility：服務方要做的事比顧客經歷的步驟還多。"""
    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict)]
    customer = sum(1 for n in nodes if n.get("layer") == "顧客行動")
    provider = sum(1 for n in nodes if n.get("layer") in ("後台行動", "支援流程"))
    if not customer or provider < customer:
        return []
    return [f"顧客走 {customer} 步，服務方要做 {provider} 件事來支撐它。"]


def support_processes_carry_ongoing_cost(graph, **_):
    """Viability：圖上出現了必須一直運轉下去的支援流程。"""
    support = [n for n in (graph.get("nodes") or [])
               if isinstance(n, dict) and n.get("layer") == "支援流程"]
    if not support:
        return []
    return [f"這張圖有 {len(support)} 個支援流程要一直運轉，服務才成立。"]


# ── detect-job-map-opportunity-gaps：Job Map（應然）跟結構圖（實然）的比對 ──
# 這條也是「值不值得問」的信號，不是判準——只查「這個階段的證據有沒有在圖上
# 出現過」，純參照比對，不查內容是否真的對應，也不分辨「完全沒出現」（缺席）
# 跟「出現了但被標成失誤點／等候點」（故障，屬於既有的標記機制）。找到的是
# 候選機會，研究者才是裁定的人。

def job_map_step_unaddressed(graph, job_map=None, **_):
    """一個 Job Map 階段的證據如果在整張圖裡完全沒被任何節點的 claim 引用過，
    代表目前的服務完全沒有碰到這個階段——值得研究者看一眼，不是系統下結論。"""
    if not isinstance(job_map, dict):
        return []
    graph_evidence = {
        c.get("evidence_ref")
        for n in (graph.get("nodes") or []) if isinstance(n, dict)
        for c in (n.get("claims") or []) if isinstance(c, dict) and c.get("evidence_ref")
    }
    out = []
    for entry in job_map.get("job_maps") or []:
        if not isinstance(entry, dict):
            continue
        for step in entry.get("steps") or []:
            if not isinstance(step, dict):
                continue
            refs = step.get("evidence_refs") or []
            if refs and not (set(refs) & graph_evidence):
                out.append(
                    f"Job「{entry.get('job_id')}」的階段「{step.get('id')}」，"
                    f"它的證據完全沒有在目前的服務結構圖裡被任何節點引用過，值得看一眼。"
                )
    return out


ASK_CONDITIONS = {
    "evidence_over_extended": evidence_over_extended,
    "customer_friction_concentrated": customer_friction_concentrated,
    "provider_work_outweighs_customer_steps": provider_work_outweighs_customer_steps,
    "support_processes_carry_ongoing_cost": support_processes_carry_ongoing_cost,
    "job_map_step_unaddressed": job_map_step_unaddressed,
}

JUDGMENT_ASK_CONDITIONS = tuple(ASK_CONDITIONS)


JUDGMENT_CHECKS = (
    "over_generalized_scope",
    "planned_not_yet_effective",
    "actor_not_in_evidence",
    "marker_reason_is_generic",
    "same_moment_two_paths",
    "decision_marker_missing",
    *PRINCIPLE_CHECKS,
    # 聚：主軸編碼輸出的判準（add-axial-selective-coding）。不屬於
    # PRINCIPLE_CHECKS——那組假設輸入是 graph，這兩個吃的是 categories 文件。
    "dimension_independence",
    "distribution_required",
    # 聚：Persona Matrix 輸出的判準（add-persona-matrix）。吃的是
    # persona_matrix.yaml 文件，不是 graph 也不是 categories 文件。
    "axis_not_grounded",
    "subject_unplaced_on_every_axis",
    # flag-persona-matrix-subject-identity-conflicts: a subject's identity
    # comes from evidence.yaml's source strings, and this project's own
    # prompt already warns the model that source can mix or split people --
    # these two are the code-side check that instruction never had.
    "subject_source_names_multiple_people",
    "subject_identity_overlap_across_subjects",
    # `distribution-required` 通則的軸層級實作。同一個通則底下的類別層級規則
    # （`distribution_required`）留著不動——兩個都有用，但只有這個查的是通則
    # 真正宣告的東西（enforce-axis-quality-criteria-in-persona-matrix）。
    "axis_without_placement_spread",
    # `dimension-independence` 通則的軸層級實作，同一種理由：類別層級規則
    # （`dimension_independence`）留著不動，這個查的是通則對「軸」講的那件事
    # （enforce-axis-quality-criteria-in-persona-matrix）。
    "axis_independence_in_persona_matrix",
    # 聚：Persona 內容輸出的判準（add-persona-content）。吃的是
    # persona_content.yaml 文件跟 persona_matrix.yaml 的受訪者 id 集合。
    "persona_member_not_grounded",
    "subject_unassigned_to_any_persona",
    # 聚：Persona 欄位來源政策（evidence-aware-persona-provenance）。表本身寫在
    # 判準檔裡，這裡只宣告這個檢查存在——表與檢查共用同一筆來源。
    "persona_field_provenance_policy",
    # 聚：JTBD 輸出的判準（add-jtbd）。吃的是 jtbd.yaml 文件。
    "job_not_grounded",
    # 聚：Job Map 輸出的判準（add-job-map）。吃的是 job_map.yaml 文件。
    "step_evidence_outside_job",
    # 問：HMW 輸出的判準（add-how-might-we）。吃的是 hmw.yaml 文件。
    "hmw_entry_not_grounded",
)


def _abstract_actor_findings(graph) -> list[tuple[str, str]]:
    """(node_id, actor) pairs for provider-delivered (frontstage/backstage)
    action nodes whose actor is a collective noun in ABSTRACT_ACTOR_TERMS --
    something that cannot be assigned, observed or changed, so it cannot
    really be who did the action."""
    if not isinstance(graph, dict):
        return []
    out = []
    for n in graph.get("nodes") or []:
        if not isinstance(n, dict) or n.get("kind") != "action":
            continue
        if n.get("actor_boundary") != _ABSTRACT_ACTOR_BOUNDARY:
            continue
        actor = n.get("actor")
        if actor in ABSTRACT_ACTOR_TERMS:
            out.append((n.get("id"), actor))
    return out


def abstract_actor_errors(graph) -> list[str]:
    """Hard-failure form of `_abstract_actor_findings`, for the structure
    stage's generation retry loop only (stages.py's `_validate_graph`) --
    the one place a fresh model output can still be told to fix it.

    Deliberately NOT part of `graph_errors()` / `_graph_errors_and_warnings()`,
    which `projection_errors()` also calls -- and `projection_errors()` is
    shared with re-projecting an *existing* graph (the standalone
    `sbp-gen project` command, project-existing-graph-without-model-call).
    An already-generated graph must keep loading, projecting and rendering
    even when it predates this rule; only `graph_warnings()` surfaces it
    for those. See ABSTRACT_ACTOR_TERMS for why organisations are banned
    here and internal_support/external_dependency are exempt."""
    return [
        f"[graph] {node_id} actor={actor!r} 是抽象集合名詞，不是可以指派、觀察或修改的人或角色"
        for node_id, actor in _abstract_actor_findings(graph)
    ]


def graph_errors(graph, evidence_ids=None, evidence_texts=None, journey=None, evidence_tiers=None):
    """Hard structural/referential rules for the action graph — see
    `_graph_errors_and_warnings()`. Blocks generation retries; use
    `graph_warnings()` for the advisory findings that don't."""
    errors, _ = _graph_errors_and_warnings(graph, evidence_ids, evidence_texts, journey, evidence_tiers)
    return errors


def graph_warnings(graph, evidence_ids=None, evidence_texts=None, journey=None, evidence_tiers=None):
    """Advisory findings ("...？" 結尾) for the action graph: things a
    researcher should confirm, not things that make the JSON wrong. These
    never block `_generate()`'s retry loop — see `_graph_errors_and_warnings()`.
    """
    _, warnings = _graph_errors_and_warnings(graph, evidence_ids, evidence_texts, journey, evidence_tiers)
    return warnings


def _graph_errors_and_warnings(graph, evidence_ids=None, evidence_texts=None, journey=None, evidence_tiers=None):
    """Semantic rules for the action graph, beyond draft-07 structure checks.

    Returns `(errors, warnings)`. `errors` are hard failures — malformed or
    contradictory data — and should block a generation retry loop. `warnings`
    are advisory ("...？" 結尾的訊息): a heuristic cue that a researcher should
    look at, not proof the output is wrong (flag-unmarked-decision-language 決
    定：模型對這幾類線索的判斷可能是對的，重試無法讓它「猜對」research 判斷，
    所以不能跟結構錯誤混在同一個 blocking 清單裡)。

    `evidence_texts`（選填，`{evidence_id: text}`）給了才會做過度通則化檢查——
    這個參數預設 None，是為了不動到既有呼叫端（含測試）的行為；stages.py 的
    `structure()` 手上有完整 evidence，會主動傳進來。`journey`（選填）給了才會
    做步驟指派一致性檢查，同一個理由：不改動既有呼叫端行為。`evidence_tiers`
    （選填，`{evidence_id: tier}`，tier 是 evidence.yaml 既有的
    frontstage/backstage/interpretation 欄位）給了才會做 ground-structure-
    stage-action-boundaries 的行為者佐證檢查——同樣預設 None，不動既有呼叫端。
    """
    if not isinstance(graph, dict):
        return [], []
    errors, warnings, nodes = [], [], graph.get("nodes") or []
    errors += _disposition_coverage_errors(graph)
    errors += _orphan_action_errors(graph)
    errors += step_axis_errors(graph, journey)
    errors += scenario_axis_errors(graph, journey)
    by_id = {n.get("id"): n for n in nodes if isinstance(n, dict) and n.get("id")}
    ids = [n.get("id") for n in nodes if isinstance(n, dict)]
    for node_id in sorted({x for x in ids if x and ids.count(x) > 1}):
        errors.append(f"[graph] node id 重複：{node_id}")
    marker_types_by_node: dict[str, set] = {}
    for marker in graph.get("markers") or []:
        if isinstance(marker, dict) and marker.get("node_ref"):
            marker_types_by_node.setdefault(marker["node_ref"], set()).add(marker.get("type"))
    # require-marker-specific-reason-note：note 是不是「只是把類別定義複述一遍」
    # 只能用字面比對抓，抓不到「寫了但寫得很爛」——跟這個函式其他機械線索檢查
    # 同一種取捨，只做非阻斷提醒，不擋驗證通過。只有給了 evidence_texts 才做，
    # 跟本函式其他依賴原文的檢查同一個慣例（沒給就代表呼叫端沒有原文可查）。
    if evidence_texts is not None:
        for marker in graph.get("markers") or []:
            if not isinstance(marker, dict):
                continue
            mtype, note = marker.get("type"), (marker.get("note") or "").strip()
            generic = MARKER_GENERIC_NOTE.get(mtype)
            if generic and (not note or note.casefold() == generic.casefold()):
                _emit_judgment(
                    warnings,
                    "marker_reason_is_generic",
                    f"{marker.get('node_ref')} 上的「{mtype}」沒有寫出這一次具體是什麼情況，"
                    f"只是把分類的定義複述一遍或留空。",
                )
    for n in nodes:
        if not isinstance(n, dict):
            continue
        nid, kind, layer = n.get("id"), n.get("kind"), n.get("layer")
        selected_ref = n.get("selected_solution_ref")
        if selected_ref and selected_ref not in _selected_solution_ids_by_position(journey).get(n.get("step"), set()):
            errors.append(f"[graph] {nid} 指到未選取的 Solution {selected_ref}")
        if kind == "artifact" and layer != "實體證據":
            errors.append(f"[graph] artifact {nid} 必須在 實體證據")
        if kind == "action":
            boundary, visibility = n.get("actor_boundary"), n.get("visibility")
            expected = None
            if boundary == "customer": expected = "顧客行動"
            elif boundary == "provider": expected = "前台行動" if visibility == "customer_visible" else "後台行動"
            if expected and layer != expected:
                errors.append(f"[graph] {nid} 的 actor_boundary/visibility 應在 {expected}，不是 {layer}")
            role_map = {
                "customer": ("customer", "customer_visible", "顧客行動"),
                "frontstage_delivery": ("provider", "customer_visible", "前台行動"),
                "backstage_delivery": ("provider", "customer_invisible", "後台行動"),
                "internal_support": ("included_support", "customer_invisible", "支援流程"),
                "external_dependency": ("external_dependency", "customer_invisible", "後台行動"),
                "out_of_scope": ("out_of_scope", "not_applicable", "後台行動"),
            }
            service_role = n.get("service_role")
            if service_role in role_map:
                required = role_map[service_role]
                actual = (boundary, visibility, layer)
                if actual != required:
                    errors.append(f"[graph] {nid} service_role={service_role} 應映射為 {required}，不是 {actual}")
            # validate-concrete-actors: warning only here, never an error --
            # this function is shared with re-projecting an existing graph
            # via projection_errors(), so a legacy graph predating the rule
            # must still project and render. The blocking form lives in
            # abstract_actor_errors(), called only from the structure
            # stage's fresh-generation retry loop. Gated on actor_boundary,
            # not service_role -- see _ABSTRACT_ACTOR_BOUNDARY's comment.
            if boundary == _ABSTRACT_ACTOR_BOUNDARY and n.get("actor") in ABSTRACT_ACTOR_TERMS:
                warnings.append(
                    f"{nid} 的 actor 是「{n.get('actor')}」，這是抽象集合名詞，指不出可以訪談的人或角色，是否要改成具體角色？"
                )
            if (
                evidence_texts is not None
                and evidence_tiers is not None
                and service_role in ("frontstage_delivery", "backstage_delivery")
            ):
                node_claims = n.get("claims") or []
                grounded = False
                for claim in node_claims:
                    if not isinstance(claim, dict):
                        continue
                    if claim.get("inferred"):
                        grounded = True
                        break
                    ref = claim.get("evidence_ref")
                    if evidence_tiers.get(ref) == "backstage":
                        grounded = True
                        break
                    claim_text = evidence_texts.get(ref, "")
                    if claim_text and any(term in claim_text for term in PROVIDER_ROLE_TERMS):
                        grounded = True
                        break
                if not grounded and node_claims:
                    _emit_judgment(
                        warnings,
                        "actor_not_in_evidence",
                        f"{nid} 說這件事是「{n.get('actor')}」做的，但引用的原文裡沒有任何一句"
                        f"提到服務方的人做了什麼，也沒有標成推論。請確認這個行動者是真的被觀察到，"
                        f"還是其實是推出來的。",
                    )
        for claim in n.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            src, ref = claim.get("source"), claim.get("evidence_ref")
            if src in ("reference", "model_prior"):
                errors.append(f"[graph] {nid} 不得把 reference 當 case claim")
            if src == "evidence" and evidence_ids is not None and ref not in evidence_ids:
                errors.append(f"[graph] {nid} evidence_ref={ref!r} 不存在")
            if src == "researcher" and ref:
                errors.append(f"[graph] {nid} researcher claim 不得有 evidence_ref")
            if (
                evidence_texts is not None
                and claim.get("scope") == "general_pattern"
                and not claim.get("inferred")
            ):
                text = evidence_texts.get(ref, "")
                if text and not any(q in text for q in UNIVERSAL_QUANTIFIERS):
                    _emit_judgment(
                        warnings,
                        "over_generalized_scope",
                        f"{nid} 有一句話被當成普遍現象，但它引用的原文沒有「都／每／通常／一律」"
                        f"這類講法。這句話可能只成立於這一次，不是每次都這樣。",
                    )
            if evidence_texts is not None and kind == "action" and n.get("status") != "planned":
                text = evidence_texts.get(ref, "")
                marker = next((m for m in NOT_YET_EFFECTIVE_MARKERS if m in text), None)
                if marker:
                    _emit_judgment(
                        warnings,
                        "planned_not_yet_effective",
                        f"{nid} 引用的原文帶有「{marker}」這種還沒發生的講法，"
                        f"但這張卡片被當成已經在跑的動作。",
                    )
            if evidence_texts is not None and kind == "action" and not marker_types_by_node.get(nid):
                text = evidence_texts.get(ref, "")
                cue = next((c for c in DECISION_LANGUAGE_MARKERS if c in text), None)
                if cue:
                    _emit_judgment(
                        warnings,
                        "decision_marker_missing",
                        f"{nid} 引用的原文出現「{cue}」這種詞，但這張卡片沒有標成決策點。"
                        f"如果那裡真的有人在兩個做法之間挑，要補標；只是照規則走的話就不用。",
                    )

    for node_id in sorted(planned_nodes_without_stage_evidence(graph, journey)):
        errors.append(f"[計畫未錨定] {node_id} 是計畫節點，但沒有證據把它跟目前歷程步驟的其他動作連起來。")

    selected_by_position = _selected_solution_ids_by_position(journey)
    supported = journey_anchored_support_ids(graph, journey)
    relation_ids = []
    for rel in graph.get("relations") or []:
        if not isinstance(rel, dict):
            continue
        rid, typ = rel.get("id"), rel.get("type")
        selected_ref = rel.get("selected_solution_ref")
        if selected_ref and not any(selected_ref in ids for ids in selected_by_position.values()):
            errors.append(f"[graph] relation {rid} 指到未選取的 Solution {selected_ref}")
        relation_ids.append(rid)
        ends = rel.get("endpoints") if typ == "same_event" else [rel.get("from"), rel.get("to")]
        for end in ends or []:
            if end not in by_id:
                errors.append(f"[graph] relation {rid} 指到不存在 node：{end}")
        if len(ends or []) == 2 and ends[0] == ends[1]:
            errors.append(f"[graph] relation {rid} 不得 self-edge")
        if rel.get("source") == "evidence":
            for ref in rel.get("evidence_refs") or []:
                if evidence_ids is not None and ref not in evidence_ids:
                    errors.append(f"[graph] relation {rid} evidence_ref={ref!r} 不存在")
        if typ == "supports" and len(ends or []) == 2:
            src, dst = by_id.get(ends[0]), by_id.get(ends[1])
            if not src or src.get("layer") != "支援流程":
                errors.append(f"[graph] supports {rid} 必須由 支援流程 出發")
            if not dst or dst.get("kind") != "action" or dst.get("layer") not in ("前台行動", "後台行動"):
                errors.append(f"[graph] supports {rid} 必須指向前台或後台 action")
            if src and dst and src.get("layer") == "支援流程" and dst.get("layer") in ("前台行動", "後台行動"):
                if src.get("step") != dst.get("step"):
                    errors.append(f"[graph] supports {rid} 的支援流程與目標 action 必須在同一個歷程步驟")
                selected_inference = (
                    rel.get("source") == "inference"
                    and rel.get("selected_solution_ref") in selected_by_position.get(src.get("step"), set())
                    and src.get("selected_solution_ref") == rel.get("selected_solution_ref")
                    and any(claim.get("source") == "inference" for claim in src.get("claims") or [] if isinstance(claim, dict))
                )
                if src.get("id") in to_be_rooted_ids(graph, journey) and rel.get("source") == "inference":
                    selected_inference = True
                if not selected_inference and (rel.get("source") != "evidence" or not rel.get("evidence_refs")):
                    errors.append(f"[graph] supports {rid} 必須有直接證據，或明確連到同一步研究者選取的 Solution 推論")
        if typ == "handoff" and len(ends or []) == 2:
            a, b = by_id.get(ends[0]), by_id.get(ends[1])
            if a and b and a.get("actor") == b.get("actor") and a.get("actor_boundary") == b.get("actor_boundary"):
                errors.append(f"[graph] handoff {rid} 必須跨 actor 或 actor_boundary")
        if len(ends or []) == 2:
            a, b = by_id.get(ends[0]), by_id.get(ends[1])
            a_refs, b_refs = set((a or {}).get("scenario_refs") or []), set((b or {}).get("scenario_refs") or [])
            if a_refs and b_refs and a_refs.isdisjoint(b_refs) and typ != "same_event":
                errors.append(f"[graph] relation {rid} 不能直接連接互斥 scenario")
            a_objs, b_objs = set((a or {}).get("object_refs") or []), set((b or {}).get("object_refs") or [])
            if a_objs and b_objs and a_objs.isdisjoint(b_objs) and typ != "same_event":
                errors.append(f"[graph] relation {rid} 兩端 object_refs 完全不重疊")
    for n in nodes:
        if isinstance(n, dict) and n.get("layer") == "支援流程" and n.get("id") not in supported:
            errors.append(f"[graph] support node {n.get('id')} 缺少有效 outgoing supports")
    for marker in graph.get("markers") or []:
        if isinstance(marker, dict):
            target = by_id.get(marker.get("node_ref"))
            if not target or target.get("kind") != "action":
                errors.append(f"[graph] marker {marker.get('id')} 必須 attach 到 action node")
            if evidence_ids is not None and marker.get("evidence_ref") not in evidence_ids:
                errors.append(f"[graph] marker {marker.get('id')} evidence_ref 不存在")
    warnings += action_node_consolidation_warnings(graph)
    # 五原則的機器可判部分。每一條的名字、敘述與出處都在判準來源檔；這裡只負責
    # 呼叫被授權的那些，所以撤下一條判準就等於這條檢查不再產出任何東西。
    # to-be-blueprint-generation: a To-be graph runs only the checks the
    # criteria source binds to `structure_to_be` (the inherited customer layer
    # and anchored design inference are not judged by the As-is principles).
    to_be_checks = _checks_bound_to("structure_to_be") if (graph.get("meta") or {}).get("blueprint_method") == TO_BE_METHOD else None
    for check, run_check in PRINCIPLE_CHECKS.items():
        if to_be_checks is not None and check not in to_be_checks:
            continue
        for detail in run_check(graph, journey=journey):
            _emit_judgment(warnings, check, detail)
    # to-be-blueprint-generation: the To-be rules (no-op for any other graph).
    errors.extend(to_be_errors(graph, journey))
    return errors, warnings


def projection_errors(draft, evidence_ids=None, case=None, evidence_texts=None, journey=None):
    if not isinstance(draft, dict):
        return []
    # to-be-blueprint-generation: keep the draft's meta and topology flags when
    # rebuilding the graph, or a To-be draft is checked by the legacy rules.
    errors = graph_errors({"meta": draft.get("meta") or {}, "nodes": (draft.get("graph") or {}).get("nodes", []),
                           "relations": draft.get("relations", []), "markers": draft.get("markers", []),
                           "topology_flags": draft.get("topology_flags") or [],
                           "external_references": draft.get("external_references") or []},
                          evidence_ids, evidence_texts, journey=journey)
    nodes = {n.get("id"): n for n in (draft.get("graph") or {}).get("nodes", []) if isinstance(n, dict)}
    expectations = {x.get("id"): x for x in draft.get("expectations") or [] if isinstance(x, dict)}
    placements = Counter()
    for cell in draft.get("cells") or []:
        for ref in cell.get("node_refs") or []:
            placements[ref] += 1
            if ref not in nodes: errors.append(f"[projection] node_ref {ref} 不存在")
        for ref in cell.get("expectation_refs") or []:
            if ref not in expectations: errors.append(f"[projection] expectation_ref {ref} 不存在")
        if cell.get("blank") == "no_evidence" and not cell.get("blank_note", "").strip():
            errors.append(f"[projection] no_evidence cell 必須命名待查角色／資料")
    for nid, node in nodes.items():
        is_external = node.get("actor_boundary") in ("external_dependency", "out_of_scope")
        is_secondary_only = not node_in_primary_scenario(node, journey)
        if not is_external and not is_secondary_only and placements[nid] != 1:
            errors.append(f"[projection] node {nid} 必須恰好投影一次（目前 {placements[nid]}）")
        if is_external and placements[nid]:
            errors.append(f"[projection] external node {nid} 不得進 grid")
        if is_secondary_only and placements[nid]:
            errors.append(f"[projection] node {nid} 屬於非主情境，不得進主藍圖 grid")
    if case:
        expected = {(i + 1, layer) for i in range(len(case.get("steps") or [])) for layer in LAYERS}
        got = [(c.get("step"), c.get("layer")) for c in draft.get("cells") or []]
        if set(got) != expected or len(got) != len(set(got)):
            errors.append("[projection] cells 必須剛好覆蓋每個 steps × layers 組合一次")
    for exp in expectations.values():
        status = exp.get("status")
        refs = exp.get("matched_node_refs") or []
        if status in ("supported", "partially_supported") and any(ref not in nodes for ref in refs):
            errors.append(f"[reference] {exp.get('id')} matched_node_refs 不存在")
        if status == "unsupported" and (not exp.get("ask_roles") or not exp.get("validation_question")):
            errors.append(f"[reference] {exp.get('id')} unsupported 必須有 ask_roles 與 validation_question")
    return errors


def briefing_errors(briefing, evidence_ids=None, node_ids=None, conflict_ids=None, step_ids=None, run_has_open_items=None,
                    relation_ids=None, marker_ids=None, annotation_ids=None):
    """Semantic rules for the research briefing (add-research-briefing-layer).

    Structural shape (field names, closed `kind` set, no `rank` field) is
    schema-checked; what's left is exactly the same referential-integrity
    pattern as `graph_errors()`: every reference must resolve, and — the one
    rule with no graph equivalent — every finding must cite *something*,
    because an uncited finding is a confident, unverifiable statement, which
    is exactly what this project treats as unacceptable everywhere else.
    """
    if not isinstance(briefing, dict):
        return []
    findings = briefing.get("findings") or []
    if not findings:
        if run_has_open_items:
            errors = ["[briefing] findings 是空陣列，但這個 run 至少有一項研究缺口／衝突/未處理的體驗註記——"
                      "空白結果跟「這一段沒跑」無法區分"]
        else:
            errors = []
    else:
        errors = []
    ids = [f.get("id") for f in findings if isinstance(f, dict)]
    for dup in sorted({i for i in ids if i and ids.count(i) > 1}):
        errors.append(f"[briefing] finding id 重複：{dup}")
    for f in findings:
        if not isinstance(f, dict):
            continue
        fid = f.get("id")
        refs_by_kind = {
            "evidence_ref": (f.get("evidence_refs") or [], evidence_ids),
            "node_ref": (f.get("node_refs") or [], node_ids),
            "conflict_ref": (f.get("conflict_refs") or [], conflict_ids),
            "step_ref": (f.get("step_refs") or [], step_ids),
        }
        if not any(refs for refs, _ in refs_by_kind.values()):
            errors.append(f"[briefing] finding {fid} 沒有任何引用")
        for ref_label, (refs, valid_ids) in refs_by_kind.items():
            if valid_ids is None:
                continue
            for ref in refs:
                if ref not in valid_ids:
                    errors.append(f"[briefing] finding {fid} {ref_label}={ref!r} 不存在")
    interpretation = briefing.get("interpretation") or {}
    item_ids = []
    for item in interpretation.get("items") or []:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        item_ids.append(item_id)
        refs_by_kind = {
            "evidence_ref": (item.get("evidence_refs") or [], evidence_ids),
            "node_ref": (item.get("node_refs") or [], node_ids),
            "relation_ref": (item.get("relation_refs") or [], relation_ids),
            "marker_ref": (item.get("marker_refs") or [], marker_ids),
            "annotation_ref": (item.get("annotation_refs") or [], annotation_ids),
            "conflict_ref": (item.get("conflict_refs") or [], conflict_ids),
            "step_ref": (item.get("step_refs") or [], step_ids),
        }
        if not any(refs for refs, _ in refs_by_kind.values()):
            errors.append(f"[briefing] interpretation {item_id} 沒有任何引用")
        if item.get("basis") == "evidence_synthesis" and not (item.get("evidence_refs") or item.get("node_refs") or item.get("relation_refs") or item.get("marker_refs") or item.get("annotation_refs") or item.get("conflict_refs")):
            errors.append(f"[briefing] evidence_synthesis interpretation {item_id} 缺少證據衍生引用")
        for ref_label, (refs, valid_ids) in refs_by_kind.items():
            if valid_ids is None:
                continue
            for ref in refs:
                if ref not in valid_ids:
                    errors.append(f"[briefing] interpretation {item_id} {ref_label}={ref!r} 不存在")
    for dup in sorted({i for i in item_ids if i and item_ids.count(i) > 1}):
        errors.append(f"[briefing] interpretation id 重複：{dup}")
    return errors


def referential_errors(kind, data, evidence_ids):
    if kind in ("case", "evidence", "service-graph", "reference-hypotheses"):
        return []
    errors = []
    cells = data.get("cells", [])

    # evidence_ref 存在性檢查——只對 sbp-draft 做。GT 的 evidence_ref 依 §1.4／gt.schema.json
    # 刻意可以指向 evidence.yaml 以外的來源（通常是全部資料的原始文件，如論文），不能拿 AI
    # 的證據池去卡 GT，否則就是拿 AI 看到的子集去限制「用全部資料做的標尺」。
    if evidence_ids is not None and kind == "sbp-draft":
        for ci, cell in enumerate(cells):
            step, layer = cell.get("step"), cell.get("layer")
            for cj, claim in enumerate(cell.get("claims", [])):
                ref = claim.get("evidence_ref")
                if ref and ref not in evidence_ids:
                    errors.append(
                        f"[referential] cells[{ci}](step={step},layer={layer}).claims[{cj}]: "
                        f"evidence_ref={ref!r} 在 evidence.yaml 裡找不到"
                    )
            for mj, marker in enumerate(cell.get("markers", [])):
                ref = marker.get("evidence_ref")
                if ref and ref not in evidence_ids:
                    errors.append(
                        f"[referential] cells[{ci}](step={step},layer={layer}).markers[{mj}]: "
                        f"evidence_ref={ref!r} 在 evidence.yaml 裡找不到"
                    )

    # relations[].claim_refs 指向的 (step, layer, claim_idx) 是否存在
    index = {}
    for cell in cells:
        key = (cell.get("step"), cell.get("layer"))
        index[key] = len(cell.get("claims", []))
    for ri, rel in enumerate(data.get("relations", [])):
        for cr in rel.get("claim_refs", []):
            key = (cr.get("step"), cr.get("layer"))
            n = index.get(key)
            if n is None:
                errors.append(
                    f"[referential] relations[{ri}]({rel.get('id')}): "
                    f"claim_ref 指到不存在的 cell (step={cr.get('step')}, layer={cr.get('layer')})"
                )
            elif cr.get("claim_idx", -1) >= n:
                errors.append(
                    f"[referential] relations[{ri}]({rel.get('id')}): "
                    f"claim_idx={cr.get('claim_idx')} 超出該 cell 的 claims 範圍（只有 {n} 則）"
                )
    return errors


def marker_errors(kind, data):
    """cell.markers[].type 不得在同一格重複——draft-07 JSON Schema 無法直接表達
    『陣列裡某欄位值不重複』，所以這一條放在 Python 層做（sbp-draft.schema.json
    的 cell.markers 註解有寫明這件事）。"""
    if kind != "sbp-draft":
        return []
    errors = []
    for ci, cell in enumerate(data.get("cells", [])):
        step, layer = cell.get("step"), cell.get("layer")
        seen = Counter(m.get("type") for m in cell.get("markers", []) if m.get("type"))
        for mtype, n in seen.items():
            if n > 1:
                errors.append(
                    f"[markers] cells[{ci}](step={step},layer={layer}): "
                    f"節點符號 type={mtype!r} 在同一格重複出現 {n} 次"
                )
    return errors


def coverage_warnings(kind, data):
    """軟檢查：§2.5『每個 (step×layer) 都必須輸出一筆 cell』。不通過只警告，不算失敗。"""
    if kind == "evidence":
        return []
    layers = ["實體證據", "顧客行動", "前台行動", "後台行動", "支援流程"]
    seen = {(c.get("step"), c.get("layer")) for c in data.get("cells", [])}
    steps = sorted({s for s, _ in seen if s is not None})
    warnings = []
    for s in steps:
        for layer in layers:
            if (s, layer) not in seen:
                warnings.append(f"[覆蓋率警告] 缺 cell: step={s}, layer={layer}")
    return warnings



# 節點 id 的 schema 形狀是 `^(A|P)-[A-Za-z0-9_-]+$`，不是只有數字流水號。原本這
# 條樣式寫成 `[A-Z]{1,3}-\d+`，於是 `A-obs-passby` 這種 id 直接原樣印到研究者面
# 前——只在第三份真實產出上才看得到，前兩份剛好都用數字編號。兩種形狀都收：舊
# 的涵蓋 `E-226`、`R-1` 這類引用與關係編號，新的涵蓋帶字母的節點 id。
NODE_ID_PATTERN = re.compile(r"\b([A-Z]{1,3}-\d+|[AP]-[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*)\b")


FINDING_PREFIX_PATTERN = re.compile(r"^\s*\[[a-z_]+\]\s*")

# 驗證訊息是寫給跑生成管線的人看的，裡面夾雜架構詞。專案規則禁止這些字出現在
# 研究者畫面上（跟禁止裸編號是同一條理由），所以在訊息離開伺服器之前換掉。
# 這是一份「已知會出現什麼就換什麼」的明確清單，不是通用翻譯器——清單沒蓋到的
# 字會原樣通過，那是該回到訊息產生處修的可讀性問題。
FINDING_TERM_SUBSTITUTIONS = (
    ("不得進主藍圖 grid", "不該出現在主藍圖上"),
    ("主藍圖 grid", "主藍圖"),
    ("node ", ""),
    ("節點", "卡片"),
    ("情境", "服務情境"),
)


def researcher_finding(text: str, labels: dict[str, str]) -> str:
    """把一則驗證訊息改寫成研究者看得懂、且符合專案規則的句子。

    做兩件事：把 `A-02` 這種內部編號換成卡片名稱（找不到對應卡片時明講
    「已不在這個版本裡」，仍然不印編號），以及拿掉管線自己的 `[graph]`
    這類子系統前綴與已知的架構詞。在伺服器端就做完，每個消費端都自動
    符合規則，瀏覽器也不需要知道編號長什麼樣子。
    """
    def swap(match):
        node_id = match.group(1)
        label = labels.get(node_id)
        return f"「{label}」" if label else "某張已不在這個版本裡的卡片"

    result = FINDING_PREFIX_PATTERN.sub("", str(text))
    result = NODE_ID_PATTERN.sub(swap, result)
    for term, replacement in FINDING_TERM_SUBSTITUTIONS:
        result = result.replace(term, replacement)
    result = re.sub(r"\bstep=(\d+)", r"第 \1 步", result)
    result = " ".join(result.split())
    # 換掉編號、拿掉架構詞之後，原本用來隔開英文 token 的空格會夾在兩個中文字
    # 之間（原文長這樣：`node A-02 屬於…`、`step=7 不在…`）。中文之間的空格一律
    # 收掉；夾著數字或英文的空格保留，那些是真的需要分隔的內容。
    return re.sub(r"(?<=[^\x00-\x7F])\s+(?=[^\x00-\x7F])", "", result)


def judgment_report(graph, evidence_ids=None, evidence_texts=None, journey=None,
                    evidence_tiers=None, job_map=None):
    """兩段話：這個系統已經判完的，和它在等研究者判的。

    分開是這份報告存在的理由。合在一起會讓「圖上第 3 步沒有任何卡片」跟「這條
    歷程對顧客值不值得走」看起來是同一種東西——前者讀檔案就能確定，後者只有
    研究者能回答。把後者寫成結論，就是把推論寫得像事實。

    兩段都一定會印。沒有發現的時候明說沒有，不是整段消失——不然研究者分不出
    「查過、沒事」跟「根本沒查」。
    """
    from sbp_gen import judgment_criteria as JC

    criteria = JC.load_default()
    labels = {
        node.get("id"): node.get("label")
        for node in (graph.get("nodes") or [])
        if isinstance(node, dict) and node.get("id") and node.get("label")
    }
    findings = [
        w for w in graph_warnings(graph, evidence_ids, evidence_texts,
                                  journey=journey, evidence_tiers=evidence_tiers)
        if isinstance(w, JC.JudgmentFinding)
    ]

    lines = ["── 這個系統已經判完的 ──"]
    if not findings:
        lines.append("  逐條看過了，沒有發現問題。")
    else:
        by_criterion = {}
        for found in findings:
            by_criterion.setdefault(found.criterion, []).append(found)
        for criterion, group in by_criterion.items():
            lines.append(f"  {criterion.label}（{len(group)} 處）")
            # 同一句話照筆數重印，佔了畫面卻不多給任何判斷依據——這個專案已經在
            # 藍圖的「沒畫線」通知上踩過一次（實測六條裡五條一字不差）。相同的句
            # 子收成一行加次數，研究者要的是「這件事發生了幾次」，不是看它幾遍。
            seen = {}
            for found in group:
                detail = str(found)[len(criterion.label) + 3:].strip()
                line = researcher_finding(detail, labels)
                seen[line] = seen.get(line, 0) + 1
            for line, times in seen.items():
                lines.append("    · " + line + (f"（{times} 次）" if times > 1 else ""))

    lines.append("")
    lines.append("── 這些在等你判 ──")
    asked = []
    for criterion in criteria.asked_of_researcher():
        condition = ASK_CONDITIONS.get(criterion.asked_when)
        reasons = condition(graph, job_map=job_map) if condition else []
        if reasons:
            asked.append((criterion, reasons))
    if not asked:
        lines.append("  這份產出沒有觸發任何需要你判斷的問題。")
    else:
        for criterion, reasons in asked:
            standing = ("這條判準在本專案的語料裡核實過"
                        if criterion.source_status == "corpus_verified"
                        else "這條判準來自通用的服務設計文獻，不是本專案語料，你可以不採用")
            lines.append(f"  {criterion.label}？")
            for reason in reasons:
                lines.append("    · " + researcher_finding(reason, labels))
            lines.append(f"    （{standing}）")
    return lines


def gap_report(kind, data):
    """研究者面向的缺口報告（ADR-0002 決定二／四）。

    兩件事：
      1. 空白三態統計 —— `no_evidence` 的那些格就是待訪清單
      2. 引用複用次數排行 —— 拉伸集中在被反覆引用的少數證據上，
         所以「依複用次數排序、只查最上面幾條」是免費的抽查 triage
    """
    if kind == "evidence":
        return []
    lines = []
    cells = data.get("cells", [])
    new_format = kind == "sbp-draft" and "graph" in data
    if new_format:
        nodes = {n.get("id"): n for n in (data.get("graph") or {}).get("nodes", [])}
        has = [c for c in cells if c.get("node_refs")]
        absent = [c for c in cells if not c.get("node_refs") and c.get("blank") == "confirmed_absent"]
        noev = [c for c in cells if not c.get("node_refs") and c.get("blank") == "no_evidence"]
        lines.append(f"[空白三態] 有 node {len(has)} 格　確認沒有 {len(absent)} 格　沒人問過 {len(noev)} 格")
        for c in noev:
            lines.append(f"    待訪: step={c['step']} {c['layer']} — {c.get('blank_note') or '（未註明要問誰）'}")
        refs = Counter(cl.get("evidence_ref") for n in nodes.values() for cl in n.get("claims", []) if cl.get("evidence_ref"))
        if refs:
            n_claims = sum(refs.values())
            lines.append(f"[引用抽查 triage] 有來源 claim {n_claims} 則 ÷ 被引用證據 {len(refs)} 條 = 每條平均撐 {n_claims/len(refs):.2f} 則。複用越高越該抽查：")
            lines.extend(f"    {rid}: 被引用 {n} 次" for rid, n in refs.most_common(5))
        marker_types = Counter(m.get("type") for m in data.get("markers", []) if m.get("type"))
        lines.append("[符號密度] " + ("、".join(f"{t} {n} 個" for t, n in marker_types.items()) if marker_types else "這次結果沒有產出任何節點符號（失誤點／等候點／決策點）。"))
        return lines

    has, absent, noev = [], [], []
    for c in cells:
        cl = c.get("claims") or []
        if cl:
            has.append(c)
        elif c.get("blank") == "no_evidence" or c.get("gt_unknown"):
            noev.append(c)
        else:
            absent.append(c)
    lines.append(
        f"[空白三態] 有 claim {len(has)} 格　確認沒有 {len(absent)} 格　沒人問過 {len(noev)} 格"
    )
    for c in noev:
        note = c.get("blank_note") or c.get("gt_unknown_reason") or "（未註明要問誰）"
        lines.append(f"    待訪: step={c['step']} {c['layer']} — {note}")

    refs = Counter()
    for c in cells:
        for cm in (c.get("claims") or []):
            r = (cm.get("evidence_ref") or "").strip()
            if r:
                refs[r] += 1
    if refs:
        n_claims = sum(refs.values())
        lines.append(
            f"[引用抽查 triage] 有來源 claim {n_claims} 則 ÷ 被引用證據 {len(refs)} 條 "
            f"= 每條平均撐 {n_claims/len(refs):.2f} 則。複用越高越該抽查："
        )
        for rid, n in refs.most_common(5):
            lines.append(f"    {rid}: 被引用 {n} 次")

    lines.extend(_marker_density(cells, has))
    return lines


def _marker_density(cells, non_empty_cells):
    """節點符號密度統計（ADR-0005 風險一）：這是分布問題，不是單筆對錯問題——
    標得太密或太疏都代表符號實質上在做評分，所以只印分布，不設門檻、不判定對錯。"""
    marker_types = ["失誤點", "等候點", "決策點"]
    n_non_empty = len(non_empty_cells) or 1
    counts = Counter()
    by_layer = {t: Counter() for t in marker_types}
    for c in cells:
        for m in c.get("markers", []):
            t = m.get("type")
            if t in marker_types:
                counts[t] += 1
                by_layer[t][c.get("layer")] += 1

    lines = []
    if not counts:
        lines.append("[符號密度] 這次結果沒有產出任何節點符號（失誤點／等候點／決策點）。")
        return lines

    lines.append(f"[符號密度] 非空格 {len(non_empty_cells)} 格：")
    for t in marker_types:
        n = counts.get(t, 0)
        share = n / n_non_empty * 100
        layer_str = "、".join(f"{layer} {cnt}" for layer, cnt in by_layer[t].most_common()) or "—"
        lines.append(f"    {t} {n} 個（佔非空格 {share:.0f}%）　分布：{layer_str}")
    return lines


CONTENT_ID_PATTERNS = {
    "evidence": re.compile(r"^E-[0-9]+$"),
    "as_is_element": re.compile(r"^(A|P)-[A-Za-z0-9_-]+$"),
    "issue": re.compile(r"^I-[A-Za-z0-9_-]+$"),
    "research_source": re.compile(r"^R-[A-Za-z0-9_-]+$"),
    "research_claim": re.compile(r"^RC-[A-Za-z0-9_-]+$"),
    "proposal": re.compile(r"^T-[A-Za-z0-9_-]+$"),
}

RESEARCH_QUERY_FORBIDDEN_PATTERNS = (
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("mobile_phone", re.compile(r"(?<!\d)09\d{8}(?!\d)")),
    ("taiwan_id", re.compile(r"\b[A-Z][12]\d{8}\b", re.IGNORECASE)),
    ("named_identifier", re.compile(r"(?:姓名|客戶編號|身分證|身份證)\s*[:：]")),
)


def _record_index(artifact, collection, records, namespace):
    """Build one deterministic typed index and report every malformed ID."""
    errors, index = [], {}
    pattern = CONTENT_ID_PATTERNS[namespace]
    for position, record in enumerate(records or []):
        if not isinstance(record, dict):
            continue
        record_id = record.get("id")
        if not isinstance(record_id, str) or not pattern.fullmatch(record_id):
            errors.append(
                f"[provenance] {artifact}.{collection}[{position}].id={record_id!r} "
                f"不屬於 {namespace} namespace"
            )
            continue
        if record_id in index:
            errors.append(
                f"[provenance] {artifact}.{collection} id 重複：{record_id}"
            )
            continue
        index[record_id] = record
    return index, errors


def build_content_indexes(*, evidence=None, graph=None, issues=None, research=None, to_be=None):
    """Return typed content indexes plus stable duplicate/namespace errors."""
    indexes, errors = {}, []
    specs = (
        ("evidence", "items", evidence or [], "evidence"),
        ("service-graph", "nodes", (graph or {}).get("nodes") or [], "as_is_element"),
        ("issue-package", "issues", (issues or {}).get("issues") or [], "issue"),
        ("research-catalog", "sources", (research or {}).get("sources") or [], "research_source"),
        ("research-catalog", "claims", (research or {}).get("claims") or [], "research_claim"),
        ("to-be-bundle", "proposals", (to_be or {}).get("proposals") or [], "proposal"),
    )
    for artifact, collection, records, namespace in specs:
        index, index_errors = _record_index(artifact, collection, records, namespace)
        indexes[namespace] = index
        errors.extend(index_errors)
    return indexes, errors


def _artifact_boundary(artifact, data):
    if artifact == "service-graph":
        meta = (data or {}).get("meta") or {}
        return meta.get("case_id"), meta.get("scenario_id")
    return (data or {}).get("case_id"), (data or {}).get("scenario_id")


def content_boundary_errors(artifacts):
    """Require every scenario-scoped artifact to agree with the first one."""
    errors, expected = [], None
    for artifact, data in artifacts:
        case_id, scenario_id = _artifact_boundary(artifact, data)
        if expected is None:
            expected = (artifact, case_id, scenario_id)
            continue
        expected_name, expected_case, expected_scenario = expected
        if case_id != expected_case:
            errors.append(
                f"[provenance] {artifact}.case_id={case_id!r} 與 "
                f"{expected_name}.case_id={expected_case!r} 不一致"
            )
        if scenario_id != expected_scenario:
            errors.append(
                f"[provenance] {artifact}.scenario_id={scenario_id!r} 與 "
                f"{expected_name}.scenario_id={expected_scenario!r} 不一致"
            )
    return errors


def canonical_graph_dependency_errors(graph):
    if not isinstance(graph, dict) or graph.get("schema_version") != 2:
        return [
            "[provenance] service-graph.schema_version 必須是 2；legacy graph "
            "只能唯讀，請從 confirmed journey 產生 canonical graph"
        ]
    meta = graph.get("meta") or {}
    if meta.get("artifact_type") != "as_is_graph":
        return ["[provenance] service-graph.meta.artifact_type 必須是 'as_is_graph'"]
    return []


def _graph_support_indexes(graph):
    nodes = {
        node.get("id"): node
        for node in (graph or {}).get("nodes") or []
        if isinstance(node, dict) and node.get("id")
    }
    relations = {
        relation.get("id"): relation
        for relation in (graph or {}).get("relations") or []
        if isinstance(relation, dict) and relation.get("id")
    }
    markers = {
        marker.get("id"): marker
        for marker in (graph or {}).get("markers") or []
        if isinstance(marker, dict) and marker.get("id")
    }
    return nodes, relations, markers


def issue_package_errors(issue_package, evidence, graph):
    indexes, errors = build_content_indexes(
        evidence=evidence, graph=graph, issues=issue_package
    )
    errors.extend(canonical_graph_dependency_errors(graph))
    errors.extend(content_boundary_errors((
        ("service-graph", graph), ("issue-package", issue_package)
    )))
    nodes, relations, markers = _graph_support_indexes(graph)
    for issue in (issue_package or {}).get("issues") or []:
        if not isinstance(issue, dict):
            continue
        issue_id = issue.get("id")
        element_refs = issue.get("as_is_element_refs") or []
        evidence_refs = issue.get("evidence_refs") or []
        support_refs = set()
        for ref in element_refs:
            node = nodes.get(ref)
            if node is None:
                errors.append(
                    f"[provenance] issue-package {issue_id}.as_is_element_refs={ref!r} 不存在"
                )
                continue
            support_refs.update(
                claim.get("evidence_ref")
                for claim in node.get("claims") or []
                if isinstance(claim, dict) and claim.get("evidence_ref")
            )
        for ref in issue.get("relation_refs") or []:
            relation = relations.get(ref)
            if relation is None:
                errors.append(
                    f"[provenance] issue-package {issue_id}.relation_refs={ref!r} 不存在"
                )
                continue
            endpoints = set(relation.get("endpoints") or []) | {
                relation.get("from"), relation.get("to")
            }
            if not endpoints.intersection(element_refs):
                errors.append(
                    f"[provenance] issue-package {issue_id}.relation_refs={ref!r} "
                    "未連接任何 cited As-is element"
                )
                continue
            support_refs.update(relation.get("evidence_refs") or [])
        for ref in issue.get("marker_refs") or []:
            marker = markers.get(ref)
            if marker is None:
                errors.append(
                    f"[provenance] issue-package {issue_id}.marker_refs={ref!r} 不存在"
                )
                continue
            if marker.get("node_ref") not in element_refs:
                errors.append(
                    f"[provenance] issue-package {issue_id}.marker_refs={ref!r} "
                    "不屬於任何 cited As-is element"
                )
                continue
            if marker.get("evidence_ref"):
                support_refs.add(marker["evidence_ref"])
        for ref in evidence_refs:
            if ref not in indexes["evidence"]:
                errors.append(
                    f"[provenance] issue-package {issue_id}.evidence_refs={ref!r} 不存在"
                )
            elif ref not in support_refs:
                errors.append(
                    f"[provenance] issue-package {issue_id}.evidence_refs={ref!r} "
                    f"未由 cited elements {element_refs!r} 或其 cited relations/markers 支持"
                )
    return errors


def research_catalog_errors(research_catalog):
    indexes, errors = build_content_indexes(research=research_catalog)
    sources = indexes["research_source"]
    for claim in (research_catalog or {}).get("claims") or []:
        if not isinstance(claim, dict):
            continue
        claim_id = claim.get("id")
        admitted_support = False
        for link in claim.get("source_links") or []:
            if not isinstance(link, dict):
                continue
            ref = link.get("source_ref")
            source = sources.get(ref)
            if source is None:
                errors.append(
                    f"[provenance] research-catalog {claim_id}.source_links.source_ref={ref!r} 不存在"
                )
                continue
            if link.get("relationship") == "supports" and source.get("admission") == "admitted":
                admitted_support = True
        if not admitted_support:
            errors.append(
                f"[provenance] research-catalog {claim_id}.source_links "
                "缺少 admitted supports R-* source"
            )
    for position, query in enumerate((research_catalog or {}).get("query_audit") or []):
        if not isinstance(query, dict):
            continue
        for ref in query.get("returned_source_refs") or []:
            if ref not in sources:
                errors.append(
                    f"[provenance] research-catalog.query_audit[{position}]."
                    f"returned_source_refs={ref!r} 不存在"
                )
        query_text = query.get("deidentified_query") or ""
        for label, pattern in RESEARCH_QUERY_FORBIDDEN_PATTERNS:
            if pattern.search(query_text):
                errors.append(
                    f"[privacy] research-catalog.query_audit[{position}]."
                    f"deidentified_query 命中禁止模式 {label}"
                )
    return errors


def to_be_bundle_errors(to_be, evidence, graph, issue_package, research_catalog):
    indexes, errors = build_content_indexes(
        evidence=evidence,
        graph=graph,
        issues=issue_package,
        research=research_catalog,
        to_be=to_be,
    )
    errors.extend(structural_errors(to_be, load_schema("to-be-bundle")))
    errors.extend(canonical_graph_dependency_errors(graph))
    errors.extend(content_boundary_errors((
        ("service-graph", graph),
        ("issue-package", issue_package),
        ("research-catalog", research_catalog),
        ("to-be-bundle", to_be),
    )))
    issues = indexes["issue"]
    research_claims = indexes["research_claim"]
    sources = indexes["research_source"]
    nodes = indexes["as_is_element"]
    proposals = indexes["proposal"]
    evidence_index = indexes["evidence"]
    scenario_id = (to_be or {}).get("scenario_id")
    certainty_pattern = re.compile(
        r"(?:已(?:證明|核准|批准|實施|可行)|保證|必然|一定會|ready to implement|"
        r"proven for|guaranteed|approved|implemented|organizationally feasible)",
        re.IGNORECASE,
    )
    alternative_pattern = re.compile(
        r"(?:ranked alternatives?|option\s+[A-C](?:\s|:)|選項\s*[ABCＡＢＣ]|方案\s*[ABCＡＢＣ].{0,12}(?:評分|排名))",
        re.IGNORECASE,
    )
    direction = (to_be or {}).get("direction") or {}
    for field in ("title", "summary"):
        text = str(direction.get(field) or "")
        if certainty_pattern.search(text):
            errors.append(f"[proposal] to-be-bundle.direction.{field} 含有已可行或已實施宣稱")
        if alternative_pattern.search(text):
            errors.append(f"[proposal] to-be-bundle.direction.{field} 含有競爭方案或排名語言")

    coverage_by_issue = {}
    addressed_count = 0
    for position, coverage in enumerate((to_be or {}).get("issue_coverage") or []):
        if not isinstance(coverage, dict):
            continue
        issue_ref = coverage.get("issue_ref")
        if issue_ref in coverage_by_issue:
            errors.append(f"[proposal] to-be-bundle.issue_coverage issue_ref 重複：{issue_ref}")
        coverage_by_issue[issue_ref] = coverage
        if issue_ref not in issues:
            errors.append(
                f"[provenance] to-be-bundle.issue_coverage[{position}].issue_ref={issue_ref!r} 不存在"
            )
        if coverage.get("status") == "addressed":
            addressed_count += 1
            for ref in coverage.get("proposal_refs") or []:
                proposal = proposals.get(ref)
                if proposal is None:
                    errors.append(
                        f"[provenance] to-be-bundle.issue_coverage {issue_ref}.proposal_refs={ref!r} 不存在"
                    )
                elif issue_ref not in (proposal.get("issue_refs") or []):
                    errors.append(
                        f"[provenance] to-be-bundle.issue_coverage {issue_ref} 與 proposal {ref} 未互相引用"
                    )
        elif coverage.get("status") == "not_addressed":
            explanation = str(coverage.get("explanation") or "")
            if re.search(r"(?:低優先|不重要|低價值|不值得|low priority|low value|unimportant)", explanation, re.I):
                errors.append(
                    f"[proposal] to-be-bundle.issue_coverage {issue_ref} 以未授權的商業排序解釋省略"
                )
    expected_issue_refs = set(issues)
    received_issue_refs = set(coverage_by_issue)
    for ref in sorted(expected_issue_refs - received_issue_refs):
        errors.append(f"[proposal] to-be-bundle.issue_coverage 缺少 issue_ref={ref!r}")
    for ref in sorted(received_issue_refs - expected_issue_refs):
        errors.append(f"[proposal] to-be-bundle.issue_coverage 多出 issue_ref={ref!r}")
    if expected_issue_refs and addressed_count == 0:
        errors.append("[proposal] to-be-bundle 至少需要一個 addressed issue")

    for proposal in (to_be or {}).get("proposals") or []:
        if not isinstance(proposal, dict):
            continue
        proposal_id = proposal.get("id")
        for field in ("statement", "expected_change"):
            text = str(proposal.get(field) or "")
            if certainty_pattern.search(text):
                errors.append(
                    f"[proposal] to-be-bundle {proposal_id}.{field} 含有已可行或已實施宣稱"
                )
            if alternative_pattern.search(text):
                errors.append(
                    f"[proposal] to-be-bundle {proposal_id}.{field} 含有競爭方案或排名語言"
                )
        for ref in proposal.get("issue_refs") or []:
            if ref not in issues:
                errors.append(
                    f"[provenance] to-be-bundle {proposal_id}.issue_refs={ref!r} 不存在"
                )
            else:
                coverage = coverage_by_issue.get(ref) or {}
                if coverage.get("status") != "addressed":
                    errors.append(
                        f"[provenance] to-be-bundle {proposal_id}.issue_refs={ref!r} 未標為 addressed"
                    )
                issue = issues[ref]
                if not any(item in nodes for item in issue.get("as_is_element_refs") or []):
                    errors.append(
                        f"[provenance] to-be-bundle {proposal_id}.issue_refs={ref!r} 沒有可解析 As-is element"
                    )
                if not any(item in evidence_index for item in issue.get("evidence_refs") or []):
                    errors.append(
                        f"[provenance] to-be-bundle {proposal_id}.issue_refs={ref!r} 沒有可解析 evidence"
                    )
        cited_claims = []
        for ref in proposal.get("research_claim_refs") or []:
            claim = research_claims.get(ref)
            if claim is None:
                errors.append(
                    f"[provenance] to-be-bundle {proposal_id}.research_claim_refs={ref!r} 不存在"
                )
            else:
                cited_claims.append(claim)
        source_refs = set(proposal.get("source_refs") or [])
        linked_refs = {
            link.get("source_ref")
            for claim in cited_claims
            for link in claim.get("source_links") or []
            if isinstance(link, dict) and link.get("source_ref")
        }
        for ref in sorted(source_refs):
            source = sources.get(ref)
            if source is None:
                errors.append(
                    f"[provenance] to-be-bundle {proposal_id}.source_refs={ref!r} 不存在"
                )
            elif source.get("admission") != "admitted":
                errors.append(
                    f"[provenance] to-be-bundle {proposal_id}.source_refs={ref!r} 未 admitted"
                )
            if ref not in linked_refs:
                errors.append(
                    f"[provenance] to-be-bundle {proposal_id}.source_refs={ref!r} "
                    "未由 cited RC-* claim 連結"
                )
        for claim in cited_claims:
            admitted_supports = {
                link.get("source_ref")
                for link in claim.get("source_links") or []
                if isinstance(link, dict)
                and link.get("relationship") == "supports"
                and (sources.get(link.get("source_ref")) or {}).get("admission") == "admitted"
            }
            if not admitted_supports.intersection(source_refs):
                errors.append(
                    f"[provenance] to-be-bundle {proposal_id}.research_claim_refs={claim.get('id')!r} "
                    "沒有對應的 admitted supports R-* in source_refs"
                )
        validation_plan = proposal.get("validation_plan") or {}
        success_condition = str(validation_plan.get("success_condition") or "")
        decision_rule = str(validation_plan.get("decision_rule") or "")
        if not re.search(r"(?:\d|%|％|基準|baseline|比較|下降|增加|不高於|不低於|至少|至多)", success_condition, re.I):
            errors.append(
                f"[validation] to-be-bundle {proposal_id}.validation_plan.success_condition 缺少可觀察門檻或比較規則"
            )
        if not re.search(r"(?:繼續|擴大|採用|continue|proceed)", decision_rule, re.I):
            errors.append(
                f"[validation] to-be-bundle {proposal_id}.validation_plan.decision_rule 缺少 continue 條件"
            )
        if not re.search(r"(?:修訂|調整|修改|revise|adjust)", decision_rule, re.I):
            errors.append(
                f"[validation] to-be-bundle {proposal_id}.validation_plan.decision_rule 缺少 revise 條件"
            )
        if not re.search(r"(?:停止|終止|不採用|stop|terminate|reject)", decision_rule, re.I):
            errors.append(
                f"[validation] to-be-bundle {proposal_id}.validation_plan.decision_rule 缺少 stop 條件"
            )
    local_element_ids = set()
    covered_as_is = {}
    graph_nodes = [node for node in (graph or {}).get("nodes") or [] if isinstance(node, dict)]
    in_scope_nodes = {
        node.get("id"): node
        for node in graph_nodes
        if not node.get("scenario_refs") or scenario_id in (node.get("scenario_refs") or [])
    }
    valid_steps = {node.get("step") for node in in_scope_nodes.values() if isinstance(node.get("step"), int)}
    for change in (to_be or {}).get("blueprint_changes") or []:
        if not isinstance(change, dict):
            continue
        change_id, operation = change.get("id"), change.get("operation")
        as_is_ref = change.get("as_is_element_ref")
        if operation in ("retain", "modify", "remove") and as_is_ref not in nodes:
            errors.append(
                f"[provenance] to-be-bundle blueprint_change {change_id}."
                f"as_is_element_ref={as_is_ref!r} 不存在"
            )
        if operation in ("retain", "modify", "remove") and as_is_ref in in_scope_nodes:
            if as_is_ref in covered_as_is:
                errors.append(
                    f"[projection] to-be-bundle blueprint_changes 重複涵蓋 As-is element {as_is_ref}"
                )
            covered_as_is[as_is_ref] = change_id
        if operation in ("modify", "add") and change.get("step") not in valid_steps:
            errors.append(
                f"[projection] to-be-bundle blueprint_change {change_id}.step={change.get('step')!r} 不在 confirmed journey step axis"
            )
        local_id = change.get("local_element_id")
        if local_id:
            if local_id in local_element_ids:
                errors.append(
                    f"[provenance] to-be-bundle blueprint_changes local_element_id 重複：{local_id}"
                )
            local_element_ids.add(local_id)
        for ref in change.get("proposal_refs") or []:
            if ref not in proposals:
                errors.append(
                    f"[provenance] to-be-bundle blueprint_change {change_id}."
                    f"proposal_refs={ref!r} 不存在"
                )
    for ref in sorted(set(in_scope_nodes) - set(covered_as_is)):
        errors.append(f"[projection] to-be-bundle blueprint_changes 缺少 As-is element {ref}")
    return errors


def pilot_record_errors(pilot, to_be, *, previous_pilot=None):
    """Validate a pilot snapshot against its exact accepted To-be content."""
    errors = []
    errors.extend(structural_errors(pilot, load_schema("pilot-record")))
    errors.extend(content_boundary_errors((
        ("to-be-bundle", to_be), ("pilot-record", pilot)
    )))
    proposals = [
        item for item in (to_be or {}).get("proposals") or [] if isinstance(item, dict)
    ]
    protocol = [
        item for item in (pilot or {}).get("protocol") or [] if isinstance(item, dict)
    ]
    expected_refs = [item.get("id") for item in proposals]
    actual_refs = [item.get("proposal_ref") for item in protocol]
    if actual_refs != expected_refs:
        errors.append(
            f"[validation] pilot-record.protocol proposal order={actual_refs!r} "
            f"does not match To-be proposals={expected_refs!r}"
        )
    copied_fields = {
        "target_outcome": "target_outcome",
        "metric": "metric",
        "method": "method",
        "success_condition": "success_condition",
        "decision_rule": "decision_rule",
        "planned_pilot_scope": "pilot_scope",
    }
    proposals_by_id = {item.get("id"): item for item in proposals if item.get("id")}
    for entry in protocol:
        proposal_ref = entry.get("proposal_ref")
        proposal = proposals_by_id.get(proposal_ref)
        if proposal is None:
            errors.append(
                f"[provenance] pilot-record.protocol proposal_ref={proposal_ref!r} does not exist"
            )
            continue
        plan = proposal.get("validation_plan") or {}
        for pilot_field, plan_field in copied_fields.items():
            if entry.get(pilot_field) != plan.get(plan_field):
                errors.append(
                    f"[validation] pilot-record.protocol {proposal_ref}.{pilot_field} "
                    "does not faithfully copy the accepted To-be validation plan"
                )

    observations = [
        item for item in (pilot or {}).get("observations") or [] if isinstance(item, dict)
    ]
    observation_ids = [item.get("id") for item in observations]
    for duplicate in sorted({item for item in observation_ids if item and observation_ids.count(item) > 1}):
        errors.append(f"[provenance] pilot-record.observations id duplicated: {duplicate}")
    observation_by_id = {item.get("id"): item for item in observations if item.get("id")}
    for observation in observations:
        observation_id = observation.get("id")
        proposal_ref = observation.get("proposal_ref")
        if proposal_ref not in proposals_by_id:
            errors.append(
                f"[provenance] pilot-record observation {observation_id}.proposal_ref={proposal_ref!r} does not exist"
            )
        value = observation.get("value")
        if observation.get("value_type") == "quantitative" and (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            errors.append(
                f"[validation] pilot-record observation {observation_id}.value must be finite"
            )
    dispositions = [
        item for item in (pilot or {}).get("not_observed") or [] if isinstance(item, dict)
    ]
    disposition_refs = [item.get("proposal_ref") for item in dispositions]
    for duplicate in sorted({item for item in disposition_refs if item and disposition_refs.count(item) > 1}):
        errors.append(f"[validation] pilot-record.not_observed proposal_ref duplicated: {duplicate}")
    for ref in disposition_refs:
        if ref not in proposals_by_id:
            errors.append(
                f"[provenance] pilot-record.not_observed proposal_ref={ref!r} does not exist"
            )
    if (pilot or {}).get("state") == "completed":
        if not observations:
            errors.append("[validation] completed pilot-record must contain at least one observation")
        observed_refs = {item.get("proposal_ref") for item in observations}
        disposition_set = set(disposition_refs)
        for proposal_ref in expected_refs:
            has_observation = proposal_ref in observed_refs
            has_disposition = proposal_ref in disposition_set
            if has_observation == has_disposition:
                if has_observation:
                    detail = "has both observations and not_observed disposition"
                else:
                    detail = "has neither observations nor not_observed disposition"
                errors.append(
                    f"[validation] completed pilot-record proposal_ref={proposal_ref!r} {detail}"
                )

    if isinstance(previous_pilot, dict):
        errors.extend(content_boundary_errors((
            ("previous-pilot-record", previous_pilot), ("pilot-record", pilot)
        )))
        if previous_pilot.get("pilot_cycle_id") != (pilot or {}).get("pilot_cycle_id"):
            errors.append("[validation] pilot-record replacement changes pilot_cycle_id")
        previous_observations = [
            item for item in previous_pilot.get("observations") or [] if isinstance(item, dict)
        ]
        if observations[:len(previous_observations)] != previous_observations:
            errors.append("[validation] pilot-record replacement alters or removes prior observations")
        if previous_pilot.get("state") == "completed" and pilot != previous_pilot:
            errors.append("[validation] completed pilot-record is immutable")
    return errors


def _recommended_validation_decision(assessments):
    values = {
        item.get("rule_assessment")
        for item in assessments
        if isinstance(item, dict)
    }
    if "stop" in values:
        return "stop"
    if values.intersection({"revise", "inconclusive"}):
        return "revise"
    return "continue"


def validation_outcome_errors(outcome, pilot, to_be):
    """Validate an accountable outcome against one completed pilot snapshot."""
    errors = []
    errors.extend(structural_errors(outcome, load_schema("validation-outcome")))
    errors.extend(content_boundary_errors((
        ("to-be-bundle", to_be),
        ("pilot-record", pilot),
        ("validation-outcome", outcome),
    )))
    if (pilot or {}).get("state") != "completed":
        errors.append("[validation] validation-outcome requires a completed pilot-record")
    if (outcome or {}).get("pilot_cycle_id") != (pilot or {}).get("pilot_cycle_id"):
        errors.append("[provenance] validation-outcome.pilot_cycle_id does not match pilot-record")
    proposals = [
        item for item in (to_be or {}).get("proposals") or [] if isinstance(item, dict)
    ]
    expected_refs = [item.get("id") for item in proposals]
    assessments = [
        item for item in (outcome or {}).get("assessments") or [] if isinstance(item, dict)
    ]
    actual_refs = [item.get("proposal_ref") for item in assessments]
    if len(actual_refs) != len(set(actual_refs)):
        errors.append("[validation] validation-outcome assessments contain duplicate proposal_ref")
    if set(actual_refs) != set(expected_refs):
        errors.append(
            f"[validation] validation-outcome assessment coverage={sorted(set(actual_refs))!r} "
            f"does not match proposals={sorted(set(expected_refs))!r}"
        )
    observations = {
        item.get("id"): item
        for item in (pilot or {}).get("observations") or []
        if isinstance(item, dict) and item.get("id")
    }
    dispositions = {
        item.get("proposal_ref"): item
        for item in (pilot or {}).get("not_observed") or []
        if isinstance(item, dict) and item.get("proposal_ref")
    }
    for assessment in assessments:
        proposal_ref = assessment.get("proposal_ref")
        for observation_ref in assessment.get("observation_refs") or []:
            observation = observations.get(observation_ref)
            if observation is None:
                errors.append(
                    f"[provenance] validation-outcome {proposal_ref}.observation_refs={observation_ref!r} does not exist"
                )
            elif observation.get("proposal_ref") != proposal_ref:
                errors.append(
                    f"[provenance] validation-outcome {proposal_ref}.observation_refs={observation_ref!r} "
                    f"belongs to {observation.get('proposal_ref')!r}"
                )
        not_observed_ref = assessment.get("not_observed_ref")
        if not_observed_ref is not None and (
            not_observed_ref != proposal_ref or proposal_ref not in dispositions
        ):
            errors.append(
                f"[provenance] validation-outcome {proposal_ref}.not_observed_ref={not_observed_ref!r} does not resolve"
            )
    recommended = _recommended_validation_decision(assessments)
    if (outcome or {}).get("recommended_decision") != recommended:
        errors.append(
            f"[validation] validation-outcome recommended_decision={(outcome or {}).get('recommended_decision')!r} "
            f"must equal derived {recommended!r}"
        )
    final_decision = (outcome or {}).get("final_decision")
    expected_alignment = "aligned" if final_decision == recommended else "deviated"
    if (outcome or {}).get("decision_alignment") != expected_alignment:
        errors.append(
            f"[validation] validation-outcome decision_alignment must be {expected_alignment!r}"
        )
    return errors


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=list(SCHEMA_FILES))
    ap.add_argument("file", type=Path)
    ap.add_argument("--evidence", type=Path, default=None, help="對應的 evidence.yaml，用來做 referential 檢查")
    ap.add_argument("--service-graph", type=Path, default=None,
                    help="issue/To-be provenance 使用的 canonical service_graph.yaml")
    ap.add_argument("--issue-package", type=Path, default=None,
                    help="To-be provenance 使用的 issue_package.yaml")
    ap.add_argument("--research-catalog", type=Path, default=None,
                    help="To-be provenance 使用的 research_catalog.yaml")
    ap.add_argument("--to-be-bundle", type=Path, default=None,
                    help="pilot/outcome provenance 使用的 to_be_bundle.yaml")
    ap.add_argument("--pilot-record", type=Path, default=None,
                    help="validation outcome 使用的 completed pilot record")
    ap.add_argument("--previous-pilot-record", type=Path, default=None,
                    help="pilot replacement append-only comparison 使用的 predecessor")
    ap.add_argument("--job-map", type=Path, default=None,
                    help="對應的 job_map.yaml，用來做「這則 Job 的階段有沒有在圖上出現過」的候選機會缺口比對（僅 --judgment 時使用）")
    ap.add_argument("--judgment", action="store_true",
                    help="附上判斷判準報告：系統已經判完的，與在等研究者判的，分成兩段")
    ap.add_argument("--gaps", action="store_true",
                    help="附上研究者面向的缺口報告：空白三態統計（待訪清單）＋引用複用次數排行（抽查 triage）")
    args = ap.parse_args()

    if args.kind == "issue-package":
        missing = [
            flag for flag, value in (
                ("--evidence", args.evidence), ("--service-graph", args.service_graph)
            ) if value is None
        ]
        if missing:
            ap.error("issue-package cross-validation requires " + " and ".join(missing))
    if args.kind == "to-be-bundle":
        missing = [
            flag for flag, value in (
                ("--evidence", args.evidence),
                ("--service-graph", args.service_graph),
                ("--issue-package", args.issue_package),
                ("--research-catalog", args.research_catalog),
            ) if value is None
        ]
        if missing:
            ap.error("to-be-bundle cross-validation requires " + ", ".join(missing))
    if args.kind == "pilot-record" and args.to_be_bundle is None:
        ap.error("pilot-record cross-validation requires --to-be-bundle")
    if args.kind == "validation-outcome":
        missing = [
            flag for flag, value in (
                ("--to-be-bundle", args.to_be_bundle),
                ("--pilot-record", args.pilot_record),
            ) if value is None
        ]
        if missing:
            ap.error("validation-outcome cross-validation requires " + ", ".join(missing))

    data = load_yaml(args.file)
    schema = load_schema(args.kind)

    errors = (
        lifecycle.lifecycle_errors(data)
        if args.kind == "artifact-lifecycle"
        else structural_errors(data, schema)
    )
    if args.kind == "case":
        errors += case_errors(data)

    ev = None
    evidence_ids = None
    evidence_texts = None
    evidence_tiers = None
    if args.evidence:
        ev = load_yaml(args.evidence)
        ev_schema = load_schema("evidence")
        errors += [f"(evidence.yaml) {e}" for e in structural_errors(ev, ev_schema)]
        evidence_ids = {item["id"] for item in ev if "id" in item}
        # 離線重跑 sbp validate 時也要能抓過度通則化——Codex review 這次抓到的缺口：
        # 之前只有生成當下的 stages.structure() 會傳 evidence_texts，CLI 這條路
        # 一直沒傳，同一份 draft 事後用 CLI 重驗會靜默漏掉這條檢查。
        evidence_texts = {item["id"]: item.get("text", "") for item in ev if "id" in item}
        # ground-structure-stage-action-boundaries：同一個理由，行為者佐證檢查
        # 也要在離線重驗時跑，不能只在生成當下跑一次。
        evidence_tiers = {item["id"]: item.get("tier") for item in ev if "id" in item}

    dependency_data = {}
    dependency_specs = (
        ("service-graph", args.service_graph, "service-graph"),
        ("issue-package", args.issue_package, "issue-package"),
        ("research-catalog", args.research_catalog, "research-catalog"),
        ("to-be-bundle", args.to_be_bundle, "to-be-bundle"),
        ("pilot-record", args.pilot_record, "pilot-record"),
        ("previous-pilot-record", args.previous_pilot_record, "pilot-record"),
    )
    for label, path, kind in dependency_specs:
        if path is None:
            continue
        dependency = load_yaml(path)
        dependency_data[label] = dependency
        dependency_structural = structural_errors(dependency, load_schema(kind))
        errors += [f"({path.name}) {error}" for error in dependency_structural]
        if kind == "service-graph" and not dependency_structural:
            errors += [
                f"({path.name}) {error}"
                for error in graph_errors(dependency, evidence_ids, evidence_texts)
            ]

    # 同一個 run 目錄下的 journey.yaml 給了才做步驟指派一致性檢查——CLI 離線
    # 重驗跟生成當下的 stages.structure() 要跑同一條規則（決定二）。
    journey = None
    journey_path = args.file.parent / "journey.yaml"
    if journey_path.exists():
        journey = load_yaml(journey_path)

    errors += referential_errors(args.kind, data, evidence_ids)
    if args.kind == "service-graph":
        errors += graph_errors(data, evidence_ids, evidence_texts, journey=journey)
    elif args.kind == "sbp-draft":
        errors += projection_errors(data, evidence_ids, evidence_texts=evidence_texts, journey=journey)
    elif args.kind == "briefing":
        # 獨立 CLI 只拿得到 evidence_ref 可核對的清單（--evidence）；node/conflict/step
        # 存在同一個 run 目錄的其他檔案裡，CLI 單檔呼叫沒有它們，只能略過那三種引用的
        # referential 檢查——跟 stages.py 的 brief() 呼叫端（拿得到完整 run 內容）不一樣。
        errors += briefing_errors(data, evidence_ids)
    elif args.kind == "issue-package" and not errors:
        errors += issue_package_errors(
            data, ev, dependency_data["service-graph"]
        )
    elif args.kind == "research-catalog" and not errors:
        errors += research_catalog_errors(data)
    elif args.kind == "to-be-bundle" and not errors:
        issue_package = dependency_data["issue-package"]
        research_catalog = dependency_data["research-catalog"]
        graph = dependency_data["service-graph"]
        errors += issue_package_errors(issue_package, ev, graph)
        errors += research_catalog_errors(research_catalog)
        errors += to_be_bundle_errors(
            data, ev, graph, issue_package, research_catalog
        )
    elif args.kind == "pilot-record" and not errors:
        errors += pilot_record_errors(
            data,
            dependency_data["to-be-bundle"],
            previous_pilot=dependency_data.get("previous-pilot-record"),
        )
    elif args.kind == "validation-outcome" and not errors:
        errors += validation_outcome_errors(
            data,
            dependency_data["pilot-record"],
            dependency_data["to-be-bundle"],
        )
    errors += marker_errors(args.kind, data)
    warnings = coverage_warnings(args.kind, data)
    if args.kind == "service-graph":
        warnings += graph_warnings(data, evidence_ids, evidence_texts, journey=journey, evidence_tiers=evidence_tiers)

    print(f"== {args.file} ({args.kind}) ==")
    if not errors:
        print(f"✓ 結構／referential 檢查通過")
    for e in errors:
        print(f"✗ {e}")
    for w in warnings:
        print(f"! {w}")
    print(f"結果：{len(errors)} 個錯誤、{len(warnings)} 個警告")

    if args.judgment:
        if args.kind != "service-graph":
            print("\n（判斷判準目前只套用在 service-graph）")
        else:
            job_map_doc = load_yaml(args.job_map) if args.job_map else None
            print()
            for line in judgment_report(data, evidence_ids, evidence_texts,
                                        journey=journey, evidence_tiers=evidence_tiers,
                                        job_map=job_map_doc):
                print(line)

    if args.gaps:
        print()
        for line in gap_report(args.kind, data):
            print(line)

    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
