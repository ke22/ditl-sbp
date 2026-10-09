"""Bounded, replay-safe orchestration for evidence-grounded To-be work.

This module owns operation identity and canonical input resolution.  Model
output, publication, and review transitions are deliberately added through
separate injected boundaries so none of them can change lifecycle state by
returning a particular payload.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml
import validate_sbp as V
import storage_locations

from . import as_is_research, improvement_goal_review, inference_reviews, lifecycle, research_catalog_review


OPERATION_SCHEMA_VERSION = 1
OPERATION_KIND = "to_be_generation"
POLICY_VERSIONS = {
    "prompt": "to-be-proposals-v6",
    "schema": "to-be-bundle-v2",
    "normalizer": "to-be-normalizer-v2",
    "projection": "as-is-aligned-projection-v1",
}
DEFAULT_MODEL_POLICY = {
    "provider": "anthropic",
    "model": "claude-opus-4-6",
    "effort": "high",
    "max_attempts": 3,
    "max_tokens": 16_000,
    "tools": [],
}
ACTIVE_STATES = {"queued", "running"}
TERMINAL_PHASES = {"to_be_draft_ready"}
PHASES = (
    "synthesizing",
    "validating",
    "publishing",
    "to_be_draft_ready",
)


ERROR_MESSAGES: dict[str, tuple[str, bool]] = {
    "missing_improvement_goal": ("請先補充改善目標，再產生 To-be 方案。", False),
    "missing_research_review": ("請先完成外部研究的取捨裁定，再產生 To-be 方案。", False),
    "research_review_stale": ("外部研究的取捨裁定與目前研究資料不符，請重新裁定。", False),
    "research_review_invalid": ("外部研究的取捨裁定內容無效，請修正後再產生 To-be 方案。", False),
    "missing_to_be_baseline": ("尚未具備可產生 To-be 方案的完整現況、問題與調研資料。", False),
    "no_grounded_issues": ("目前沒有可由證據支持的現況問題，因此未產生 To-be 方案。", False),
    "to_be_baseline_not_current": ("現況或調研資料已更新，請使用目前版本。", False),
    "to_be_baseline_changed": ("方案產生期間上游資料已更新，請重新產生。", False),
    "to_be_operation_conflict": ("目前已有另一份 To-be 方案正在產生。", False),
    "to_be_configuration_missing": ("To-be 方案模型尚未完成系統設定。", True),
    "to_be_output_invalid": ("To-be 方案未通過內容驗證。", True),
    "no_grounded_to_be_direction": ("目前資料無法形成至少一項有雙重依據的 To-be 建議。", False),
    "to_be_publication_failed": ("To-be 方案無法安全儲存，既有版本未受影響。", True),
    "missing_revision_instruction": ("請輸入希望調整方案的具體說明。", False),
    "invalid_revision_decisions": ("逐提案的調整裁定不完整或不一致，請重新確認後再送出。", False),
    "stale_to_be_fingerprint": ("方案已更新，請對目前版本重新提出調整。", False),
    "supplemental_research_failed": ("未取得可支持這次調整的新增研究資料。", True),
    "invalid_review_decision": ("這個方案決策不受支援。", False),
    "decline_confirmation_required": ("請先確認保留 As-is 且不採用這份 To-be 的影響。", False),
}


class ToBeOperationError(RuntimeError):
    """Stable error that is safe to project to a researcher."""

    def __init__(self, code: str, *, detail: object | None = None):
        message, retryable = ERROR_MESSAGES.get(code, ("To-be 方案未完成。", False))
        super().__init__(message)
        self.code = code
        self.user_message = message
        self.retryable = retryable
        self.detail_digest = _digest_text(repr(detail)) if detail is not None else None


class ToBeOperationConflict(ToBeOperationError):
    def __init__(self, *, detail: object | None = None):
        super().__init__("to_be_operation_conflict", detail=detail)


@dataclass(frozen=True)
class ToBeBaseline:
    run_dir: Path
    registry_path: Path
    case_id: str
    scenario_id: str
    as_is_version_id: str
    as_is_digest: str
    issue_version_id: str
    issue_digest: str
    research_version_id: str
    research_digest: str
    reviewed_fingerprint: str
    improvement_goal: str
    improvement_goal_digest: str
    evidence: list[dict[str, Any]]
    graph: dict[str, Any]
    issue_package: dict[str, Any]
    research_catalog: dict[str, Any]  # SELECT-filtered driver catalog, never the full catalog
    research_directives: dict[str, list[dict[str, Any]]]
    research_operation_request_digest: str | None
    research_review_digest: str
    hmw_statements: list[str]
    primary_hmw: dict[str, Any] | None
    primary_target: dict[str, Any] | None


@dataclass
class ToBeDependencies:
    generate: Callable[..., Any] | None = None
    normalize: Callable[..., Any] | None = None
    publish_bundle: Callable[..., Any] | None = None
    now: Callable[[], str] = field(
        default=lambda: datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    )


@dataclass(frozen=True)
class CurrentToBe:
    version_id: str
    content_digest: str
    fingerprint: str
    bundle: dict[str, Any]


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


def _read_json(path: Path, error_code: str = "missing_to_be_baseline") -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToBeOperationError(error_code, detail=exc) from exc
    if not isinstance(value, dict):
        raise ToBeOperationError(error_code, detail=f"{path.name} is not an object")
    return value


def _read_yaml(path: Path, error_code: str = "missing_to_be_baseline") -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ToBeOperationError(error_code, detail=exc) from exc


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


def _version_map(registry: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        record["version_id"]: record
        for record in registry.get("versions", [])
        if isinstance(record, dict) and isinstance(record.get("version_id"), str)
    }


def _current_version(
    registry: Mapping[str, Any], artifact_type: str, case_id: str, scenario_id: str
) -> str | None:
    for pointer in registry.get("current", []):
        if not isinstance(pointer, dict):
            continue
        if (
            pointer.get("artifact_type") == artifact_type
            and pointer.get("case_id") == case_id
            and pointer.get("scenario_id") == scenario_id
        ):
            value = pointer.get("version_ref")
            return value if isinstance(value, str) else None
    return None


def _dependency_ref(record: Mapping[str, Any], role: str) -> str | None:
    matches = [
        item.get("version_ref")
        for item in record.get("dependencies", [])
        if isinstance(item, dict) and item.get("role") == role
    ]
    return matches[0] if len(matches) == 1 and isinstance(matches[0], str) else None


def _load_research_contents(
    run_dir: Path,
    *,
    issue_version_id: str,
    research_version_id: str,
    issue_digest: str,
    research_digest: str,
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    """Load the immutable contents behind the exact published research IDs.

    The third value is the producing External Research operation's request
    digest, or None when the contents come from the canonical-file fallback
    (no operation is known, so a research review cannot bind to it)."""
    candidates: list[Path] = []
    root = as_is_research.operations_dir(run_dir)
    if root.is_dir():
        candidates.extend(sorted(root.rglob("operation.json")))
    for operation_file in reversed(candidates):
        operation = _read_json(operation_file)
        result = operation.get("result") if isinstance(operation.get("result"), dict) else {}
        if (
            result.get("issue_version_id") != issue_version_id
            or result.get("research_version_id") != research_version_id
        ):
            continue
        stage_dir = operation_file.parent / "staged"
        issue = _read_yaml(stage_dir / "issue_package.yaml")
        research = _read_yaml(stage_dir / "research_catalog.yaml")
        if isinstance(issue, dict) and isinstance(research, dict):
            if (
                lifecycle.canonical_content_digest(issue) == issue_digest
                and lifecycle.canonical_content_digest(research) == research_digest
            ):
                operation_digest = operation.get("request_digest")
                return issue, research, operation_digest if isinstance(operation_digest, str) else None
    # A canonical-file fallback supports imported or migrated runs without
    # weakening digest/version binding.
    issue = _read_yaml(run_dir / "issue_package.yaml") if (run_dir / "issue_package.yaml").is_file() else None
    research = _read_yaml(run_dir / "research_catalog.yaml") if (run_dir / "research_catalog.yaml").is_file() else None
    if (
        isinstance(issue, dict)
        and isinstance(research, dict)
        and lifecycle.canonical_content_digest(issue) == issue_digest
        and lifecycle.canonical_content_digest(research) == research_digest
    ):
        return issue, research, None
    raise ToBeOperationError(
        "missing_to_be_baseline", detail="published issue/research content is unavailable"
    )


def _resolve_primary_hmw(run_path: Path, hmw_entries: list) -> dict[str, Any] | None:
    """wire-hmw-and-improvement-goal-into-to-be: read `primary_hmw_selection.yaml`
    and resolve it into the shape the To-be worker sees. `None` on absence,
    an unreadable/malformed file, or (for the reference branch) an `hmw_id`
    that no longer matches any entry in the run's current `hmw.yaml` — a
    stale selection degrades to no signal, it never blocks To-be generation.
    `grounded_in` (the authored branch's own field) is not checked against
    the run's current axial-coding categories: it is descriptive text here,
    not a structural reference any code in this function computes from.
    """
    selection_path = run_path / "primary_hmw_selection.yaml"
    if not selection_path.exists():
        return None
    try:
        selection = yaml.safe_load(selection_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return None
    if not isinstance(selection, dict):
        return None
    if "hmw_id" in selection:
        hmw_id = selection.get("hmw_id")
        for entry in hmw_entries:
            if isinstance(entry, dict) and entry.get("id") == hmw_id:
                return {
                    "statement": entry.get("statement"),
                    # hmw-framings-from-problem-structures: new framings carry
                    # problem_structure; older entries carry tension.
                    "tension": entry.get("problem_structure") or entry.get("tension"),
                    "evidence_refs": entry.get("evidence_refs"),
                }
        return None
    # A researcher-authored HMW needs only its statement (2026-09-27 ruling);
    # tension, grounded_in and evidence_refs are optional and may be absent.
    if selection.get("statement"):
        return {
            "statement": selection.get("statement"),
            "tension": selection.get("tension"),
            "grounded_in": selection.get("grounded_in"),
            "evidence_refs": selection.get("evidence_refs") or [],
        }
    return None


def _resolve_primary_target(run_path: Path) -> dict[str, Any] | None:
    """quadrant-members-are-designer-assigned: read
    `target_segmentation_selection.yaml`'s `primary_quadrant_id` and resolve
    it against the run's `persona_content.yaml` (for the persona whose
    `quadrant_id` matches, giving a human-readable `label`) and
    `target_segmentation_candidates.yaml` (for that quadrant's `portrait`).
    Replaces the old `_resolve_primary_persona()`, which read
    `primary_persona_selection.yaml` before that mechanism was removed.
    `None` on absence or an unreadable/malformed selection file -- the
    existing tolerance ("absence means the researcher has not chosen yet,
    not a defect") is preserved. Unlike the old function, a resolvable
    `primary_rationale` alone (without a resolvable persona label or
    portrait) is still useful framing text, so those two are best-effort
    and degrade to `None` individually rather than failing the whole
    resolution.
    """
    selection_path = run_path / "target_segmentation_selection.yaml"
    if not selection_path.exists():
        return None
    try:
        selection = yaml.safe_load(selection_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return None
    if not isinstance(selection, dict):
        return None
    quadrant_id = selection.get("primary_quadrant_id")
    business_rationale = selection.get("primary_rationale")
    if not quadrant_id:
        return None

    portrait = None
    candidates_path = run_path / "target_segmentation_candidates.yaml"
    if candidates_path.exists():
        try:
            candidates = yaml.safe_load(candidates_path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            candidates = None
        if isinstance(candidates, dict):
            for quadrant in candidates.get("quadrants") or []:
                if isinstance(quadrant, dict) and quadrant.get("id") == quadrant_id:
                    portrait = quadrant.get("portrait")
                    break

    label = None
    persona_content_path = run_path / "persona_content.yaml"
    if persona_content_path.exists():
        try:
            persona_content = yaml.safe_load(persona_content_path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            persona_content = None
        if isinstance(persona_content, dict):
            for persona in persona_content.get("personas") or []:
                if isinstance(persona, dict) and persona.get("quadrant_id") == quadrant_id:
                    label = persona.get("label")
                    break

    return {"label": label, "portrait": portrait, "business_rationale": business_rationale}


def resolve_baseline(
    run_dir: str | Path,
    *,
    registry_path: str | Path | None = None,
    expected_versions: Mapping[str, str] | None = None,
) -> ToBeBaseline:
    """Resolve one exact lifecycle-current As-is → issue → research chain."""
    run_path = Path(run_dir).resolve()
    run_state = _read_json(run_path / "run.json")
    declared_registry = registry_path or run_state.get("artifact_registry")
    if not isinstance(declared_registry, (str, Path)) or not str(declared_registry):
        raise ToBeOperationError("missing_to_be_baseline")
    if registry_path is not None:
        registry_file = Path(registry_path).expanduser().resolve()
    else:
        try:
            registry_file = storage_locations.resolve_registry_pointer(
                declared_registry, run_path, storage_locations.runs_root_for_run(run_path)
            )
        except storage_locations.RegistryPointerError as exc:
            raise ToBeOperationError("missing_to_be_baseline", detail=str(exc)) from exc
    registry = _read_yaml(registry_file)
    errors = lifecycle.lifecycle_errors(registry)
    if errors:
        raise ToBeOperationError("to_be_baseline_not_current", detail=errors)
    records = _version_map(registry)
    states = lifecycle.resolve_lifecycle(registry)

    graph = _read_yaml(run_path / "service_graph.yaml")
    evidence = _read_yaml(run_path / "evidence.yaml")
    case = _read_yaml(run_path / "case.yaml")
    if not isinstance(graph, dict) or not isinstance(evidence, list) or not isinstance(case, dict):
        raise ToBeOperationError("missing_to_be_baseline")
    case_id = str((graph.get("meta") or {}).get("case_id") or registry.get("case_id") or "")
    scenario_id = str((graph.get("meta") or {}).get("scenario_id") or "")
    if not case_id or not scenario_id:
        raise ToBeOperationError("missing_to_be_baseline")

    ids = {
        artifact_type: _current_version(registry, artifact_type, case_id, scenario_id)
        for artifact_type in ("as_is_graph", "issue_package", "research_catalog")
    }
    if any(not isinstance(value, str) for value in ids.values()):
        raise ToBeOperationError("missing_to_be_baseline")
    if expected_versions:
        for artifact_type, expected in expected_versions.items():
            if artifact_type in ids and ids[artifact_type] != expected:
                raise ToBeOperationError(
                    "to_be_baseline_not_current", detail=f"{artifact_type} version mismatch"
                )
    graph_record = records.get(str(ids["as_is_graph"]))
    issue_record = records.get(str(ids["issue_package"]))
    research_record = records.get(str(ids["research_catalog"]))
    if not graph_record or not issue_record or not research_record:
        raise ToBeOperationError("missing_to_be_baseline")
    if any(states.get(str(value)) != "current" for value in ids.values()):
        raise ToBeOperationError("to_be_baseline_not_current")
    if (
        _dependency_ref(issue_record, "as_is_graph") != ids["as_is_graph"]
        or _dependency_ref(research_record, "issue_package") != ids["issue_package"]
    ):
        raise ToBeOperationError("to_be_baseline_not_current", detail="dependency chain mismatch")

    graph_digest = lifecycle.canonical_content_digest(graph)
    if graph_digest != graph_record.get("content_digest"):
        raise ToBeOperationError("to_be_baseline_not_current", detail="As-is content mismatch")
    current_fingerprint = as_is_research.run_artifact_fingerprint(run_path)
    reviewed_fingerprint = run_state.get("reviewed_artifact_fingerprint")
    if (
        run_state.get("review_state") != "reviewed"
        or not isinstance(current_fingerprint, str)
        or reviewed_fingerprint != current_fingerprint
    ):
        raise ToBeOperationError("missing_to_be_baseline", detail="As-is is not reviewed")

    improvement_goal = improvement_goal_review.read_operative_goal(run_path)
    if not improvement_goal:
        raise ToBeOperationError("missing_improvement_goal")
    hmw_statements = improvement_goal_review.read_hmw_statements(run_path)
    hmw_entries: list = []
    hmw_path = run_path / "hmw.yaml"
    if hmw_path.exists():
        try:
            hmw_doc = yaml.safe_load(hmw_path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            hmw_doc = None
        if isinstance(hmw_doc, dict) and isinstance(hmw_doc.get("entries"), list):
            hmw_entries = hmw_doc["entries"]
    primary_hmw = _resolve_primary_hmw(run_path, hmw_entries)
    primary_target = _resolve_primary_target(run_path)
    issue_digest = str(issue_record.get("content_digest") or "")
    research_digest = str(research_record.get("content_digest") or "")
    issue_package, full_research_catalog, research_operation_request_digest = _load_research_contents(
        run_path,
        issue_version_id=str(ids["issue_package"]),
        research_version_id=str(ids["research_catalog"]),
        issue_digest=issue_digest,
        research_digest=research_digest,
    )
    if not issue_package.get("issues"):
        raise ToBeOperationError("no_grounded_issues")
    try:
        resolved_review = research_catalog_review.resolve(
            run_path,
            full_research_catalog,
            catalog_request_digest=research_operation_request_digest,
            case_id=case_id,
            scenario_id=scenario_id,
        )
    except research_catalog_review.ReviewError as exc:
        raise ToBeOperationError(exc.code, detail=exc.errors or exc.code) from exc
    return ToBeBaseline(
        run_dir=run_path,
        registry_path=registry_file,
        case_id=case_id,
        scenario_id=scenario_id,
        as_is_version_id=str(ids["as_is_graph"]),
        as_is_digest=graph_digest,
        issue_version_id=str(ids["issue_package"]),
        issue_digest=issue_digest,
        research_version_id=str(ids["research_catalog"]),
        research_digest=research_digest,
        reviewed_fingerprint=current_fingerprint,
        improvement_goal=improvement_goal,
        improvement_goal_digest=_digest_text(improvement_goal),
        evidence=evidence,
        graph=graph,
        issue_package=issue_package,
        research_catalog=resolved_review.driver_catalog,
        research_directives=resolved_review.directives,
        research_operation_request_digest=research_operation_request_digest,
        research_review_digest=resolved_review.review_digest,
        hmw_statements=hmw_statements,
        primary_hmw=primary_hmw,
        primary_target=primary_target,
    )


def request_digest(
    baseline: ToBeBaseline,
    *,
    policy_versions: Mapping[str, str] | None = None,
    model_policy: Mapping[str, Any] | None = None,
    revision_context: Mapping[str, Any] | None = None,
) -> str:
    identity = {
        "as_is_version_id": baseline.as_is_version_id,
        "as_is_digest": baseline.as_is_digest,
        "issue_version_id": baseline.issue_version_id,
        "issue_digest": baseline.issue_digest,
        "research_version_id": baseline.research_version_id,
        "research_digest": baseline.research_digest,
        "research_review_digest": baseline.research_review_digest,
        "improvement_goal_digest": baseline.improvement_goal_digest,
        "policy_versions": dict(policy_versions or POLICY_VERSIONS),
        "model_policy": dict(model_policy or DEFAULT_MODEL_POLICY),
        "revision_context": dict(revision_context or {"kind": "initial"}),
    }
    return _digest_text(_canonical_json(identity))


def operations_dir(run_dir: str | Path) -> Path:
    return Path(run_dir) / ".to_be_generation"


def operation_path(run_dir: str | Path, digest: str) -> Path:
    return operations_dir(run_dir) / digest.removeprefix("sha256:") / "operation.json"


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
    baseline: ToBeBaseline,
    digest: str,
    *,
    policy_versions: Mapping[str, str],
    model_policy: Mapping[str, Any],
    revision_context: Mapping[str, Any],
    now: str,
) -> dict[str, Any]:
    return {
        "schema_version": OPERATION_SCHEMA_VERSION,
        "kind": OPERATION_KIND,
        "operation_id": "TBOP-" + digest.removeprefix("sha256:")[:16],
        "state": "queued",
        "phase": "synthesizing",
        "retryable": False,
        "request_digest": digest,
        "baseline": {
            "case_id": baseline.case_id,
            "scenario_id": baseline.scenario_id,
            "as_is_version_id": baseline.as_is_version_id,
            "as_is_digest": baseline.as_is_digest,
            "issue_version_id": baseline.issue_version_id,
            "issue_digest": baseline.issue_digest,
            "research_version_id": baseline.research_version_id,
            "research_digest": baseline.research_digest,
            "reviewed_fingerprint": baseline.reviewed_fingerprint,
            "improvement_goal_digest": baseline.improvement_goal_digest,
        },
        "policy_versions": dict(policy_versions),
        "model_policy": dict(model_policy),
        "revision_context": dict(revision_context),
        "attempt": 1,
        "phase_transitions": [{"phase": "synthesizing", "state": "queued", "at": now}],
        "counts": {"proposals": 0, "blueprint_changes": 0},
        "result": {},
        "error": None,
        "created_at": now,
        "updated_at": now,
    }


def prepare_operation(
    baseline: ToBeBaseline,
    *,
    policy_versions: Mapping[str, str] | None = None,
    model_policy: Mapping[str, Any] | None = None,
    revision_context: Mapping[str, Any] | None = None,
    now: str | None = None,
) -> tuple[dict[str, Any], bool]:
    policies = dict(policy_versions or POLICY_VERSIONS)
    models = dict(model_policy or DEFAULT_MODEL_POLICY)
    revision = dict(revision_context or {"kind": "initial"})
    digest = request_digest(
        baseline,
        policy_versions=policies,
        model_policy=models,
        revision_context=revision,
    )
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
        raise ToBeOperationConflict(detail="a different To-be request is active")
    timestamp = now or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    operation = _new_operation(
        baseline,
        digest,
        policy_versions=policies,
        model_policy=models,
        revision_context=revision,
        now=timestamp,
    )
    operation["attempt"] = prior_attempt + 1
    if prior_errors:
        operation["prior_errors"] = prior_errors
    persist_operation(baseline.run_dir, operation)
    return operation, False


def persist_operation(run_dir: str | Path, operation: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(operation)
    digest = value.get("request_digest")
    if not isinstance(digest, str):
        raise ValueError("operation request_digest is required")
    _write_json_atomic(operation_path(run_dir, digest), value)
    _write_json_atomic(current_operation_path(run_dir), {"request_digest": digest})
    return value


def transition_operation(
    baseline: ToBeBaseline,
    operation: Mapping[str, Any],
    phase: str,
    *,
    state: str = "running",
    now: str | None = None,
    **updates: Any,
) -> dict[str, Any]:
    if phase not in PHASES:
        raise ValueError(f"unknown to_be_generation phase: {phase}")
    timestamp = now or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    value = dict(operation)
    value.update(updates)
    value.update({"phase": phase, "state": state, "updated_at": timestamp})
    transitions = list(value.get("phase_transitions") or [])
    transitions.append({"phase": phase, "state": state, "at": timestamp})
    value["phase_transitions"] = transitions
    return persist_operation(baseline.run_dir, value)


def fail_operation(
    baseline: ToBeBaseline,
    operation: Mapping[str, Any],
    error: ToBeOperationError,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    timestamp = now or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    value = dict(operation)
    value.update({
        "state": "failed",
        "retryable": error.retryable,
        "updated_at": timestamp,
        "error": {"code": error.code, "user_message": error.user_message},
    })
    if error.detail_digest:
        value["diagnostic_digest"] = error.detail_digest
    return persist_operation(baseline.run_dir, value)


def assert_baseline_current(baseline: ToBeBaseline) -> None:
    try:
        latest = resolve_baseline(
            baseline.run_dir,
            registry_path=baseline.registry_path,
            expected_versions={
                "as_is_graph": baseline.as_is_version_id,
                "issue_package": baseline.issue_version_id,
                "research_catalog": baseline.research_version_id,
            },
        )
    except ToBeOperationError as exc:
        raise ToBeOperationError("to_be_baseline_changed", detail=exc.code) from exc
    identity = (
        latest.as_is_digest,
        latest.issue_digest,
        latest.research_digest,
        latest.reviewed_fingerprint,
        latest.improvement_goal_digest,
    )
    expected = (
        baseline.as_is_digest,
        baseline.issue_digest,
        baseline.research_digest,
        baseline.reviewed_fingerprint,
        baseline.improvement_goal_digest,
    )
    if identity != expected:
        raise ToBeOperationError("to_be_baseline_changed")


def project_operation(operation: Mapping[str, Any]) -> dict[str, Any]:
    """Return a provider-neutral projection without prompts, models, or digests."""
    return {
        "kind": OPERATION_KIND,
        "operation_id": operation.get("operation_id"),
        "state": operation.get("state"),
        "phase": operation.get("phase"),
        "retryable": bool(operation.get("retryable")),
        "summary": operation.get("summary"),
        "counts": dict(operation.get("counts") or {}),
        "result": dict(operation.get("result") or {}),
        "error": dict(operation["error"]) if isinstance(operation.get("error"), dict) else None,
    }


def _to_be_fingerprint(version_id: str, content_digest: str) -> str:
    return _digest_text(_canonical_json({
        "version_id": version_id,
        "content_digest": content_digest,
    }))


def reviews_dir(run_dir: str | Path) -> Path:
    return Path(run_dir) / ".to_be_reviews"


def review_path(run_dir: str | Path, version_id: str) -> Path:
    return reviews_dir(run_dir) / f"{version_id}.json"


def ensure_draft_review(
    run_dir: str | Path,
    *,
    version_id: str,
    content_digest: str,
    now: str,
) -> dict[str, Any]:
    path = review_path(run_dir, version_id)
    fingerprint = _to_be_fingerprint(version_id, content_digest)
    if path.is_file():
        existing = _read_json(path)
        if (
            existing.get("version_id") != version_id
            or existing.get("fingerprint") != fingerprint
        ):
            raise ToBeOperationError(
                "to_be_publication_failed", detail="review identity conflict"
            )
        return existing
    review = {
        "schema_version": 1,
        "version_id": version_id,
        "fingerprint": fingerprint,
        "state": "draft",
        "decisions": [],
        "created_at": now,
        "updated_at": now,
    }
    _write_json_atomic(path, review)
    return review


def load_current_review(baseline: ToBeBaseline) -> tuple[CurrentToBe, dict[str, Any]]:
    current = resolve_current_to_be(baseline)
    path = review_path(baseline.run_dir, current.version_id)
    if not path.is_file():
        raise ToBeOperationError("missing_to_be_baseline", detail="review record missing")
    review = _read_json(path)
    if review.get("fingerprint") != current.fingerprint:
        raise ToBeOperationError("stale_to_be_fingerprint")
    return current, review


def decide_current_to_be(
    baseline: ToBeBaseline,
    *,
    expected_fingerprint: str,
    decision: str,
    impact_confirmed: bool = False,
    now: str | None = None,
) -> dict[str, Any]:
    current, review = load_current_review(baseline)
    if expected_fingerprint != current.fingerprint:
        raise ToBeOperationError(
            "stale_to_be_fingerprint", detail=current.fingerprint
        )
    target = {
        "accept": "accepted_pending_validation",
        "decline": "declined",
    }.get(decision)
    if target is None:
        raise ToBeOperationError("invalid_review_decision")
    if decision == "decline" and impact_confirmed is not True:
        raise ToBeOperationError("decline_confirmation_required")
    if review.get("state") == target:
        return review
    if review.get("state") != "draft":
        raise ToBeOperationError("invalid_review_decision", detail="decision is final")
    timestamp = now or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    updated = dict(review)
    updated["state"] = target
    updated["updated_at"] = timestamp
    decisions = list(updated.get("decisions") or [])
    decisions.append({"decision": target, "at": timestamp})
    updated["decisions"] = decisions
    _write_json_atomic(review_path(baseline.run_dir, current.version_id), updated)
    return updated


def resolve_current_to_be(baseline: ToBeBaseline) -> CurrentToBe:
    registry = _read_yaml(baseline.registry_path)
    if lifecycle.lifecycle_errors(registry):
        raise ToBeOperationError("to_be_baseline_not_current")
    version_id = _current_version(
        registry, "to_be_bundle", baseline.case_id, baseline.scenario_id
    )
    records = _version_map(registry)
    record = records.get(str(version_id))
    states = lifecycle.resolve_lifecycle(registry)
    if not isinstance(version_id, str) or not record or states.get(version_id) != "current":
        raise ToBeOperationError("missing_to_be_baseline", detail="no current To-be bundle")
    expected_dependencies = {
        "as_is_graph": baseline.as_is_version_id,
        "issue_package": baseline.issue_version_id,
        "research_catalog": baseline.research_version_id,
    }
    if any(_dependency_ref(record, role) != ref for role, ref in expected_dependencies.items()):
        raise ToBeOperationError("to_be_baseline_not_current", detail="To-be dependency mismatch")
    content_digest = str(record.get("content_digest") or "")
    bundle = None
    for operation_file in reversed(sorted(operations_dir(baseline.run_dir).glob("*/operation.json"))):
        operation = _read_json(operation_file)
        if (operation.get("result") or {}).get("to_be_version_id") != version_id:
            continue
        candidate = _read_yaml(operation_file.parent / "staged" / "to_be_bundle.yaml")
        if isinstance(candidate, dict) and lifecycle.canonical_content_digest(candidate) == content_digest:
            bundle = candidate
            break
    fallback = baseline.run_dir / "to_be_bundle.yaml"
    if bundle is None and fallback.is_file():
        candidate = _read_yaml(fallback)
        if isinstance(candidate, dict) and lifecycle.canonical_content_digest(candidate) == content_digest:
            bundle = candidate
    if bundle is None:
        raise ToBeOperationError("missing_to_be_baseline", detail="current To-be content unavailable")
    return CurrentToBe(
        version_id=version_id,
        content_digest=content_digest,
        fingerprint=_to_be_fingerprint(version_id, content_digest),
        bundle=bundle,
    )


def classify_revision(
    instruction: str, research_catalog: Mapping[str, Any]
) -> str:
    """Conservatively decide whether the admitted catalog can support revision."""
    normalized = " ".join(instruction.casefold().split())
    if not normalized:
        raise ToBeOperationError("missing_revision_instruction")
    explicit_research = (
        "調研", "研究", "文獻", "案例", "證據", "來源", "查找", "搜尋",
        "research", "evidence", "literature", "source", "case study",
    )
    if any(term in normalized for term in explicit_research):
        return "supplement_research"
    synthesis_only = (
        "簡化", "縮小", "改寫", "合併", "排序", "時點", "試行", "pilot",
        "風險", "指標", "門檻", "語氣", "說明", "scope", "simplify",
    )
    if any(term in normalized for term in synthesis_only):
        return "reuse_current_research"
    catalog_text = _canonical_json(research_catalog).casefold()
    meaningful = {
        token
        for token in re.findall(r"[\w\u4e00-\u9fff]{2,}", normalized)
        if token not in {"方案", "建議", "調整", "希望", "請改"}
    }
    return (
        "reuse_current_research"
        if any(token in catalog_text for token in meaningful)
        else "supplement_research"
    )


def generalized_revision_purpose(instruction: str) -> str:
    """Map a local instruction into one fixed, non-reconstructive research purpose."""
    text = instruction.casefold()
    categories = (
        (("識字", "易讀", "看不懂", "可理解"), "研究不同識字能力顧客可理解的服務狀態提示方式"),
        (("無障礙", "障礙", "高齡", "可近"), "研究具可近性需求顧客的服務狀態溝通方式"),
        (("通知", "簡訊", "訊息", "管道"), "研究服務等待期間狀態通知的可轉移設計與限制"),
        (("交接", "handoff", "人員"), "研究服務交接資訊連續性的可轉移做法與限制"),
    )
    for keywords, purpose in categories:
        if any(keyword in text for keyword in keywords):
            return purpose
    return "研究與目前改善目標相關之服務介入的可轉移證據與適用限制"


REVISION_DECISIONS = ("retain", "revise", "remove")


def normalize_revision_decisions(
    decisions: object, prior_bundle: Mapping[str, Any]
) -> list[dict[str, str]]:
    """Check a researcher decision set against the base draft it rules on.

    Nothing here is inferred: the set has to name every base proposal exactly
    once, so an unsupplied ruling can never be defaulted into existence.
    """
    if not isinstance(decisions, (list, tuple)) or not decisions:
        raise ToBeOperationError(
            "invalid_revision_decisions", detail="decisions must be a non-empty list"
        )
    base_ids = [
        str(proposal.get("id"))
        for proposal in prior_bundle.get("proposals") or []
        if isinstance(proposal, dict) and isinstance(proposal.get("id"), str)
    ]
    normalized: list[dict[str, str]] = []
    seen: list[str] = []
    for index, raw in enumerate(decisions):
        if not isinstance(raw, Mapping):
            raise ToBeOperationError(
                "invalid_revision_decisions", detail=f"decisions/{index} must be an object"
            )
        unknown = sorted(set(raw) - {"proposal_ref", "decision", "instruction"})
        if unknown:
            raise ToBeOperationError(
                "invalid_revision_decisions",
                detail=f"decisions/{index} has unexpected keys: {', '.join(unknown)}",
            )
        ref = raw.get("proposal_ref")
        if not isinstance(ref, str) or ref not in base_ids:
            raise ToBeOperationError(
                "invalid_revision_decisions",
                detail=f"decisions/{index} proposal_ref={ref!r} is not a base proposal",
            )
        if ref in seen:
            raise ToBeOperationError(
                "invalid_revision_decisions", detail=f"proposal_ref={ref!r} ruled on twice"
            )
        seen.append(ref)
        decision = raw.get("decision")
        if decision not in REVISION_DECISIONS:
            raise ToBeOperationError(
                "invalid_revision_decisions",
                detail=f"decisions/{index} decision={decision!r} is not one of "
                + ", ".join(REVISION_DECISIONS),
            )
        item = {"proposal_ref": ref, "decision": decision}
        raw_instruction = raw.get("instruction")
        exact = " ".join(str(raw_instruction).split()) if raw_instruction is not None else ""
        if decision == "retain":
            if exact:
                raise ToBeOperationError(
                    "invalid_revision_decisions",
                    detail=f"{ref} is retained, so it takes no instruction",
                )
        elif not exact:
            raise ToBeOperationError(
                "invalid_revision_decisions",
                detail=f"{ref} is {decision}d, so it needs a non-empty instruction",
            )
        else:
            item["instruction"] = exact
        normalized.append(item)
    missing = [ref for ref in base_ids if ref not in seen]
    if missing:
        raise ToBeOperationError(
            "invalid_revision_decisions",
            detail="no decision for " + ", ".join(missing),
        )
    normalized.sort(key=lambda item: base_ids.index(item["proposal_ref"]))
    return normalized


def request_revision(
    run_dir: str | Path,
    *,
    expected_fingerprint: str,
    instruction: str,
    decisions: object | None = None,
    registry_path: str | Path | None = None,
    dependencies: ToBeDependencies | None = None,
    supplement_research: Callable[..., Any] | None = None,
    model_policy: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], bool, str]:
    """Run a complete fingerprint-bound revision replacement."""
    exact_instruction = " ".join(instruction.split())
    if not exact_instruction:
        raise ToBeOperationError("missing_revision_instruction")
    baseline = resolve_baseline(run_dir, registry_path=registry_path)
    current_to_be = resolve_current_to_be(baseline)
    if expected_fingerprint != current_to_be.fingerprint:
        raise ToBeOperationError(
            "stale_to_be_fingerprint", detail=current_to_be.fingerprint
        )
    classification = classify_revision(exact_instruction, baseline.research_catalog)
    revision_context = {
        "kind": "revision",
        "prior_to_be_version_ref": current_to_be.version_id,
        # The content digest, not just the version ref, so a base draft that
        # changes underneath the request cannot pass as the same request.
        "base_content_digest": current_to_be.content_digest,
        "instruction": exact_instruction,
        "instruction_digest": _digest_text(exact_instruction),
    }
    if decisions is not None:
        revision_context["decisions"] = normalize_revision_decisions(
            decisions, current_to_be.bundle
        )
    if classification == "supplement_research":
        if supplement_research is None:
            from . import as_is_research as research_boundary

            supplement_research = research_boundary.execute_supplemental_research
        try:
            supplement_research(
                baseline,
                generalized_purpose=generalized_revision_purpose(exact_instruction),
            )
        except Exception as exc:
            # Persist a replayable failed request without touching the current
            # To-be pointer or exposing exact model/provider diagnostics.
            operation, _ = prepare_operation(
                baseline,
                model_policy={**DEFAULT_MODEL_POLICY, **dict(model_policy or {})},
                revision_context=revision_context,
            )
            failed = fail_operation(
                baseline,
                operation,
                ToBeOperationError("supplemental_research_failed", detail=exc),
            )
            return failed, False, classification
        baseline = resolve_baseline(run_dir, registry_path=registry_path)
    operation, reused = execute_operation(
        baseline.run_dir,
        registry_path=baseline.registry_path,
        revision_context=revision_context,
        prior_bundle=current_to_be.bundle,
        dependencies=dependencies,
        model_policy=model_policy,
    )
    return operation, reused, classification


def _find_validation_revision_replay(
    run_dir: str | Path,
    *,
    expected_fingerprint: str,
    validation_outcome_version_ref: str,
) -> tuple[dict[str, Any], str] | None:
    root = operations_dir(run_dir)
    if not root.is_dir():
        return None
    for path in reversed(sorted(root.glob("*/operation.json"))):
        try:
            operation = _read_json(path)
        except ToBeOperationError:
            continue
        context = operation.get("revision_context") or {}
        if (
            context.get("kind") == "validation_revision"
            and context.get("prior_to_be_fingerprint") == expected_fingerprint
            and context.get("validation_outcome_version_ref")
            == validation_outcome_version_ref
            and operation.get("state") in ACTIVE_STATES.union({"done"})
        ):
            return operation, str(context.get("research_mode") or "reuse_current_research")
    return None


def request_validation_revision(
    run_dir: str | Path,
    *,
    expected_to_be_fingerprint: str,
    pilot_version_ref: str,
    validation_outcome_version_ref: str,
    registry_path: str | Path | None = None,
    dependencies: ToBeDependencies | None = None,
    supplement_research: Callable[..., Any] | None = None,
    model_policy: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], bool, str]:
    """Replace a whole To-be bundle from one exact final revise outcome."""
    replay = _find_validation_revision_replay(
        run_dir,
        expected_fingerprint=expected_to_be_fingerprint,
        validation_outcome_version_ref=validation_outcome_version_ref,
    )
    if replay is not None:
        operation, classification = replay
        return operation, True, classification

    from . import validation as validation_boundary

    validation_baseline = validation_boundary.resolve_baseline(
        run_dir, registry_path=registry_path
    )
    current_to_be = validation_baseline.current_to_be
    if expected_to_be_fingerprint != current_to_be.fingerprint:
        raise ToBeOperationError(
            "stale_to_be_fingerprint", detail=current_to_be.fingerprint
        )
    pilot = validation_boundary.load_current_pilot(validation_baseline)
    outcome = validation_boundary.load_current_outcome(validation_baseline)
    if (
        pilot is None
        or outcome is None
        or pilot.lifecycle_state != "current"
        or outcome.lifecycle_state != "current"
        or pilot.version_id != pilot_version_ref
        or outcome.version_id != validation_outcome_version_ref
        or pilot.record.get("state") != "completed"
        or outcome.outcome.get("final_decision") != "revise"
        or outcome.outcome.get("source_pilot_version_ref") != pilot.version_id
    ):
        raise ToBeOperationError(
            "stale_to_be_fingerprint", detail="validation lineage is not current revise"
        )
    summary = validation_boundary.normalized_revision_summary(
        pilot.record, outcome.outcome
    )
    classifier_text = " ".join([
        str(summary.get("rationale") or ""),
        *(str(item.get("relation_to_rule") or "") for item in summary["assessments"]),
        *(str(item.get("explanation") or "") for item in summary["deviations"]),
        *(str(item.get("explanation") or "") for item in summary["adverse_effects"]),
    ])
    classification = classify_revision(classifier_text, validation_baseline.to_be_baseline.research_catalog)
    registry = _read_yaml(validation_baseline.registry_path)
    records = _version_map(registry)
    pilot_record = records.get(pilot.version_id)
    outcome_record = records.get(outcome.version_id)
    if pilot_record is None or outcome_record is None:
        raise ToBeOperationError("stale_to_be_fingerprint", detail="lineage record missing")
    revision_context = {
        "kind": "validation_revision",
        "prior_to_be_version_ref": current_to_be.version_id,
        "prior_to_be_fingerprint": current_to_be.fingerprint,
        "pilot_version_ref": pilot.version_id,
        "pilot_content_digest": pilot.content_digest,
        "validation_outcome_version_ref": outcome.version_id,
        "validation_outcome_content_digest": outcome.content_digest,
        "validation_context_digest": _digest_text(_canonical_json(summary)),
        "research_mode": classification,
        "validation_context": summary,
    }
    if classification == "supplement_research":
        if supplement_research is None:
            from . import as_is_research as research_boundary

            supplement_research = research_boundary.execute_supplemental_research
        try:
            supplement_research(
                validation_baseline.to_be_baseline,
                generalized_purpose=generalized_revision_purpose(classifier_text),
            )
        except Exception as exc:
            operation, _ = prepare_operation(
                validation_baseline.to_be_baseline,
                model_policy={**DEFAULT_MODEL_POLICY, **dict(model_policy or {})},
                revision_context=revision_context,
            )
            failed = fail_operation(
                validation_baseline.to_be_baseline,
                operation,
                ToBeOperationError("supplemental_research_failed", detail=exc),
            )
            return failed, False, classification
    operation, reused = execute_operation(
        run_dir,
        registry_path=registry_path,
        revision_context=revision_context,
        dependencies=dependencies,
        model_policy=model_policy,
    )
    return operation, reused, classification


def _bundle_errors(bundle: Any, baseline: ToBeBaseline) -> list[str]:
    if not isinstance(bundle, dict):
        return ["[structure] To-be bundle must be an object"]
    return V.to_be_bundle_errors(
        bundle,
        baseline.evidence,
        baseline.graph,
        baseline.issue_package,
        baseline.research_catalog,
    )


def _current_to_be_identity(baseline: ToBeBaseline) -> tuple[str, str] | None:
    """The registry's current To-be version and content digest, or None.

    Deliberately narrower than `resolve_current_to_be`: a `supplement_research`
    revision publishes against a replacement catalog, so the base draft's own
    dependency refs are expected to be older and must not count as drift here.
    """
    try:
        registry = _read_yaml(baseline.registry_path)
        version_id = _current_version(
            registry, "to_be_bundle", baseline.case_id, baseline.scenario_id
        )
        record = _version_map(registry).get(str(version_id))
    except ToBeOperationError:
        return None
    if not isinstance(version_id, str) or not isinstance(record, dict):
        return None
    digest = record.get("content_digest")
    return (version_id, digest) if isinstance(digest, str) else None


def _locked_context_errors(bundle: Any, baseline: ToBeBaseline) -> list[str]:
    """Recheck local goal/fingerprint/content from inside publisher's lock."""
    errors = _bundle_errors(bundle, baseline)
    revision = bundle.get("revision_context") if isinstance(bundle, Mapping) else None
    if isinstance(revision, Mapping) and revision.get("kind") == "revision":
        # The request is bound to one exact base draft; if that draft moved
        # while this candidate was being built, the revision is stale.
        expected = (
            revision.get("prior_to_be_version_ref"),
            revision.get("base_content_digest"),
        )
        if _current_to_be_identity(baseline) != expected:
            errors.append("[freshness] base to-be draft changed")
    try:
        # case.yaml stays the historical baseline; only its readability is checked.
        _read_yaml(baseline.run_dir / "case.yaml")
        graph = _read_yaml(baseline.run_dir / "service_graph.yaml")
    except ToBeOperationError as exc:
        return errors + [f"[freshness] {exc.code}"]
    try:
        review_now = research_catalog_review.load(baseline.run_dir)
    except research_catalog_review.ReviewError as exc:
        errors.append(f"[freshness] research review {exc.code}")
    else:
        if lifecycle.canonical_content_digest(review_now) != baseline.research_review_digest:
            errors.append("[freshness] research review changed")
    # Same operative-goal source resolve_baseline used, so the two cannot drift.
    goal = improvement_goal_review.read_operative_goal(baseline.run_dir)
    if goal is None:
        errors.append("[freshness] improvement goal missing_improvement_goal")
    elif _digest_text(goal) != baseline.improvement_goal_digest:
        errors.append("[freshness] improvement goal changed")
    if lifecycle.canonical_content_digest(graph) != baseline.as_is_digest:
        errors.append("[freshness] As-is content changed")
    if as_is_research.run_artifact_fingerprint(baseline.run_dir) != baseline.reviewed_fingerprint:
        errors.append("[freshness] reviewed As-is fingerprint changed")
    return errors


def publish_canonical_result(
    baseline: ToBeBaseline,
    operation: Mapping[str, Any],
    bundle: Mapping[str, Any],
    *,
    publisher=lifecycle.publish_batch,
    now: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Stage and atomically publish one complete lifecycle-bound bundle."""
    timestamp = now or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    candidate = dict(bundle)
    stage_dir = operation_path(
        baseline.run_dir, str(operation.get("request_digest") or "")
    ).parent / "staged"
    _write_yaml_atomic(stage_dir / "to_be_bundle.yaml", candidate)
    staged_digest = lifecycle.canonical_content_digest(candidate)
    current = dict(operation)
    current["staged_digests"] = {"to_be_bundle": staged_digest}
    current["updated_at"] = timestamp
    persist_operation(baseline.run_dir, current)

    errors = _bundle_errors(candidate, baseline)
    if errors:
        raise ToBeOperationError("to_be_output_invalid", detail=errors)
    assert_baseline_current(baseline)
    artifact = {
        "key": "to_be_bundle",
        "artifact_type": "to_be_bundle",
        "case_id": baseline.case_id,
        "scenario_id": baseline.scenario_id,
        "content": candidate,
        # publish_batch invokes this callback while holding the registry lock.
        "validate": lambda content: _locked_context_errors(content, baseline),
        "dependencies": {
            "as_is_graph": baseline.as_is_version_id,
            "issue_package": baseline.issue_version_id,
            "research_catalog": baseline.research_version_id,
        },
    }
    dependency_records = _version_map(_read_yaml(baseline.registry_path))
    expected_record = lifecycle.build_version_record(
        artifact_type="to_be_bundle",
        case_id=baseline.case_id,
        scenario_id=baseline.scenario_id,
        content_digest=staged_digest,
        dependencies=[{
            "role": role,
            "version_ref": version_ref,
            "content_digest": dependency_records[version_ref]["content_digest"],
        } for role, version_ref in sorted(artifact["dependencies"].items())],
        created_at=timestamp,
    )
    ensure_draft_review(
        baseline.run_dir,
        version_id=expected_record["version_id"],
        content_digest=staged_digest,
        now=timestamp,
    )
    try:
        publication = publisher(baseline.registry_path, [artifact], timestamp)
    except ToBeOperationError:
        raise
    except Exception as exc:
        try:
            assert_baseline_current(baseline)
        except ToBeOperationError as changed:
            raise changed from exc
        raise ToBeOperationError("to_be_publication_failed", detail=exc) from exc
    version_ids = publication.get("version_ids") if isinstance(publication, dict) else None
    version_id = version_ids.get("to_be_bundle") if isinstance(version_ids, dict) else None
    if not isinstance(version_id, str):
        raise ToBeOperationError(
            "to_be_publication_failed", detail="publisher returned no To-be version"
        )
    if version_id != expected_record["version_id"]:
        raise ToBeOperationError(
            "to_be_publication_failed", detail="publisher returned unexpected version"
        )
    return publication, current


def _normalize_error_code(exc: ValueError) -> str:
    if "至少需要一個 addressed" in str(exc):
        return "no_grounded_to_be_direction"
    return "to_be_output_invalid"


def _is_suspended_relay(operation: Mapping[str, Any]) -> bool:
    """A same-digest operation left running by a manual-transport suspension."""
    return (
        operation.get("state") == "running"
        and operation.get("phase") == "synthesizing"
        and operation.get("execution_mode") == "relay"
    )


def execute_operation(
    run_dir: str | Path,
    *,
    handoff: Mapping[str, str] | None = None,
    registry_path: str | Path | None = None,
    revision_context: Mapping[str, Any] | None = None,
    prior_bundle: Mapping[str, Any] | None = None,
    dependencies: ToBeDependencies | None = None,
    model_policy: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Execute or replay one complete synthesis → validation → publication."""
    from . import llm, stages

    deps = dependencies or ToBeDependencies()
    models = {**DEFAULT_MODEL_POLICY, **dict(model_policy or {})}
    expected_versions = None
    if handoff is not None:
        required = {
            "as_is_version_id", "issue_version_id", "research_version_id",
            "improvement_goal_digest",
        }
        if set(handoff) != required:
            raise ToBeOperationError("missing_to_be_baseline", detail="invalid internal handoff")
        expected_versions = {
            "as_is_graph": handoff["as_is_version_id"],
            "issue_package": handoff["issue_version_id"],
            "research_catalog": handoff["research_version_id"],
        }
    baseline = resolve_baseline(
        run_dir, registry_path=registry_path, expected_versions=expected_versions
    )
    if handoff is not None and handoff["improvement_goal_digest"] != baseline.improvement_goal_digest:
        raise ToBeOperationError("to_be_baseline_not_current", detail="goal digest mismatch")
    operation, reused = prepare_operation(
        baseline,
        model_policy=models,
        revision_context=revision_context,
        now=deps.now(),
    )
    # Manual-transport relay: the operation record on disk is the only state a
    # new process has after TransportSuspended, so the mode is persisted with the
    # running/synthesizing transition, before the worker call.
    try:
        relaying = llm.resolve_provider(None, stage="to_be") == "transport"
    except llm.LLMError:
        relaying = False
    if reused:
        if not (relaying and _is_suspended_relay(operation)):
            return operation, True
        current = operation
    else:
        current = transition_operation(
            baseline, operation, "synthesizing", state="running", now=deps.now(),
            execution_mode="relay" if relaying else "live",
        )
    normalize = deps.normalize or stages.normalize_to_be_candidate
    # Filled by the normalizer: base proposal id -> the id it came back as.
    correspondence: dict[str, str] = {}

    def normalize_candidate(candidate):
        return normalize(
            candidate,
            case_id=baseline.case_id,
            scenario_id=baseline.scenario_id,
            source_as_is_ref=baseline.as_is_version_id,
            improvement_goal=baseline.improvement_goal,
            evidence=baseline.evidence,
            graph=baseline.graph,
            issue_package=baseline.issue_package,
            research_catalog=baseline.research_catalog,
            hmw_statements=baseline.hmw_statements,
            primary_hmw=baseline.primary_hmw,
            primary_target=baseline.primary_target,
            research_directives=baseline.research_directives,
            revision_context=dict(revision_context or {"kind": "initial"}),
            prior_bundle=prior_bundle,
            correspondence=correspondence,
        )

    def candidate_check(candidate):
        # Retryable normalization errors stay ValueError (repair feedback for
        # the next bounded attempt); a non-retryable outcome ends the operation.
        try:
            normalize_candidate(candidate)
        except ValueError as exc:
            code = _normalize_error_code(exc)
            if not ERROR_MESSAGES[code][1]:
                raise ToBeOperationError(code, detail=exc) from exc
            raise

    try:
        generate = deps.generate or stages.generate_to_be_candidates
        try:
            candidate, usage = generate(
                # 研究者判為「需要調整」或「證據不足」的 AI 推論不給 To-be 當依據。
                inference_reviews.graph_for_downstream(baseline.graph, inference_reviews.load(baseline.run_dir)),
                baseline.issue_package,
                baseline.research_catalog,
                improvement_goal=baseline.improvement_goal,
                hmw_statements=baseline.hmw_statements,
                primary_hmw=baseline.primary_hmw,
                primary_target=baseline.primary_target,
                research_directives=baseline.research_directives,
                revision_context=dict(revision_context or {"kind": "initial"}),
                prior_bundle=prior_bundle,
                opts={
                    "to_be_provider": "transport" if relaying else models["provider"],
                    "to_be_model": models["model"],
                    "to_be_effort": models["effort"],
                    "to_be_max_attempts": models["max_attempts"],
                    "max_tokens": models["max_tokens"],
                },
                candidate_check=candidate_check,
            )
        except llm.ValidatedJSONError as exc:
            current["model_attempts"] = exc.attempts
            persist_operation(baseline.run_dir, current)
            raise ToBeOperationError("to_be_output_invalid", detail=exc) from exc
        current = transition_operation(
            baseline, current, "validating", now=deps.now(), usage=usage
        )
        try:
            bundle = normalize_candidate(candidate)
        except ValueError as exc:
            raise ToBeOperationError(_normalize_error_code(exc), detail=exc) from exc
        current = transition_operation(
            baseline, current, "publishing", now=deps.now()
        )
        publish = deps.publish_bundle or publish_canonical_result
        publication, current = publish(
            baseline, current, bundle, now=deps.now()
        )
        version_id = publication["version_ids"]["to_be_bundle"]
        completed = transition_operation(
            baseline,
            current,
            "to_be_draft_ready",
            state="done",
            now=deps.now(),
            retryable=False,
            counts={
                "proposals": len(bundle.get("proposals") or []),
                "blueprint_changes": len(bundle.get("blueprint_changes") or []),
            },
            # The canonical bundle carries lineage only at bundle level, so the
            # per-proposal base -> new mapping is kept here as operation history.
            result=(
                {"to_be_version_id": version_id, "revision_correspondence": dict(correspondence)}
                if correspondence
                else {"to_be_version_id": version_id}
            ),
            summary=str((bundle.get("direction") or {}).get("title") or "To-be 方案已可確認"),
            error=None,
        )
        return completed, False
    except ToBeOperationError as exc:
        return fail_operation(baseline, current, exc, now=deps.now()), False
    except Exception as exc:
        safe = ToBeOperationError("to_be_output_invalid", detail=exc)
        return fail_operation(baseline, current, safe, now=deps.now()), False
