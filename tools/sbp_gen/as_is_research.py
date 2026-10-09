"""Server-owned orchestration for confirmed As-is issue and research work.

The operation record is deliberately noncanonical.  Canonical content reaches
the lifecycle registry only through :func:`lifecycle.publish_batch` after all
policy and content checks have completed.
"""
from __future__ import annotations

import hashlib
import asyncio
import functools
import inspect
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml
import validate_sbp as V
import storage_locations

from . import improvement_goal_review, lifecycle


OPERATION_SCHEMA_VERSION = 1
OPERATION_KIND = "as_is_research"
POLICY_VERSIONS = {
    "issue_prompt": "issue-v2",
    "issue_grounding": "issue-grounding-v2",
    "research_projection": "research-projection-v2",
    "research_policy": "research-policy-v1",
}
DEFAULT_MODEL_POLICY = {
    "issues_provider": "gemini",
    "issues_fallback_provider": "anthropic",
    "issue_grounding_provider": "gemini",
    "issue_grounding_fallback_provider": "anthropic",
    # 外部調研已改用 OpenAI（move-external-research-to-openai）；這個值會直接
    # 傳進 research_agent.run_research_agent()，留著 Claude 型號會覆蓋掉那邊的預設。
    "research_model": "gpt-5",
    "citation_verifier_provider": "anthropic",
}
ACTIVE_STATES = {"queued", "running"}
# 辨識問題這一步的總嘗試次數（含第一次）。跟 structure() 一樣是「產生→驗證→把
# 錯誤餵回去」，只是這裡的驗證要等 grounding 與 normalize 都跑完才判得出來。
ISSUE_DERIVATION_ATTEMPTS = 3


def _sum_usage(total: Mapping[str, Any] | None, attempt: Mapping[str, Any] | None) -> dict:
    """Accumulate tokens/seconds across retried attempts.

    Counting only the successful attempt would systematically under-report what
    a run actually cost — the rejected attempts really did spend tokens.
    """
    merged = dict(total or {})
    for key, value in (attempt or {}).items():
        if isinstance(value, (int, float)) and isinstance(merged.get(key), (int, float)):
            merged[key] = merged[key] + value
        else:
            merged[key] = value
    return merged
TERMINAL_PHASES = {"research_ready", "no_grounded_issues"}
PHASES = (
    "deriving_issues",
    "checking_privacy",
    "researching",
    "validating",
    "publishing",
    "research_ready",
    "no_grounded_issues",
)


# 「找不到可供調研的已確認現況藍圖」原本是十二個不同失敗點共用的一句話：還沒按
# 確認、證據檔沒落地、版本紀錄對不上、內容摘要不符——全都講同一句，於是這句話等
# 於沒說。研究者拿到它不知道下一步要做什麼，開發者也看不出是哪一關。
# 拆開的原則是「研究者接下來要做的事不同，就該是不同的訊息」；同一個動作能解的
# 就留在同一個代碼裡。
ERROR_MESSAGES: dict[str, tuple[str, bool]] = {
    "missing_confirmed_baseline": ("找不到可供調研的已確認現況藍圖。", False),
    "as_is_not_confirmed_yet": ("這份現況藍圖還沒有被確認為基線。", False),
    "baseline_evidence_file_missing": ("這個版本的證據沒有存在版本資料夾裡，無法核對。", False),
    "baseline_record_unreadable": ("這個版本的產出紀錄讀不出來。", False),
    "baseline_record_incomplete": ("這個版本的產出紀錄缺少現況藍圖或證據的版本編號。", False),
    "baseline_content_unreadable": ("這個版本的現況藍圖或證據內容讀不出來。", False),
    "missing_case_file": ("找不到這個案子的 case.yaml，無法繼續。", False),
    "missing_improvement_goal": ("請先執行 sbp-gen improvement-goal-review --confirm-file 確認改善目標，再開始外部調研。", False),
    "baseline_not_current": ("現況藍圖已更新，請先重新確認。", False),
    "baseline_changed": ("調研期間現況藍圖已更新，請重新確認後再試。", False),
    "issue_generation_failed": ("現況問題分析未完成，請稍後重試。", True),
    "issue_grounding_rejected": ("目前資料不足以形成可追溯的現況問題。", False),
    "privacy_boundary_rejected": ("資料未通過外部調研的隱私檢查。", False),
    "research_configuration_missing": ("外部調研尚未完成系統設定。", False),
    "tool_policy_rejected": ("外部調研因安全限制而停止。", False),
    "source_unavailable": ("外部資料來源都取不到內容，這次調研沒有完成。", True),
    "research_budget_exhausted": ("外部調研已達本次執行上限。", True),
    "no_admissible_research": ("未找到符合來源標準且可支持判斷的資料。", True),
    "citation_support_rejected": ("外部資料未能支持研究判斷。", True),
    "research_timed_out": ("外部調研沒有在時限內回應，這次沒有完成。", True),
    "research_service_error": ("外部服務沒有正常回應，這次調研沒有完成。", True),
    "content_validation_failed": ("調研結果未通過內容驗證。", True),
    "publication_failed": ("調研結果無法安全儲存，既有版本未受影響。", True),
}


# 哪些失敗是「這台機器自己的前提沒滿足」，跟外部服務無關。畫面把每一則失敗都寫成
# 「外部服務回覆」，於是「你還沒按確認」被說成外面出事——研究者會去查網路和金鑰，
# 查的是錯的東西。分類放在這裡而不是畫面，因為知道原因的是這裡。
LOCAL_PRECONDITION_CODES = frozenset({
    "missing_confirmed_baseline",
    "as_is_not_confirmed_yet",
    "baseline_evidence_file_missing",
    "baseline_record_unreadable",
    "baseline_record_incomplete",
    "baseline_content_unreadable",
    "missing_case_file",
    "missing_improvement_goal",
    "baseline_not_current",
    "baseline_changed",
    "research_configuration_missing",
})


class ResearchOperationError(RuntimeError):
    """A stable, researcher-safe operation failure."""

    def __init__(self, code: str, *, detail: object | None = None):
        message, retryable = ERROR_MESSAGES.get(
            code, ("外部調研未完成。", False)
        )
        super().__init__(message)
        self.code = code
        self.user_message = message
        self.retryable = retryable
        self.source = "local" if code in LOCAL_PRECONDITION_CODES else "external"
        self.detail_digest = _digest_text(repr(detail)) if detail is not None else None
        # 只留「是哪一種失敗」，不留內容。原本只有雜湊，等於什麼都查不到——
        # measure-henglong 那次逾時，要靠人工重打一次 API 才知道原因。類別名稱
        # 與 HTTP 狀態碼不含案例資料，留著才有辦法在不重跑的情況下判斷。
        self.detail_kind = None
        if detail is not None:
            kind = type(detail).__name__
            status = getattr(detail, "status", None)
            self.detail_kind = f"{kind}({status})" if isinstance(status, int) else kind


class ResearchOperationConflict(ResearchOperationError):
    def __init__(self, *, detail: object | None = None):
        super().__init__("baseline_changed", detail=detail)


@dataclass(frozen=True)
class Baseline:
    run_dir: Path
    registry_path: Path
    case_id: str
    scenario_id: str
    evidence_version_id: str
    evidence_digest: str
    as_is_version_id: str
    as_is_digest: str
    reviewed_fingerprint: str
    improvement_goal: str
    improvement_goal_digest: str
    evidence: list[dict[str, Any]]
    graph: dict[str, Any]


def _journey_has_solution_candidates(run_dir: str | Path) -> bool:
    path = Path(run_dir) / "journey.yaml"
    journey = _read_yaml(path) if path.is_file() else {}
    if not isinstance(journey, dict):
        return False
    scenario = next((item for item in journey.get("scenarios") or []
                     if isinstance(item, dict) and item.get("id") == journey.get("primary_scenario")), {})
    return any("solution_candidates" in step for step in scenario.get("steps") or [] if isinstance(step, dict))


def sbp_evaluation_questions(run_dir: str | Path, graph: Mapping[str, Any] | None = None) -> list[dict[str, str]]:
    """Read the current CJM/SBP and state what external research should test.

    Questions are local research framing. They do not convert selected design
    options or inferred service activity into participant evidence.
    """
    run_path = Path(run_dir)
    journey_path = run_path / "journey.yaml"
    journey = _read_yaml(journey_path) if journey_path.is_file() else {}
    if not isinstance(journey, dict):
        return []
    scenario = next((item for item in journey.get("scenarios") or []
                     if isinstance(item, dict) and item.get("id") == journey.get("primary_scenario")), {})
    if not any("solution_candidates" in step for step in scenario.get("steps") or [] if isinstance(step, dict)):
        return []
    channel_names = {
        "people": "人員", "physical_environment": "實體環境",
        "digital_tools": "數位工具", "digital_environments": "數位環境",
    }
    questions: list[dict[str, str]] = []
    for step in scenario.get("steps") or []:
        selected = set(step.get("selected_solution_ids") or [])
        for candidate in step.get("solution_candidates") or []:
            if not isinstance(candidate, dict) or candidate.get("id") not in selected:
                continue
            channels = [channel_names[key] for key in (candidate.get("delivery") or {}) if key in channel_names]
            delivery = "、".join(channels) if channels else "服務接觸"
            questions.append({
                "kind": "selected_solution", "solution_id": str(candidate["id"]),
                "step_id": str(step.get("id") or ""),
                "question": f"「{candidate.get('text')}」透過{delivery}交付是否可行？需要哪些資源、維護與後台支援？",
            })
    current_graph = graph if graph is not None else (_read_yaml(run_path / "service_graph.yaml")
                                               if (run_path / "service_graph.yaml").is_file() else {})
    for node in (current_graph or {}).get("nodes") or []:
        if not isinstance(node, dict) or node.get("layer") not in {"後台行動", "支援流程"}:
            continue
        if not any(claim.get("source") == "inference" for claim in node.get("claims") or [] if isinstance(claim, dict)):
            continue
        questions.append({
            "kind": "service_assumption", "node_id": str(node.get("id") or ""),
            "question": f"服務藍圖推定的「{node.get('label')}」需要哪些實際作業、資源與驗證？",
        })
    return questions


@dataclass
class ResearchDependencies:
    """Injectable execution boundaries used by offline and production runs."""

    derive_issues: Callable[..., Any] | None = None
    ground_issues: Callable[..., Any] | None = None
    build_projection: Callable[..., Any] | None = None
    run_research: Callable[..., Any] | None = None
    build_catalog: Callable[..., Any] | None = None
    publish: Callable[..., Any] = lifecycle.publish_batch
    now: Callable[[], str] = field(
        default=lambda: datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_json(path: Path, code: str = "baseline_record_unreadable") -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchOperationError(code, detail=exc) from exc
    if not isinstance(value, dict):
        raise ResearchOperationError(code, detail="run state is not an object")
    return value


def _read_yaml(path: Path, code: str = "baseline_content_unreadable") -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ResearchOperationError(code, detail=exc) from exc


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            json.dump(value, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def run_artifact_fingerprint(run_dir: Path) -> str | None:
    """Mirror the review adapter's researcher-visible artifact fingerprint."""
    draft_path = run_dir / "sbp_draft.yaml"
    if not draft_path.is_file():
        return None
    stat = draft_path.stat()
    parts = [f"{stat.st_mtime_ns:x}-{stat.st_size:x}"]
    briefing_path = run_dir / "briefing.yaml"
    if briefing_path.is_file():
        b_stat = briefing_path.stat()
        parts.append(f"{b_stat.st_mtime_ns:x}-{b_stat.st_size:x}")
    return "-".join(parts)


def _version_map(registry: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        record["version_id"]: record
        for record in registry.get("versions", [])
        if isinstance(record, dict) and isinstance(record.get("version_id"), str)
    }


def _baseline_evidence(run_path: Path, run_state: Mapping[str, Any]) -> Any:
    """The evidence this run was actually built from.

    Normally `<run>/evidence.yaml`. Runs generated with `--from-evidence`
    before that path wrote the file have no local copy, so fall back to the
    source they recorded in `run.json`. The digest check below is what makes
    the fallback safe: a source file edited since the run was generated no
    longer matches the published version and is rejected, not silently used.
    """
    local = run_path / "evidence.yaml"
    if local.exists():
        return _read_yaml(local)
    recorded = run_state.get("from_evidence")
    if not isinstance(recorded, str) or not recorded.strip():
        raise ResearchOperationError(
            "baseline_evidence_file_missing", detail="run.json 也沒有記錄證據來源")
    source = Path(recorded)
    if not source.is_absolute():
        source = (run_path.parent.parent / source).resolve()
    if not source.exists():
        raise ResearchOperationError(
            "baseline_evidence_file_missing", detail="記錄的證據來源已經不在原處")
    return _read_yaml(source)


def resolve_baseline(
    run_dir: str | Path,
    *,
    registry_path: str | Path | None = None,
    expected_as_is_version: str | None = None,
    require_reviewed: bool = True,
) -> Baseline:
    """Resolve and verify the exact canonical inputs before model work."""
    run_path = Path(run_dir).resolve()
    run_state = _read_json(run_path / "run.json")
    declared_registry = registry_path or run_state.get("artifact_registry")
    versions = run_state.get("artifact_versions")
    if not declared_registry or not isinstance(versions, dict):
        raise ResearchOperationError("baseline_record_incomplete", detail="run.json 沒有版本紀錄位置或版本對照")
    if registry_path is not None:
        registry_file = Path(registry_path).expanduser().resolve()
    else:
        try:
            registry_file = storage_locations.resolve_registry_pointer(
                declared_registry, run_path, storage_locations.runs_root_for_run(run_path)
            )
        except storage_locations.RegistryPointerError as exc:
            raise ResearchOperationError(
                "baseline_record_incomplete", detail=str(exc)
            ) from exc
    registry = _read_yaml(registry_file)
    errors = lifecycle.lifecycle_errors(registry)
    if errors:
        raise ResearchOperationError("baseline_not_current", detail=errors)
    states = lifecycle.resolve_lifecycle(registry)
    records = _version_map(registry)
    as_is_id = expected_as_is_version or versions.get("as_is_graph")
    evidence_id = versions.get("evidence")
    if not isinstance(as_is_id, str) or not isinstance(evidence_id, str):
        raise ResearchOperationError("baseline_record_incomplete", detail="缺少現況藍圖或證據的版本編號")
    graph_record = records.get(as_is_id)
    evidence_record = records.get(evidence_id)
    if not graph_record or not evidence_record:
        raise ResearchOperationError("baseline_record_incomplete", detail="版本編號在紀錄裡找不到對應")
    if graph_record.get("artifact_type") != "as_is_graph" or evidence_record.get("artifact_type") != "evidence":
        raise ResearchOperationError("baseline_record_incomplete", detail="版本編號指到的產出類型不對")
    graph_evidence_refs = {
        dependency.get("version_ref")
        for dependency in graph_record.get("dependencies", [])
        if isinstance(dependency, dict) and dependency.get("role") == "evidence"
    }
    if graph_evidence_refs != {evidence_id}:
        raise ResearchOperationError("missing_confirmed_baseline", detail="graph evidence dependency mismatch")
    if states.get(as_is_id) != "current" or states.get(evidence_id) != "current":
        raise ResearchOperationError("baseline_not_current")

    current_fingerprint = run_artifact_fingerprint(run_path)
    reviewed_fingerprint = run_state.get("reviewed_artifact_fingerprint")
    if require_reviewed and (
        run_state.get("review_state") != "reviewed"
        or not isinstance(reviewed_fingerprint, str)
        or reviewed_fingerprint != current_fingerprint
    ):
        raise ResearchOperationError("as_is_not_confirmed_yet")
    if not isinstance(current_fingerprint, str):
        raise ResearchOperationError("baseline_record_incomplete", detail="無法計算這個版本的內容識別")

    evidence = _baseline_evidence(run_path, run_state)
    graph = _read_yaml(run_path / "service_graph.yaml")
    if not isinstance(evidence, list) or not isinstance(graph, dict):
        raise ResearchOperationError("baseline_content_unreadable", detail="證據或現況藍圖不是預期的結構")
    evidence_digest = lifecycle.canonical_content_digest(evidence)
    graph_digest = lifecycle.canonical_content_digest(graph)
    if evidence_digest != evidence_record.get("content_digest") or graph_digest != graph_record.get("content_digest"):
        raise ResearchOperationError("baseline_not_current", detail="content digest mismatch")
    case_id = graph_record.get("case_id")
    scenario_id = graph_record.get("scenario_id")
    if not isinstance(case_id, str) or not isinstance(scenario_id, str):
        raise ResearchOperationError("missing_confirmed_baseline")
    case = _read_yaml(run_path / "case.yaml", code="missing_case_file")
    if not isinstance(case, dict):
        raise ResearchOperationError("missing_case_file")
    # 決定一：這個閘門本身留著，只是它擋的值從站①（case.yaml 的研究前直覺）
    # 換成站⑩（研究者看過 HMW 之後確認/改寫的值，improvement_goal_review.yaml）。
    # 下面每一個讀 baseline.improvement_goal／.improvement_goal_digest 的地方
    # （build_research_projection、assert_baseline_current 等）都不用改——
    # 它們本來就只認 Baseline 的欄位，不管欄位的值來自哪裡。
    journey_path = run_path / "journey.yaml"
    journey = _read_yaml(journey_path) if journey_path.is_file() else {}
    scenario = next((item for item in (journey or {}).get("scenarios") or []
                     if isinstance(item, dict) and item.get("id") == journey.get("primary_scenario")), {})
    fresh_sbp = any("solution_candidates" in step for step in scenario.get("steps") or [] if isinstance(step, dict))
    if fresh_sbp:
        # The field name belongs to the legacy research-operation record. On
        # the new path its value is an internal research purpose derived from
        # the confirmed SBP, never a second researcher goal decision.
        improvement_goal = "Evaluate the current service blueprint, selected solutions, channel feasibility, resources and unsupported service assumptions."
    else:
        improvement_goal = improvement_goal_review.read_operative_goal(run_path)
        if not improvement_goal:
            raise ResearchOperationError("missing_improvement_goal")
    improvement_goal_digest = _digest_text(improvement_goal)
    return Baseline(
        run_dir=run_path,
        registry_path=registry_file,
        case_id=case_id,
        scenario_id=scenario_id,
        evidence_version_id=evidence_id,
        evidence_digest=evidence_digest,
        as_is_version_id=as_is_id,
        as_is_digest=graph_digest,
        reviewed_fingerprint=current_fingerprint,
        improvement_goal=improvement_goal,
        improvement_goal_digest=improvement_goal_digest,
        evidence=evidence,
        graph=graph,
    )


def normalize_research_question(question: object) -> str:
    """The question this round of research is meant to answer.

    Separate from the case's improvement goal on purpose. The case goal is a
    boundary condition generation reads, so it belongs to the baseline and
    changing it invalidates a confirmed blueprint. This is a parameter of one
    research request: the researcher writes it after seeing the real present
    state, and changing it should re-run research without touching a blueprint
    they already confirmed.
    """
    return " ".join(str(question or "").split())


def request_digest(
    baseline: Baseline,
    *,
    policy_versions: Mapping[str, str] | None = None,
    model_policy: Mapping[str, Any] | None = None,
    research_question: str | None = None,
) -> str:
    identity = {
        "evidence_version_id": baseline.evidence_version_id,
        "evidence_digest": baseline.evidence_digest,
        "as_is_version_id": baseline.as_is_version_id,
        "as_is_digest": baseline.as_is_digest,
        "improvement_goal_digest": baseline.improvement_goal_digest,
        # 這一輪要查什麼，屬於請求不屬於基線：換題目要重跑調研，但不該讓
        # 已經確認的現況藍圖失效。
        "research_question": normalize_research_question(research_question),
        "policy_versions": dict(policy_versions or POLICY_VERSIONS),
        "model_policy": dict(model_policy or {}),
    }
    return _digest_text(_canonical_json(identity))


def operations_dir(run_dir: str | Path) -> Path:
    return Path(run_dir) / ".as_is_research"


def operation_path(run_dir: str | Path, digest: str) -> Path:
    safe = digest.removeprefix("sha256:")
    return operations_dir(run_dir) / safe / "operation.json"


def current_operation_path(run_dir: str | Path) -> Path:
    return operations_dir(run_dir) / "current.json"


def load_current_operation(run_dir: str | Path) -> dict[str, Any] | None:
    pointer = current_operation_path(run_dir)
    if not pointer.is_file():
        return None
    try:
        identity = json.loads(pointer.read_text(encoding="utf-8"))
        digest = identity["request_digest"]
        value = json.loads(operation_path(run_dir, digest).read_text(encoding="utf-8"))
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _new_operation(
    baseline: Baseline,
    digest: str,
    *,
    policy_versions: Mapping[str, str],
    model_policy: Mapping[str, Any],
    now: str,
) -> dict[str, Any]:
    return {
        "schema_version": OPERATION_SCHEMA_VERSION,
        "kind": OPERATION_KIND,
        "state": "queued",
        "phase": "deriving_issues",
        "retryable": False,
        "request_digest": digest,
        "baseline": {
            "case_id": baseline.case_id,
            "scenario_id": baseline.scenario_id,
            "evidence_version_id": baseline.evidence_version_id,
            "evidence_digest": baseline.evidence_digest,
            "as_is_version_id": baseline.as_is_version_id,
            "as_is_digest": baseline.as_is_digest,
            "reviewed_fingerprint": baseline.reviewed_fingerprint,
            "improvement_goal_digest": baseline.improvement_goal_digest,
        },
        "policy_versions": dict(policy_versions),
        "model_policy": dict(model_policy),
        "attempt": 1,
        "phase_transitions": [
            {"phase": "deriving_issues", "state": "queued", "at": now}
        ],
        "tool_audit": [],
        "source_decisions": [],
        "staged_digests": {},
        "counts": {"issues": 0, "sources": 0, "claims": 0},
        "result": {},
        "error": None,
        "created_at": now,
        "updated_at": now,
    }


def prepare_operation(
    baseline: Baseline,
    *,
    policy_versions: Mapping[str, str] | None = None,
    model_policy: Mapping[str, Any] | None = None,
    research_question: str | None = None,
    now: str | None = None,
) -> tuple[dict[str, Any], bool]:
    policies = dict(policy_versions or POLICY_VERSIONS)
    models = dict(model_policy or {})
    question = normalize_research_question(research_question)
    digest = request_digest(baseline, policy_versions=policies, model_policy=models,
                            research_question=question)
    current = load_current_operation(baseline.run_dir)
    if current and current.get("request_digest") == digest:
        if current.get("state") in ACTIVE_STATES or (
            current.get("state") == "done" and current.get("phase") in TERMINAL_PHASES
        ):
            return current, True
        prior_attempt = int(current.get("attempt") or 0)
        prior_errors = list(current.get("prior_errors") or [])
        if isinstance(current.get("error"), dict):
            prior_errors.append(dict(current["error"]))
    else:
        prior_attempt = 0
        prior_errors = []
    if current and current.get("state") in ACTIVE_STATES:
        raise ResearchOperationConflict(detail="a different operation is active")
    timestamp = now or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    operation = _new_operation(
        baseline,
        digest,
        policy_versions=policies,
        model_policy=models,
        now=timestamp,
    )
    operation["attempt"] = prior_attempt + 1
    if question:
        operation["research_question"] = question
    if prior_errors:
        operation["prior_errors"] = prior_errors
    _write_json_atomic(operation_path(baseline.run_dir, digest), operation)
    _write_json_atomic(current_operation_path(baseline.run_dir), {"request_digest": digest})
    return operation, False


def _admissibility_record(diagnostics: Mapping[str, Any] | None,
                          agent_result: Mapping[str, Any] | None) -> dict[str, Any]:
    """research-admissibility-diagnostics: the per-link record from the
    admissibility check plus where each fetched source's excerpts came from."""
    record = dict(diagnostics or {})
    record.setdefault("links", [])
    record["excerpt_provenance"] = dict((agent_result or {}).get("excerpt_provenance") or {})
    return record


def persist_operation(run_dir: str | Path, operation: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(operation)
    digest = value.get("request_digest")
    if not isinstance(digest, str):
        raise ValueError("operation request_digest is required")
    _write_json_atomic(operation_path(run_dir, digest), value)
    _write_json_atomic(current_operation_path(run_dir), {"request_digest": digest})
    return value


def transition_operation(
    baseline: Baseline,
    operation: Mapping[str, Any],
    phase: str,
    *,
    state: str = "running",
    now: str | None = None,
    **updates: Any,
) -> dict[str, Any]:
    if phase not in PHASES:
        raise ValueError(f"unknown as_is_research phase: {phase}")
    timestamp = now or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    value = dict(operation)
    value.update(updates)
    value["phase"] = phase
    value["state"] = state
    value["updated_at"] = timestamp
    transitions = list(value.get("phase_transitions") or [])
    transitions.append({"phase": phase, "state": state, "at": timestamp})
    value["phase_transitions"] = transitions
    return persist_operation(baseline.run_dir, value)


def fail_operation(
    baseline: Baseline,
    operation: Mapping[str, Any],
    error: ResearchOperationError,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    timestamp = now or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    value = dict(operation)
    value["state"] = "failed"
    value["retryable"] = error.retryable
    value["updated_at"] = timestamp
    value["error"] = {"code": error.code, "user_message": error.user_message, "source": error.source}
    if error.code == "no_admissible_research":
        # Say what actually happened, from this operation's own audit and the
        # recorded reason (research-admissibility-diagnostics): only a quote
        # that was not found is described as not matching.
        fetched = sum(1 for event in value.get("tool_audit") or []
                      if event.get("event") == "terminal" and event.get("outcome") == "success")
        admitted = sum(1 for decision in value.get("source_decisions") or []
                       if decision.get("admission") == "admitted")
        reason = (value.get("admissibility_diagnostics") or {}).get("reason")
        if fetched:
            counted = f"抓回 {fetched} 份來源，其中 {admitted} 份符合來源標準"
            if reason == "no_admitted_sources":
                message = f"抓回 {fetched} 份來源，但沒有一份符合來源標準，所以都不能採用。"
            elif reason == "quote_not_found":
                message = (f"{counted}，但沒有任何一段引文能在抓回的原文中逐字對上，所以都不能採用。"
                           "重新進行調研時，系統會改找全文公開的來源。")
            elif reason == "quote_found_but_not_supporting_claim":
                message = f"{counted}；引文都在原文裡找得到，但核對後沒有一段足以支持調研的說法，所以都不能採用。"
            else:
                message = f"{counted}，但沒有一段引文能採用。"
            value["error"]["user_message"] = message
    if getattr(error, "detail_kind", None):
        value["diagnostic_kind"] = error.detail_kind
    if error.detail_digest:
        value["diagnostic_digest"] = error.detail_digest
    return persist_operation(baseline.run_dir, value)


def assert_baseline_current(baseline: Baseline) -> None:
    try:
        latest = resolve_baseline(
            baseline.run_dir,
            registry_path=baseline.registry_path,
            expected_as_is_version=baseline.as_is_version_id,
            require_reviewed=True,
        )
    except ResearchOperationError as exc:
        raise ResearchOperationError("baseline_changed", detail=exc.code) from exc
    if (
        latest.evidence_version_id != baseline.evidence_version_id
        or latest.evidence_digest != baseline.evidence_digest
        or latest.as_is_digest != baseline.as_is_digest
        or latest.reviewed_fingerprint != baseline.reviewed_fingerprint
        or latest.improvement_goal_digest != baseline.improvement_goal_digest
    ):
        raise ResearchOperationError("baseline_changed")


def project_operation(operation: Mapping[str, Any]) -> dict[str, Any]:
    """Return the closed API projection without internal policy or audit data."""
    return {
        "kind": OPERATION_KIND,
        "state": operation.get("state"),
        "phase": operation.get("phase"),
        "retryable": bool(operation.get("retryable")),
        "request_digest": operation.get("request_digest"),
        "counts": dict(operation.get("counts") or {}),
        "result": dict(operation.get("result") or {}),
        "error": dict(operation["error"]) if isinstance(operation.get("error"), dict) else None,
    }


def _write_yaml_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            yaml.safe_dump(value, temporary, allow_unicode=True, sort_keys=False)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def _issue_errors(content: Any, baseline: Baseline) -> list[str]:
    errors = V.structural_errors(content, V.load_schema("issue-package"))
    if isinstance(content, dict):
        errors += V.issue_package_errors(content, baseline.evidence, baseline.graph)
    return errors


def _research_errors(content: Any) -> list[str]:
    errors = V.structural_errors(content, V.load_schema("research-catalog"))
    if isinstance(content, dict):
        errors += V.research_catalog_errors(content)
    return errors


def publish_canonical_results(
    baseline: Baseline,
    operation: Mapping[str, Any],
    issue_package: Mapping[str, Any],
    research_catalog: Mapping[str, Any] | None,
    *,
    publisher: Callable[..., Any] = lifecycle.publish_batch,
    now: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Stage, validate, re-check the baseline, then publish exactly one batch."""
    timestamp = now or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    stage_dir = operation_path(baseline.run_dir, str(operation["request_digest"])).parent / "staged"
    issue_path = stage_dir / "issue_package.yaml"
    _write_yaml_atomic(issue_path, dict(issue_package))
    staged_digests = {
        "issue_package": lifecycle.canonical_content_digest(dict(issue_package))
    }
    if research_catalog is not None:
        research_path = stage_dir / "research_catalog.yaml"
        _write_yaml_atomic(research_path, dict(research_catalog))
        staged_digests["research_catalog"] = lifecycle.canonical_content_digest(
            dict(research_catalog)
        )
    value = dict(operation)
    value["staged_digests"] = staged_digests
    value["updated_at"] = timestamp
    value = persist_operation(baseline.run_dir, value)

    issue_errors = _issue_errors(issue_package, baseline)
    research_errors = _research_errors(research_catalog) if research_catalog is not None else []
    if issue_errors or research_errors:
        raise ResearchOperationError(
            "content_validation_failed", detail=issue_errors + research_errors
        )
    fresh_sbp = bool(sbp_evaluation_questions(baseline.run_dir, baseline.graph))
    if not fresh_sbp and bool(issue_package.get("issues")) != (research_catalog is not None):
        raise ResearchOperationError(
            "content_validation_failed", detail="research presence does not match issue count"
        )
    assert_baseline_current(baseline)
    artifacts: list[dict[str, Any]] = [{
        "key": "issue_package",
        "artifact_type": "issue_package",
        "case_id": baseline.case_id,
        "scenario_id": baseline.scenario_id,
        "content": dict(issue_package),
        "validate": lambda content: _issue_errors(content, baseline),
        "dependencies": {
            "evidence": baseline.evidence_version_id,
            "as_is_graph": baseline.as_is_version_id,
        },
    }]
    if research_catalog is not None:
        artifacts.append({
            "key": "research_catalog",
            "artifact_type": "research_catalog",
            "case_id": baseline.case_id,
            "scenario_id": baseline.scenario_id,
            "content": dict(research_catalog),
            "validate": _research_errors,
            "dependencies": {"issue_package": "@issue_package"},
        })
    try:
        publication = publisher(baseline.registry_path, artifacts, timestamp)
    except ResearchOperationError:
        raise
    except Exception as exc:
        try:
            assert_baseline_current(baseline)
        except ResearchOperationError as changed:
            raise changed from exc
        raise ResearchOperationError("publication_failed", detail=exc) from exc
    version_ids = publication.get("version_ids") if isinstance(publication, dict) else None
    expected_keys = {artifact["key"] for artifact in artifacts}
    if not isinstance(version_ids, dict) or set(version_ids) != expected_keys:
        raise ResearchOperationError("publication_failed", detail="publisher returned invalid IDs")
    return publication, value


def _await_if_needed(value: Any) -> Any:
    return asyncio.run(value) if inspect.isawaitable(value) else value


def _evidence_spans(evidence: Iterable[Mapping[str, Any]]) -> list[str]:
    return [
        str(item.get("text"))
        for item in evidence
        if isinstance(item, Mapping) and isinstance(item.get("text"), str)
    ]


def research_execution_mode() -> str:
    """Return the selected execution adapter used by this process.

    The result is one of ``relay``, ``cli``, ``api``, or ``live``. This is the
    exact criterion `execute_operation` records into its operation state.
    into an operation's `execution_mode` field (see the comment beside that
    assignment below). A single accessor so a caller deciding whether
    confirming As-is would spend never re-derives this rule, and never
    reaches into `research_agent`'s private `_relay_selected()` directly.
    """
    from . import research_agent

    if research_agent._relay_selected():
        return "relay"
    if research_agent._cli_selected():
        return "cli"
    if (os.environ.get("SBP_EXECUTION_MODE") or "").strip().lower() == "api":
        return "api"
    return "live"


def research_provider_disclosure(
    model_policy: Mapping[str, Any] | None = None,
    *,
    execution_mode: str | None = None,
    api_provider: str | None = None,
) -> dict[str, Any]:
    """What confirming As-is and starting External Research would actually
    do: which providers, and whether it can incur live spend.

    Read-only -- does not prepare an operation, resolve a baseline, or touch
    the run directory. require-explicit-authorization-for-live-model-calls:
    this is what `POST /api/review` consults before enqueueing research, and
    what the interface's preview surfaces before the researcher confirms.
    """
    mode = execution_mode or research_execution_mode()
    if mode == "transport":
        mode = "relay"
    if mode == "relay":
        return {
            "execution_mode": "relay",
            "providers": ["transport"],
            "spend_capable": False,
            "plain_summary": "這次會使用手動傳輸，不會呼叫任何線上服務，也不會有線上 fallback。",
        }
    if mode == "cli":
        return {
            "execution_mode": "cli",
            "providers": ["authenticated CLI runner"],
            "spend_capable": True,
            "plain_summary": "這次會交由已登入的本機 CLI 執行，不需要在 Workbench 輸入 API key。",
        }
    if mode == "api":
        provider = (api_provider or os.environ.get("SBP_API_PROVIDER") or "openai").strip().lower()
        return {
            "execution_mode": "api",
            "providers": [provider],
            "spend_capable": True,
            "plain_summary": "這次會使用你提供的 API key 呼叫外部服務，並可能產生費用。",
        }
    policy = {**DEFAULT_MODEL_POLICY, **dict(model_policy or {})}
    # Every provider this operation could actually reach, including the
    # fallback providers issues/issue_grounding fall back to when their
    # primary fails, and the OpenAI research session `run_research_agent`
    # always talks to directly regardless of `model_policy` (it takes a
    # model name, not a provider choice).
    providers = sorted({
        policy["issues_provider"],
        policy["issues_fallback_provider"],
        policy["issue_grounding_provider"],
        policy["issue_grounding_fallback_provider"],
        policy["citation_verifier_provider"],
        "openai",
    })
    return {
        "execution_mode": "live",
        "providers": providers,
        "spend_capable": True,
        "plain_summary": "這次會把逐字稿內容送到外部服務（" + "、".join(providers) + "），並產生實際花費。",
    }


def execute_operation(
    run_dir: str | Path,
    *,
    registry_path: str | Path | None = None,
    expected_as_is_version: str | None = None,
    dependencies: ResearchDependencies | None = None,
    model_policy: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Execute or replay the complete confirmed As-is issue/research chain."""
    from . import research_agent, research_policy, research_privacy, source_snapshot, stages

    deps = dependencies or ResearchDependencies()
    models = {**DEFAULT_MODEL_POLICY, **dict(model_policy or {})}
    baseline = resolve_baseline(
        run_dir,
        registry_path=registry_path,
        expected_as_is_version=expected_as_is_version,
    )
    operation, reused = prepare_operation(
        baseline,
        model_policy=models,
        now=deps.now(),
    )
    if reused and operation.get("state") == "done" and operation.get("phase") in TERMINAL_PHASES:
        return operation, True

    current = transition_operation(
        baseline, operation, "deriving_issues", state="running", now=deps.now()
    )
    try:
        # 這兩個呼叫跑在調研 session 之前，而 relay-external-research-through-
        # manual-transport 只接了 session 那一段。它們把 provider 明確傳下去，
        # 而 `resolve_provider()` 的明確指定一律贏過環境變數（那是全域語意，
        # 不動），所以接力模式下這裡必須自己讓路，否則整次操作會在信封還沒出現
        # 之前就因為缺 live 憑證失敗。判準沿用下面 `execution_mode` 用的同一個。
        execution_mode = research_execution_mode()
        relaying = execution_mode == "relay"
        selected_provider = (
            "transport" if relaying else
            "cli" if execution_mode == "cli" else
            "openai" if execution_mode == "api" else
            None
        )
        opts = {
            "issues_provider": selected_provider or models["issues_provider"],
            "issue_grounding_provider": (
                selected_provider or models["issue_grounding_provider"]
            ),
            "issues_model": models.get("issues_model"),
            "issue_grounding_model": models.get("issue_grounding_model"),
            "max_tokens": 8_000,
        }
        sbp_questions = sbp_evaluation_questions(baseline.run_dir, baseline.graph)
        fresh_sbp = _journey_has_solution_candidates(baseline.run_dir)
        derive = deps.derive_issues or stages.derive_issue_candidates
        ground = deps.ground_issues or stages.ground_issue_candidates
        # 產生 → 驗證 → 把錯誤餵回去重試，跟 structure() 用的是同一個模式。
        # 這一段以前完全沒有重試：模型只要有一則問題引用了「它所引用的元素沒有
        # 帶著」的證據，整次調研就終止，而且標成不可重試。實測 2026-08-22 那份
        # MUJI 藍圖就是這樣掛掉的——同一則問題裡它引用對了一個證據、引用錯了
        # 另一個，是改得回來的錯，卻沒有第二次機會。
        issue_feedback: list[str] = []
        issue_usage = {"tokens": 0, "seconds": 0}
        grounding_usage = {"tokens": 0, "seconds": 0}
        issue_package = ({
            "schema_version": 1, "artifact_type": "issue_package",
            "case_id": baseline.case_id, "scenario_id": baseline.scenario_id,
            "issues": [],
        } if fresh_sbp else None)
        last_error: Exception | None = None
        for attempt in range(0 if fresh_sbp else ISSUE_DERIVATION_ATTEMPTS):
            try:
                # 第一次用三個參數呼叫，保持既有介面（測試注入的 derive_issues
                # 替身多半只接受三個）；只有真的要餵回錯誤時才多傳一個。
                candidates, attempt_issue_usage = (
                    derive(baseline.evidence, baseline.graph, opts, issue_feedback)
                    if issue_feedback
                    else derive(baseline.evidence, baseline.graph, opts)
                )
                verdicts, attempt_grounding_usage = ground(
                    candidates, baseline.evidence, baseline.graph, opts
                )
                issue_usage = _sum_usage(issue_usage, attempt_issue_usage)
                grounding_usage = _sum_usage(grounding_usage, attempt_grounding_usage)
                issue_package = stages.normalize_issue_package(
                    candidates,
                    verdicts,
                    baseline.evidence,
                    baseline.graph,
                    case_id=baseline.case_id,
                    scenario_id=baseline.scenario_id,
                )
                break
            except ValueError as exc:
                # Only the provenance/shape rejection is worth another attempt;
                # it is the model's citation choice, which it can revise.
                last_error = exc
                issue_feedback = [
                    line for line in str(exc).splitlines()[1:] if line.strip()
                ]
                if attempt == ISSUE_DERIVATION_ATTEMPTS - 1:
                    raise ResearchOperationError("issue_grounding_rejected", detail=exc) from exc
            except Exception as exc:
                raise ResearchOperationError("issue_generation_failed", detail=exc) from exc
        if issue_package is None:  # pragma: no cover - loop always raises or breaks
            raise ResearchOperationError("issue_grounding_rejected", detail=last_error)
        current = transition_operation(
            baseline,
            current,
            "checking_privacy",
            now=deps.now(),
            usage={"issues": issue_usage, "issue_grounding": grounding_usage},
            counts={"issues": len(issue_package["issues"]), "sources": 0, "claims": 0},
        )

        research_catalog = None
        if issue_package["issues"] or sbp_questions:
            case_path = baseline.run_dir / "case.yaml"
            case = _read_yaml(case_path)
            known_values = research_privacy.case_value_replacements(case)
            evidence_spans = _evidence_spans(baseline.evidence)
            build_projection = deps.build_projection or research_privacy.build_research_projection
            try:
                projection_options = {
                    "service_context": str(case.get("service_type") or "service"),
                    "improvement_goal": baseline.improvement_goal,
                    "known_values": known_values,
                    "evidence_spans": evidence_spans,
                }
                if sbp_questions:
                    projection_options["sbp_questions"] = sbp_questions
                projection = build_projection(issue_package, **projection_options)
            except Exception as exc:
                raise ResearchOperationError("privacy_boundary_rejected", detail=exc) from exc
            current = transition_operation(
                baseline, current, "researching", now=deps.now()
            )
            # Recorded before the call, not after: a relayed attempt that then
            # fails is still a relayed attempt, and this is the fact a later
            # reader needs — not whether it succeeded. Read once here rather
            # than inside the operation-record helpers below so an injected
            # test double (deps.run_research) can't make this field lie about
            # which real path executed; it reflects this process's own
            # environment, the same thing research_agent itself checks.
            current["execution_mode"] = execution_mode
            run_research = deps.run_research
            if run_research is None:
                # persist-external-research-source-snapshots-for-replay: only
                # bind a snapshot store to the real fetcher path. An injected
                # `deps.run_research` (every test double in this contract
                # suite) replaces the whole research step, so there is no
                # real fetcher underneath it for a store to wrap — passing
                # `source_snapshots` there would just be a kwarg the double
                # has to shrug off for no benefit.
                run_research = functools.partial(
                    research_agent.run_research_agent,
                    source_snapshots=source_snapshot.store_for(
                        baseline.run_dir, str(current["request_digest"])
                    ),
                )
            try:
                agent_result, tool_audit = _await_if_needed(run_research(
                    projection,
                    known_values=known_values,
                    evidence_spans=evidence_spans,
                    model=str(models["research_model"]),
                ))
            except research_agent.ResearchAgentError as exc:
                current["tool_audit"] = exc.audit
                current["failure_causes"] = research_agent.failure_causes(exc)
                persist_operation(baseline.run_dir, current)
                raise ResearchOperationError(exc.code, detail=exc) from exc
            if not isinstance(agent_result, Mapping):
                raise ResearchOperationError("citation_support_rejected")
            current = transition_operation(
                baseline,
                current,
                "validating",
                now=deps.now(),
                tool_audit=list(tool_audit),
            )
            build_catalog = deps.build_catalog or research_policy.build_research_catalog
            try:
                research_catalog, policy_trace = build_catalog(
                    case_id=baseline.case_id,
                    scenario_id=baseline.scenario_id,
                    source_candidates=agent_result.get("sources") or [],
                    claim_candidates=agent_result.get("claims") or [],
                    query_candidates=agent_result.get("query_audit") or [],
                    accessed_at=deps.now()[:10],
                    fetched_bodies=agent_result.get("fetched_bodies") or {},
                )
            except research_policy.ResearchPolicyError as exc:
                current["source_decisions"] = exc.decisions
                current["failure_causes"] = research_agent.failure_causes(exc)
                # research-admissibility-diagnostics: a failure keeps its why.
                current["admissibility_diagnostics"] = _admissibility_record(
                    exc.diagnostics, agent_result)
                persist_operation(baseline.run_dir, current)
                raise ResearchOperationError(exc.code, detail=exc) from exc
            current["source_decisions"] = policy_trace["source_decisions"]
            current["fetched_sources"] = policy_trace["fetched_sources"]
            current["admissibility_diagnostics"] = _admissibility_record(
                policy_trace.get("admissibility"), agent_result)

        current = transition_operation(
            baseline, current, "publishing", now=deps.now()
        )
        publication, current = publish_canonical_results(
            baseline,
            current,
            issue_package,
            research_catalog,
            publisher=deps.publish,
            now=deps.now(),
        )
        result = {
            "issue_version_id": publication["version_ids"]["issue_package"]
        }
        if "research_catalog" in publication["version_ids"]:
            result["research_version_id"] = publication["version_ids"]["research_catalog"]
        counts = {
            "issues": len(issue_package["issues"]),
            "sources": len((research_catalog or {}).get("sources") or []),
            "claims": len((research_catalog or {}).get("claims") or []),
        }
        final_phase = "research_ready" if research_catalog is not None else "no_grounded_issues"
        handoff = None
        if research_catalog is not None:
            handoff = {
                "as_is_version_id": baseline.as_is_version_id,
                "issue_version_id": result["issue_version_id"],
                "research_version_id": result["research_version_id"],
                "improvement_goal_digest": baseline.improvement_goal_digest,
            }
        completed = transition_operation(
            baseline,
            current,
            final_phase,
            state="done",
            now=deps.now(),
            retryable=False,
            counts=counts,
            result=result,
            to_be_handoff=handoff,
            error=None,
        )
        return completed, False
    except ResearchOperationError as exc:
        return fail_operation(baseline, current, exc, now=deps.now()), False
    except Exception as exc:
        safe = ResearchOperationError("content_validation_failed", detail=exc)
        return fail_operation(baseline, current, safe, now=deps.now()), False


def execute_supplemental_research(
    baseline: Any,
    *,
    generalized_purpose: str,
    run_research: Callable[..., Any] | None = None,
    build_catalog: Callable[..., Any] | None = None,
    publisher: Callable[..., Any] = lifecycle.publish_batch,
    model: str = "gpt-5",
    now: str | None = None,
) -> dict[str, Any]:
    """Use the same constrained boundary to replace one research catalog.

    ``baseline`` is the trusted local To-be baseline.  Only the rebuilt
    privacy projection and a fixed generalized purpose cross to the web agent.

    Deliberately not wired to `source_snapshot` (persist-external-research-
    source-snapshots-for-replay): its own operation directory below is named
    from the research *result*'s digest, computed only after this session
    already ran, so there is no stable operation identity available before
    the fetch a store would need to key on has already happened.
    """
    from . import research_agent, research_policy, research_privacy
    from . import to_be as to_be_orchestrator

    timestamp = now or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    case = _read_yaml(Path(baseline.run_dir) / "case.yaml")
    known_values = research_privacy.case_value_replacements(case or {})
    evidence_spans = _evidence_spans(baseline.evidence)
    try:
        base_projection = research_privacy.build_research_projection(
            baseline.issue_package,
            service_context=str((case or {}).get("service_type") or "service"),
            improvement_goal=baseline.improvement_goal,
            known_values=known_values,
            evidence_spans=evidence_spans,
        )
        projection = research_privacy.build_supplemental_research_projection(
            base_projection,
            generalized_purpose=generalized_purpose,
            known_values=known_values,
            evidence_spans=evidence_spans,
        )
    except research_privacy.PrivacyBoundaryError as exc:
        raise ResearchOperationError("privacy_boundary_rejected", detail=exc.audit) from exc
    agent = run_research or research_agent.run_research_agent
    try:
        agent_result, tool_audit = _await_if_needed(agent(
            projection,
            known_values=known_values,
            evidence_spans=evidence_spans,
            model=model,
        ))
    except research_agent.ResearchAgentError as exc:
        raise ResearchOperationError(exc.code, detail=exc) from exc
    if not isinstance(agent_result, Mapping):
        raise ResearchOperationError("citation_support_rejected")
    catalog_builder = build_catalog or research_policy.build_research_catalog
    try:
        catalog, policy_trace = catalog_builder(
            case_id=baseline.case_id,
            scenario_id=baseline.scenario_id,
            source_candidates=agent_result.get("sources") or [],
            claim_candidates=agent_result.get("claims") or [],
            query_candidates=agent_result.get("query_audit") or [],
            accessed_at=timestamp[:10],
            fetched_bodies=agent_result.get("fetched_bodies") or {},
        )
    except research_policy.ResearchPolicyError as exc:
        raise ResearchOperationError(exc.code, detail=exc.decisions) from exc
    errors = _research_errors(catalog)
    if errors:
        raise ResearchOperationError("content_validation_failed", detail=errors)
    to_be_orchestrator.assert_baseline_current(baseline)
    digest = _digest_text(_canonical_json({
        "issue_version_id": baseline.issue_version_id,
        "purpose": generalized_purpose,
        "catalog_digest": lifecycle.canonical_content_digest(catalog),
    }))
    operation_dir = operations_dir(baseline.run_dir) / "supplemental" / digest.removeprefix("sha256:")
    staged = operation_dir / "staged"
    _write_yaml_atomic(staged / "issue_package.yaml", baseline.issue_package)
    _write_yaml_atomic(staged / "research_catalog.yaml", catalog)
    try:
        publication = publisher(
            baseline.registry_path,
            [{
                "key": "research_catalog",
                "artifact_type": "research_catalog",
                "case_id": baseline.case_id,
                "scenario_id": baseline.scenario_id,
                "content": catalog,
                "validate": _research_errors,
                "dependencies": {"issue_package": baseline.issue_version_id},
            }],
            timestamp,
        )
    except Exception as exc:
        raise ResearchOperationError("publication_failed", detail=exc) from exc
    research_version_id = publication["version_ids"]["research_catalog"]
    operation_record = {
        "schema_version": OPERATION_SCHEMA_VERSION,
        "kind": "supplemental_research",
        "state": "done",
        "phase": "research_ready",
        "request_digest": digest,
        "result": {
            "issue_version_id": baseline.issue_version_id,
            "research_version_id": research_version_id,
        },
        "counts": {
            "sources": len(catalog.get("sources") or []),
            "claims": len(catalog.get("claims") or []),
        },
        "tool_audit": list(tool_audit),
        "source_decisions": list((policy_trace or {}).get("source_decisions") or []),
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    _write_json_atomic(operation_dir / "operation.json", operation_record)
    return {
        "publication": publication,
        "operation": operation_record,
        "projection": projection,
    }
