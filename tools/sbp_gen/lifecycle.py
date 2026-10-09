"""Deterministic lifecycle registry for canonical SBP artifacts.

The registry is operational provenance for ordinary artifacts.  Confirmed
Journey versions additionally keep a complete immutable history snapshot so a
destructive Journey write can be recovered without adding a second domain
artifact or lifecycle state.  Validation never performs network/model work.
"""
from __future__ import annotations

import copy
import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import yaml
from jsonschema import Draft7Validator

from .as_is_projection import as_is_input_projection_digest


SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema" / "artifact-lifecycle.schema.json"
VERSION_RE = re.compile(r"^AV-[a-f0-9]{64}$")

# role -> required dependency artifact type
ARTIFACT_RULES: dict[str, dict[str, Any]] = {
    "evidence": {"scope": "case", "dependencies": {}},
    "confirmed_journey": {
        "scope": "case",
        "dependencies": {"evidence": "evidence"},
    },
    "as_is_graph": {
        "scope": "scenario",
        "dependencies": {
            "evidence": "evidence",
            "confirmed_journey": "confirmed_journey",
        },
    },
    "issue_package": {
        "scope": "scenario",
        "dependencies": {"evidence": "evidence", "as_is_graph": "as_is_graph"},
    },
    "research_catalog": {
        "scope": "scenario",
        "dependencies": {"issue_package": "issue_package"},
    },
    "to_be_bundle": {
        "scope": "scenario",
        "dependencies": {
            "as_is_graph": "as_is_graph",
            "issue_package": "issue_package",
            "research_catalog": "research_catalog",
        },
    },
    "pilot_record": {
        "scope": "scenario",
        "dependencies": {"to_be_bundle": "to_be_bundle"},
    },
    "validation_outcome": {
        "scope": "scenario",
        "dependencies": {"pilot_record": "pilot_record"},
    },
}


class LifecycleValidationError(ValueError):
    """Raised when a registry or publication candidate violates the contract."""


def _canonical_json(data: Any) -> str:
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_content_digest(data: Any) -> str:
    """Return the canonical SHA-256 digest of parsed YAML/JSON content."""
    encoded = _canonical_json(data).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _slot(record: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(record.get("artifact_type", "")),
        str(record.get("scope", "")),
        str(record.get("case_id", "")),
        str(record.get("scenario_id", "")),
    )


def _slot_label(slot: tuple[str, str, str, str]) -> str:
    artifact_type, scope, case_id, scenario_id = slot
    scenario = f", scenario_id={scenario_id!r}" if scenario_id else ""
    return f"artifact_type={artifact_type!r}, scope={scope!r}, case_id={case_id!r}{scenario}"


def build_version_record(
    *,
    artifact_type: str,
    case_id: str,
    content_digest: str,
    dependencies: Iterable[Mapping[str, str]],
    created_at: str,
    scenario_id: str | None = None,
    as_is_input_projection_digest: str | None = None,
) -> dict[str, Any]:
    """Build one immutable record; creation time intentionally does not affect its ID."""
    if artifact_type not in ARTIFACT_RULES:
        raise LifecycleValidationError(f"unknown artifact_type={artifact_type!r}")
    scope = ARTIFACT_RULES[artifact_type]["scope"]
    if scope == "case" and scenario_id is not None:
        raise LifecycleValidationError(
            f"artifact_type={artifact_type!r} is case-scoped and forbids scenario_id={scenario_id!r}"
        )
    if scope == "scenario" and not scenario_id:
        raise LifecycleValidationError(
            f"artifact_type={artifact_type!r} is scenario-scoped and requires scenario_id"
        )
    if as_is_input_projection_digest is not None and not re.fullmatch(
        r"sha256:[0-9a-f]{64}", as_is_input_projection_digest
    ):
        raise LifecycleValidationError(
            "as_is_input_projection_digest must be a sha256 digest"
        )
    ordered_dependencies = sorted(
        (dict(dependency) for dependency in dependencies),
        key=lambda item: (str(item.get("role", "")), str(item.get("version_ref", ""))),
    )
    identity: dict[str, Any] = {
        "artifact_type": artifact_type,
        "scope": scope,
        "case_id": case_id,
        "content_digest": content_digest,
        "dependencies": ordered_dependencies,
    }
    if scenario_id is not None:
        identity["scenario_id"] = scenario_id
    if as_is_input_projection_digest is not None:
        identity["as_is_input_projection_digest"] = as_is_input_projection_digest
    version_id = "AV-" + hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
    record = {"version_id": version_id, **identity, "created_at": created_at}
    # Keep the public record order human-readable and stable.
    record["dependencies"] = ordered_dependencies
    if as_is_input_projection_digest is not None:
        record["as_is_input_projection_digest"] = as_is_input_projection_digest
    return record


def _structural_errors(registry: Any) -> list[str]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    schema.pop("$id", None)
    errors = []
    for error in sorted(
        Draft7Validator(schema).iter_errors(registry),
        key=lambda item: (tuple(str(part) for part in item.absolute_path), item.message),
    ):
        location = "/".join(str(part) for part in error.absolute_path) or "(root)"
        errors.append(f"[structure] registry/{location}: {error.message}")
    return errors


def _cycle_errors(records: Mapping[str, Mapping[str, Any]]) -> list[str]:
    adjacency = {
        version_id: sorted(
            dependency.get("version_ref")
            for dependency in record.get("dependencies", [])
            if isinstance(dependency, dict)
            and isinstance(dependency.get("version_ref"), str)
            and dependency.get("version_ref") in records
        )
        for version_id, record in records.items()
    }
    visited: set[str] = set()
    active: set[str] = set()
    stack: list[str] = []
    found: set[tuple[str, ...]] = set()

    def visit(version_id: str) -> None:
        if version_id in active:
            start = stack.index(version_id)
            open_cycle = stack[start:]
            # Rotate to one deterministic representative. Descending version-ID
            # order matches the contract's AV-I1 → AV-G1 → AV-I1 example and
            # avoids emitting one message per possible rotation.
            anchor = max(range(len(open_cycle)), key=lambda index: open_cycle[index])
            rotated = open_cycle[anchor:] + open_cycle[:anchor]
            cycle = tuple(rotated + [rotated[0]])
            found.add(cycle)
            return
        if version_id in visited:
            return
        active.add(version_id)
        stack.append(version_id)
        for dependency_id in adjacency.get(version_id, []):
            visit(dependency_id)
        stack.pop()
        active.remove(version_id)
        visited.add(version_id)

    for version_id in sorted(records):
        visit(version_id)
    return [f"[cycle] registry/versions: {' → '.join(cycle)}" for cycle in sorted(found)]


def lifecycle_errors(registry: Any) -> list[str]:
    """Return deterministic structural and referential errors without mutation."""
    structure = _structural_errors(registry)
    if not isinstance(registry, dict):
        return structure

    semantic: list[str] = []
    root_case = registry.get("case_id")
    versions = registry.get("versions") if isinstance(registry.get("versions"), list) else []
    pointers = registry.get("current") if isinstance(registry.get("current"), list) else []
    journey_history = registry.get("journey_history") if isinstance(registry.get("journey_history"), dict) else {}
    journey_changes = registry.get("journey_changes") if isinstance(registry.get("journey_changes"), dict) else {}

    records: dict[str, Mapping[str, Any]] = {}
    duplicate_ids: set[str] = set()
    for index, record in enumerate(versions):
        if not isinstance(record, dict):
            continue
        version_id = record.get("version_id")
        if not isinstance(version_id, str):
            continue
        if version_id in records:
            duplicate_ids.add(version_id)
        else:
            records[version_id] = record
        if record.get("case_id") != root_case:
            semantic.append(
                f"[boundary] registry/versions/{index} version={version_id!r} "
                f"case_id={record.get('case_id')!r} does not match registry case_id={root_case!r}"
            )
    for version_id in sorted(duplicate_ids):
        semantic.append(f"[identity] registry/versions version_id={version_id!r} is duplicated")

    for history_id, snapshot in sorted(journey_history.items()):
        if not VERSION_RE.fullmatch(str(history_id)):
            semantic.append(f"[history] journey_history key={history_id!r} is not an AV-* version id")
            continue
        if not isinstance(snapshot, dict):
            semantic.append(f"[history] journey_history/{history_id} must be a Journey object")
        elif snapshot.get("case_id") is not None and snapshot.get("case_id") != root_case:
            semantic.append(
                f"[history] journey_history/{history_id} case_id={snapshot.get('case_id')!r} "
                f"does not match registry case_id={root_case!r}"
            )
    for change_id, change in sorted(journey_changes.items()):
        if not VERSION_RE.fullmatch(str(change_id)):
            semantic.append(f"[history] journey_changes key={change_id!r} is not an AV-* version id")
        elif not isinstance(change, dict) or not isinstance(change.get("operation"), str) or not change.get("operation"):
            semantic.append(f"[history] journey_changes/{change_id} requires operation")
        elif change.get("operation") in {"scenario_to_branch", "branch_to_scenario"} and not str(change.get("rationale") or "").strip():
            semantic.append(f"[history] journey_changes/{change_id} conversion requires rationale")

    for version_id, record in sorted(records.items()):
        artifact_type = record.get("artifact_type")
        rule = ARTIFACT_RULES.get(artifact_type)
        if rule is None:
            continue
        dependencies = record.get("dependencies") if isinstance(record.get("dependencies"), list) else []
        dependency_dicts = [item for item in dependencies if isinstance(item, dict)]
        roles = [item.get("role") for item in dependency_dicts if isinstance(item.get("role"), str)]
        duplicate_roles = sorted(role for role in set(roles) if roles.count(role) > 1)
        for role in duplicate_roles:
            semantic.append(
                f"[dependency] registry/version={version_id!r} role={role!r} occurs more than once"
            )
        required = set(rule["dependencies"])
        actual = set(roles)
        for role in sorted(required - actual):
            semantic.append(
                f"[dependency] registry/version={version_id!r} missing required role={role!r}"
            )
        for role in sorted(actual - required):
            semantic.append(
                f"[dependency] registry/version={version_id!r} has illegal role={role!r}"
            )

        for dependency in sorted(
            dependency_dicts,
            key=lambda item: (str(item.get("role", "")), str(item.get("version_ref", ""))),
        ):
            role = dependency.get("role")
            reference = dependency.get("version_ref")
            if not isinstance(reference, str) or reference not in records:
                semantic.append(
                    f"[reference] registry/version={version_id!r} role={role!r} "
                    f"version_ref={reference!r} is unresolved"
                )
                continue
            target = records[reference]
            expected_type = rule["dependencies"].get(role)
            if expected_type and target.get("artifact_type") != expected_type:
                semantic.append(
                    f"[dependency] registry/version={version_id!r} role={role!r} "
                    f"version_ref={reference!r} targets artifact_type={target.get('artifact_type')!r}, "
                    f"expected {expected_type!r}"
                )
            if dependency.get("content_digest") != target.get("content_digest"):
                semantic.append(
                    f"[digest] registry/version={version_id!r} role={role!r} "
                    f"content_digest={dependency.get('content_digest')!r} does not match "
                    f"version_ref={reference!r} digest={target.get('content_digest')!r}"
                )
            if target.get("case_id") != record.get("case_id"):
                semantic.append(
                    f"[boundary] registry/version={version_id!r} role={role!r} "
                    f"version_ref={reference!r} case_id={target.get('case_id')!r} does not match "
                    f"owner case_id={record.get('case_id')!r}"
                )
            if target.get("scope") == "scenario" and (
                target.get("scenario_id") != record.get("scenario_id")
            ):
                semantic.append(
                    f"[boundary] registry/version={version_id!r} role={role!r} "
                    f"version_ref={reference!r} scenario_id={target.get('scenario_id')!r} does not match "
                    f"owner scenario_id={record.get('scenario_id')!r}"
                )

        identity_fields = {
            "artifact_type": artifact_type,
            "case_id": record.get("case_id"),
            "scenario_id": record.get("scenario_id"),
            "content_digest": record.get("content_digest"),
            "dependencies": dependency_dicts,
            "created_at": record.get("created_at"),
        }
        if record.get("as_is_input_projection_digest") is not None:
            identity_fields["as_is_input_projection_digest"] = record.get(
                "as_is_input_projection_digest"
            )
        try:
            rebuilt = build_version_record(**identity_fields)
        except (LifecycleValidationError, TypeError, ValueError):
            rebuilt = None
        if rebuilt is not None and rebuilt["version_id"] != version_id:
            semantic.append(
                f"[identity] registry/version={version_id!r} version_id does not match "
                f"canonical identity {rebuilt['version_id']!r}"
            )
        if artifact_type != "confirmed_journey":
            if version_id in journey_history:
                semantic.append(f"[history] journey_history/{version_id} must reference confirmed_journey")
            if version_id in journey_changes:
                semantic.append(f"[history] journey_changes/{version_id} must reference confirmed_journey")
        elif version_id in journey_history:
            snapshot = journey_history[version_id]
            if isinstance(snapshot, dict) and canonical_content_digest(snapshot) != record.get("content_digest"):
                semantic.append(f"[history] journey_history/{version_id} digest does not match content_digest")

    for history_id in sorted(set(journey_history) - set(records)):
        semantic.append(f"[history] journey_history/{history_id} references an unknown version")
    for change_id in sorted(set(journey_changes) - set(records)):
        semantic.append(f"[history] journey_changes/{change_id} references an unknown version")

    pointer_slots: dict[tuple[str, str, str, str], int] = {}
    pointed_slots: set[tuple[str, str, str, str]] = set()
    for index, pointer in enumerate(pointers):
        if not isinstance(pointer, dict):
            continue
        slot = _slot(pointer)
        if slot in pointer_slots:
            semantic.append(
                f"[pointer] registry/current/{index} duplicates logical slot {_slot_label(slot)}"
            )
        else:
            pointer_slots[slot] = index
        if pointer.get("case_id") != root_case:
            semantic.append(
                f"[boundary] registry/current/{index} case_id={pointer.get('case_id')!r} "
                f"does not match registry case_id={root_case!r}"
            )
        reference = pointer.get("version_ref")
        target = records.get(reference) if isinstance(reference, str) else None
        if target is None:
            semantic.append(
                f"[pointer] registry/current/{index} version_ref={reference!r} is unresolved"
            )
        elif _slot(target) != slot:
            semantic.append(
                f"[pointer] registry/current/{index} version_ref={reference!r} targets "
                f"{_slot_label(_slot(target))}, not {_slot_label(slot)}"
            )
        else:
            pointed_slots.add(slot)

    record_slots = {_slot(record) for record in records.values()}
    for slot in sorted(record_slots - pointed_slots):
        semantic.append(f"[pointer] registry/current missing logical slot {_slot_label(slot)}")

    semantic.extend(_cycle_errors(records))
    return structure + sorted(set(semantic))


def resolve_lifecycle(registry: Mapping[str, Any]) -> dict[str, str]:
    """Derive current/stale/superseded for every immutable version record."""
    errors = lifecycle_errors(registry)
    if errors:
        raise LifecycleValidationError("invalid artifact lifecycle registry:\n" + "\n".join(errors))
    records = {record["version_id"]: record for record in registry["versions"]}
    selected = {_slot(pointer): pointer["version_ref"] for pointer in registry["current"]}
    memo: dict[str, str] = {}
    active: set[str] = set()

    def dependency_is_current(record: Mapping[str, Any], dependency: Mapping[str, Any]) -> bool:
        """Resolve As-is freshness from its consumed-input projection.

        Legacy records without the explicit projection metadata retain the
        previous whole-Journey dependency behavior.  New records can remain
        current across a Journey version change when the consumed projection
        is unchanged, while the Journey version reference remains available for
        audit/history.
        """
        if (
            record.get("artifact_type") == "as_is_graph"
            and dependency.get("role") == "confirmed_journey"
        ):
            graph_projection = record.get("as_is_input_projection_digest")
            target = records.get(dependency.get("version_ref"))
            target_projection = target.get("as_is_input_projection_digest") if target else None
            current_journey = next(
                (
                    item
                    for item in records.values()
                    if item.get("artifact_type") == "confirmed_journey"
                    and selected.get(_slot(item)) == item.get("version_id")
                ),
                None,
            )
            if graph_projection and current_journey:
                current_projection = current_journey.get("as_is_input_projection_digest")
                if current_projection:
                    return graph_projection == current_projection
            if graph_projection and target_projection and target_projection == graph_projection:
                return True
        return status(dependency["version_ref"]) == "current"

    def status(version_id: str) -> str:
        if version_id in memo:
            return memo[version_id]
        record = records[version_id]
        if selected[_slot(record)] != version_id:
            memo[version_id] = "superseded"
            return "superseded"
        if version_id in active:
            raise LifecycleValidationError(f"dependency cycle encountered at version={version_id!r}")
        active.add(version_id)
        result = "current"
        for dependency in record["dependencies"]:
            if not dependency_is_current(record, dependency):
                result = "stale"
                break
        active.remove(version_id)
        memo[version_id] = result
        return result

    ordered = sorted(
        records.values(),
        key=lambda record: (
            record["scope"],
            record.get("scenario_id", ""),
            record["artifact_type"],
            record["version_id"],
        ),
    )
    return {record["version_id"]: status(record["version_id"]) for record in ordered}


def _set_pointer(registry: dict[str, Any], record: Mapping[str, Any]) -> None:
    slot = _slot(record)
    pointer = {
        "artifact_type": record["artifact_type"],
        "scope": record["scope"],
        "case_id": record["case_id"],
    }
    if record["scope"] == "scenario":
        pointer["scenario_id"] = record["scenario_id"]
    pointer["version_ref"] = record["version_id"]
    for index, existing in enumerate(registry["current"]):
        if _slot(existing) == slot:
            registry["current"][index] = pointer
            return
    registry["current"].append(pointer)


def _publication_error(key: Any, message: str) -> LifecycleValidationError:
    return LifecycleValidationError(f"publication artifact={key!r}: {message}")


def _pointer_lineage(
    before: Mapping[str, Any], after: Mapping[str, Any], now: str, ledger: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Describe only actual current-pointer moves, without changing registry IDs."""
    old_slots = {_slot(item): item["version_ref"] for item in before.get("current", [])}
    new_slots = {_slot(item): item["version_ref"] for item in after.get("current", [])}
    old_versions = {item["version_id"]: item for item in before.get("versions", [])}
    new_versions = {item["version_id"]: item for item in after.get("versions", [])}
    prior_lineage = {
        _slot(item): item["derivation_id"]
        for item in ledger.get("derivations", [])
        if item.get("name") == "artifact_current_pointer"
    }
    entries = []
    for slot, next_ref in sorted(new_slots.items()):
        prior_ref = old_slots.get(slot)
        if prior_ref == next_ref:
            continue
        artifact_type, scope, case_id, scenario_id = slot
        identity = {
            "name": "artifact_current_pointer",
            "artifact_type": artifact_type,
            "scope": scope,
            "case_id": case_id,
            "previous_lineage_ref": prior_lineage.get(slot),
            "previous": (
                {"version_ref": prior_ref, "content_digest": old_versions[prior_ref]["content_digest"]}
                if prior_ref else None
            ),
            "current": {
                "version_ref": next_ref,
                "content_digest": new_versions[next_ref]["content_digest"],
            },
        }
        if scenario_id:
            identity["scenario_id"] = scenario_id
        digest = canonical_content_digest(identity).split(":", 1)[1]
        entries.append({
            "derivation_id": f"DR-{digest}",
            **identity,
            "inputs": [],
            "produced_at": now,
            "recorded_by": "system",
        })
    return entries


def _lineage_ledger(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema_version": 1, "confirmations": [], "derivations": [], "operations": []}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or loaded.get("schema_version") != 1:
        raise LifecycleValidationError(f"invalid owning run ledger: {path}")
    for field in ("confirmations", "derivations", "operations"):
        if field in loaded and not isinstance(loaded[field], list):
            raise LifecycleValidationError(f"invalid owning run ledger {field}: {path}")
        loaded.setdefault(field, [])
    return loaded


def _restore_publication_file(path: Path, previous: bytes | None) -> None:
    """Restore a file under its held lock after an ordinary second-write error."""
    if previous is None:
        path.unlink(missing_ok=True)
        return
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".rollback", delete=False) as temporary:
            temporary_name = temporary.name
            temporary.write(previous)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    except OSError:
        # The original bytes still take priority if atomic replacement itself
        # is unavailable; both publication locks remain held during recovery.
        path.write_bytes(previous)
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def publish_batch(
    registry_path: str | Path,
    artifacts: Iterable[Mapping[str, Any]],
    now: str,
    *,
    precommit: Callable[[Mapping[str, Any]], None] | None = None,
    journey_history: Mapping[str, Any] | None = None,
    ledger_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate and atomically publish one ordered artifact batch.

    Dependencies are registered ``AV-*`` IDs or ``@key`` references to an
    earlier item in this same batch.  The caller cannot provide digests or IDs.
    """
    path = Path(registry_path)
    supplied = list(artifacts)
    if not supplied:
        raise LifecycleValidationError("publication batch must contain at least one artifact")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    owning_ledger = Path(ledger_path) if ledger_path is not None else None
    if owning_ledger is not None and (
        owning_ledger.name != "ledger.yaml" or owning_ledger.parent.name != ".gates"
    ):
        raise LifecycleValidationError("owning ledger path must end in .gates/ledger.yaml")

    with lock_path.open("a+", encoding="utf-8") as lock_file, contextlib.ExitStack() as held_locks:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise LifecycleValidationError(f"registry lock failed for {lock_path}: {exc}") from exc

        if owning_ledger is not None:
            from . import gates
            held_locks.enter_context(gates._ledger_lock(owning_ledger.parent.parent))
            prior_ledger_bytes = owning_ledger.read_bytes() if owning_ledger.is_file() else None
            ledger = _lineage_ledger(owning_ledger)
        else:
            prior_ledger_bytes = None
            ledger = None
        prior_registry_bytes = path.read_bytes() if path.is_file() else None

        if path.exists():
            registry = yaml.safe_load(path.read_text(encoding="utf-8"))
            existing_errors = lifecycle_errors(registry)
            if existing_errors:
                raise LifecycleValidationError(
                    "existing registry is invalid:\n" + "\n".join(existing_errors)
                )
        else:
            first_case = supplied[0].get("case_id")
            registry = {"schema_version": 1, "case_id": first_case, "versions": [], "current": []}

        if precommit is not None:
            precommit(copy.deepcopy(registry))

        before = copy.deepcopy(registry)
        candidate = copy.deepcopy(registry)
        if journey_history:
            existing_history = candidate.setdefault("journey_history", {})
            if not isinstance(existing_history, dict):
                raise LifecycleValidationError("journey_history must be an object")
            for version_id, snapshot in journey_history.items():
                if version_id in existing_history and existing_history[version_id] != snapshot:
                    raise LifecycleValidationError(
                        f"journey_history already contains conflicting version={version_id!r}"
                    )
                existing_history[version_id] = copy.deepcopy(snapshot)
        by_id = {record["version_id"]: record for record in candidate["versions"]}
        batch_ids: dict[str, str] = {}
        batch_contents = {
            item.get("key"): item.get("content")
            for item in supplied
            if isinstance(item, Mapping) and isinstance(item.get("key"), str)
        }

        for artifact in supplied:
            key = artifact.get("key")
            artifact_type = artifact.get("artifact_type")
            case_id = artifact.get("case_id")
            scenario_id = artifact.get("scenario_id")
            if not isinstance(key, str) or not key or key in batch_ids:
                raise _publication_error(key, "key must be a unique non-empty string")
            if artifact_type not in ARTIFACT_RULES:
                raise _publication_error(key, f"unknown artifact_type={artifact_type!r}")
            if case_id != candidate.get("case_id"):
                raise _publication_error(
                    key,
                    f"case_id={case_id!r} does not match registry case_id={candidate.get('case_id')!r}",
                )
            forbidden = sorted(set(artifact) & {"version_id", "content_digest", "created_at"})
            if forbidden:
                raise _publication_error(key, f"caller-supplied authoritative fields are forbidden: {forbidden}")
            validator = artifact.get("validate")
            if not callable(validator):
                raise _publication_error(key, "validate must be a callable content validator")
            content_errors = list(validator(artifact.get("content")) or [])
            if content_errors:
                raise _publication_error(key, "content validation failed: " + "; ".join(map(str, content_errors)))

            raw_dependencies = artifact.get("dependencies", {})
            if not isinstance(raw_dependencies, dict):
                raise _publication_error(key, f"dependencies={raw_dependencies!r} must be a role mapping")
            required_roles = set(ARTIFACT_RULES[artifact_type]["dependencies"])
            actual_roles = set(raw_dependencies)
            if actual_roles != required_roles:
                missing = sorted(required_roles - actual_roles)
                extra = sorted(actual_roles - required_roles)
                raise _publication_error(key, f"dependency roles missing={missing}, extra={extra}")

            projection_digest = artifact.get("as_is_input_projection_digest")
            if artifact_type == "confirmed_journey":
                content = artifact.get("content")
                if isinstance(content, Mapping) and content.get("schema_version") in (2, 3):
                    # A canonical write operation may supply the deterministic
                    # projection computed with an explicit presentation-only
                    # operation marker.  Without that marker, derive it from
                    # the complete Journey content and fail closed.
                    if projection_digest is None:
                        projection_digest = as_is_input_projection_digest(content)
            elif artifact_type == "as_is_graph" and projection_digest is None:
                journey_ref = raw_dependencies.get("confirmed_journey")
                journey_content = None
                if isinstance(journey_ref, str) and journey_ref.startswith("@"):
                    journey_content = batch_contents.get(journey_ref[1:])
                elif isinstance(journey_ref, str) and journey_ref in by_id:
                    projection_digest = by_id[journey_ref].get("as_is_input_projection_digest")
                if isinstance(journey_content, Mapping) and journey_content.get("schema_version") in (2, 3):
                    projection_digest = as_is_input_projection_digest(journey_content)
            if projection_digest is not None and not re.fullmatch(
                r"sha256:[0-9a-f]{64}", str(projection_digest)
            ):
                raise _publication_error(key, "as_is_input_projection_digest must be a sha256 digest")

            dependencies = []
            for role in sorted(raw_dependencies):
                raw_reference = raw_dependencies[role]
                if not isinstance(raw_reference, str):
                    raise _publication_error(key, f"role={role!r} dependency={raw_reference!r} is not a registered AV-* ID")
                if raw_reference.startswith("@"):
                    local_key = raw_reference[1:]
                    if local_key not in batch_ids:
                        raise _publication_error(
                            key,
                            f"role={role!r} dependency={raw_reference!r} does not name an earlier batch artifact",
                        )
                    reference = batch_ids[local_key]
                else:
                    reference = raw_reference
                if not VERSION_RE.fullmatch(reference):
                    raise _publication_error(
                        key,
                        f"role={role!r} dependency={raw_reference!r} is not a registered AV-* version reference",
                    )
                target = by_id.get(reference)
                if target is None:
                    raise _publication_error(
                        key,
                        f"role={role!r} missing registered version={reference!r}",
                    )
                expected_type = ARTIFACT_RULES[artifact_type]["dependencies"][role]
                if target["artifact_type"] != expected_type:
                    raise _publication_error(
                        key,
                        f"role={role!r} version={reference!r} targets {target['artifact_type']!r}, expected {expected_type!r}",
                    )
                if target["case_id"] != case_id:
                    raise _publication_error(key, f"role={role!r} version={reference!r} crosses case boundary")
                if target["scope"] == "scenario" and target.get("scenario_id") != scenario_id:
                    raise _publication_error(key, f"role={role!r} version={reference!r} crosses scenario boundary")
                statuses = resolve_lifecycle(candidate)
                if statuses[reference] != "current":
                    raise _publication_error(
                        key,
                        f"role={role!r} version={reference!r} is {statuses[reference]!r}, not current",
                    )
                dependencies.append(
                    {
                        "role": role,
                        "version_ref": reference,
                        "content_digest": target["content_digest"],
                    }
                )

            digest = canonical_content_digest(artifact.get("content"))
            record = build_version_record(
                artifact_type=artifact_type,
                case_id=case_id,
                scenario_id=scenario_id,
                content_digest=digest,
                dependencies=dependencies,
                created_at=now,
                as_is_input_projection_digest=projection_digest,
            )
            existing = by_id.get(record["version_id"])
            if existing is not None:
                comparable_existing = dict(existing)
                comparable_new = dict(record)
                comparable_existing.pop("created_at", None)
                comparable_new.pop("created_at", None)
                if comparable_existing != comparable_new:
                    raise _publication_error(key, f"conflicting duplicate version_id={record['version_id']!r}")
                record = existing
            else:
                candidate["versions"].append(record)
                by_id[record["version_id"]] = record
            if artifact_type == "confirmed_journey":
                history = candidate.setdefault("journey_history", {})
                prior = history.get(record["version_id"])
                if prior is not None and prior != artifact.get("content"):
                    raise _publication_error(key, "journey history conflicts with immutable content")
                history[record["version_id"]] = copy.deepcopy(artifact.get("content"))
                change = artifact.get("journey_change")
                if change is not None:
                    if not isinstance(change, Mapping) or not isinstance(change.get("operation"), str):
                        raise _publication_error(key, "journey_change requires an operation")
                    changes = candidate.setdefault("journey_changes", {})
                    previous_change = changes.get(record["version_id"])
                    if previous_change is not None and previous_change != change:
                        raise _publication_error(key, "journey change metadata conflicts with immutable history")
                    changes[record["version_id"]] = copy.deepcopy(dict(change))
            _set_pointer(candidate, record)
            batch_ids[key] = record["version_id"]

        candidate["versions"] = sorted(candidate["versions"], key=lambda item: item["version_id"])
        candidate["current"] = sorted(candidate["current"], key=_slot)
        candidate_errors = lifecycle_errors(candidate)
        if candidate_errors:
            raise LifecycleValidationError("publication candidate is invalid:\n" + "\n".join(candidate_errors))
        lifecycle = resolve_lifecycle(candidate)

        lineage = _pointer_lineage(before, candidate, now, ledger) if ledger is not None else []
        if lineage:
            ledger["derivations"].extend(lineage)

        if candidate != before:
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
                    yaml.safe_dump(candidate, temporary, allow_unicode=True, sort_keys=False)
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
        if lineage:
            try:
                gates._write_ledger_locked(owning_ledger.parent.parent, ledger)
            except Exception:
                if candidate != before:
                    _restore_publication_file(path, prior_registry_bytes)
                if owning_ledger.is_file() and (
                    prior_ledger_bytes is None or owning_ledger.read_bytes() != prior_ledger_bytes
                ):
                    _restore_publication_file(owning_ledger, prior_ledger_bytes)
                raise
        return {"version_ids": batch_ids, "lifecycle": lifecycle, "registry": candidate}


def journey_version_content(registry: Mapping[str, Any], version_id: str) -> Any:
    """Return the complete immutable Journey snapshot for one version.

    History is deliberately separate from the version record: the record keeps
    identity/dependency metadata while this ledger preserves the full canonical
    representation needed to recover a destructive Journey edit.
    """
    history = registry.get("journey_history") if isinstance(registry, Mapping) else None
    if not isinstance(history, Mapping) or version_id not in history:
        raise LifecycleValidationError(f"Journey history is unavailable for version={version_id!r}")
    return copy.deepcopy(history[version_id])


def journey_change_metadata(registry: Mapping[str, Any], version_id: str) -> dict[str, Any] | None:
    """Return immutable operation metadata associated with a Journey version."""
    changes = registry.get("journey_changes") if isinstance(registry, Mapping) else None
    value = changes.get(version_id) if isinstance(changes, Mapping) else None
    return copy.deepcopy(value) if isinstance(value, Mapping) else None
