"""Canonical, human-observed To-be pilot and validation workflow.

The module owns deterministic request identity, immutable content snapshots,
fingerprint checks, and lifecycle publication.  It deliberately has no model,
web, raw-upload, or implementation authority.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

import validate_sbp as V

from . import lifecycle, to_be


ERROR_MESSAGES: dict[str, tuple[str, bool]] = {
    "validation_not_eligible": ("請先採納目前 To-be 方案，再開始試行。", False),
    "validation_baseline_changed": ("To-be 方案已更新，請使用目前版本。", False),
    "pilot_scope_deviation_required": ("實際試行範圍與計畫不同，請先說明差異。", False),
    "pilot_already_started": ("目前方案已有試行紀錄，請繼續該次試行。", False),
    "pilot_fingerprint_stale": ("試行紀錄已更新，請重新載入目前版本。", False),
    "pilot_observation_invalid": ("觀測紀錄不完整或無法驗證。", False),
    "pilot_observation_immutable": ("既有觀測不能修改或移除。", False),
    "pilot_evidence_incomplete": ("試行資料尚未涵蓋所有提案項目。", False),
    "pilot_already_completed": ("這次試行已完成資料收集。", False),
    "validation_outcome_invalid": ("驗證結果缺少完整依據或決策說明。", False),
    "validation_outcome_already_recorded": ("這次試行已記錄驗證結果。", False),
    "validation_publication_failed": ("驗證資料無法安全儲存；既有版本未受影響。", True),
}


class ValidationOperationError(RuntimeError):
    """Stable, researcher-safe validation workflow error."""

    def __init__(self, code: str, *, detail: object | None = None):
        message, retryable = ERROR_MESSAGES.get(code, ("驗證操作未完成。", False))
        super().__init__(message)
        self.code = code
        self.user_message = message
        self.retryable = retryable
        self.detail = detail


@dataclass(frozen=True)
class ValidationBaseline:
    to_be_baseline: to_be.ToBeBaseline
    current_to_be: to_be.CurrentToBe
    review: dict[str, Any]

    @property
    def run_dir(self) -> Path:
        return self.to_be_baseline.run_dir

    @property
    def registry_path(self) -> Path:
        return self.to_be_baseline.registry_path

    @property
    def case_id(self) -> str:
        return self.to_be_baseline.case_id

    @property
    def scenario_id(self) -> str:
        return self.to_be_baseline.scenario_id


@dataclass(frozen=True)
class CurrentPilot:
    version_id: str
    content_digest: str
    fingerprint: str
    record: dict[str, Any]
    lifecycle_state: str


@dataclass(frozen=True)
class CurrentOutcome:
    version_id: str
    content_digest: str
    fingerprint: str
    outcome: dict[str, Any]
    lifecycle_state: str


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _fingerprint(version_id: str, content_digest: str) -> str:
    return _digest({"version_id": version_id, "content_digest": content_digest})


def _default_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def root_dir(run_dir: str | Path) -> Path:
    return Path(run_dir) / ".to_be_validation"


def content_dir(run_dir: str | Path) -> Path:
    return root_dir(run_dir) / "content"


def request_dir(run_dir: str | Path) -> Path:
    return root_dir(run_dir) / "requests"


def _content_path(run_dir: str | Path, content_digest: str) -> Path:
    return content_dir(run_dir) / f"{content_digest.removeprefix('sha256:')}.yaml"


def _request_path(run_dir: str | Path, request_digest: str) -> Path:
    return request_dir(run_dir) / f"{request_digest.removeprefix('sha256:')}.json"


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=path.name + ".", suffix=".tmp", delete=False,
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
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=path.name + ".", suffix=".tmp", delete=False,
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


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationOperationError("validation_publication_failed", detail=exc) from exc
    if not isinstance(value, dict):
        raise ValidationOperationError("validation_publication_failed", detail=path)
    return value


def _read_registry(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValidationOperationError("validation_baseline_changed", detail=exc) from exc
    errors = lifecycle.lifecycle_errors(value)
    if errors:
        raise ValidationOperationError("validation_baseline_changed", detail=errors)
    return value


def _version_map(registry: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        item["version_id"]: item
        for item in registry.get("versions", [])
        if isinstance(item, dict) and isinstance(item.get("version_id"), str)
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


def _load_content(run_dir: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    path = _content_path(run_dir, str(record.get("content_digest") or ""))
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValidationOperationError("validation_publication_failed", detail=exc) from exc
    if (
        not isinstance(value, dict)
        or lifecycle.canonical_content_digest(value) != record.get("content_digest")
    ):
        raise ValidationOperationError("validation_publication_failed", detail="content digest mismatch")
    return value


def _normalize_actor(value: Mapping[str, Any]) -> dict[str, str]:
    actor = {
        "actor_id": str(value.get("actor_id") or "").strip(),
        "actor_type": str(value.get("actor_type") or "").strip(),
        "display_name": str(value.get("display_name") or "").strip(),
    }
    if (
        not all(actor.values())
        or actor["actor_type"] not in {"researcher", "operator"}
    ):
        raise ValidationOperationError("pilot_observation_invalid", detail="invalid actor")
    return actor


def resolve_baseline(
    run_dir: str | Path,
    *,
    registry_path: str | Path | None = None,
    require_accepted: bool = True,
) -> ValidationBaseline:
    try:
        baseline = to_be.resolve_baseline(run_dir, registry_path=registry_path)
        current, review = to_be.load_current_review(baseline)
    except to_be.ToBeOperationError as exc:
        raise ValidationOperationError("validation_not_eligible", detail=exc.code) from exc
    if require_accepted and review.get("state") != "accepted_pending_validation":
        raise ValidationOperationError("validation_not_eligible", detail=review.get("state"))
    return ValidationBaseline(baseline, current, review)


def _assert_to_be_current(
    baseline: ValidationBaseline, registry: Mapping[str, Any]
) -> None:
    current_id = _current_version(
        registry, "to_be_bundle", baseline.case_id, baseline.scenario_id
    )
    states = lifecycle.resolve_lifecycle(registry)
    if (
        current_id != baseline.current_to_be.version_id
        or states.get(str(current_id)) != "current"
    ):
        raise ValidationOperationError("validation_baseline_changed")
    review_path = to_be.review_path(baseline.run_dir, baseline.current_to_be.version_id)
    review = _read_json(review_path)
    if (
        review.get("fingerprint") != baseline.current_to_be.fingerprint
        or review.get("state") != "accepted_pending_validation"
    ):
        raise ValidationOperationError("validation_baseline_changed")


def _current_artifact(
    baseline: ValidationBaseline, artifact_type: str
) -> tuple[Mapping[str, Any], str] | None:
    registry = _read_registry(baseline.registry_path)
    version_id = _current_version(registry, artifact_type, baseline.case_id, baseline.scenario_id)
    if not isinstance(version_id, str):
        return None
    record = _version_map(registry).get(version_id)
    if record is None:
        return None
    state = lifecycle.resolve_lifecycle(registry).get(version_id)
    return record, str(state or "")


def load_current_pilot(baseline: ValidationBaseline) -> CurrentPilot | None:
    resolved = _current_artifact(baseline, "pilot_record")
    if resolved is None:
        return None
    record, state = resolved
    content = _load_content(baseline.run_dir, record)
    return CurrentPilot(
        version_id=str(record["version_id"]),
        content_digest=str(record["content_digest"]),
        fingerprint=_fingerprint(str(record["version_id"]), str(record["content_digest"])),
        record=content,
        lifecycle_state=state,
    )


def load_current_outcome(baseline: ValidationBaseline) -> CurrentOutcome | None:
    resolved = _current_artifact(baseline, "validation_outcome")
    if resolved is None:
        return None
    record, state = resolved
    content = _load_content(baseline.run_dir, record)
    return CurrentOutcome(
        version_id=str(record["version_id"]),
        content_digest=str(record["content_digest"]),
        fingerprint=_fingerprint(str(record["version_id"]), str(record["content_digest"])),
        outcome=content,
        lifecycle_state=state,
    )


def _protocol(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries = []
    for proposal in bundle.get("proposals") or []:
        plan = proposal.get("validation_plan") or {}
        entries.append({
            "proposal_ref": proposal.get("id"),
            "target_outcome": plan.get("target_outcome"),
            "metric": plan.get("metric"),
            "method": plan.get("method"),
            "success_condition": plan.get("success_condition"),
            "decision_rule": plan.get("decision_rule"),
            "planned_pilot_scope": plan.get("pilot_scope"),
        })
    return entries


def _request_identity(kind: str, payload: Mapping[str, Any]) -> str:
    return _digest({"kind": kind, "payload": payload})


def _load_replayed_request(
    baseline: ValidationBaseline, request_digest: str
) -> dict[str, Any] | None:
    path = _request_path(baseline.run_dir, request_digest)
    if not path.is_file():
        return None
    request = _read_json(path)
    if request.get("state") != "committed":
        return None
    registry = _read_registry(baseline.registry_path)
    record = _version_map(registry).get(str(request.get("version_id") or ""))
    if record is None:
        raise ValidationOperationError("validation_publication_failed", detail="request version missing")
    content = _load_content(baseline.run_dir, record)
    return {
        "reused": True,
        "request_digest": request_digest,
        "version_id": record["version_id"],
        "content_digest": record["content_digest"],
        "fingerprint": _fingerprint(record["version_id"], record["content_digest"]),
        "artifact": content,
        "lifecycle_state": lifecycle.resolve_lifecycle(registry)[record["version_id"]],
    }


def _publish(
    baseline: ValidationBaseline,
    *,
    kind: str,
    request_digest: str,
    artifact_type: str,
    content: dict[str, Any],
    dependencies: Mapping[str, str],
    now: str,
    validate,
    assert_current,
) -> dict[str, Any]:
    content_digest = lifecycle.canonical_content_digest(content)
    _write_yaml_atomic(_content_path(baseline.run_dir, content_digest), content)
    pending = {
        "schema_version": 1,
        "kind": kind,
        "request_digest": request_digest,
        "state": "pending",
        "artifact_type": artifact_type,
        "content_digest": content_digest,
        "created_at": now,
    }
    _write_json_atomic(_request_path(baseline.run_dir, request_digest), pending)

    def precommit(registry):
        _assert_to_be_current(baseline, registry)
        assert_current(registry)

    try:
        publication = lifecycle.publish_batch(
            baseline.registry_path,
            [{
                "key": artifact_type,
                "artifact_type": artifact_type,
                "case_id": baseline.case_id,
                "scenario_id": baseline.scenario_id,
                "content": content,
                "dependencies": dict(dependencies),
                "validate": validate,
            }],
            now,
            precommit=precommit,
            ledger_path=baseline.run_dir / ".gates" / "ledger.yaml",
        )
    except ValidationOperationError:
        raise
    except (OSError, lifecycle.LifecycleValidationError) as exc:
        raise ValidationOperationError("validation_publication_failed", detail=exc) from exc
    version_id = publication["version_ids"][artifact_type]
    committed = {
        **pending,
        "state": "committed",
        "version_id": version_id,
        "committed_at": now,
    }
    _write_json_atomic(_request_path(baseline.run_dir, request_digest), committed)
    return {
        "reused": False,
        "request_digest": request_digest,
        "version_id": version_id,
        "content_digest": content_digest,
        "fingerprint": _fingerprint(version_id, content_digest),
        "artifact": content,
        "lifecycle_state": publication["lifecycle"][version_id],
    }


def start_pilot(
    run_dir: str | Path,
    *,
    expected_to_be_fingerprint: str,
    actual_scope: str,
    actor: Mapping[str, Any],
    scope_deviation: str | None = None,
    registry_path: str | Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    baseline = resolve_baseline(run_dir, registry_path=registry_path)
    scope = " ".join(str(actual_scope or "").split())
    deviation = " ".join(str(scope_deviation or "").split())
    normalized_actor = _normalize_actor(actor)
    if expected_to_be_fingerprint != baseline.current_to_be.fingerprint:
        raise ValidationOperationError(
            "validation_baseline_changed", detail=baseline.current_to_be.fingerprint
        )
    if not scope:
        raise ValidationOperationError("pilot_observation_invalid", detail="actual_scope is empty")
    protocol = _protocol(baseline.current_to_be.bundle)
    planned_scopes = {str(item.get("planned_pilot_scope") or "") for item in protocol}
    if (len(planned_scopes) != 1 or scope not in planned_scopes) and not deviation:
        raise ValidationOperationError("pilot_scope_deviation_required")
    identity = {
        "to_be_fingerprint": expected_to_be_fingerprint,
        "actual_scope": scope,
        "scope_deviation": deviation or None,
        "actor": normalized_actor,
    }
    request_digest = _request_identity("start_pilot", identity)
    replay = _load_replayed_request(baseline, request_digest)
    if replay is not None:
        return replay
    existing = load_current_pilot(baseline)
    if existing is not None and (
        existing.lifecycle_state == "current"
        and existing.record.get("source_to_be_version_ref") == baseline.current_to_be.version_id
    ):
        raise ValidationOperationError("pilot_already_started", detail=existing.fingerprint)
    timestamp = now or _default_now()
    cycle_id = "PV-" + request_digest.removeprefix("sha256:")[:16]
    candidate = {
        "schema_version": 1,
        "artifact_type": "pilot_record",
        "case_id": baseline.case_id,
        "scenario_id": baseline.scenario_id,
        "pilot_cycle_id": cycle_id,
        "source_to_be_version_ref": baseline.current_to_be.version_id,
        "source_to_be_fingerprint": baseline.current_to_be.fingerprint,
        "state": "collecting",
        "protocol": protocol,
        "actual_scope": scope,
        "observations": [],
        "not_observed": [],
        "deviations": [],
        "adverse_effects": [],
        "started_by": normalized_actor,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    if deviation:
        candidate["scope_deviation"] = deviation

    def assert_no_current_pilot(registry):
        current_id = _current_version(
            registry, "pilot_record", baseline.case_id, baseline.scenario_id
        )
        if current_id is None:
            return
        states = lifecycle.resolve_lifecycle(registry)
        record = _version_map(registry).get(current_id)
        if record and states.get(current_id) == "current":
            raise ValidationOperationError("pilot_already_started", detail=current_id)

    return _publish(
        baseline,
        kind="start_pilot",
        request_digest=request_digest,
        artifact_type="pilot_record",
        content=candidate,
        dependencies={"to_be_bundle": baseline.current_to_be.version_id},
        now=timestamp,
        validate=lambda value: V.pilot_record_errors(value, baseline.current_to_be.bundle),
        assert_current=assert_no_current_pilot,
    )


def _parse_timestamp(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed


def _prepare_new_observations(
    current: CurrentPilot,
    observations: Iterable[Mapping[str, Any]],
    actor: Mapping[str, Any],
    now: str,
) -> list[dict[str, Any]]:
    normalized_actor = _normalize_actor(actor)
    existing = list(current.record.get("observations") or [])
    existing_ids = {item.get("id") for item in existing if isinstance(item, dict)}
    try:
        now_value = _parse_timestamp(now)
        last_time = max(
            (_parse_timestamp(str(item.get("collected_at"))) for item in existing),
            default=None,
        )
    except (TypeError, ValueError) as exc:
        raise ValidationOperationError("pilot_observation_invalid", detail=exc) from exc
    prepared = []
    for raw in observations:
        item = dict(raw)
        if "actor" in item:
            raise ValidationOperationError("pilot_observation_invalid", detail="actor is server-owned")
        item["actor"] = normalized_actor
        observation_id = item.get("id")
        if observation_id in existing_ids or any(x.get("id") == observation_id for x in prepared):
            raise ValidationOperationError("pilot_observation_invalid", detail="duplicate observation id")
        try:
            collected = _parse_timestamp(str(item.get("collected_at") or ""))
        except (TypeError, ValueError) as exc:
            raise ValidationOperationError("pilot_observation_invalid", detail=exc) from exc
        if collected > now_value + timedelta(minutes=5):
            raise ValidationOperationError("pilot_observation_invalid", detail="future timestamp")
        if last_time is not None and collected < last_time:
            raise ValidationOperationError("pilot_observation_invalid", detail="non-append timestamp")
        prepared.append(item)
    return sorted(prepared, key=lambda item: (str(item.get("collected_at")), str(item.get("id"))))


def append_pilot_data(
    run_dir: str | Path,
    *,
    expected_pilot_fingerprint: str,
    actor: Mapping[str, Any],
    observations: Iterable[Mapping[str, Any]] = (),
    not_observed: Iterable[Mapping[str, Any]] = (),
    deviations: Iterable[Mapping[str, Any]] = (),
    adverse_effects: Iterable[Mapping[str, Any]] = (),
    registry_path: str | Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    baseline = resolve_baseline(run_dir, registry_path=registry_path)
    observation_items = [dict(item) for item in observations]
    not_observed_items = [dict(item) for item in not_observed]
    deviation_items = [dict(item) for item in deviations]
    adverse_effect_items = [dict(item) for item in adverse_effects]
    normalized_actor = _normalize_actor(actor)
    identity_observations = []
    for item in observation_items:
        identity = dict(item)
        identity["actor"] = normalized_actor
        identity_observations.append(identity)
    request_digest = _request_identity("append_pilot_data", {
        "pilot_fingerprint": expected_pilot_fingerprint,
        "observations": identity_observations,
        "not_observed": not_observed_items,
        "deviations": deviation_items,
        "adverse_effects": adverse_effect_items,
        "actor": normalized_actor,
    })
    replay = _load_replayed_request(baseline, request_digest)
    if replay is not None:
        return replay
    current = load_current_pilot(baseline)
    if current is None or current.lifecycle_state != "current":
        raise ValidationOperationError("pilot_fingerprint_stale")
    if expected_pilot_fingerprint != current.fingerprint:
        raise ValidationOperationError("pilot_fingerprint_stale", detail=current.fingerprint)
    if current.record.get("state") == "completed":
        raise ValidationOperationError("pilot_already_completed")
    timestamp = now or _default_now()
    new_observations = _prepare_new_observations(
        current, observation_items, actor, timestamp
    )
    for collection in (not_observed_items, deviation_items, adverse_effect_items):
        for item in collection:
            if "actor" in item:
                raise ValidationOperationError("pilot_observation_invalid", detail="actor is server-owned")
    if not any((new_observations, not_observed_items, deviation_items, adverse_effect_items)):
        raise ValidationOperationError("pilot_observation_invalid", detail="empty append")
    candidate = copy.deepcopy(current.record)
    candidate["observations"] = list(candidate.get("observations") or []) + new_observations
    candidate["not_observed"] = list(candidate.get("not_observed") or []) + not_observed_items
    candidate["deviations"] = list(candidate.get("deviations") or []) + deviation_items
    candidate["adverse_effects"] = list(candidate.get("adverse_effects") or []) + adverse_effect_items
    candidate["updated_at"] = timestamp
    candidate_errors = V.pilot_record_errors(
        candidate, baseline.current_to_be.bundle, previous_pilot=current.record
    )
    if candidate_errors:
        code = (
            "pilot_observation_immutable"
            if any("alters or removes" in item for item in candidate_errors)
            else "pilot_observation_invalid"
        )
        raise ValidationOperationError(code, detail=candidate_errors)

    def assert_pilot(registry):
        current_id = _current_version(
            registry, "pilot_record", baseline.case_id, baseline.scenario_id
        )
        records = _version_map(registry)
        record = records.get(str(current_id))
        if (
            record is None
            or _fingerprint(record["version_id"], record["content_digest"]) != current.fingerprint
        ):
            raise ValidationOperationError("pilot_fingerprint_stale")

    result = _publish(
        baseline,
        kind="append_pilot_data",
        request_digest=request_digest,
        artifact_type="pilot_record",
        content=candidate,
        dependencies={"to_be_bundle": baseline.current_to_be.version_id},
        now=timestamp,
        validate=lambda value: V.pilot_record_errors(
            value, baseline.current_to_be.bundle, previous_pilot=current.record
        ),
        assert_current=assert_pilot,
    )
    return result


def complete_pilot(
    run_dir: str | Path,
    *,
    expected_pilot_fingerprint: str,
    actor: Mapping[str, Any],
    registry_path: str | Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    baseline = resolve_baseline(run_dir, registry_path=registry_path)
    normalized_actor = _normalize_actor(actor)
    request_digest = _request_identity("complete_pilot", {
        "pilot_fingerprint": expected_pilot_fingerprint,
        "actor": normalized_actor,
    })
    replay = _load_replayed_request(baseline, request_digest)
    if replay is not None:
        return replay
    current = load_current_pilot(baseline)
    if current is None or current.lifecycle_state != "current":
        raise ValidationOperationError("pilot_fingerprint_stale")
    if expected_pilot_fingerprint != current.fingerprint:
        raise ValidationOperationError("pilot_fingerprint_stale", detail=current.fingerprint)
    if current.record.get("state") == "completed":
        raise ValidationOperationError("pilot_already_completed")
    timestamp = now or _default_now()
    candidate = copy.deepcopy(current.record)
    candidate.update({"state": "completed", "completed_at": timestamp, "updated_at": timestamp})
    errors = V.pilot_record_errors(
        candidate, baseline.current_to_be.bundle, previous_pilot=current.record
    )
    if errors:
        raise ValidationOperationError("pilot_evidence_incomplete", detail=errors)

    def assert_pilot(registry):
        current_id = _current_version(
            registry, "pilot_record", baseline.case_id, baseline.scenario_id
        )
        record = _version_map(registry).get(str(current_id))
        if (
            record is None
            or _fingerprint(record["version_id"], record["content_digest"]) != current.fingerprint
        ):
            raise ValidationOperationError("pilot_fingerprint_stale")

    return _publish(
        baseline,
        kind="complete_pilot",
        request_digest=request_digest,
        artifact_type="pilot_record",
        content=candidate,
        dependencies={"to_be_bundle": baseline.current_to_be.version_id},
        now=timestamp,
        validate=lambda value: V.pilot_record_errors(
            value, baseline.current_to_be.bundle, previous_pilot=current.record
        ),
        assert_current=assert_pilot,
    )


def derive_recommended_decision(assessments: Iterable[Mapping[str, Any]]) -> str:
    return V._recommended_validation_decision(list(assessments))


def normalized_revision_summary(
    pilot: Mapping[str, Any], outcome: Mapping[str, Any]
) -> dict[str, Any]:
    """Return only outcome-cited observations and explicit validation facts."""
    cited = {
        reference
        for assessment in outcome.get("assessments") or []
        if isinstance(assessment, dict)
        for reference in assessment.get("observation_refs") or []
    }
    observations = sorted(
        [
            copy.deepcopy(item)
            for item in pilot.get("observations") or []
            if isinstance(item, dict) and item.get("id") in cited
        ],
        key=lambda item: (str(item.get("collected_at")), str(item.get("id"))),
    )
    summary = {
        "pilot_cycle_id": pilot.get("pilot_cycle_id"),
        "cited_observations": observations,
        "not_observed": copy.deepcopy(list(pilot.get("not_observed") or [])),
        "deviations": copy.deepcopy(list(pilot.get("deviations") or [])),
        "adverse_effects": copy.deepcopy(list(pilot.get("adverse_effects") or [])),
        "assessments": copy.deepcopy(list(outcome.get("assessments") or [])),
        "final_decision": outcome.get("final_decision"),
        "rationale": outcome.get("rationale"),
    }
    if outcome.get("deviation_rationale"):
        summary["deviation_rationale"] = outcome["deviation_rationale"]
    return summary


def record_outcome(
    run_dir: str | Path,
    *,
    expected_pilot_fingerprint: str,
    assessments: Iterable[Mapping[str, Any]],
    final_decision: str,
    rationale: str,
    actor: Mapping[str, Any],
    deviation_rationale: str | None = None,
    impact_confirmed: bool = False,
    registry_path: str | Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    baseline = resolve_baseline(run_dir, registry_path=registry_path)
    normalized_actor = _normalize_actor(actor)
    assessment_list = [dict(item) for item in assessments]
    recommendation = derive_recommended_decision(assessment_list)
    final = str(final_decision or "").strip()
    reason = " ".join(str(rationale or "").split())
    deviation = " ".join(str(deviation_rationale or "").split())
    aligned = final == recommendation
    payload = {
        "pilot_fingerprint": expected_pilot_fingerprint,
        "assessments": assessment_list,
        "recommended_decision": recommendation,
        "final_decision": final,
        "rationale": reason,
        "deviation_rationale": deviation or None,
        "impact_confirmed": impact_confirmed,
        "actor": normalized_actor,
    }
    request_digest = _request_identity("record_validation_outcome", payload)
    replay = _load_replayed_request(baseline, request_digest)
    if replay is not None:
        return replay
    current = load_current_pilot(baseline)
    if current is None or current.lifecycle_state != "current":
        raise ValidationOperationError("pilot_fingerprint_stale")
    if expected_pilot_fingerprint != current.fingerprint:
        raise ValidationOperationError("pilot_fingerprint_stale", detail=current.fingerprint)
    if current.record.get("state") != "completed":
        raise ValidationOperationError("pilot_evidence_incomplete")
    timestamp = now or _default_now()
    existing = load_current_outcome(baseline)
    if existing is not None and existing.lifecycle_state == "current":
        raise ValidationOperationError(
            "validation_outcome_already_recorded", detail=existing.fingerprint
        )
    candidate = {
        "schema_version": 1,
        "artifact_type": "validation_outcome",
        "case_id": baseline.case_id,
        "scenario_id": baseline.scenario_id,
        "outcome_id": "VO-" + request_digest.removeprefix("sha256:")[:16],
        "pilot_cycle_id": current.record["pilot_cycle_id"],
        "source_pilot_version_ref": current.version_id,
        "source_pilot_fingerprint": current.fingerprint,
        "assessments": assessment_list,
        "recommended_decision": recommendation,
        "final_decision": final,
        "decision_alignment": "aligned" if aligned else "deviated",
        "rationale": reason,
        "decided_by": normalized_actor,
        "decided_at": timestamp,
    }
    if not aligned:
        candidate["deviation_rationale"] = deviation
        if impact_confirmed is True:
            candidate["impact_confirmed"] = True
    errors = V.validation_outcome_errors(
        candidate, current.record, baseline.current_to_be.bundle
    )
    if errors:
        raise ValidationOperationError("validation_outcome_invalid", detail=errors)

    def assert_pilot_and_no_outcome(registry):
        pilot_id = _current_version(
            registry, "pilot_record", baseline.case_id, baseline.scenario_id
        )
        records = _version_map(registry)
        pilot_record = records.get(str(pilot_id))
        if (
            pilot_record is None
            or _fingerprint(pilot_record["version_id"], pilot_record["content_digest"])
            != current.fingerprint
        ):
            raise ValidationOperationError("pilot_fingerprint_stale")
        outcome_id = _current_version(
            registry, "validation_outcome", baseline.case_id, baseline.scenario_id
        )
        if outcome_id is not None and lifecycle.resolve_lifecycle(registry).get(outcome_id) == "current":
            raise ValidationOperationError("validation_outcome_already_recorded")

    return _publish(
        baseline,
        kind="record_validation_outcome",
        request_digest=request_digest,
        artifact_type="validation_outcome",
        content=candidate,
        dependencies={"pilot_record": current.version_id},
        now=timestamp,
        validate=lambda value: V.validation_outcome_errors(
            value, current.record, baseline.current_to_be.bundle
        ),
        assert_current=assert_pilot_and_no_outcome,
    )


def _load_historical_to_be(
    baseline: ValidationBaseline,
    version_id: str,
    content_digest: str,
) -> dict[str, Any]:
    if baseline.current_to_be.version_id == version_id:
        return copy.deepcopy(baseline.current_to_be.bundle)
    for path in sorted(
        (baseline.run_dir / ".to_be_generation").glob("*/staged/to_be_bundle.yaml")
    ):
        try:
            candidate = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if (
            isinstance(candidate, dict)
            and lifecycle.canonical_content_digest(candidate) == content_digest
        ):
            return candidate
    raise ValidationOperationError(
        "validation_publication_failed", detail="source To-be content unavailable"
    )


def export_validation(run_dir: str | Path, *, registry_path: str | Path | None = None) -> dict[str, Any]:
    baseline = resolve_baseline(run_dir, registry_path=registry_path, require_accepted=False)
    registry = _read_registry(baseline.registry_path)
    current = load_current_pilot(baseline)
    if current is None:
        raise ValidationOperationError("validation_not_eligible")
    cycle_id = current.record.get("pilot_cycle_id")
    versions = []
    for record in registry.get("versions") or []:
        if not isinstance(record, dict) or record.get("artifact_type") != "pilot_record":
            continue
        content = _load_content(baseline.run_dir, record)
        if content.get("pilot_cycle_id") == cycle_id:
            versions.append({"lifecycle": dict(record), "content": content})
    versions.sort(key=lambda item: (
        str(item["lifecycle"].get("created_at") or ""),
        str(item["lifecycle"].get("version_id") or ""),
    ))
    outcome = load_current_outcome(baseline)
    records = _version_map(registry)
    source_to_be_id = str(current.record.get("source_to_be_version_ref") or "")
    source_to_be_record = records.get(source_to_be_id)
    if source_to_be_record is None:
        raise ValidationOperationError(
            "validation_publication_failed", detail="source To-be lineage missing"
        )
    source_to_be = _load_historical_to_be(
        baseline,
        source_to_be_id,
        str(source_to_be_record.get("content_digest") or ""),
    )
    try:
        source_review = _read_json(to_be.review_path(baseline.run_dir, source_to_be_id))
    except ValidationOperationError:
        source_review = {}
    cited = set()
    if outcome is not None and outcome.outcome.get("pilot_cycle_id") == cycle_id:
        for assessment in outcome.outcome.get("assessments") or []:
            cited.update(assessment.get("observation_refs") or [])
    observations = sorted(
        [item for item in current.record.get("observations") or [] if item.get("id") in cited],
        key=lambda item: (str(item.get("collected_at")), str(item.get("id"))),
    )
    dependency_order = [source_to_be_id]
    dependency_order.extend(
        str(item["lifecycle"].get("version_id") or "") for item in versions
    )
    if outcome is not None and outcome.outcome.get("pilot_cycle_id") == cycle_id:
        dependency_order.append(outcome.version_id)
    dependency_records = [
        {
            "version_id": version_id,
            "artifact_type": records[version_id].get("artifact_type"),
            "dependencies": copy.deepcopy(records[version_id].get("dependencies") or []),
        }
        for version_id in dependency_order
        if version_id in records
    ]
    return {
        "schema_version": 1,
        "case_id": baseline.case_id,
        "scenario_id": baseline.scenario_id,
        "accepted_to_be": {
            "version_id": source_to_be_id,
            "content_digest": source_to_be_record.get("content_digest"),
            "fingerprint": current.record.get("source_to_be_fingerprint"),
            "review_state": source_review.get("state"),
            "review_fingerprint": source_review.get("fingerprint"),
            "bundle": source_to_be,
        },
        "pilot_cycle_id": cycle_id,
        "pilot_versions": versions,
        "current_pilot": current.record,
        "validation_outcome": outcome.outcome if outcome else None,
        "cited_observations": observations,
        "lifecycle_dependencies": dependency_records,
        "disclaimer": (
            "此結果只代表界定範圍內的試行判斷，不代表已實施、組織核准、因果證明或普遍有效。"
        ),
    }
