"""pre-blueprint-implementation-research：建立 To-be 藍圖之前的落地研究。

研究者採用解方之後，可以選擇先查「別人怎麼讓這類做法落地」，再建立 SBP。
這支不經過現況藍圖的基準與問題推導（`as_is_research.execute_operation`），
只沿用研究的基礎設施：隱私投影、研究 session、來源政策與引文核對、來源快照、
逐條審查的契約。

- 問題：每個已採用的解方一則，程式固定產生，不呼叫模型，也不宣稱找到缺口。
- 作業：snapshot（已採用解方、勾選的問題、來源政策版本）的 digest 就是作業
  id；snapshot 跟現在不同的作業就是過期——保留、不擋建立、不進輸入。
- 關卡：開始了的研究要審查完或明確略過，才能建立 SBP（`gate`）。
- 只有研究者「採用」的 claim 會進 To-be 生成（`accepted_references`），
  而且永遠是設計參考，不是本案證據、也不是錨點。
"""
from __future__ import annotations

import functools
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

import validate_sbp as V

from . import lifecycle
from . import research_catalog_review as CR
from . import research_policy
from . import research_privacy
from . import research_review_record

DIRNAME = ".implementation_research"
SELECTION_FILENAME = "selection.yaml"
SKIP_FILENAME = "skip.json"
OPERATION_FILENAME = "operation.json"
CATALOG_FILENAME = "catalog.yaml"
TRACE_FILENAME = "policy_trace.json"
REVIEW_FILENAME = "implementation_research_review.yaml"
SCHEMA_VERSION = 1
RESEARCH_MODEL = "gpt-5"

QUESTION_TEMPLATE = "其他服務要讓「{text}」這類做法落地，用了哪些做法、需要哪些條件、遇過哪些限制？"
# built-in-implementation-research（研究者 2026-10-15）：研究依 CJM、Persona、HMW 自主進行。
# 問題帶入選定 HMW 與目標 Persona 的名稱、需求、挑戰；Persona 的成員、理由與代表句不送出。
CONTEXT_TEMPLATE = "設計方向：{hmw}\n目標顧客：{persona}\n"

# The gate states. built-in-implementation-research: research runs inside the
# blueprint build and never blocks it, so nothing is blocking; a finished
# operation needs no review.
NOT_STARTED = "not_started"
RUNNING = "running"
FAILED = "failed"
NEEDS_REVIEW = "needs_review"
NO_ADMISSIBLE = "no_admissible"
REVIEWED = "reviewed"
SKIPPED = "skipped"
BLOCKING: frozenset = frozenset()

REASONS = {
    RUNNING: "落地研究進行中，完成後再建立 SBP。",
    FAILED: "落地研究沒有完成：重新研究，或略過這次研究。",
    NEEDS_REVIEW: "落地研究還沒審查完：逐條決定後保存，或略過這次研究。",
}
NO_ADMISSIBLE_MESSAGE = "目前未找到符合來源標準的外部資料"
# external-research-honest-status: the execution method could not search the
# web, so no external research happened -- model knowledge is never stood in
# for a web search.
SEARCH_UNAVAILABLE = "search_unavailable"
INTERRUPTED_MESSAGE = "落地研究中斷了：重新研究，或略過這次研究。"


class ImplementationResearchError(Exception):
    """A request the researcher can correct; the message is researcher-facing."""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _load_yaml(path: Path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, yaml.YAMLError):
        return None


def _load_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    Path(tmp).replace(path)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    _write_atomic(path, json.dumps(value, ensure_ascii=False, indent=2))


# ---- questions and selection (no model call) ----

def adopted_solutions(journey: dict | None) -> list[dict]:
    """The researcher's adopted Solutions, in journey order."""
    result = []
    for step in V._primary_steps(journey):
        if step.get("develop_solution", True) is not True:
            continue
        candidates = {str(c.get("id")): c for c in step.get("solution_candidates") or [] if isinstance(c, dict)}
        for solution_id in step.get("selected_solution_ids") or []:
            candidate = candidates.get(str(solution_id)) or {}
            result.append({"solution_id": str(solution_id), "step_ref": str(step.get("id") or ""),
                           "step_label": str(step.get("label") or ""),
                           "text": str(candidate.get("text") or "").strip()})
    return result


def questions(journey: dict | None, context: Mapping[str, str] | None = None) -> list[dict]:
    """One broad implementation research question per adopted Solution, led by
    the selected HMW and target Persona when the run has them."""
    lead = ""
    if context and (context.get("hmw") or context.get("persona")):
        lead = CONTEXT_TEMPLATE.format(hmw=context.get("hmw") or "（未選定）",
                                       persona=context.get("persona") or "（未確認）")
    return [{"id": f"IQ-{item['solution_id']}", "solution_id": item["solution_id"],
             "step_label": item["step_label"], "solution_text": item["text"],
             "text": lead + QUESTION_TEMPLATE.format(text=item["text"])}
            for item in adopted_solutions(journey) if item["text"]]


def research_context(run_dir: str | Path) -> dict[str, str]:
    """The selected HMW statement and the target Persona's label, need and
    challenge. Persona members, rationale and quote stay in the case."""
    from . import to_be  # local: to_be imports the research stack
    run = Path(run_dir)
    hmw_doc = _load_yaml(run / "hmw.yaml") or {}
    hmw = to_be._resolve_primary_hmw(run, hmw_doc.get("entries") or [] if isinstance(hmw_doc, dict) else []) or {}
    snapshot_doc = _load_yaml(run / "persona_confirmed.yaml") or {}
    persona_doc = snapshot_doc.get("persona_content") if isinstance(snapshot_doc, dict) else None
    personas = (persona_doc or {}).get("personas") if isinstance(persona_doc, dict) else None
    target = personas[0] if isinstance(personas, list) and personas and isinstance(personas[0], dict) else {}
    label, need, challenge = (str(target.get(key) or "").strip() for key in ("label", "need", "challenge"))
    persona = (label + "：" if label and (need or challenge) else label) + need + challenge
    return {"hmw": str(hmw.get("statement") or "").strip(), "persona": persona}


def _research_dir(run_dir: Path) -> Path:
    return Path(run_dir) / DIRNAME


def _journey(run_dir: Path) -> dict:
    journey = _load_yaml(Path(run_dir) / "journey.yaml")
    return journey if isinstance(journey, dict) else {}


def load_selection(run_dir: str | Path) -> list[str]:
    """Ticked question ids that still name a current question, in question order."""
    run = Path(run_dir)
    doc = _load_yaml(_research_dir(run) / SELECTION_FILENAME) or {}
    ticked = {str(i) for i in (doc.get("ticked_question_ids") or [])} if isinstance(doc, dict) else set()
    return [q["id"] for q in questions(_journey(run)) if q["id"] in ticked]


def save_selection(run_dir: str | Path, ticked_ids: list) -> list[str]:
    """Ticking is saved, never a confirmation, and never starts research."""
    run = Path(run_dir)
    known = [q["id"] for q in questions(_journey(run))]
    if not isinstance(ticked_ids, list) or any(str(i) not in known for i in ticked_ids):
        raise ImplementationResearchError("勾選的研究問題不是目前已採用解方的問題，請重新載入。")
    wanted = {str(i) for i in ticked_ids}
    ordered = [i for i in known if i in wanted]
    _write_atomic(_research_dir(run) / SELECTION_FILENAME,
                  yaml.safe_dump({"ticked_question_ids": ordered}, allow_unicode=True, sort_keys=False))
    return ordered


# ---- operation snapshot, digest and state ----

def snapshot(run_dir: str | Path) -> dict:
    """What an operation started now would be bound to."""
    run = Path(run_dir)
    journey = _journey(run)
    case = _load_yaml(run / "case.yaml") or {}
    current = questions(journey, research_context(run))
    ticked = [q["id"] for q in current]
    by_id = {q["id"]: q for q in current}
    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": str(case.get("case_id") or journey.get("case_id") or ""),
        "scenario_id": str(journey.get("primary_scenario") or ""),
        "adopted_solutions": [{k: s[k] for k in ("solution_id", "step_ref", "text")} for s in adopted_solutions(journey)],
        "ticked_question_ids": ticked,
        "questions": [{"id": i, "text": by_id[i]["text"]} for i in ticked],
        "source_policy_version": research_policy.POLICY_VERSION,
    }


def snapshot_digest(value: Mapping[str, Any]) -> str:
    return lifecycle.canonical_content_digest(dict(value))


def _operation_dir(run_dir: Path, digest: str) -> Path:
    return _research_dir(run_dir) / digest


def load_operation(run_dir: str | Path, digest: str) -> dict | None:
    return _load_json(_operation_dir(Path(run_dir), digest) / OPERATION_FILENAME)


def current_operation(run_dir: str | Path) -> dict | None:
    """The operation bound to the run's present Solutions and ticked questions."""
    run = Path(run_dir)
    value = snapshot(run)
    if not value["ticked_question_ids"]:
        return None
    return load_operation(run, snapshot_digest(value))


def stale_operations(run_dir: str | Path) -> list[dict]:
    """Operations kept on disk that are no longer current, newest first."""
    run = Path(run_dir)
    root = _research_dir(run)
    current = current_operation(run)
    current_digest = (current or {}).get("digest")
    found = []
    if root.is_dir():
        for child in root.iterdir():
            operation = _load_json(child / OPERATION_FILENAME) if child.is_dir() else None
            if operation and operation.get("digest") != current_digest:
                found.append(operation)
    return sorted(found, key=lambda op: str(op.get("started_at") or ""), reverse=True)


def load_catalog(run_dir: str | Path, digest: str) -> dict | None:
    catalog = _load_yaml(_operation_dir(Path(run_dir), digest) / CATALOG_FILENAME)
    return catalog if isinstance(catalog, dict) else None


def _review_is_current(run: Path, operation: dict) -> bool:
    catalog = load_catalog(run, operation["digest"])
    if catalog is None:
        return False
    try:
        review = CR.load(run, filename=REVIEW_FILENAME)
    except CR.ReviewError:
        return False
    snap = operation.get("snapshot") or {}
    errors = CR.binding_errors(review, catalog, catalog_request_digest=operation["digest"],
                               case_id=snap.get("case_id"), scenario_id=snap.get("scenario_id"))
    return not errors and not CR.semantic_errors(review, catalog)


def _skipped(run: Path, digest: str) -> bool:
    record = _load_json(_research_dir(run) / SKIP_FILENAME) or {}
    return record.get("operation_digest") == digest


def gate(run_dir: str | Path, *, job_active: bool = True) -> dict:
    """Whether 建立 SBP may proceed (design D1). `job_active` is False when
    the caller knows no implementation research job is running, so an
    operation left `running` by an interrupted process is reported as failed
    rather than blocking forever."""
    run = Path(run_dir)
    operation = current_operation(run)
    if operation is None:
        return {"state": NOT_STARTED, "reason": None, "operation_digest": None}
    digest = operation["digest"]
    state = operation.get("state")
    if _skipped(run, digest):
        result = SKIPPED
    elif state == RUNNING:
        result = RUNNING if job_active else FAILED
    elif state == FAILED:
        result = FAILED
    elif state == NO_ADMISSIBLE:
        result = NO_ADMISSIBLE
    elif state == "complete":
        result = REVIEWED
    else:
        result = FAILED
    reason = REASONS.get(result)
    if result == FAILED and state == RUNNING:
        reason = INTERRUPTED_MESSAGE
    return {"state": result, "reason": reason, "operation_digest": digest}


def skip(run_dir: str | Path) -> dict:
    """The researcher skips the current operation; applies to that one only."""
    run = Path(run_dir)
    operation = current_operation(run)
    if operation is None:
        raise ImplementationResearchError("目前沒有進行過的落地研究可以略過。")
    _write_json(_research_dir(run) / SKIP_FILENAME,
                {"operation_digest": operation["digest"], "skipped_at": _now()})
    return gate(run, job_active=False)


def record_review(run_dir: str | Path, decisions: list, *, reviewed_at: str | None = None) -> dict:
    """Persist the researcher's decision on every claim of the current
    operation. Raises `CR.ReviewError` and writes nothing when invalid."""
    run = Path(run_dir)
    operation = current_operation(run)
    if operation is None or operation.get("state") != "complete":
        raise ImplementationResearchError("目前沒有可以審查的落地研究結果。")
    catalog = load_catalog(run, operation["digest"])
    if catalog is None:
        raise ImplementationResearchError("目前沒有可以審查的落地研究結果。")
    snap = operation.get("snapshot") or {}
    doc = research_review_record.record_decisions(
        run, catalog,
        catalog_request_digest=operation["digest"],
        catalog_path=f"{DIRNAME}/{operation['digest']}/{CATALOG_FILENAME}",
        case_id=snap.get("case_id"), scenario_id=snap.get("scenario_id"),
        decisions=decisions, reviewed_at=reviewed_at or _now(), filename=REVIEW_FILENAME)
    # The researcher's latest choice wins: reviewing after skipping un-skips.
    if _skipped(run, operation["digest"]):
        (_research_dir(run) / SKIP_FILENAME).unlink()
    return doc


def saved_decisions(run_dir: str | Path, digest: str) -> dict[str, dict]:
    """The saved decision per claim when the review file is bound to `digest`."""
    try:
        review = CR.load(Path(run_dir), filename=REVIEW_FILENAME)
    except CR.ReviewError:
        return {}
    if (review.get("reviewed_catalog") or {}).get("request_digest") != digest:
        return {}
    return {str(d.get("claim_ref")): {"decision": d.get("decision"), "rationale": d.get("researcher_rationale")}
            for d in review.get("claim_decisions") or [] if isinstance(d, dict)}


# ---- running one operation ----

def begin(run_dir: str | Path) -> dict:
    """Write the `running` operation for the present snapshot. The caller has
    already checked that research may start (questions ticked, no job)."""
    run = Path(run_dir)
    value = snapshot(run)
    if not value["ticked_question_ids"]:
        raise ImplementationResearchError("請先勾選至少一個要研究的問題。")
    digest = snapshot_digest(value)
    operation = {"schema_version": SCHEMA_VERSION, "digest": digest, "snapshot": value,
                 "state": RUNNING, "started_at": _now(), "finished_at": None, "error": None,
                 "counts": {"sources": 0, "claims": 0}}
    errors = V.structural_errors(operation, V.load_schema("implementation-research-operation"))
    if errors:
        raise ImplementationResearchError("落地研究的作業紀錄不完整：" + "；".join(errors))
    _write_json(_operation_dir(run, digest) / OPERATION_FILENAME, operation)
    return operation


def _finish(run: Path, operation: dict, **fields) -> dict:
    operation = {**operation, **fields, "finished_at": _now()}
    _write_json(_operation_dir(run, operation["digest"]) / OPERATION_FILENAME, operation)
    return operation


def _evidence_spans(run: Path) -> list[str]:
    """Participant evidence never leaves the case: the projection and the
    research session both refuse a payload that repeats it."""
    evidence = _load_yaml(run / "evidence.yaml")
    return [str(item.get("text")) for item in evidence or [] if isinstance(item, dict) and isinstance(item.get("text"), str)]


def _projection(run: Path, operation: dict) -> tuple[dict, dict, list[str]]:
    case = _load_yaml(run / "case.yaml") or {}
    known_values = research_privacy.case_value_replacements(case)
    spans = _evidence_spans(run)
    projection = research_privacy.build_research_projection(
        {"issues": []},
        service_context=str(case.get("service_type") or "service"),
        improvement_goal=str(case.get("improvement_goal") or ""),
        known_values=known_values,
        evidence_spans=spans,
        sbp_questions=[{"question": q["text"]} for q in operation["snapshot"]["questions"]],
        mode="implementation",
    )
    return projection, known_values, spans


def run(run_dir: str | Path, *, run_research: Callable | None = None,
        build_catalog: Callable | None = None, model: str = RESEARCH_MODEL) -> dict:
    """Run the current operation once: projection, research session, catalog.
    Finishes as complete, no_admissible, or failed; never raises for a
    research failure (the failure is recorded on the operation)."""
    from . import research_agent, source_snapshot  # heavy imports only when research runs

    run_path = Path(run_dir)
    operation = current_operation(run_path)
    if operation is None or operation.get("state") != RUNNING:
        operation = begin(run_path)
    digest = operation["digest"]
    try:
        projection, known_values, spans = _projection(run_path, operation)
    except Exception as exc:
        return _finish(run_path, operation, state=FAILED,
                       error={"code": "privacy_boundary_rejected", "detail": str(exc)})
    if run_research is None:
        run_research = functools.partial(
            research_agent.run_research_agent,
            source_snapshots=source_snapshot.store_for(_operation_dir(run_path, digest), digest))
    try:
        agent_result, tool_audit = run_research(projection, known_values=known_values, evidence_spans=spans,
                                                model=model)
    except Exception as exc:
        # record-research-failure-trace: keep what the session was refused and why.
        causes = research_agent.failure_causes(exc)
        _write_json(_operation_dir(run_path, digest) / TRACE_FILENAME,
                    {"tool_audit": list(getattr(exc, "audit", []) or []), "failure_causes": causes})
        return _finish(run_path, operation, state=FAILED,
                       error={"code": getattr(exc, "code", "research_failed"), "detail": causes[-1]["message"]})
    if not isinstance(agent_result, Mapping):
        return _finish(run_path, operation, state=FAILED, error={"code": "citation_support_rejected", "detail": ""})
    if "unavailable" in ((agent_result.get("runner_usage") or {}).get("search") or []):
        _write_json(_operation_dir(run_path, digest) / TRACE_FILENAME, {"tool_audit": list(tool_audit or [])})
        return _finish(run_path, operation, state=FAILED, error={"code": SEARCH_UNAVAILABLE, "detail": ""})
    snap = operation["snapshot"]
    try:
        catalog, trace = (build_catalog or research_policy.build_research_catalog)(
            case_id=snap["case_id"], scenario_id=snap["scenario_id"],
            source_candidates=agent_result.get("sources") or [],
            claim_candidates=agent_result.get("claims") or [],
            query_candidates=agent_result.get("query_audit") or [],
            accessed_at=_now()[:10],
            fetched_bodies=agent_result.get("fetched_bodies") or {},
        )
    except research_policy.ResearchPolicyError as exc:
        _write_json(_operation_dir(run_path, digest) / TRACE_FILENAME,
                    {"source_decisions": exc.decisions, "admissibility": exc.diagnostics,
                     "tool_audit": list(tool_audit or []), "failure_causes": research_agent.failure_causes(exc)})
        if exc.code == "no_admissible_research":
            return _finish(run_path, operation, state=NO_ADMISSIBLE, error=None)
        return _finish(run_path, operation, state=FAILED, error={"code": exc.code, "detail": str(exc)})
    _write_atomic(_operation_dir(run_path, digest) / CATALOG_FILENAME,
                  yaml.safe_dump(catalog, allow_unicode=True, sort_keys=False))
    _write_json(_operation_dir(run_path, digest) / TRACE_FILENAME, {**trace, "tool_audit": list(tool_audit or [])})
    return _finish(run_path, operation, state="complete", error=None,
                   counts={"sources": len(catalog.get("sources") or []), "claims": len(catalog.get("claims") or [])})


def record_search_unavailable(run_dir: str | Path) -> dict | None:
    """The blueprint build's research step when the execution method cannot
    search: keep a finished operation for the current snapshot (it is reused),
    otherwise record a failed operation without calling any model. None when
    the run has no adopted Solution to research."""
    run = Path(run_dir)
    if not snapshot(run)["ticked_question_ids"]:
        return None
    finished = current_operation(run)
    if finished and finished.get("state") in ("complete", NO_ADMISSIBLE):
        return finished
    return _finish(run, begin(run), state=FAILED, error={"code": SEARCH_UNAVAILABLE, "detail": ""})


def research_status(run_dir: str | Path, *, job_active: bool = False) -> dict:
    """What external research actually happened for the current snapshot, read
    from its operation record: not_applicable, not_run, running, unsupported,
    failed, no_admissible or complete."""
    run = Path(run_dir)
    if not snapshot(run)["ticked_question_ids"]:
        return {"state": "not_applicable"}
    operation = current_operation(run)
    if operation is None:
        return {"state": "not_run"}
    state = operation.get("state")
    if state == RUNNING:
        return {"state": "running" if job_active else "failed"}
    if state == FAILED:
        code = (operation.get("error") or {}).get("code")
        return {"state": "unsupported" if code == SEARCH_UNAVAILABLE else "failed"}
    if state == NO_ADMISSIBLE:
        return {"state": "no_admissible"}
    if state == "complete":
        return {"state": "complete", "claims": int((operation.get("counts") or {}).get("claims") or 0)}
    return {"state": "failed"}


# ---- what enters To-be generation ----

def _source_view(catalog: dict, link: dict) -> dict:
    source = next((s for s in catalog.get("sources") or [] if s.get("id") == link.get("source_ref")), {})
    return {"title": source.get("title"), "publisher": source.get("authors_or_publisher"), "url": source.get("url") or source.get("doi")}


def claim_questions(operation: Mapping[str, Any], claim: Mapping[str, Any]) -> list[str]:
    """bind-external-reference-provenance: the questions a claim answers, from
    the operation itself. One researched question binds every claim; with
    several, the session's aliases (`Q-001` is the first question sent) map
    back through the snapshot, unknown aliases are dropped, and no alias means
    unknown -- never a guess from the text."""
    questions = [q["id"] for q in (operation.get("snapshot") or {}).get("questions") or []]
    if len(questions) == 1:
        return questions
    by_alias = {f"Q-{index:03d}": qid for index, qid in enumerate(questions, 1)}
    named = {by_alias[ref] for ref in claim.get("question_refs") or [] if ref in by_alias}
    return [qid for qid in questions if qid in named]


def _related_solutions(operation: Mapping[str, Any], question_ids: list[str]) -> list[str]:
    adopted = {s["solution_id"] for s in (operation.get("snapshot") or {}).get("adopted_solutions") or []}
    return [qid[len("IQ-"):] for qid in question_ids if qid.startswith("IQ-") and qid[len("IQ-"):] in adopted]


def accepted_references(run_dir: str | Path, *, job_active: bool = False) -> list[dict]:
    """Every admitted claim of the current finished operation; empty otherwise
    (built-in-implementation-research: no per-claim review)."""
    run = Path(run_dir)
    status = gate(run, job_active=job_active)
    if status["state"] != REVIEWED:
        return []
    operation = load_operation(run, status["operation_digest"]) or {}
    catalog = load_catalog(run, status["operation_digest"]) or {}
    references = []
    for claim in catalog.get("claims") or []:
        links = claim.get("source_links") or []
        question_ids = claim_questions(operation, claim)
        references.append({
            "id": f"XR-{len(references) + 1}",
            "text": claim.get("text"),
            **({"summary": claim["summary"]} if claim.get("summary") else {}),
            "applicability_context": claim.get("applicability_context"),
            "uncertainty": claim.get("uncertainty"),
            "supports": [_source_view(catalog, l) for l in links if l.get("relationship") == "supports"],
            "limits": [_source_view(catalog, l) for l in links if l.get("relationship") == "limits"],
            "research_question_refs": question_ids,
            "related_solution_refs": _related_solutions(operation, question_ids),
        })
    return references
