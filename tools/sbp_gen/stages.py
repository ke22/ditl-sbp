"""三段生成流程：align → compare → build。

ADR-0002 決定一要求三段分開執行，且萃取階段不得帶入步驟軸／層結構。
這裡用資料流強制那條規則：align 與 compare 的 prompt 根本拿不到 STEPS／
LAYERS，不是靠 prompt 裡寫「請不要用」。

ADR-0003 決定一：三段之間不停下來給人審查，人只在頭尾介入。
"""
from __future__ import annotations

import concurrent.futures
import copy
import hashlib
import json
import math
import os
import re
import shutil
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import validate_sbp as V  # noqa: E402
from . import performance_trace as perf
import internal_id_patterns  # noqa: E402

from . import as_is_projection, cli_runner, judgment_criteria, lifecycle, llm, run_files, schema_contract  # noqa: E402

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
CHANNEL_REASONING_PATH = Path(__file__).resolve().with_name("channel_reasoning.md")

# Channel is a shared reasoning lens, not an analysis-chain stage.  Keep the
# canonical text in one fragment and inject it only into the runtime prompts
# that can reason about Journey, As-is, or To-be channel semantics.
CHANNEL_REASONING_STAGES = frozenset({
    # cjm-experience-and-moment-solutions: CJM generation no longer decides
    # Channels; Solution development for selected moments does.
    "journey_solutions",
    "journey_recheck",
    "structure",
    # to-be-solution-carry-through: the shared rule names To-be; the To-be
    # blueprint prompt is where To-be channel work is reasoned about.
    "structure_to_be",
    "as_is_recheck",
    "issues",
    "issue_grounding",
    "to_be_proposals",
})

LAYERS = ["實體證據", "顧客行動", "前台行動", "後台行動", "支援流程"]

# 評估端專用欄位，餵給生成端前要投影掉。
#
# note（ADR-0003 煙霧測試發現一）：研究團隊的分析註解（★矛盾候選、[OPINION] 等）
# 一定要投影掉——送進生成端等於把答案先告訴模型，兩段都不能看到。
#
# role 不投影（ADR-0003 煙霧測試發現三，2026-08-19 改變決定）：
# schema/README.md 原本要求 role 也投影掉，理由是避免洩漏「誰講的」。但逐字稿版
# 證據（evidence_chongzheng_verbatim.yaml）的 text 是第一手引述（「我」「我們」），
# 不是團隊改寫過的第三人稱摘要（「司機反映」）——build 階段把第一人稱引述寫成
# claim 時，本來就需要判斷這是誰的話才能標歸屬（規則四），而 role 正是這個判斷
# 唯一結構化的依據。若把 role 也投影掉，規則四會變成不可能滿足的要求。
# 兩害相權：「不洩漏誰講的」目前沒有實證支持真的造成問題，「規則四可滿足」有。
EVAL_ONLY_FIELDS = ("note",)

MAX_RETRIES = 2
# The line printed when a stage retries because the reply held no JSON. The Workbench's
# local diagnostics count retries from this exact template (sbp_review/diagnostics.py).
RETRY_NOTICE = "  [{stage}] 第 {attempt} 次沒有回傳 JSON，重試"
# This is a budget for the complete align prompt, not merely the raw document.
# Codex receives the stage instructions and the envelope as well as source text.
# Keep headroom below the model context so a large transcript cannot turn one
# first-stage request into a ten-minute-or-longer wait.
#
# real-use validation on the 恆隆行 case (2026-09-29): a first cut at 24_000
# still bounded the *input* correctly (verified: three chunks, all <=24k
# tokens) but the first chunk's align call still hit the CLI runner's default
# 600s per-call timeout (`runner_timed_out`) -- align's wall-clock cost tracks
# the number of evidence units it has to segment out of the chunk, not just
# input size, and a 24k-token slice of dense transcript still asks for a large
# structured output. Lowered to a third of that, which keeps a chunk's
# evidence count (and so its output) proportionally smaller without touching
# the runner timeout itself.
ALIGN_INPUT_TOKEN_BUDGET = 8_000
_ALIGN_BLOCK_SEPARATOR = "\n\n---\n\n"
_ALIGN_USER_PREFIX = "以下是這個案例的原始資料。請依系統指示整理成證據索引。\n\n=== 原始資料開始 ===\n"
_ALIGN_USER_SUFFIX = "\n=== 原始資料結束 ==="
MAX_ISSUE_INPUT_CHARS = 120_000
MAX_ISSUE_OUTPUT_TOKENS = 8_000
MAX_ISSUE_CANDIDATES = 25
TO_BE_ALLOWED_TOP_LEVEL = {
    "direction",
    "components",
    "issue_coverage",
    "blueprint_changes",
}


def _prompt(name: str) -> str:
    prose = (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")
    if name not in CHANNEL_REASONING_STAGES:
        return prose
    shared = CHANNEL_REASONING_PATH.read_text(encoding="utf-8").strip()
    return f"{prose.rstrip()}\n\n{shared}\n"


def _estimate_tokens(text: str) -> int:
    """Estimate tokens conservatively when the exact runtime tokenizer is absent."""
    try:
        import tiktoken
        return len(tiktoken.get_encoding("o200k_base").encode(text))
    except (ImportError, ValueError):
        # UTF-8 bytes / 2 deliberately overestimates CJK-heavy research text.
        return math.ceil(len(text.encode("utf-8")) / 2)


def _align_prompt_tokens(raw_material: str) -> int:
    """Count the complete completion-envelope prompt used by the CLI adapter."""
    return _estimate_tokens(
        "## 系統提示詞\n\n" + _prompt("align")
        + "\n\n## 使用者訊息\n\n" + _ALIGN_USER_PREFIX + raw_material + _ALIGN_USER_SUFFIX + "\n"
    )


def _align_blocks(raw_material: str) -> list[str]:
    """Return ingestion document blocks without changing their headers or order."""
    return [block for block in raw_material.split(_ALIGN_BLOCK_SEPARATOR) if block]


def _split_align_block(block: str) -> list[str]:
    """Split one oversized source only, repeating its source/locator header.

    Ingestion prefixes every block with ``# filename · locator``.  Repeating
    that exact header gives every continuation the same traceable provenance;
    content is partitioned at newline boundaries and never summarized.
    """
    if _align_prompt_tokens(block) <= ALIGN_INPUT_TOKEN_BUDGET:
        return [block]
    header, marker, content = block.partition("\n\n")
    if not marker or not header.startswith("# "):
        # A future ingestion format without a header still must not silently
        # drop text.  Preserve the whole block as labelled source material.
        header, content = "# 原始資料 · 未分段定位", block
    def take_prefix(text: str) -> tuple[str, str]:
        """Take the largest exact character prefix that fits this source."""
        low, high = 1, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if _align_prompt_tokens(header + "\n\n" + text[:middle]) <= ALIGN_INPUT_TOKEN_BUDGET:
                low = middle
            else:
                high = middle - 1
        if _align_prompt_tokens(header + "\n\n" + text[:low]) > ALIGN_INPUT_TOKEN_BUDGET:
            raise llm.LLMError("align 的來源定位本身超過輸入預算，無法安全分段。")
        return text[:low], text[low:]

    pieces, current = [], ""
    for line in content.splitlines(keepends=True):
        candidate = current + line
        if current and _align_prompt_tokens(header + "\n\n" + candidate) > ALIGN_INPUT_TOKEN_BUDGET:
            pieces.append(header + "\n\n" + current)
            current = line
        else:
            current = candidate
        while current and _align_prompt_tokens(header + "\n\n" + current) > ALIGN_INPUT_TOKEN_BUDGET:
            prefix, current = take_prefix(current)
            pieces.append(header + "\n\n" + prefix)
    if current:
        pieces.append(header + "\n\n" + current)
    return pieces or [header + "\n\n" + content]


def _align_chunks(raw_material: str) -> list[str]:
    """Pack full source blocks into bounded prompts, splitting only a source."""
    chunks, current = [], ""
    for block in _align_blocks(raw_material):
        for piece in _split_align_block(block):
            candidate = piece if not current else current + _ALIGN_BLOCK_SEPARATOR + piece
            if current and _align_prompt_tokens(candidate) > ALIGN_INPUT_TOKEN_BUDGET:
                chunks.append(current)
                current = piece
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks or [raw_material]


# 哪些 stage 的機械欄位契約由 schema 產生。`.md` 只留 schema 表達不了的語意判斷
# （什麼算顧客步驟、service_role 與 layer 的對應⋯），必填欄位、禁止額外欄位、
# enum、字數上限都從驗證這份輸出的同一份 schema 讀出來——改 schema 不需要有人
# 記得回頭改散文，那份散文以前只擬合到「已經撞過的錯」。
# A stage whose schema root is a `oneOf` of versions renders its contract from
# the version it generates; journey generation writes schema_version 2.
STAGE_SCHEMA_DEFINITIONS = {"journey": "journey_v2"}

STAGE_SCHEMA_KINDS = {
    # to-be-blueprint-generation: same output schema, its own prose and criteria.
    "structure_to_be": "service-graph",
    "journey": "journey",
    "journey_solutions": "journey-solutions",
    "journey_annotations": "journey-annotation-draft",
    "chart_guides": "chart-guide-draft",
    "selective_themes": "selective-themes-draft",
    "semantic_segments": "semantic-segments-draft",
    "structure": "service-graph",
    "axial_coding": "axial-coding",
    "persona_matrix": "persona-matrix",
    "persona_content": "persona-content",
    "persona_dimension_candidates": "persona-dimension-candidates",
    "jtbd": "jtbd",
    "primary_job_candidates": "primary-job-candidates",
    "job_map": "job-map",
    "how_might_we": "hmw",
    # primary_hmw_candidates left this registry on 2026-10-01: it is a deterministic
    # projection, not a model stage (hmw-framings-from-problem-structures).
    "core_category_candidates": "core-category-candidates",
    "narrative_job_candidates": "narrative-job-candidates-draft",
    # selective_coding_narrative deliberately NOT registered here: this
    # stage's model output is only the three narrative fields
    # (secondary_themes/narrative/key_insights), merged into the existing
    # selective_coding.yaml afterwards -- core_category_id/rationale must
    # not be re-produced. Appending the full selective-coding schema
    # contract (which lists those two as required) would tell the model to
    # regenerate the researcher's already-made choice.
    #
    # open_coding deliberately NOT registered here either, for the same
    # reason: each call's `source` is already known (it's which evidence
    # group the caller is on), so the model is only asked to produce
    # `concepts`; `open_coding()` fills in `source`/`schema_version`/
    # `case_id` itself before validating the merged candidate.
}


def _system(stage: str) -> str:
    """Stage system prompt: hand-written prose, then the two derived contracts.

    Both contracts are derived from the same artifacts that check the output:
    the mechanical one from the validating schema, the judgment one from the
    criteria source. What stays hand-written above them is this stage's task
    framing and worked examples — material specific to the stage, not a rule
    applied to its output.
    """
    prose = _prompt(stage)
    appended = []
    kind = STAGE_SCHEMA_KINDS.get(stage)
    if kind is not None:
        appended.append(schema_contract.render(kind, STAGE_SCHEMA_DEFINITIONS.get(stage)))
    criteria = judgment_criteria.render(stage)
    if criteria:
        appended.append(criteria.rstrip())
    if not appended:
        # A stage with nothing derived to append gets its markdown byte for
        # byte, trailing newline included -- appending nothing must not be
        # distinguishable from not passing through here at all.
        return prose
    return "\n\n".join([prose.rstrip(), *appended])


def _yaml_dump(obj) -> str:
    import yaml

    return yaml.dump(obj, allow_unicode=True, sort_keys=False)


def _to_be_candidate_errors(data: object) -> list[str]:
    """Validate the worker envelope before authoritative normalization.

    IDs and canonical ordering are intentionally absent from this contract;
    those are assigned by the trusted orchestrator in the next boundary.
    """
    if not isinstance(data, dict):
        return ["[structure] To-be candidate 頂層必須是物件"]
    errors: list[str] = []
    extra = sorted(set(data) - TO_BE_ALLOWED_TOP_LEVEL)
    if extra:
        errors.append(f"[authority] candidate 包含禁止欄位：{', '.join(extra)}")
    direction = data.get("direction")
    if not isinstance(direction, dict):
        errors.append("[structure] direction 必須是物件")
    else:
        for field in ("title", "summary"):
            if not isinstance(direction.get(field), str) or not direction[field].strip():
                errors.append(f"[structure] direction.{field} 必須是非空文字")
    components = data.get("components")
    component_keys: set[str] = set()
    if not isinstance(components, list) or not components:
        errors.append("[structure] components 必須是非空陣列")
    else:
        for index, component in enumerate(components):
            if not isinstance(component, dict):
                errors.append(f"[structure] components/{index} 必須是物件")
                continue
            key = component.get("key")
            if not isinstance(key, str) or not key.strip():
                errors.append(f"[structure] components/{index}/key 必須是非空文字")
            elif key in component_keys:
                errors.append(f"[identity] component key={key!r} 重複")
            else:
                component_keys.add(key)
            for field in ("statement", "expected_change"):
                if not isinstance(component.get(field), str) or not component[field].strip():
                    errors.append(f"[structure] components/{index}/{field} 必須是非空文字")
            for field in ("issue_refs", "research_claim_refs", "source_refs", "assumptions", "risks"):
                value = component.get(field)
                if not isinstance(value, list) or not value:
                    errors.append(f"[structure] components/{index}/{field} 必須是非空陣列")
            plan = component.get("validation_plan")
            if not isinstance(plan, dict):
                errors.append(f"[structure] components/{index}/validation_plan 必須是物件")
            else:
                for field in (
                    "target_outcome", "metric", "method", "success_condition",
                    "decision_rule", "pilot_scope",
                ):
                    if not isinstance(plan.get(field), str) or not plan[field].strip():
                        errors.append(
                            f"[structure] components/{index}/validation_plan/{field} 必須是非空文字"
                        )
    coverage = data.get("issue_coverage")
    if not isinstance(coverage, list) or not coverage:
        errors.append("[structure] issue_coverage 必須是非空陣列")
    changes = data.get("blueprint_changes")
    if not isinstance(changes, list) or not changes:
        errors.append("[structure] blueprint_changes 必須是非空陣列")

    forbidden_keys = {
        "tool", "tools", "tool_call", "tool_calls", "command", "commands",
        "review_state", "lifecycle", "publish", "raw_upload", "filesystem",
    }
    forbidden_phrases = (
        "browse the web", "search the web", "execute code", "run shell",
        "read raw file", "change review state", "mark as approved",
        "瀏覽網路", "執行指令", "讀取原始檔", "更改審查狀態",
    )

    def inspect(value: object, path: str = "candidate") -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if str(key).lower() in forbidden_keys:
                    errors.append(f"[authority] {path}/{key} 請求了禁止能力")
                inspect(nested, f"{path}/{key}")
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                inspect(nested, f"{path}/{index}")
        elif isinstance(value, str):
            lowered = value.lower()
            if any(phrase in lowered for phrase in forbidden_phrases):
                errors.append(f"[authority] {path} 包含禁止的工具或狀態指令")

    inspect(data)
    return sorted(set(errors))


def _capped_feedback(errors: list[str]) -> list[str]:
    """First 40 errors, plus every correspondence line so positions stay readable."""
    context = [line for line in errors if line.startswith("[對照]")]
    return [line for line in errors if not line.startswith("[對照]")][:40] + context


def generate_to_be_candidates(
    graph: dict,
    issue_package: dict,
    research_catalog: dict,
    *,
    improvement_goal: str,
    hmw_statements: list[str],
    primary_hmw: dict | None = None,
    primary_target: dict | None = None,
    research_directives: dict | None = None,
    revision_context: dict | None = None,
    prior_bundle: dict | None = None,
    opts: dict | None = None,
    candidate_check=None,
) -> tuple[dict, dict]:
    """Call one bounded Anthropic (or manual-transport) stage with no tool-bearing interface.

    `candidate_check` runs after the worker contract passes; a ValueError it
    raises is repair feedback for the next attempt, anything else propagates.
    """
    options = dict(opts or {})
    provider = options.get("to_be_provider", "anthropic")
    if provider not in ("anthropic", "transport"):
        raise llm.LLMError("to_be stage 只允許設定的 Anthropic route 或手動傳輸")
    max_attempts = int(options.get("to_be_max_attempts", 3))
    if max_attempts < 1 or max_attempts > 5:
        raise llm.LLMError("to_be_max_attempts 必須介於 1 與 5")
    allowed_input = {
        "improvement_goal": " ".join(improvement_goal.split()),
        "confirmed_as_is_graph": graph,
        "grounded_issue_package": issue_package,
        "admitted_research_catalog": research_catalog,
        "hmw_statements": list(hmw_statements),
        "revision_context": dict(revision_context or {"kind": "initial"}),
    }
    if primary_hmw is not None:
        allowed_input["primary_hmw"] = dict(primary_hmw)
    if primary_target is not None:
        allowed_input["primary_target"] = dict(primary_target)
    if research_directives is not None:
        allowed_input["research_directives"] = copy.deepcopy(research_directives)
    # A revision is grounded in the draft it revises; without the content itself
    # the worker only has a version id it cannot resolve.
    if allowed_input["revision_context"].get("kind") == "revision" and prior_bundle:
        allowed_input["prior_to_be_bundle"] = copy.deepcopy(prior_bundle)
    user = (
        "請只根據以下受信任輸入，產生一份完整 To-be candidate JSON。\n\n"
        + json.dumps(allowed_input, ensure_ascii=False, sort_keys=True)
    )
    system = _prompt("to_be_proposals")
    attempts: list[dict] = []
    total_tokens = 0
    total_seconds = 0.0
    conversation = user
    for attempt in range(1, max_attempts + 1):
        try:
            result = llm.call(
                stage="to_be",
                system=system,
                user=conversation,
                model=options.get("to_be_model") or llm.DEFAULT_MODELS["to_be"],
                effort=options.get("to_be_effort") or llm.DEFAULT_EFFORT["to_be"],
                max_tokens=int(options.get("max_tokens", 16_000)),
                provider=provider,
            )
            total_tokens += result.tokens
            total_seconds += result.seconds
            data = llm.extract_json(result.text)
            errors = _to_be_candidate_errors(data)
        except llm.LLMError as exc:
            data = None
            errors = [str(exc)]
        if not errors and candidate_check is not None:
            try:
                candidate_check(data)
            except ValueError as exc:
                errors = [line for line in str(exc).splitlines() if line.strip()]
        attempts.append({"attempt": attempt, "validation_errors": _capped_feedback(errors)})
        if not errors and isinstance(data, dict):
            return data, {
                "tokens": total_tokens,
                "seconds": round(total_seconds, 3),
                "attempts": attempts,
            }
        if attempt < max_attempts:
            shown = "\n".join(f"- {error}" for error in _capped_feedback(errors))
            conversation = (
                f"{user}\n\n---\n上一次完整 JSON 未通過 worker contract 或正規化檢查：\n{shown}\n\n"
                "請修正後重新輸出完整 JSON；不得要求任何工具或狀態變更。"
            )
    raise llm.ValidatedJSONError("to_be bounded attempts exhausted", attempts)


# Worker item keys are the canonical schema's, except that the orchestrator
# assigns identifiers and turns the worker's component_keys into proposal_refs.
TO_BE_ORCHESTRATOR_ITEM_KEYS = {"id", "local_element_id", "proposal_refs"}


def to_be_worker_item_keys() -> dict[str, set[str]]:
    definitions = V.load_schema("to-be-bundle")["definitions"]
    return {
        section: set(definitions[name]["properties"]) - TO_BE_ORCHESTRATOR_ITEM_KEYS
        | {"component_keys"}
        for section, name in (
            ("issue_coverage", "issue_coverage"),
            ("blueprint_changes", "blueprint_change"),
        )
    }


def _to_be_worker_shape_errors(candidate: dict) -> list[str]:
    """Worker fields the canonical mapping would otherwise drop or rewrite silently."""
    errors = []
    for section, allowed in to_be_worker_item_keys().items():
        for index, item in enumerate(candidate.get(section) or []):
            if not isinstance(item, dict):
                continue
            # Coverage is re-ordered by issue during normalization, so name the issue.
            where = (
                f"issue_coverage(issue_ref={item.get('issue_ref')!r})"
                if section == "issue_coverage" else f"{section}/{index}"
            )
            for key in sorted(set(item) - allowed):
                errors.append(
                    f"{where}: Additional properties are not allowed ({key!r} was unexpected)"
                )
            if section == "issue_coverage" and item.get("status") not in ("addressed", "not_addressed"):
                errors.append(
                    f"{where}/status: {item.get('status')!r} is not one of "
                    "['addressed', 'not_addressed']"
                )
    return errors


# A proposal's identity and claim_state are assigned by the orchestrator; the
# rest is the content a retained component has to bring back unchanged. Derived
# from the canonical schema so it cannot drift away from it.
TO_BE_ORCHESTRATOR_PROPOSAL_KEYS = {"id", "claim_state"}


def to_be_proposal_content_fields() -> tuple[str, ...]:
    properties = V.load_schema("to-be-bundle")["definitions"]["proposal"]["properties"]
    return tuple(
        field for field in properties if field not in TO_BE_ORCHESTRATOR_PROPOSAL_KEYS
    )


def _comparable_proposal_content(source: dict, fields: tuple[str, ...]) -> dict:
    return {
        # Reference lists are normalized into sorted sets, so compare them the
        # same way rather than reporting a reordering as a content change.
        field: (
            sorted(set(source.get(field) or []))
            if field.endswith("_refs")
            else source.get(field)
        )
        for field in fields
    }


def _to_be_revision_errors(
    candidate: dict, prior_bundle: dict | None, decisions: list | None
) -> list[str]:
    """Check a revision candidate against the draft and rulings it answers to.

    Proposal ids are reassigned by content order during normalization, so
    whether a base proposal survived is only answerable through the base
    reference each component declares.
    """
    if not prior_bundle:
        return []
    base = {
        str(proposal["id"]): proposal
        for proposal in prior_bundle.get("proposals") or []
        if isinstance(proposal, dict) and isinstance(proposal.get("id"), str)
    }
    ruled = {
        str(item["proposal_ref"]): str(item["decision"])
        for item in decisions or []
        if isinstance(item, dict) and item.get("proposal_ref") and item.get("decision")
    }
    errors: list[str] = []
    claimed: dict[str, str] = {}
    content_fields = to_be_proposal_content_fields()
    for component in candidate.get("components") or []:
        if not isinstance(component, dict):
            continue
        ref = component.get("base_proposal_ref")
        if ref is None:
            continue
        key = component.get("key")
        if not isinstance(ref, str) or ref not in base:
            errors.append(
                f"[修訂] component key={key!r} 的 base_proposal_ref={ref!r} "
                "不在 prior_to_be_bundle.proposals 裡"
            )
            continue
        if ref in claimed:
            errors.append(
                f"[修訂] {ref} 同時被 component key={claimed[ref]!r} 與 key={key!r} 宣告延續，"
                "一個基準提案只能有一個延續者"
            )
            continue
        claimed[ref] = key
        if ruled.get(ref) == "remove":
            errors.append(
                f"[修訂] {ref} 已被裁定 remove，不得有任何 component 延續它；"
                "請改在 issue_coverage 交代它原本處理的問題"
            )
        elif ruled.get(ref) == "retain":
            expected = _comparable_proposal_content(base[ref], content_fields)
            actual = _comparable_proposal_content(component, content_fields)
            changed = [field for field in content_fields
                       if expected[field] != actual[field]]
            if changed:
                errors.append(
                    f"[修訂] {ref} 已被裁定 retain，內容必須與 prior_to_be_bundle 完全相同，"
                    f"但 component key={key!r} 改動了：{', '.join(changed)}"
                )
    required = (
        [ref for ref, decision in ruled.items() if decision in ("retain", "revise")]
        if ruled
        # With no rulings there is nothing to infer from, so nothing may be
        # dropped either: every base proposal has to come back.
        else list(base)
    )
    for ref in required:
        if ref not in claimed:
            reason = f"裁定為 {ruled[ref]}" if ruled else "本次沒有逐提案裁定"
            errors.append(
                f"[修訂] {ref} {reason}，必須有一個 component 以 base_proposal_ref 延續它"
            )
    return errors


def normalize_to_be_candidate(
    candidate: dict,
    *,
    case_id: str,
    scenario_id: str,
    source_as_is_ref: str,
    improvement_goal: str,
    evidence: list,
    graph: dict,
    issue_package: dict,
    research_catalog: dict,
    hmw_statements: list[str] | None = None,
    primary_hmw: dict | None = None,
    primary_target: dict | None = None,
    research_directives: dict | None = None,
    revision_context: dict | None = None,
    prior_bundle: dict | None = None,
    correspondence: dict | None = None,
) -> dict:
    """Turn untrusted worker keys into one deterministic canonical bundle.

    `correspondence`, when given, is filled with the base proposal id -> new
    proposal id mapping of a revision.
    """
    envelope_errors = _to_be_candidate_errors(candidate)
    if envelope_errors:
        raise ValueError("invalid To-be candidate:\n" + "\n".join(envelope_errors))
    if correspondence is not None:
        correspondence.clear()
    issue_order = {
        issue.get("id"): index
        for index, issue in enumerate(issue_package.get("issues") or [])
        if isinstance(issue, dict) and isinstance(issue.get("id"), str)
    }
    changes = [dict(item) for item in candidate.get("blueprint_changes") or []]
    change_order: dict[str, int] = {}
    for index, change in enumerate(changes):
        for key in change.get("component_keys") or []:
            change_order.setdefault(str(key), index)

    # Collapse exact semantic duplicates while retaining every model-authored
    # temporary key as an alias for coverage and blueprint references.
    unique: list[dict] = []
    aliases: dict[str, str] = {}
    fingerprints: dict[str, str] = {}
    # Revision bookkeeping, like `key`: consumed here, never canonical output.
    base_refs: dict[str, str] = {}
    for component in candidate.get("components") or []:
        value = dict(component)
        key = str(value.pop("key"))
        base_ref = value.pop("base_proposal_ref", None)
        # Two components continuing different base proposals stay distinct even
        # when their content matches, so neither lineage is silently collapsed.
        fingerprint = json.dumps(
            [value, base_ref], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        canonical_key = fingerprints.get(fingerprint)
        if canonical_key is None:
            canonical_key = key
            fingerprints[fingerprint] = key
            unique.append({"key": key, **value})
            if isinstance(base_ref, str):
                base_refs[key] = base_ref
        aliases[key] = canonical_key

    def component_sort_key(component: dict) -> tuple:
        key = component["key"]
        issue_position = min(
            (issue_order.get(ref, len(issue_order)) for ref in component.get("issue_refs") or []),
            default=len(issue_order),
        )
        referenced_change = min(
            (
                position
                for alias, position in change_order.items()
                if aliases.get(alias, alias) == key
            ),
            default=len(changes),
        )
        stable_content = json.dumps(
            {name: value for name, value in component.items() if name != "key"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return issue_position, referenced_change, stable_content

    unique.sort(key=component_sort_key)
    proposal_ids = {component["key"]: f"T-{index:03d}" for index, component in enumerate(unique, 1)}

    def map_component_keys(keys: object) -> list[str]:
        if not isinstance(keys, list):
            return []
        mapped = []
        for key in keys:
            canonical_key = aliases.get(str(key), str(key))
            proposal_id = proposal_ids.get(canonical_key)
            if proposal_id and proposal_id not in mapped:
                mapped.append(proposal_id)
        return mapped

    proposals = []
    for component in unique:
        proposal = {
            "id": proposal_ids[component["key"]],
            "claim_state": "proposal",
            "statement": component.get("statement"),
            "expected_change": component.get("expected_change"),
            "issue_refs": sorted(
                set(component.get("issue_refs") or []),
                key=lambda ref: (issue_order.get(ref, len(issue_order)), ref),
            ),
            "research_claim_refs": sorted(set(component.get("research_claim_refs") or [])),
            "source_refs": sorted(set(component.get("source_refs") or [])),
            "assumptions": component.get("assumptions"),
            "risks": component.get("risks"),
            "validation_plan": component.get("validation_plan"),
        }
        proposals.append(proposal)

    coverage_by_issue = {
        item.get("issue_ref"): item
        for item in candidate.get("issue_coverage") or []
        if isinstance(item, dict)
    }
    issue_coverage = []
    for issue_ref in issue_order:
        raw = coverage_by_issue.get(issue_ref, {})
        if raw.get("status") == "addressed":
            issue_coverage.append({
                "issue_ref": issue_ref,
                "status": "addressed",
                "proposal_refs": map_component_keys(raw.get("component_keys")),
            })
        else:
            issue_coverage.append({
                "issue_ref": issue_ref,
                "status": "not_addressed",
                "reason": raw.get("reason"),
                "explanation": raw.get("explanation"),
            })

    blueprint_changes = []
    add_index = 0
    for index, raw in enumerate(changes, 1):
        operation = raw.get("operation")
        change = {
            "id": f"CHANGE-{index:03d}",
            "operation": operation,
            "description": raw.get("description"),
        }
        if operation in ("retain", "modify", "remove"):
            change["as_is_element_ref"] = raw.get("as_is_element_ref")
        if operation in ("modify", "add"):
            change.update({
                "label": raw.get("label"),
                "step": raw.get("step"),
                "layer": raw.get("layer"),
            })
        if operation == "add":
            add_index += 1
            change["local_element_id"] = f"TB-{add_index:03d}"
        if operation != "retain":
            change["proposal_refs"] = map_component_keys(raw.get("component_keys"))
        blueprint_changes.append(change)

    direction = candidate.get("direction") or {}
    bundle = {
        "schema_version": 1,
        "artifact_type": "to_be_bundle",
        "case_id": case_id,
        "scenario_id": scenario_id,
        "source_as_is_ref": source_as_is_ref,
        "improvement_goal": " ".join(improvement_goal.split()),
        "direction": {
            "claim_state": "proposal",
            "title": direction.get("title"),
            "summary": direction.get("summary"),
        },
        "issue_coverage": issue_coverage,
        "revision_context": dict(revision_context or {"kind": "initial"}),
        "proposals": proposals,
        "blueprint_changes": blueprint_changes,
    }
    errors = (
        _to_be_worker_shape_errors(candidate)
        + _to_be_revision_errors(
            candidate,
            prior_bundle,
            (revision_context or {}).get("decisions"),
        )
        + V.to_be_bundle_errors(bundle, evidence, graph, issue_package, research_catalog)
    )
    if errors:
        # Error paths name normalized positions; the worker only knows its own
        # component keys, so the repair feedback carries the correspondence.
        mapping = [
            f"[對照] proposals/{index}（{proposal['id']}）= components key={component['key']!r}"
            for index, (proposal, component) in enumerate(zip(proposals, unique))
        ]
        mapping.extend(
            f"[對照] issue_coverage/{index} = issue_ref={ref!r}"
            for index, ref in enumerate(issue_order)
        )
        mapping.append(
            "[對照] proposal_refs 由你的 component_keys 產生；"
            "blueprint_changes/N 對應你的 blueprint_changes 第 N 項（從 0 起算）"
        )
        raise ValueError(
            "normalized To-be bundle is invalid:\n" + "\n".join(errors + mapping)
        )
    if correspondence is not None:
        correspondence.update(
            {base_refs[key]: proposal_id for key, proposal_id in proposal_ids.items()
             if key in base_refs}
        )
    return bundle


# 觀察到的真實情況（restore-browser-generation-workflow 驗證時連續撞到）：
# structure 這類長時間、高 effort 的呼叫偶爾會被 provider 端直接斷線，
# 不是模型輸出格式錯誤。這種傳輸層錯誤不該吃掉 schema 重試的預算——
# 重打一次通常就過了，跟「模型格式寫錯，要把錯誤餵回去讓它改」是不同問題。
_TRANSIENT_NETWORK_MARKERS = (
    "RemoteProtocolError",
    "ConnectionError",
    "ConnectTimeout",
    "ReadTimeout",
    "ServerDisconnectedError",
    "ServerError",
    "500 INTERNAL",
    "502",
    "503",
    "504",
    # OpenRouter 的免費模型偶爾整批回應是空的（choices 是空陣列）——同一份內容
    # 直接打 API 手動重試會成功，是 provider 端的間歇性問題，不是內容或格式錯誤。
    "回了空結果，沒有 choices",
)

# Groq 的免費 tier 有很緊的每分鐘 token 上限，pipeline 一次跑好幾個階段很容易
# 撞到——但錯誤訊息本身就講了要等幾秒（"Please try again in 34.38s"），照那個
# 秒數等一次再重試，比整段直接判失敗更划算，也比每次呼叫都固定停頓更精準
# （沒撞到限流的呼叫完全不受影響）。
#
# 429（暫時性、這分鐘額度用完）才值得等待重打；413（單次請求本身就超過上限）
# 不管等多久、重打幾次都是同一個結果——兩者共用同一個 `"code":"rate_limit_
# exceeded"` 欄位，只能靠 HTTP 狀態碼本身分辨，不能靠 code 欄位文字。
_RATE_LIMIT_MARKER = "429"
_RATE_LIMIT_WAIT_PATTERN = re.compile(r"try again in (\d+(?:\.\d+)?)s")
# 上限：這個秒數是從 provider 回應的訊息字串解析來的，不是這邊算出來的，
# provider 回報一個異常大的數字（或訊息格式被誤判）就會讓整個 process
# 卡住那麼久——最多再等 max_attempts 次網路重試，不值得為了配合一次 429
# 回應等超過兩分鐘，寧可提早進下一次重試／判失敗。
_MAX_RATE_LIMIT_WAIT_SECONDS = 120.0


def _rate_limit_wait_seconds(message: str) -> float | None:
    if _RATE_LIMIT_MARKER not in message:
        return None
    match = _RATE_LIMIT_WAIT_PATTERN.search(message)
    if not match:
        return None
    # 給 provider 端的計時器一點餘裕，避免卡在剛好還沒重置的那一瞬間。
    return min(float(match.group(1)) + 1.0, _MAX_RATE_LIMIT_WAIT_SECONDS)


def _call_with_network_retry(call_kwargs: dict, *, max_attempts: int = 3) -> llm.CallResult:
    last_exc: llm.LLMError | None = None
    for network_attempt in range(max_attempts):
        try:
            with perf.phase('model_call', network_attempt=network_attempt + 1) as observation:
                result = llm.call(**call_kwargs)
                if observation is not None:
                    observation.update(tokens=result.tokens, runner_reported_seconds=result.seconds,
                                       model_requested=call_kwargs.get('model'),
                                       effort_requested=call_kwargs.get('effort'),
                                       system_characters=len(call_kwargs.get('system') or ''),
                                       user_characters=len(call_kwargs.get('user') or ''))
                return result
        except llm.LLMError as exc:
            last_exc = exc
            message = str(exc)
            wait_seconds = _rate_limit_wait_seconds(message)
            is_rate_limited = _RATE_LIMIT_MARKER in message
            is_transient = is_rate_limited or any(
                marker in message for marker in _TRANSIENT_NETWORK_MARKERS
            )
            if observation is not None:
                observation['failure_kind'] = ('rate_limit' if is_rate_limited else
                                                'transient_error' if is_transient else 'terminal_error')
            if not is_transient:
                raise
            if network_attempt == max_attempts - 1:
                raise
            delay = wait_seconds if wait_seconds is not None else 2.0
            reason = "限流，重打" if is_rate_limited else "傳輸層錯誤，重打"
            print(
                f"  [{call_kwargs['stage']}] {reason}（{network_attempt + 2}/{max_attempts}，等 {delay:.1f}s）：{exc}",
                file=sys.stderr,
            )
            with perf.phase('transport_backoff', reason='rate_limit' if is_rate_limited else 'transient_error'):
                time.sleep(delay)
    raise last_exc  # pragma: no cover - loop always returns or raises above


_DIAGNOSTIC_ITEM_RE = re.compile(r"\w+\[(\d+)\]")


@perf.timed('write')
def _record_attempt_diagnostic(opts: dict, stage: str, attempt: int, max_attempts: int,
                               errors: list, data: object, raw_text: str, call: dict | None = None) -> None:
    """Keep one rejected attempt for diagnosis when `opts["diagnostics_dir"]`
    is set: the full error list, the item each error names, and the model's
    raw reply. Diagnostics only -- never read back, never a research
    artifact, and a write failure never changes the generation outcome."""
    directory = opts.get("diagnostics_dir")
    if not directory:
        return
    label = opts.get("diagnostics_label") or stage
    rejected = []
    for error in errors:
        match = _DIAGNOSTIC_ITEM_RE.search(error)
        index = int(match.group(1)) if match else None
        item = data[index] if isinstance(data, list) and index is not None and index < len(data) else None
        rejected.append({"error": error, "item_index": index,
                         "item": item if isinstance(item, dict) else None})
    record = {"stage": stage, "label": label, "attempt": attempt, "max_attempts": max_attempts,
              "recorded_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
              "error_count": len(errors), "item_count": len(data) if isinstance(data, list) else None,
              "rejected": rejected, "raw_response": raw_text}
    if opts.get("diagnostics_source"):
        record["source"] = opts["diagnostics_source"]
    if call:
        # What this one attempt cost, as the runner reported it (never estimated).
        record["call"] = call
    try:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{label}-attempt-{attempt}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"  [{stage}] 診斷紀錄沒有寫成：{exc}", file=sys.stderr)


REPAIR_CALL_TIMEOUT_SECONDS = 240.0
_ITEM_ERROR_RES = (re.compile(r"\bevidence\[(\d+)\]"), re.compile(r"^\[結構\] (\d+)(?:/|:)"))


def _failed_item_indices(errors: list, count: int) -> set | None:
    """The list positions the errors name, or None when any error is not
    about one item (a duplicate id, a wrong top-level shape, ...)."""
    failed = set()
    for error in errors:
        match = next((m for m in (r.search(error) for r in _ITEM_ERROR_RES) if m), None)
        if match is None or int(match.group(1)) >= count:
            return None
        failed.add(int(match.group(1)))
    return failed


def _merge_repaired_items(base: list, failed: set, reply: object) -> tuple[list, int]:
    """Put repaired items back in their original places, matched by id.
    Items that passed are kept exactly. A failed item the reply does not
    return stays as it was, still failing, so it is asked for again or fails
    the segment -- an omission is never taken as a deletion. Returns
    (merged, number the reply left out)."""
    repaired = {item.get("id"): item for item in reply if isinstance(item, dict)} if isinstance(reply, list) else {}
    merged, left_out = [], 0
    for index, item in enumerate(base):
        if index in failed and isinstance(item, dict) and item.get("id") in repaired:
            merged.append(repaired[item["id"]])
            continue
        if index in failed:
            left_out += 1
        merged.append(item)
    return merged, left_out


def _item_repair_message(user: str, data: list, failed: set, errors: list) -> str:
    """Ask for only the failed items again; the rest is already kept."""
    items = [data[i] for i in sorted(failed)]
    shown = []
    for error in errors[:40]:
        index = _failed_item_indices([error], len(data))
        item = data[next(iter(index))] if index else None
        label = item.get("id") if isinstance(item, dict) else None
        shown.append(f"- {label}：{error}" if label else f"- {error}")
    return (
        f"{user}\n\n---\n\n你上一次整理出 {len(data)} 筆，其中 {len(items)} 筆沒通過檢查；"
        "其餘已經保留，不要重寫。\n\n沒通過的是這幾筆：\n\n"
        f"{json.dumps(items, indent=2, ensure_ascii=False)}\n\n問題如下：\n\n" + "\n".join(shown)
        + "\n\n請只回傳這幾筆修正後的 JSON 陣列，每筆保留原本的 id。"
    )


@perf.timed('retry_prepare')
def _generation_retry_message(*args, **kwargs):
    return llm.build_retry_message(*args, **kwargs)


def _generate(stage: str, system: str, user: str, validate, opts, max_retries: int = MAX_RETRIES,
              item_repair: bool = False, items_key: str | None = None, partial_repair=None) -> tuple:
    """產生 → 驗證 → 把錯誤餵回去重試。回傳 `(data, usage)`。

    usage 累加每一次嘗試的 tokens／seconds，包含驗證失敗被丟回去重寫的
    那幾次——那些呼叫真的花了 token，只算最後成功那次會系統性低估
    （generation-usage-tracking change 設計決定二）。

    `max_retries`：多數階段用模組預設值就夠；`structure` 階段要同時滿足的
    內容一致性規則（relation 連通性、object_refs 對齊、schema 必填欄位⋯）
    比其他階段多很多，同樣 3 次總嘗試次數下更容易在收斂邊緣用完額度就放棄
    ——實測看過同一個 run 在第 2 次嘗試只剩 1 筆錯誤，第 3 次卻因為模型過度
    修正又冒出新錯誤，所以 structure() 呼叫時會傳更高的值。

    重試時錨定在「錯誤筆數最少」的那次嘗試，不是最新一次（fix-structure-
    retry-anchoring 設計決定一）：模型看不到自己上一版寫了什麼，只靠錯誤
    清單重寫整份輸出——清單越短，能參照的資訊就越少，越容易在收斂邊緣退化。

    `item_repair`：輸出是一筆一筆的清單、而且每條錯誤都指得出是哪一筆時，
    已通過的那幾筆原樣保留，下一次只請模型重寫沒過的那幾筆，再合回原位、
    用同一套驗證整份檢查；有任何一條錯誤不屬於單一筆，就照舊整份重寫。
    算在同一個嘗試次數上限裡。

    `items_key`（align-source-coverage）：輸出是一個物件、逐筆的清單放在
    `data[items_key]` 時，逐條修補就作用在那個清單上，物件其他欄位原樣保留；
    沒給時行為不變。

    `partial_repair`（experiment-open-coding-partial-repair）：給了就在驗證失敗後，
    先問它能不能只重做失敗的那幾段（`plan`）。能的話，這次請求仍帶完整的基準
    輸出與錯誤、只要求回傳那幾段的替代內容（`closing`），回來後由程式拼回
    （`splice`）；每次 `_generate` 最多一次，算在同一個嘗試次數上限裡。任何一步
    不成立，就照舊整份重送。沒給時行為不變。
    """
    def items_of(value):
        return value.get(items_key) if items_key and isinstance(value, dict) else value

    convo = user
    last_errors: list[str] = []
    baseline = llm.RepairBaseline()
    total_tokens = 0
    total_seconds = 0.0
    # measure-open-coding-wall-clock-gap：`result.seconds` 是執行器自己回報的
    # 「模型生成花多久」，不含排隊、重打、CLI 冷啟動這些真實牆鐘時間——一支
    # 案例跑了 90 分鐘卻只記錄 15 分鐘模型時間，缺口就是靠這個對照抓出來的。
    # `wall_seconds` 是這支函式自己量的，兩者的差就是「花在等待，不是在算」。
    total_wall_seconds = 0.0
    started_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    executor = os.environ.get("SBP_CLI_EXECUTOR") or "codex"
    # ai-execution-info：執行器自己回報的逐類 token 與要求的模型／effort（是設定，
    # 不是執行器確認實際用了什麼）；沒回報就不寫，不估算。
    token_detail: dict = {}
    model_requested = effort_requested = None
    model_source = model_effective = None
    pending_repair = None  # (previous full output, failed positions)
    left_out_total = 0
    partial_used = False
    pending_partial = None  # (baseline output the repair edits, target segments)
    partial_targets: frozenset = frozenset()
    partial_record = None  # observability only; nothing below reads it back
    partial_key = getattr(partial_repair, "record_key", "targeted_segments")
    for attempt in range(max_retries + 1):
        trace = perf.current()
        if trace:
            trace.begin_attempt(attempt + 1, 'items' if pending_repair is not None else
                                'partial' if pending_partial is not None else 'full')
        call_kwargs = {
            "stage": stage,
            "system": system,
            "user": convo,
            "model": opts.get(f"{stage}_model"),
            "effort": opts.get(f"{stage}_effort"),
            "max_tokens": opts.get("max_tokens", 32000),
        }
        provider = opts.get(f"{stage}_provider") or opts.get("provider")
        if provider:
            call_kwargs["provider"] = provider
        call_started = time.monotonic()
        if pending_repair is not None:
            # Only a few items are rewritten, so a stuck repair is cut sooner.
            with cli_runner.call_timeout(REPAIR_CALL_TIMEOUT_SECONDS):
                with perf.phase('model_wait'):
                    result = _call_with_network_retry(call_kwargs)
        else:
            with perf.phase('model_wait'):
                result = _call_with_network_retry(call_kwargs)
        attempt_wall_seconds = time.monotonic() - call_started
        total_wall_seconds += attempt_wall_seconds
        attempt_call = {"wall_seconds": round(attempt_wall_seconds, 1), "model_seconds": result.seconds,
                        "tokens": result.tokens,
                        **({"token_detail": dict(result.usage)} if getattr(result, "usage", None) else {})}
        total_tokens += result.tokens
        total_seconds += result.seconds
        for kind, count in (getattr(result, "usage", None) or {}).items():
            token_detail[kind] = token_detail.get(kind, 0) + count
        model_requested = getattr(result, "model", None) or model_requested
        effort_requested = getattr(result, "effort", None) or effort_requested
        model_source = getattr(result, "model_source", None) or model_source
        model_effective = getattr(result, "model_effective", None) or model_effective
        try:
            with perf.phase('validation', operation='parse_json'):
                data = llm.extract_json(result.text)
        except llm.LLMError as exc:
            # retry-on-non-json-reply：本機 coding agent 執行器有時會把該填進
            # JSON 欄位的內容寫成給人看的文章（標題、條列），完全沒有 JSON——
            # 這在 extract_json() 裡本來會直接中斷整個 _generate()，一次也不重試
            # 就宣告失敗。跟 schema 驗證失敗一樣值得再打一次，只是沒有可錨定的
            # best_data，用直接的格式提醒取代錯誤清單。
            last_errors = [f"[回覆不是 JSON] {exc}"]
            if trace:
                trace.validation(last_errors)
            _record_attempt_diagnostic(opts, stage, attempt + 1, max_retries + 1, last_errors, None, result.text,
                                       attempt_call)
            if attempt == max_retries:
                break
            print(RETRY_NOTICE.format(stage=stage, attempt=attempt + 1), file=sys.stderr)
            if pending_partial is not None:
                pending_partial = None
                partial_record = _partial_repair_record(partial_targets, "fell_back", "reply_not_json", partial_key)
                anchor_data, anchor_errors = baseline.anchor()
                convo = _generation_retry_message(
                    user, json.dumps(anchor_data, indent=2, ensure_ascii=False), anchor_errors
                )
                continue
            convo = (
                f"{convo}\n\n[提醒] 你上一次的回覆不是有效的 JSON，"
                "這次只能回傳 JSON 本身，不要加任何說明文字、標題或條列。"
            )
            continue
        merged_partial = False
        if pending_partial is not None:
            previous, targets = pending_partial
            pending_partial = None
            merged, reason = partial_repair.splice(previous, targets, data)
            if merged is None:
                if trace:
                    trace.validation([f'partial_repair: {reason}'])
                partial_record = _partial_repair_record(targets, "fell_back", reason, partial_key)
                print(f"  [{stage}] 局部修補的回覆不能用（{reason}），改為整份重送", file=sys.stderr)
                anchor_data, anchor_errors = baseline.anchor()
                convo = _generation_retry_message(
                    user, json.dumps(anchor_data, indent=2, ensure_ascii=False), anchor_errors
                )
                continue
            data = merged
            merged_partial = True
        if pending_repair is not None:
            previous, failed_positions = pending_repair
            merged, left_out = _merge_repaired_items(items_of(previous), failed_positions, data)
            data = {**previous, items_key: merged} if items_key else merged
            pending_repair = None
            if left_out:
                left_out_total += left_out
                print(f"  [{stage}] 修正回覆少了 {left_out} 筆，這幾筆仍算沒通過", file=sys.stderr)
        with perf.phase('validation', operation='validate_output'):
            errors = validate(data)
        if errors and isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
            # Observed with the Gemini provider: JSON mode sometimes wraps a
            # single-object result in a one-element array. Only unwrap when
            # doing so is what actually satisfies the stage's own contract,
            # so a stage whose real output is a one-item array is untouched.
            with perf.phase('validation', operation='validate_unwrapped_output'):
                unwrapped_errors = validate(data[0])
            if not unwrapped_errors:
                data = data[0]
                errors = unwrapped_errors
        if trace:
            trace.validation(errors)
        if merged_partial:
            partial_record = _partial_repair_record(
                partial_targets, "fell_back" if errors else "succeeded",
                "still_failed_after_repair" if errors else None, partial_key)
        if not errors:
            if attempt:
                print(f"  [{stage}] 第 {attempt + 1} 次通過驗證", file=sys.stderr)
            return data, {"tokens": total_tokens, "seconds": round(total_seconds),
                          "wall_seconds": round(total_wall_seconds),
                          "started_at": started_at,
                          "finished_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                          "executor": executor, "stage": stage,
                          "attempts": attempt + 1,
                          "model_requested": model_requested,
                          "effort_requested": effort_requested,
                          **({"model_source": model_source} if model_source else {}),
                          **({"model_effective": model_effective} if model_effective else {}),
                          **({"repair_left_out": left_out_total} if left_out_total else {}),
                          **({"partial_repair": partial_record} if partial_record else {}),
                          **({"token_detail": token_detail} if token_detail else {})}
        last_errors = errors
        items = items_of(data)
        _record_attempt_diagnostic(opts, stage, attempt + 1, max_retries + 1, errors, items, result.text, attempt_call)
        baseline.offer(data, errors)
        failed = _failed_item_indices(errors, len(items)) if item_repair and isinstance(items, list) else None
        if failed and len(failed) < len(items):
            print(f"  [{stage}] 第 {attempt + 1} 次驗證失敗（{len(errors)} 筆），只重寫沒過的 {len(failed)} 筆",
                  file=sys.stderr)
            pending_repair = (data, failed)
            convo = _item_repair_message(user, items, failed, errors)
            continue
        if partial_repair is not None and not partial_used and baseline.complete is not None:
            targets = partial_repair.plan(baseline.complete[0], baseline.complete[1])
            if targets:
                partial_used = True
                partial_targets = targets
                pending_partial = (baseline.complete[0], targets)
                print(f"  [{stage}] 第 {attempt + 1} 次驗證失敗（{len(errors)} 筆），只修補沒過的 {len(targets)} 處",
                      file=sys.stderr)
                convo = _generation_retry_message(
                    user, json.dumps(baseline.complete[0], indent=2, ensure_ascii=False), baseline.complete[1],
                    closing=partial_repair.closing(targets, baseline.complete[1]),
                )
                continue
        print(
            f"  [{stage}] 第 {attempt + 1} 次驗證失敗（{len(errors)} 筆），重試",
            file=sys.stderr,
        )
        anchor_data, anchor_errors = baseline.anchor()
        convo = _generation_retry_message(
            user, json.dumps(anchor_data, indent=2, ensure_ascii=False), anchor_errors
        )
    raise llm.LLMError(
        f"{stage} 連續 {max_retries + 1} 次未通過驗證，最後一次錯誤：\n"
        + "\n".join(last_errors[:20])
    )


# --------------------------------------------------------------------------
# 第一段：萃取
# --------------------------------------------------------------------------
# Interviewer turns are never participant evidence (2026-09-30 ruling).
INTERVIEWER_ROLE_MARKERS = ("組員", "訪員", "訪談者", "主持人", "interviewer")


def _question_context_errors(index: int, item: dict) -> list[str]:
    """A short answer keeps the interviewer's question as `question_context`;
    the question must stay out of `text`, and an interviewer is never the
    evidence's speaker."""
    errs = []
    role = str(item.get("role") or "")
    if any(marker in role.lower() for marker in INTERVIEWER_ROLE_MARKERS):
        errs.append(f"[question_context] evidence[{index}] 的 role 是訪員；訪員的話不是受訪者證據，"
                    "要嘛放進該回答的 question_context，要嘛不收")
    question = str(item.get("question_context") or "").strip()
    if question and question in str(item.get("text") or ""):
        errs.append(f"[question_context] evidence[{index}] 的 text 含有訪員的問題；text 只放受訪者自己說的話")
    return errs


def _evidence_unit(e: dict, *fields: str) -> dict:
    """Model-facing projection of one evidence item. The interviewer question
    travels beside a short answer as labelled context, never inside the
    participant's text."""
    unit = {field: e.get(field) for field in fields}
    if e.get("question_context"):
        unit["question_context"] = e["question_context"]
    return unit


PROGRESS_PREFIX = "[進度]"


def _report_progress(label: str, done: int, total: int, unit: str) -> None:
    """One machine-readable progress line on stderr, e.g. `[進度] 整理原始資料 3/7 段`.
    The workbench shows the latest one beside the processing message."""
    print(f"{PROGRESS_PREFIX} {label} {done}/{total} {unit}", file=sys.stderr, flush=True)


def _evidence_text_entry(e: dict):
    """A text-list projection entry: the participant's words, plus the
    interviewer question beside them when the answer needs it."""
    if e.get("question_context"):
        return {"text": e.get("text"), "question_context": e["question_context"]}
    return e.get("text")


_SUBTITLE_TIME_RE = re.compile(r"^(\d{2}:\d{2}:\d{2})[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}$")


def _compact_subtitles(text: str) -> str:
    """Collapse subtitle (SRT) blocks -- sequence number, timecode, text -- to one
    `[hh:mm:ss] text` line each. Automatic transcripts are mostly this scaffolding
    (81% of the characters in the MUJI 統一時代店 files), which only lengthened
    evidence extraction. The spoken words and a start time for locating them are
    kept; anything that is not a full subtitle block passes through unchanged."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        timecode = _SUBTITLE_TIME_RE.match(lines[i + 1].strip()) if line.isdigit() and i + 1 < len(lines) else None
        if not timecode:
            out.append(lines[i])
            i += 1
            continue
        j, spoken = i + 2, []
        while j < len(lines) and lines[j].strip():
            spoken.append(lines[j].strip())
            j += 1
        if spoken:
            out.append(f"[{timecode.group(1)}] {' '.join(spoken)}")
        # skip the blank line that ends the block
        i = j + 1 if j < len(lines) else j
    return "\n".join(out)


_VERBATIM_IGNORED = re.compile(r"[\s\W_]", re.UNICODE)


def _is_verbatim_excerpt(text: str, source: str) -> bool:
    """The excerpt's characters appear in the source, in order, inside a short
    stretch of it. Deleting a few words (fillers, repeats) is allowed;
    replacing any -- including turning simplified characters into traditional
    ones -- or gathering characters from all over the source is not. Spacing
    and punctuation are ignored."""
    wanted = _VERBATIM_IGNORED.sub("", text or "")
    if not wanted:
        return True
    haystack = _VERBATIM_IGNORED.sub("", source or "")
    limit = max(len(wanted) * 2, len(wanted) + 12)
    start = haystack.find(wanted[0])
    while start != -1:
        position, found = start, True
        for char in wanted[1:]:
            position = haystack.find(char, position + 1, start + limit)
            if position == -1:
                found = False
                break
        if found:
            return True
        start = haystack.find(wanted[0], start + 1)
    return False


# source-scope consistency (2026-10-04): the one definition of which text a
# source name covers, shared by source coverage, the verbatim/source check
# and semantic segmentation. A file's blocks (`# name · …` after the block
# separator) are joined in order; a file that declares original documents
# (`- source: `x``) is split at each declaration, each declared document
# covering its own stretch, and the file name covering only the text before
# its first declaration (when there is any).
_SCOPE_LINE_HEADER_RE = re.compile(r"^# (.+?) · [^\n]*\n\n", re.MULTILINE)
_SCOPE_SEPARATOR = "\n\n---\n\n"


def source_scopes(text: str) -> list[tuple[str, str]]:
    """(source name, its text) in order; a name may repeat when a document
    continues in a later block -- `source_scope_map` joins them."""
    order: list[str] = []
    parts: dict[str, list[str]] = {}
    for block in (text or "").split(_SCOPE_SEPARATOR):
        # a block normally starts with its header; a later header line in
        # the same block starts another file (text before any header is not
        # attributed to any source)
        headers = list(_SCOPE_LINE_HEADER_RE.finditer(block))
        for index, match in enumerate(headers):
            end = headers[index + 1].start() if index + 1 < len(headers) else len(block)
            name = match.group(1)
            if name not in parts:
                parts[name] = []
                order.append(name)
            parts[name].append(block[match.end():end])
    out: list[tuple[str, str]] = []
    for name in order:
        content = "\n".join(parts[name])
        declared = list(_SOURCE_DECLARATION_RE.finditer(content))
        if not declared:
            out.append((name, content))
            continue
        preamble = content[:declared[0].start()]
        if preamble.strip():
            out.append((name, preamble))
        for index, match in enumerate(declared):
            end = declared[index + 1].start() if index + 1 < len(declared) else len(content)
            out.append((match.group(1), content[match.start():end]))
    return out


def source_scope_map(text: str) -> dict[str, str]:
    scopes: dict[str, str] = {}
    for name, scope in source_scopes(text):
        scopes[name] = f"{scopes[name]}\n{scope}" if name in scopes else scope
    return scopes


# align-variant-source-form (researcher ruling 2026-10-04): one-to-one glyph
# variants an excerpt may be matched through. Only these pairs; adding one
# needs a researcher ruling. The stored text always takes the source's glyph.
VARIANT_PAIRS = (("為", "爲"),)
_VARIANTS = {a: b for pair in VARIANT_PAIRS for a, b in (pair, pair[::-1])}


def _variant_source_form(text: str, source: str) -> str | None:
    """The excerpt with its whitelisted variants replaced by the source's own
    glyphs, when the excerpt matches the source -- with the same deletion-only,
    bounded-stretch rule as `_is_verbatim_excerpt`, equating only whitelisted
    pairs -- in exactly one source form. None when no such single form exists."""
    if not text or not any(ch in _VARIANTS for ch in text) or _is_verbatim_excerpt(text, source):
        return None
    wanted = _VERBATIM_IGNORED.sub("", text)
    haystack = _VERBATIM_IGNORED.sub("", source or "")
    limit = max(len(wanted) * 2, len(wanted) + 12)

    def same(a: str, b: str) -> bool:
        return a == b or _VARIANTS.get(a) == b

    forms = set()
    for start, first in enumerate(haystack):
        if not same(wanted[0], first):
            continue
        matched, position = [first], start
        for char in wanted[1:]:
            position = next((k for k in range(position + 1, min(start + limit, len(haystack)))
                             if same(char, haystack[k])), -1)
            if position == -1:
                break
            matched.append(haystack[position])
        if len(matched) == len(wanted):
            forms.add("".join(matched))
    if len(forms) != 1:
        return None
    glyphs = iter(forms.pop())
    restored = "".join(next(glyphs) if not _VERBATIM_IGNORED.fullmatch(ch) else ch for ch in text)
    return restored if _is_verbatim_excerpt(restored, source) else None


_SOURCE_HEADER_RE = re.compile(r"^# (.+?) · ", re.MULTILINE)
# A merged upload declares each original document it contains on its own
# provenance line, e.g. "- source: `訪談逐字稿.pdf`". Only such a line counts;
# a file name mentioned anywhere else in the text does not.
_SOURCE_DECLARATION_RE = re.compile(r"^- source: `([^`\n]+)`[ \t]*$", re.MULTILINE)


_SUBTITLE_MARK_RE = re.compile(r"\[\d{2}:\d{2}:\d{2}\]")


def _carry_source_declarations(chunks: list[str]) -> list[str]:
    """A source split across segments keeps its provenance: a continuation
    piece (same `# file · …` header as the piece before it) that starts with
    text before any `- source:` line gets the declaration that was active at
    the split, right after its header. Original declarations stay where they
    are; text, segment boundaries and segment count are unchanged."""
    out, active, previous = [], {}, None
    for chunk in chunks:
        pieces = []
        for piece in chunk.split(_ALIGN_BLOCK_SEPARATOR):
            header, marker, content = piece.partition("\n\n")
            name = _SOURCE_HEADER_RE.match(header + "\n")
            name = name.group(1) if name else None
            first = _SOURCE_DECLARATION_RE.search(content)
            lead = content[:first.start()] if first else content
            if marker and name is not None and name == previous and active.get(name) and lead.strip():
                piece = f"{header}\n\n- source: `{active[name]}`\n\n{content}"
            declarations = _SOURCE_DECLARATION_RE.findall(content)
            if name is not None and declarations:
                active[name] = declarations[-1]
            previous = name
            pieces.append(piece)
        out.append(_ALIGN_BLOCK_SEPARATOR.join(pieces))
    return out


def _stripped_with_positions(text: str) -> tuple[str, list[int]]:
    kept = [(k, ch) for k, ch in enumerate(text or "") if not _VERBATIM_IGNORED.fullmatch(ch)]
    return "".join(ch for _k, ch in kept), [k for k, _ch in kept]


def _verbatim_mismatch(text: str, source: str) -> tuple[str, str] | None:
    """align-verbatim-mismatch-detail: (what differs, the source around it)
    for an excerpt the verbatim check refused, or None when no single source
    place can be determined -- then nothing is guessed. Message only."""
    import difflib

    wanted, _ = _stripped_with_positions(text)
    haystack, positions = _stripped_with_positions(source)
    if not wanted or not haystack:
        return None
    anchor = difflib.SequenceMatcher(None, wanted, haystack, autojunk=False).find_longest_match(
        0, len(wanted), 0, len(haystack))
    if anchor.size < max(4, len(wanted) // 3):
        return None
    piece = wanted[anchor.a:anchor.a + anchor.size]
    found: list[tuple[str, str]] = []
    start = haystack.find(piece)
    while start != -1 and len(found) < 10:
        lo = max(0, start - anchor.a)
        hi = min(len(haystack), start - anchor.a + len(wanted) * 2)
        window = haystack[lo:hi]
        ops = []
        for op in difflib.SequenceMatcher(None, wanted, window, autojunk=False).get_opcodes():
            # a deletion next to an insertion is one substitution (它 for 他)
            if ops and {ops[-1][0], op[0]} == {"delete", "insert"}:
                prev = ops.pop()
                op = ("replace", min(prev[1], op[1]), max(prev[2], op[2]), min(prev[3], op[3]), max(prev[4], op[4]))
            ops.append(op)
        notes = []
        for tag, i1, i2, j1, j2 in ops:
            if tag == "replace" and j2 - j1 <= 2 * (i2 - i1) + 1:
                notes.append(f"模型寫「{wanted[i1:i2]}」，原稿這個位置是「{window[j1:j2]}」")
            elif tag == "replace":
                # far more source text than the model wrote: not a substitution
                notes.append(f"原稿這裡沒有「{wanted[i1:i2]}」")
            elif tag == "delete":
                notes.append(f"原稿這裡沒有「{wanted[i1:i2]}」")
        equal = [(j1, j2) for tag, _i1, _i2, j1, j2 in ops if tag == "equal"]
        first, last = lo + equal[0][0], lo + equal[-1][1] - 1
        snippet = source[positions[max(0, first - 4)]:positions[min(len(positions) - 1, last + 4)] + 1]
        snippet = " ".join(snippet.split())
        found.append(("；".join(notes) or "字都在原稿裡，但不是同一段連續的話",
                      snippet if len(snippet) <= 80 else snippet[:79] + "…"))
        start = haystack.find(piece, start + 1)
    return found[0] if len({note for note, _snippet in found}) == 1 else None


def _verbatim_sources(chunk: str) -> dict[str, str]:
    """Each source's own text in a chunk, as the verbatim check reads it."""
    return source_scope_map(_SUBTITLE_MARK_RE.sub(" ", _compact_subtitles(chunk)))


def _restore_variant_glyphs(evidence: list, chunk: str) -> dict:
    """Put the source's glyph into excerpts that differ only by a whitelisted
    variant (in place); returns {evidence id: restored text}."""
    by_source = _verbatim_sources(chunk)
    whole = _SUBTITLE_MARK_RE.sub(" ", _compact_subtitles(chunk))
    restored = {}
    for item in evidence:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        form = _variant_source_form(item["text"], by_source.get(item.get("source"), "") if by_source else whole)
        if form is not None:
            item["text"] = form
            restored[item.get("id")] = form
    return restored


def _verbatim_errors(data: object, chunk: str, declared_sources: frozenset = frozenset()) -> list[str]:
    """`declared_sources` are the original documents a merged upload declares
    (see _SOURCE_DECLARATION_RE). An item may name one of them instead of the
    upload's own file name, which keeps the more specific provenance."""
    errors = []
    sources = set(_SOURCE_HEADER_RE.findall(chunk))
    allowed = sources | set(declared_sources)
    # Subtitle numbering and timecodes sit between spoken lines; an excerpt
    # that runs across two lines must not be counted as scattered because of
    # them. This only affects the comparison, not what the model is sent.
    chunk = _SUBTITLE_MARK_RE.sub(" ", _compact_subtitles(chunk))
    # source-scope consistency: an excerpt is compared only against the text
    # its own source covers (source_scopes) -- never the whole merged file.
    # A batch with no source headers at all has one unnamed scope.
    scopes = source_scope_map(chunk)
    for index, item in enumerate(data if isinstance(data, list) else []):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "")
        scope = scopes.get(item.get("source"), "") if scopes else chunk
        if not _is_verbatim_excerpt(text, scope):
            elsewhere = [name for name, other in scopes.items()
                         if name != item.get("source") and _is_verbatim_excerpt(text, other)]
            detail = _verbatim_mismatch(text, scope)
            if elsewhere:
                errors.append(f"[逐字] evidence[{index}] 標記的來源是「{item.get('source')}」，但引言不在這個來源的範圍裡；"
                              f"這段話在「{'、'.join(elsewhere)}」的段落裡——source 要寫成引言真正所在的那份文件")
            elif detail:
                errors.append(f"[逐字] evidence[{index}] 的 text 不是只刪不增地取自原稿：{detail[0]}。"
                              f"原稿附近：「{detail[1]}」——照原稿的字與用詞摘錄，可以刪字，不能換字")
            else:
                errors.append(f"[逐字] evidence[{index}] 的 text 有字不在原始資料裡，也找不到唯一對應的原稿段落"
                              f"（例如簡體改成繁體、換了用詞或改寫）：{str(item.get('text'))[:40]}"
                              "——照原稿的字逐字摘錄，可以刪字，不能換字")
        if sources and item.get("source") not in allowed:
            errors.append(f"[來源] evidence[{index}] 的 source 是「{item.get('source')}」，要寫成原始資料標題裡的檔名："
                          f"{'、'.join(sorted(allowed))} 其中一個")
    return errors


_ALIGN_BATCH_FIELDS = ("started_at", "finished_at", "wall_seconds", "seconds", "tokens", "attempts", "executor",
                       "model_requested", "effort_requested", "model_source", "model_effective",
                       "token_detail", "repair_left_out")
_ALIGN_HEADER_RE = re.compile(r"^# (.+?) · ", re.MULTILINE)


def _align_chunk_transcripts(chunk: str) -> list[str]:
    """The transcripts a batch holds: each `# file · …` header and each
    declared `- source:` inside it, in order, without repeats."""
    names = [m.group(1) for m in _ALIGN_HEADER_RE.finditer(chunk)] + _SOURCE_DECLARATION_RE.findall(chunk)
    found = sorted(((chunk.find(n), n) for n in dict.fromkeys(names)))
    return [name for _pos, name in found]


COVERAGE_STATUSES = ("evidence_present", "no_relevant_evidence")

# default-coding-concurrency-five (accepted 2026-10-04): evidence
# preparation, semantic segmentation and open coding run up to five calls at
# once unless opts or their environment variable say otherwise.
CODING_CONCURRENCY_DEFAULT = 5


def map_largest_first(pool, fn, items: list, size) -> list:
    """`pool.map(fn, items)`, but the largest items are submitted first.

    safe-performance-optimization: per-source calls are independent, so a large
    source submitted last ran alone at the end (恆隆行 open coding: ~190 s with
    four of five slots idle). Only the start order changes -- results come back
    in the original order, and the first failure in that order raises, as
    with pool.map."""
    order = sorted(range(len(items)), key=lambda index: -size(items[index]))
    futures = {index: pool.submit(fn, items[index]) for index in order}
    return [futures[index].result() for index in range(len(items))]


def _align_coverage_units(chunk: str) -> list[str]:
    """align-source-coverage: the sources a batch must account for -- the
    canonical source scopes (source_scopes), the same split semantic
    segmentation makes into transcripts."""
    return list(dict.fromkeys(name for name, _scope in source_scopes(chunk)))


def _align_coverage_errors(reply: object, units: list[str]) -> list[str]:
    """Every source in the batch is accounted for exactly once, and what each
    entry claims agrees with the evidence. Batch-level errors: they never name
    an evidence item, so the whole batch is rewritten."""
    if not isinstance(reply, dict) or not isinstance(reply.get("source_coverage"), list) \
            or not isinstance(reply.get("evidence"), list):
        return ["[來源涵蓋] 回覆要是一個物件，含 source_coverage（每份來源一筆）與 evidence（證據清單）兩個清單"]
    errors: list[str] = []
    seen: dict[str, dict] = {}
    for entry in reply["source_coverage"]:
        source = entry.get("source") if isinstance(entry, dict) else None
        if not isinstance(source, str) or not source:
            errors.append("[來源涵蓋] source_coverage 每一筆都要寫 source")
            continue
        if source in seen:
            errors.append(f"[來源涵蓋] 「{source}」交代了兩次，每份來源只寫一筆")
            continue
        seen[source] = entry
    counts: dict[str, int] = {}
    for item in reply["evidence"]:
        if isinstance(item, dict):
            counts[item.get("source")] = counts.get(item.get("source"), 0) + 1
    missing = [unit for unit in units if unit not in seen]
    if missing:
        errors.append(f"[來源涵蓋] 這一批有來源沒有交代：{'、'.join(missing)}——每份來源都要在 source_coverage 寫一筆，"
                      "有相關內容就整理成證據並標 evidence_present，真的沒有才標 no_relevant_evidence 並寫理由")
    for source, entry in seen.items():
        if source not in units:
            errors.append(f"[來源涵蓋] 「{source}」不在這一批；這一批的來源是：{'、'.join(units)}")
            continue
        status = entry.get("status")
        if status not in COVERAGE_STATUSES:
            errors.append(f"[來源涵蓋] 「{source}」的 status 要是 evidence_present 或 no_relevant_evidence")
        elif status == "evidence_present" and not counts.get(source):
            errors.append(f"[來源涵蓋] 「{source}」標了 evidence_present，但沒有任何一條證據的 source 是它")
        elif status == "no_relevant_evidence":
            if counts.get(source):
                errors.append(f"[來源涵蓋] 「{source}」標了 no_relevant_evidence，卻有 {counts[source]} 條證據")
            if not str(entry.get("reason") or "").strip():
                errors.append(f"[來源涵蓋] 「{source}」標了 no_relevant_evidence，要寫一句理由")
    return errors


def _align_source_codes(units: list[str]) -> dict[str, str]:
    """align-source-codes: a short code per source of the batch, in list order."""
    return {f"S{index}": unit for index, unit in enumerate(units, 1)}


def _align_sources_note(units: list[str]) -> str:
    return ("\n\n這一批的來源（source_coverage 要逐一交代；每條證據與 source_coverage 的 source 都寫左邊的代號）：\n"
            + "\n".join(f"- {code}：{unit}" for code, unit in _align_source_codes(units).items()))


def _restore_source_codes(reply: object, codes: dict[str, str]) -> None:
    """align-source-codes: the model names sources by the batch's codes; the
    program writes the full names back before any check or storage. A full
    name is kept; anything else is left for the existing source checks."""
    if not isinstance(reply, dict):
        return
    for key in ("evidence", "source_coverage"):
        for entry in reply.get(key) if isinstance(reply.get(key), list) else []:
            if isinstance(entry, dict) and entry.get("source") in codes:
                entry["source"] = codes[entry["source"]]


def align(raw_material: str, opts: dict) -> tuple:
    schema = V.load_schema("evidence")

    def validate(data):
        errs = V.structural_errors(data, schema)
        if isinstance(data, list):
            ids = [e.get("id") for e in data if isinstance(e, dict)]
            dupes = {i for i in ids if ids.count(i) > 1}
            errs += [f"[id 重複] {i}" for i in sorted(dupes) if i]
            for i, item in enumerate(data):
                if isinstance(item, dict) and not item.get("content_type"):
                    errs.append(f"[typed evidence] evidence[{i}] 缺少 content_type")
                if isinstance(item, dict):
                    errs += _question_context_errors(i, item)
        return errs

    merged, total_tokens, total_seconds = [], 0, 0.0
    # Subtitle compaction is off by default: in the 2026-09-30 MUJI measurement
    # the fewer, denser chunks it produced put a simplified-character transcript
    # beside traditional ones, and the model kept converting it to traditional
    # even when sent back. Opt in with SBP_ALIGN_COMPACT_SUBTITLES=1.
    compact = (os.environ.get("SBP_ALIGN_COMPACT_SUBTITLES") or "").strip() == "1"
    chunks = _carry_source_declarations(list(_align_chunks(_compact_subtitles(raw_material) if compact else raw_material)))
    # A chunk may continue a document whose declaration line sits in an
    # earlier chunk, so declarations are read from the whole upload.
    declared_sources = frozenset(_SOURCE_DECLARATION_RE.findall(raw_material))
    progress_lock, finished = threading.Lock(), [0]

    def one(numbered: tuple[int, str]) -> tuple[list, dict]:
        number, chunk = numbered
        units = _align_coverage_units(chunk)
        user = _ALIGN_USER_PREFIX + chunk + _ALIGN_USER_SUFFIX + _align_sources_note(units)
        label = f"align-chunk-{number:02d}-of-{len(chunks):02d}"
        transcripts = _align_chunk_transcripts(chunk)
        chunk_opts = {**opts, "diagnostics_label": label, "diagnostics_source": "、".join(transcripts)}
        restored: dict = {}  # evidence id -> restored text

        codes = _align_source_codes(units)

        def validate_reply(reply):
            _restore_source_codes(reply, codes)
            # align-source-coverage: every source accounted for, then the
            # existing per-item checks on the evidence list (same error paths).
            coverage = _align_coverage_errors(reply, units)
            evidence = reply.get("evidence") if isinstance(reply, dict) else None
            if not isinstance(evidence, list):
                return coverage
            # align-variant-source-form: an excerpt that differs only by a
            # whitelisted variant takes the source's glyph before checking.
            restored.update(_restore_variant_glyphs(evidence, chunk))
            return coverage + validate(evidence) + _verbatim_errors(evidence, chunk, declared_sources)

        reply, usage = _generate("align", _prompt("align"), user, validate_reply, chunk_opts,
                                 item_repair=True, items_key="evidence")
        data = reply["evidence"]
        kept = sum(1 for item in data if isinstance(item, dict) and restored.get(item.get("id")) == item.get("text"))
        with progress_lock:
            finished[0] += 1
            _report_progress("整理原始資料", finished[0], len(chunks), "段")
        # Evidence-preparation observability (2026-10-04): what this batch
        # held and what it cost, as the runner reported it; nothing estimated.
        batch = {"batch": label, "transcripts": transcripts,
                 **{key: usage.get(key) for key in _ALIGN_BATCH_FIELDS if usage.get(key) is not None},
                 **({"variant_restored": kept} if kept else {}),
                 # align-source-coverage: how the accepted reply accounted for
                 # each source -- run metadata only, never evidence.yaml.
                 "source_coverage": reply["source_coverage"]}
        return data, {**usage, "batch": batch}

    # Chunks are independent source slices, so up to CODING_CONCURRENCY_DEFAULT run at once
    # (SBP_ALIGN_CONCURRENCY); results are merged in chunk order. A hand-relayed
    # turn is answered one prompt at a time.
    workers = max(1, int(opts.get("align_concurrency") or os.environ.get("SBP_ALIGN_CONCURRENCY")
                         or CODING_CONCURRENCY_DEFAULT))
    if (os.environ.get("SBP_LLM_PROVIDER") or "").strip().lower() == "transport":
        workers = 1
    wall_started = time.monotonic()
    stage_started_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    if len(chunks) > 1 and workers > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(chunks))) as pool:
            results = list(pool.map(one, enumerate(chunks, 1)))
    else:
        results = [one(numbered) for numbered in enumerate(chunks, 1)]
    # Do not write or return a partial canonical artifact: an exception in
    # any chunk leaves this function without a result.
    left_out = 0
    batches, token_detail = [], {}
    for data, usage in results:
        merged.extend(data)
        total_tokens += usage.get("tokens", 0)
        total_seconds += usage.get("seconds", 0)
        left_out += usage.get("repair_left_out", 0)
        batches.append(usage["batch"])
        for kind, count in (usage.get("token_detail") or {}).items():
            token_detail[kind] = token_detail.get(kind, 0) + count

    # Chunk-local model calls all begin their ids at E-001.  Identity is a
    # canonical artifact concern, so renumber deterministically only after all
    # chunks validated; source, locator, role and verbatim text are untouched.
    for index, item in enumerate(merged, 1):
        item["id"] = f"E-{index:03d}"
    errors = validate(merged)
    if errors:
        raise llm.LLMError("align 合併結果未通過驗證：\n" + "\n".join(errors[:20]))
    # How many times a repair reply left a failed item out (observability).
    # Stage wall time, settings and per-batch records sit beside the totals;
    # a value the runner did not report is left out, never estimated.
    observed = {"wall_seconds": round(time.monotonic() - wall_started), "started_at": stage_started_at,
                "finished_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "concurrency": workers, "attempts": sum(b.get("attempts") or 0 for b in batches) or None,
                "executor": next((b["executor"] for b in batches if b.get("executor")), None),
                "model_requested": next((b["model_requested"] for b in batches if b.get("model_requested")), None),
                "effort_requested": next((b["effort_requested"] for b in batches if b.get("effort_requested")), None),
                **{key: value for key in ("model_source", "model_effective")
                   if (value := next((b[key] for b in batches if b.get(key)), None))},
                "token_detail": token_detail or None, "batches": batches}
    return merged, {"tokens": total_tokens, "seconds": round(total_seconds),
                    **({"repair_left_out": left_out} if left_out else {}),
                    **{key: value for key, value in observed.items() if value is not None}}


# --------------------------------------------------------------------------
# 第二段：比對
# --------------------------------------------------------------------------
def compare(evidence: list, opts: dict) -> tuple:
    valid_ids = {e["id"] for e in evidence}

    def validate(data):
        errs = []
        if not isinstance(data, list):
            return ["[結構] 頂層必須是陣列"]
        for i, c in enumerate(data):
            if not isinstance(c, dict):
                errs.append(f"[結構] conflicts[{i}] 必須是物件")
                continue
            for field in ("id", "evidence_refs", "description"):
                if not c.get(field):
                    errs.append(f"[結構] conflicts[{i}] 缺少 {field}")
            refs = c.get("evidence_refs") or []
            if isinstance(refs, list):
                if len(refs) < 2:
                    errs.append(
                        f"[結構] conflicts[{i}] evidence_refs 至少要兩條才構成矛盾"
                    )
                for r in refs:
                    if r not in valid_ids:
                        errs.append(
                            f"[referential] conflicts[{i}] evidence_ref {r} 不存在"
                        )
            if "affected_cells" in c:
                errs.append(
                    f"[結構] conflicts[{i}] 不得含 affected_cells"
                    "——比對階段沒有格子，不該猜"
                )
        return errs

    # 比對階段看得到 role：判斷「同一個角色前後說法不一致」需要它，
    # 而 conflicts 不是受測對象本身（受測對象是 build 產出的圖）。
    # 但 note 不能給——那是研究團隊自己標的分析註解（★矛盾候選等），
    # 送進 compare 等於直接告訴它答案。這是 ADR-0003 煙霧測試發現一
    # 撞出的漏洞：note 曾經完全沒被投影，兩段都看得到。
    user = (
        "以下是這個案例的證據索引。請依系統指示找出彼此矛盾的地方。\n\n"
        f"```yaml\n{_yaml_dump(_project(evidence, fields=('note',)))}```"
    )
    return _generate("compare", _prompt("compare"), user, validate, opts)


# --------------------------------------------------------------------------
# 第三段：建模與推論
# --------------------------------------------------------------------------
def _project(evidence: list, fields: tuple = EVAL_ONLY_FIELDS) -> list:
    """拿掉評估端專用欄位，避免汙染受測對象。

    build 用預設（role + note 都拿掉）；compare 只拿掉 note，保留 role
    （見上方 compare() 的說明）。
    """
    return [{k: v for k, v in e.items() if k not in fields} for e in evidence]


def _researcher_note_block(researcher_note: str | None) -> str:
    """研究者在確認畫面寫的自由文字備註，原封不動帶進 user 訊息，不拆成逐條
    per-role 判斷（collapse-confirm-screen-to-single-note 決定二：畫面③不再
    算角色缺口，這裡也就沒有 stmt／role 可以標了）。

    沒有備註時回傳空字串，讓 user 訊息跟這個欄位存在之前逐字元相同——
    這是 researcher-note-passthrough spec「Structure stage's user message is
    unchanged when no note is given」那條的字面要求，方便用字串相等直接測。
    """
    if not researcher_note or not researcher_note.strip():
        return ""
    return (
        f"\n\n## 研究者指定的內容\n"
        f"以下是研究者在確認畫面寫的修正或補充，不是逐字稿內容，沒有證據來源。"
        f"如果裡面提出了具體的服務動作，把它寫成 claim，`source` 標 `researcher`；"
        f"如果只是一般說明，就用來調整你對這個案例的理解，不要另外造一格。\n\n"
        f"{researcher_note.strip()}"
    )


# --------------------------------------------------------------------------
# Action-graph pipeline (ADR-0006)
# --------------------------------------------------------------------------
#
# 舊版 `build()`／`_build_user_message()`／`tools/sbp_gen/prompts/build.md`
# （35 格逐格判斷式生成，對應舊版 sbp-draft.schema.json 的 cells[].claims 形狀）
# 在這次架構轉型時被拿掉了：`__main__.py` 已經完全改用下面這條
# journey → structure → project 的管線（streamline-sbp-generation-pipeline
# 決定一、二之後，eventize 併入 structure，reference/reconcile 變成
# `sbp-gen reference --run <dir>` 事後才跑的選配子指令，不在關鍵路徑上），
# `build()` 沒有任何呼叫端，而它驗證用的 schema（V.load_schema("sbp-draft")）
# 已經被改寫成要求 graph/expectations/node_refs 的新形狀——不管有沒有人呼叫，
# 舊 build() 現在對現在的 schema 一定驗證失敗。與其留著一個看起來能跑、
# 實際上打不過 schema 的死函式，不如直接拿掉；`_overreach_warnings()`
# 那條「general_pattern 但來源沒有普遍量詞」的機械檢查有實質價值，已經
# 搬進 validate_sbp.py 的 graph_errors()（見該函式對 evidence_texts 的說明），
# 沒有連檢查邏輯一起丟掉。
_SCENARIO_ID_RE = re.compile(r"^scenario-\d{2}$")
_STEP_ID_RE = re.compile(r"^step-\d+-\d+$")


def _generation_required_errors(data: object, schema_kind: str) -> list[str]:
    """Fields a fresh model output must carry, which a stored artifact need not.

    Same reasoning as `_generated_journey_id_errors()` below, one level up:
    `_journey_errors()` and `_validate_graph()` also judge documents read back
    from disk, and stored runs predate these requirements. So the check lives
    here, where the only input is fresh model output, and a violation becomes a
    retry the model has to fix.

    Which fields those are is declared in the schema itself, as
    `x-generation-required` — not as a second list in this file. Subtracting the
    read-schema errors leaves exactly the generation-only ones, so nothing the
    caller already reports is repeated.
    """
    strict = V.structural_errors(data, V.generation_schema(V.load_schema(schema_kind)))
    lenient = set(V.structural_errors(data, V.load_schema(schema_kind)))
    return [error for error in strict if error not in lenient]


def _generated_journey_id_errors(data: object) -> list[str]:
    """Id-convention check for freshly generated journeys only.

    Deliberately *not* a `pattern` in journey.schema.json and *not* part of
    `_journey_errors()`: both of those also judge journeys read back from
    disk, and about half the stored journey documents predate this convention
    (`filter-return`, `S-01`, `SC1-1`, `primary`). Enforcing it there would
    make those runs unopenable in the review app.

    The only input here is fresh model output, so a violation becomes a
    validation error that the retry loop carries back to the model together
    with its previous draft.
    """
    if not isinstance(data, dict) or data.get("schema_version", 2) != 2:
        return []
    errors: list[str] = []
    for scenario in data.get("scenarios") or []:
        if not isinstance(scenario, dict):
            continue
        scenario_id = scenario.get("id")
        if isinstance(scenario_id, str) and not _SCENARIO_ID_RE.match(scenario_id):
            errors.append(
                f"[journey] scenario id {scenario_id!r} 不合慣例，要寫成 scenario-01 這種兩位數序號"
            )
        for step in scenario.get("steps") or []:
            if not isinstance(step, dict):
                continue
            step_id = step.get("id")
            if isinstance(step_id, str) and not _STEP_ID_RE.match(step_id):
                errors.append(
                    f"[journey] step id {step_id!r} 不合慣例，要寫成 step-1-1 這種"
                    "「所屬 scenario 序號-scenario 內順序」格式"
                )
    return errors


def _journey_stage_and_action_errors(scenario: dict, evidence_ids: set[str],
                                     allow_split_stages: bool = False) -> list[str]:
    """Stages group adjacent steps; actions say what the customer did, with evidence.

    The same rules are stated in prompts/journey.md; tests bind the two together.
    """
    errs: list[str] = []
    if not isinstance(scenario, dict):
        return errs
    sid = scenario.get("id")
    steps = [step for step in scenario.get("steps") or [] if isinstance(step, dict)]
    for step in steps:
        label = (step.get("label") or "").strip()
        for action in step.get("customer_actions") or []:
            if not isinstance(action, dict):
                continue
            if (action.get("text") or "").strip() == label:
                errs.append(f"[journey] step {step.get('id')} 的 customer action 只是重複步驟標題")
            for ref in action.get("evidence_refs") or []:
                if ref not in evidence_ids:
                    errs.append(f"[journey] step {step.get('id')} customer action evidence_ref {ref} 不存在")
        # ground-journey-candidate-channel-in-as-is-solution: an As-is
        # Solution is the evidenced current-service delivery its Channel
        # classifies -- never a restated action or step label.
        action_texts = {(a.get("text") or "").strip() for a in step.get("customer_actions") or [] if isinstance(a, dict)}
        for solution in step.get("as_is_solutions") or []:
            if not isinstance(solution, dict):
                continue
            text = (solution.get("text") or "").strip()
            if text and (text == label or text in action_texts):
                errs.append(f"[journey] step {step.get('id')} 的 As-is Solution 只是重複步驟標題或顧客行動")
            for ref in solution.get("evidence_refs") or []:
                if ref not in evidence_ids:
                    errs.append(f"[journey] step {step.get('id')} As-is Solution evidence_ref {ref} 不存在")
            for touchpoint in solution.get("touchpoints") or []:
                for ref in (touchpoint.get("evidence_refs") or []) if isinstance(touchpoint, dict) else []:
                    if ref not in evidence_ids:
                        errs.append(f"[journey] step {step.get('id')} touchpoint evidence_ref {ref} 不存在")
            delivery = solution.get("delivery") if isinstance(solution.get("delivery"), dict) else {}
            for channel, role in delivery.items():
                for ref in (role.get("evidence_refs") or []) if isinstance(role, dict) else []:
                    if ref not in evidence_ids:
                        errs.append(f"[journey] step {step.get('id')} {channel} 管道 evidence_ref {ref} 不存在")
                if isinstance(role, dict) and (role.get("text") or "").strip() == text:
                    errs.append(f"[journey] step {step.get('id')} 的 {channel} 管道只是重抄現行做法")
    stages = [stage for stage in scenario.get("stages") or [] if isinstance(stage, dict)]
    if not stages:
        if any(step.get("stage_id") for step in steps):
            errs.append(f"[journey] scenario {sid} 的步驟指到階段，但沒有 stages")
        return errs
    stage_ids = [stage.get("id") for stage in stages]
    if len(stage_ids) != len(set(stage_ids)):
        errs.append(f"[journey] scenario {sid} stage id 重複")
    if len(steps) > 2 and len(stages) >= len(steps):
        errs.append(f"[journey] scenario {sid} 的階段數不能和步驟一樣多（一步一個階段）")
    seen: list[str] = []
    for step in steps:
        ref = step.get("stage_id")
        if not ref:
            errs.append(f"[journey] step {step.get('id')} 沒有 stage_id")
            continue
        if ref not in stage_ids:
            errs.append(f"[journey] step {step.get('id')} 指到不存在的階段 {ref}")
            continue
        # A generated Journey groups adjacent steps into stages. After a researcher
        # reorders a fresh CJM, each step keeps its own stage (atomic column), so a
        # stage may appear as more than one segment; that is shown, not rewritten.
        if seen and seen[-1] != ref and ref in seen and not allow_split_stages:
            errs.append(f"[journey] 階段 {ref} 的步驟不相鄰")
        if not seen or seen[-1] != ref:
            seen.append(ref)
    for stage in stages:
        if stage.get("id") not in seen:
            errs.append(f"[journey] 階段 {stage.get('id')} 沒有任何步驟")
    return errs


def satisfaction_items(value: object, keep_invalid: bool = False) -> list:
    """A step's qualitative satisfaction as a list (0..N). An older single
    evaluation reads as one item; an old 1-5 level is not an evaluation."""
    items = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
    return items if keep_invalid else [item for item in items if isinstance(item, dict) and "level" not in item]


def _experience_level_value(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, dict) and isinstance(value.get("level"), int):
        return value["level"]
    return None


def _journey_step_is_problematic(step: dict) -> bool:
    """Solutions are a response to a problem, never a default row filler."""
    if any(isinstance(text, str) and text.strip() for text in step.get("pain_points") or []):
        return True
    emotion = _experience_level_value(step.get("emotion"))
    return emotion is not None and emotion <= 2


def solution_candidate_channels(candidate: dict) -> dict[str, list[dict]]:
    """Project the channels AI determined for one Solution during CJM creation.

    Researcher selection is deliberately absent. All candidates retain their
    channel interpretation in the CJM, including unselected candidates.
    """
    candidate_id = str(candidate.get("id") or "")
    channels: dict[str, list[dict]] = {key: [] for key in (
        "people", "physical_environment", "digital_tools", "digital_environments"
    )}
    delivery = candidate.get("delivery") if isinstance(candidate.get("delivery"), dict) else {}
    for channel, roles in delivery.items():
        if channel not in channels:
            continue
        for role in roles if isinstance(roles, list) else [roles]:
            if not isinstance(role, dict) or not str(role.get("text") or "").strip():
                continue
            channels[channel].append({
                "text": str(role["text"]).strip(),
                "basis": role.get("basis"),
                "solution_id": candidate_id,
                "evidence_refs": list(role.get("evidence_refs") or []),
            })
    return {channel: items for channel, items in channels.items() if items}


def journey_uses_solution_selection(journey_data: object) -> bool:
    """True for a new-workflow Journey: its primary scenario carries Solution
    candidates (CJM + selected Solutions -> SBP). Older Journeys follow the
    legacy As-is -> Improvement Goal -> To-be workflow."""
    if not isinstance(journey_data, dict):
        return False
    scenario = next((item for item in journey_data.get("scenarios") or []
                     if isinstance(item, dict) and item.get("id") == journey_data.get("primary_scenario")), {})
    return any("solution_candidates" in step for step in scenario.get("steps") or [] if isinstance(step, dict))


def journey_sbp_handoff(journey_data: dict) -> dict:
    """Pass reviewed CJM context and only selected Solutions to SBP.

    The complete journey remains stored in journey.yaml. This payload is the
    model-facing subset, so an unselected design option cannot turn into an
    SBP action or be mistaken for participant evidence.
    """
    scenario = next((item for item in journey_data.get("scenarios") or []
                     if isinstance(item, dict) and item.get("id") == journey_data.get("primary_scenario")), {})
    steps = []
    for position, step in enumerate(scenario.get("steps") or [], 1):
        if not isinstance(step, dict):
            continue
        selected_ids = set(step.get("selected_solution_ids") or [])
        if "develop_solution" in step and step.get("develop_solution") is not True:
            # A moment the researcher did not select keeps any earlier candidates
            # readable, but nothing from it reaches SBP.
            selected_ids = set()
        selected = []
        for candidate in step.get("solution_candidates") or []:
            if isinstance(candidate, dict) and candidate.get("id") in selected_ids:
                selected.append({
                    "id": candidate.get("id"), "text": candidate.get("text"),
                    "basis": candidate.get("basis"),
                    "evidence_refs": list(candidate.get("evidence_refs") or []),
                    "channels": solution_candidate_channels(candidate),
                    "selection_source": "researcher",
                })
        steps.append({
            "id": step.get("id"), "position": position, "stage_id": step.get("stage_id"),
            "journey": step.get("label"),
            "actions": list(step.get("customer_actions") or []),
            "pain_points": list(step.get("pain_points") or []),
            # 2026-10-02 ruling: emotion is the researcher's reading of the
            # journey, not a blueprint input; it is not handed to SBP.
            "satisfaction": step.get("satisfaction"),
            "evidence_refs": list(step.get("evidence_refs") or []),
            "selected_solutions": selected,
        })
    return {
        "case_id": journey_data.get("case_id"),
        "scenario_id": journey_data.get("primary_scenario"),
        "stages": list(scenario.get("stages") or []),
        "steps": steps,
    }


def _journey_solution_errors(step: dict, evidence_ids: set[str]) -> list[str]:
    errors: list[str] = []
    candidates = [item for item in step.get("solution_candidates") or [] if isinstance(item, dict)]
    if "develop_solution" in step:
        # Moment-selection CJM: the researcher chose which moments develop
        # Solutions. A deselected moment keeps its candidates, adopts none.
        if step.get("develop_solution") is not True and step.get("selected_solution_ids"):
            errors.append(f"[journey] step {step.get('id')} 沒有選這一刻發展解方，不能採用這一刻的解方")
    elif candidates and not _journey_step_is_problematic(step):
        # Older CJMs (Solutions drafted with the CJM) keep the problem-moment rule.
        errors.append(f"[journey] step {step.get('id')} 非問題時刻不得產生 Solution 候選")
    ids = [item.get("id") for item in candidates]
    if len(ids) != len(set(ids)):
        errors.append(f"[journey] step {step.get('id')} Solution candidate id 重複")
    candidate_ids = set(ids)
    for candidate in candidates:
        cid = candidate.get("id")
        text = str(candidate.get("text") or "").strip()
        if not text:
            errors.append(f"[journey] step {step.get('id')} Solution candidate {cid} 缺少文字")
        for ref in candidate.get("evidence_refs") or []:
            if ref not in evidence_ids:
                errors.append(f"[journey] step {step.get('id')} Solution candidate {cid} evidence_ref {ref} 不存在")
        delivery = candidate.get("delivery") if isinstance(candidate.get("delivery"), dict) else {}
        for channel, roles in delivery.items():
            if channel not in {"people", "physical_environment", "digital_tools", "digital_environments"}:
                errors.append(f"[journey] step {step.get('id')} Solution candidate {cid} 管道不受支援：{channel}")
                continue
            for role in roles if isinstance(roles, list) else [roles]:
                if not isinstance(role, dict) or not str(role.get("text") or "").strip():
                    errors.append(f"[journey] step {step.get('id')} Solution candidate {cid} 的 {channel} 缺少交付內容")
                    continue
                for ref in role.get("evidence_refs") or []:
                    if ref not in evidence_ids:
                        errors.append(f"[journey] step {step.get('id')} {channel} evidence_ref {ref} 不存在")
    selected = step.get("selected_solution_ids") or []
    if len(selected) != len(set(selected)):
        errors.append(f"[journey] step {step.get('id')} selected_solution_ids 重複")
    for selected_id in selected:
        if selected_id not in candidate_ids:
            errors.append(f"[journey] step {step.get('id')} 選取了不存在的 Solution {selected_id}")
    return errors


def _journey_errors(data: dict, evidence_ids: set[str], *, require_confirmed=False,
                    allow_split_stages: bool = False) -> list[str]:
    errs = V.structural_errors(data, V.load_schema("journey"))
    if not isinstance(data, dict):
        return errs
    errs += V.journey_identity_topology_errors(data)
    scenarios = data.get("scenarios") or []
    ids = [s.get("id") for s in scenarios if isinstance(s, dict)]
    if data.get("primary_scenario") not in ids:
        errs.append("[journey] primary_scenario 不存在")
    for scenario in scenarios:
        if require_confirmed and scenario.get("status") != "confirmed":
            errs.append(f"[journey] scenario {scenario.get('id')} 尚未 confirmed")
        step_ids = [s.get("id") for s in scenario.get("steps") or []]
        if len(step_ids) != len(set(step_ids)):
            errs.append(f"[journey] scenario {scenario.get('id')} step id 重複")
        for step in scenario.get("steps") or []:
            for ref in step.get("evidence_refs") or []:
                if ref not in evidence_ids:
                    errs.append(f"[journey] step {step.get('id')} evidence_ref {ref} 不存在")
            errs += _journey_solution_errors(step, evidence_ids)
        errs += _journey_stage_and_action_errors(scenario, evidence_ids, allow_split_stages)
    if data.get("schema_version") == 3:
        for branch in data.get("branches") or []:
            if not isinstance(branch, dict):
                continue
            for step in branch.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                for ref in step.get("evidence_refs") or []:
                    if ref not in evidence_ids:
                        errs.append(
                            f"[journey] Branch {branch.get('branch_id')} step {step.get('id')} evidence_ref {ref} 不存在"
                        )
    return errs


def _journey_coding_view(categories: list | None) -> list:
    """build-journey-candidate-after-persona: axial categories as analysis of
    the evidence -- name, description and the evidence ids they rest on. Not a
    citation source: journey references stay evidence ids."""
    view = []
    for category in categories or []:
        if not isinstance(category, dict):
            continue
        item = {k: category.get(k) for k in ("label", "discriminating_basis") if category.get(k)}
        refs = [ref for ref in category.get("evidence_refs") or [] if isinstance(ref, str)]
        if refs:
            item["evidence_refs"] = refs
        if item:
            view.append(item)
    return view


def _cjm_experience_errors(step: dict) -> list[str]:
    """cjm-experience-and-moment-solutions: a fresh primary step is projected
    from its experience reconstruction. These checks are structural only --
    whether the projection reads right is the model's and the researcher's
    judgment, not a keyword match."""
    sid = step.get("id")
    errors: list[str] = []
    experience = step.get("experience")
    if not isinstance(experience, dict):
        errors.append(f"[journey] step {sid} 缺少 experience（先重建這一步的經驗，再寫痛點、情緒、滿意度）")
        experience = {}
    present = lambda key: bool(str(experience.get(key) or "").strip())  # noqa: E731
    if any(isinstance(text, str) and text.strip() for text in step.get("pain_points") or []) and not present("friction"):
        errors.append(f"[journey] step {sid} 有痛點，但 experience 沒有寫卡住的地方（friction）；痛點要從卡住的地方來")
    # 2026-10-02 researcher ruling: no unjudged emotion. Every generated step
    # carries one; without a reaction to ground a direction it is neutral (3).
    emotion = step.get("emotion")
    if not isinstance(emotion, int) or isinstance(emotion, bool) or not 1 <= emotion <= 5:
        errors.append(f"[journey] step {sid} 每一步都要有情緒，是 1 到 5 的整數；看不出方向就寫 3（中性）")
    elif emotion != 3 and not present("reaction"):
        errors.append(f"[journey] step {sid} 情緒不是 3，但 experience 沒有寫反應（reaction）；沒有依據就寫 3（中性）")
    satisfaction = step.get("satisfaction")
    if "satisfaction" in step and not isinstance(satisfaction, (dict, list)):
        errors.append(f"[journey] step {sid} 滿意度要寫成評價與理由（evaluation、reason），不是分數")
    elif any(not isinstance(item, dict) or "level" in item for item in satisfaction_items(satisfaction, keep_invalid=True)):
        errors.append(f"[journey] step {sid} 滿意度要寫成評價與理由（evaluation、reason），不是分數")
    for field in ("solution_candidates", "selected_solution_ids", "develop_solution", "solution_analysis", "solution_basis"):
        if step.get(field):
            errors.append(f"[journey] step {sid} 建立旅程時不提出解方；解方要等研究者選定時刻後另外發展")
            break
    return errors


JOURNEY_TARGET_MIN_PRIMARY_STEPS = 10  # prompt target; not enforced (no invented moments)
JOURNEY_MAX_PRIMARY_STEPS = 15


def journey(evidence: list, case: dict, opts: dict, coding_categories: list | None = None,
            confirmed_persona: dict | None = None, hmw_scope: dict | None = None) -> tuple:
    boundary = {k: case.get(k) for k in ("case_id", "service_type", "customers", "primary_provider", "journey_start", "journey_end") if case.get(k) is not None}
    user = "## Minimal case boundary\n```yaml\n" + _yaml_dump(boundary) + "```\n\n"
    if hmw_scope:
        # The selected HMW says which part of the experience matters for design;
        # it never supplies journey facts and never filters the evidence below.
        user += ("## 研究者選定的 HMW（設計焦點，不是證據，也不篩選證據；旅程事實只從下面的證據來）\n```yaml\n"
                 + _yaml_dump(hmw_scope) + "```\n\n")
    user += "## Typed evidence\n```yaml\n" + _yaml_dump(_project(evidence)) + "```"
    coding = _journey_coding_view(coding_categories)
    if coding:
        user += ("\n\n## Axial coding (analysis of the evidence above; cite evidence ids only, never a category)\n```yaml\n"
                 + _yaml_dump(coding) + "```")
    if confirmed_persona:
        user += ("\n\n## Confirmed Persona and target segment (research scope, not participant evidence)\n```yaml\n"
                 + _yaml_dump(confirmed_persona) + "```")
    evidence_ids = {e["id"] for e in evidence}

    def validate(d):
        # 這兩個欄位是呼叫端在成功後才補上的，不是要求模型自己寫；驗證前先補上，
        # 避免每次都白白燒掉一次重試額度在補這兩個從一開始就不該要求模型猜的欄位
        # （手動傳輸這次真的執行才發現：第一次一定會因為缺這兩個欄位失敗）。
        if isinstance(d, dict):
            d.setdefault("schema_version", 2)
            d.setdefault("case_id", case["case_id"])
            scenarios = d.get("scenarios") or []
        fresh_errors = []
        if isinstance(d, dict):
            # The primary scenario is the confirmed Persona's journey and carries the
            # experience reconstruction and its projection. Other scenarios stay as
            # the read-only 「另一種走法」 (accepted surface, RESEARCH_SURFACE_INVARIANTS).
            # Solutions are developed later, only for moments the researcher selects.
            for scenario in d.get("scenarios") or []:
                if not isinstance(scenario, dict):
                    continue
                primary = scenario.get("id") == d.get("primary_scenario")
                for step in scenario.get("steps") or []:
                    if not isinstance(step, dict):
                        continue
                    if not primary:
                        if step.get("solution_candidates") or step.get("selected_solution_ids"):
                            fresh_errors.append(f"[journey] step {step.get('id')} 另一種走法只列步驟，不提出 Solution")
                        continue
                    if "pain_points" not in step:
                        fresh_errors.append(f"[journey] step {step.get('id')} 缺少 pain_points（沒有痛點就給空陣列）")
                    fresh_errors += _cjm_experience_errors(step)
                    if "as_is_solutions" in step:
                        fresh_errors.append(f"[journey] step {step.get('id')} 新 CJM 不輸出現行做法欄")
            # 2026-10-02 researcher ruling: a newly generated primary journey aims
            # for 10-15 moments and never exceeds JOURNEY_MAX_PRIMARY_STEPS. Fewer
            # than 10 stays allowed when the evidence cannot support more (the
            # prompt asks for 10; nothing is invented to reach it). Generation
            # only -- stored CJMs keep validating whatever their length.
            primary_steps = next((len(s.get("steps") or []) for s in scenarios
                                  if isinstance(s, dict) and s.get("id") == d.get("primary_scenario")), 0)
            if primary_steps > JOURNEY_MAX_PRIMARY_STEPS:
                fresh_errors.append(f"[journey] 主要歷程最多 {JOURNEY_MAX_PRIMARY_STEPS} 步，目前 {primary_steps} 步："
                                    "把最細的相鄰時刻合併，保留研究者最需要看見的那些")
        return (_journey_errors(d, evidence_ids) + _generated_journey_id_errors(d)
                + _generation_required_errors(d, "journey") + fresh_errors)

    data, usage = _generate("journey", _system("journey"), user, validate, opts)
    data["schema_version"] = 2
    data["case_id"] = case["case_id"]
    # Moment selection and Solutions are researcher decisions and a later step;
    # every fresh primary step starts with them explicitly empty, which is also
    # what marks this CJM as the Solution-selection workflow downstream.
    for scenario in data.get("scenarios") or []:
        if not isinstance(scenario, dict) or scenario.get("id") != data.get("primary_scenario"):
            continue
        for step in scenario.get("steps") or []:
            if isinstance(step, dict):
                step.update({"solution_candidates": [], "selected_solution_ids": [], "develop_solution": False})
    return data, usage


# cjm-experience-and-moment-solutions: what a step's Solutions were developed
# from. When any of these change, the Solutions are stale.
# A Solution is fresh while the step content it was formed from is unchanged.
# The fingerprint covers exactly the step fields sent to Solution formation
# (JOURNEY_SOLUTION_STEP_FIELDS minus the id). 2026-10-02 researcher ruling:
# emotion is the researcher's reading of the journey, drawn as the emotion
# curve -- not a Solution input -- so it is in neither list.
# sbp-solution-adoption-and-core-problem: the researcher's core problem (`core_problem`)
# frames the Solution, so rewriting it makes an adopted Solution stale. Steps
# without it hash exactly as before.
JOURNEY_STEP_DIGEST_FIELDS = ("label", "description", "stage_id", "customer_actions", "pain_points",
                              "satisfaction", "experience", "core_problem")
# Solutions adopted before that ruling saved a fingerprint that also covered
# emotion; they stay fresh whatever the emotion was or is now.
_LEGACY_EMOTION_LEVELS = (None, 1, 2, 3, 4, 5)


def _step_digest(step: dict, fields: tuple) -> str:
    view = {field: step.get(field) for field in fields if step.get(field) is not None}
    view["customer_actions"] = [item.get("text") for item in view.get("customer_actions") or []
                                if isinstance(item, dict)]
    payload = json.dumps(view, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def journey_step_digest(step: dict) -> str:
    return _step_digest(step, JOURNEY_STEP_DIGEST_FIELDS)


def _legacy_step_digests(step: dict) -> set[str]:
    fields = JOURNEY_STEP_DIGEST_FIELDS + ("emotion",)
    return {_step_digest({**step, "emotion": level}, fields) for level in _LEGACY_EMOTION_LEVELS}


def journey_step_solutions_stale(step: dict) -> bool:
    basis = step.get("solution_basis")
    if not basis or not step.get("solution_candidates"):
        return False
    return basis != journey_step_digest(step) and basis not in _legacy_step_digests(step)


def _primary_steps(journey_data: dict) -> list[dict]:
    scenario = next((item for item in journey_data.get("scenarios") or []
                     if isinstance(item, dict) and item.get("id") == journey_data.get("primary_scenario")), {})
    return [step for step in scenario.get("steps") or [] if isinstance(step, dict)]


def selected_solution_moments(journey_data: dict) -> list[str]:
    return [step["id"] for step in _primary_steps(journey_data) if step.get("develop_solution") is True]


JOURNEY_SOLUTION_STEP_FIELDS = ("id", "label", "description", "stage_id", "customer_actions", "experience",
                                "pain_points", "satisfaction", "core_problem")


def _journey_solutions_user_message(journey_data: dict, moment_ids: list[str], evidence: list,
                                    confirmed_persona: dict | None, hmw_scope: dict | None) -> str:
    steps = _primary_steps(journey_data)
    scenario = next((item for item in journey_data.get("scenarios") or []
                     if isinstance(item, dict) and item.get("id") == journey_data.get("primary_scenario")), {})
    journey_view = {
        "stages": [{"id": st.get("id"), "label": st.get("label")} for st in scenario.get("stages") or []
                   if isinstance(st, dict)],
        "steps": [{k: step.get(k) for k in JOURNEY_SOLUTION_STEP_FIELDS if step.get(k) not in (None, [], "")}
                  for step in steps],
    }
    cited: list[str] = []
    for step in steps:
        cited += list(step.get("evidence_refs") or [])
        cited += [ref for a in step.get("customer_actions") or [] if isinstance(a, dict) for ref in a.get("evidence_refs") or []]
        cited += list((step.get("experience") or {}).get("evidence_refs") or [])
        for item in satisfaction_items(step.get("satisfaction")):
            cited += list(item.get("evidence_refs") or [])
    wanted = set(cited)
    relevant = [item for item in evidence if isinstance(item, dict) and item.get("id") in wanted]
    user = ""
    if hmw_scope:
        user += ("## 研究者選定的 HMW（設計焦點，不是證據）\n```yaml\n" + _yaml_dump(hmw_scope) + "```\n\n")
    if confirmed_persona:
        user += ("## 確認的 Persona（看事情的角度，不是證據）\n```yaml\n" + _yaml_dump(confirmed_persona) + "```\n\n")
    user += ("## 已經過研究者檢視的顧客旅程（主要歷程）\n```yaml\n" + _yaml_dump(journey_view) + "```\n\n"
             "## 研究者選定要發展解方的時刻（只對這些 step_id 輸出，一個都不能少，也不能多）\n```yaml\n"
             + _yaml_dump(moment_ids) + "```\n\n"
             "## 旅程引用到的證據原文\n```yaml\n" + _yaml_dump(_project(relevant)) + "```")
    return user


# A Solution is one declarative sentence: no second sentence, semicolon or
# line break before its final full stop. Researcher edits are not checked.
_SENTENCE_BREAK_RE = re.compile(r"[。！？!?；;\n]")
# 2026-10-02: the Solution is one concrete proposal -- what is done, where,
# to what extent -- in at most 50 characters, with no outcome tail.
SOLUTION_TEXT_MAX_CHARS = 50
_SOLUTION_OUTCOME_TAIL_RE = re.compile(r"[，,、]\s*(?:好讓|讓|使得|使|以便|以利|藉此)")


def _journey_solutions_errors(data: object, moment_ids: list[str], steps_by_id: dict, evidence_ids: set[str]) -> list[str]:
    errors = V.structural_errors(data, V.load_schema("journey-solutions"))
    if not isinstance(data, dict):
        return errors
    returned = [m.get("step_id") for m in data.get("moments") or [] if isinstance(m, dict)]
    if sorted(returned) != sorted(moment_ids):
        errors.append(f"[journey_solutions] 要恰好針對研究者選定的時刻 {moment_ids} 各輸出一筆，實際是 {returned}")
    for moment in data.get("moments") or []:
        if not isinstance(moment, dict):
            continue
        step = steps_by_id.get(moment.get("step_id")) or {}
        pains = {str(text).strip() for text in step.get("pain_points") or [] if str(text).strip()}
        # cjm-v1-delta-one-solution: one primary Solution per selected moment.
        for solution in [moment.get("solution")]:
            if not isinstance(solution, dict):
                continue
            text = str(solution.get("text") or "").strip()
            if text in pains:
                errors.append(f"[journey_solutions] {moment.get('step_id')} 有解方只是照抄痛點；解方要寫服務怎麼介入")
            if _SENTENCE_BREAK_RE.search(text.rstrip("。.")):
                errors.append(f"[journey_solutions] {moment.get('step_id')} 的解方要寫成一句話，"
                              "只在句尾用一個句號，不用分號、不接第二句")
            if len(text.rstrip("。.")) > SOLUTION_TEXT_MAX_CHARS:
                errors.append(f"[journey_solutions] {moment.get('step_id')} 的解方超過 {SOLUTION_TEXT_MAX_CHARS} 字："
                              "只寫一句具體做法，誰來做與分工寫進管道")
            if _SOLUTION_OUTCOME_TAIL_RE.search(text):
                errors.append(f"[journey_solutions] {moment.get('step_id')} 的解方只寫具體做法，"
                              "不要加「讓⋯能⋯」「以便⋯」這種結果尾句")
            refs = list(solution.get("evidence_refs") or [])
            for roles in (solution.get("delivery") or {}).values():
                for role in roles if isinstance(roles, list) else []:
                    if isinstance(role, dict):
                        refs += list(role.get("evidence_refs") or [])
            for ref in refs:
                if ref not in evidence_ids:
                    errors.append(f"[journey_solutions] {moment.get('step_id')} 引用了不存在的證據 {ref}")
    return errors


def journey_solutions(journey_data: dict, evidence: list, opts: dict, confirmed_persona: dict | None = None,
                      hmw_scope: dict | None = None) -> tuple:
    """Develop Solutions only for the moments the researcher selected."""
    moment_ids = selected_solution_moments(journey_data)
    if not moment_ids:
        raise ValueError("研究者還沒有選任何要發展解方的時刻。")
    steps_by_id = {step["id"]: step for step in _primary_steps(journey_data) if step.get("id")}
    evidence_ids = {item["id"] for item in evidence if isinstance(item, dict) and item.get("id")}
    user = _journey_solutions_user_message(journey_data, moment_ids, evidence, confirmed_persona, hmw_scope)
    return _generate("journey_solutions", _system("journey_solutions"), user,
                     lambda d: _journey_solutions_errors(d, moment_ids, steps_by_id, evidence_ids), opts)


def apply_journey_solutions(journey_data: dict, output: dict) -> dict:
    """Write developed Solutions back onto their steps. The step's earlier
    candidates are replaced; the new Solution is adopted, because checking
    發展解方 is the researcher's adoption (2026-10-02 ruling). Ids are
    assigned here, never by the model."""
    result = copy.deepcopy(journey_data)
    by_step = {m["step_id"]: m for m in output.get("moments") or [] if isinstance(m, dict)}
    for step in _primary_steps(result):
        moment = by_step.get(step.get("id"))
        if moment is None:
            continue
        step["solution_analysis"] = copy.deepcopy(moment["analysis"])
        # The researcher's own wording of the core problem stays the problem the
        # Solution answers; the model never replaces it.
        if str(step.get("core_problem") or "").strip():
            step["solution_analysis"]["problem"] = step["core_problem"].strip()
        solution = moment["solution"]
        step["solution_candidates"] = [
            {"id": f"{step['id']}-SOL-1", "text": solution["text"],
             "evidence_refs": list(solution.get("evidence_refs") or []),
             "basis": solution["basis"], "delivery": copy.deepcopy(solution["delivery"])}
        ]
        step["selected_solution_ids"] = [f"{step['id']}-SOL-1"]
        step["solution_basis"] = journey_step_digest(step)
    return result


ANNOTATION_DRAFT_FIELDS = ("emotion", "satisfaction", "solutions")


def journey_annotation_draft(steps: list, evidence: list, pain_points: dict, opts: dict) -> tuple:
    """Draft emotion, satisfaction and solutions for every confirmed step."""
    step_ids = [step["id"] for step in steps]
    evidence_ids = {item["id"] for item in evidence}
    view = [{"id": step["id"], "label": step.get("label"), "description": step.get("description"),
             "evidence_refs": step.get("evidence_refs") or [], "pain_points": pain_points.get(step["id"]) or []}
            for step in steps]
    user = ("## Confirmed journey steps\n```yaml\n" + _yaml_dump(view) + "```\n\n## Typed evidence\n```yaml\n"
            + _yaml_dump(_project(evidence)) + "```")

    def validate(data):
        errs = V.structural_errors(data, V.load_schema("journey-annotation-draft"))
        if not isinstance(data, dict):
            return errs
        drafted = [item.get("step_id") for item in data.get("steps") or [] if isinstance(item, dict)]
        if sorted(drafted) != sorted(step_ids):
            errs.append("[journey_annotations] 每個步驟要恰好一筆，不能新增或漏掉")
        for item in data.get("steps") or []:
            if not isinstance(item, dict):
                continue
            for field in ANNOTATION_DRAFT_FIELDS:
                entry = item.get(field) or {}
                level = entry.get("level")
                if field != "solutions" and not (isinstance(level, int) and 1 <= level <= 5):
                    errs.append(f"[journey_annotations] {item.get('step_id')} {field} 等級要是 1 到 5")
                if field == "solutions" and len(entry.get("texts") or []) > 3:
                    errs.append(f"[journey_annotations] {item.get('step_id')} solutions 最多 3 條")
                for ref in entry.get("evidence_refs") or []:
                    if ref not in evidence_ids:
                        errs.append(f"[journey_annotations] {item.get('step_id')} {field} evidence_ref {ref} 不存在")
        return errs

    return _generate("journey_annotations", _system("journey_annotations"), user, validate, opts)


def merge_annotation_draft(document: dict, draft: dict, now: str) -> dict:
    """Fill absent or still-drafted fields; a researcher's field is never replaced."""
    merged = {"schema_version": 1, "steps": {k: dict(v) for k, v in (document.get("steps") or {}).items()}}
    for item in draft.get("steps") or []:
        entry = merged["steps"].setdefault(item["step_id"], {})
        for field in ANNOTATION_DRAFT_FIELDS:
            current = entry.get(field)
            if current and current.get("source") == "researcher":
                continue
            source = item[field]
            value = {"texts": [t.strip() for t in source["texts"] if t.strip()]} if field == "solutions" else {"level": source["level"]}
            entry[field] = {**value, "source": "ai-draft", "updated_at": now,
                            "basis": source["basis"], "evidence_refs": list(source["evidence_refs"])}
    return merged


CHART_GUIDE_QUESTIONS = {
    "persona": ("Persona Matrix 與 Persona",
                ["這群人是誰", "為什麼選這群（Matrix 上的兩條軸與選定的那一格）", "這群人的核心目標、挑戰與需求",
                 "這對後面的顧客旅程與解方代表什麼"]),
    "journey": ("顧客旅程圖",
                ["這段旅程整體的特徵", "最關鍵的幾個時刻", "主要痛點與情緒、滿意度的轉折",
                 "哪些時刻被選去發展解方，為什麼是這幾個"]),
    "blueprint": ("服務藍圖",
                  ["這份藍圖的設計重點", "哪些服務接點被改動", "前台、後台與支援怎麼配合", "這份藍圖想改善什麼問題"]),
}


def chart_guide_request(kind: str) -> str:
    """「這次的要求」: the questions for this kind of chart and the length rules,
    read from the same table the validator uses."""
    from . import chart_guides as G
    title, questions = CHART_GUIDE_QUESTIONS[kind]
    rules = G.LENGTH_RULES[kind]
    low, high = rules["ask"]
    fewest, most = rules["points"]
    points = f"恰好 {fewest} 點" if fewest == most else f"{fewest}–{most} 點"
    lines = [f"## 這次的要求：{title}的導讀", "", "導讀要回答："]
    lines += [f"- {q}" for q in questions]
    lines += ["", f"- 摘要：全部句子加起來 {low}–{high} 字。",
              f"- 重點：{points}，每點最多 {G.POINT_MAX_CHARS} 字。",
              "- 每一句摘要、每一點重點都要有 evidence_refs，只能用下方內容裡出現過的 evidence id。"]
    return "\n".join(lines)


def draft_chart_guide(run_dir, kind: str, opts: dict) -> tuple:
    """Draft one chart guide from that station's read-only projection.
    Three attempts at most; nothing is written here."""
    from . import chart_guides as G
    projection = G.input_projection(run_dir, kind)
    if projection is None:
        raise llm.LLMError(f"chart_guides：這一站還沒有可以讀的內容（{kind}）")
    user = (chart_guide_request(kind) + "\n\n## 這張圖的內容\n```yaml\n" + _yaml_dump(projection) + "```")
    schema = V.load_schema("chart-guide-draft")

    def validate(data):
        errs = V.structural_errors(data, schema)
        return errs or G.guide_errors(data, projection, kind)

    data, usage = _generate("chart_guides", _system("chart_guides"), user, validate, opts, max_retries=2)
    return data, usage, G.projection_digest(projection)


def selective_themes(view: dict, opts: dict) -> tuple:
    """add-selective-coding-layer: form core and secondary selective themes
    from the axial themes in relation to the research purpose. `view` is
    `selective_themes.structure_input(run_dir)`; nothing is written here."""
    from . import selective_themes as ST
    user = (f"## 研究目的\n{view['research_purpose']}\n\n"
            "## 主軸主題\n```yaml\n" + _yaml_dump(view["axial_themes"]) + "```\n\n"
            "## 主軸主題引用的開放編碼\n```yaml\n" + _yaml_dump(view["open_codes"]) + "```\n\n"
            f"## 主題形成說明\n{view.get('formation_note') or '（沒有）'}")
    schema = V.load_schema("selective-themes-draft")

    def validate(data):
        return V.structural_errors(data, schema) or ST.theme_errors(data, view)

    return _generate("selective_themes", _system("selective_themes"), user, validate, opts)


def _recheck_source_units(raw_material: str, source_manifest: dict) -> dict:
    """Project ingested case material into file-local validation units."""
    filenames = {
        source.get("filename")
        for source in (source_manifest or {}).get("sources") or []
        if isinstance(source, dict) and source.get("filename")
    }
    units = {}
    for block in raw_material.split("\n\n---\n\n"):
        header, _, body = block.partition("\n")
        if not header.startswith("# "):
            continue
        source_and_locator = header[2:].split(" · ", 1)
        filename = source_and_locator[0].strip()
        locator = source_and_locator[1].strip() if len(source_and_locator) == 2 else "*"
        if filename not in filenames:
            continue
        content = body.strip()
        units[(filename, locator)] = content
        units[(filename, "*")] = (units.get((filename, "*"), "") + "\n" + content).strip()
    return units


def _canonicalize_recheck_result(
    data: dict,
    *,
    case_id: str,
    request_digest: str,
    baseline_digest: str,
    edited_digest: str,
    attempt_trace: list,
) -> None:
    """Apply orchestrator-owned fields before validating model content."""
    if not isinstance(data, dict):
        return
    data.update({
        "schema_version": 1,
        "case_id": case_id,
        "state": "awaiting_confirmation",
        "request_digest": request_digest,
        "baseline_editable_digest": baseline_digest,
        "edited_editable_digest": edited_digest,
    })
    journey_data = data.get("reconciled_journey")
    if isinstance(journey_data, dict):
        journey_data.setdefault("schema_version", 2)
        journey_data["case_id"] = case_id
        for scenario in journey_data.get("scenarios") or []:
            if isinstance(scenario, dict):
                scenario["status"] = "proposed"
    data["attempt_trace"] = attempt_trace


def journey_recheck_digests(
    raw_material: str,
    evidence: list,
    baseline_journey: dict,
    edited_journey: dict,
    case_id: str,
    *,
    presentation_only_step_ids: set[str] | None = None,
    include_projection_metadata: bool = False,
) -> dict[str, str | bool]:
    """Return the server-owned identities for one bounded recheck request."""
    baseline_digest = V.editable_journey_digest(baseline_journey)
    edited_digest = V.editable_journey_digest(edited_journey)
    baseline_projection_digest = as_is_projection.as_is_input_projection_digest(
        baseline_journey,
        presentation_only_step_ids=presentation_only_step_ids or (),
    )
    edited_projection_digest = as_is_projection.as_is_input_projection_digest(
        edited_journey,
        presentation_only_step_ids=presentation_only_step_ids or (),
    )
    request_digest = lifecycle.canonical_content_digest({
        "case_id": case_id,
        "baseline_editable_digest": baseline_digest,
        "edited_editable_digest": edited_digest,
        "baseline_as_is_input_projection_digest": baseline_projection_digest,
        "edited_as_is_input_projection_digest": edited_projection_digest,
        "raw_material_digest": lifecycle.canonical_content_digest(raw_material),
        "evidence_digest": lifecycle.canonical_content_digest(evidence),
    })
    result: dict[str, str | bool] = {
        "request_digest": request_digest,
        "baseline_editable_digest": baseline_digest,
        "edited_editable_digest": edited_digest,
    }
    if include_projection_metadata:
        result.update({
            "baseline_as_is_input_projection_digest": baseline_projection_digest,
            "edited_as_is_input_projection_digest": edited_projection_digest,
            "as_is_input_projection_changed": baseline_projection_digest != edited_projection_digest,
        })
    return result


def journey_recheck(
    raw_material: str,
    source_manifest: dict,
    evidence: list,
    baseline_journey: dict,
    edited_journey: dict,
    case: dict,
    opts: dict,
) -> tuple:
    """Reconcile an edited journey against one selected case and stop."""
    case_id = case.get("case_id")
    if not case_id or baseline_journey.get("case_id") != case_id or edited_journey.get("case_id") != case_id:
        raise ValueError("journey recheck 的 case_id 必須與選定案例一致")
    digests = journey_recheck_digests(
        raw_material, evidence, baseline_journey, edited_journey, case_id
    )
    allowed_case = {
        key: case.get(key)
        for key in ("case_id", "service_type", "customers", "primary_provider", "journey_start", "journey_end")
        if case.get(key) is not None
    }
    user = (
        "## Selected case boundary\n```yaml\n" + _yaml_dump(allowed_case) + "```\n\n"
        "## Source manifest\n```yaml\n" + _yaml_dump(source_manifest) + "```\n\n"
        "## Complete extracted raw material\n" + raw_material + "\n\n"
        "## Current evidence\n```yaml\n" + _yaml_dump(_project(evidence)) + "```\n\n"
        "## Server baseline journey\n```yaml\n" + _yaml_dump(baseline_journey) + "```\n\n"
        "## Latest researcher-edited journey\n```yaml\n" + _yaml_dump(edited_journey) + "```"
    )
    schema = V.load_schema("journey-recheck")
    source_units = _recheck_source_units(raw_material, source_manifest)
    prior_ids = {item.get("id") for item in evidence if isinstance(item, dict) and item.get("id")}

    def canonicalize(data, attempt_trace):
        _canonicalize_recheck_result(
            data,
            case_id=case_id,
            request_digest=digests["request_digest"],
            baseline_digest=digests["baseline_editable_digest"],
            edited_digest=digests["edited_editable_digest"],
            attempt_trace=attempt_trace,
        )

    def validate(data):
        errors = V.structural_errors(data, schema)
        errors += V.journey_recheck_errors(
            data,
            source_manifest=source_manifest,
            source_units=source_units,
            prior_evidence_ids=prior_ids,
        )
        received_ids = {
            item.get("id")
            for item in (data or {}).get("reconciled_evidence") or []
            if isinstance(item, dict) and item.get("id")
        }
        for missing in sorted(prior_ids - received_ids):
            errors.append(f"[journey-recheck] 既有 evidence {missing} 不得被省略")
        return errors

    primary_provider = llm.resolve_provider(
        opts.get("journey_recheck_provider"), stage="journey_recheck"
    )
    generated = llm.validated_json_with_fallback(
        stage="journey_recheck",
        system=_prompt("journey_recheck"),
        user=user,
        validate=validate,
        canonicalize=canonicalize,
        primary_provider=primary_provider,
        fallback_provider="anthropic",
        models={
            primary_provider: opts.get("journey_recheck_model"),
            "anthropic": opts.get("journey_recheck_fallback_model"),
        },
        efforts={
            primary_provider: opts.get("journey_recheck_effort"),
            "anthropic": opts.get("journey_recheck_fallback_effort"),
        },
        max_tokens=opts.get("max_tokens", 32000),
    )
    return generated.data, {
        "tokens": generated.tokens,
        "seconds": round(generated.seconds),
        "attempts": generated.attempts,
    }


# 研究者看不懂、也不該看到的東西。重新檢查的輸出直接進「要你決定」清單，所以
# 這道檢查擋的是內部詞彙上畫面——專案規則明令不得出現的那一類。
_RECHECK_BANNED_IN_TEXT = (
    "layer", "actor_boundary", "visibility", "service_role",
    "evidence_ref", "source", "step",
)
_RECHECK_ID_PATTERN = re.compile(r"\b[EAR]-[A-Za-z0-9_-]+")


def _as_is_recheck_errors(data: object) -> list[str]:
    """Findings must be a shape the decisions list can render, in plain language.

    No separate schema file: the payload is one list of three-field objects,
    and a file would put the contract further from the only code that reads it.
    """
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        return ["[as-is-recheck] 輸出必須是帶 findings 陣列的 object"]
    errors = []
    for index, finding in enumerate(data["findings"]):
        where = f"findings[{index}]"
        if not isinstance(finding, dict):
            errors.append(f"[as-is-recheck] {where} 必須是 object")
            continue
        for field in ("about", "text"):
            if not str(finding.get(field) or "").strip():
                errors.append(f"[as-is-recheck] {where}.{field} 不得為空")
        if not isinstance(finding.get("caused_by_adjustment"), bool):
            errors.append(f"[as-is-recheck] {where}.caused_by_adjustment 必須是布林值")
        text = str(finding.get("text") or "")
        for banned in _RECHECK_BANNED_IN_TEXT:
            if banned in text:
                errors.append(f"[as-is-recheck] {where}.text 不得出現內部欄位名 {banned!r}")
        leaked = _RECHECK_ID_PATTERN.search(text) or _RECHECK_ID_PATTERN.search(
            str(finding.get("about") or ""))
        if leaked:
            errors.append(
                f"[as-is-recheck] {where} 不得出現內部編號 {leaked.group(0)!r}，請改寫成標題或原文")
    return errors


def as_is_recheck(graph: dict, evidence: list, journey: dict, case: dict,
                  judgements: list, opts: dict) -> tuple:
    """Report what a researcher's adjustments broke, without changing anything.

    Same shape as `journey_recheck` and for the same reasons: the adjusted
    artefact is authoritative, the whole of it is examined rather than only
    what moved (elements interconnect, so checking only the changed ones
    misses the consequences), and the output is a list the researcher rules
    on rather than anything that takes effect.
    """
    allowed_case = {
        key: case.get(key)
        for key in ("case_id", "service_type", "customers", "primary_provider",
                    "journey_start", "journey_end", "improvement_goal")
        if case.get(key) is not None
    }
    user = (
        "## Selected case boundary\n```yaml\n" + _yaml_dump(allowed_case) + "```\n\n"
        "## Current evidence\n```yaml\n" + _yaml_dump(_project(evidence)) + "```\n\n"
        "## Confirmed journey\n```yaml\n" + _yaml_dump(journey) + "```\n\n"
        "## Researcher-adjusted graph\n```yaml\n" + _yaml_dump(graph) + "```\n\n"
        "## Researcher judgements in this round\n```yaml\n" + _yaml_dump(judgements) + "```"
    )
    generated = llm.validated_json_with_fallback(
        stage="as_is_recheck",
        system=_prompt("as_is_recheck"),
        user=user,
        validate=_as_is_recheck_errors,
        # No digest/case_id reconciliation to do here, unlike journey_recheck's
        # canonicalize — this stage reports findings against whatever graph the
        # caller hands it and takes no further action on the result.
        canonicalize=lambda _data, _attempt_trace: None,
        primary_provider=llm.resolve_provider(opts.get("as_is_recheck_provider"),
                                              stage="as_is_recheck"),
        fallback_provider="anthropic",
        models={},
        efforts={},
        max_tokens=opts.get("max_tokens", 32000),
    )
    return generated.data.get("findings") or [], {
        "tokens": generated.tokens,
        "seconds": round(generated.seconds),
        "attempts": generated.attempts,
    }


def journey_from_case(case: dict) -> dict:
    scenario_id = case.get("primary_scenario") or "primary"
    return {
        "schema_version": 2,
        "case_id": case["case_id"],
        "primary_scenario": scenario_id,
        "scenarios": [{
            "id": scenario_id,
            "label": case.get("service_type", scenario_id),
            "customer_goal": f"完成{case.get('service_type', '服務')}",
            "object_types": [],
            "status": "confirmed",
            "steps": [{"id": f"S-{i:02d}", "label": label, "evidence_refs": []} for i, label in enumerate(case.get("steps") or [], 1)],
        }],
    }


# --------------------------------------------------------------------------
# Confirmed As-is → grounded issue package
# --------------------------------------------------------------------------

def _issue_candidate_errors(data: object) -> list[str]:
    if not isinstance(data, dict) or set(data) != {"issues"}:
        return ["[issues] top level must contain exactly issues"]
    issues = data.get("issues")
    if not isinstance(issues, list):
        return ["[issues] issues must be an array"]
    errors: list[str] = []
    if len(issues) > MAX_ISSUE_CANDIDATES:
        errors.append(f"[issues] at most {MAX_ISSUE_CANDIDATES} candidates are allowed")
    required = {
        "statement", "impact", "as_is_element_refs", "evidence_refs", "uncertainty"
    }
    optional = {"relation_refs", "marker_refs"}
    for index, issue in enumerate(issues):
        if not isinstance(issue, dict):
            errors.append(f"[issues] issues[{index}] must be an object")
            continue
        missing = sorted(required - set(issue))
        extra = sorted(set(issue) - required - optional)
        if missing:
            errors.append(f"[issues] issues[{index}] missing {missing}")
        if extra:
            errors.append(f"[issues] issues[{index}] has forbidden fields {extra}")
        for field in ("statement", "impact"):
            value = issue.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > 500:
                errors.append(f"[issues] issues[{index}].{field} must be 1..500 characters")
        for field in ("as_is_element_refs", "evidence_refs"):
            refs = issue.get(field)
            if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) for ref in refs):
                errors.append(f"[issues] issues[{index}].{field} must be a non-empty string array")
            elif len(refs) != len(set(refs)):
                errors.append(f"[issues] issues[{index}].{field} contains duplicates")
        for field in optional:
            refs = issue.get(field)
            if refs is not None and (
                not isinstance(refs, list)
                or not refs
                or any(not isinstance(ref, str) for ref in refs)
                or len(refs) != len(set(refs))
            ):
                errors.append(f"[issues] issues[{index}].{field} must be a unique non-empty string array")
        uncertainty = issue.get("uncertainty")
        if not isinstance(uncertainty, dict) or uncertainty.get("state") not in {
            "bounded", "uncertain", "disputed"
        }:
            errors.append(f"[issues] issues[{index}].uncertainty is invalid")
        elif uncertainty.get("state") in {"uncertain", "disputed"} and not isinstance(
            uncertainty.get("explanation"), str
        ):
            errors.append(f"[issues] issues[{index}].uncertainty requires explanation")
    return errors


def _bounded_payload(label: str, value: object) -> str:
    serialized = _yaml_dump(value)
    if len(serialized) > MAX_ISSUE_INPUT_CHARS:
        raise ValueError(f"{label} exceeds {MAX_ISSUE_INPUT_CHARS} characters")
    return serialized


def derive_issue_candidates(
    evidence: list, graph: dict, opts: dict, feedback: list[str] | None = None
) -> tuple[list, dict]:
    """Generate untrusted As-is issue candidates with one provider attempt each.

    `feedback` carries the provenance errors a previous attempt's package
    failed on, so the next attempt can fix exactly those. The candidate shape
    is validated inside this call, but whether an issue's cited evidence is
    reachable from its cited elements can only be judged after grounding and
    normalization — so that error has to come back in from the caller.
    """
    user = (
        "## Canonical As-is graph\n```yaml\n"
        + _bounded_payload("As-is graph", graph)
        + "```\n\n## Resolved evidence\n```yaml\n"
        + _bounded_payload("evidence", evidence)
        + "```"
    )
    if feedback:
        user += (
            "\n\n---\n上一次的輸出沒有通過依據檢查：\n"
            + "\n".join(f"- {line}" for line in feedback[:20])
            + "\n\n請重新輸出完整 JSON。每一則問題只能引用「它所引用的元素真的帶有」"
              "的證據；找不到可引用的證據就改引用帶有該證據的元素，或捨棄那一則問題。"
        )
    primary = llm.resolve_provider(opts.get("issues_provider"), stage="issues")
    generated = llm.validated_json_with_fallback(
        stage="issues",
        system=_prompt("issues"),
        user=user,
        validate=_issue_candidate_errors,
        canonicalize=lambda _data, _trace: None,
        primary_provider=primary,
        fallback_provider="anthropic",
        models={
            primary: opts.get("issues_model"),
            "anthropic": opts.get("issues_fallback_model"),
        },
        efforts={
            primary: opts.get("issues_effort"),
            "anthropic": opts.get("issues_fallback_effort"),
        },
        max_tokens=min(int(opts.get("max_tokens", MAX_ISSUE_OUTPUT_TOKENS)), MAX_ISSUE_OUTPUT_TOKENS),
        max_schema_retries=0,
    )
    return generated.data["issues"], {
        "tokens": generated.tokens,
        "seconds": round(generated.seconds),
        "attempts": generated.attempts,
    }


def _grounding_context(candidates: list, evidence: list, graph: dict) -> dict:
    evidence_by_id = {
        item.get("id"): item
        for item in evidence
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    elements = {
        item.get("id"): item
        for item in graph.get("nodes") or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    relations = {
        item.get("id"): item
        for item in graph.get("relations") or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    markers = {
        item.get("id"): item
        for item in graph.get("markers") or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    rows = []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            continue
        valid_evidence = [
            evidence_by_id[ref]
            for ref in candidate.get("evidence_refs") or []
            if ref in evidence_by_id
        ]
        if not valid_evidence:
            continue
        rows.append({
            "candidate_index": index,
            "statement": candidate.get("statement"),
            "impact": candidate.get("impact"),
            "elements": [
                elements[ref]
                for ref in candidate.get("as_is_element_refs") or []
                if ref in elements
            ],
            "relations": [
                relations[ref]
                for ref in candidate.get("relation_refs") or []
                if ref in relations
            ],
            "markers": [
                markers[ref]
                for ref in candidate.get("marker_refs") or []
                if ref in markers
            ],
            "evidence": valid_evidence,
        })
    return {"candidates": rows}


def _grounding_errors(data: object, expected_pairs: set[tuple[int, str]]) -> list[str]:
    if not isinstance(data, dict) or set(data) != {"verdicts"} or not isinstance(data.get("verdicts"), list):
        return ["[issue-grounding] top level must contain exactly verdicts array"]
    errors: list[str] = []
    seen: set[tuple[int, str]] = set()
    for index, verdict in enumerate(data["verdicts"]):
        if not isinstance(verdict, dict) or set(verdict) != {
            "candidate_index", "evidence_ref", "relationship", "rationale"
        }:
            errors.append(f"[issue-grounding] verdicts[{index}] has invalid shape")
            continue
        pair = (verdict.get("candidate_index"), verdict.get("evidence_ref"))
        if pair not in expected_pairs:
            errors.append(f"[issue-grounding] verdicts[{index}] references unexpected pair {pair!r}")
        if pair in seen:
            errors.append(f"[issue-grounding] duplicate verdict for {pair!r}")
        seen.add(pair)
        if verdict.get("relationship") not in {"supports", "limits", "unsupported"}:
            errors.append(f"[issue-grounding] verdicts[{index}].relationship is invalid")
        if not isinstance(verdict.get("rationale"), str) or not verdict["rationale"].strip():
            errors.append(f"[issue-grounding] verdicts[{index}].rationale is required")
    for pair in sorted(expected_pairs - seen):
        errors.append(f"[issue-grounding] missing verdict for {pair!r}")
    return errors


def ground_issue_candidates(candidates: list, evidence: list, graph: dict, opts: dict) -> tuple[list, dict]:
    context = _grounding_context(candidates, evidence, graph)
    expected_pairs = {
        (row["candidate_index"], item["id"])
        for row in context["candidates"]
        for item in row["evidence"]
    }
    if not expected_pairs:
        return [], {"tokens": 0, "seconds": 0, "attempts": []}
    user = "## Candidate-specific evidence\n```yaml\n" + _bounded_payload("issue grounding", context) + "```"
    primary = llm.resolve_provider(opts.get("issue_grounding_provider"), stage="issue_grounding")
    generated = llm.validated_json_with_fallback(
        stage="issue_grounding",
        system=_prompt("issue_grounding"),
        user=user,
        validate=lambda data: _grounding_errors(data, expected_pairs),
        canonicalize=lambda _data, _trace: None,
        primary_provider=primary,
        fallback_provider="anthropic",
        models={
            primary: opts.get("issue_grounding_model"),
            "anthropic": opts.get("issue_grounding_fallback_model"),
        },
        efforts={
            primary: opts.get("issue_grounding_effort"),
            "anthropic": opts.get("issue_grounding_fallback_effort"),
        },
        max_tokens=min(int(opts.get("max_tokens", MAX_ISSUE_OUTPUT_TOKENS)), MAX_ISSUE_OUTPUT_TOKENS),
        max_schema_retries=0,
    )
    return generated.data["verdicts"], {
        "tokens": generated.tokens,
        "seconds": round(generated.seconds),
        "attempts": generated.attempts,
    }


_SOLUTION_LANGUAGE = re.compile(
    r"(?:應該|建議|必須|需要).{0,8}(?:導入|新增|建立|提供|改為|採用|實施|部署)"
    r"|(?:將會|預期).{0,16}(?:降低|消除|解決)"
    r"|\b(?:should|must|recommend|recommended|deploy|introduce|implement)\b"
    r"|\b(?:will|would|could)\s+(?:reduce|eliminate|solve)\b",
    re.IGNORECASE,
)


def normalize_issue_package(
    candidates: list,
    verdicts: list,
    evidence: list,
    graph: dict,
    *,
    case_id: str,
    scenario_id: str,
) -> dict:
    """Canonicalize model candidates after references and semantic support pass."""
    evidence_ids = {
        item.get("id") for item in evidence if isinstance(item, dict) and item.get("id")
    }
    elements = {
        item.get("id"): item
        for item in graph.get("nodes") or []
        if isinstance(item, dict) and item.get("id")
    }
    relations = {
        item.get("id"): item
        for item in graph.get("relations") or []
        if isinstance(item, dict) and item.get("id")
    }
    markers = {
        item.get("id"): item
        for item in graph.get("markers") or []
        if isinstance(item, dict) and item.get("id")
    }
    order = {identifier: index for index, identifier in enumerate(elements)}
    supported = {
        (item.get("candidate_index"), item.get("evidence_ref"))
        for item in verdicts
        if isinstance(item, dict) and item.get("relationship") == "supports"
    }
    retained: list[dict] = []
    seen: set[tuple[Any, ...]] = set()
    for candidate_index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict) or _issue_candidate_errors({"issues": [candidate]}):
            continue
        statement = candidate["statement"].strip()
        impact = candidate["impact"].strip()
        if _SOLUTION_LANGUAGE.search(statement) or _SOLUTION_LANGUAGE.search(impact):
            continue
        element_refs = [ref for ref in candidate["as_is_element_refs"] if ref in elements]
        if len(element_refs) != len(candidate["as_is_element_refs"]):
            continue
        relation_refs = candidate.get("relation_refs")
        if relation_refs is not None and any(ref not in relations for ref in relation_refs):
            continue
        marker_refs = candidate.get("marker_refs")
        if marker_refs is not None and any(ref not in markers for ref in marker_refs):
            continue
        evidence_refs = [
            ref
            for ref in candidate["evidence_refs"]
            if ref in evidence_ids and (candidate_index, ref) in supported
        ]
        if not evidence_refs:
            continue
        key = (
            statement.casefold(),
            tuple(sorted(element_refs)),
            tuple(sorted(evidence_refs)),
            tuple(sorted(relation_refs or [])),
            tuple(sorted(marker_refs or [])),
        )
        if key in seen:
            continue
        seen.add(key)
        issue = {
            "statement": statement,
            "impact": impact,
            "claim_state": "inference",
            "as_is_element_refs": sorted(element_refs, key=lambda ref: (order.get(ref, 10**9), ref)),
            "evidence_refs": sorted(evidence_refs),
            "uncertainty": candidate["uncertainty"],
        }
        if relation_refs:
            issue["relation_refs"] = sorted(relation_refs)
        if marker_refs:
            issue["marker_refs"] = sorted(marker_refs)
        retained.append(issue)
    retained.sort(key=lambda item: (
        min(order.get(ref, 10**9) for ref in item["as_is_element_refs"]),
        item["statement"].casefold(),
    ))
    for index, issue in enumerate(retained, 1):
        issue["id"] = f"I-{index:03d}"
    package = {
        "schema_version": 1,
        "artifact_type": "issue_package",
        "case_id": case_id,
        "scenario_id": scenario_id,
        "issues": retained,
    }
    errors = V.structural_errors(package, V.load_schema("issue-package"))
    errors += V.issue_package_errors(package, evidence, graph)
    if errors:
        raise ValueError("issue package validation failed:\n" + "\n".join(errors))
    return package


def grounded_issue_package(
    evidence: list,
    graph: dict,
    *,
    case_id: str,
    scenario_id: str,
    opts: dict,
) -> tuple[dict, dict]:
    candidates, issue_usage = derive_issue_candidates(evidence, graph, opts)
    verdicts, grounding_usage = ground_issue_candidates(candidates, evidence, graph, opts)
    package = normalize_issue_package(
        candidates,
        verdicts,
        evidence,
        graph,
        case_id=case_id,
        scenario_id=scenario_id,
    )
    return package, {"issues": issue_usage, "issue_grounding": grounding_usage}


def case_with_journey(case: dict, journey_data: dict, evidence_ids=None) -> dict:
    # sbp-solution-adoption-and-core-problem: a Solution may cite any evidence of the run (the
    # Solution formation and the CJM confirmation already check against all of it), so callers
    # that have the run's evidence pass its ids; without them only the steps' own references
    # are known, as before.
    known = ({r for s in journey_data.get("scenarios") or [] for st in s.get("steps") or [] for r in st.get("evidence_refs") or []}
             if evidence_ids is None else set(evidence_ids))
    errors = _journey_errors(journey_data, known, require_confirmed=True)
    if errors:
        raise ValueError("確認歷程無效：\n" + "\n".join(errors))
    scenario = next(s for s in journey_data["scenarios"] if s["id"] == journey_data["primary_scenario"])
    return {**case, "steps": [step["label"] for step in scenario["steps"]], "journey_start": scenario["steps"][0]["label"], "journey_end": scenario["steps"][-1]["label"]}


def _disposition_and_annotation_errors(data: dict, evidence_ids: set[str], journey: dict | None) -> list[str]:
    """Structure now produces the disposition ledger and experience annotations
    directly (streamline-sbp-generation-pipeline 決定二) — this reuses the same
    completeness/referential rigor the old eventize()/_eventization_errors() had,
    just pointed at the merged graph's top-level `dispositions`/`annotations`
    instead of a separate eventization document.
    """
    errs = []
    if not isinstance(data, dict):
        return errs
    dispositions = [d for d in data.get("dispositions") or [] if isinstance(d, dict)]
    refs = [d.get("evidence_ref") for d in dispositions]
    for ref in sorted(evidence_ids - set(refs)):
        errs.append(f"[disposition] evidence {ref} 缺少 disposition")
    for ref in sorted({r for r in refs if r and refs.count(r) > 1}):
        errs.append(f"[disposition] evidence {ref} disposition 重複")
    for ref in sorted(set(refs) - evidence_ids):
        errs.append(f"[disposition] disposition 指到不存在 evidence {ref}")
    if journey:
        scenarios = {s.get("id"): s for s in journey.get("scenarios") or []}
        steps = {(sid, st.get("id")) for sid, sc in scenarios.items() for st in sc.get("steps") or []}
        for item in data.get("annotations") or []:
            if not isinstance(item, dict):
                continue
            key = (item.get("scenario_ref"), item.get("step_ref"))
            if key not in steps:
                errs.append(f"[annotation] {item.get('id')} 指到不存在 journey step {key}")
            for ref in item.get("evidence_refs") or []:
                if ref not in evidence_ids:
                    errs.append(f"[annotation] {item.get('id')} evidence_ref {ref} 不存在")
    return errs


def _validate_graph(data: dict, evidence_ids: set[str], evidence_texts: dict[str, str], journey_data: dict | None = None) -> list[str]:
    return (
        V.structural_errors(data, V.load_schema("service-graph"))
        + V.graph_errors(data, evidence_ids, evidence_texts, journey=journey_data)
        + _disposition_and_annotation_errors(data, evidence_ids, journey_data)
        # validate-concrete-actors: blocking only for fresh model output
        # here, in the retry loop -- not part of V.graph_errors(), which
        # projection_errors() also calls when re-projecting an *existing*
        # graph (e.g. the standalone `sbp-gen project` command), and a
        # legacy graph predating this rule must keep projecting.
        + V.abstract_actor_errors(data)
    )


def _set_external_references(graph: dict, external_references: list | None) -> None:
    """pre-blueprint-implementation-research: the adopted references are
    written by code, like the customer layer -- the model only cites their
    ids through `external_refs`."""
    if not isinstance(graph, dict):
        return
    if external_references:
        graph["external_references"] = [dict(ref) for ref in external_references]
    else:
        graph.pop("external_references", None)


def journey_has_solutions(journey_data) -> bool:
    """A journey that went through Solution selection (any step carries
    solution candidates). Such a run builds a To-be blueprint."""
    return bool(journey_data) and any(
        "solution_candidates" in step for scenario in journey_data.get("scenarios") or []
        for step in scenario.get("steps") or [] if isinstance(step, dict))


def to_be_customer_nodes(journey_data: dict) -> list[dict]:
    """to-be-blueprint-generation: the inherited customer layer as graph
    nodes, built from the same list the validator compares against."""
    scenario_id = journey_data.get("primary_scenario") or "scenario-01"
    nodes = []
    for index, entry in enumerate(V.to_be_customer_layer(journey_data), 1):
        text = entry["text"]
        claims = [{"text": text, "source": "evidence", "inferred": False, "scope": "individual_case",
                   "evidence_ref": ref} for ref in entry["evidence_refs"]]
        if not claims:
            # A confirmed step with no cited evidence is still the researcher's
            # confirmed journey, never a model inference.
            claims = [{"text": text, "source": "researcher", "inferred": True, "scope": "individual_case"}]
        nodes.append({"id": f"A-C{index}", "kind": "action", "label": text[:16], "description": text,
                      "step": entry["position"], "layer": "顧客行動", "journey_step_ref": entry["step_ref"],
                      "actor": "顧客", "actor_boundary": "customer", "visibility": "customer_visible",
                      "service_role": "customer", "scenario_refs": [scenario_id], "claims": claims})
    return nodes


def to_be_customer_sequence(customer_nodes: list[dict]) -> list[dict]:
    """The order of the inherited customer layer is the researcher-confirmed
    step order, so its sequence relations are the program's too."""
    return [{"id": f"R-C{index}", "type": "sequence", "source": "researcher", "from": a["id"], "to": b["id"],
             "note": "已確認旅程的先後順序"}
            for index, (a, b) in enumerate(zip(customer_nodes, customer_nodes[1:]), 1)]


def _merge_to_be_customer_layer(data: dict, journey_data: dict) -> None:
    """The customer layer is the program's, not the model's: drop any customer
    node -- and any relation between two customer nodes -- the model returned,
    and put the inherited layer and its confirmed order in front."""
    customer = to_be_customer_nodes(journey_data)
    customer_ids = {n["id"] for n in customer}
    model_nodes = [n for n in data.get("nodes") or [] if isinstance(n, dict) and n.get("layer") != "顧客行動"]
    # drop-relations-for-discarded-customer-nodes: a relation that points at a
    # node gone after the merge (a discarded model customer node) goes too,
    # instead of failing validation and costing a retry.
    kept_ids = customer_ids | {n.get("id") for n in model_nodes}
    model_relations = [r for r in data.get("relations") or [] if isinstance(r, dict)
                       and not set(r.get("endpoints") or [r.get("from"), r.get("to")]) <= customer_ids
                       and set(r.get("endpoints") or [r.get("from"), r.get("to")]) <= kept_ids
                       and not str(r.get("id") or "").startswith("R-C")]
    data["nodes"] = customer + model_nodes
    data["relations"] = to_be_customer_sequence(customer) + model_relations
    data.setdefault("meta", {})["blueprint_method"] = V.TO_BE_METHOD


def structure_input_view(evidence: list, case: dict, journey_data=None, coding_categories: list | None = None,
                         external_references: list | None = None) -> dict:
    """The upstream content sent to blueprint generation. The structure message
    is assembled from this view and the blueprint's freshness fingerprint is
    its digest (track-sbp-upstream-freshness), so the two cannot drift apart.
    Conflicts (derived from the evidence within the same build) and the
    researcher note (one build's instruction) are not upstream content.

    pre-blueprint-implementation-research: the external references the
    researcher adopted join the view only when there are any, so a run without
    them keeps its input and fingerprint."""
    has_solutions = journey_has_solutions(journey_data)
    view = {
        "boundary": {k: case[k] for k in ("case_id", "service_type", "customers", "primary_provider", "journey_start",
                                         "journey_end", "steps", "external_partners") if k in case},
        "evidence": _project(evidence),
        "coding": _journey_coding_view(coding_categories),
        "journey_kind": "handoff" if has_solutions else ("confirmed" if journey_data else None),
        "journey": journey_sbp_handoff(journey_data) if has_solutions else (_without_emotion(journey_data) or None),
    }
    if has_solutions and external_references:
        view["external_references"] = [dict(ref) for ref in external_references]
    return view


def _without_emotion(journey_data):
    """The confirmed journey as blueprint input: every step without its emotion
    (2026-10-02 ruling -- emotion is the researcher's reading of the journey)."""
    if not journey_data:
        return journey_data
    copied = copy.deepcopy(journey_data)
    for scenario in copied.get("scenarios") or []:
        for step in (scenario or {}).get("steps") or []:
            if isinstance(step, dict):
                step.pop("emotion", None)
    return copied


def _structure_user_message(evidence: list, conflicts: list, case: dict, researcher_note=None, journey_data=None,
                            coding_categories: list | None = None, to_be: bool = False,
                            external_references: list | None = None) -> str:
    """The evidence-aware modelling payload.  Kept separate from reference payload.

    generate-as-is-blueprint-candidate: axial coding joins as analysis of the
    evidence; node and relation citations stay evidence ids."""
    view = structure_input_view(evidence, case, journey_data, coding_categories, external_references)
    coding_block = ("## Axial coding (analysis of the evidence above; cite evidence ids only, never a category)\n```yaml\n"
                    + _yaml_dump(view["coding"]) + "```\n\n") if view["coding"] else ""
    if view["journey_kind"] == "handoff":
        journey_block = ("## Reviewed CJM and researcher-selected Solutions\n"
                         "(Journey, Action, Pain and Satisfaction are analytical synthesis. Only the listed "
                         "Solutions were selected by the researcher. Their Channels were determined during CJM generation "
                         "from Journey, Action, Solution and surrounding context. Unselected options are excluded. "
                         "This context does not prove frontstage, backstage, support or relation claims.)\n"
                         "```yaml\n" + _yaml_dump(view["journey"]) + "```\n\n")
    elif view["journey_kind"] == "confirmed":
        journey_block = ("## Confirmed journey\n(time order and customer actions only; service claims need evidence)\n"
                         "```yaml\n" + _yaml_dump(view["journey"]) + "```\n\n")
    else:
        journey_block = ""
    if to_be:
        # Derived from the confirmed journey already in the view, so the
        # blueprint's freshness fingerprint (the view) is unchanged.
        layer = [{"id": n["id"], "step": n["step"], "journey_step_ref": n["journey_step_ref"], "text": n["description"],
                  "evidence_refs": [c["evidence_ref"] for c in n["claims"] if c.get("evidence_ref")]}
                 for n in to_be_customer_nodes(journey_data)]
        journey_block += ("## 顧客層（已確認，程式建立；不要輸出 layer: 顧客行動 的節點。這裡的 evidence_refs 只是顧客層自己的引用，不是可用的證據範圍）\n```yaml\n"
                          + _yaml_dump(layer) + "```\n\n")
    if view.get("external_references"):
        journey_block += ("## 建立藍圖前研究找到的外部參考（設計參考，不是本案證據；只能用節點的 external_refs 引用，"
                          "不能放進 claims 的 evidence_ref 或 anchors，也不能取代錨點與必要性理由）\n```yaml\n"
                          + _yaml_dump(view["external_references"]) + "```\n\n")
    return (
        "## Case boundary\n```yaml\n" + _yaml_dump(view["boundary"]) + "```\n\n"
        + "## Evidence index\n```yaml\n" + _yaml_dump(view["evidence"]) + "```\n\n"
        + coding_block
        + journey_block
        + "## Conflicts\n```yaml\n" + _yaml_dump(conflicts or []) + "```"
        + _researcher_note_block(researcher_note)
    )


def _canonicalize_graph_metadata(data: dict, case: dict, journey_data: dict | None) -> None:
    """Apply trusted boundary metadata after model output.

    Calls without a confirmed journey retain the historical graph shape for
    compatibility.  The production CLI always supplies a confirmed journey.
    """
    if not isinstance(data, dict):
        return
    meta = data.get("meta")
    if not isinstance(meta, dict):
        meta = {}
        data["meta"] = meta
    meta["case_id"] = case["case_id"]
    if not journey_data:
        return
    scenario_id = journey_data.get("primary_scenario")
    if not isinstance(scenario_id, str) or not scenario_id:
        return
    canonical = json.dumps(
        journey_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    data["schema_version"] = 2
    meta.update({
        "artifact_type": "as_is_graph",
        "case_id": case["case_id"],
        "scenario_id": scenario_id,
        "journey_ref": "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    })


# --------------------------------------------------------------------------
# experiment-structure-relation-repair
# --------------------------------------------------------------------------
# A blueprint that failed only relation checks: ask for the missing or
# corrected relations only and keep everything else. The model only produces
# relations; replacing, appending and numbering are done here, so nodes,
# dispositions, annotations and markers cannot change.
_STRUCTURE_RELATION_ERROR_RE = re.compile(r"^\[graph\] (?:relation|supports|handoff) (\S+) ")
_STRUCTURE_ISOLATED_ACTION_RE = re.compile(r"^\[graph\] action (\S+) 沒有任何 relation")
# The To-be customer layer's relations; the program builds and owns them.
_CUSTOMER_RELATION_PREFIX = "R-C"


def _relation_ends(relation: dict) -> list:
    return relation.get("endpoints") or [relation.get("from"), relation.get("to")]


def _structure_relation_targets(data: object, errors: list) -> frozenset | None:
    """The relations and isolated actions a relation repair would fix, or None
    when it must not run: every error has to name one relation, or be an
    isolated action; each named relation occurs exactly once and is not a
    customer-layer relation; each named action is a node; no id is both."""
    if not isinstance(data, dict) or not errors:
        return None
    relations, nodes = data.get("relations"), data.get("nodes")
    if not isinstance(relations, list) or not isinstance(nodes, list):
        return None
    relation_ids = [r.get("id") for r in relations if isinstance(r, dict)]
    node_ids = {n.get("id") for n in nodes if isinstance(n, dict)}
    targets = set()
    for error in errors:
        text = str(error)
        named = _STRUCTURE_RELATION_ERROR_RE.match(text)
        isolated = None if named else _STRUCTURE_ISOLATED_ACTION_RE.match(text)
        if named:
            rid = named.group(1)
            if relation_ids.count(rid) != 1 or rid.startswith(_CUSTOMER_RELATION_PREFIX) or rid in node_ids:
                return None
            targets.add(rid)
        elif isolated:
            nid = isolated.group(1)
            if nid not in node_ids or nid in relation_ids:
                return None
            targets.add(nid)
        else:
            return None
    return frozenset(targets)


def _structure_relation_repair_closing(targets: frozenset, errors=()) -> str:
    """The last paragraph of a relation repair request: which relations and
    actions failed the existing relation rules, and to fix only those."""
    return (
        f"沒通過的是這幾處：{'、'.join(sorted(targets))}。它們沒有通過既有的關係規則。"
        "請只修這幾處：被點名的 relation 回傳修正後的整條，保留原本的 id；"
        "沒有任何 relation 的 action，新增連到它的 relation。"
        "其餘節點、證據去向、註記與其他 relation 維持不變，也不用回傳。"
        "只回傳一個 JSON 物件，格式是 {\"relations\": [...]}。"
    )


def _splice_structure_relation_repair(previous: object, targets: frozenset, reply: object) -> tuple:
    """Put the repaired relations into the previous blueprint. Returns
    (merged, None), or (None, reason) when the reply must be abandoned. A
    named relation is replaced in place; any other new relation must touch a
    named action, is appended, and gets the next unused R-<number> id. Only
    the relations list changes."""
    if not isinstance(previous, dict):
        return None, "reply_shape"
    items = reply.get("relations") if isinstance(reply, dict) else reply
    if not isinstance(items, list) or not items or not all(
            isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"] for r in items):
        return None, "reply_shape"
    base = list(previous.get("relations") or [])
    base_ids = {r.get("id") for r in base if isinstance(r, dict)}
    named_relations = set(targets) & base_ids
    named_actions = set(targets) - named_relations
    nodes = [n for n in previous.get("nodes") or [] if isinstance(n, dict)]
    node_ids = {n.get("id") for n in nodes}
    # In a To-be blueprint every customer node is the program's; a relation
    # between two of them is dropped by the customer-layer merge.
    to_be = (previous.get("meta") or {}).get("blueprint_method") == V.TO_BE_METHOD
    customer_ids = {n.get("id") for n in nodes if n.get("layer") == "顧客行動"} if to_be else set()
    replaced, added, covered = {}, [], set()
    for relation in items:
        rid = relation["id"]
        if rid.startswith(_CUSTOMER_RELATION_PREFIX):
            return None, "reply_reserved_id"
        ends = _relation_ends(relation)
        if not ends or any(end not in node_ids for end in ends):
            return None, "reply_unknown_endpoint"
        if rid in named_relations:
            if rid in replaced:
                return None, "reply_shape"
            replaced[rid] = relation
        elif rid in base_ids:
            return None, "reply_changes_untargeted"
        else:
            if set(ends) <= customer_ids:
                return None, "reply_customer_only"
            if not set(ends) & named_actions:
                return None, "reply_new_not_targeted"
            added.append(relation)
        covered.update(set(ends) & named_actions)
    if set(replaced) != named_relations or not named_actions <= covered:
        return None, "target_not_covered"
    numbers = [int(m.group(1)) for r in base if isinstance(r, dict) and isinstance(r.get("id"), str)
               and (m := re.fullmatch(r"R-(\d+)", r["id"]))]
    next_number = max(numbers, default=0) + 1
    merged = copy.deepcopy(previous)
    merged["relations"] = [copy.deepcopy(replaced.get(r.get("id"), r)) if isinstance(r, dict) else r for r in base]
    for relation in added:
        merged["relations"].append({**copy.deepcopy(relation), "id": f"R-{next_number}"})
        next_number += 1
    return merged, None


class _StructureRelationRepair:
    """What `_generate` needs to run a relation repair for the blueprint."""
    plan = staticmethod(_structure_relation_targets)
    closing = staticmethod(_structure_relation_repair_closing)
    splice = staticmethod(_splice_structure_relation_repair)
    record_key = "targeted_items"


def structure(evidence: list, conflicts: list, case: dict, opts: dict, researcher_note=None, journey_data=None,
              coding_categories: list | None = None, external_references: list | None = None) -> tuple:
    evidence_ids = {e["id"] for e in evidence}
    evidence_texts = {e["id"]: e.get("text", "") for e in evidence}
    # to-be-blueprint-generation: a Solution-selection run builds a To-be
    # blueprint; every other run keeps the existing structure prompt and rules.
    to_be = journey_has_solutions(journey_data)

    def validate(data):
        # 同一個模式又出現一次（journey() 已經修過一次）：meta.case_id 是呼叫端
        # 成功後才補上的，不該逼模型自己猜、也不該讓它白白吃掉一次重試額度。
        if isinstance(data, dict):
            if to_be:
                _merge_to_be_customer_layer(data, journey_data)
                _set_external_references(data, external_references)
            _canonicalize_graph_metadata(data, case, journey_data)
        return (_validate_graph(data, evidence_ids, evidence_texts, journey_data)
                + _generation_required_errors(data, "service-graph"))

    with perf.phase('prepare', operation='build_prompt'):
        system = _system("structure_to_be" if to_be else "structure")
        user = _structure_user_message(evidence, conflicts, case, researcher_note, journey_data, coding_categories,
                                       to_be=to_be, external_references=external_references if to_be else None)
    graph, usage = _generate(
        "structure", system, user, validate, opts, max_retries=4, partial_repair=_StructureRelationRepair,
    )
    with perf.phase('validation', operation='finalize_graph'):
        if to_be:
            _merge_to_be_customer_layer(graph, journey_data)
            _set_external_references(graph, external_references)
        _canonicalize_graph_metadata(graph, case, journey_data)
    return graph, usage


REFERENCE_ALLOWED = ("case_id", "service_type", "customers", "primary_provider", "journey_start", "journey_end", "steps", "external_partners")


def _reference_user_message(case: dict) -> str:
    """Allowlist-only payload: deliberately accepts *only* the case contract."""
    forbidden = {"evidence", "conflicts", "researcher_notes", "nodes", "relations", "graph"} & set(case)
    if forbidden:
        raise ValueError("reference payload 禁止 evidence-derived fields: " + ", ".join(sorted(forbidden)))
    payload = {key: case[key] for key in REFERENCE_ALLOWED if key in case}
    return "## Validated service boundary and journey\n```yaml\n" + _yaml_dump(payload) + "```"


def _reference_errors(data: dict, *, reconciled=False, graph=None, conflicts=None) -> list[str]:
    errs = V.structural_errors(data, V.load_schema("reference-hypotheses"))
    if not isinstance(data, dict): return errs
    for e in data.get("expectations") or []:
        label = (e.get("label") or "")
        if any(word in label for word in ("理想", "最佳實務", "必須", "應該實施")):
            errs.append(f"[reference] {e.get('id')} 使用規範性語言")
        if reconciled and e.get("status") in ("unsupported", "partially_supported"):
            q = e.get("validation_question", "")
            if any(word in q for word in ("是否有", "請確認", "是不是")):
                errs.append(f"[reference] {e.get('id')} validation_question 過度引導")
    if reconciled and graph:
        node_ids = {n.get("id") for n in graph.get("nodes") or []}
        conflict_ids = {c.get("id") for c in conflicts or []}
        for e in data.get("expectations") or []:
            for ref in e.get("matched_node_refs") or []:
                if ref not in node_ids: errs.append(f"[reference] {e.get('id')} 指到不存在 node {ref}")
            for ref in e.get("conflict_refs") or []:
                if ref not in conflict_ids: errs.append(f"[reference] {e.get('id')} 指到不存在 conflict {ref}")
    return errs


def reference(case: dict, opts: dict) -> tuple:
    user = _reference_user_message(case)
    data, usage = _generate("reference", _prompt("reference"), user, lambda d: _reference_errors(d), opts)
    data.setdefault("meta", {})["case_id"] = case["case_id"]
    return data, usage


def reconcile(graph: dict, hypotheses: dict, conflicts: list, case: dict, opts: dict) -> tuple:
    user = (
        "## Case boundary\n```yaml\n" + _yaml_dump({k: case[k] for k in REFERENCE_ALLOWED if k in case}) + "```\n\n"
        "## Action graph\n```yaml\n" + _yaml_dump(graph) + "```\n\n"
        "## Reference hypotheses\n```yaml\n" + _yaml_dump(hypotheses) + "```\n\n"
        "## Conflicts\n```yaml\n" + _yaml_dump(conflicts or []) + "```"
    )
    data, usage = _generate("reconcile", _prompt("reconcile"), user, lambda d: _reference_errors(d, reconciled=True, graph=graph, conflicts=conflicts), opts)
    data.setdefault("meta", {})["case_id"] = case["case_id"]
    return data, usage


def _axial_coding_user_message(concepts: list, case_id: str, evidence: list | None = None) -> str:
    """feed-axial-coding-the-open-coding-concepts：模型看到的是開放編碼的
    概念層，不是逐字稿原話——`tools/tests/test_axial_coding_input_contract.py`
    把這裡送出的欄位跟 prompt 陳述的欄位綁在一起測，兩邊不一致就會紅。
    """
    items = [
        {"id": c.get("id"), "name": c.get("name"), "description": c.get("description"),
         "evidence_refs": c.get("evidence_refs"), **({"quote": c["quote"]} if c.get("quote") else {})}
        for c in concepts if isinstance(c, dict)
    ]
    # converge-coding-contracts: when every concept carries its readable quote,
    # representative quotes are picked from those, so the verbatim evidence
    # block (whose only use was picking quotes) is not sent.
    if items and all(item.get("quote") for item in items):
        evidence = None
    # axial-explicit-membership: the verbatim evidence the concepts cite, so
    # representative quotes are chosen from the actual words.
    cited = {ref for c in concepts if isinstance(c, dict) for ref in c.get("evidence_refs") or []}
    quotes = [{"id": e.get("id"), "role": e.get("role"), "text": e.get("text")}
              for e in evidence or [] if isinstance(e, dict) and e.get("id") in cited]
    quote_block = ("\n\n## 證據原話\n```yaml\n" + _yaml_dump(quotes) + "```") if quotes else ""
    return (
        f"## 案例\ncase_id: {case_id}\n\n"
        "## 開放編碼概念\n```yaml\n" + _yaml_dump(items) + "```"
        + quote_block
    )


AXIAL_DEFINITION_MAX_CHARS = 50
AXIAL_FORMATION_NOTE_MAX_CHARS = 100
AXIAL_REPRESENTATIVE_MAX = 5
# coding-support-summary (researcher ruling 2026-10-05)
SUPPORT_SUMMARY_MAX_CHARS = 150
SUPPORT_SUMMARY_MAX_SENTENCES = 3


def support_summary_errors(owner: str, text: object) -> list[str]:
    """Presence, length and sentence count only -- whether it adds unsupported
    causes is the prompt's and quality review's job, never checked here."""
    summary = str(text or "").strip()
    if not summary:
        return [f"{owner} 缺少支持摘要（support_summary）"]
    errors = []
    if len(summary) > SUPPORT_SUMMARY_MAX_CHARS:
        errors.append(f"{owner} 的支持摘要 {len(summary)} 字，要在 {SUPPORT_SUMMARY_MAX_CHARS} 字以內")
    sentences = [s for s in re.split(r"[。！？!?]", summary) if s.strip()]
    if len(sentences) > SUPPORT_SUMMARY_MAX_SENTENCES:
        errors.append(f"{owner} 的支持摘要有 {len(sentences)} 句，要 1–{SUPPORT_SUMMARY_MAX_SENTENCES} 句")
    return errors


def _axial_generation_errors(data: object, evidence_ids: set[str], concept_ids: set[str],
                             quoted_ids: set[str] | None = None) -> list[str]:
    """axial-explicit-membership (2026-10-03): what newly generated axial coding
    must carry on top of `_axial_coding_errors` -- a short definition, explicit
    open codes, representative evidence chosen from the category's own, and a
    formation note. Stored documents are read with `_axial_coding_errors`
    alone, so older runs stay readable. A concept may sit in several themes;
    MECE is a self-check in the prompt, never a check here."""
    errors = _axial_coding_errors(data, evidence_ids)
    if not isinstance(data, dict):
        return errors
    if not str(data.get("formation_note") or "").strip():
        errors.append("[axial_coding] 缺少主題形成說明（formation_note）")
    for category in data.get("categories") or []:
        if not isinstance(category, dict):
            continue
        cid = category.get("id")
        if not str(category.get("category_name") or "").strip():
            errors.append(f"[axial_coding] category {cid} 缺少正式主題名稱（category_name）")
        if not str(category.get("definition") or "").strip():
            errors.append(f"[axial_coding] category {cid} 缺少定義（definition）")
        errors += support_summary_errors(f"[axial_coding] category {cid}", category.get("support_summary"))
        refs = category.get("concept_refs")
        if not refs:
            errors.append(f"[axial_coding] category {cid} 缺少包含的開放編碼（concept_refs）")
        for ref in refs or []:
            if ref not in concept_ids:
                errors.append(f"[axial_coding] category {cid} 的 concept_refs 引用不存在的開放編碼：{ref}")
        if quoted_ids is not None:
            # converge-coding-contracts: pick member open codes' readable quotes.
            members = set(refs or [])
            picked = category.get("representative_quote_refs")
            if category.get("representative_evidence_refs"):
                errors.append(f"[axial_coding] category {cid} 新產出用 representative_quote_refs 挑代表語句，不寫 representative_evidence_refs")
            if not picked:
                errors.append(f"[axial_coding] category {cid} 缺少代表語句（representative_quote_refs）")
            for ref in picked or []:
                if ref not in members:
                    errors.append(f"[axial_coding] category {cid} 的代表語句 {ref} 不是這個主題包含的開放編碼")
                elif ref not in quoted_ids:
                    errors.append(f"[axial_coding] category {cid} 的代表語句 {ref} 沒有整理後引言")
            continue
        own = set(category.get("evidence_refs") or [])
        picked = category.get("representative_evidence_refs")
        if not picked:
            errors.append(f"[axial_coding] category {cid} 缺少代表語句（representative_evidence_refs）")
        for ref in picked or []:
            if ref not in own:
                errors.append(f"[axial_coding] category {cid} 的代表語句 {ref} 不在這個主題的 evidence_refs 裡")
    return errors


def _axial_coding_errors(data: object, evidence_ids: set[str]) -> list[str]:
    errors = V.structural_errors(data, V.load_schema("axial-coding"))
    if not isinstance(data, dict):
        return errors
    for category in data.get("categories") or []:
        if not isinstance(category, dict):
            continue
        for ref in category.get("evidence_refs") or []:
            if ref not in evidence_ids:
                errors.append(f"[axial_coding] category {category.get('id')} 引用不存在的證據：{ref}")
    return errors


# axial-label-quality-contract (2026-10-01): deterministic label quality
# signals. They are recorded, never raised as validation errors -- a failed
# validation reruns the whole axial-coding call (tens of thousands of tokens),
# and whether a label names the right actor or one relationship is semantic
# judgement no rule can make reliably. Those stay with the method prompt, its
# whole-set self-review, the axial-coding-quality skill and the researcher.
AXIAL_LABEL_SAME_OPENING_RUN = 3
_LABEL_OPENING_STRIP = re.compile(r"[\s\W_]+", re.UNICODE)
# "A 造成／形成／導致／驅動 B" written as one clause: an ontology-style label
# that hides who acts. A comma means the label is already split into a
# sentence and is left alone.
_NOUN_CHAIN_LABEL = re.compile(r"^[^，,、；;]{2,}(?:造成|形成|導致|驅動)[^，,、；;]{2,}$")


def _label_opening(label: str) -> str:
    """The first two characters of a label once whitespace and punctuation are removed."""
    return _LABEL_OPENING_STRIP.sub("", str(label or ""))[:2]


def axial_label_quality_signals(data: object) -> list[dict]:
    """Quality signals for a produced axial-coding document's labels.

    `same_opening_run`: three or more adjacent labels share an opening.
    `noun_chain_template`: a one-clause "A 造成 B" style label.
    `name_equals_label`: `category_name` repeats the label verbatim.
    Empty or over-long labels are schema errors already, not signals.
    """
    if not isinstance(data, dict):
        return []
    categories = [c for c in data.get("categories") or [] if isinstance(c, dict)]
    signals: list[dict] = []
    run: list[dict] = []
    for category in categories:
        label = str(category.get("label") or "").strip()
        opening = _label_opening(label)
        if run and opening and opening == _label_opening(run[-1].get("label")):
            run.append(category)
        else:
            run = [category]
        if len(run) == AXIAL_LABEL_SAME_OPENING_RUN:
            signals.append({"signal": "same_opening_run", "opening": opening,
                            "categories": [c.get("id") for c in run]})
        elif len(run) > AXIAL_LABEL_SAME_OPENING_RUN:
            signals[-1]["categories"].append(category.get("id"))
        if label and _NOUN_CHAIN_LABEL.match(label):
            signals.append({"signal": "noun_chain_template", "categories": [category.get("id")], "label": label})
        name = str(category.get("category_name") or "").strip()
        if name and name == label:
            signals.append({"signal": "name_equals_label", "categories": [category.get("id")], "label": label})
    return signals


def axial_coding(concepts: list, evidence: list, opts: dict) -> tuple:
    """feed-axial-coding-the-open-coding-concepts：把開放編碼產出的概念聚成
    有解釋力的類別，不是重新從逐字稿原話分類一次（那是這支 change 修掉的
    行為——見 design.md「Axial coding receives concepts, and the model
    still cites evidence ids directly」）。

    獨立指令（`sbp-gen axial-coding --run <dir>`）呼叫這個函式，不進主管線
    ——現在沒有任何 stage 消費它的產出，見 design.md「`axial-coding` is a
    standalone CLI command, not a pipeline stage」。

    `evidence` 只用來驗證模型引用的證據編號真的存在（`_axial_coding_errors`），
    不會出現在送給模型的訊息裡——那份訊息只由 `concepts` 組成。
    `evidence.yaml` 本身沒有 `case_id` 欄位（見 evidence.schema.json），跟
    其他 stage 不一樣不接受 `case` 參數——呼叫端把 case_id 放進
    `opts["case_id"]`。
    """
    case_id = opts.get("case_id") or "unknown"
    evidence_ids = {e["id"] for e in evidence if isinstance(e, dict) and e.get("id")}
    user = _axial_coding_user_message(concepts, case_id, evidence)
    # `axial_coding` is registered in STAGE_SCHEMA_KINDS but this call used to
    # pass `_prompt(...)` (hand-written prose only) instead of `_system(...)`
    # (prose plus the schema-derived output contract) -- the same bug already
    # fixed for journey/structure, never applied here. Discovered empirically
    # during this change's real-data verification run: without the derived
    # contract the model never learns the required field names and every
    # response fails structural validation on the first try.
    concept_ids = {c.get("id") for c in concepts if isinstance(c, dict) and c.get("id")}
    # converge-coding-contracts: open codes that carry readable quotes switch
    # representative quotes to representative_quote_refs.
    quoted = {c.get("id") for c in concepts if isinstance(c, dict) and c.get("quote")}
    quoted_ids = quoted if concepts and len(quoted) == len(concept_ids) else None
    data, usage = _generate(
        "axial_coding", _system("axial_coding"), user,
        lambda d: _axial_generation_errors(d, evidence_ids, concept_ids, quoted_ids), opts,
    )
    data["case_id"] = case_id
    return data, usage


def _group_evidence_by_source(evidence: list) -> list[tuple[str | None, list]]:
    """add-open-coding-stage：開放編碼的分析單位是 `source`（一份逐字稿），
    不是整個 run 一次處理。依 `source` 欄位分組，保留第一次出現的順序；
    沒有 `source`（或空字串）的記錄不丟棄，收進同一組（key 為 `None`）。
    """
    order: list[str | None] = []
    groups: dict[str | None, list] = {}
    for record in evidence:
        if not isinstance(record, dict):
            continue
        source = record.get("source")
        key = source if isinstance(source, str) and source.strip() else None
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(record)
    return [(key, groups[key]) for key in order]


# open-coding-on-semantic-segments (2026-10-03): a semantic segment carries
# at most this many open codes; the prompt states the same number.
OPEN_CODING_MAX_CODES_PER_SEGMENT = 2


def _open_coding_user_message(segments: list, evidence_by_id: dict, case_id: str) -> str:
    """The transcript's in-scope semantic segments: full respondent passage,
    the interviewer's question, a reading summary, and the evidence units the
    segment contains (the only evidence a code may cite)."""
    blocks = []
    for segment in segments:
        blocks.append({
            "segment": segment["id"], "summary": segment.get("summary"), "role": segment.get("role"),
            "interviewer": segment.get("question_passage") or "",
            "respondent": segment.get("respondent_passage") or "",
            "evidence": [_evidence_unit(evidence_by_id[ref], "id", "text")
                         for ref in segment.get("evidence_refs") or [] if ref in evidence_by_id],
        })
    return (
        f"## 案例\ncase_id: {case_id}\n\n"
        "## 逐字稿的語意段落（單一來源，依時間順序）\n```yaml\n" + _yaml_dump(blocks) + "```"
    )


def _open_coding_quote_error(cid, answer, question, refs: list, passages: dict) -> str:
    """calibrate-coding-analysis-unit: say what failed, for a quote that the
    deletion-only check already refused. Message only; acceptance unchanged."""
    head = f"[open_coding] concept {cid} 的引言不是只刪不增"
    answer = str(answer or "")
    for ref in refs:
        respondent, interviewer = passages[ref]
        if _is_verbatim_excerpt(answer, respondent) and question:
            echoed = "；這句出現在受訪者自己的話裡，不是訪員的問題" if _is_verbatim_excerpt(question, respondent) else ""
            return (f"{head}：回答沒問題，但 question「{question}」不在 {ref} 訪員說的話裡{echoed}。"
                    "question 只放訪員的問題，回答讀得懂就不要附")
    if any(_is_verbatim_excerpt(answer, passages[ref][1]) for ref in refs):
        return (f"{head}：回答「{answer}」是訪員說的話，不是受訪者的回答。"
                "answer 只能取自受訪者原話；受訪者只回短答時，把訪員的問題放進 question")
    if any(set(answer) - set(" \n") <= set(passages[ref][0]) for ref in refs):
        return (f"{head}：回答「{answer}」的字都在受訪者原話裡，但不是同一段連續的話。"
                "只取一段連續的原話，可以刪字，不要把相隔較遠的部分拼在一起")
    respondent_chars = set("".join(passages[ref][0] for ref in refs))
    missing = "".join(dict.fromkeys(ch for ch in answer if ch not in respondent_chars and not ch.isspace()))[:10]
    return (f"{head}：回答「{answer}」有字不在受訪者原話裡：{missing}。"
            "照原稿的字摘錄（簡體就寫簡體），可以刪字，不能換字或改寫")


def _open_coding_segment_errors(concepts: object, segments: list) -> list[str]:
    """Each code cites segments of this transcript and only their evidence;
    no segment carries more than OPEN_CODING_MAX_CODES_PER_SEGMENT codes."""
    if not isinstance(concepts, list):
        return []
    evidence_of = {s["id"]: set(s.get("evidence_refs") or []) for s in segments}
    passages = {s["id"]: (s.get("respondent_passage") or "", s.get("question_passage") or "") for s in segments}
    errors, counts = [], {}
    for concept in concepts:
        if not isinstance(concept, dict):
            continue
        cid, refs = concept.get("id"), concept.get("segment_refs") or []
        if not refs:
            errors.append(f"[open_coding] concept {cid} 要記 segment_refs：它讀自哪些語意段落")
            continue
        allowed = set()
        for ref in refs:
            if ref not in evidence_of:
                errors.append(f"[open_coding] concept {cid} 引用了不在這份逐字稿分析範圍內的段落：{ref}")
                continue
            allowed |= evidence_of[ref]
            counts[ref] = counts.get(ref, 0) + 1
        for ref in concept.get("evidence_refs") or []:
            if ref not in allowed:
                errors.append(f"[open_coding] concept {cid} 的證據 {ref} 不屬於它引用的段落")
        # converge-coding-contracts: a readable quote cut down (deletions only)
        # from the respondent's words of one cited segment; its question, if
        # any, from the same segment's interviewer lines.
        quote = concept.get("quote")
        if not isinstance(quote, dict) or not str(quote.get("answer") or "").strip():
            errors.append(f"[open_coding] concept {cid} 要有 quote：從它所讀段落整理出的一句引言")
            continue
        answer, question = quote.get("answer"), quote.get("question")
        fits = [ref for ref in refs if ref in passages and _is_verbatim_excerpt(answer, passages[ref][0])
                and (not question or _is_verbatim_excerpt(question, passages[ref][1]))]
        if not fits:
            errors.append(_open_coding_quote_error(cid, answer, question, [ref for ref in refs if ref in passages],
                                                   passages))
    for ref, count in counts.items():
        if count > OPEN_CODING_MAX_CODES_PER_SEGMENT:
            errors.append(f"[open_coding] 段落 {ref} 有 {count} 個概念，一段最多 {OPEN_CODING_MAX_CODES_PER_SEGMENT} 個")
    return errors


# --------------------------------------------------------------------------
# experiment-open-coding-partial-repair
# --------------------------------------------------------------------------
# One transcript failed only the two-concepts-per-segment cap: redo just the
# over-cap segments and keep every concept that passed. The model only
# produces replacement concepts; removing, inserting and numbering are done
# here, so nothing outside the targeted segments can change.
_OPEN_CODING_CAP_ERROR_RE = re.compile(r"^\[open_coding\] 段落 (\S+) 有 \d+ 個概念，一段最多 \d+ 個$")
# extend-open-coding-partial-repair: two more errors that point at one concept,
# so at the segments it cites. Bound to the messages `validate_sbp.structural_errors`
# and `_open_coding_quote_error` write (see the partial-repair tests).
_OPEN_CODING_LENGTH_ERROR_RE = re.compile(r"^\[結構\] concepts/(\d+)/(name|description): .* is too long$", re.S)
_OPEN_CODING_QUESTION_ERROR_RE = re.compile(r"^\[open_coding\] concept (\S+) 的引言不是只刪不增：回答沒問題，但 question「")


def _concepts_of(data: object) -> list | None:
    """The concept list of an open coding output, whichever shape it has."""
    concepts = data.get("concepts") if isinstance(data, dict) else data
    return concepts if isinstance(concepts, list) else None


def _open_coding_cap_targets(data: object, errors: list) -> frozenset | None:
    """What a partial repair would redo, or None when it must not run.

    Targets are strings: a segment over the cap (`SEG-4`, recoded whole), a
    concept field that failed (`OC-12#name`, `OC-12#description`,
    `OC-12#question`, only that field is changed), and a coupled segment
    (`~SEG-9`): another segment a recoded concept cites, which the recoded
    concepts may cite again and which must stay cited. Every error has to be
    one of the recognised kinds (open-coding-coupled-repair)."""
    concepts = _concepts_of(data)
    if concepts is None or not errors or not all(isinstance(c, dict) for c in concepts):
        return None
    for concept in concepts:
        refs = concept.get("segment_refs") or []
        if not isinstance(refs, list) or any(not isinstance(r, str) or "#" in r or r.startswith("~") for r in refs):
            return None
    by_id = {c.get("id"): c for c in concepts}
    segments, fields = set(), set()
    for error in errors:
        text = str(error)
        if match := _OPEN_CODING_CAP_ERROR_RE.match(text):
            segments.add(match.group(1))
            continue
        if match := _OPEN_CODING_LENGTH_ERROR_RE.match(text):
            index = int(match.group(1))
            concept, field = (concepts[index] if index < len(concepts) else None), match.group(2)
        elif match := _OPEN_CODING_QUESTION_ERROR_RE.match(text):
            concept, field = by_id.get(match.group(1)), "question"
        else:
            return None
        if (not isinstance(concept, dict) or not isinstance(concept.get("id"), str)
                or list(by_id).count(concept["id"]) != 1 or not concept.get("segment_refs")):
            return None
        fields.add((concept["id"], field))
    recoded = [c for c in concepts if segments.intersection(c.get("segment_refs") or [])]
    recoded_ids = {c.get("id") for c in recoded}
    coupled = {ref for c in recoded for ref in c.get("segment_refs") or []} - segments
    return frozenset(segments | {f"{cid}#{field}" for cid, field in fields if cid not in recoded_ids}
                     | {f"~{ref}" for ref in coupled})


def _open_coding_target_parts(targets: frozenset) -> tuple[set, dict, set]:
    """(segments to recode, {concept id: fields to change}, coupled segments)."""
    segments = {t for t in targets if "#" not in t and not t.startswith("~")}
    coupled = {t[1:] for t in targets if t.startswith("~")}
    fields: dict = {}
    for target in targets:
        if "#" in target:
            cid, field = target.split("#", 1)
            fields.setdefault(cid, set()).add(field)
    return segments, fields, coupled


_OPEN_CODING_FIELD_LABELS = {"name": "名稱", "description": "描述", "question": "question"}


def _open_coding_partial_repair_closing(targets: frozenset, errors=()) -> str:
    """The last paragraph of a partial repair request: which segments and
    fields failed which existing rules, and to redo only those. Nothing else is
    said; with cap errors only and no coupled segment (or no errors given) it
    reads as it always has."""
    segments, fields, coupled = _open_coding_target_parts(targets)
    texts = [str(e) for e in errors]
    rules = [f"一段最多 {OPEN_CODING_MAX_CODES_PER_SEGMENT} 個概念"] if (
        not texts or any(_OPEN_CODING_CAP_ERROR_RE.match(t) for t in texts)) else []
    if any(_OPEN_CODING_LENGTH_ERROR_RE.match(t) for t in texts):
        rules.append("概念名稱與描述不能超過字數上限")
    if any(_OPEN_CODING_QUESTION_ERROR_RE.match(t) for t in texts):
        rules.append("question 只放訪員的問題")
    failed = []
    if segments:
        failed.append(f"沒通過的是這幾段：{'、'.join(sorted(segments))}。")
    if fields:
        named = "、".join(f"{cid} 的{_OPEN_CODING_FIELD_LABELS[f]}" for cid in sorted(fields) for f in sorted(fields[cid]))
        failed.append(f"沒通過的欄位是：{named}。")
    asks = []
    if segments:
        asks.append("請只重做這幾段，其餘概念維持不變，也不用回傳。")
    if coupled:
        asks.append(f"重做的概念可以照原文同時引用 {'、'.join(sorted(coupled))}。")
    if fields:
        asks.append("被點名的概念只改那個欄位，保留原本的 id 與其他欄位。")
    if not segments:
        asks.append("其餘概念維持不變，也不用回傳。")
    returned = "這幾段的概念與被點名的概念" if segments and fields else ("這幾段的概念" if segments else "被點名的概念")
    return (
        "".join(failed)
        + f"它們沒有通過既有的規則（{'；'.join(rules)}）。"
        + "".join(asks)
        + f"只回傳{returned}，格式是 {{\"concepts\": [...]}}。"
    )


def _splice_open_coding_repair(previous: object, targets: frozenset, reply: object) -> tuple:
    """Put the repair into the previous output. Returns (merged, None), or
    (None, reason) when the reply must be abandoned.

    A target field is copied from the reply's concept with the same id onto
    the original concept, and nothing else of that concept is taken. A recoded
    concept has to cite a target segment and may cite only target or coupled
    segments; every target segment has to be covered and every coupled segment
    still cited afterwards (an omission is never a deletion). Concepts that
    cite no target segment are kept as they were, apart from their target
    fields."""
    base, replies = _concepts_of(previous), _concepts_of(reply)
    if base is None or not replies or not all(isinstance(c, dict) for c in replies):
        return None, "reply_shape"
    segments, fields, coupled = _open_coding_target_parts(targets)
    fixes, replacements = {}, []
    for concept in replies:
        cid = concept.get("id")
        if cid in fields and not segments.intersection(concept.get("segment_refs") or []):
            if cid in fixes:
                return None, "reply_shape"
            fixes[cid] = concept
        else:
            replacements.append(concept)
    if set(fixes) != set(fields):
        return None, "target_not_covered"
    covered: set = set()
    for concept in replacements:
        refs = concept.get("segment_refs")
        if (not isinstance(refs, list) or not refs or not segments.intersection(refs)
                or not set(refs) <= segments | coupled):
            return None, "reply_cites_outside_targets"
        covered.update(set(refs) & segments)
    if covered != segments:
        return None, "target_not_covered"
    kept, removed, slot = [], [], {}
    for concept in base:
        refs = concept.get("segment_refs") or []
        if segments.intersection(refs):
            removed.append(concept)
            for ref in segments.intersection(refs):
                slot.setdefault(ref, len(kept))  # where this segment's concepts used to start
            continue
        if concept.get("id") in fields:
            fixed, reason = _open_coding_field_fix(concept, fixes[concept["id"]], fields[concept["id"]])
            if reason:
                return None, reason
            concept = fixed
        kept.append(concept)
    if segments and not removed:
        return None, "nothing_to_replace"
    cited = {ref for c in [*kept, *replacements] for ref in c.get("segment_refs") or []}
    if not coupled <= cited:
        return None, "coupled_segment_uncovered"
    freed = [c["id"] for c in removed if isinstance(c.get("id"), str)]
    taken = {c.get("id") for c in kept}
    numbers = [int(m.group(1)) for i in [*taken, *freed] if isinstance(i, str) and (m := re.fullmatch(r"OC-(\d+)", i))]
    width = len(freed[0]) - 3 if freed and re.fullmatch(r"OC-0\d*", freed[0]) else 0
    next_number = max(numbers, default=0) + 1
    groups: dict = {}
    for index, concept in enumerate(replacements):
        if index < len(freed):
            new_id = freed[index]
        else:
            new_id = f"OC-{next_number:0{width}d}"
            next_number += 1
        taken.add(new_id)
        # back where its (first) target segment's concepts used to start, reply order kept
        groups.setdefault(min(slot[ref] for ref in concept["segment_refs"] if ref in slot), []).append(
            {**concept, "id": new_id})
    merged = []
    for index, concept in enumerate(kept):
        merged.extend(groups.get(index, []))
        merged.append(concept)
    merged.extend(groups.get(len(kept), []))
    return ({**previous, "concepts": merged} if isinstance(previous, dict) else merged), None


def _open_coding_field_fix(original: dict, reply: dict, fields: set) -> tuple:
    """Copy only the named fields from the reply's concept onto a copy of the
    original. The concept's segments must not change; a question fix keeps the
    answer and only sets or removes the question."""
    if reply.get("segment_refs") != original.get("segment_refs"):
        return None, "reply_changes_untargeted"
    fixed = copy.deepcopy(original)
    for field in fields:
        if field in ("name", "description"):
            if not isinstance(reply.get(field), str):
                return None, "reply_shape"
            fixed[field] = reply[field]
        else:
            before, after = original.get("quote"), reply.get("quote")
            if not isinstance(before, dict) or not isinstance(after, dict) or after.get("answer") != before.get("answer"):
                return None, "reply_changes_untargeted"
            quote = dict(fixed["quote"])
            if after.get("question"):
                quote["question"] = after["question"]
            else:
                quote.pop("question", None)
            fixed["quote"] = quote
    return fixed, None


class _OpenCodingPartialRepair:
    """What `_generate` needs to run a partial repair for open coding."""
    plan = staticmethod(_open_coding_cap_targets)
    closing = staticmethod(_open_coding_partial_repair_closing)
    splice = staticmethod(_splice_open_coding_repair)


def _partial_repair_record(targets: frozenset, outcome: str, reason: str | None = None,
                           key: str = "targeted_segments") -> dict:
    """Observability only: was a partial repair attempted, which segments, did
    it succeed or fall back and why. Tokens, seconds and attempt numbers are
    already on the call record, so they are not repeated. Nothing reads this.
    `key` names the targets field; a stage whose targets are not segments
    (experiment-structure-relation-repair) names its own."""
    return {"attempted": True, key: sorted(targets), "outcome": outcome, "fallback_reason": reason}


def open_coding(evidence: list, opts: dict, segments_doc: dict | None = None) -> tuple:
    """add-open-coding-stage：把 align 產出的證據單元逐句轉成行為概念，
    是主軸編碼之前原本不存在的一層（見 openspec/LANGUAGE.md「開放編碼這一層
    原本不存在」的決定）。

    獨立指令（`sbp-gen open-coding --run <dir>`），每個 `source` 各呼叫一次
    模型，經同一套重試迴圈，最後合併成一份 `open_coding.yaml`——合併只是把
    每組的 `concepts` 接起來，不跨組合併同名概念：跨逐字稿的語意合併是研究者
    在選擇編碼那一步的判斷，不是這裡自動做的事。任何一組耗盡重試次數，整支
    指令回傳非零、不寫任何檔案——半份文件比沒有文件更容易被誤用成完整結果。
    """
    # open-coding-on-semantic-segments: the unit is the semantic segment, so a
    # new generation needs the run's segments; there is no evidence-only path.
    if not isinstance(segments_doc, dict) or not segments_doc.get("transcripts"):
        raise ValueError("這份 run 還沒有語意段落；開放編碼要先完成語意分段。")
    case_id = opts.get("case_id") or "unknown"
    evidence_by_id = {e["id"]: e for e in evidence if isinstance(e, dict) and e.get("id")}
    segments_by_source = open_coding_segments(segments_doc)
    sources = [source for source, segments in segments_by_source.items() if segments]
    # per-source calls are recorded in usage["calls"] (measure-open-coding-
    # wall-clock-gap) so one slow transcript stays visible.
    progress_lock, finished = threading.Lock(), [0]

    def one(source) -> tuple[list, dict]:
        number = sources.index(source) + 1
        concepts, call = open_code_transcript(source, segments_by_source[source], evidence_by_id, case_id, opts,
                                              number, len(sources))
        with progress_lock:
            finished[0] += 1
            _report_progress("開放編碼", finished[0], len(sources), "份")
        return concepts, call

    # Each source is an independent transcript with no cross-source rule, so
    # up to CODING_CONCURRENCY_DEFAULT are coded at once (SBP_OPEN_CODING_CONCURRENCY), the
    # largest first; results keep source order and any failed source raises
    # here, before anything is merged or written. A hand-relayed turn is answered one prompt at a time.
    workers = open_coding_concurrency(opts)
    wall_started = time.monotonic()
    if len(sources) > 1 and workers > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(sources))) as pool:
            results = map_largest_first(pool, one, sources, lambda source: len(str(segments_by_source[source])))
    else:
        results = [one(source) for source in sources]
    return merge_open_coding(results, case_id, time.monotonic() - wall_started,
                             min(workers, len(sources)) if sources else 0)


def open_coding_concurrency(opts: dict) -> int:
    workers = max(1, int(opts.get("open_coding_concurrency")
                         or os.environ.get("SBP_OPEN_CODING_CONCURRENCY") or CODING_CONCURRENCY_DEFAULT))
    if (os.environ.get("SBP_LLM_PROVIDER") or "").strip().lower() == "transport":
        workers = 1
    return workers


def open_coding_segments(segments_doc: dict) -> dict:
    """Each transcript's in-scope segments, in upload order."""
    return {t["source"]: [s for s in t.get("segments") or [] if isinstance(s, dict) and s.get("in_scope")]
            for t in segments_doc["transcripts"] if isinstance(t, dict) and t.get("source")}


def open_code_transcript(source, segments: list, evidence_by_id: dict, case_id: str, opts: dict,
                         number: int, total: int) -> tuple[list, dict]:
    """One transcript's open coding call (validated, retried): (concepts, call)."""
    evidence_ids = set(evidence_by_id)
    record_ids = {ref for s in segments for ref in s.get("evidence_refs") or [] if ref in evidence_by_id}
    user = _open_coding_user_message(segments, evidence_by_id, case_id)
    # Records with no recorded source are grouped under key None (see
    # _group_evidence_by_source); the schema requires a non-empty
    # `source` string per concept, so substitute an explicit placeholder
    # rather than writing None into the produce layer.
    source_label = source if source is not None else "(未記錄來源)"

    def _validate(partial, _record_ids=record_ids, _source=source_label):
        # The model only produces `concepts` (see the prompt's 輸出格式
        # section); schema_version/case_id/source are known to the
        # caller already and injected here before validating against
        # the full open-coding schema, same pattern as
        # selective_coding_narrative's merged-candidate validation.
        raw_concepts = partial.get("concepts") if isinstance(partial, dict) else partial
        concepts_with_source = (
            [{**c, "source": _source} if isinstance(c, dict) else c for c in raw_concepts]
            if isinstance(raw_concepts, list) else raw_concepts
        )
        candidate = {"schema_version": 1, "case_id": case_id, "concepts": concepts_with_source}
        errors = V.open_coding_errors(candidate, evidence_ids)
        errors += _open_coding_segment_errors(candidate.get("concepts"), segments)
        if isinstance(candidate.get("concepts"), list):
            for concept in candidate["concepts"]:
                if not isinstance(concept, dict):
                    continue
                for ref in concept.get("evidence_refs") or []:
                    if ref not in _record_ids:
                        errors.append(
                            f"[open_coding] concept {concept.get('id')} 引用了不屬於這個來源的證據：{ref}"
                        )
        return errors

    # Rejected attempts are kept per transcript when opts carry a
    # diagnostics_dir (observability only; retries are unchanged).
    call_opts = {**opts, "diagnostics_label": f"open-coding-{number:02d}-of-{total:02d}",
                 "diagnostics_source": source_label}
    data, usage = _generate("open_coding", _prompt("open_coding"), user, _validate, call_opts,
                            partial_repair=_OpenCodingPartialRepair)
    call = {
        "source": source_label,
        "seconds": usage.get("seconds", 0),
        "wall_seconds": usage.get("wall_seconds", 0),
        "started_at": usage.get("started_at"),
        "finished_at": usage.get("finished_at"),
        "executor": usage.get("executor"),
        "tokens": usage.get("tokens", 0),
        "attempts": usage.get("attempts"),
        "model_requested": usage.get("model_requested"),
        "effort_requested": usage.get("effort_requested"),
        **{key: usage[key] for key in ("model_source", "model_effective") if usage.get(key)},
        **({"token_detail": usage["token_detail"]} if usage.get("token_detail") else {}),
        **({"partial_repair": usage["partial_repair"]} if usage.get("partial_repair") else {}),
    }
    concepts = []
    for concept in data.get("concepts") or []:
        if isinstance(concept, dict):
            concept.setdefault("source", source_label)
            concepts.append(concept)
    return concepts, call


def merge_open_coding(results: list, case_id: str, wall_seconds: float, workers: int) -> tuple[dict, dict]:
    """Join the transcripts' concepts in upload order and number them."""
    all_concepts = [concept for concepts, _call in results for concept in concepts]
    per_source_seconds = [call for _concepts, call in results]
    # Each group's model call numbers its own concepts starting at OC-1, so
    # ids collide across groups after merging -- renumber sequentially here
    # so every concept id is unique in the merged document (ids are the
    # produce-layer join key; the schema pattern only constrains shape, not
    # cross-group uniqueness, since each group is validated independently
    # during generation).
    for index, concept in enumerate(all_concepts, start=1):
        concept["id"] = f"OC-{index}"
    merged = {"schema_version": 1, "case_id": case_id, "concepts": all_concepts}
    detail: dict = {}
    for call in per_source_seconds:
        for kind, count in (call.get("token_detail") or {}).items():
            detail[kind] = detail.get(kind, 0) + count
    first = per_source_seconds[0] if per_source_seconds else {}
    return merged, {"tokens": sum(call.get("tokens", 0) for call in per_source_seconds),
                     "seconds": round(sum(call.get("seconds", 0) for call in per_source_seconds)),
                     "wall_seconds": round(wall_seconds), "calls": per_source_seconds,
                     "concurrency": workers,
                     "executor": first.get("executor"),
                     "attempts": sum(call.get("attempts") or 0 for call in per_source_seconds) or None,
                     "model_requested": first.get("model_requested"),
                     "effort_requested": first.get("effort_requested"),
                     **{key: first[key] for key in ("model_source", "model_effective") if first.get(key)},
                     "started_at": min((call.get("started_at") or "" for call in per_source_seconds),
                                       default=None) or None,
                     "finished_at": max((call.get("finished_at") or "" for call in per_source_seconds),
                                        default=None) or None,
                     **({"token_detail": detail} if detail else {})}


def _category_properties_user_message(categories_doc: dict, evidence: list, case_id: str) -> str:
    evidence_by_id = {e["id"]: e for e in evidence if isinstance(e, dict) and e.get("id")}
    blocks = []
    for category in categories_doc.get("categories") or []:
        if not isinstance(category, dict):
            continue
        refs = category.get("evidence_refs") or []
        texts = [
            _evidence_text_entry(evidence_by_id[ref]) for ref in refs
            if isinstance(evidence_by_id.get(ref), dict) and evidence_by_id[ref].get("text")
        ]
        blocks.append({
            "id": category.get("id"), "label": category.get("label"),
            "discriminating_basis": category.get("discriminating_basis"),
            "evidence_refs": refs, "evidence_texts": texts,
        })
    return (
        f"## 案例\ncase_id: {case_id}\n\n"
        "## 主軸編碼類別與其證據\n```yaml\n" + _yaml_dump(blocks) + "```"
    )


def category_properties(categories_doc: dict, evidence: list, opts: dict) -> tuple:
    """derive-axis-candidates-from-properties：紮根理論在類別跟 Persona
    Matrix 軸之間原本缺的一層——屬性（這個類別可以從哪個角度看它變化）與
    面向（那個角度的兩端，各自帶設計後果）。一個類別可以有零到多個屬性，
    不設下限也不設上限；每個面向的兩端寫不出各自的設計後果，就代表這個
    屬性不該存在，不硬填。

    模型只需要產出 `properties`（不含 id／schema_version／case_id，見
    `category_properties.md` 的輸出格式段），`id` 由這支函式依序指派
    `PROP-1`、`PROP-2`⋯——避免同一次呼叫裡模型自己編號时前後不一致或跳號。
    """
    case_id = opts.get("case_id") or "unknown"
    categories = [c for c in (categories_doc.get("categories") or []) if isinstance(c, dict) and c.get("id")]
    category_ids = {c["id"] for c in categories}
    evidence_ids = {e["id"] for e in evidence if isinstance(e, dict) and e.get("id")}

    def number(raw: list) -> list:
        return [{**p, "id": f"PROP-{i + 1}"} if isinstance(p, dict) else p for i, p in enumerate(raw)]

    # resume-category-properties-units: a category that passed its own
    # validation is saved at once, outside the run directory, so a step that
    # fails partway keeps it; a retry reuses it only when everything that was
    # sent for it is unchanged and it still validates. Callers that give no
    # place for saved units get the step as before.
    checkpoint_dir = Path(opts["checkpoint_dir"]) if opts.get("checkpoint_dir") else None

    def fingerprint(user: str) -> str:
        return lifecycle.canonical_content_digest({
            "stage": "category_properties",
            "system_prompt": _prompt("category_properties"),
            "user": user,
            "provider": opts.get("category_properties_provider") or os.environ.get("SBP_LLM_PROVIDER"),
            "executor": os.environ.get("SBP_CLI_EXECUTOR"),
            "model": opts.get("category_properties_model"),
            "effort": opts.get("category_properties_effort"),
            "method_version": opts.get("category_properties_method_version"),
        })

    def saved_unit(category_id: str, unit_print: str, validate) -> list | None:
        import yaml
        try:
            unit = yaml.safe_load((checkpoint_dir / f"{category_id}.yaml").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - an unreadable unit is regenerated, never a failure
            return None
        if (not isinstance(unit, dict) or unit.get("fingerprint") != unit_print
                or not isinstance(unit.get("properties"), list)):
            return None
        if validate({"properties": unit["properties"]}):
            return None
        return unit["properties"]

    def save_unit(category_id: str, unit_print: str, props: list) -> None:
        try:
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            run_files.write_yaml_atomic(checkpoint_dir / f"{category_id}.yaml",
                                        {"fingerprint": unit_print, "category_id": category_id, "properties": props})
        except OSError:
            pass  # saving only spares a retry; the step itself does not depend on it

    # One call per category (2026-09-30): a property belongs to exactly one
    # category and the prompt has no cross-category rule, so each category is
    # read on its own. That keeps each call well inside the CLI runner's
    # per-call limit and lets calls run side by side; ids are assigned once,
    # after merging, in category order.
    def one(category: dict) -> tuple[list, dict]:
        single = {**categories_doc, "categories": [category]}
        user = _category_properties_user_message(single, evidence, case_id)

        def _validate(partial):
            raw = partial.get("properties") if isinstance(partial, dict) else partial
            candidate = {"schema_version": 1, "case_id": case_id,
                         "properties": number(raw) if isinstance(raw, list) else raw}
            errors = V.category_properties_errors(candidate, category_ids, evidence_ids)
            for prop in raw if isinstance(raw, list) else []:
                if isinstance(prop, dict) and prop.get("category_id") != category["id"]:
                    errors.append(f"[category_properties] 這一次只讀類別 {category['id']}，"
                                  f"屬性不能掛到 {prop.get('category_id')}")
            return errors

        unit_print = fingerprint(user) if checkpoint_dir is not None else None
        reused = saved_unit(category["id"], unit_print, _validate) if checkpoint_dir is not None else None
        if reused is not None:
            with progress_lock:
                finished[0] += 1
                _report_progress("屬性與面向", finished[0], len(categories), "類")
            return reused, {"reused": True, "tokens": 0, "seconds": 0, "attempts": 0}

        # Calls run concurrently, so each category keeps its own rejected-attempt
        # record (observability only; the request and retries are unchanged).
        call_opts = {**opts, "diagnostics_label": f"category-properties-{category['id']}",
                     "diagnostics_source": category["id"]}
        data, usage = _generate("category_properties", _prompt("category_properties"), user, _validate, call_opts)
        props = [p for p in data.get("properties") or [] if isinstance(p, dict)]
        if checkpoint_dir is not None:
            save_unit(category["id"], unit_print, props)
        with progress_lock:
            finished[0] += 1
            _report_progress("屬性與面向", finished[0], len(categories), "類")
        return props, usage

    started_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    wall_started = time.monotonic()
    progress_lock, finished = threading.Lock(), [0]
    workers = max(1, int(opts.get("category_properties_concurrency")
                         or os.environ.get("SBP_CATEGORY_PROPERTIES_CONCURRENCY") or 3))
    if (os.environ.get("SBP_LLM_PROVIDER") or "").strip().lower() == "transport":
        workers = 1  # a hand-relayed turn is answered one prompt at a time
    if categories:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(categories))) as pool:
            results = list(pool.map(one, categories))
    else:
        results = []
    properties = number([prop for props, _usage in results for prop in props])
    result = {"schema_version": 1, "case_id": case_id, "properties": properties}
    errors = V.category_properties_errors(result, category_ids, evidence_ids)
    if errors:
        if checkpoint_dir is not None:
            # The same saved units would fail the same way again.
            shutil.rmtree(checkpoint_dir, ignore_errors=True)
        raise llm.LLMError("category_properties 合併結果未通過驗證：\n" + "\n".join(errors[:20]))
    calls = [{"category_id": category["id"], **usage} for category, (_props, usage) in zip(categories, results)]
    usage = {
        "tokens": sum(call.get("tokens", 0) for call in calls),
        "seconds": sum(call.get("seconds", 0) for call in calls),
        "wall_seconds": round(time.monotonic() - wall_started),
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "executor": os.environ.get("SBP_CLI_EXECUTOR") or "codex",
        "stage": "category_properties",
        "attempts": sum(call.get("attempts", 1) for call in calls),
        "concurrency": min(workers, len(categories)) if categories else 0,
        "calls": calls,
    }
    return result, usage


def _selective_coding_errors(data: object, category_ids: set[str]) -> list[str]:
    """add-selective-coding：純函式，跟 `_axial_coding_errors` 一樣不做任何
    檔案讀寫、不呼叫模型——只檢查 schema 沒辦法表達的兩件事：`core_category_id`
    是不是真的在主軸編碼產出的類別清單裡、`rationale` 去掉空白後是不是空的
    （schema 的 `minLength: 1` 擋得住空字串，擋不住只有空白的字串）。
    """
    errors = V.structural_errors(data, V.load_schema("selective-coding"))
    if not isinstance(data, dict):
        return errors
    core_id = data.get("core_category_id")
    if isinstance(core_id, str) and core_id not in category_ids:
        errors.append(f"[selective_coding] core_category_id 不在 axial_coding.yaml 的類別清單裡：{core_id}")
    rationale = data.get("rationale")
    if isinstance(rationale, str) and not rationale.strip():
        errors.append("[selective_coding] rationale 不能是空白")
    return errors


def _selective_coding_narrative_user_message(categories_doc: dict, selective_coding_doc: dict,
                                              conflicts: list, evidence: list, case_id: str) -> str:
    core_id = selective_coding_doc.get("core_category_id")
    categories = categories_doc.get("categories") or []
    core = next((c for c in categories if isinstance(c, dict) and c.get("id") == core_id), None)
    payload = {
        "case_id": case_id,
        "researcher_chosen_core_category": core,
        "all_categories": categories,
        "conflicts": conflicts,
        "evidence": [
            _evidence_unit(e, "id", "role", "text")
            for e in evidence if isinstance(e, dict)
        ],
    }
    return "研究者已選定的核心類別、全部主軸編碼類別、已記錄的矛盾與證據如下：\n\n```yaml\n" + _yaml_dump(payload) + "```"


def selective_coding_narrative(categories_doc: dict, selective_coding_doc: dict, conflicts: list,
                                evidence: list, opts: dict) -> tuple:
    """selective-coding-produces-a-narrative：選擇編碼原本只記一個 id 加一句
    理由，方法論第六步要求的是把全部類別串成一個有解釋力的故事——次要主題、
    150–200 字洞察敘事、3–5 條關鍵洞察。這一支補上後半段，`core_category_id`
    與 `rationale`（研究者的判斷）完全不動，新欄位合併進同一份文件，標成
    `narrative_composed_by: assistant`（CLAUDE.md 的分工：助理綜合，研究者
    裁定；已經裁定的核心類別不在這裡重新評估）。
    """
    case_id = opts.get("case_id") or "unknown"
    evidence_ids = {e["id"] for e in evidence if isinstance(e, dict) and e.get("id")}
    user = _selective_coding_narrative_user_message(categories_doc, selective_coding_doc, conflicts, evidence, case_id)

    def _validate(partial):
        # The model only produces the three narrative fields; validate the
        # merged document (core_category_id/rationale untouched, already
        # valid) since selective_coding_narrative_errors() checks the whole
        # schema, not a fragment.
        candidate = {**selective_coding_doc, **(partial if isinstance(partial, dict) else {}), "narrative_composed_by": "assistant"}
        return V.selective_coding_narrative_errors(candidate, evidence_ids)

    data, usage = _generate("selective_coding_narrative", _prompt("selective_coding_narrative"), user, _validate, opts)
    merged = {**selective_coding_doc, **data, "narrative_composed_by": "assistant"}
    return merged, usage


def _core_category_candidates_errors(data: object, category_ids: set[str]) -> list[str]:
    """propose-core-category-candidates：跟 `_selective_coding_errors` 一樣的
    純函式，檢查 schema 表達不了的兩件事：每個 candidate 的 category_id 是不是
    真的在主軸編碼的類別清單裡、三個 candidate 的 category_id 是不是彼此不同
    （schema 的 minItems/maxItems 擋得住候選數量不對，擋不住同一個類別出現
    兩次）。
    """
    errors = V.structural_errors(data, V.load_schema("core-category-candidates"))
    if not isinstance(data, dict):
        return errors
    candidates = [c for c in (data.get("candidates") or []) if isinstance(c, dict)]
    seen: set[str] = set()
    for candidate in candidates:
        cid = candidate.get("category_id")
        if not isinstance(cid, str):
            continue
        if cid not in category_ids:
            errors.append(f"[core_category_candidates] candidate 引用不存在的類別：{cid}")
        if cid in seen:
            errors.append(f"[core_category_candidates] 三個候選的 category_id 有重複：{cid}")
        seen.add(cid)
    return errors


def _propose_core_category_candidates_user_message(categories_doc: dict, evidence: list, case_id: str) -> str:
    findings = V.axial_coding_warnings(categories_doc, evidence)
    blocks = []
    for category in categories_doc.get("categories") or []:
        if not isinstance(category, dict):
            continue
        label = category.get("label")
        matches = [str(f) for f in findings if f"「{label}」" in f]
        blocks.append({
            "id": category.get("id"), "label": label,
            "discriminating_basis": category.get("discriminating_basis"),
            "evidence_refs": category.get("evidence_refs") or [],
            "judgment_findings": matches,
        })
    return (
        f"## 案例\ncase_id: {case_id}\n\n"
        "## 主軸編碼類別與既有判準發現\n```yaml\n" + _yaml_dump(blocks) + "```"
    )


def propose_core_category_candidates(categories_doc: dict, evidence: list, opts: dict) -> tuple:
    """propose-core-category-candidates：讀完全部主軸編碼類別跟既有判準發現，
    提出剛好 3 個核心類別候選並各附理由，不推薦任何一個為預設答案——挑哪一個
    是研究者的判斷，這支函式只負責讀完、排序、講重點（CLAUDE.md「助理的
    界線」）。跟 `jtbd`/`persona_matrix` 同一個形狀。
    """
    case_id = opts.get("case_id") or "unknown"
    category_ids = {c["id"] for c in (categories_doc.get("categories") or []) if isinstance(c, dict) and c.get("id")}
    user = _propose_core_category_candidates_user_message(categories_doc, evidence, case_id)
    data, usage = _generate(
        "core_category_candidates", _system("core_category_candidates"), user,
        lambda d: _core_category_candidates_errors(d, category_ids), opts,
    )
    data["case_id"] = case_id
    return data, usage


def _narrative_job_candidates_errors(data: object, category_ids: set[str]) -> list[str]:
    """propose-paired-narrative-and-job-candidates：跟 `_jtbd_errors` 同一個
    形狀的純函式，檢查 schema 表達不了的兩件事。

    解法字眼用的是 `_jtbd_errors` 已經在用的那個 `_SOLUTION_LANGUAGE`，不是
    另外寫一份——「Job 陳述不得內含解法或指名產品」是同一條規則，規則只該有
    一份。這也是這個檢查住在這裡而不是 `validate_sbp.py` 的原因：那個 pattern
    在這個模組，而 import 的方向是 stages → validate_sbp，反過來不成立。
    """
    errors = V.structural_errors(data, V.load_schema("narrative-job-candidates"))
    if not isinstance(data, dict):
        return errors
    for index, candidate in enumerate(data.get("candidates") or [], start=1):
        if not isinstance(candidate, dict):
            continue
        for cat_id in candidate.get("grounded_in") or []:
            if cat_id not in category_ids:
                errors.append(f"[narrative_job_candidates] 第 {index} 組引用不存在的類別：{cat_id}")
        # 2026-09-29 研究者裁定：站②的 JTBD 是四段。schema 為了讀舊檔讓動機可以缺，
        # 新產出在這裡要求一定要有。
        if not str(candidate.get("motivation") or "").strip():
            errors.append(f"[narrative_job_candidates] 第 {index} 組缺少動機（motivation）")
        for field in ("situation", "progress", "motivation", "outcome"):
            value = candidate.get(field)
            if isinstance(value, str) and _SOLUTION_LANGUAGE.search(value):
                errors.append(
                    f"[narrative_job_candidates] 第 {index} 組的 {field} 內含解法或指名技術／產品，"
                    f"不該出現在這裡：{value}"
                )
    return errors


def _jtbd_selection_list_errors(doc: object, category_ids: set[str], selective_ids: set[str]) -> list[str]:
    """multi-jtbd-single-job-source: a new selection is a list of one or more
    JTBDs, each held to the same checks a generated candidate faces."""
    errors = V.structural_errors(doc, V.load_schema("narrative-job-selection"))
    if not isinstance(doc, dict):
        return errors
    if doc.get("schema_version") != 2 or not isinstance(doc.get("selections"), list):
        return errors + ["[narrative_job_selection] 新的選擇要寫成 selections 清單"]
    if not doc["selections"]:
        errors.append("[narrative_job_selection] 至少要選一則顧客任務")
    for index, item in enumerate(doc["selections"], start=1):
        if not isinstance(item, dict):
            continue
        for field in ("narrative", "narrative_composed_by"):
            if field in item:
                errors.append(f"[narrative_job_selection] 第 {index} 則不收 {field}")
        # 2026-10-03 researcher ruling: an AI candidate keeps the themes that
        # really formed it; a researcher-written JTBD claims no theme.
        refs = item.get("selective_refs") or []
        if item.get("origin") not in ("ai_candidate", "researcher"):
            errors.append(f"[narrative_job_selection] 第 {index} 則要記 origin：ai_candidate 或 researcher")
        if item.get("origin") == "ai_candidate" and not refs:
            errors.append(f"[narrative_job_selection] 第 {index} 則是 AI 候選，要帶它依據的選擇主題")
        if item.get("origin") == "researcher" and refs:
            errors.append(f"[narrative_job_selection] 第 {index} 則是研究者自己寫的，不替它宣稱選擇主題支持")
        for ref in refs:
            if ref not in selective_ids:
                errors.append(f"[narrative_job_selection] 第 {index} 則引用不存在的選擇主題：{ref}")
        for ref in item.get("grounded_in") or []:
            if ref not in category_ids:
                errors.append(f"[narrative_job_selection] 第 {index} 則引用不存在的主軸主題：{ref}")
        for field in ("situation", "progress", "motivation", "outcome"):
            value = item.get(field)
            if isinstance(value, str) and _SOLUTION_LANGUAGE.search(value):
                errors.append(f"[narrative_job_selection] 第 {index} 則的 {field} 內含解法或指名技術／產品，不該出現在這裡：{value}")
    return errors


def _narrative_job_selection_errors(data: object, category_ids: set[str], *, new_write: bool = False) -> list[str]:
    """record-the-chosen-narrative-and-job-pair：研究者選定的那一則顧客任務
    要過的關，跟候選過的是同一關。

    重點在自填：研究者自己寫的文字如果不檢查，等於同一個畫面會拒絕模型
    寫的、卻放行自己寫的同一種毛病。解法字眼一樣用 `_SOLUTION_LANGUAGE`
    ——同一條規則只該有一份實作。

    `new_write`（select-jtbd-from-selective-synthesis，2026-10-01 裁定）：
    第 ② 站只記錄顧客任務。新寫入不收敘事與敘事作者，動機一定要有；
    舊選定檔（當時要選敘事）照舊通過，讀舊檔時不傳 `new_write`。
    """
    errors = V.structural_errors(data, V.load_schema("narrative-job-selection"))
    if not isinstance(data, dict):
        return errors
    if new_write:
        for field in ("narrative", "narrative_composed_by"):
            if field in data:
                errors.append(f"[narrative_job_selection] 第 2 站只記錄顧客任務，不收 {field}")
        if not str(data.get("motivation") or "").strip():
            errors.append("[narrative_job_selection] 缺少動機（motivation）")
    for cat_id in data.get("grounded_in") or []:
        if cat_id not in category_ids:
            errors.append(f"[narrative_job_selection] 引用不存在的類別：{cat_id}")
    for field in ("situation", "progress", "motivation", "outcome"):
        value = data.get(field)
        if isinstance(value, str) and _SOLUTION_LANGUAGE.search(value):
            errors.append(
                f"[narrative_job_selection] {field} 內含解法或指名技術／產品，不該出現在這裡：{value}"
            )
    return errors


def _jtbd_candidates_user_message(view: dict, case_id: str) -> str:
    """multi-jtbd-single-job-source: Selective Coding is the primary upstream
    synthesis; the cited axial themes are support only."""
    selective = [{k: v for k, v in t.items() if v not in (None, "", [])} for t in view["selective_themes"]]
    # converge-coding-contracts: a new-method run sends no Axial list at all.
    axial = ("\n\n## 這些主題引用的主軸主題（支持用）\n```yaml\n" + _yaml_dump(view["axial_support"]) + "```"
             if view.get("axial_support") else "")
    return (
        f"## 案例\ncase_id: {case_id}\n\n## 研究目的\n{view.get('research_purpose') or ''}\n\n"
        "## 選擇編碼主題（主要依據）\n```yaml\n" + _yaml_dump(selective) + "```" + axial
    )


# converge-coding-contracts (2026-10-03): the sentence supplies 「所以我可以」,
# so a stored outcome never begins with an ability word.
OUTCOME_ABILITY_WORDS = ("可以", "能夠", "會想", "能", "會")
_OUTCOME_ABILITY_WORD = re.compile(r"^(?:" + "|".join(OUTCOME_ABILITY_WORDS) + ")")


def _jtbd_candidate_generation_errors(data: object, view: dict) -> list[str]:
    """New candidates: the draft schema, 1-8 candidates or an honest
    insufficient-evidence reason, refs that exist, a motivation, and no
    solution language. No narrative (the draft schema has no such field)."""
    errors = V.structural_errors(data, V.load_schema("narrative-job-candidates-draft"))
    if not isinstance(data, dict):
        return errors
    candidates = [c for c in data.get("candidates") or [] if isinstance(c, dict)]
    if not candidates and not data.get("insufficient_evidence"):
        errors.append("[narrative_job_candidates] 沒有候選時要寫 insufficient_evidence 說明證據撐不起的原因")
    if candidates and data.get("insufficient_evidence"):
        errors.append("[narrative_job_candidates] 有候選就不要同時寫 insufficient_evidence")
    for index, candidate in enumerate(candidates, start=1):
        for ref in candidate.get("selective_refs") or []:
            if ref not in view["selective_ids"]:
                errors.append(f"[narrative_job_candidates] 第 {index} 則引用不存在的選擇主題：{ref}")
        for ref in candidate.get("grounded_in") or []:
            if ref not in view["axial_ids"]:
                errors.append(f"[narrative_job_candidates] 第 {index} 則引用不存在的主軸主題：{ref}")
        for field in ("situation", "progress", "motivation", "outcome"):
            value = candidate.get(field)
            if isinstance(value, str) and _SOLUTION_LANGUAGE.search(value):
                errors.append(f"[narrative_job_candidates] 第 {index} 則的 {field} 內含解法或指名技術／產品，不該出現在這裡：{value}")
        if view.get("new_method") and candidate.get("grounded_in"):
            errors.append(f"[narrative_job_candidates] 第 {index} 則不要寫 grounded_in：出處由 selective_refs 往下回溯")
        outcome = str(candidate.get("outcome") or "").strip()
        if _OUTCOME_ABILITY_WORD.match(outcome):
            errors.append(f"[narrative_job_candidates] 第 {index} 則的 outcome 不要以「可以／能夠／能／會想／會」開頭"
                          f"（句子已經有「所以我可以」）：{outcome}")
    return errors


def narrative_job_candidates(view: dict, opts: dict) -> tuple:
    """multi-jtbd-single-job-source (2026-10-03): the customer jobs Selective
    Coding and the evidence support -- one to eight, no target count, no
    hidden narrative. `view` is `selective_themes.candidates_input(run_dir)`.
    No candidate is ranked or recommended; choosing is the researcher's."""
    case_id = opts.get("case_id") or "unknown"
    user = _jtbd_candidates_user_message(view, case_id)
    data, usage = _generate(
        "narrative_job_candidates", _system("narrative_job_candidates"), user,
        lambda d: _jtbd_candidate_generation_errors(d, view), opts,
    )
    data["case_id"] = case_id
    data["schema_version"] = 2
    data["framed_by"] = dict(view["framed_by"])
    return data, usage


def _persona_dimension_candidates_errors(data: object, category_ids: set[str]) -> list[str]:
    """propose-persona-dimension-candidates：跟 `_core_category_candidates_errors`
    一樣的純函式，檢查 schema 表達不了的事：每個候選軸的 grounded_in 類別 id
    是不是真的在主軸編碼的類別清單裡。
    """
    errors = V.structural_errors(data, V.load_schema("persona-dimension-candidates"))
    if not isinstance(data, dict):
        return errors
    for candidate in data.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        for axis in candidate.get("axes") or []:
            if not isinstance(axis, dict):
                continue
            for cat_id in axis.get("grounded_in") or []:
                if cat_id not in category_ids:
                    errors.append(
                        f"[persona_dimension_candidates] 候選軸「{axis.get('label')}」"
                        f"引用不存在的類別：{cat_id}"
                    )
    return errors


def _persona_dimensions_selection_errors(data: object, category_ids: set[str]) -> list[str]:
    """propose-persona-dimension-candidates：跟 `_selective_coding_errors` 一樣
    的純函式，檢查 schema 表達不了的事：研究者選定的每條軸的 grounded_in
    類別 id 是不是真的在主軸編碼的類別清單裡——不管這組軸是照抄某個候選、
    還是完全自己寫的，都走同一個檢查（選擇檔本身不記錄來源，見
    persona-dimensions-selection.schema.json 的說明）。
    """
    errors = V.structural_errors(data, V.load_schema("persona-dimensions-selection"))
    if not isinstance(data, dict):
        return errors
    for axis in data.get("axes") or []:
        if not isinstance(axis, dict):
            continue
        for cat_id in axis.get("grounded_in") or []:
            if cat_id not in category_ids:
                errors.append(
                    f"[persona_dimensions_selection] 軸「{axis.get('label')}」"
                    f"引用不存在的類別：{cat_id}"
                )
    return errors


def _propose_persona_dimension_candidates_user_message(categories_doc: dict, evidence: list, case_id: str) -> str:
    findings = V.axial_coding_warnings(categories_doc, evidence)
    blocks = []
    for category in categories_doc.get("categories") or []:
        if not isinstance(category, dict):
            continue
        label = category.get("label")
        matches = [str(f) for f in findings if f"「{label}」" in f]
        blocks.append({
            "id": category.get("id"), "label": label,
            "discriminating_basis": category.get("discriminating_basis"),
            "evidence_refs": category.get("evidence_refs") or [],
            "judgment_findings": matches,
        })
    evidence_blocks = [
        _evidence_unit(e, "id", "role", "text")
        for e in evidence if isinstance(e, dict)
    ]
    return (
        f"## 案例\ncase_id: {case_id}\n\n"
        "## 主軸編碼類別與既有判準發現\n```yaml\n" + _yaml_dump(blocks) + "```\n\n"
        "## 全部證據單元\n```yaml\n" + _yaml_dump(evidence_blocks) + "```"
    )


def _propose_persona_dimension_candidates_from_properties_user_message(properties: list, case_id: str) -> str:
    blocks = [
        {
            "id": p.get("id"), "category_id": p.get("category_id"), "name": p.get("name"),
            "pole_low": p.get("pole_low"), "pole_high": p.get("pole_high"),
            "discriminating_basis": p.get("discriminating_basis"),
        }
        for p in properties
    ]
    return (
        f"## 案例\ncase_id: {case_id}\n\n"
        "## 屬性與面向候選池\n```yaml\n" + _yaml_dump(blocks) + "```"
    )


def _persona_dimension_candidates_from_properties_errors(data: object, property_ids: set[str]) -> list[str]:
    """derive-axis-candidates-from-properties: when a category_properties.yaml
    exists, the model's own output is a lightweight selection -- which
    properties belong to each of the 3 framings -- not the full axis shape.
    The full axis content (label/poles/design consequences/discriminating_
    basis) is copied verbatim from the property pool afterwards by
    _assemble_candidate_axes_from_properties(), never retyped by the model,
    so this check only needs to confirm the selection references real
    properties -- it validates the model's envelope, not the persisted
    persona-dimension-candidates schema (that's checked once, after
    assembly, in propose_persona_dimension_candidates() itself)."""
    if not isinstance(data, dict):
        return ["[persona_dimension_candidates] 頂層必須是物件"]
    errors: list[str] = []
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 3:
        errors.append("[persona_dimension_candidates] candidates 必須恰好 3 組")
        return errors
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            errors.append(f"[persona_dimension_candidates] candidates/{index} 必須是物件")
            continue
        label = candidate.get("framing_label")
        if not isinstance(label, str) or not label.strip():
            errors.append(f"[persona_dimension_candidates] candidates/{index}/framing_label 必須是非空文字")
        rationale = candidate.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            errors.append(f"[persona_dimension_candidates] candidates/{index}/rationale 必須是非空文字")
        ids = candidate.get("property_ids")
        if not isinstance(ids, list) or not ids:
            errors.append(f"[persona_dimension_candidates] candidates/{index}/property_ids 必須是非空陣列")
            continue
        for pid in ids:
            if pid not in property_ids:
                errors.append(f"[persona_dimension_candidates] candidates/{index} 引用不存在的屬性：{pid}")
    return errors


def _assemble_candidate_axes_from_properties(candidates_raw: list, properties_by_id: dict) -> list[dict]:
    """The model only decides which properties go into each framing; the
    axis content itself (label/poles/design consequences/discriminating_
    basis/grounded_in) is copied character for character from the property
    pool here -- same position persona_matrix takes toward a researcher-
    selected dimension: what's already decided upstream, downstream doesn't
    rephrase."""
    candidates = []
    for candidate in candidates_raw:
        if not isinstance(candidate, dict):
            continue
        axes = []
        for pid in candidate.get("property_ids") or []:
            prop = properties_by_id.get(pid)
            if not isinstance(prop, dict):
                continue
            axes.append({
                "label": prop.get("name"),
                "pole_low": prop.get("pole_low"),
                "pole_high": prop.get("pole_high"),
                "pole_low_design": prop.get("pole_low_design"),
                "pole_high_design": prop.get("pole_high_design"),
                "discriminating_basis": prop.get("discriminating_basis"),
                "grounded_in": [prop.get("category_id")],
            })
        candidates.append({
            "framing_label": candidate.get("framing_label"),
            "rationale": candidate.get("rationale"),
            "axes": axes,
        })
    return candidates


def propose_persona_dimension_candidates(categories_doc: dict, evidence: list, opts: dict,
                                          properties_doc: dict | None = None) -> tuple:
    """propose-persona-dimension-candidates：不推薦任何一組為預設答案——挑
    哪一組是研究者的判斷,這支函式只負責讀完、排序、講重點（CLAUDE.md「助理
    的界線」）。

    derive-axis-candidates-from-properties：這支的工作從「造」變成「挑」。
    有 `properties_doc`（該 run 已跑過屬性與面向）時，候選池是屬性清單，
    模型只決定要選哪些屬性、怎麼分組——選中屬性的 label／兩極／設計後果／
    區辨依據由 `_assemble_candidate_axes_from_properties()` 逐字從屬性池
    貼過去，不讓模型重寫（跟 `persona_matrix` 對研究者已選定維度的既有立場
    一致）。沒有 `properties_doc`（該 run 還沒跑過那一關）時，退回舊路徑
    ——直接從類別與證據造軸，跟這個 change之前完全一樣——並在輸出的
    `dimension_source` 註明走的是哪一條路，不靜默降級。
    """
    case_id = opts.get("case_id") or "unknown"
    properties = [
        p for p in (properties_doc or {}).get("properties") or []
        if isinstance(p, dict) and p.get("id")
    ]

    if properties:
        properties_by_id = {p["id"]: p for p in properties}
        user = _propose_persona_dimension_candidates_from_properties_user_message(properties, case_id)
        data, usage = _generate(
            "persona_dimension_candidates", _system("persona_dimension_candidates"), user,
            lambda d: _persona_dimension_candidates_from_properties_errors(d, set(properties_by_id)), opts,
        )
        candidates = _assemble_candidate_axes_from_properties(data.get("candidates") or [], properties_by_id)
        result = {
            "schema_version": 1, "case_id": case_id, "candidates": candidates,
            "dimension_source": "category_properties",
        }
        assembly_errors = V.structural_errors(result, V.load_schema("persona-dimension-candidates"))
        if assembly_errors:
            raise llm.LLMError(
                "persona_dimension_candidates 用屬性組裝後未通過驗證，屬性池本身可能有問題：\n"
                + "\n".join(assembly_errors)
            )
        return result, usage

    category_ids = {c["id"] for c in (categories_doc.get("categories") or []) if isinstance(c, dict) and c.get("id")}
    user = _propose_persona_dimension_candidates_user_message(categories_doc, evidence, case_id)
    data, usage = _generate(
        "persona_dimension_candidates", _system("persona_dimension_candidates"), user,
        lambda d: _persona_dimension_candidates_errors(d, category_ids), opts,
    )
    data["case_id"] = case_id
    data["dimension_source"] = "categories_fallback"
    return data, usage


def _primary_job_candidates_errors(data: object, category_ids: set[str]) -> list[str]:
    """propose-primary-job-candidates：跟 `_persona_dimension_candidates_errors`
    一樣的純函式，檢查 schema 表達不了的事：每個候選的 grounded_in 類別 id
    是不是真的在主軸編碼的類別清單裡。
    """
    errors = V.structural_errors(data, V.load_schema("primary-job-candidates"))
    if not isinstance(data, dict):
        return errors
    for candidate in data.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        for cat_id in candidate.get("grounded_in") or []:
            if cat_id not in category_ids:
                errors.append(f"[primary_job_candidates] candidate 引用不存在的類別：{cat_id}")
    return errors


def _primary_job_selection_errors(data: object, category_ids: set[str]) -> list[str]:
    """propose-primary-job-candidates：跟 `_persona_dimensions_selection_errors`
    一樣的純函式，檢查研究者選定的主要 Job 的 grounded_in 類別 id 是不是真的
    在主軸編碼的類別清單裡——不管這則 Job 是照抄某個候選、還是完全自己寫的，
    都走同一個檢查。
    """
    errors = V.structural_errors(data, V.load_schema("primary-job-selection"))
    if not isinstance(data, dict):
        return errors
    for cat_id in data.get("grounded_in") or []:
        if cat_id not in category_ids:
            errors.append(f"[primary_job_selection] 引用不存在的類別：{cat_id}")
    return errors


def _propose_primary_job_candidates_user_message(categories_doc: dict, evidence: list, case_id: str) -> str:
    categories = [
        {"id": c.get("id"), "label": c.get("label"), "discriminating_basis": c.get("discriminating_basis")}
        for c in (categories_doc.get("categories") or []) if isinstance(c, dict)
    ]
    units = [
        _evidence_unit(e, "id", "role", "source", "text")
        for e in evidence if isinstance(e, dict)
    ]
    return (
        f"## 案例\ncase_id: {case_id}\n\n"
        "## 主軸編碼類別\n```yaml\n" + _yaml_dump(categories) + "```\n\n"
        "## 證據單元\n```yaml\n" + _yaml_dump(units) + "```"
    )


def propose_primary_job_candidates(categories_doc: dict, evidence: list, opts: dict) -> tuple:
    """propose-primary-job-candidates：讀完全部主軸編碼類別跟證據，提出剛好
    3 個候選「主要 Job」框架，不推薦任何一個為預設答案——挑哪一個是研究者的
    判斷，這支函式只負責讀完、排序、講重點（CLAUDE.md「助理的界線」）。跟
    `propose_persona_dimension_candidates` 同一個形狀，但不需要候選池篩選
    方法論那套內部推理——一則 Job 就是一句情境／進展／結果陳述，不需要受訪者
    落點資料才能判斷好壞。
    """
    case_id = opts.get("case_id") or "unknown"
    category_ids = {c["id"] for c in (categories_doc.get("categories") or []) if isinstance(c, dict) and c.get("id")}
    user = _propose_primary_job_candidates_user_message(categories_doc, evidence, case_id)
    data, usage = _generate(
        "primary_job_candidates", _system("primary_job_candidates"), user,
        lambda d: _primary_job_candidates_errors(d, category_ids), opts,
    )
    data["case_id"] = case_id
    return data, usage


def _primary_hmw_candidates_errors(data: object, hmw_ids: set[str], *, require_all: bool = False) -> list[str]:
    """propose-primary-hmw-candidates：跟其他候選機制的 `_errors` 函式一樣的
    純函式，檢查 schema 表達不了的事：每個候選的 `hmw_id` 是不是真的在供應
    的 hmw.yaml 的 entries 清單裡。
    """
    errors = V.structural_errors(data, V.load_schema("primary-hmw-candidates"))
    if not isinstance(data, dict):
        return errors
    for candidate in data.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        hmw_id = candidate.get("hmw_id")
        if isinstance(hmw_id, str) and hmw_id not in hmw_ids:
            errors.append(f"[primary_hmw_candidates] candidate 引用不存在的 HMW：{hmw_id}")
    # hmw-exactly-three-framing-roles: at generation every framing is presented
    # once; nothing is ranked away.
    if require_all:
        chosen = [c.get("hmw_id") for c in data.get("candidates") or [] if isinstance(c, dict)]
        if sorted(chosen) != sorted(hmw_ids):
            errors.append(f"[primary_hmw_candidates] 候選要剛好涵蓋全部 {len(hmw_ids)} 則 HMW，各一次，現在是：{chosen}")
        # hmw-candidate-rationale-boundary: the rationale is researcher-facing,
        # so the same raw-id rule as the HMW text applies.
        for candidate in data.get("candidates") or []:
            found = _RAW_INTERNAL_ID.search((candidate or {}).get("rationale") or "") if isinstance(candidate, dict) else None
            if found:
                errors.append(f"[primary_hmw_candidates] {candidate.get('hmw_id')} 的理由露出內部編號 {found.group(0)}，要用研究者看得懂的話寫")
    return errors


def _primary_hmw_selection_errors(data: object, hmw_ids: set[str], category_ids: set[str]) -> list[str]:
    """propose-primary-hmw-candidates：研究者選定的主要 HMW 的參照完整性
    檢查——schema 是 oneOf 兩個分支（照抄既有 hmw_id、或完全自填一則新
    的），`V.structural_errors` 已經檢查了「剛好符合其中一支」；這裡只補
    schema 表達不了的事：如果是參照分支，`hmw_id` 是不是真的在 hmw.yaml
    裡；如果是自填分支，`grounded_in` 是不是真的在主軸編碼的類別清單裡。
    """
    errors = V.structural_errors(data, V.load_schema("primary-hmw-selection"))
    if not isinstance(data, dict):
        return errors
    if "hmw_id" in data:
        hmw_id = data.get("hmw_id")
        if isinstance(hmw_id, str) and hmw_id not in hmw_ids:
            errors.append(f"[primary_hmw_selection] 引用不存在的 HMW：{hmw_id}")
    elif "grounded_in" in data:
        cat_id = data.get("grounded_in")
        if isinstance(cat_id, str) and cat_id not in category_ids:
            errors.append(f"[primary_hmw_selection] 引用不存在的類別：{cat_id}")
    return errors


def propose_primary_hmw_candidates(hmw_doc: dict, categories_doc: dict, opts: dict,
                                   narrative_job_selection: dict | None = None) -> tuple:
    """hmw-framings-from-problem-structures（2026-10-01）：HMW 候選是 hmw.yaml
    框法的程式投影——依 entries 順序列出 hmw_id，不呼叫模型、不寫理由。

    這一步原本是第二次 LLM 呼叫：只把前一步的結果照列、再寫一段沒有任何
    畫面或下游在讀的理由，沒有自己的分析對象（AI 呼叫要對應一個明確的
    分析對象）。候選檔照舊產出，HMW 站、決策索引與過期判斷都讀它。
    hmw.yaml 沒有 entries 時回傳 `(None, None)`，跟以前一樣不產候選。

    `categories_doc`、`narrative_job_selection` 保留在簽名裡給既有呼叫端，
    框定指紋由呼叫端照舊寫進產出。
    """
    entries = [e for e in (hmw_doc.get("entries") or []) if isinstance(e, dict) and e.get("id")]
    if not entries:
        return None, None
    data = {"schema_version": 1, "case_id": opts.get("case_id") or "unknown",
            "candidates": [{"hmw_id": e["id"]} for e in entries]}
    return data, {"tokens": 0, "seconds": 0, "executor": "deterministic", "stage": "primary_hmw_candidates"}


_DEFAULT_PAIR_SUPERLATIVE = re.compile(r"最佳|最好|最適合|最理想")


def _segmentation_axis_candidates_errors(data: object, axis_ids: set[str], *,
                                         selectable_axis_ids: set[str] | None = None,
                                         require_default: bool = False) -> list[str]:
    """add-segmentation-axis-designation：跟其他候選機制的 `_errors` 函式
    一樣的純函式——每一條軸都要有一份簡報，不遺漏也不重複；schema 的
    `minItems: 1` 表達不了「剛好等於這次 run 的軸數」這種依賴外部輸入的數量
    要求，所以在這裡補。
    """
    errors = V.structural_errors(data, V.load_schema("segmentation-axis-candidates"))
    if not isinstance(data, dict):
        return errors
    briefed_ids: list[str] = []
    for brief in data.get("briefs") or []:
        if not isinstance(brief, dict):
            continue
        axis_id = brief.get("axis_id")
        if isinstance(axis_id, str):
            briefed_ids.append(axis_id)
            if axis_id not in axis_ids:
                errors.append(f"[segmentation_axis_candidates] brief 引用不存在的軸：{axis_id}")
    missing = axis_ids - set(briefed_ids)
    if missing:
        errors.append(f"[segmentation_axis_candidates] 沒有為以下軸寫簡報：{'、'.join(sorted(missing))}")
    duplicated = {aid for aid in briefed_ids if briefed_ids.count(aid) > 1}
    if duplicated:
        errors.append(f"[segmentation_axis_candidates] 以下軸有超過一份簡報：{'、'.join(sorted(duplicated))}")
    # persona-matrix-hmw-scope-default (2026-10-01 ruling): one default axis
    # pair -- a default analytical projection, not the best segmentation.
    selectable = axis_ids if selectable_axis_ids is None else selectable_axis_ids
    pair = data.get("default_pair")
    no_pair_reason = data.get("no_default_pair_reason")
    # persona-segmentation-reasoning-contract: no default pair is a valid
    # answer when no two axes support a meaningful matrix -- but say why.
    if isinstance(pair, dict) and no_pair_reason:
        errors.append("[segmentation_axis_candidates] 提了 default_pair 就不要同時寫 no_default_pair_reason")
    basis = str(pair.get("basis") or "") if isinstance(pair, dict) else ""
    if require_default and (internal_id_patterns.contains_internal_id(basis)
                            or internal_id_patterns.contains_internal_id(str(no_pair_reason or ""))):
        errors.append("[segmentation_axis_candidates] 預設軸的依據或不提預設的原因寫了內部編號；用軸的名稱與受訪者的說法寫")
    if isinstance(pair, dict):
        for axis_id in pair.get("axis_ids") or []:
            if axis_id not in axis_ids:
                errors.append(f"[segmentation_axis_candidates] default_pair 引用不存在的軸：{axis_id}")
            elif axis_id not in selectable:
                errors.append(f"[segmentation_axis_candidates] default_pair 的 {axis_id} 缺一端的描述，不能當預設")
        if _DEFAULT_PAIR_SUPERLATIVE.search(str(pair.get("basis") or "")):
            errors.append("[segmentation_axis_candidates] default_pair 的依據不要寫最佳／最好，這只是預設的切法")
    elif require_default and len(selectable) >= 2 and not no_pair_reason:
        errors.append("[segmentation_axis_candidates] 有兩條以上可選的軸：提出 default_pair，或在 no_default_pair_reason 講為什麼沒有一對撐得起分群")
    return errors


def _segmentation_axis_selection_errors(data: object, axis_ids: set[str]) -> list[str]:
    """add-segmentation-axis-designation：研究者指定的目標族群軸的參照完整性
    檢查——schema 已經檢查數量恰好 2、不重複、business_rationale 非空字串；
    這裡只補 schema 表達不了的事：每個 axis_id 是不是真的在 persona_matrix.yaml
    裡。這裡沒有「自填新內容」的分支——軸已經由 Persona Matrix 階段固定下來，
    這裡只指定哪兩條當目標族群軸。
    """
    errors = V.structural_errors(data, V.load_schema("segmentation-axis-selection"))
    if not isinstance(data, dict):
        return errors
    for axis_id in data.get("axis_ids") or []:
        if isinstance(axis_id, str) and axis_id not in axis_ids:
            errors.append(f"[segmentation_axis_selection] 引用不存在的軸：{axis_id}")
    return errors


def _propose_segmentation_axis_candidates_user_message(persona_matrix_doc: dict, case_id: str,
                                                       hmw_scope: dict | None = None) -> str:
    axes = [
        {"id": a.get("id"), "label": a.get("label"), "representation": a.get("representation") or "continuum",
         "pole_low": a.get("pole_low"), "pole_high": a.get("pole_high"),
         "discriminating_basis": a.get("discriminating_basis")}
        for a in (persona_matrix_doc.get("axes") or []) if isinstance(a, dict)
    ]
    real_placements_by_axis: dict[str, list[dict]] = {}
    for subject in persona_matrix_doc.get("subjects") or []:
        if not isinstance(subject, dict):
            continue
        for placement in subject.get("placements") or []:
            if not isinstance(placement, dict) or placement.get("position") == "no_data":
                continue
            real_placements_by_axis.setdefault(placement.get("axis_id"), []).append(
                {"subject_id": subject.get("id"), "position": placement.get("position")})
    return (
        f"## 案例\ncase_id: {case_id}\n\n"
        + _hmw_scope_section(hmw_scope)
        + "## 軸的定義\n```yaml\n" + _yaml_dump(axes) + "```\n\n"
        "## 每一條軸的真實落點（不含 no_data）\n```yaml\n"
        + _yaml_dump(real_placements_by_axis) + "```\n\n"
        "## 每一對軸的共同涵蓋（由程式算出：兩條軸都落在其中一端、能放進四格的受訪者）\n```yaml\n"
        + _yaml_dump(joint_participant_support(persona_matrix_doc)) + "```"
    )


def joint_participant_support(persona_matrix_doc: dict) -> list[dict]:
    """persona-segmentation-reasoning-contract: for every pair of axes with
    both poles described, the subjects who sit at one end of both -- the only
    ones the pair's 2x2 can place. Computed, not judged; no threshold."""
    axes = [a.get("id") for a in (persona_matrix_doc.get("axes") or [])
            if isinstance(a, dict) and a.get("id") and a.get("pole_low") and a.get("pole_high")]
    ends: dict[str, set[str]] = {aid: set() for aid in axes}
    for subject in persona_matrix_doc.get("subjects") or []:
        if not isinstance(subject, dict):
            continue
        for placement in subject.get("placements") or []:
            if isinstance(placement, dict) and placement.get("axis_id") in ends \
                    and placement.get("position") in ("near_low", "near_high"):
                ends[placement["axis_id"]].add(subject.get("id"))
    pairs = []
    for i, first in enumerate(axes):
        for second in axes[i + 1:]:
            shared = sorted(ends[first] & ends[second], key=lambda sid: (len(str(sid)), str(sid)))
            pairs.append({"axis_ids": [first, second], "count": len(shared), "subject_ids": shared})
    return pairs


def propose_segmentation_axis_candidates(persona_matrix_doc: dict, opts: dict,
                                         hmw_scope: dict | None = None) -> tuple:
    """add-segmentation-axis-designation：讀完 persona_matrix.yaml 已經產出的
    軸與真實落點，對每一條軸寫一份簡報（不篩選、不排序）。
    persona-matrix-hmw-scope-default（2026-10-01 裁定）另外提出一組預設兩軸
    `default_pair`——預設的分析切法，不是最佳分群；目標族群仍由研究者依業務
    邏輯與商業價值選（CLAUDE.md「助理的界線」）。跟 persona-dimension-candidates 不同：
    這裡不是從候選池篩出最多 3 個，是對 persona_matrix.yaml 裡**每一條軸**
    都寫一份簡報，軸的數量由上游（Persona Matrix 階段）決定，不是這個函式
    決定。`persona_matrix.yaml` 的 `axes` 陣列本身 schema 就要求至少 1 項，
    不需要空結果短路。
    """
    case_id = opts.get("case_id") or "unknown"
    axes = [a for a in (persona_matrix_doc.get("axes") or []) if isinstance(a, dict) and a.get("id")]
    axis_ids = {a["id"] for a in axes}
    selectable = {a["id"] for a in axes if a.get("pole_low") and a.get("pole_high")}

    def _candidate(partial):
        # 提示詞要求模型輸出 `briefs` 與 `default_pair`；schema_version／case_id
        # 由這裡補上再驗證，不逼模型自己覆誦樣板欄位。
        briefs = partial.get("briefs") if isinstance(partial, dict) else partial
        candidate = {"schema_version": 1, "case_id": case_id, "briefs": briefs}
        if isinstance(partial, dict) and partial.get("default_pair") is not None:
            candidate["default_pair"] = partial["default_pair"]
        if isinstance(partial, dict) and partial.get("no_default_pair_reason"):
            candidate["no_default_pair_reason"] = partial["no_default_pair_reason"]
        return candidate

    def _validate(partial):
        return _segmentation_axis_candidates_errors(
            _candidate(partial), axis_ids, selectable_axis_ids=selectable, require_default=True)

    user = _propose_segmentation_axis_candidates_user_message(persona_matrix_doc, case_id, hmw_scope)
    data, usage = _generate(
        "segmentation_axis_candidates", _prompt("segmentation_axis_candidates"), user,
        _validate, opts,
    )
    return _candidate(data), usage


TARGET_SEGMENTATION_NO_SUPPORT_PORTRAIT = (
    "這一格沒有任何受訪者的真實落點同時符合這兩條軸的這個組合——"
    "是招募沒招到，還是這種人不存在，需要研究者判斷。"
)


def _quadrant_suggested_ids(quadrant: dict) -> list:
    """quadrant-members-are-designer-assigned: reads either field name so a
    candidates document written before this change (`member_subject_ids`)
    and one written after (`suggested_subject_ids`) are read identically.
    New documents only ever carry the new name -- this is a read-side
    compatibility shim, not a write-side choice."""
    if "suggested_subject_ids" in quadrant:
        return list(quadrant.get("suggested_subject_ids") or [])
    return list(quadrant.get("member_subject_ids") or [])


def assign_target_segmentation_quadrants(persona_matrix_doc: dict, axis_ids: list[str]) -> tuple:
    """add-target-segmentation-designation：把 persona_matrix.yaml 的受訪者依
    `axis_ids` 指定的兩條軸的真實落點,機械分派到 4 格裡的其中一格,或歸類成
    「無法歸類」。這是純函式,不呼叫模型——哪個受訪者落在哪一格是可以算出來
    的事實,不是判斷題(design.md 決定一)。

    一個受訪者在 `axis_ids` 任一條軸上的落點是 `mid`、`no_data`,或完全沒有
    這條軸的落點,就不分派到任何一格,進 `unclassified_subject_ids`——不勉強
    湊進最近的格子。

    quadrant-members-are-designer-assigned：欄位名是 `suggested_subject_ids`
    ——這個分派只是系統依落點算出的建議，成員以研究者的指派（寫進
    target_segmentation_selection.yaml）為準，語意寫在名字上不只寫在註解裡。
    """
    axis_1, axis_2 = axis_ids[0], axis_ids[1]
    pole_pairs = [
        ("near_low", "near_low"), ("near_low", "near_high"),
        ("near_high", "near_low"), ("near_high", "near_high"),
    ]
    quadrants = [
        {"id": f"QUADRANT-{i}", "axis_1_pole": p1, "axis_2_pole": p2, "suggested_subject_ids": []}
        for i, (p1, p2) in enumerate(pole_pairs, start=1)
    ]
    by_pair = {(q["axis_1_pole"], q["axis_2_pole"]): q for q in quadrants}
    unclassified: list[str] = []
    for subject in persona_matrix_doc.get("subjects") or []:
        if not isinstance(subject, dict) or not subject.get("id"):
            continue
        placements = {
            p.get("axis_id"): p.get("position")
            for p in (subject.get("placements") or []) if isinstance(p, dict)
        }
        pos_1 = placements.get(axis_1)
        pos_2 = placements.get(axis_2)
        if pos_1 in ("near_low", "near_high") and pos_2 in ("near_low", "near_high"):
            by_pair[(pos_1, pos_2)]["suggested_subject_ids"].append(subject["id"])
        else:
            unclassified.append(subject["id"])
    return quadrants, unclassified


def _target_segmentation_portraits_errors(data: object, expected_quadrant_ids: set[str]) -> list[str]:
    """add-target-segmentation-designation：模型只回覆有成員的格子各一份
    畫像——這支函式檢查回覆的格子集合跟預期(有成員的那些)一模一樣,不多不少,
    每份畫像非空字串。這是模型回應的中繼格式,不是最終持久化的
    target-segmentation.schema.json 形狀(那份文件的 member_subject_ids 是
    程式碼直接算出來貼上去的,從來不讀模型回應的這個欄位)。
    """
    if not isinstance(data, dict):
        return ["[target_segmentation] 回應不是物件"]
    portraits = data.get("portraits")
    if not isinstance(portraits, list):
        return ["[target_segmentation] 缺少 portraits 陣列"]
    errors: list[str] = []
    seen: list[str] = []
    for entry in portraits:
        if not isinstance(entry, dict):
            errors.append("[target_segmentation] portraits 裡有非物件項目")
            continue
        quadrant_id = entry.get("quadrant_id")
        if not isinstance(quadrant_id, str) or quadrant_id not in expected_quadrant_ids:
            errors.append(f"[target_segmentation] portraits 引用不在預期範圍內的格子：{quadrant_id}")
            continue
        seen.append(quadrant_id)
        portrait = entry.get("portrait")
        if not isinstance(portrait, str) or not portrait.strip():
            errors.append(f"[target_segmentation] {quadrant_id} 的畫像是空字串")
        elif internal_id_patterns.contains_internal_id(portrait):
            # scrub-internal-ids-from-quadrant-portraits: the model's input
            # labels each member by an internal id (subject_id) for it to
            # cross-reference against evidence -- that id must never leak
            # into the descriptive prose it writes back.
            errors.append(f"[target_segmentation] {quadrant_id} 的畫像裡出現內部編號，不能把受訪者的內部代號寫進畫像文字。")
    missing = expected_quadrant_ids - set(seen)
    if missing:
        errors.append(f"[target_segmentation] 沒有為以下格子寫畫像：{'、'.join(sorted(missing))}")
    duplicated = {q for q in seen if seen.count(q) > 1}
    if duplicated:
        errors.append(f"[target_segmentation] 以下格子有超過一份畫像：{'、'.join(sorted(duplicated))}")
    return errors


def _target_segmentation_selection_errors(
    data: object, quadrant_ids: set[str],
    subject_ids: set[str] | None = None, unclassified_subject_ids: set[str] | None = None,
    non_empty_quadrant_ids: set[str] | None = None,
) -> list[str]:
    """add-target-segmentation-designation：研究者指定的主要/次要目標的參照
    完整性檢查——schema 已經檢查 secondary 兩欄位同進同出（選擇原因本身
    2026-09-15 起可以留白，不再檢查非空）；這裡只補 schema 表達不了的事：
    quadrant id 是不是真的在這次 run 的 target_segmentation.yaml 裡。

    quadrant-members-are-designer-assigned：`subject_ids`／
    `unclassified_subject_ids` 選填，只有呼叫端真的傳了才檢查
    `quadrant_members`／`unplaced_dispositions`——舊呼叫端（尚未傳這兩個
    參數）的行為完全不變。有傳的話額外檢查：格子 id 與受訪者 id 是否真的
    存在、`unplaced_dispositions` 是不是真的在指落點缺資料的那批（不是系統
    已經建議進某格的人）、同一位受訪者有沒有被指派進兩格、`assigned` 的
    處置是不是真的在對應格子的 `quadrant_members` 裡出現。

    generate-persona-for-target-quadrant：`non_empty_quadrant_ids` 選填——
    傳了才檢查「主要目標不能指到一個沒有人的格子」，省略時完全不做這項
    檢查（舊呼叫端行為不變）。一個格子只要滿足其一就算非空：候選檔算出來的
    落點本來就不是空的，或者這次 `data` 自己的 `quadrant_members` 明確給了
    這一格至少一位成員——後者讓研究者可以把原本落點空的格子，透過指派
    落點缺資料的受訪者，變成有資格當主要目標，不是被候選檔的計算結果鎖死。
    次要目標不受這條限制：鎖定一個還沒被觸及的族群仍然是合法的商業判斷，
    只是它不會再長出 persona。
    """
    errors = V.structural_errors(data, V.load_schema("target-segmentation-selection"))
    if not isinstance(data, dict):
        return errors
    primary = data.get("primary_quadrant_id")
    if isinstance(primary, str) and primary not in quadrant_ids:
        errors.append(f"[target_segmentation_selection] 引用不存在的格子：{primary}")
    elif isinstance(primary, str) and non_empty_quadrant_ids is not None and primary not in non_empty_quadrant_ids:
        primary_has_confirmed_members = any(
            isinstance(m, dict) and m.get("quadrant_id") == primary and (m.get("subject_ids") or [])
            for m in (data.get("quadrant_members") or [])
        )
        if not primary_has_confirmed_members:
            errors.append(
                f"[target_segmentation_selection] {primary} 沒有任何確認過的成員，"
                f"一個沒有人的格子不能被指定為主要目標"
            )
    secondary = data.get("secondary_quadrant_id")
    if isinstance(secondary, str) and secondary not in quadrant_ids:
        errors.append(f"[target_segmentation_selection] 引用不存在的格子：{secondary}")

    if subject_ids is None:
        return errors

    members_by_quadrant: dict[str, list[str]] = {}
    all_confirmed: list[str] = []
    for entry in data.get("quadrant_members") or []:
        if not isinstance(entry, dict):
            continue
        qid = entry.get("quadrant_id")
        if isinstance(qid, str) and qid not in quadrant_ids:
            errors.append(f"[target_segmentation_selection] quadrant_members 引用不存在的格子：{qid}")
        sids = [s for s in entry.get("subject_ids") or [] if isinstance(s, str)]
        for sid in sids:
            if sid not in subject_ids:
                errors.append(f"[target_segmentation_selection] quadrant_members 引用不存在的受訪者：{sid}")
        members_by_quadrant[qid] = sids
        all_confirmed.extend(sids)
    duplicated = {sid for sid in all_confirmed if all_confirmed.count(sid) > 1}
    if duplicated:
        errors.append(
            f"[target_segmentation_selection] 以下受訪者被指派到超過一格：{'、'.join(sorted(duplicated))}"
        )

    unclassified_subject_ids = unclassified_subject_ids or set()
    for entry in data.get("unplaced_dispositions") or []:
        if not isinstance(entry, dict):
            continue
        sid = entry.get("subject_id")
        if isinstance(sid, str) and sid not in unclassified_subject_ids:
            errors.append(
                f"[target_segmentation_selection] unplaced_dispositions 引用的受訪者不在這次 run "
                f"落點缺資料的名單裡：{sid}"
            )
        disposition = entry.get("disposition")
        assigned_qid = entry.get("assigned_quadrant_id")
        if disposition == "assigned":
            if isinstance(assigned_qid, str) and assigned_qid not in quadrant_ids:
                errors.append(f"[target_segmentation_selection] unplaced_dispositions 引用不存在的格子：{assigned_qid}")
            elif isinstance(sid, str) and sid not in members_by_quadrant.get(assigned_qid, []):
                errors.append(
                    f"[target_segmentation_selection] {sid} 標成指派到 {assigned_qid}，"
                    f"但沒有出現在該格 quadrant_members 裡"
                )
        elif isinstance(sid, str) and sid in all_confirmed:
            errors.append(
                f"[target_segmentation_selection] {sid} 的處置是「{disposition}」，"
                f"卻同時出現在 quadrant_members 裡——處置與成員名單互相矛盾"
            )
    return errors


def _target_segmentation_user_message(non_empty_quadrants: list[dict], subjects_by_id: dict,
                                       evidence_by_id: dict, case_id: str) -> str:
    blocks = []
    for quadrant in non_empty_quadrants:
        members = []
        for subject_id in _quadrant_suggested_ids(quadrant):
            subject = subjects_by_id.get(subject_id) or {}
            texts = [
                _evidence_text_entry(evidence_by_id[ref]) for ref in subject.get("evidence_refs") or []
                if isinstance(evidence_by_id.get(ref), dict) and evidence_by_id[ref].get("text")
            ]
            members.append({"subject_id": subject_id, "evidence_texts": texts})
        blocks.append({"quadrant_id": quadrant["id"], "members": members})
    return (
        f"## 案例\ncase_id: {case_id}\n\n"
        "## 有成員的格子與其成員證據(只會問這些格子)\n```yaml\n" + _yaml_dump(blocks) + "```"
    )


def target_segmentation(evidence: list, persona_matrix_doc: dict, selection_doc: dict, opts: dict) -> tuple:
    """add-target-segmentation-designation：把 segmentation_axis_selection.yaml
    選定的兩條軸拿去交叉 persona_matrix.yaml 的真實落點,產出 2×2 目標族群
    切分。格子分派(誰在哪一格)是機械算出來的(`assign_target_segmentation_
    quadrants`),模型只負責替**有建議成員**的格子寫畫像;沒有建議成員的格子
    由這支函式直接填 `TARGET_SEGMENTATION_NO_SUPPORT_PORTRAIT`,不呼叫模型
    (design.md 決定二)。四格全空時完全不呼叫模型,usage 回傳零值。

    quadrant-members-are-designer-assigned：這裡產出的成員永遠是**建議**，
    不是最終成員——研究者指派後的真正成員寫進 target_segmentation_selection
    .yaml，這支函式跟它的輸出完全不知道那份檔案的存在。
    """
    case_id = opts.get("case_id") or "unknown"
    axis_ids = list(selection_doc.get("axis_ids") or [])
    quadrants, unclassified = assign_target_segmentation_quadrants(persona_matrix_doc, axis_ids)
    non_empty = [q for q in quadrants if q["suggested_subject_ids"]]

    portraits_by_id: dict[str, str] = {}
    usage = {"tokens": 0, "seconds": 0}
    if non_empty:
        subjects_by_id = {
            s["id"]: s for s in (persona_matrix_doc.get("subjects") or [])
            if isinstance(s, dict) and s.get("id")
        }
        evidence_by_id = {e["id"]: e for e in evidence if isinstance(e, dict) and e.get("id")}
        user = _target_segmentation_user_message(non_empty, subjects_by_id, evidence_by_id, case_id)
        expected_ids = {q["id"] for q in non_empty}
        data, usage = _generate(
            "target_segmentation", _prompt("target_segmentation"), user,
            lambda d: _target_segmentation_portraits_errors(d, expected_ids), opts,
        )
        portraits_by_id = {p["quadrant_id"]: p["portrait"] for p in data.get("portraits") or []}

    for quadrant in quadrants:
        quadrant["portrait"] = (
            portraits_by_id.get(quadrant["id"], TARGET_SEGMENTATION_NO_SUPPORT_PORTRAIT)
            if quadrant["suggested_subject_ids"] else TARGET_SEGMENTATION_NO_SUPPORT_PORTRAIT
        )

    result = {
        "schema_version": 1,
        "case_id": case_id,
        "axis_ids": axis_ids,
        "quadrants": quadrants,
        "unclassified_subject_ids": unclassified,
    }
    return result, usage


def _assign_axis_ids(selected_axes: list) -> list[dict]:
    """connect-dimension-and-job-selections-to-downstream: a dimension
    selection's axes carry no `id` (`schema/persona-dimensions-selection
    .schema.json` assigns ids only once a matrix is actually produced, per
    its own field description). Identifiers are assigned here — `AXIS-1`
    first, `AXIS-2` second, in the order the axes appear in the selection
    file — rather than left for the model to invent, so the positional
    verbatim comparison in `_persona_matrix_errors` has a stable order to
    compare against regardless of what the model does with the ids
    themselves."""
    return [{"id": f"AXIS-{i}", **axis} for i, axis in enumerate(selected_axes, start=1)]


def _dimension_selection_mismatches(axes: list, selected_axes: list) -> list[str]:
    """connect-dimension-and-job-selections-to-downstream: when a dimension
    selection was supplied, the produced axes must reuse it verbatim — the
    researcher chose these axes, and a rewritten axis returns a matrix they
    did not ask for. Compares positionally against `selected_axes` (already
    ordered and carrying assigned ids from `_assign_axis_ids`), not by
    matching labels, since nothing guarantees labels stay unique after a
    rewrite and a reordered response would otherwise pass as identical."""
    if len(axes) != len(selected_axes):
        return [
            f"[persona_matrix] 研究者的維度選擇有 {len(selected_axes)} 條軸，"
            f"但產出了 {len(axes)} 條——有選擇時軸數必須一致，不能增減。"
        ]
    errors = []
    fields = ("label", "pole_low", "pole_high", "discriminating_basis", "grounded_in")
    for i, (produced, selected) in enumerate(zip(axes, selected_axes), start=1):
        if not isinstance(produced, dict):
            errors.append(f"[persona_matrix] 第 {i} 條軸的產出不是物件，無法比對選定的軸。")
            continue
        for field in fields:
            if produced.get(field) != selected.get(field):
                errors.append(
                    f"[persona_matrix] 第 {i} 條軸的「{field}」與研究者選定的不符"
                    f"——有選擇時軸要逐字沿用，不能改寫。"
                )
    return errors


def _core_category_block(core_category: dict | None) -> str:
    """core-category-as-ranking-signal-not-filter: shared by
    `_persona_matrix_user_message` and `_jtbd_user_message` -- the
    researcher's core-category selection surfaces as this one extra prose
    block naming the category, never as a narrower `categories`/evidence
    list. Both callers still send every category and every evidence unit
    they always did; this only adds emphasis, it removes nothing."""
    if not core_category:
        return ""
    label = core_category.get("label", core_category.get("id"))
    category_id = core_category.get("id")
    return (
        "## 研究者指定的核心類別\n"
        f"研究者已將「{label}」（{category_id}）指定為本案例的核心類別——分析時對它與其他類別的關聯"
        "特別留意，但仍要完整處理全部類別與全部證據，不能因為這個指定而略過其他類別。\n\n"
    )


def _hmw_scope_section(hmw_scope: dict | None) -> str:
    """persona-matrix-hmw-scope-default: the selected HMW as design-relevance
    scope for segmentation. Framing only: it never filters evidence or
    categories, and never decides which axes exist."""
    if not hmw_scope:
        return ""
    return ("## 研究者選定的 HMW（設計範圍，不是證據；只用來判斷差異對這個設計題目有沒有意義）\n```yaml\n"
            + _yaml_dump(hmw_scope) + "```\n\n")


def _persona_matrix_user_message(evidence: list, categories_doc: dict, case_id: str,
                                  selected_axes: list | None = None,
                                  core_category: dict | None = None,
                                  hmw_scope: dict | None = None,
                                  attribution: dict | None = None) -> str:
    # persona-segmentation-reasoning-contract (2026-10-01): the analysis
    # object is the comparison of participants, so evidence arrives grouped
    # by source (who said it) and each unit's text appears exactly once;
    # categories carry only evidence ids, as analytical material rather than
    # the structure axes are generated from.
    # speaker-attribution-for-persona: with a source attribution file, each
    # unit also says its confirmed speaker, or that it cannot be attributed.
    from . import source_attribution
    attribution_block, unit_extra = source_attribution.generation_block(evidence, attribution)
    sources: dict[str, list[dict]] = {}
    for e in evidence:
        if isinstance(e, dict):
            sources.setdefault(str(e.get("source") or ""), []).append(
                {**_evidence_unit(e, "id", "role", "text"), **unit_extra.get(e.get("id"), {})})
    groups = [{"source": source, "units": units} for source, units in sources.items()]
    categories = []
    for c in categories_doc.get("categories") or []:
        if not isinstance(c, dict):
            continue
        block = {"id": c.get("id"), "label": c.get("label"), "discriminating_basis": c.get("discriminating_basis")}
        # The axial relationships (paradigm model), where recorded.
        for field in ("conditions", "actions", "consequences"):
            if c.get(field):
                block[field] = c.get(field)
        if c.get("evidence_refs"):
            block["evidence_refs"] = c.get("evidence_refs")
        categories.append(block)
    selected_block = ""
    if selected_axes:
        selected_block = (
            "## 研究者已選定的維度（沿用這些軸，逐字重製，不要改寫或重新排序；"
            "你的工作是把受訪者放到這些軸上）\n```yaml\n" + _yaml_dump(selected_axes) + "```\n\n"
        )
    return (
        f"## 案例\ncase_id: {case_id}\n\n"
        + _hmw_scope_section(hmw_scope)
        + _core_category_block(core_category)
        + selected_block
        + attribution_block
        + "## 主軸編碼類別（分析材料，只列證據 id）\n```yaml\n" + _yaml_dump(categories) + "```\n\n"
        "## 證據單元（依 source 分組；同一個 source 可能混著兩個人，同一個人也可能分在好幾組，"
        "誰是誰由你判斷）\n```yaml\n" + _yaml_dump(groups) + "```"
    )


def _persona_matrix_errors(data: object, category_ids: set[str], evidence_ids: set[str],
                           selected_axes: list | None = None, *, generation: bool = False) -> list[str]:
    """add-persona-matrix：跟 `_axial_coding_errors` 一樣的純函式，檢查 schema
    表達不了的兩件事：每一條軸的 `grounded_in` 是不是真的在主軸編碼的類別清單
    裡、每個受訪者的 `evidence_refs` 是不是真的在證據清單裡。

    connect-dimension-and-job-selections-to-downstream：當 `selected_axes`
    有值（研究者已選定維度，見 `_assign_axis_ids`），額外用
    `_dimension_selection_mismatches` 檢查產出是不是逐字沿用了選擇——這條
    檢查跟既有兩條共用同一個錯誤清單，走同一個重試迴圈。

    require-complete-persona-matrix-placements：再加一條 schema 本身表達不了
    的完整性檢查——每個受訪者的 `placements` 是不是恰好覆蓋 `axes` 裡的每一條
    軸一次，不多不少。這是「拿 axes 陣列跟每個受訪者的 placements 比對」，
    schema 沒有跨欄位比對的能力，所以跟 grounded_in／evidence_refs 一樣走
    這支純函式。"""
    errors = V.structural_errors(data, V.load_schema("persona-matrix"))
    if not isinstance(data, dict):
        return errors
    axes = data.get("axes") or []
    for axis in axes:
        if not isinstance(axis, dict):
            continue
        for ref in axis.get("grounded_in") or []:
            if ref not in category_ids:
                errors.append(f"[persona_matrix] axis {axis.get('id')} 引用不存在的類別：{ref}")
        # persona-segmentation-reasoning-contract: researcher-facing prose
        # names no internal ids (a copied researcher-selected axis is exempt:
        # it is reproduced verbatim).
        # persona-axis-representation: a new axis says whether it is a degree
        # (continuum) or two contrasting orientations (contrast).
        if generation and selected_axes is None and axis.get("representation") not in AXIS_REPRESENTATIONS:
            errors.append(f"[persona_matrix] axis {axis.get('id')} 要寫 representation：continuum（程度）或 contrast（兩種取向）")
        if generation and selected_axes is None \
                and internal_id_patterns.contains_internal_id(str(axis.get("discriminating_basis") or "")):
            errors.append(f"[persona_matrix] axis {axis.get('id')} 的區辨依據寫了內部編號；用講得出是誰、說了什麼的方式寫")
    axis_ids = {axis.get("id") for axis in axes if isinstance(axis, dict) and axis.get("id")}
    for subject in data.get("subjects") or []:
        if not isinstance(subject, dict):
            continue
        for ref in subject.get("evidence_refs") or []:
            if ref not in evidence_ids:
                errors.append(f"[persona_matrix] subject {subject.get('id')} 引用不存在的證據：{ref}")
        placements = [p for p in (subject.get("placements") or []) if isinstance(p, dict)]
        if generation:
            # Stored matrices (export, delivery) predate these rules and stay valid.
            errors.extend(_persona_placement_errors(subject, placements))
        placement_axis_ids = [p.get("axis_id") for p in placements]
        missing = axis_ids - set(placement_axis_ids)
        if missing:
            errors.append(
                f"[persona_matrix] subject {subject.get('id')} 缺少下列軸的落點"
                f"（含「無資料」）：{'、'.join(sorted(missing))}"
            )
        seen: set = set()
        duplicated: set = set()
        for aid in placement_axis_ids:
            if aid in seen:
                duplicated.add(aid)
            seen.add(aid)
        if duplicated:
            errors.append(
                f"[persona_matrix] subject {subject.get('id')} 對同一條軸重複給了落點："
                f"{'、'.join(sorted(duplicated))}"
            )
    if selected_axes is not None:
        errors.extend(_dimension_selection_mismatches(axes, selected_axes))
    return errors


AXIS_REPRESENTATIONS = ("continuum", "contrast")


def _persona_placement_errors(subject: dict, placements: list[dict]) -> list[str]:
    """persona-segmentation-reasoning-contract (2026-10-01 ruling): absence of
    evidence is not evidence for a pole. A real position cites the subject's
    own evidence in `evidence_refs`; `no_data` cites none; the rationale is
    researcher-facing prose without internal ids. Whether the cited words
    actually support the pole stays with the researcher."""
    errors: list[str] = []
    sid = subject.get("id")
    own = set(subject.get("evidence_refs") or [])
    for placement in placements:
        aid, position = placement.get("axis_id"), placement.get("position")
        refs = placement.get("evidence_refs") or []
        if position == "no_data":
            if refs:
                errors.append(f"[persona_matrix] subject {sid} 在 {aid} 是無資料，不該引用證據")
        elif position in ("near_low", "mid", "near_high"):
            if not refs:
                errors.append(f"[persona_matrix] subject {sid} 在 {aid} 有真實落點，卻沒有在 evidence_refs 列出撐住它的證據；"
                              f"沒有證據就寫 no_data，沒提到不等於落在某一端")
            foreign = [r for r in refs if r not in own]
            if foreign:
                errors.append(f"[persona_matrix] subject {sid} 在 {aid} 引用了不屬於這個受訪者的證據：{'、'.join(foreign)}")
        if internal_id_patterns.contains_internal_id(str(placement.get("rationale") or "")):
            errors.append(f"[persona_matrix] subject {sid} 在 {aid} 的落點理由寫了內部編號；證據 id 只放 evidence_refs")
    return errors


def persona_matrix(evidence: list, categories_doc: dict, opts: dict, selected_axes: list | None = None,
                    core_category: dict | None = None, hmw_scope: dict | None = None,
                    attribution: dict | None = None) -> tuple:
    """add-persona-matrix：把主軸編碼（axial-coding）產出的類別讀成一組區辨
    行為變項軸，並把每個可辨識的受訪者放到這些軸上。

    獨立指令（`sbp-gen persona-matrix --run <dir>`）呼叫這個函式，不進主管線
    ——見 design.md「`persona-matrix` is a standalone CLI command consuming
    two existing files, not a pipeline stage」。跟 `axial_coding()` 一樣只吃
    evidence 跟 opts，case_id 從 `opts["case_id"]` 來。

    `selected_axes`（connect-dimension-and-job-selections-to-downstream）：
    研究者記錄的維度選擇（`persona_dimensions_selection.yaml` 的 `axes`
    列表，尚未指派 id），有值時這個階段不再自己造軸——選定的軸經
    `_assign_axis_ids` 指派 id 之後，原文放進 user message 要求模型逐字沿用，
    產出是否真的沿用由 `_persona_matrix_errors` 驗證。省略或傳 `None` 時，
    行為與這個參數存在之前完全相同：階段自己造軸，不做這項比對。

    `core_category`（core-category-as-ranking-signal-not-filter）：研究者
    指定的核心類別記錄（`{"id":..., "label":..., ...}`），只當排序/加權
    訊號放進 user message 裡的一段提示，`categories_doc`／`evidence` 完整
    不縮減——呼叫端（`__main__.py`）不再自己篩過類別，這裡收到的一律是
    全部類別、全部證據。省略或傳 `None` 時完全沒有這段提示，行為不變。

    persona-axes-from-full-evidence：這個階段不接收研究者選定的敘事／JTBD。
    軸只從主軸編碼類別與完整證據推導，不受任何研究問題的相關性篩選。

    `hmw_scope`（persona-matrix-hmw-scope-default，2026-10-01 裁定）：研究者
    選定的 HMW 只當設計相關性的判斷背景放進輸入；證據與類別照舊完整，跟
    HMW 關係弱的軸不刪。
    """
    case_id = opts.get("case_id") or "unknown"
    category_ids = {c["id"] for c in (categories_doc.get("categories") or []) if isinstance(c, dict) and c.get("id")}
    evidence_ids = {e["id"] for e in evidence if isinstance(e, dict) and e.get("id")}
    axes_with_ids = _assign_axis_ids(selected_axes) if selected_axes else None
    user = _persona_matrix_user_message(evidence, categories_doc, case_id, axes_with_ids, core_category,
                                        hmw_scope=hmw_scope, attribution=attribution)
    # speaker-attribution-for-persona: with a source attribution file, a
    # respondent is one confirmed person backed by that person's evidence.
    from . import source_attribution
    data, usage = _generate(
        "persona_matrix", _system("persona_matrix"), user,
        lambda d: _persona_matrix_errors(d, category_ids, evidence_ids, axes_with_ids, generation=True)
        + source_attribution.persona_attribution_errors(d, evidence, attribution), opts,
    )
    data["case_id"] = case_id
    return data, usage


def _persona_content_axis_block(axis: dict, pole: object, categories_by_id: dict) -> dict:
    """generate-persona-for-target-quadrant：一條選定軸完整的討論結構——
    label、兩端、判別依據、這一格代表的那一端，以及這條軸的 grounded_in
    類別（id 連同從 axial_coding.yaml 解出來的 label）。不帶這條軸以外的
    任何東西。"""
    grounded_in = []
    for cid in axis.get("grounded_in") or []:
        category = categories_by_id.get(cid) or {}
        grounded_in.append({"id": cid, "label": category.get("label", "")})
    return {
        "id": axis.get("id"),
        "label": axis.get("label"),
        "pole_low": axis.get("pole_low"),
        "pole_high": axis.get("pole_high"),
        "discriminating_basis": axis.get("discriminating_basis"),
        "grounded_in": grounded_in,
        "representation": axis.get("representation") or "continuum",
        "this_quadrant_pole": pole,
        # persona-pole-text-for-generation (2026-10-01): the enum stays as data;
        # the words the model writes from are the pole's own text.
        "this_quadrant_pole_text": _pole_label(axis, pole),
    }


def _pole_label(axis: dict, position: object) -> str:
    """Researcher-readable wording of a placement on `axis` for model-facing
    context; the enum value itself is never meant to be written out."""
    contrast = axis.get("representation") == "contrast"
    if position == "near_low":
        return str(axis.get("pole_low") or "")
    if position == "near_high":
        return str(axis.get("pole_high") or "")
    if position == "mid":
        return "兩種都出現" if contrast else "兩端之間"
    return "資料不足"


def _persona_content_user_message(evidence: list, quadrant: dict, axes: list, subjects_by_id: dict,
                                   categories_by_id: dict, case_id: str) -> str:
    """generate-persona-for-target-quadrant：只餵一格——研究者指定的主要
    目標象限——連同它代表的、選定的兩條軸的完整討論結構，以及每一位確認過
    的成員在這兩條軸上的落點與落點理由。不帶其他六條軸的任何落點，不帶
    選定的敘事、Job 或 HMW：軸的選擇已經把八條軸縮到兩條，這裡不把其他六條
    加回來；上位的框架選擇一旦餵進來會變成每個欄位共同的模子，模型會複述
    框架而不是讀成員。"""
    evidence_by_id = {e["id"]: e for e in evidence if isinstance(e, dict) and e.get("id")}
    # persona-evidence-grounded-embodiment: which coding categories each member's
    # own evidence sits in -- context for reading the member, not a citation source.
    categories_by_ref: dict[str, list[str]] = {}
    for category in categories_by_id.values():
        if isinstance(category, dict) and category.get("label"):
            for ref in category.get("evidence_refs") or []:
                categories_by_ref.setdefault(ref, []).append(category["label"])
    axis_1, axis_2 = axes[0], axes[1]
    axis_blocks = [
        _persona_content_axis_block(axis_1, quadrant.get("axis_1_pole"), categories_by_id),
        _persona_content_axis_block(axis_2, quadrant.get("axis_2_pole"), categories_by_id),
    ]
    members = []
    for subject_id in quadrant.get("subject_ids") or []:
        subject = subjects_by_id.get(subject_id) or {}
        # A short answer carries its interviewer question beside it (labelled
        # context, not the member's words). persona-content-cites-member-ids
        # (2026-10-01): every item carries its evidence id -- field provenance
        # and the representative quote must cite member ids, and without them
        # the model guessed sequential ids that belonged to other people.
        texts = [
            _evidence_unit(evidence_by_id[ref], "id", "text")
            for ref in subject.get("evidence_refs") or []
            if isinstance(evidence_by_id.get(ref), dict) and evidence_by_id[ref].get("text")
        ]
        placements_by_axis = {
            p.get("axis_id"): p for p in (subject.get("placements") or []) if isinstance(p, dict)
        }
        axis_placements = []
        for axis in (axis_1, axis_2):
            placement = placements_by_axis.get(axis.get("id")) or {}
            axis_placements.append({
                "axis_id": axis.get("id"),
                "position": placement.get("position"),
                "position_text": _pole_label(axis, placement.get("position")),
                "rationale": placement.get("rationale"),
            })
        coded_as = sorted({label for ref in subject.get("evidence_refs") or [] for label in categories_by_ref.get(ref, [])})
        member = {"subject_id": subject_id, "evidence_texts": texts, "axis_placements": axis_placements}
        if coded_as:
            member["coded_as"] = coded_as
        members.append(member)
    block = {
        "quadrant_id": quadrant.get("id"),
        "axes": axis_blocks,
        "members": members,
    }
    return (
        f"## 案例\ncase_id: {case_id}\n\n"
        "## 指定的主要目標象限、它代表的兩條選定軸、與研究者確認過的成員(只為這一格寫一份 persona)\n"
        "```yaml\n" + _yaml_dump(block) + "```"
    )


PERSONA_BIOGRAPHY_TERMS = ("上班族", "學生", "主婦", "媽媽", "爸爸", "孩子", "小孩", "兒子", "女兒", "老公", "老婆",
                           "太太", "先生", "丈夫", "妻子", "退休", "單身", "已婚", "下班", "週末", "假日", "年輕", "中年", "歲")
# persona-nine-canonical-fields (2026-10-01): the nine canonical Persona units
# are description, representative_quote, user_story and these six content fields.
PERSONA_CORE_FIELDS = ("context", "motivation_goal", "decision_pattern", "behavior_strategy", "challenge", "need")
PERSONA_EVIDENCE_WRITTEN_FIELDS = ("description",) + PERSONA_CORE_FIELDS + ("user_story",)
PERSONA_DISTINCT_PAIRS = (("need", "motivation_goal"), ("challenge", "motivation_goal"),
                          ("context", "behavior_strategy"), ("decision_pattern", "behavior_strategy"),
                          ("decision_pattern", "motivation_goal"), ("description", "motivation_goal"))
# Retired for new generation; older Personas that carry them stay readable.
PERSONA_RETIRED_FIELDS = ("worldview", "emotional_tendencies", "behavioral_patterns", "linguistic_style",
                          "motivation", "behavior")
# Consequence connectors: a field typed as synthesis may not add a cause the
# members' own words do not contain (it must be typed as inference instead).
PERSONA_CAUSAL_MARKERS = ("所以", "因此", "導致", "於是", "以致", "使得", "才會", "因而")


def _persona_text_overlap(a: str, b: str) -> float:
    grams = lambda text: {text[i:i + 2] for i in range(len(text) - 1)}  # noqa: E731
    left, right = grams(re.sub(r"[\s，。、；：！？「」（）]", "", a)), grams(re.sub(r"[\s，。、；：！？「」（）]", "", b))
    return len(left & right) / len(left | right) if left and right else 0.0


def _persona_embodiment_errors(persona: dict, member_text: str) -> list[str]:
    """persona-evidence-grounded-embodiment: the few boundaries a machine can
    check. Whether prose is a coherent perspective is the researcher's call."""
    pid, errors = persona.get("id"), []
    for field in PERSONA_RETIRED_FIELDS:
        if field in persona:
            errors.append(f"[persona_content] persona {pid} 不再產出「{field}」——新的 Persona 只有 9 格。")
    description = str(persona.get("description") or "").strip()
    if description and "我" not in description:
        errors.append(f"[persona_content] persona {pid} 的 description 要寫成第一人稱的一句話（我），不是第三人稱介紹。")
    story = str(persona.get("user_story") or "").strip()
    if not story:
        errors.append(f"[persona_content] persona {pid} 沒有 user_story——要用第一人稱把這一群人的決策過程說出來。")
    elif "我" not in story:
        errors.append(f"[persona_content] persona {pid} 的 user_story 要用第一人稱（我）來寫。")
    for field in PERSONA_EVIDENCE_WRITTEN_FIELDS:
        text = str(persona.get(field) or "")
        invented = [term for term in PERSONA_BIOGRAPHY_TERMS if term in text and term not in member_text]
        if invented:
            errors.append(f"[persona_content] persona {pid} 的「{field}」出現成員證據裡沒有的身分描述：{'、'.join(invented)}。")
    core = [str(persona.get(field) or "") for field in PERSONA_CORE_FIELDS if persona.get(field)]
    if core and sum("我" in text for text in core) < max(1, len(core) - 1):
        errors.append(f"[persona_content] persona {pid} 的核心欄位要用 Persona 自己的第一人稱（我）來寫。")
    typed = {entry.get("field"): entry.get("type") for entry in persona.get("field_provenance") or []
             if isinstance(entry, dict)}
    for field in PERSONA_EVIDENCE_WRITTEN_FIELDS:
        if typed.get(field) not in ("evidence_synthesis", "research_evidence"):
            continue
        added = [m for m in PERSONA_CAUSAL_MARKERS if m in str(persona.get(field) or "") and m not in member_text]
        if added:
            errors.append(f"[persona_content] persona {pid} 的「{field}」寫了成員證據沒有講到的因果（{'、'.join(added)}），"
                          f"要嘛拿掉，要嘛標成 evidence_inference 並說明推測。")
    for field, other in PERSONA_DISTINCT_PAIRS:
        a, b = str(persona.get(field) or ""), str(persona.get(other) or "")
        if a and b and _persona_text_overlap(a, b) >= 0.7:
            errors.append(f"[persona_content] persona {pid} 的「{field}」跟「{other}」幾乎是同一句話，每一欄要負責不同的事。")
    return errors


def _persona_content_errors(data: object, subject_ids: set[str],
                             designated_quadrant_id: str | None = None,
                             evidence_refs_by_subject: dict[str, set[str]] | None = None,
                             allow_researcher: bool = False,
                             member_texts: dict[str, str] | None = None) -> list[str]:
    """generate-persona-for-target-quadrant：跟 `_persona_matrix_errors` 一樣
    的純函式，檢查 schema 表達不了的事：每個 persona 的 `member_subject_ids`
    是不是真的在供給的 persona_matrix.yaml 受訪者清單裡；`representative_quote`
    的 `evidence_ref` 是不是真的屬於這份 persona 自己的某個成員。

    `designated_quadrant_id` 選填——只有傳了才額外檢查「恰好產出 1 份
    persona，而且它的 quadrant_id 就是這一格」，省略時完全不做這項檢查
    （既有只測參照完整性的呼叫端行為不變）。

    `evidence_refs_by_subject` 選填——只有傳了才檢查 representative_quote
    與推論欄位的證據歸屬，省略時這兩項都不做。

    evidence-aware-persona-provenance：四型來源、欄位政策與各型別各自欠的
    證明，全部寫在 `V.persona_field_provenance_errors`，不在這裡複製一份——
    同一條規則兩份實作會漂移，這個專案已經付過那個代價。政策表本身又寫在
    `schema/service-design-judgment.criteria.yaml`，跟送給模型的那段文字同
    一筆來源。
    """
    errors = V.structural_errors(data, V.load_schema("persona-content"))
    if not isinstance(data, dict):
        return errors
    personas = data.get("personas") or []
    for persona in personas:
        if not isinstance(persona, dict):
            continue
        member_ids = persona.get("member_subject_ids") or []
        for ref in member_ids:
            if ref not in subject_ids:
                errors.append(f"[persona_content] persona {persona.get('id')} 引用不存在的受訪者：{ref}")
        if evidence_refs_by_subject is not None:
            quote = persona.get("representative_quote")
            if isinstance(quote, dict):
                quote_ref = quote.get("evidence_ref")
                member_evidence = {
                    ref for sid in member_ids for ref in evidence_refs_by_subject.get(sid, set())
                }
                if quote_ref is not None and quote_ref not in member_evidence:
                    errors.append(
                        f"[persona_content] persona {persona.get('id')} 的 representative_quote 引用了"
                        f"不屬於這份 persona 任何成員的證據：{quote_ref}"
                    )
    errors.extend(V.persona_field_provenance_errors(data, evidence_refs_by_subject,
                                                    allow_researcher=allow_researcher))
    # Generation only: the embodiment boundaries apply to what the model writes,
    # not to a stored Persona being exported or confirmed.
    if member_texts is not None:
        for persona in personas:
            if isinstance(persona, dict):
                text = " ".join(member_texts.get(sid, "") for sid in persona.get("member_subject_ids") or [])
                errors.extend(_persona_embodiment_errors(persona, text))
    if designated_quadrant_id is not None:
        if len(personas) != 1:
            errors.append(
                f"[persona_content] 應該恰好產出 1 份 persona（對應 {designated_quadrant_id}），"
                f"實際 {len(personas)} 份"
            )
        else:
            qid = personas[0].get("quadrant_id") if isinstance(personas[0], dict) else None
            if qid != designated_quadrant_id:
                errors.append(
                    f"[persona_content] persona 應該對應主要目標象限 {designated_quadrant_id}，"
                    f"實際引用了 {qid}"
                )
    return errors


def persona_content(evidence: list, quadrant: dict, axes: list, persona_matrix_doc: dict,
                     categories_by_id: dict, opts: dict) -> tuple:
    """generate-persona-for-target-quadrant：把研究者指定的主要目標象限，依
    確認過的成員與他們在選定兩條軸上的落點，寫成唯一一份 persona——不是
    四象限各一份。取代 add-persona-content 原本「四象限各長一份」的行為。

    `quadrant`：`{"id": "QUADRANT-N", "subject_ids": [...],
    "axis_1_pole": "...", "axis_2_pole": "..."}`——`subject_ids` 是研究者在
    target_segmentation_selection.yaml 確認過的成員，不是候選檔的建議；
    `axis_1_pole`/`axis_2_pole` 是這一格在兩條選定軸上代表的兩端。呼叫端
    （`__main__.py`）負責用 persona_matrix.yaml 與選定兩軸算出格子兩端、
    合併 target_segmentation_selection.yaml 的成員，算出這個 quadrant，並確認它就是研究者
    指定的主要目標、且成員不是空的——空格子不能被指定為主要目標（見
    target-segmentation-designation），所以這裡收到的 quadrant 不會是空的。

    `axes`：長度 2 的 list，依序對應 `quadrant` 的 `axis_1_pole`／
    `axis_2_pole`，每一個是 persona_matrix.yaml 裡完整的軸物件（label、
    pole_low、pole_high、discriminating_basis、grounded_in）。呼叫端只挑出
    segmentation_axis_selection.yaml 選定的這兩條，其餘軸不傳進來。

    `categories_by_id`：axial_coding.yaml 的類別 id → 類別物件，只用來把
    `axes` 的 `grounded_in` 類別 id 解析成類別 label，不是把整份主軸編碼
    餵進去。

    獨立指令（`sbp-gen persona-content --run <dir>`）呼叫這個函式，不進主管線。
    """
    case_id = opts.get("case_id") or "unknown"
    subjects_by_id = {
        s["id"]: s for s in (persona_matrix_doc.get("subjects") or []) if isinstance(s, dict) and s.get("id")
    }
    subject_ids = set(subjects_by_id)
    evidence_refs_by_subject = {
        sid: set(subject.get("evidence_refs") or []) for sid, subject in subjects_by_id.items()
    }
    designated_quadrant_id = quadrant.get("id")
    evidence_by_id = {e["id"]: e for e in evidence if isinstance(e, dict) and e.get("id")}
    member_texts = {
        sid: " ".join(str((evidence_by_id.get(ref) or {}).get(key) or "")
                      for ref in refs for key in ("text", "role"))
        for sid, refs in evidence_refs_by_subject.items()
    }
    user = _persona_content_user_message(evidence, quadrant, axes, subjects_by_id, categories_by_id, case_id)
    data, usage = _generate(
        "persona_content", _system("persona_content"), user,
        lambda d: _persona_content_errors(d, subject_ids, designated_quadrant_id, evidence_refs_by_subject,
                                          member_texts=member_texts),
        opts,
    )
    data["case_id"] = case_id
    return data, usage


def _jtbd_user_message(evidence: list, categories_doc: dict, case_id: str,
                        core_category: dict | None = None) -> str:
    units = [
        _evidence_unit(e, "id", "role", "source", "text")
        for e in evidence if isinstance(e, dict)
    ]
    categories = [
        {"id": c.get("id"), "label": c.get("label"), "discriminating_basis": c.get("discriminating_basis")}
        for c in (categories_doc.get("categories") or []) if isinstance(c, dict)
    ]
    return (
        f"## 案例\ncase_id: {case_id}\n\n"
        + _core_category_block(core_category)
        + "## 主軸編碼類別\n```yaml\n" + _yaml_dump(categories) + "```\n\n"
        "## 證據單元\n```yaml\n" + _yaml_dump(units) + "```"
    )


def _jtbd_errors(data: object, category_ids: set[str], evidence_ids: set[str]) -> list[str]:
    """add-jtbd：跟 `_persona_matrix_errors` 一樣的純函式，檢查 schema 表達不了
    的一件事：每一則 Job 的 `grounded_in` 是不是真的在主軸編碼的類別清單裡、
    `evidence_refs` 是不是真的在證據清單裡。

    correct-false-machine-arbiter-declarations：no-embedded-solution 通則點名
    JTBD、Job Map、HMW、issues 四種產物都不得內含解法，但 `_SOLUTION_LANGUAGE`
    在這之前只用在 issues 一種——這裡補上 JTBD 這一份，跟其他規則一樣觸發重試，
    不是靜默丟棄。"""
    errors = V.structural_errors(data, V.load_schema("jtbd"))
    if not isinstance(data, dict):
        return errors
    for job in data.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        for ref in job.get("grounded_in") or []:
            if ref not in category_ids:
                errors.append(f"[jtbd] job {job.get('id')} 引用不存在的類別：{ref}")
        for ref in job.get("evidence_refs") or []:
            if ref not in evidence_ids:
                errors.append(f"[jtbd] job {job.get('id')} 引用不存在的證據：{ref}")
        for field in ("situation", "progress", "outcome"):
            value = job.get(field)
            if isinstance(value, str) and _SOLUTION_LANGUAGE.search(value):
                errors.append(f"[jtbd] job {job.get('id')} 的 {field} 內含解法或指名技術／產品，不該出現在這裡：{value}")
        # jtbd-carries-forces-and-layers：emotional_layer／social_layer 的
        # text 也是自由文字，一樣可能夾帶解法——跟三格同一條規則、同一個
        # 檢查，不是新規則。
        for layer_field in ("emotional_layer", "social_layer"):
            layer = job.get(layer_field)
            if isinstance(layer, dict):
                text = layer.get("text")
                if isinstance(text, str) and _SOLUTION_LANGUAGE.search(text):
                    errors.append(f"[jtbd] job {job.get('id')} 的 {layer_field} 內含解法或指名技術／產品，不該出現在這裡：{text}")
        forces = job.get("forces")
        if isinstance(forces, dict):
            if forces.get("status") == "not_supported" and not (forces.get("rationale") or "").strip():
                errors.append(f"[jtbd] job {job.get('id')} 的 forces 標成 not_supported 卻沒有寫 rationale")
            for force_field in ("push", "pull", "anxiety", "habit"):
                value = forces.get(force_field)
                if isinstance(value, str) and _SOLUTION_LANGUAGE.search(value):
                    errors.append(f"[jtbd] job {job.get('id')} 的 forces.{force_field} 內含解法或指名技術／產品，不該出現在這裡：{value}")
        strength = job.get("strength")
        if strength is not None and not (job.get("strength_basis") or "").strip():
            errors.append(f"[jtbd] job {job.get('id')} 有 strength 卻沒有 strength_basis")
    return errors


def jtbd(evidence: list, categories_doc: dict, opts: dict, core_category: dict | None = None) -> tuple:
    """add-jtbd：把主軸編碼（axial-coding）產出的類別與證據讀成情境／進展／
    結果三格的 Job 陳述。

    獨立指令（`sbp-gen jtbd --run <dir>`）呼叫這個函式，不進主管線——跟
    `persona_matrix()` 消費完全一樣的兩個輸入（evidence.yaml、
    axial_coding.yaml），但不讀 persona_matrix.yaml：兩者是平行讀法，不是
    依序推導（見 design.md「`jtbd` is a standalone CLI command consuming
    `evidence.yaml` and `axial_coding.yaml`, not `persona_matrix.yaml`」）。

    `core_category`（core-category-as-ranking-signal-not-filter）：同
    `persona_matrix()` 的同名參數——只當排序/加權訊號放進 user message，
    `categories_doc`／`evidence` 完整不縮減。省略或傳 `None` 時完全沒有這段
    提示，行為不變。
    """
    case_id = opts.get("case_id") or "unknown"
    category_ids = {c["id"] for c in (categories_doc.get("categories") or []) if isinstance(c, dict) and c.get("id")}
    evidence_ids = {e["id"] for e in evidence if isinstance(e, dict) and e.get("id")}
    user = _jtbd_user_message(evidence, categories_doc, case_id, core_category)
    data, usage = _generate(
        "jtbd", _system("jtbd"), user,
        lambda d: _jtbd_errors(d, category_ids, evidence_ids), opts,
    )
    data["case_id"] = case_id
    # multi-jtbd-single-job-source (2026-10-03): no workflow calls this stage
    # any more (one JTBD source); it stays for reading old runs. The strength
    # ordering is retired -- need strength is never ranked for the researcher.
    return data, usage


def _job_map_user_message(jobs_doc: dict, evidence: list, case_id: str) -> str:
    evidence_by_id = {e["id"]: e for e in evidence if isinstance(e, dict) and e.get("id")}
    jobs = []
    for job in jobs_doc.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        units = [
            _evidence_unit(e, "id", "role", "source", "text")
            for ref in job.get("evidence_refs") or []
            for e in [evidence_by_id.get(ref)] if e is not None
        ]
        jobs.append({
            "id": job.get("id"), "label": job.get("label"),
            "situation": job.get("situation"), "progress": job.get("progress"), "outcome": job.get("outcome"),
            "evidence": units,
        })
    return (
        f"## 案例\ncase_id: {case_id}\n\n"
        "## Job 清單（每則已附上自己範圍內的證據）\n```yaml\n" + _yaml_dump(jobs) + "```"
    )


def _job_map_errors(data: object, job_evidence: dict[str, set[str]]) -> list[str]:
    """add-job-map：跟 `_jtbd_errors` 一樣的純函式，檢查 schema 表達不了的兩件
    事：每個 job_map 條目的 job_id 是不是真的在 jtbd.yaml 裡、每個階段的
    evidence_refs 是不是真的在那則 Job 自己的證據範圍內（不是別則 Job 的）。

    correct-false-machine-arbiter-declarations：同 `_jtbd_errors`，補上
    no-embedded-solution 對 Job Map 這一份的檢查。"""
    errors = V.structural_errors(data, V.load_schema("job-map"))
    if not isinstance(data, dict):
        return errors
    for entry in data.get("job_maps") or []:
        if not isinstance(entry, dict):
            continue
        job_id = entry.get("job_id")
        if job_id not in job_evidence:
            errors.append(f"[job_map] job_maps 引用不存在的 job_id：{job_id}")
            continue
        allowed = job_evidence[job_id]
        for step in entry.get("steps") or []:
            if not isinstance(step, dict):
                continue
            for ref in step.get("evidence_refs") or []:
                if ref not in allowed:
                    errors.append(f"[job_map] job {job_id} 的 step {step.get('id')} 引用了範圍外的證據：{ref}")
            for field in ("label", "description"):
                value = step.get(field)
                if isinstance(value, str) and _SOLUTION_LANGUAGE.search(value):
                    errors.append(
                        f"[job_map] job {job_id} 的 step {step.get('id')} 的 {field} 內含解法或指名技術／產品，"
                        f"不該出現在這裡：{value}"
                    )
    return errors


def job_map(jobs_doc: dict, evidence: list, opts: dict) -> tuple:
    """add-job-map：把 JTBD（jtbd.yaml）產出的每一則 Job 拆成一組有序、
    solution-independent 的階段。

    獨立指令（`sbp-gen job-map --run <dir>`）呼叫這個函式，不進主管線——讀
    jtbd.yaml（要拆的 Job）跟 evidence.yaml（把 Job 自己的 evidence_refs
    id 解回原文），不讀 axial_coding.yaml 或 persona_matrix.yaml（見
    design.md「`job-map` is a standalone CLI command consuming `jtbd.yaml`
    and `evidence.yaml`, not `axial_coding.yaml` or `persona_matrix.yaml`」）。
    """
    case_id = opts.get("case_id") or "unknown"
    job_evidence = {
        job["id"]: set(job.get("evidence_refs") or [])
        for job in (jobs_doc.get("jobs") or []) if isinstance(job, dict) and job.get("id")
    }
    user = _job_map_user_message(jobs_doc, evidence, case_id)
    data, usage = _generate(
        "job_map", _system("job_map"), user,
        lambda d: _job_map_errors(d, job_evidence), opts,
    )
    data["case_id"] = case_id
    return data, usage


def _narrative_framing_section(selections) -> str:
    """The researcher's selected JTBDs as one framing section
    (multi-jtbd-single-job-source: one or more JTBDs form one design scope).
    A single selection dict reads as a one-item list. Framing, never evidence
    (method-dependency `jtbd_to_hmw: frames_scope_not_evidence_whitelist`)."""
    if isinstance(selections, dict):
        selections = [selections]
    items = []
    for number, selection in enumerate([s for s in selections or [] if isinstance(s, dict)], start=1):
        item = {key: selection.get(key) for key in ("narrative", "situation", "progress", "motivation", "outcome",
                                                     "selective_refs", "grounded_in") if selection.get(key)}
        if item:
            # 2026-10-03: each framing records which selected JTBDs it was
            # induced from (supporting_jtbd_refs), by this position.
            items.append({"id": f"JTBD-{number}", **item})
    if not items:
        return ""
    return ("## 研究者選定的顧客任務（一則或多則，合起來是這次的設計範圍；框架，不是證據）\n```yaml\n"
            + _yaml_dump(items) + "```\n\n")


def _how_might_we_user_message(categories_doc: dict, category_properties_doc: dict, case_id: str,
                                selections=None, selective_doc: dict | None = None, *,
                                evidence: list | None = None) -> str:
    """HMW method v9 (multi-jtbd-single-job-source, 2026-10-03): the material
    for the three analytical functions -- observed behaviour (axial
    relationships + behaviour patterns), the user-need scope (the selected
    JTBD set + related selective themes), and the tension framing the model
    derives between them. No `jobs` or `job_map_steps`: the v8 lens artifacts
    are retired; the three analytical functions are kept."""
    if isinstance(selections, dict):
        selections = [selections]
    selections = [s for s in selections or [] if isinstance(s, dict)]
    axes_by_category: dict[str, list] = {}
    for prop in (category_properties_doc or {}).get("properties") or []:
        cat = prop.get("category_id")
        if cat:
            axes_by_category.setdefault(cat, []).append(prop)
    themes = {t.get("id"): t for t in (selective_doc or {}).get("themes") or [] if isinstance(t, dict)}
    selected_theme_ids = [ref for s in selections for ref in s.get("selective_refs") or [] if ref in themes]
    in_scope = {cat for s in selections for cat in s.get("grounded_in") or []}
    in_scope |= {cat for ref in selected_theme_ids for cat in themes[ref].get("axial_refs") or []}
    evidence_by_id = {e["id"]: e for e in (evidence or []) if isinstance(e, dict) and e.get("id")}

    blocks = []
    for category in categories_doc.get("categories") or []:
        cat_id = category.get("id")
        entry: dict = {"category": {"id": cat_id, "label": category.get("label"),
                                     "discriminating_basis": category.get("discriminating_basis")}}
        for field in ("conditions", "actions", "consequences"):
            if category.get(field):
                entry["category"][field] = category.get(field)
        axes = axes_by_category.get(cat_id) or []
        if axes:
            entry["axes"] = [
                {"id": prop.get("id"), "name": prop.get("name"), "pole_low": prop.get("pole_low"),
                 "pole_high": prop.get("pole_high"), "discriminating_basis": prop.get("discriminating_basis"),
                 "evidence_refs": prop.get("evidence_refs")}
                for prop in axes
            ]
        if cat_id in in_scope:
            entry["in_scope"] = True
            framing_evidence = [
                _evidence_unit(evidence_by_id[ref], "id", "text")
                for ref in category.get("evidence_refs") or [] if ref in evidence_by_id
            ]
            if framing_evidence:
                entry["framing_evidence"] = framing_evidence
        blocks.append(entry)
    related = [{"id": ref, "name": themes[ref].get("name"), "core_sentence": themes[ref].get("core_sentence"),
                "key_insights": themes[ref].get("key_insights")} for ref in dict.fromkeys(selected_theme_ids)]
    related_block = ("## 選定顧客任務依據的選擇編碼主題\n```yaml\n" + _yaml_dump(related) + "```\n\n") if related else ""
    return (
        f"## 案例\ncase_id: {case_id}\n\n"
        + _narrative_framing_section(selections)
        + related_block
        + "## 各類別的主軸關係與行為模式（in_scope 標出選定顧客任務涵蓋的類別）\n```yaml\n" + _yaml_dump(blocks) + "```"
    )


# form-hmw-tension-first: fields of the retired HMW method (framing roles,
# priority, convergence). Older documents keep them and stay readable; a new
# generation must not carry them.
HMW_RETIRED_ENTRY_FIELDS = ("framing_role", "priority", "priority_basis", "tension")
HMW_RETIRED_DOCUMENT_FIELDS = ("final_questions", "final_questions_rationale")
# Raw pipeline identifiers never belong in researcher-facing text. Only the
# canonical id prefixes are matched; natural-language method jargon is the
# prompt's job, not a blacklist's.
_RAW_INTERNAL_ID = re.compile(r"(?<![A-Za-z0-9])(?:CAT|JOB|E|HMW|STEP)-\d+")


def _selection_count(selections) -> int:
    if isinstance(selections, dict):
        return 1
    return len([s for s in selections or [] if isinstance(s, dict)])


def _how_might_we_errors(data: object, category_ids: set[str], evidence_ids: set[str], *,
                         generation: bool = False, selection_count: int | None = None) -> list[str]:
    """add-how-might-we：跟 `_jtbd_errors` 一樣的純函式，檢查 schema 表達不了
    的一件事：每則 HMW 的 grounded_in 是不是真的在類別清單裡、evidence_refs
    是不是真的在證據清單裡。

    correct-false-machine-arbiter-declarations：同 `_jtbd_errors`，補上
    no-embedded-solution 對 HMW 這一份的檢查——四種產物裡最後一種，issues
    以外全部補齊。

    `generation=True`（form-hmw-tension-first；hmw-framings-from-problem-structures
    改為 1–3 則框法）只在生成時傳：1–3 則或零則加原因、每則要 problem_structure
    與類別陣列、拒收舊方法欄位（含 tension）、不露內部編號。讀舊檔時不傳，priority 與
    final_questions 的檢查只會在舊文件上碰到。"""
    errors = V.structural_errors(data, V.load_schema("hmw"))
    if not isinstance(data, dict):
        return errors
    for entry in data.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        grounded = entry.get("grounded_in")
        for cat_id in (grounded if isinstance(grounded, list) else [grounded]):
            if cat_id not in category_ids:
                errors.append(f"[how_might_we] entry {entry.get('id')} 引用不存在的類別：{cat_id}")
        for ref in entry.get("evidence_refs") or []:
            if ref not in evidence_ids:
                errors.append(f"[how_might_we] entry {entry.get('id')} 引用不存在的證據：{ref}")
        for field in ("problem_structure", "tension", "statement"):
            value = entry.get(field)
            if isinstance(value, str) and _SOLUTION_LANGUAGE.search(value):
                errors.append(f"[how_might_we] entry {entry.get('id')} 的 {field} 內含解法或指名技術／產品，不該出現在這裡：{value}")
        priority = entry.get("priority")
        if priority is not None and not (entry.get("priority_basis") or "").strip():
            errors.append(f"[how_might_we] entry {entry.get('id')} 有 priority 卻沒有 priority_basis")
    # hmw-priority-and-convergence (older documents only since
    # form-hmw-tension-first; new generations reject these fields): convergence aggregation checks. Every
    # final question must name statements that actually exist in this same
    # document, and must name at least one -- schema's minItems already
    # blocks zero, this catches an id pointing at nothing.
    entry_ids = {e.get("id") for e in (data.get("entries") or []) if isinstance(e, dict)}
    for question in data.get("final_questions") or []:
        if not isinstance(question, dict):
            continue
        if _SOLUTION_LANGUAGE.search(question.get("statement") or ""):
            errors.append(f"[how_might_we] final_questions 的陳述內含解法或指名技術／產品，不該出現在這裡：{question.get('statement')}")
        for ref in question.get("aggregates") or []:
            if ref not in entry_ids:
                errors.append(f"[how_might_we] final_questions 聚合了不存在的 entry：{ref}")
    # hmw-framings-from-problem-structures (2026-10-01): generation-time only;
    # reading an older document never passes `generation`. A successful
    # formation is one to three project-level framings synthesized from the
    # tensions (never padded to three, never one per tension), or zero with a
    # stated reason. Each framing names its problem structure and the
    # categories it draws on (any the run has -- the JTBD is problem context,
    # not a whitelist). Whether a framing drifts from the JTBD is the
    # instruction's and the researcher's judgement, not a machine check.
    if generation:
        reason = ((data.get("insufficient_evidence") or {}).get("reason") or "").strip()
        entries = [e for e in (data.get("entries") or []) if isinstance(e, dict)]
        if not entries and not reason:
            errors.append("[how_might_we] entries 是空的，但沒有 insufficient_evidence.reason——要說明證據為什麼撐不起任何設計框法")
        if entries and reason:
            errors.append("[how_might_we] 有 entries 就不能同時寫 insufficient_evidence")
        if len(entries) > 3:
            errors.append(f"[how_might_we] 最多 3 則框法，現在是 {len(entries)} 則")
        for field in HMW_RETIRED_DOCUMENT_FIELDS:
            if field in data:
                errors.append(f"[how_might_we] 新的 HMW 不再產出 {field}")
        for entry in entries:
            for field in HMW_RETIRED_ENTRY_FIELDS:
                if field in entry:
                    errors.append(f"[how_might_we] entry {entry.get('id')} 不再產出 {field}")
            if not str(entry.get("problem_structure") or "").strip():
                errors.append(f"[how_might_we] entry {entry.get('id')} 缺少 problem_structure（這則框法要處理的問題結構）")
            if not isinstance(entry.get("grounded_in"), list):
                errors.append(f"[how_might_we] entry {entry.get('id')} 的 grounded_in 要寫成類別陣列")
            for field in ("problem_structure", "statement"):
                found = _RAW_INTERNAL_ID.search(entry.get(field) or "")
                if found:
                    errors.append(f"[how_might_we] entry {entry.get('id')} 的 {field} 露出內部編號 {found.group(0)}，要用研究者看得懂的話寫")
    # 2026-10-03 (HMW v12): a new framing names the selected JTBDs it was
    # induced from; provenance, not priority.
    if generation and selection_count:
        valid = {f"JTBD-{n}" for n in range(1, selection_count + 1)}
        for entry in data.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            refs = entry.get("supporting_jtbd_refs") or []
            if not refs:
                errors.append(f"[how_might_we] {entry.get('id')} 要記 supporting_jtbd_refs：它從哪幾則選定的顧客任務歸納而來")
            for ref in refs:
                if ref not in valid:
                    errors.append(f"[how_might_we] {entry.get('id')} 的 supporting_jtbd_refs 引用不存在的顧客任務：{ref}")
    return errors


def how_might_we(categories_doc: dict, category_properties_doc: dict, evidence: list, opts: dict,
                  selections=None, selective_doc: dict | None = None) -> tuple:
    """HMW method v9 (multi-jtbd-single-job-source, 2026-10-03): the selected
    JTBDs, one or more, frame one design scope. The model reads observed
    behaviour (axial relationships, behaviour patterns), the need scope (the
    JTBD set and its selective themes) and derives the tensions between them
    before writing one to three project-level framings. Every category is still
    provided; the scope marks, it does not filter."""
    case_id = opts.get("case_id") or "unknown"
    category_ids = {c["id"] for c in (categories_doc.get("categories") or []) if isinstance(c, dict) and c.get("id")}
    evidence_ids = {e["id"] for e in evidence if isinstance(e, dict) and e.get("id")}
    user = _how_might_we_user_message(categories_doc, category_properties_doc, case_id, selections, selective_doc,
                                       evidence=evidence)
    data, usage = _generate(
        "how_might_we", _system("how_might_we"), user,
        lambda d: _how_might_we_errors(d, category_ids, evidence_ids, generation=True,
                                       selection_count=_selection_count(selections)), opts,
    )
    data["case_id"] = case_id
    return data, usage

def _step_ids_from_journey(journey_data: dict | None) -> set[str]:
    """Only confirmed scenarios count as "the run's confirmed journey" —
    a briefing citing a step from a still-`proposed` scenario is citing
    something that was never actually confirmed."""
    if not journey_data:
        return set()
    return {
        step.get("id")
        for scenario in journey_data.get("scenarios") or []
        if scenario.get("status") == "confirmed"
        for step in scenario.get("steps") or []
    }


def _brief_user_message(draft: dict, evidence: list, conflicts: list, role_coverage: dict, case: dict) -> str:
    boundary = {k: case.get(k) for k in ("case_id", "service_type", "customers", "primary_provider", "journey_start", "journey_end") if case.get(k) is not None}
    nodes = [{"id": n.get("id"), "label": n.get("label"), "step": n.get("step"), "layer": n.get("layer")} for n in (draft.get("graph") or {}).get("nodes") or []]
    unaddressed = [a for a in draft.get("annotations") or [] if not a.get("node_refs")]
    return (
        "## Case boundary\n```yaml\n" + _yaml_dump(boundary) + "```\n\n"
        + "## Graph nodes\n```yaml\n" + _yaml_dump(nodes) + "```\n\n"
        + "## Research gaps (from projection)\n```yaml\n" + _yaml_dump(draft.get("research_gaps") or []) + "```\n\n"
        + "## Evidence index\n```yaml\n" + _yaml_dump(_project(evidence)) + "```\n\n"
        + "## Conflicts\n```yaml\n" + _yaml_dump(conflicts or []) + "```\n\n"
        + "## Role coverage facts\n```yaml\n" + _yaml_dump(role_coverage or {}) + "```\n\n"
        + "## Experience annotations with no addressing service action\n```yaml\n" + _yaml_dump(unaddressed) + "```"
    )


def brief(draft: dict, evidence: list, conflicts: list, role_coverage: dict, case: dict, opts: dict) -> tuple:
    """Reads the already-computed facts (gaps, conflicts, role coverage,
    unaddressed annotations) and writes a ranked, cited briefing — the
    synthesis step add-research-briefing-layer adds after projection.
    Non-fatal by construction: the caller (`__main__.py`) decides what a
    failure here means for the run; this function just raises like any
    other stage on exhausted retries.
    """
    evidence_ids = {e["id"] for e in evidence}
    node_ids = {n.get("id") for n in (draft.get("graph") or {}).get("nodes") or []}
    conflict_ids = {c.get("id") for c in conflicts or [] if isinstance(c, dict)}
    step_ids = _step_ids_from_journey(draft.get("journey"))
    relation_ids = {r.get("id") for r in draft.get("relations") or [] if isinstance(r, dict)}
    marker_ids = {m.get("id") for m in draft.get("markers") or [] if isinstance(m, dict)}
    annotation_ids = {a.get("id") for a in draft.get("annotations") or [] if isinstance(a, dict)}
    run_has_open_items = bool(draft.get("research_gaps") or conflicts)

    def validate(data):
        # 跟 journey()／structure() 同一個模式：schema_version/case_id 是呼叫端
        # 成功後才補上的，不該逼模型自己猜、也不該讓它白白吃掉一次重試額度。
        if isinstance(data, dict):
            data.setdefault("schema_version", 2)
            data.setdefault("case_id", case["case_id"])
        errors = V.structural_errors(data, V.load_schema("briefing"))
        errors += V.briefing_errors(data, evidence_ids, node_ids, conflict_ids, step_ids, run_has_open_items, relation_ids, marker_ids, annotation_ids)
        return errors

    user = _brief_user_message(draft, evidence, conflicts, role_coverage, case)
    data, usage = _generate("brief", _prompt("brief"), user, validate, opts)
    data["schema_version"] = 2
    data["case_id"] = case["case_id"]
    data.setdefault("interpretation", {"operation_summary": data.get("summary", "研究者尚未完成服務設計判讀。"), "items": []})
    return data, usage


def layer_role_hint(layer: str, nodes: list, evidence: list) -> dict | None:
    """該層最可能對應的角色，以及證據池裡有沒有這個角色的第一手資料
    （surface-blank-cell-meaning 決定一）。

    角色來源是該層既有節點的 `actor` 欄位（出現頻率最高者）——不是猜的，是
    這份圖裡已經記錄的事實；「有沒有第一手資料」比對 `evidence[].role` 的
    既有集合，跟 `server.py` 的 `evidence_roles()`（distinguish-first-hand
    -from-reported-claims）同一種判定方式。純字面比對、不呼叫模型，也繼承
    `tools/role_coverage.py` 的教訓：字面找不到不等於真的沒有，這裡回傳的
    只是線索，不是定論——呼叫端的說明文字必須留「請人工核對」的提醒。

    找不到任何角色時回傳 None。
    """
    actor_counts: Counter = Counter()
    for node in nodes or []:
        if isinstance(node, dict) and node.get("layer") == layer and node.get("actor"):
            actor_counts[node["actor"]] += 1
    if not actor_counts:
        return None
    role = actor_counts.most_common(1)[0][0]
    roles_with_evidence = {e.get("role") for e in (evidence or []) if isinstance(e, dict) and e.get("role")}
    return {"role": role, "has_first_hand": role in roles_with_evidence}


def project(graph: dict, hypotheses: dict | None, conflicts: list, case: dict, *, journey_data=None, annotations=None, evidence=None) -> dict:
    """Pure, deterministic graph → coverage grid projection; no model call.

    `hypotheses=None` means reference/reconcile have not been run for this case
    yet (streamline-sbp-generation-pipeline 決定一 — they are a deferred, optional
    stage now). The projected draft's `expectations` key is present only when
    `hypotheses` is given, never written as an empty list, so the review UI can
    tell "not run" apart from "ran, found nothing."
    """
    nodes = graph.get("nodes") or []
    expectations = (hypotheses or {}).get("expectations") or []
    annotations = annotations or []
    primary = None
    journey_steps = []
    if journey_data:
        primary = journey_data.get("primary_scenario")
        scenario = next((s for s in journey_data.get("scenarios") or [] if s.get("id") == primary), None)
        journey_steps = (scenario or {}).get("steps") or []
    # 這一步的證據其實被畫在哪些其他步驟（fix-fabricated-research-gaps-from-step
    # -axis-collapse 決定二）：journey 步驟 n 的 evidence_refs，被 step != n 的
    # 節點 claim 引用時，回傳那些「實際步驟」的集合，n 自己整欄沒有節點時才有意義。
    evidence_actual_steps: dict[str, set] = {}
    for n in nodes:
        for cl in n.get("claims") or []:
            ref = cl.get("evidence_ref") if isinstance(cl, dict) else None
            if ref:
                evidence_actual_steps.setdefault(ref, set()).add(n.get("step"))

    def misplaced_steps(journey_step_no: int) -> list[int]:
        if journey_step_no > len(journey_steps):
            return []
        refs = journey_steps[journey_step_no - 1].get("evidence_refs") or []
        actual = {s for ref in refs for s in evidence_actual_steps.get(ref, ()) if s != journey_step_no}
        return sorted(actual)

    cells = []
    for step in range(1, len(case["steps"]) + 1):
        elsewhere = misplaced_steps(step)
        for layer in LAYERS:
            node_refs = [
                n["id"] for n in nodes
                if n.get("step") == step and n.get("layer") == layer
                and n.get("actor_boundary") not in ("external_dependency", "out_of_scope")
                and V.node_in_primary_scenario(n, journey_data)
            ]
            exp_refs = [x["id"] for x in expectations if x.get("step") == step and x.get("layer") == layer and x.get("status") != "out_of_scope"]
            cell = {"step": step, "layer": layer, "node_refs": node_refs, "expectation_refs": exp_refs}
            if node_refs:
                cell["coverage_state"] = "has_evidence"
            elif elsewhere:
                # 這一整欄沒有節點，但 journey 指派給這一步的證據已經被畫在別的步驟——
                # 不是真的缺資料，是位置問題，不套用「尚未觀察到」的空泛措辭
                # （不得回頭寫 blank，那個欄位保留給真的沒有證據的情況）。
                steps_text = "、".join(f"第 {s} 步" for s in elsewhere)
                cell["coverage_state"] = "expected_unverified"
                # narrow-follow-up-cards-to-hypothesis-cause：明確標記這一格
                # expected_unverified 的成因是「位置畫錯」，不是「有未驗證假設」，
                # 讓呈現層不必去猜 blank_note 的句型就能分辨——這一格即使也巧合
                # 命中某個 expectation_refs，呈現層也不該把它當成假設型顯示。
                cell["expected_unverified_cause"] = "elsewhere"
                cell["blank_note"] = f"這一步的支撐資料目前畫在{steps_text}，請確認位置"
            else:
                unresolved = [x for x in expectations if x.get("id") in exp_refs and x.get("status") in ("unsupported", "partially_supported")]
                if unresolved:
                    cell["coverage_state"] = "expected_unverified"
                    cell["expected_unverified_cause"] = "hypothesis"
                    roles = sorted({role for x in unresolved for role in x.get("ask_roles") or []})
                    questions = "；".join(x.get("validation_question", "") for x in unresolved if x.get("validation_question"))
                    role_text = "、".join(roles) if roles else "相關角色"
                    note = f"向{role_text}確認：{questions}" if questions else f"向{role_text}確認這個待驗證服務環節"
                else:
                    cell["coverage_state"] = "unobserved"
                    # surface-blank-cell-meaning 決定二：不再是把步驟全名念一遍
                    # 的樣板——步驟與層別由呈現層自己顯示（決定三），這裡只給
                    # 「該問誰」的線索，而且明講這只是線索、不是定論。
                    hint = layer_role_hint(layer, nodes, evidence)
                    if hint is None:
                        note = "從現有證據找不到對應的角色，請人工核對這一步驟由誰負責"
                    elif hint["has_first_hand"]:
                        note = f"這裡的動作通常由「{hint['role']}」負責，證據池裡有「{hint['role']}」的第一手資料，可核對是否涵蓋這一步"
                    else:
                        note = f"這裡的動作通常由「{hint['role']}」負責，但證據池裡沒有「{hint['role']}」的第一手說法，建議向「{hint['role']}」確認"
                cell.update({"blank": "no_evidence", "blank_note": note})
            cells.append(cell)
    research_gaps = []
    for x in expectations:
        if x.get("status") not in ("unsupported", "partially_supported"):
            continue
        research_gaps.append({"id": f"G-{x.get('id')}", "type": "expected_unverified", "step": x.get("step"), "layer": x.get("layer"), "expectation_ref": x.get("id"), "ask_roles": x.get("ask_roles") or [], "question": x.get("validation_question", "")})
    step_index = {s.get("id"): i for i, s in enumerate(journey_steps, 1)}
    for annotation in annotations:
        if annotation.get("type") == "pain_point" and not annotation.get("node_refs"):
            research_gaps.append({"id": f"G-{annotation.get('id')}", "type": "unserved_pain_point", "step": step_index.get(annotation.get("step_ref")), "annotation_ref": annotation.get("id"), "question": f"目前哪個服務動作處理「{annotation.get('text', '')}」？"})
    # 非主情境的節點被投影排除（separate-non-primary-scenarios-in-blueprint
    # 決定二），但不得從資料裡消失——依情境分組寫進 other_scenarios，讓
    # ④畫面能明說「還有哪些服務路徑沒有畫進來」而不是靜默弄丟。
    other_scenarios = []
    excluded = [n for n in nodes if not V.node_in_primary_scenario(n, journey_data)]
    if journey_data and excluded:
        scenario_by_id = {s.get("id"): s for s in journey_data.get("scenarios") or [] if isinstance(s, dict)}
        grouped: dict[str, list] = {}
        for n in excluded:
            for ref in n.get("scenario_refs") or []:
                if ref != primary:
                    grouped.setdefault(ref, []).append(n)
        for scenario_id, group_nodes in grouped.items():
            scenario = scenario_by_id.get(scenario_id, {})
            other_scenarios.append({
                "scenario_id": scenario_id,
                "label": scenario.get("label", scenario_id),
                "customer_goal": scenario.get("customer_goal", ""),
                "nodes": group_nodes,
            })

    result = {"schema_version": 2, "meta": {"case_id": case["case_id"]}, "graph": {"nodes": nodes}, "relations": graph.get("relations") or [], "markers": graph.get("markers") or [], "conflicts": conflicts or [], "annotations": annotations, "research_gaps": research_gaps, "cells": cells, "other_scenarios": other_scenarios}
    # to-be-blueprint-generation: the draft keeps which method built it and the
    # Solutions that need a different journey, so readers never guess.
    method = (graph.get("meta") or {}).get("blueprint_method")
    if method:
        result["meta"]["blueprint_method"] = method
        result["topology_flags"] = [dict(flag) for flag in graph.get("topology_flags") or [] if isinstance(flag, dict)]
        if graph.get("external_references"):
            result["external_references"] = [dict(ref) for ref in graph["external_references"] if isinstance(ref, dict)]
    if hypotheses is not None:
        result["expectations"] = expectations
    if journey_data:
        result["journey"] = journey_data
    return result
