"""Read-only Local V1 ``as_is_blueprint`` delivery adapter.

The adapter resolves the reviewed, lifecycle-current As-is graph through the
existing baseline resolver and aligns its nodes to the current confirmed
Journey.  It preserves canonical claim and relation provenance; it never
rebuilds the graph, fills empty lanes, or reads To-be artifacts.
"""
from __future__ import annotations

import copy
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

import yaml

from . import as_is_research, lifecycle, stages
from .delivery import (
    DeliveryError,
    DeliveryViewModel,
    _digest,
    _read_json,
    _read_yaml,
    _require_valid,
    _resolve_registry_path,
    _schema_errors,
)
from .delivery_customer_journey import build_customer_journey_view


AS_IS_BLUEPRINT = "as_is_blueprint"
# The export id stays `as_is_blueprint` for both workflows; only the title differs.
# A new-workflow blueprint already realizes researcher-selected Solutions, so it
# is not an "As-is" blueprint.
LEGACY_BLUEPRINT_TITLE = "As-is Service Blueprint"
SBP_BLUEPRINT_TITLE = "Service Blueprint"

# These are the graph's canonical lanes, not a presentation template.  Do not
# add literature-derived lanes merely to make a matrix look complete.
CANONICAL_LAYERS = ("實體證據", "顧客行動", "前台行動", "後台行動", "支援流程")


def _required_file(run_dir: Path, filename: str, artifact: str) -> Path:
    path = run_dir / filename
    if not path.is_file():
        raise DeliveryError("required_artifact_missing", artifact, filename)
    return path


def _status_for_claim(claim: Mapping[str, Any]) -> str:
    source = claim.get("source")
    if source == "evidence" and claim.get("inferred") is False:
        return "observed"
    if source == "inference" or claim.get("inferred") is True:
        return "inferred"
    if source == "researcher":
        return "researcher_input"
    return "unresolved"


def _aggregate_provenance(statuses: list[str]) -> str:
    unique = set(statuses)
    if unique == {"observed"}:
        return "observed"
    if unique == {"inferred"}:
        return "inferred"
    if unique == {"researcher_input"}:
        return "researcher_input"
    if not unique:
        return "unresolved"
    return "mixed"


def _claim_view(claim: Mapping[str, Any], evidence_ids: set[str]) -> dict[str, Any]:
    status = _status_for_claim(claim)
    evidence_ref = claim.get("evidence_ref")
    if evidence_ref is not None and evidence_ref not in evidence_ids:
        raise DeliveryError("delivery_evidence_reference_missing", "service_graph", evidence_ref)
    return {
        "text": claim.get("text"),
        "source": claim.get("source"),
        "inferred": bool(claim.get("inferred")),
        "scope": claim.get("scope"),
        "evidence_ref": evidence_ref,
        "provenance": status,
    }


def _node_view(node: Mapping[str, Any], evidence_ids: set[str]) -> dict[str, Any]:
    claims = [
        _claim_view(claim, evidence_ids)
        for claim in (node.get("claims") or [])
        if isinstance(claim, Mapping)
    ]
    evidence_refs = sorted({claim["evidence_ref"] for claim in claims if claim.get("evidence_ref")})
    statuses = [str(claim["provenance"]) for claim in claims]
    return {
        "node_id": node.get("id"),
        "kind": node.get("kind"),
        "label": node.get("label"),
        "description": node.get("description"),
        "layer": node.get("layer"),
        "step": node.get("step"),
        "actor": node.get("actor"),
        "actor_boundary": node.get("actor_boundary"),
        "visibility": node.get("visibility"),
        "service_role": node.get("service_role"),
        # A channel is included only when the graph recorded one.  Missing
        # channels remain absent rather than receiving an inferred taxonomy.
        "channel": node.get("channel"),
        "scenario_refs": list(node.get("scenario_refs") or []),
        **({"selected_solution_ref": node.get("selected_solution_ref")} if node.get("selected_solution_ref") else {}),
        "provenance": _aggregate_provenance(statuses),
        "evidence_refs": evidence_refs,
        "claims": claims,
    }


def _relation_view(relation: Mapping[str, Any], evidence_ids: set[str], node_ids: set[str]) -> dict[str, Any]:
    endpoints = relation.get("endpoints") or [relation.get("from"), relation.get("to")]
    if not isinstance(endpoints, list) or any(endpoint not in node_ids for endpoint in endpoints if endpoint):
        raise DeliveryError("service_graph_relation_endpoint_missing", "service_graph", relation.get("id"))
    refs = list(relation.get("evidence_refs") or [])
    missing = sorted(set(refs) - evidence_ids)
    if missing:
        raise DeliveryError("delivery_evidence_reference_missing", "service_graph", ",".join(missing))
    source = relation.get("source")
    provenance = "observed" if source == "evidence" else "inferred" if source == "inference" else "researcher_input" if source == "researcher" else "unresolved"
    return {
        "relation_id": relation.get("id"),
        "type": relation.get("type"),
        "source": source,
        "provenance": provenance,
        "from": relation.get("from"),
        "to": relation.get("to"),
        "endpoints": list(endpoints),
        "evidence_refs": refs,
        "note": relation.get("note"),
        "muted": bool(relation.get("muted")),
    }


def _marker_view(marker: Mapping[str, Any], evidence_ids: set[str], node_ids: set[str]) -> dict[str, Any]:
    evidence_ref = marker.get("evidence_ref")
    if evidence_ref not in evidence_ids:
        raise DeliveryError("delivery_evidence_reference_missing", "service_graph", evidence_ref)
    if marker.get("node_ref") not in node_ids:
        raise DeliveryError("service_graph_marker_node_missing", "service_graph", marker.get("node_ref"))
    return copy.deepcopy(dict(marker))


def _annotation_view(annotation: Mapping[str, Any], evidence_ids: set[str], node_ids: set[str]) -> dict[str, Any]:
    refs = list(annotation.get("evidence_refs") or [])
    missing = sorted(set(refs) - evidence_ids)
    if missing:
        raise DeliveryError("delivery_evidence_reference_missing", "service_graph", ",".join(missing))
    node_refs = list(annotation.get("node_refs") or [])
    missing_nodes = sorted(set(node_refs) - node_ids)
    if missing_nodes:
        raise DeliveryError("service_graph_annotation_node_missing", "service_graph", ",".join(missing_nodes))
    return copy.deepcopy(dict(annotation))


def _source_ref(artifact_type: str, source_ref: str, data: Any, *, status: str = "ready", version_ref: str | None = None, reason: str | None = None) -> dict[str, Any]:
    ref: dict[str, Any] = {
        "artifact_type": artifact_type,
        "source_ref": source_ref,
        "status": status,
        "version_ref": version_ref,
        "content_digest": _digest(data, artifact_type) if data is not None else None,
        "versioning": "lifecycle_current" if version_ref else "run_scoped_current_file",
    }
    if reason:
        ref["reason"] = reason
    return ref


def _evidence_sources(evidence: list[Mapping[str, Any]], referenced_ids: set[str]) -> list[dict[str, Any]]:
    by_id = {item.get("id"): item for item in evidence if isinstance(item, Mapping)}
    missing = sorted(referenced_ids - set(by_id))
    if missing:
        raise DeliveryError("delivery_evidence_reference_missing", "evidence", ",".join(missing))
    return [
        {
            "evidence_id": evidence_id,
            "source": by_id[evidence_id].get("source"),
            "locator": by_id[evidence_id].get("locator"),
            "role": by_id[evidence_id].get("role"),
            "tier": by_id[evidence_id].get("tier"),
        }
        for evidence_id in sorted(referenced_ids)
    ]


def _journey_steps(journey_payload: Mapping[str, Any]) -> tuple[dict[str, dict[int, Mapping[str, Any]]], list[Mapping[str, Any]]]:
    by_scenario: dict[str, dict[int, Mapping[str, Any]]] = {}
    scenarios = [item for item in journey_payload.get("scenarios", []) if isinstance(item, Mapping)]
    for scenario in scenarios:
        scenario_id = scenario.get("scenario_id")
        if not isinstance(scenario_id, str):
            raise DeliveryError("journey_alignment_invalid", "confirmed_journey", "scenario id missing")
        by_scenario[scenario_id] = {
            int(step.get("order")): step
            for step in (scenario.get("steps") or [])
            if isinstance(step, Mapping) and isinstance(step.get("order"), int)
        }
    return by_scenario, scenarios


def _branch_views(journey_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Carry v3 Branch structure without inventing Service Graph alignment."""
    branches: list[dict[str, Any]] = []
    if "branches" not in journey_payload:
        return branches
    for branch in journey_payload.get("branches") or []:
        if not isinstance(branch, Mapping):
            continue
        steps = []
        for index, step in enumerate(branch.get("steps") or [], 1):
            if not isinstance(step, Mapping):
                continue
            refs = list(step.get("evidence_refs") or [])
            raw_provenance = step.get("provenance")
            if isinstance(raw_provenance, Mapping):
                provenance = raw_provenance.get("content_status", "unavailable")
            else:
                provenance = raw_provenance if raw_provenance is not None else "unavailable"
            steps.append({
                "step_id": step.get("id"),
                "order": index,
                "label": step.get("label"),
                "description": step.get("description"),
                "evidence_refs": refs,
                "nodes": [],
                "provenance": {
                    "content_status": provenance,
                    "source_ref": "journey.yaml",
                    "evidence_refs": refs,
                },
            })
        branches.append({
            "branch_id": branch.get("branch_id"),
            "label": branch.get("label"),
            "divergence_step_id": branch.get("divergence_step_id"),
            "merge_step_id": branch.get("merge_step_id"),
            "topology_provenance": branch.get("topology_provenance"),
            "steps": steps,
        })
    return branches


def _lineage_is_current_from_lifecycle(
    run_path: Path,
    baseline: Any,
    journey_snapshot: Mapping[str, Any],
    graph_journey_ref: Any,
) -> bool:
    """Use lifecycle projection metadata without recomputing a projection.

    The legacy full Journey digest remains the fallback for runs whose records
    predate Phase 2.  New lifecycle records compare the already-persisted
    As-is input projection digests; this adapter never produces either digest.
    """
    run_state_path = run_path / "run.json"
    if not run_state_path.is_file():
        return graph_journey_ref == journey_snapshot.get("content_digest")
    run_state = _read_json(run_state_path, "run.json")
    registry_ref = run_state.get("artifact_registry")
    versions = run_state.get("artifact_versions")
    if not isinstance(registry_ref, str) or not isinstance(versions, Mapping):
        return graph_journey_ref == journey_snapshot.get("content_digest")
    registry_path = _resolve_registry_path(run_path, registry_ref)
    registry = _read_yaml(registry_path, "artifact_registry")
    if not isinstance(registry, Mapping):
        return graph_journey_ref == journey_snapshot.get("content_digest")
    records = {
        item.get("version_id"): item
        for item in registry.get("versions", [])
        if isinstance(item, Mapping) and isinstance(item.get("version_id"), str)
    }
    graph_record = records.get(baseline.as_is_version_id)
    journey_version = versions.get("confirmed_journey")
    journey_record = records.get(journey_version)
    graph_projection = graph_record.get("as_is_input_projection_digest") if isinstance(graph_record, Mapping) else None
    journey_projection = journey_record.get("as_is_input_projection_digest") if isinstance(journey_record, Mapping) else None
    if isinstance(graph_projection, str) and isinstance(journey_projection, str):
        return graph_projection == journey_projection
    return graph_journey_ref == journey_snapshot.get("content_digest")


def build_as_is_blueprint_view(run_dir: str | Path) -> DeliveryViewModel:
    """Build a normalized As-is projection from the selected reviewed run."""
    run_path = Path(run_dir).resolve()
    if not run_path.is_dir():
        raise DeliveryError("run_directory_missing", "run", run_path.name)

    try:
        baseline = as_is_research.resolve_baseline(run_path, require_reviewed=True)
    except Exception as exc:
        if isinstance(exc, DeliveryError):
            raise
        raise DeliveryError("as_is_baseline_unavailable", "as_is_graph", type(exc).__name__) from exc

    graph = baseline.graph
    _require_valid(_schema_errors(graph, "service-graph"), "service_graph")
    if graph.get("meta", {}).get("case_id") != baseline.case_id:
        raise DeliveryError("artifact_case_identity_mismatch", "service_graph", graph.get("meta", {}).get("case_id"))
    if graph.get("meta", {}).get("scenario_id") != baseline.scenario_id:
        raise DeliveryError("artifact_scenario_identity_mismatch", "service_graph", graph.get("meta", {}).get("scenario_id"))

    journey_view = build_customer_journey_view(run_path).to_dict()
    journey_doc = _read_yaml(_required_file(run_path, "journey.yaml", "confirmed_journey"), "confirmed_journey")
    journey_snapshot = next(
        (item for item in journey_view.get("source_snapshot", []) if item.get("artifact_type") == "confirmed_journey"),
        None,
    )
    if not isinstance(journey_snapshot, Mapping):
        raise DeliveryError("journey_source_snapshot_missing", "confirmed_journey")
    if not _lineage_is_current_from_lifecycle(
        run_path,
        baseline,
        journey_snapshot,
        graph.get("meta", {}).get("journey_ref"),
    ):
        raise DeliveryError("as_is_journey_lineage_mismatch", "service_graph", graph.get("meta", {}).get("journey_ref"))

    # New workflow (CJM + selected Solutions -> Service Blueprint): there is no
    # Improvement Goal, and none is synthesized. The legacy path is unchanged.
    sbp_workflow = stages.journey_uses_solution_selection(journey_doc)
    goal_doc = None
    goal_text = None
    handoff_by_step: dict[str, Mapping[str, Any]] = {}
    if sbp_workflow:
        handoff_by_step = {
            str(step.get("id")): step
            for step in stages.journey_sbp_handoff(journey_doc).get("steps") or []
            if isinstance(step, Mapping)
        }
    else:
        goal_doc = _read_yaml(_required_file(run_path, "improvement_goal_review.yaml", "improvement_goal_review"), "improvement_goal_review")
        if not isinstance(goal_doc, Mapping) or goal_doc.get("case_id") != baseline.case_id:
            raise DeliveryError("operative_goal_source_mismatch", "improvement_goal_review")
        goal_text = " ".join(str(goal_doc.get("operative_goal") or "").split())
        if goal_text != baseline.improvement_goal:
            raise DeliveryError("operative_goal_source_mismatch", "improvement_goal_review")

    evidence = baseline.evidence
    evidence_ids = {item.get("id") for item in evidence if isinstance(item, Mapping) and isinstance(item.get("id"), str)}
    nodes = [item for item in (graph.get("nodes") or []) if isinstance(item, Mapping)]
    node_ids = {str(node.get("id")) for node in nodes}
    if len(node_ids) != len(nodes):
        raise DeliveryError("duplicate_record_id", "service_graph", "node id")
    node_views = [_node_view(node, evidence_ids) for node in nodes]
    relations = [_relation_view(item, evidence_ids, node_ids) for item in (graph.get("relations") or []) if isinstance(item, Mapping)]
    markers = [_marker_view(item, evidence_ids, node_ids) for item in (graph.get("markers") or []) if isinstance(item, Mapping)]
    annotations = [_annotation_view(item, evidence_ids, node_ids) for item in (graph.get("annotations") or []) if isinstance(item, Mapping)]

    journey_by_scenario, journey_scenarios = _journey_steps(journey_view)
    grouped_nodes: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for node in node_views:
        for scenario_id in node.get("scenario_refs") or []:
            if scenario_id not in journey_by_scenario:
                raise DeliveryError("journey_alignment_invalid", "service_graph", scenario_id)
            step = node.get("step")
            if not isinstance(step, int) or step not in journey_by_scenario[scenario_id]:
                raise DeliveryError("journey_alignment_invalid", "service_graph", f"{scenario_id}:{step}")
            grouped_nodes[(scenario_id, step)].append(node)

    scenarios: list[dict[str, Any]] = []
    for scenario in journey_scenarios:
        scenario_id = str(scenario["scenario_id"])
        steps: list[dict[str, Any]] = []
        for order in sorted(journey_by_scenario[scenario_id]):
            step = journey_by_scenario[scenario_id][order]
            attached = grouped_nodes.get((scenario_id, order), [])
            step_view = {
                "step_id": step.get("step_id"),
                "order": order,
                "label": step.get("label"),
                "description": step.get("description"),
                "evidence_refs": list(step.get("evidence_refs") or []),
                "nodes": copy.deepcopy(attached),
                "has_as_is_nodes": bool(attached),
            }
            if isinstance(step.get("provenance"), Mapping):
                step_view["provenance"] = copy.deepcopy(step["provenance"])
            if sbp_workflow and scenario.get("is_primary"):
                # Only researcher-selected Solutions and their CJM-time Channels;
                # unselected candidates never reach the SBP export.
                handoff = handoff_by_step.get(str(step.get("step_id")), {})
                step_view["customer_actions"] = copy.deepcopy(list(handoff.get("actions") or []))
                step_view["selected_solutions"] = copy.deepcopy(list(handoff.get("selected_solutions") or []))
            steps.append(step_view)
        scenarios.append({
            "scenario_id": scenario_id,
            "label": scenario.get("label"),
            "status": scenario.get("status"),
            "branch_relation": "primary_path" if scenario.get("is_primary") else "alternative_path",
            "is_primary": bool(scenario.get("is_primary")),
            "steps": steps,
        })

    branch_views = _branch_views(journey_view)

    referenced_evidence: set[str] = set()
    for node in node_views:
        referenced_evidence.update(node.get("evidence_refs") or [])
    for relation in relations:
        referenced_evidence.update(relation.get("evidence_refs") or [])
    for marker in markers:
        referenced_evidence.add(str(marker["evidence_ref"]))
    for annotation in annotations:
        referenced_evidence.update(annotation.get("evidence_refs") or [])
    for scenario in scenarios:
        for step in scenario["steps"]:
            referenced_evidence.update(step.get("evidence_refs") or [])
    for branch in branch_views:
        for step in branch["steps"]:
            referenced_evidence.update(step.get("evidence_refs") or [])
    evidence_sources = _evidence_sources(evidence, referenced_evidence)

    layer_counts = {layer: sum(1 for node in node_views if node.get("layer") == layer) for layer in CANONICAL_LAYERS}
    layers = [
        {
            "layer": layer,
            "status": "ready" if layer_counts[layer] else "unavailable",
            "node_count": layer_counts[layer],
            "reason": None if layer_counts[layer] else "canonical_graph_contains_no_nodes_for_this_layer",
        }
        for layer in CANONICAL_LAYERS
    ]

    conflicts_path = run_path / "conflicts.yaml"
    conflicts_doc = _read_yaml(conflicts_path, "conflicts") if conflicts_path.is_file() else None
    source_snapshot = [
        _source_ref("as_is_graph", "service_graph.yaml", graph, version_ref=baseline.as_is_version_id),
        _source_ref("confirmed_journey", "journey.yaml", journey_doc, version_ref=journey_snapshot.get("version_ref")),
        _source_ref("evidence", "evidence.yaml", evidence, version_ref=baseline.evidence_version_id),
        *([] if sbp_workflow else [_source_ref("improvement_goal_review", "improvement_goal_review.yaml", goal_doc)]),
        _source_ref("conflicts", "conflicts.yaml", conflicts_doc, status="ready" if conflicts_doc is not None else "unavailable"),
        _source_ref("issue_package", "issue_package.yaml", None, status="not_applicable", reason="graph_markers_are_rendered; no supplemental issue summary requested"),
    ]

    payload = {
        "view_model_schema_version": 1,
        "delivery_type": AS_IS_BLUEPRINT,
        "export_id": AS_IS_BLUEPRINT,
        "status": "ready",
        "metadata": {
            "project_id": graph.get("meta", {}).get("project_id"),
            "case_id": baseline.case_id,
            "run_ref": run_path.name,
            "scenario_id": baseline.scenario_id,
            "title": SBP_BLUEPRINT_TITLE if stages.journey_uses_solution_selection(journey_doc) else LEGACY_BLUEPRINT_TITLE,
            "review_state": "reviewed",
            "as_is_confirmed": True,
            "graph_version_ref": baseline.as_is_version_id,
            "journey_version_ref": journey_snapshot.get("version_ref"),
            "evidence_version_ref": baseline.evidence_version_id,
        },
        "operative_goal": {
            "text": goal_text,
            "status": "ready",
            "provenance": "researcher_confirmed_operational_goal",
            "source_ref": "improvement_goal_review.yaml",
        },
        "journey_scope": {
            "confirmed": True,
            "primary_scenario": journey_view.get("metadata", {}).get("primary_scenario"),
            "scenarios": copy.deepcopy(scenarios),
        },
        "layers": layers,
        "scenarios": scenarios,
        "nodes": node_views,
        "relations": relations,
        "issues_gaps": markers,
        "annotations": annotations,
        "conflicts": copy.deepcopy(journey_view.get("conflicts") or []),
        "provenance": {
            "graph": {"source_ref": "service_graph.yaml", "version_ref": baseline.as_is_version_id, "status": "confirmed"},
            "journey": {"source_ref": "journey.yaml", "version_ref": journey_snapshot.get("version_ref"), "status": "confirmed"},
            "operative_goal": {"source_ref": "improvement_goal_review.yaml", "status": "researcher_confirmed"},
            "evidence_sources": evidence_sources,
        },
        "availability": {
            "as_is_graph": {"status": "ready", "reason": None},
            "confirmed_journey": {"status": "ready", "reason": None},
            "evidence": {"status": "ready", "reason": None},
            "operative_goal": {"status": "ready", "reason": None},
            "issue_package": {"status": "not_applicable", "reason": "graph markers are used instead of a supplemental issue package"},
            "to_be": {"status": "not_applicable", "reason": "As-is delivery does not read To-be artifacts"},
        },
        "notices": [
            {"code": "mixed_provenance_preserved", "detail": "Mixed nodes retain claim-level observed/inferred distinctions."},
            {"code": "empty_layers_preserved", "detail": "A canonical layer with no nodes remains unavailable; no support activity is inferred."},
            {"code": "causal_inference_preserved", "detail": "Inferred causal/sequence relations remain labelled as inference."},
        ],
        "source_snapshot": source_snapshot,
    }
    if sbp_workflow:
        payload.pop("operative_goal")
        payload["provenance"].pop("operative_goal")
        payload["availability"].pop("operative_goal")
        payload["availability"]["selected_solutions"] = {"status": "ready", "reason": None}
        payload["availability"]["to_be"] = {"status": "not_applicable", "reason": "Service Blueprint export does not read To-be artifacts"}
        payload["notices"].append({
            "code": "selected_solutions_only",
            "detail": "只列研究者在顧客旅程選取的 Solution 與它們在 CJM 產生時決定的管道；未選取的候選不進服務藍圖。",
        })
    if "branches" in journey_view:
        payload["branches"] = branch_views
        payload["availability"]["branch_as_is_alignment"] = {
            "status": "unavailable",
            "reason": "Service Graph v2 has no stable Branch-step binding; no alignment is inferred",
        }
        payload["notices"].append({
            "code": "branch_as_is_alignment_unavailable",
            "detail": "Service Graph v2 未提供 Branch／Branch step 穩定對應；本交付保留 Branch 結構，不推定圖上對齊。",
        })
    return DeliveryViewModel.from_payload(payload)
