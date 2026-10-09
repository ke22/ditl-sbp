"""Canonical Journey Branch researcher write operations.

Phase 3 deliberately stops at the canonical write boundary.  The functions in
this module return complete Journey documents and commit them atomically through
the existing lifecycle registry; no UI or delivery projection is involved.
"""
from __future__ import annotations

import copy
import datetime as dt
import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

import storage_locations

import validate_sbp as V
from . import as_is_projection, gates, lifecycle, stages


class JourneyOperationError(ValueError):
    """Raised when a researcher operation cannot be committed safely."""


CONVERSION_OPERATIONS = {"scenario_to_branch", "branch_to_scenario"}
OPERATIONS = {
    "create_branch",
    "rename_branch",
    "set_divergence",
    "set_merge",
    "add_branch_step",
    "rename_branch_step",
    "reorder_branch_steps",
    "remove_branch_step",
    "delete_branch",
    *CONVERSION_OPERATIONS,
}


def _fail(message: str) -> None:
    raise JourneyOperationError(message)


def _copy_journey(journey: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(journey, Mapping):
        _fail("Journey 必須是 object")
    result = copy.deepcopy(dict(journey))
    if result.get("schema_version") not in (2, 3):
        _fail("只支援 Journey schema v2 或 v3")
    return result


def _to_v3(journey: Mapping[str, Any]) -> dict[str, Any]:
    result = _copy_journey(journey)
    if result.get("schema_version") == 2:
        # Explicit Create/Conversion is the only v2 → v3 boundary.  Reading a
        # v2 document never mutates it and no scenario is reclassified here.
        result["schema_version"] = 3
        result["branches"] = []
    return result


def _scenario(journey: Mapping[str, Any], scenario_id: str) -> dict[str, Any]:
    for item in journey.get("scenarios") or []:
        if isinstance(item, dict) and item.get("id") == scenario_id:
            return item
    _fail(f"找不到 scenario_id={scenario_id!r}")


def _primary_steps(journey: Mapping[str, Any]) -> list[dict[str, Any]]:
    primary = _scenario(journey, journey.get("primary_scenario"))
    return [step for step in primary.get("steps") or [] if isinstance(step, dict)]


def _branch(journey: Mapping[str, Any], branch_id: str) -> dict[str, Any]:
    for item in journey.get("branches") or []:
        if isinstance(item, dict) and item.get("branch_id") == branch_id:
            return item
    _fail(f"找不到 branch_id={branch_id!r}")


def _all_step_ids(journey: Mapping[str, Any]) -> set[str]:
    return {
        step.get("id")
        for scenario in journey.get("scenarios") or []
        if isinstance(scenario, dict)
        for step in scenario.get("steps") or []
        if isinstance(step, dict) and isinstance(step.get("id"), str)
    } | {
        step.get("id")
        for branch in journey.get("branches") or []
        if isinstance(branch, dict)
        for step in branch.get("steps") or []
        if isinstance(step, dict) and isinstance(step.get("id"), str)
    }


def _normalize_and_validate(journey: Mapping[str, Any], *, evidence_ids: set[str] | None = None) -> dict[str, Any]:
    normalized = V.normalize_journey_topology(_copy_journey(journey))
    errors = V.structural_errors(normalized, V.load_schema("journey"))
    errors.extend(V.editable_journey_errors(normalized))
    if evidence_ids is not None:
        errors.extend(stages._journey_errors(normalized, evidence_ids, require_confirmed=True))
    if errors:
        _fail("Journey operation produced invalid canonical content:\n" + "\n".join(sorted(set(errors))))
    return normalized


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{field} 必須是非空字串")
    return value.strip()


def _require_rationale(value: Any) -> str:
    return _require_text(value, "classification-change rationale")


def create_branch(
    journey: Mapping[str, Any],
    *,
    branch_id: str,
    label: str,
    divergence_step_id: str,
    steps: list[Mapping[str, Any]],
    topology_provenance: str,
    merge_step_id: str | None = None,
) -> dict[str, Any]:
    result = _to_v3(journey)
    branch_id = _require_text(branch_id, "branch_id")
    label = _require_text(label, "branch label")
    divergence_step_id = _require_text(divergence_step_id, "divergence_step_id")
    if not isinstance(steps, list) or not steps:
        _fail("Branch 必須一次提交至少一個 branch-owned step")
    if branch_id in {item.get("branch_id") for item in result.get("branches") or [] if isinstance(item, dict)}:
        _fail(f"branch_id 已存在：{branch_id}")
    candidate = {
        "branch_id": branch_id,
        "label": label,
        "divergence_step_id": divergence_step_id,
        "steps": copy.deepcopy(steps),
        "topology_provenance": topology_provenance,
    }
    if merge_step_id is not None:
        candidate["merge_step_id"] = merge_step_id
    result.setdefault("branches", []).append(candidate)
    return _normalize_and_validate(result)


def rename_branch(journey: Mapping[str, Any], *, branch_id: str, label: str) -> dict[str, Any]:
    result = _copy_journey(journey)
    _branch(result, branch_id)["label"] = _require_text(label, "branch label")
    return _normalize_and_validate(result)


def set_divergence(journey: Mapping[str, Any], *, branch_id: str, divergence_step_id: str) -> dict[str, Any]:
    result = _copy_journey(journey)
    _branch(result, branch_id)["divergence_step_id"] = _require_text(divergence_step_id, "divergence_step_id")
    return _normalize_and_validate(result)


def set_merge(journey: Mapping[str, Any], *, branch_id: str, merge_step_id: str | None) -> dict[str, Any]:
    result = _copy_journey(journey)
    branch = _branch(result, branch_id)
    branch["merge_step_id"] = None if merge_step_id is None else _require_text(merge_step_id, "merge_step_id")
    return _normalize_and_validate(result)


def add_branch_step(journey: Mapping[str, Any], *, branch_id: str, step: Mapping[str, Any], position: int | None = None) -> dict[str, Any]:
    result = _copy_journey(journey)
    if not isinstance(step, Mapping):
        _fail("branch step 必須是 object")
    step_id = _require_text(step.get("id"), "step_id")
    if step_id in _all_step_ids(result):
        _fail(f"document-wide step_id 已存在：{step_id}")
    steps = _branch(result, branch_id).setdefault("steps", [])
    item = copy.deepcopy(dict(step))
    if position is None:
        steps.append(item)
    elif isinstance(position, int) and 0 <= position <= len(steps):
        steps.insert(position, item)
    else:
        _fail("branch step position 超出範圍")
    return _normalize_and_validate(result)


def rename_branch_step(journey: Mapping[str, Any], *, branch_id: str, step_id: str, label: str) -> dict[str, Any]:
    result = _copy_journey(journey)
    branch_step = next((s for s in _branch(result, branch_id).get("steps") or [] if s.get("id") == step_id), None)
    if branch_step is None:
        _fail(f"找不到 branch step_id={step_id!r}")
    branch_step["label"] = _require_text(label, "step label")
    return _normalize_and_validate(result)


def reorder_branch_steps(journey: Mapping[str, Any], *, branch_id: str, step_ids: list[str]) -> dict[str, Any]:
    result = _copy_journey(journey)
    branch = _branch(result, branch_id)
    current = [step.get("id") for step in branch.get("steps") or []]
    if step_ids != current and set(step_ids) != set(current):
        _fail("reorder 必須完整且只使用目前 Branch step_id")
    if len(step_ids) != len(current) or len(set(step_ids)) != len(step_ids):
        _fail("reorder step_ids 必須無重複且完整")
    by_id = {step.get("id"): step for step in branch.get("steps") or []}
    branch["steps"] = [by_id[step_id] for step_id in step_ids]
    return _normalize_and_validate(result)


def remove_branch_step(journey: Mapping[str, Any], *, branch_id: str, step_id: str) -> dict[str, Any]:
    result = _copy_journey(journey)
    branch = _branch(result, branch_id)
    remaining = [step for step in branch.get("steps") or [] if step.get("id") != step_id]
    if len(remaining) == len(branch.get("steps") or []):
        _fail(f"找不到 branch step_id={step_id!r}")
    if not remaining:
        _fail("Branch 至少需要一個 branch-owned step")
    branch["steps"] = remaining
    return _normalize_and_validate(result)


def delete_branch(journey: Mapping[str, Any], *, branch_id: str) -> dict[str, Any]:
    result = _copy_journey(journey)
    before = len(result.get("branches") or [])
    result["branches"] = [item for item in result.get("branches") or [] if item.get("branch_id") != branch_id]
    if len(result["branches"]) == before:
        _fail(f"找不到 branch_id={branch_id!r}")
    return _normalize_and_validate(result)


def scenario_to_branch(
    journey: Mapping[str, Any],
    *,
    scenario_id: str,
    branch_id: str,
    divergence_step_id: str,
    topology_provenance: str,
    rationale: str,
    merge_step_id: str | None = None,
) -> dict[str, Any]:
    _require_rationale(rationale)
    result = _to_v3(journey)
    if scenario_id == result.get("primary_scenario"):
        _fail("primary scenario 不能轉為 Branch")
    scenario = _scenario(result, scenario_id)
    result["scenarios"] = [item for item in result.get("scenarios") or [] if item.get("id") != scenario_id]
    # The scenario's ordered steps become Branch-owned instances.  Its label is
    # retained; customer_goal/object_types/status are Alternative Scenario data
    # and are intentionally not copied into the Branch domain object.
    return create_branch(
        result,
        branch_id=branch_id,
        label=scenario["label"],
        divergence_step_id=divergence_step_id,
        merge_step_id=merge_step_id,
        steps=scenario["steps"],
        topology_provenance=topology_provenance,
    )


def branch_to_scenario(
    journey: Mapping[str, Any],
    *,
    branch_id: str,
    scenario: Mapping[str, Any],
    rationale: str,
) -> dict[str, Any]:
    _require_rationale(rationale)
    result = _copy_journey(journey)
    branch = _branch(result, branch_id)
    if not isinstance(scenario, Mapping):
        _fail("Branch → Alternative Scenario 必須提供完整 scenario object")
    target = copy.deepcopy(dict(scenario))
    target["steps"] = copy.deepcopy(branch["steps"])
    result.setdefault("scenarios", []).append(target)
    result["branches"] = [item for item in result.get("branches") or [] if item.get("branch_id") != branch_id]
    return _normalize_and_validate(result)


def _rollback_journey_lineage(
    run: Path, ledger_path: Path, before: bytes | None, published: bytes | None,
) -> None:
    """Undo this publication's pointer lineage under the ledger lock.

    publish_batch has already released its locks when a later Journey or
    run-state write fails. If nobody touched the ledger since, restore the
    prior bytes atomically; otherwise drop only the derivations this
    publication appended so a concurrent gate write survives.
    """
    if published is None or published == before:
        return
    with gates._ledger_lock(run):
        current = ledger_path.read_bytes() if ledger_path.is_file() else None
        if current == published:
            lifecycle._restore_publication_file(ledger_path, before)
            return
        if current is None:
            return
        prior_ids = {
            item.get("derivation_id")
            for item in (yaml.safe_load(before) or {}).get("derivations", [])
        } if before is not None else set()
        added_ids = {
            item.get("derivation_id")
            for item in (yaml.safe_load(published) or {}).get("derivations", [])
        } - prior_ids
        ledger = yaml.safe_load(current) or {}
        ledger["derivations"] = [
            item for item in ledger.get("derivations", [])
            if item.get("derivation_id") not in added_ids
        ]
        gates._write_ledger_locked(run, ledger)


def _write_yaml_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as temp:
            temp_name = temp.name
            yaml.safe_dump(value, temp, allow_unicode=True, sort_keys=False)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink()
            except FileNotFoundError:
                pass


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as temp:
            temp_name = temp.name
            json.dump(value, temp, ensure_ascii=False, indent=2)
            temp.write("\n")
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink()
            except FileNotFoundError:
                pass


def commit_journey_operation(
    run_dir: str | Path,
    operation: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    *,
    operation_name: str,
    rationale: str | None = None,
    now: str | None = None,
    registry_path: str | Path | None = None,
    presentation_only_step_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Apply one complete Journey operation and publish it atomically.

    The operation callable is pure and receives the current canonical Journey.
    Registry history is written before the new pointer is committed, so a
    destructive edit cannot erase the prior complete representation.
    """
    if operation_name not in OPERATIONS:
        _fail(f"不支援的 Journey operation={operation_name!r}")
    if operation_name in CONVERSION_OPERATIONS:
        _require_rationale(rationale)
    run = Path(run_dir)
    journey_path = run / "journey.yaml"
    run_json_path = run / "run.json"
    lock_path = run / ".journey-operation.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        if not journey_path.is_file():
            _fail(f"找不到 canonical Journey：{journey_path}")
        current = yaml.safe_load(journey_path.read_text(encoding="utf-8")) or {}
        state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.is_file() else {}
        case_id = current.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            _fail("Journey 缺少 case_id")
        evidence_path = run / "evidence.yaml"
        evidence = yaml.safe_load(evidence_path.read_text(encoding="utf-8")) if evidence_path.is_file() else []
        evidence_ids = {item.get("id") for item in evidence or [] if isinstance(item, dict) and item.get("id")}
        # Validate the baseline without changing it.  This also rejects an
        # accidental operation against stale/invalid canonical input.
        baseline = _normalize_and_validate(current, evidence_ids=evidence_ids or None)
        updated = _normalize_and_validate(operation(copy.deepcopy(baseline)), evidence_ids=evidence_ids or None)
        if updated == baseline:
            _fail("Journey operation produced no change")
        declared_registry = registry_path or state.get("artifact_registry")
        registry_file = (
            Path(registry_path).expanduser().resolve()
            if registry_path is not None
            else storage_locations.resolve_registry_pointer(
                declared_registry, run, storage_locations.runs_root_for_run(run), case_id=case_id
            )
            if declared_registry
            else storage_locations.derive_registry_path(
                case_id, storage_locations.runs_root_for_run(run)
            )
        )
        if not registry_file.is_file():
            _fail(f"找不到 Journey lifecycle registry：{registry_file}")
        registry = yaml.safe_load(registry_file.read_text(encoding="utf-8")) or {}
        current_journey_record = next(
            (record for record in registry.get("versions", [])
             if record.get("artifact_type") == "confirmed_journey"
             and next((pointer for pointer in registry.get("current", [])
                       if pointer.get("artifact_type") == "confirmed_journey"), {}).get("version_ref") == record.get("version_id")),
            None,
        )
        if not current_journey_record:
            _fail("lifecycle registry 沒有 current confirmed_journey version")
        if current_journey_record.get("content_digest") != lifecycle.canonical_content_digest(baseline):
            _fail("canonical Journey 與 current lifecycle version 不一致")
        evidence_dep = next((dep for dep in current_journey_record.get("dependencies", []) if dep.get("role") == "evidence"), None)
        if not evidence_dep:
            _fail("current Journey version 缺少 evidence dependency")
        change = {"operation": operation_name}
        if rationale is not None:
            change["rationale"] = _require_rationale(rationale)
        now = now or dt.datetime.now().isoformat(timespec="seconds")
        if presentation_only_step_ids and current_journey_record.get("as_is_input_projection_digest"):
            # Preserve the already-published consumed-input fingerprint for an
            # explicitly presentation-only rename.  The current record was made
            # before this operation marker existed, so recomputing only the new
            # document would otherwise create a false As-is recheck.
            projection_digest = current_journey_record["as_is_input_projection_digest"]
        else:
            projection_digest = as_is_projection.as_is_input_projection_digest(
                updated,
                presentation_only_step_ids=presentation_only_step_ids or (),
            )
        before_journey = journey_path.read_bytes()
        before_state = run_json_path.read_bytes() if run_json_path.is_file() else None
        before_registry = registry_file.read_bytes()
        ledger_path = run / ".gates" / "ledger.yaml"
        before_ledger = ledger_path.read_bytes() if ledger_path.is_file() else None
        published_ledger = None
        try:
            publication = lifecycle.publish_batch(
                registry_file,
                [{
                    "key": "confirmed_journey",
                    "artifact_type": "confirmed_journey",
                    "case_id": case_id,
                    "content": updated,
                    "validate": lambda data: V.structural_errors(data, V.load_schema("journey")) + stages._journey_errors(data, evidence_ids, require_confirmed=True),
                    "dependencies": {"evidence": evidence_dep["version_ref"]},
                    "as_is_input_projection_digest": projection_digest,
                    "journey_change": change,
                }],
                now,
                journey_history={current_journey_record["version_id"]: baseline},
                ledger_path=ledger_path,
            )
            published_ledger = ledger_path.read_bytes() if ledger_path.is_file() else None
            _write_yaml_atomic(journey_path, updated)
            next_state = dict(state)
            versions = dict(next_state.get("artifact_versions") or {})
            versions["confirmed_journey"] = publication["version_ids"]["confirmed_journey"]
            next_state["artifact_versions"] = versions
            next_state["confirmed_journey_digest"] = lifecycle.canonical_content_digest(updated)
            next_state["as_is_input_projection_digest"] = projection_digest
            next_state["journey_state"] = "complete"
            _write_json_atomic(run_json_path, next_state)
            return {"journey": updated, "state": next_state, "publication": publication}
        except Exception:
            journey_path.write_bytes(before_journey)
            if before_state is not None:
                run_json_path.write_bytes(before_state)
            elif run_json_path.exists():
                run_json_path.unlink()
            registry_file.write_bytes(before_registry)
            _rollback_journey_lineage(run, ledger_path, before_ledger, published_ledger)
            raise
