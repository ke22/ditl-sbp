"""Human-gate confirmation: preview -> apply -> rebuild-to-next-gate -> stop.

Decision 19 A2 (2026-09-17) retires Decision 16's "upstream changed, delete
and rerun everything downstream" behaviour
(``tools/sbp_review/server.py``'s old ``PERSONA_DOWNSTREAM`` /
``_discard_persona_downstream`` / ``_run_persona_chain``). A confirmed
selection's lineage on mutation is: preview the impact (pure, no writes) ->
apply (locked, atomic ledger append) -> rebuild only the derivations the
resolver says are reachable -> stop at the first gate that is not itself
confirmed. Nothing is ever deleted; a superseded record and the content it
referenced stay recoverable.

This module is the *confirmation service* (``.claude/rules/coding-
boundaries.md``: domain semantics belong here, not in ``server.py``, which
only orchestrates HTTP and background jobs). ``tools/sbp_gen/lifecycle.py``
stays the case-level artifact-version registry, untouched -- this module
borrows only its digest function and its current/stale/superseded rule (see
``LifecycleParityTest`` in ``tools/tests/test_gates.py``), not its registry
or its ``publish_batch`` machinery, which does not fit pre-blueprint,
per-run, manual-transport-friendly artifacts.

Dependency map scope for this change: the Persona station only
(``segmentation_axes``, ``target_segmentation`` gates; ``target_segmentation
_candidates``, ``persona_content`` derivations). Other stations register
their own ``GateSpec``/``DerivationSpec`` entries in a follow-up change --
the resolver functions below read a ``DependencyMap`` as data and contain no
station-specific identifiers in their own control flow.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, NamedTuple

import yaml
from yaml_loader import safe_load as load_safe_yaml

from . import lifecycle

GateState = Literal["confirmed", "awaiting_confirmation", "generating", "failed", "draft"]
LineageStatus = Literal["current", "stale", "absent"]

GATES_DIRNAME = ".gates"
LEDGER_FILENAME = "ledger.yaml"
TRANSPORT_DIRNAME = "_transport"


# --------------------------------------------------------------------------
# Dependency-map vocabulary: gates, derivations, artifacts.
# --------------------------------------------------------------------------


class GateRef(NamedTuple):
    name: str


class DerivationRef(NamedTuple):
    name: str


class ArtifactRef(NamedTuple):
    name: str


Ref = GateRef | DerivationRef | ArtifactRef


def gate(name: str) -> GateRef:
    return GateRef(name)


def derivation(name: str) -> DerivationRef:
    return DerivationRef(name)


def artifact(name: str) -> ArtifactRef:
    return ArtifactRef(name)


@dataclass(frozen=True)
class CliOperation:
    """How a derivation is produced. Metadata only -- ``gates.py`` never
    shells out itself; a caller (CLI wiring or ``server.py``) runs this and
    hands the result to ``run_rebuild`` through a ``StepOutcome``."""

    command: str
    args: tuple[str, ...] = ()


def cli(command: str, *args: str) -> CliOperation:
    return CliOperation(command=command, args=args)


@dataclass(frozen=True)
class GateSpec:
    selection_file: str
    inputs: tuple[Ref, ...]
    validator: str  # dotted path, informational -- callers own validation


@dataclass(frozen=True)
class DerivationSpec:
    file: str
    inputs: tuple[Ref, ...]
    operation: CliOperation
    transport_stage: str


@dataclass(frozen=True)
class DependencyMap:
    gates: Mapping[str, GateSpec]
    derivations: Mapping[str, DerivationSpec]


GATES: dict[str, GateSpec] = {
    "segmentation_axes": GateSpec(
        selection_file="segmentation_axis_selection.yaml",
        inputs=(artifact("persona_matrix"),),
        validator="stages._segmentation_axis_selection_errors",
    ),
    "target_segmentation": GateSpec(
        selection_file="target_segmentation_selection.yaml",
        inputs=(gate("segmentation_axes"), artifact("persona_matrix")),
        validator="stages._target_segmentation_selection_errors",
    ),
}

DERIVATIONS: dict[str, DerivationSpec] = {
    "target_segmentation_candidates": DerivationSpec(
        file="target_segmentation_candidates.yaml",
        inputs=(gate("segmentation_axes"), artifact("persona_matrix")),
        operation=cli("target-segmentation", "--propose"),
        transport_stage="target_segmentation",
    ),
    "persona_content": DerivationSpec(
        file="persona_content.yaml",
        inputs=(gate("target_segmentation"), artifact("persona_matrix")),
        operation=cli("persona-content"),
        transport_stage="persona_content",
    ),
}

ARTIFACT_FILES: dict[str, str] = {
    "persona_matrix": "persona_matrix.yaml",
}

PERSONA_DEPENDENCY_MAP = DependencyMap(gates=GATES, derivations=DERIVATIONS)


# --------------------------------------------------------------------------
# Resolver: which derivations a mutation's rebuild plan reaches, and the
# first gate it cannot cross. No station-specific literals below this line.
# --------------------------------------------------------------------------


def affected(depmap: DependencyMap, source: Ref) -> set[Ref]:
    """Transitive consumers of ``source`` in ``depmap`` -- everything that
    becomes stale as a direct or indirect consequence of mutating ``source``.
    Does not include ``source`` itself."""
    result: set[Ref] = {source}
    changed = True
    while changed:
        changed = False
        for name, spec in depmap.gates.items():
            ref = gate(name)
            if ref in result:
                continue
            if any(inp in result for inp in spec.inputs):
                result.add(ref)
                changed = True
        for name, spec in depmap.derivations.items():
            ref = derivation(name)
            if ref in result:
                continue
            if any(inp in result for inp in spec.inputs):
                result.add(ref)
                changed = True
    result.discard(source)
    return result


def rebuild_plan_and_stop(
    depmap: DependencyMap,
    source: GateRef,
    status: Callable[[Ref], LineageStatus],
) -> tuple[list[str], str | None]:
    """Derivations reachable from mutating ``source``, in the order they
    become runnable, and the first gate the expansion could not cross.

    ``status`` reports the *current* (pre-mutation) lineage of any ref that
    is not itself affected by this mutation -- artifacts, and gates/
    derivations entirely unrelated to ``source``. Every ref inside
    ``affected(depmap, source)`` is treated as not-current by construction:
    that is exactly what "affected" means. Gates are never satisfied
    automatically -- a derivation whose gate input is affected (and
    therefore not current) blocks there, and that gate becomes the stop
    point; it is never silently treated as confirmed by this mutation.
    """
    scope = affected(depmap, source)
    order = [name for name in depmap.derivations if derivation(name) in scope]

    def is_current(ref: Ref) -> bool:
        if ref in scope:
            return False
        return status(ref) == "current"

    resolved: set[Ref] = {source}
    plan: list[str] = []
    stops_at: str | None = None
    progressed = True
    while progressed:
        progressed = False
        for name in order:
            if name in plan:
                continue
            spec = depmap.derivations[name]
            ready = True
            blocking_gate: str | None = None
            for ref in spec.inputs:
                if ref in resolved:
                    continue
                if isinstance(ref, DerivationRef) and ref.name in plan:
                    resolved.add(ref)
                    continue
                if is_current(ref):
                    resolved.add(ref)
                    continue
                ready = False
                if isinstance(ref, GateRef):
                    blocking_gate = ref.name
                break
            if ready:
                plan.append(name)
                resolved.add(derivation(name))
                progressed = True
            elif blocking_gate is not None and stops_at is None:
                stops_at = blocking_gate
    return plan, stops_at


# --------------------------------------------------------------------------
# Ledger: append-only confirmations/derivations/operations, locked atomic
# read-modify-write, same tempfile + os.replace pattern as lifecycle.py.
# --------------------------------------------------------------------------


def _gates_dir(run_dir: Path) -> Path:
    return run_dir / GATES_DIRNAME


def _ledger_path(run_dir: Path) -> Path:
    return _gates_dir(run_dir) / LEDGER_FILENAME


def _content_dir(run_dir: Path) -> Path:
    return _gates_dir(run_dir) / "content"


def _transport_archive_dir(run_dir: Path) -> Path:
    return _gates_dir(run_dir) / "transport"


def _content_id(prefix: str, payload: Any) -> str:
    """Content-addressed id, same digest function ``lifecycle.py`` uses for
    its own ``AV-*`` ids, just a different prefix per record kind."""
    digest = lifecycle.canonical_content_digest(payload)
    return f"{prefix}-{digest.split(':', 1)[1]}"


def _empty_ledger() -> dict[str, Any]:
    return {"schema_version": 1, "confirmations": [], "derivations": [], "operations": []}


def _read_ledger(run_dir: Path) -> dict[str, Any]:
    path = _ledger_path(run_dir)
    if not path.is_file():
        return _empty_ledger()
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return _empty_ledger()
    loaded.setdefault("confirmations", [])
    loaded.setdefault("derivations", [])
    loaded.setdefault("operations", [])
    return loaded


class _LedgerLock:
    """Holds ``.gates/ledger.yaml.lock`` for one read-modify-write. Narrow
    scope on purpose: a slow model call (``run_rebuild``'s runner) happens
    *outside* any held lock, so an unrelated preview or read is never
    blocked by an in-progress generation."""

    def __init__(self, run_dir: Path) -> None:
        self._run_dir = run_dir
        self._file = None

    def __enter__(self) -> "_LedgerLock":
        _gates_dir(self._run_dir).mkdir(parents=True, exist_ok=True)
        lock_path = _ledger_path(self._run_dir).with_name(LEDGER_FILENAME + ".lock")
        self._file = lock_path.open("a+", encoding="utf-8")
        fcntl.flock(self._file.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None


def _ledger_lock(run_dir: Path) -> _LedgerLock:
    return _LedgerLock(run_dir)


def _write_ledger_locked(run_dir: Path, ledger: dict[str, Any]) -> None:
    """Caller must already hold ``_ledger_lock(run_dir)``."""
    path = _ledger_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=path.name + ".", suffix=".tmp", delete=False,
        ) as temporary:
            temporary_name = temporary.name
            yaml.safe_dump(ledger, temporary, allow_unicode=True, sort_keys=False)
            temporary.flush()
            os.fsync(temporary.fileno())
        Path(temporary_name).replace(path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Legacy import: a run with selection files but no ledger gets one
# synthesized in memory on first preview, and persisted on first apply.
# --------------------------------------------------------------------------


def _artifact_digest(run_dir: Path, artifact_name: str) -> str | None:
    file_name = ARTIFACT_FILES.get(artifact_name)
    if file_name is None:
        return None
    path = run_dir / file_name
    if not path.is_file():
        return None
    content = load_safe_yaml(path.read_text(encoding="utf-8"))
    return lifecycle.canonical_content_digest(content)


def _working_file_digest(run_dir: Path, file_name: str) -> str | None:
    path = run_dir / file_name
    if not path.is_file():
        return None
    content = load_safe_yaml(path.read_text(encoding="utf-8"))
    return lifecycle.canonical_content_digest(content)


def _topological_order(depmap: DependencyMap) -> list[Ref]:
    """Gates and derivations in dependency order (artifacts excluded -- they
    are never "built", only referenced). Generic over any DependencyMap."""
    declared: dict[Ref, tuple[Ref, ...]] = {}
    for name, spec in depmap.gates.items():
        declared[gate(name)] = spec.inputs
    for name, spec in depmap.derivations.items():
        declared[derivation(name)] = spec.inputs
    order: list[Ref] = []
    visited: set[Ref] = set()

    def visit(ref: Ref) -> None:
        if ref in visited:
            return
        visited.add(ref)
        for dep in declared.get(ref, ()):
            if not isinstance(dep, ArtifactRef):
                visit(dep)
        order.append(ref)

    for ref in declared:
        visit(ref)
    return order


def _resolve_inputs(run_dir: Path, refs: tuple[Ref, ...], selected_id: Mapping[Ref, str]) -> list[dict[str, Any]]:
    """Turn a GateSpec/DerivationSpec's ``inputs`` (names) into the ledger's
    stored form: artifact refs carry a digest snapshot; gate/derivation refs
    carry the *specific* confirmation_id/derivation_id that was selected for
    that name at the moment this record was built -- not just the name --
    so that a later supersession of that specific id (not just any record
    for that name) is what makes this record stale, mirroring
    lifecycle.py's version_ref dependency tracking."""
    resolved: list[dict[str, Any]] = []
    for ref in refs:
        if isinstance(ref, ArtifactRef):
            resolved.append({"kind": "artifact", "name": ref.name, "digest": _artifact_digest(run_dir, ref.name)})
        elif isinstance(ref, GateRef):
            resolved.append({"kind": "confirmation", "ref": selected_id.get(ref)})
        elif isinstance(ref, DerivationRef):
            resolved.append({"kind": "derivation", "ref": selected_id.get(ref)})
    return resolved


_LEGACY_IMPORT_TIMESTAMP = "legacy_import"


def _synthesize_legacy_import(run_dir: Path) -> dict[str, Any]:
    """Build an in-memory ledger from whatever working files already exist,
    marking every synthesized record ``origin: legacy_import``. Callers
    persist this only inside ``apply_mutation``'s locked write -- a plain
    preview never writes it to disk.

    Deliberately does not take a ``now`` parameter: a legacy run's real
    confirmation time is unknowable, and stamping today's wall-clock time
    on every synthesized record would make preview_id non-deterministic
    across separate preview/apply calls -- the very thing preview_id
    exists to detect as staleness. Using a fixed sentinel here means a
    synthesized ledger's digest depends only on the working files'
    content, matching lifecycle.py's own "creation time intentionally
    does not affect its ID" rule for the same reason. The real timestamp
    is recorded once, honestly, when apply_mutation actually persists a
    legacy-imported record for the first time (see apply_mutation)."""
    ledger = _empty_ledger()
    selected_id: dict[Ref, str] = {}
    for ref in _topological_order(PERSONA_DEPENDENCY_MAP):
        if isinstance(ref, GateRef):
            spec = GATES[ref.name]
            digest = _working_file_digest(run_dir, spec.selection_file)
            if digest is None:
                continue
            inputs = _resolve_inputs(run_dir, spec.inputs, selected_id)
            confirmation_id = _content_id("HC", {"gate": ref.name, "decision_digest": digest, "inputs": inputs})
            ledger["confirmations"].append({
                "confirmation_id": confirmation_id,
                "gate": ref.name,
                "decision_digest": digest,
                "inputs": inputs,
                "supersedes": None,
                "recorded_by": "legacy_import",
                "confirmed_at": _LEGACY_IMPORT_TIMESTAMP,
            })
            selected_id[ref] = confirmation_id
        elif isinstance(ref, DerivationRef):
            spec = DERIVATIONS[ref.name]
            digest = _working_file_digest(run_dir, spec.file)
            if digest is None:
                continue
            inputs = _resolve_inputs(run_dir, spec.inputs, selected_id)
            derivation_id = _content_id("HD", {"name": ref.name, "content_digest": digest, "inputs": inputs})
            ledger["derivations"].append({
                "derivation_id": derivation_id,
                "name": ref.name,
                "content_digest": digest,
                "inputs": inputs,
                "operation": None,
                "recorded_by": "legacy_import",
                "produced_at": _LEGACY_IMPORT_TIMESTAMP,
            })
            selected_id[ref] = derivation_id
    return ledger


def _ledger_or_legacy_import(run_dir: Path, ledger: dict[str, Any], now: str) -> dict[str, Any]:
    del now  # kept for callers' symmetry with other now-taking helpers; unused, see _synthesize_legacy_import
    substantive = any(
        record.get("name") != "artifact_current_pointer"
        for record in ledger["derivations"]
    )
    if (
        ledger["confirmations"] or substantive or ledger["operations"]
        or (_ledger_path(run_dir).is_file() and not ledger["derivations"])
    ):
        return ledger
    imported = _synthesize_legacy_import(run_dir)
    if imported["confirmations"] or imported["derivations"]:
        imported["derivations"].extend(ledger["derivations"])
        return imported
    return ledger


# --------------------------------------------------------------------------
# Content store: write-once, digest-addressed. A version an apply would
# otherwise overwrite is preserved here first -- same idea as
# validation.py's content_dir, scoped under .gates/ instead.
# --------------------------------------------------------------------------


def _store_content(run_dir: Path, content: Any) -> str:
    digest = lifecycle.canonical_content_digest(content)
    hex_digest = digest.split(":", 1)[1]
    directory = _content_dir(run_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"sha256-{hex_digest}.yaml"
    if not path.is_file():
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=directory,
                prefix=path.name + ".", suffix=".tmp", delete=False,
            ) as temporary:
                temporary_name = temporary.name
                yaml.safe_dump(content, temporary, allow_unicode=True, sort_keys=False)
            Path(temporary_name).replace(path)
            temporary_name = None
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
    return digest


def load_preserved_content(run_dir: Path, digest: str) -> Any:
    hex_digest = digest.split(":", 1)[1] if ":" in digest else digest
    path = _content_dir(run_dir) / f"sha256-{hex_digest}.yaml"
    if not path.is_file():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _move_stale_transport(run_dir: Path, stage: str, tag: str) -> None:
    """Move (never delete) a derivation's _transport/<stage> envelope
    directory out of the way before it is rebuilt, so a fresh run never
    misreads a prior attempt as TransportStale, and the old envelopes stay
    recoverable as provenance."""
    source = run_dir / TRANSPORT_DIRNAME / stage
    if not source.is_dir():
        return
    destination_root = _transport_archive_dir(run_dir) / tag
    destination_root.parent.mkdir(parents=True, exist_ok=True)
    if destination_root.exists():
        return  # already archived under this tag -- idempotent
    shutil.move(str(source), str(destination_root))


# --------------------------------------------------------------------------
# Lineage resolution: current/stale per gate/derivation NAME (its selected,
# i.e. latest, record), mirroring lifecycle.resolve_lifecycle's rule.
# --------------------------------------------------------------------------


def _selected_ids(ledger: Mapping[str, Any]) -> dict[Ref, str]:
    """The latest (selected) confirmation_id/derivation_id for every gate/
    derivation name the ledger has any record of. List order is append
    order, so a plain left-to-right overwrite lands on the last one."""
    selected: dict[Ref, str] = {}
    for record in ledger.get("confirmations", []):
        selected[gate(record["gate"])] = record["confirmation_id"]
    for record in ledger.get("derivations", []):
        selected[derivation(record["name"])] = record["derivation_id"]
    return selected


def _selected_confirmation(ledger: Mapping[str, Any], gate_name: str) -> dict[str, Any] | None:
    matches = [c for c in ledger.get("confirmations", []) if c.get("gate") == gate_name]
    return matches[-1] if matches else None


def _selected_derivation(ledger: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    matches = [d for d in ledger.get("derivations", []) if d.get("name") == name]
    return matches[-1] if matches else None


def _find_confirmation(ledger: Mapping[str, Any], confirmation_id: str) -> dict[str, Any] | None:
    return next((c for c in ledger.get("confirmations", []) if c.get("confirmation_id") == confirmation_id), None)


def _find_derivation(ledger: Mapping[str, Any], derivation_id: str) -> dict[str, Any] | None:
    return next((d for d in ledger.get("derivations", []) if d.get("derivation_id") == derivation_id), None)


def _status_by_id(
    run_dir: Path, ledger: Mapping[str, Any], record_id: str | None, kind: Literal["confirmation", "derivation"],
    memo: dict[tuple[str, str], Literal["current", "stale", "superseded", "absent"]],
) -> Literal["current", "stale", "superseded", "absent"]:
    """Walk one specific ledger record's own recorded ``inputs`` (each
    already pinned to a specific confirmation_id/derivation_id at the time
    that record was built -- see ``_resolve_inputs``), recursing exactly the
    way ``lifecycle.resolve_lifecycle`` walks ``version_ref`` dependencies.
    A record is superseded if it is not the selected (latest-appended)
    record for its own gate/derivation name; otherwise stale if any input
    is not current; otherwise current."""
    if record_id is None:
        return "absent"
    memo_key = (kind, record_id)
    if memo_key in memo:
        return memo[memo_key]
    record = _find_confirmation(ledger, record_id) if kind == "confirmation" else _find_derivation(ledger, record_id)
    if record is None:
        memo[memo_key] = "absent"
        return "absent"
    name_field = "gate" if kind == "confirmation" else "name"
    id_field = "confirmation_id" if kind == "confirmation" else "derivation_id"
    selected = (
        _selected_confirmation(ledger, record[name_field]) if kind == "confirmation"
        else _selected_derivation(ledger, record[name_field])
    )
    if selected is None or selected.get(id_field) != record_id:
        memo[memo_key] = "superseded"
        return "superseded"
    result: Literal["current", "stale", "superseded", "absent"] = "current"
    for inp in record.get("inputs", []) or []:
        if inp.get("kind") == "artifact":
            if inp.get("digest") != _artifact_digest(run_dir, inp.get("name", "")):
                result = "stale"
                break
            continue
        sub_kind = inp.get("kind")
        sub_status = _status_by_id(run_dir, ledger, inp.get("ref"), sub_kind, memo)  # type: ignore[arg-type]
        if sub_status != "current":
            result = "stale"
            break
    memo[memo_key] = result
    return result


def resolve_selected_status(
    run_dir: Path, ledger: Mapping[str, Any], depmap: DependencyMap, ref: Ref,
) -> LineageStatus:
    """The selected (latest) record's own current/stale status for a gate or
    derivation NAME. Absent if that name has never been confirmed/produced."""
    if isinstance(ref, ArtifactRef):
        return "current"
    memo: dict[tuple[str, str], Literal["current", "stale", "superseded", "absent"]] = {}
    if isinstance(ref, GateRef):
        selected = _selected_confirmation(ledger, ref.name)
        if selected is None:
            return "absent"
        status = _status_by_id(run_dir, ledger, selected["confirmation_id"], "confirmation", memo)
    else:
        selected = _selected_derivation(ledger, ref.name)
        if selected is None:
            return "absent"
        status = _status_by_id(run_dir, ledger, selected["derivation_id"], "derivation", memo)
    # `status` here is never "superseded" -- `selected` is by definition the
    # selected record, so _status_by_id's own supersession check on it
    # always passes straight through to the stale/current walk.
    return status  # type: ignore[return-value]


def record_status(
    run_dir: Path, ledger: Mapping[str, Any], depmap: DependencyMap, record_id: str, kind: Literal["confirmation", "derivation"],
) -> Literal["current", "stale", "superseded", "absent"]:
    """Status of one specific ledger record by id (as opposed to
    ``resolve_selected_status``, which only asks about the selected/latest
    record for a name) -- for reporting which exact record a preview would
    supersede, and for asserting an individual record's fate in tests."""
    memo: dict[tuple[str, str], Literal["current", "stale", "superseded", "absent"]] = {}
    return _status_by_id(run_dir, ledger, record_id, kind, memo)


def resolve_gate_state(run_dir: Path, gate_name: str, *, now: str) -> GateState:
    ledger = _read_ledger(run_dir)
    ledger = _ledger_or_legacy_import(run_dir, ledger, now)
    depmap = PERSONA_DEPENDENCY_MAP
    status = resolve_selected_status(run_dir, ledger, depmap, gate(gate_name))
    if status == "current":
        return "confirmed"
    for op in reversed(ledger.get("operations", [])):
        stops_at = op.get("stops_at")
        stop_names = stops_at if isinstance(stops_at, list) else ([stops_at] if stops_at else [])
        if gate_name in stop_names:
            if op.get("state") in ("pending", "generating", "waiting_for_model"):
                return "generating"
            if op.get("state") == "failed":
                return "failed"
            break
    spec = depmap.gates[gate_name]
    inputs_current = all(resolve_selected_status(run_dir, ledger, depmap, inp) == "current" for inp in spec.inputs)
    return "awaiting_confirmation" if inputs_current else "draft"


def is_derivation_runnable(run_dir: Path, name: str, *, now: str) -> bool:
    """Whether ``name`` may be generated right now -- every gate and
    derivation input it declares is current. Gates are never satisfied
    automatically: a derivation whose gate input is stale or absent is not
    runnable, full stop, regardless of whether a working file for it
    already happens to exist on disk. This is the single check every
    generation path (CLI guard, server endpoint, run_rebuild's own
    resolver-driven plan) shares -- see spec `persona-generation-trigger`'s
    "resolver reports persona_content runnable" and `target-segmentation-
    designation`'s propose-mode refusal."""
    ledger = _read_ledger(run_dir)
    ledger = _ledger_or_legacy_import(run_dir, ledger, now)
    depmap = PERSONA_DEPENDENCY_MAP
    spec = depmap.derivations[name]
    return all(resolve_selected_status(run_dir, ledger, depmap, inp) == "current" for inp in spec.inputs)


# --------------------------------------------------------------------------
# Preview: a pure read of what apply_mutation *would* do. No writes, no
# ledger bootstrap -- a legacy-import ledger stays purely in-memory here.
# --------------------------------------------------------------------------


class GatesError(Exception):
    """Base for this module's own errors (distinct from LifecycleValidationError, which stays lifecycle.py's)."""


class StalePreview(GatesError):
    """The recomputed preview no longer matches the ``preview_id`` supplied to ``apply_mutation``."""


class ValidationFailed(GatesError):
    """The proposed decision failed the gate's own content validator."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


class UnknownOperation(GatesError):
    """``run_rebuild`` was asked to resume an operation_id the ledger has no record of."""


@dataclass(frozen=True)
class ImpactPreview:
    gate: str
    is_mutation: bool
    is_noop: bool
    superseded: list[str]
    will_become_stale: dict[str, list[str]]  # {"confirmations": [gate names], "derivations": [names]}
    rebuild_plan: list[str]
    stops_at: str | None
    preserved: list[str]
    preview_id: str
    plain_summary: str


def _plain_name(file_name: str) -> str:
    from framework_points import ARTIFACT_PLAIN_NAMES  # local import: tools/ is on sys.path for callers, not gates.py's own module import chain
    return ARTIFACT_PLAIN_NAMES.get(file_name, file_name)


def _plain_summary(gate_name: str, is_mutation: bool, is_noop: bool, plan: list[str], stops_at: str | None) -> str:
    gate_label = _plain_name(GATES[gate_name].selection_file)
    if is_noop:
        return f"跟目前確認的內容相同，不會變更{gate_label}。"
    if not is_mutation:
        return f"這是{gate_label}第一次確認，不會影響任何既有產物。"
    if not plan:
        return f"確認新的{gate_label}後，沒有下游產物需要重新生成。"
    plan_labels = "、".join(_plain_name(DERIVATIONS[name].file) for name in plan)
    if stops_at is None:
        return f"確認新的{gate_label}後，會重新生成：{plan_labels}。"
    stop_label = _plain_name(GATES[stops_at].selection_file)
    return f"確認新的{gate_label}後，會重新生成：{plan_labels}，然後停下來等你確認{stop_label}。"


def _relevant_working_files() -> list[str]:
    files = [spec.selection_file for spec in GATES.values()]
    files += [spec.file for spec in DERIVATIONS.values()]
    return sorted(set(files))


def _preview_id(ledger: Mapping[str, Any], run_dir: Path, gate_name: str, decision_digest: str) -> str:
    payload = {
        "ledger_digest": lifecycle.canonical_content_digest(ledger),
        "gate": gate_name,
        "decision_digest": decision_digest,
        "working_digests": {name: _working_file_digest(run_dir, name) for name in _relevant_working_files()},
    }
    return lifecycle.canonical_content_digest(payload)


def _preview(run_dir: Path, ledger: Mapping[str, Any], gate_name: str, proposed_decision: Mapping[str, Any]) -> ImpactPreview:
    """Shared by ``preview_mutation`` (fresh read) and ``apply_mutation``
    (recomputed under the lock, against the same already-read ``ledger``)."""
    depmap = PERSONA_DEPENDENCY_MAP
    decision_digest = lifecycle.canonical_content_digest(proposed_decision)
    selected = _selected_confirmation(ledger, gate_name)
    is_noop = selected is not None and selected["decision_digest"] == decision_digest
    is_mutation = selected is not None and not is_noop

    def status(ref: Ref) -> LineageStatus:
        result = resolve_selected_status(run_dir, ledger, depmap, ref)
        return "current" if result == "current" else ("absent" if result == "absent" else "stale")

    if is_mutation or selected is None:
        plan, stops_at = rebuild_plan_and_stop(depmap, gate(gate_name), status)
    else:
        plan, stops_at = [], None

    scope = affected(depmap, gate(gate_name)) if (is_mutation or selected is None) else set()
    stale_gates = sorted(name for name in depmap.gates if gate(name) in scope)
    stale_derivations = [name for name in depmap.derivations if derivation(name) in scope]

    superseded_ids = [selected["confirmation_id"]] if (selected is not None and is_mutation) else []

    preserved: list[str] = []
    if is_mutation:
        if selected.get("decision_digest"):
            preserved.append(selected["decision_digest"])
        for name in stale_derivations:
            record = _selected_derivation(ledger, name)
            if record is not None:
                preserved.append(record["content_digest"])

    preview_id = _preview_id(ledger, run_dir, gate_name, decision_digest)

    return ImpactPreview(
        gate=gate_name,
        is_mutation=is_mutation,
        is_noop=is_noop,
        superseded=superseded_ids,
        will_become_stale={"confirmations": stale_gates, "derivations": stale_derivations},
        rebuild_plan=plan,
        stops_at=stops_at,
        preserved=preserved,
        preview_id=preview_id,
        plain_summary=_plain_summary(gate_name, is_mutation, is_noop, plan, stops_at),
    )


def preview_mutation(run_dir: Path, gate_name: str, proposed_decision: Mapping[str, Any], *, now: str) -> ImpactPreview:
    """Pure: no writes to the run directory, the ledger, or the content
    store -- including no legacy-import bootstrap. Safe to call repeatedly
    and to discard."""
    ledger = _read_ledger(run_dir)
    ledger = _ledger_or_legacy_import(run_dir, ledger, now)
    return _preview(run_dir, ledger, gate_name, proposed_decision)


# --------------------------------------------------------------------------
# Apply: locked, atomic. Confirming a decision and rebuilding what follows
# from it are two separate, independently retryable steps (see run_rebuild
# below) -- apply_mutation only ever confirms.
# --------------------------------------------------------------------------


def _write_working_file(run_dir: Path, file_name: str, content: Any) -> None:
    """The plain (non-content-addressed) working file every existing
    consumer already reads -- a view of the latest state, not the
    authority. Same tempfile + replace pattern as the ledger/content store,
    so a crash mid-write never leaves a half-written file behind."""
    path = run_dir / file_name
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=path.name + ".", suffix=".tmp", delete=False,
        ) as temporary:
            temporary_name = temporary.name
            yaml.safe_dump(content, temporary, allow_unicode=True, sort_keys=False)
        Path(temporary_name).replace(path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


@dataclass(frozen=True)
class ApplyResult:
    confirmation_id: str
    operation_id: str | None
    stops_at: str | None


def apply_mutation(
    run_dir: Path,
    gate_name: str,
    proposed_decision: Mapping[str, Any],
    preview_id: str,
    *,
    now: str,
    validate: Callable[[Mapping[str, Any]], list[str]] | None = None,
    actor: Mapping[str, Any] | None = None,
    schedule_rebuild: bool = True,
) -> ApplyResult:
    """Confirm ``proposed_decision`` for ``gate_name``. Requires a
    ``preview_id`` from a just-computed preview of this exact decision;
    rejects (writing nothing) if the run directory changed since that
    preview. Never rebuilds anything itself -- on success it appends a
    ``pending`` operation for the caller to run via ``run_rebuild``."""
    with _ledger_lock(run_dir):
        ledger = _read_ledger(run_dir)
        ledger = _ledger_or_legacy_import(run_dir, ledger, now)
        preview = _preview(run_dir, ledger, gate_name, proposed_decision)
        if preview.is_noop:
            selected = _selected_confirmation(ledger, gate_name)
            # A legacy import can already contain the same authoritative
            # confirmation while lacking the plain working selection file.
            # Confirming that exact choice is still save-only, but the
            # current read model needs the file in order to render the
            # confirmed axes/target after reload.
            _write_working_file(run_dir, GATES[gate_name].selection_file, proposed_decision)
            _write_ledger_locked(run_dir, ledger)  # materializes a legacy import even on a noop confirm
            return ApplyResult(confirmation_id=selected["confirmation_id"], operation_id=None, stops_at=None)
        if preview.is_mutation and preview.preview_id != preview_id:
            # A first-ever selection (not is_mutation) needs no preview_id --
            # there is nothing yet for a stale comparison to protect.
            raise StalePreview(
                f"preview_id for gate={gate_name!r} is missing or no longer matches the run directory; "
                "re-preview and retry"
            )

        if validate is not None:
            errors = list(validate(proposed_decision) or [])
            if errors:
                raise ValidationFailed(errors)

        depmap = PERSONA_DEPENDENCY_MAP
        spec = depmap.gates[gate_name]

        # 1. Preserve content this apply would otherwise overwrite --
        #    before any write happens.
        prior = _selected_confirmation(ledger, gate_name)
        if prior is not None:
            existing = _working_file_digest(run_dir, spec.selection_file)
            if existing is not None:
                current_content = yaml.safe_load((run_dir / spec.selection_file).read_text(encoding="utf-8"))
                _store_content(run_dir, current_content)
        for name in preview.rebuild_plan:
            derivation_spec = depmap.derivations[name]
            path = run_dir / derivation_spec.file
            if path.is_file():
                _store_content(run_dir, yaml.safe_load(path.read_text(encoding="utf-8")))

        # 2. Move stale transport envelopes for derivations this rebuild
        #    will redo, tagged by the derivation record they belonged to.
        for name in preview.rebuild_plan:
            derivation_spec = depmap.derivations[name]
            prior_derivation = _selected_derivation(ledger, name)
            tag = prior_derivation["derivation_id"] if prior_derivation is not None else name
            _move_stale_transport(run_dir, derivation_spec.transport_stage, tag)

        # 3. Write the working selection file -- the decision itself.
        _write_working_file(run_dir, spec.selection_file, proposed_decision)

        # 4. Append the confirmation, content-addressed, superseding the
        #    prior one if any.
        decision_digest = lifecycle.canonical_content_digest(proposed_decision)
        inputs = _resolve_inputs(run_dir, spec.inputs, _selected_ids(ledger))
        confirmation_id = _content_id(
            "HC", {"gate": gate_name, "decision_digest": decision_digest, "inputs": inputs, "confirmed_at": now}
        )
        confirmation = {
            "confirmation_id": confirmation_id,
            "gate": gate_name,
            "decision_digest": decision_digest,
            "inputs": inputs,
            "supersedes": prior["confirmation_id"] if prior is not None else None,
            "recorded_by": "researcher",
            "confirmed_at": now,
        }
        if isinstance(actor, Mapping) and actor.get("actor_id"):
            confirmation["actor_id"] = str(actor["actor_id"])
            confirmation["actor_type"] = str(actor.get("actor_type") or "researcher")
        ledger["confirmations"].append(confirmation)

        # 5. Append the rebuild operation, `pending` -- run_rebuild executes
        #    it as a separate, independently retryable step.
        operation_id = None
        if preview.rebuild_plan and schedule_rebuild:
            operation_id = _content_id(
                "HO", {"trigger": confirmation_id, "plan": preview.rebuild_plan, "confirmed_at": now}
            )
            ledger["operations"].append({
                "operation_id": operation_id,
                "trigger": confirmation_id,
                "plan": list(preview.rebuild_plan),
                "state": "pending",
                "error": None,
                "stops_at": [preview.stops_at] if preview.stops_at else [],
                "updated_at": now,
            })

        _write_ledger_locked(run_dir, ledger)

    return ApplyResult(confirmation_id=confirmation_id, operation_id=operation_id, stops_at=preview.stops_at)


# --------------------------------------------------------------------------
# Rebuild: a separate, independently retryable step. Failure here never
# touches the confirmation that triggered it.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StepProduced:
    content: Any


@dataclass(frozen=True)
class StepSuspended:
    pass


@dataclass(frozen=True)
class StepFailed:
    error: str


StepOutcome = StepProduced | StepSuspended | StepFailed
StepRunner = Callable[[Path], StepOutcome]


@dataclass(frozen=True)
class OperationResult:
    operation_id: str
    state: Literal["pending", "generating", "waiting_for_model", "failed", "done"]
    error: str | None


def _find_operation(ledger: Mapping[str, Any], operation_id: str) -> dict[str, Any] | None:
    return next((o for o in ledger.get("operations", []) if o.get("operation_id") == operation_id), None)


def run_rebuild(run_dir: Path, operation_id: str, runners: Mapping[str, StepRunner], *, now: str) -> OperationResult:
    """Execute (or resume) one operation's rebuild plan. A step whose
    derivation is already current for this operation's inputs is skipped --
    idempotent on retry. A runner returns a ``StepOutcome``; ``gates.py``
    never shells out or calls a model itself (coding-boundaries.md: that
    belongs to the caller's CLI/AI-runner wiring, not the domain layer)."""
    with _ledger_lock(run_dir):
        ledger = _read_ledger(run_dir)
        operation = _find_operation(ledger, operation_id)
        if operation is None:
            raise UnknownOperation(operation_id)
        if operation["state"] == "done":
            return OperationResult(operation_id=operation_id, state="done", error=None)
        plan = list(operation["plan"])
        operation["state"] = "generating"
        operation["error"] = None
        operation["updated_at"] = now
        _write_ledger_locked(run_dir, ledger)

    depmap = PERSONA_DEPENDENCY_MAP
    for name in plan:
        with _ledger_lock(run_dir):
            ledger = _read_ledger(run_dir)
            step_status = resolve_selected_status(run_dir, ledger, depmap, derivation(name))
        if step_status == "current":
            continue  # already produced by an earlier partial run of this operation

        runner = runners.get(name)
        if runner is None:
            raise LookupError(f"no runner supplied for derivation={name!r}")
        outcome = runner(run_dir)

        with _ledger_lock(run_dir):
            ledger = _read_ledger(run_dir)
            operation = _find_operation(ledger, operation_id)
            if isinstance(outcome, StepSuspended):
                operation["state"] = "waiting_for_model"
                operation["updated_at"] = now
                _write_ledger_locked(run_dir, ledger)
                return OperationResult(operation_id=operation_id, state="waiting_for_model", error=None)
            if isinstance(outcome, StepFailed):
                operation["state"] = "failed"
                operation["error"] = outcome.error
                operation["updated_at"] = now
                _write_ledger_locked(run_dir, ledger)
                return OperationResult(operation_id=operation_id, state="failed", error=outcome.error)

            derivation_spec = depmap.derivations[name]
            content_digest = lifecycle.canonical_content_digest(outcome.content)
            selected_id = _selected_ids(ledger)
            inputs = _resolve_inputs(run_dir, derivation_spec.inputs, selected_id)
            derivation_id = _content_id(
                "HD", {"name": name, "content_digest": content_digest, "inputs": inputs, "produced_at": now}
            )
            ledger["derivations"].append({
                "derivation_id": derivation_id,
                "name": name,
                "content_digest": content_digest,
                "inputs": inputs,
                "operation": operation_id,
                "recorded_by": "researcher",
                "produced_at": now,
            })
            _write_ledger_locked(run_dir, ledger)

    with _ledger_lock(run_dir):
        ledger = _read_ledger(run_dir)
        operation = _find_operation(ledger, operation_id)
        operation["state"] = "done"
        operation["error"] = None
        operation["updated_at"] = now
        _write_ledger_locked(run_dir, ledger)
    return OperationResult(operation_id=operation_id, state="done", error=None)


__all__ = [
    "GateState", "LineageStatus", "GateRef", "DerivationRef", "ArtifactRef", "Ref",
    "gate", "derivation", "artifact", "cli", "CliOperation",
    "GateSpec", "DerivationSpec", "DependencyMap", "GATES", "DERIVATIONS", "PERSONA_DEPENDENCY_MAP",
    "affected", "rebuild_plan_and_stop",
    "resolve_selected_status", "record_status", "resolve_gate_state", "is_derivation_runnable",
    "GatesError", "StalePreview", "ValidationFailed", "UnknownOperation",
    "ImpactPreview", "preview_mutation",
    "ApplyResult", "apply_mutation", "load_preserved_content",
    "StepProduced", "StepSuspended", "StepFailed", "StepOutcome", "StepRunner",
    "OperationResult", "run_rebuild",
]
