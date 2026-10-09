"""Deterministic human-facing Markdown preview for ``as_is_blueprint``."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from .delivery import DeliveryError, DeliveryViewModel
from .delivery_as_is_blueprint import AS_IS_BLUEPRINT, CANONICAL_LAYERS, LEGACY_BLUEPRINT_TITLE, SBP_BLUEPRINT_TITLE


_PROVENANCE_LABELS = {
    "observed": "觀察／證據支持",
    "inferred": "推論",
    "mixed": "混合：觀察與推論分開",
    "researcher_input": "研究者輸入",
    "unresolved": "未確認來源",
}
_STATUS_LABELS = {
    "ready": "可用",
    "unavailable": "未提供",
    "unsupported": "未支持",
    "insufficient_evidence": "證據不足",
    "not_applicable": "不適用",
}
_CHANNEL_LABELS = {
    "physical_service": "服務人員（實體）",
    "physical_communication": "現場溝通（實體）",
    "physical_environment": "實體環境",
    "physical_product": "實體物品",
    "digital_product": "數位工具",
    "digital_environment": "數位環境",
    "digital_service": "線上服務",
    "digital_communication": "線上溝通",
}
# Researcher-facing wording for schema/experience-annotations.schema.json's
# closed `type` enum. 痛點 and 期待 reuse the workbench's existing vocabulary;
# 狀態, 結果 and 接觸點缺席 are product-language decisions taken for DELIV-02.
# 接觸點缺席 is deliberately not bare 缺席: this column stands alone, so the
# label has to say what is absent.
_ANNOTATION_TYPE_LABELS = {
    "pain_point": "痛點",
    "expectation": "期待",
    "state": "狀態",
    "outcome": "結果",
    "channel_absence": "接觸點缺席",
}


def _text(value: Any, fallback: str = "") -> str:
    # Never translate channel tokens here. This runs over labels, descriptions,
    # claims, rationale and transcript-derived prose, where a substring rewrite
    # corrupts unrelated text ("physical_environmental" -> "實體環境al").
    # Channel translation belongs to _channel_label(), at channel fields only.
    return value if isinstance(value, str) else fallback


def _inline(value: Any, fallback: str = "未提供") -> str:
    if value is None:
        return fallback
    return str(value).replace("`", "\\`").replace("\n", " ").replace("\r", " ")


def _cell(value: Any, fallback: str = "未提供") -> str:
    return _inline(value, fallback).replace("|", "\\|")


def _refs(values: Any) -> str:
    if not isinstance(values, list) or not values:
        return "尚未記錄"
    return "、".join(f"`{_inline(value)}`" for value in values)


def _records(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _channel_label(value: Any) -> str:
    """Translate a channel-typed field by exact match; never by substring."""
    label = _CHANNEL_LABELS.get(_text(value))
    return f"{label}（圖上已記錄）" if label else "未對應的接觸點類型（圖上已記錄）"


# CJM-time Channel categories of a selected Solution (journey contract v0.4.0).
_SOLUTION_CHANNEL_LABELS = {
    "people": "人員",
    "physical_environment": "實體環境",
    "digital_tools": "數位工具",
    "digital_environments": "數位環境",
}
_BASIS_LABELS = {"evidence": "證據支持", "inference": "推論", "researcher": "研究者輸入"}


def _selected_solution_lines(step: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    actions = [_text(item.get("text")) for item in _records(step.get("customer_actions")) if _text(item.get("text"))]
    lines.append(f"- 顧客行動：{_cell('、'.join(actions)) if actions else '未記錄'}。")
    selected = _records(step.get("selected_solutions"))
    if not selected:
        lines.append("- 研究者選取的 Solution：本步驟沒有。")
        return lines
    for solution in selected:
        basis = _BASIS_LABELS.get(_text(solution.get("basis")), "未確認來源")
        lines.append(f"- 研究者選取的 Solution：{_cell(_text(solution.get('text'), '未提供'))}（{basis}；選取：研究者）")
        channels = solution.get("channels") if isinstance(solution.get("channels"), Mapping) else {}
        for key, label in _SOLUTION_CHANNEL_LABELS.items():
            for role in _records(channels.get(key)):
                role_basis = _BASIS_LABELS.get(_text(role.get("basis")), "未確認來源")
                lines.append(f"  - 管道・{label}：{_cell(_text(role.get('text')))}（{role_basis}）")
        if not any(_records(channels.get(key)) for key in _SOLUTION_CHANNEL_LABELS):
            lines.append("  - 管道：CJM 沒有記錄。")
    return lines


def _annotation_type_label(value: Any) -> str:
    """Translate an annotation-type field by exact match.

    The enum is closed, so an unrecognised value means the graph never passed
    validation. Rendering the raw token would leak schema vocabulary and
    guessing a generic label would hide the problem, so fail like every other
    unusable delivery source.
    """
    label = _ANNOTATION_TYPE_LABELS.get(_text(value))
    if label is None:
        raise DeliveryError(
            "service_graph_annotation_type_unsupported", "service_graph", _text(value, "未提供")
        )
    return label


def _provenance(value: Any) -> str:
    return _PROVENANCE_LABELS.get(_text(value), _text(value, "未確認來源"))


def _claim_summary(claims: Any) -> str:
    records = _records(claims)
    if not records:
        return "尚未提供 claim"
    values: list[str] = []
    for claim in records:
        label = _provenance(claim.get("provenance"))
        text = _text(claim.get("text"), "未提供 claim 文字")
        refs = f"；證據 {claim.get('evidence_ref')}" if claim.get("evidence_ref") else ""
        values.append(f"{label}：{text}{refs}")
    return "；".join(values)


def _node_content(node: Mapping[str, Any], solutions: Mapping[str, str] | None = None) -> str:
    label = _text(node.get("label"), "未命名節點")
    description = _text(node.get("description"))
    claims = _claim_summary(node.get("claims"))
    lines = [f"{label}（{_inline(node.get('node_id'))}）"]
    if description:
        lines.append(description)
    if solutions is not None and node.get("selected_solution_ref"):
        lines.append(f"實現選取的 Solution：{solutions.get(_text(node.get('selected_solution_ref')), '未對應到選取的 Solution')}")
    if claims:
        lines.append(f"Claim：{claims}")
    return "；".join(lines)


def _render_source_snapshot(lines: list[str], sources: list[Mapping[str, Any]]) -> None:
    lines.extend(["", "## 附錄：Source Snapshot", ""])
    lines.extend(["| Artifact | Source | Status | Version / Digest |", "| --- | --- | --- | --- |"])
    for source in sources:
        status = _STATUS_LABELS.get(_text(source.get("status")), _text(source.get("status"), "未提供"))
        version = source.get("version_ref") or source.get("content_digest") or "未提供"
        lines.append(
            f"| {_cell(source.get('artifact_type'))} | {_cell(source.get('source_ref'))} | "
            f"{_cell(status)} | {_cell(version)} |"
        )


def render_as_is_blueprint_markdown(view_model: DeliveryViewModel) -> str:
    """Render only the normalized As-is view; no source reads or writes."""
    if not isinstance(view_model, DeliveryViewModel):
        raise DeliveryError("renderer_invalid_input", "delivery_view", "expected DeliveryViewModel")
    payload = view_model.to_dict()
    if payload.get("export_id") != AS_IS_BLUEPRINT or payload.get("delivery_type") != AS_IS_BLUEPRINT:
        raise DeliveryError("renderer_unsupported_delivery_type", "delivery_view", payload.get("export_id"))

    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    goal = payload.get("operative_goal") if isinstance(payload.get("operative_goal"), Mapping) else {}
    journey_scope = payload.get("journey_scope") if isinstance(payload.get("journey_scope"), Mapping) else {}
    layers = _records(payload.get("layers"))
    scenarios = _records(payload.get("scenarios"))
    has_v3_branch_surface = "branches" in payload
    branches = _records(payload.get("branches"))
    relations = _records(payload.get("relations"))
    markers = _records(payload.get("issues_gaps"))
    annotations = _records(payload.get("annotations"))
    conflicts = _records(payload.get("conflicts"))
    availability = payload.get("availability") if isinstance(payload.get("availability"), Mapping) else {}
    source_snapshot = _records(payload.get("source_snapshot"))
    notices = _records(payload.get("notices"))

    title = metadata.get("title") if metadata.get("title") in {LEGACY_BLUEPRINT_TITLE, SBP_BLUEPRINT_TITLE} else LEGACY_BLUEPRINT_TITLE
    sbp_workflow = title == SBP_BLUEPRINT_TITLE
    lines = [f"# {title}", "", "## Overview", ""]
    lines.append(f"- 個案：`{_inline(metadata.get('case_id'))}`")
    lines.append(f"- 分析執行：`{_inline(metadata.get('run_ref'))}`")
    graph_label = "服務藍圖" if sbp_workflow else "As-is graph"
    lines.append(f"- {graph_label}：`{_inline(metadata.get('graph_version_ref'))}`；狀態：已確認、已審閱。")
    lines.append(f"- Journey 對齊：`{_inline(metadata.get('journey_version_ref'))}`；只使用 current confirmed Journey。")
    if sbp_workflow:
        lines.append("- 本報告描述已確認的服務藍圖，含研究者在顧客旅程選取的 Solution；不是 To-be 方案。")
    else:
        lines.append(f"- 本報告描述現況服務系統；不把 As-is 內容轉寫成 To-be 方案。")
    if goal.get("text"):
        lines.append(f"- 現行 operative Improvement Goal：{_text(goal.get('text'))}")
        lines.append("  - 來源：`improvement_goal_review.yaml`；不回退到 `case.yaml.improvement_goal`。")

    lines.extend(["", "## Provenance rule", ""])
    lines.append("觀察／證據支持、推論與混合節點分開呈現；推論節點或關係不會因為旁邊有 evidence ref 就升級成觀察事實。")

    lines.extend(["", "## Blueprint", ""])
    for scenario in scenarios:
        relation = "主要路徑" if scenario.get("is_primary") else "替代／離開路徑"
        lines.extend([
            f"### {_text(scenario.get('label'), '未命名場景')}",
            "",
            f"- 路徑性質：{relation}（`{_inline(scenario.get('scenario_id'))}`；狀態：{_inline(scenario.get('status'))}）。",
        ])
        for step in _records(scenario.get("steps")):
            lines.extend(["", f"#### {step.get('order', '?')} · {_text(step.get('label'), '未命名步驟')}", ""])
            description = _text(step.get("description"))
            if description:
                lines.append(f"Journey 觀察描述：{description}")
            step_provenance = step.get("provenance") if isinstance(step.get("provenance"), Mapping) else None
            if step_provenance is not None:
                lines.append(f"Journey content provenance：{_cell(step_provenance.get('content_status', '未提供'))}")
            lines.append(f"Journey evidence：{_refs(step.get('evidence_refs'))}")
            solutions = None
            if sbp_workflow and "selected_solutions" in step:
                lines.extend(_selected_solution_lines(step))
                solutions = {_text(item.get("id")): _text(item.get("text")) for item in _records(step.get("selected_solutions"))}
            node_header = "服務節點／內容" if sbp_workflow else "現況節點／內容"
            lines.extend(["", f"| 服務層 | {node_header} | Provenance | Evidence |", "| --- | --- | --- | --- |"])
            nodes_by_layer: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
            for node in _records(step.get("nodes")):
                nodes_by_layer[_text(node.get("layer"), "未標示層")].append(node)
            for layer in CANONICAL_LAYERS:
                if sbp_workflow and layer == "後台行動":
                    lines.append("| 可見線（Line of Visibility） | 以上顧客看得到；以下顧客看不到 | — | — |")
                nodes = nodes_by_layer.get(layer, [])
                if not nodes:
                    continue
                for node in nodes:
                    lines.append(
                        f"| {_cell(layer)} | {_cell(_node_content(node, solutions))} | "
                        f"{_cell(_provenance(node.get('provenance')))} | {_refs(node.get('evidence_refs'))} |"
                    )
            missing_layers = [layer for layer in CANONICAL_LAYERS if not nodes_by_layer.get(layer)]
            if missing_layers:
                lines.append(f"- 本步驟未提供層：{_cell('、'.join(missing_layers))}。")
            channel_values = [
                _channel_label(node.get("channel"))
                for node in _records(step.get("nodes"))
                if node.get("channel")
            ]
            channel_text = "、".join(channel_values) if channel_values else "尚未記錄；不在交付層推定"
            lines.append(f"- 圖上已記錄渠道：{_cell(channel_text)}。")

    if has_v3_branch_surface:
        lines.extend(["", "## Branches", ""])
        lines.append("Branch 保留為 Journey 內的條件路徑；Service Graph v2 沒有穩定的 Branch-step binding，因此不推定 As-is 圖上節點對齊。")
        if branches:
            for branch in branches:
                lines.extend([
                    "",
                    f"### {_text(branch.get('label'), '未命名 Branch')}",
                    "",
                    f"- Branch ID：`{_inline(branch.get('branch_id'))}`",
                    f"- divergence anchor：`{_inline(branch.get('divergence_step_id'))}`（primary step_id）",
                    f"- merge anchor：`{_inline(branch.get('merge_step_id')) if branch.get('merge_step_id') else '無；為有效的自然結束'}`",
                    f"- topology provenance：{_inline(branch.get('topology_provenance'), '未提供')}",
                    "",
                    "| Branch 步驟 | 研究描述 | 內容 provenance | Journey evidence | As-is 圖上節點 |",
                    "| --- | --- | --- | --- | --- |",
                ])
                for step in _records(branch.get("steps")):
                    provenance = step.get("provenance") if isinstance(step.get("provenance"), Mapping) else {}
                    content_status = provenance.get("content_status", "未提供")
                    branch_step_title = f"{step.get('order', '?')} · {step.get('step_id')} · {step.get('label')}"
                    lines.append(
                        f"| {_cell(branch_step_title)} "
                        f"| {_cell(step.get('description'))} "
                        f"| {_cell(content_status)} "
                        f"| {_refs(step.get('evidence_refs'))} "
                        f"| {_cell('未對齊；無穩定 Branch-step binding')} |"
                    )
        else:
            lines.append("目前沒有 canonical Branch；v3 的 Branch read surface 保留為空。")

    lines.extend(["", "## Layer availability", ""])
    lines.extend(["| Canonical layer | Status | Node count |", "| --- | --- | --- |"])
    for layer in layers:
        status = _STATUS_LABELS.get(_text(layer.get("status")), _text(layer.get("status"), "未提供"))
        count = layer.get("node_count", 0)
        lines.append(f"| {_cell(layer.get('layer'))} | {_cell(status)} | {_cell(count)} |")
    lines.append("空白層保留為空白；本交付不從一般服務常識補寫支援流程或後台活動。")

    lines.extend(["", "## Issues / Gaps（canonical graph markers）", ""])
    if markers:
        lines.extend(["| Marker | 類型 | Node | Evidence | Canonical note |", "| --- | --- | --- | --- | --- |"])
        for marker in markers:
            lines.append(
                f"| {_cell(marker.get('id'))} | {_cell(marker.get('type'))} | {_cell(marker.get('node_ref'))} | "
                f"{_refs([marker.get('evidence_ref')])} | {_cell(marker.get('note'))} |"
            )
    else:
        lines.append("目前沒有 canonical graph marker；本交付不自行補寫 issue。")
    lines.append("Markers 標出服務中的失誤、等候、決策與放棄點；不是新的方案。" if sbp_workflow
                 else "Markers 用來呈現現況中的失誤、等候、決策與放棄點；不等同於 To-be 建議。")

    lines.extend(["", "## Experience annotations", ""])
    if annotations:
        lines.extend(["| Annotation | 類型 | 內容 | Evidence |", "| --- | --- | --- | --- |"])
        for annotation in annotations:
            lines.append(
                f"| {_cell(annotation.get('id'))} | {_cell(_annotation_type_label(annotation.get('type')))} | {_cell(annotation.get('text'))} | "
                f"{_refs(annotation.get('evidence_refs'))} |"
            )
    else:
        lines.append("目前沒有 canonical annotation。")

    lines.extend(["", "## Relations / provenance notes", ""])
    if relations:
        lines.extend(["| Relation | Type | From → To | Provenance | Evidence | Note |", "| --- | --- | --- | --- | --- | --- |"])
        for relation in relations:
            endpoints = f"{relation.get('from') or relation.get('endpoints', ['?'])[0]} → {relation.get('to') or relation.get('endpoints', ['?', '?'])[-1]}"
            lines.append(
                f"| {_cell(relation.get('relation_id'))} | {_cell(relation.get('type'))} | {_cell(endpoints)} | "
                f"{_cell(_provenance(relation.get('provenance')))} | {_refs(relation.get('evidence_refs'))} | {_cell(relation.get('note'))} |"
            )
    else:
        lines.append("目前沒有 canonical relation。")
    lines.append("Sequence、association 與 causal relation 保留 canonical type；本預覽不把 sequence 自動改寫成因果。")

    lines.extend(["", "## Conflicts / Limitations", ""])
    if conflicts:
        lines.append("以下衝突維持原狀，不在交付層替研究資料裁定：")
        for conflict in conflicts:
            lines.append(f"### {_inline(conflict.get('id'))}")
            lines.append(_text(conflict.get("description"), "未提供衝突描述。"))
            lines.append(f"- Evidence：{_refs(conflict.get('evidence_refs'))}")
    else:
        lines.append("目前沒有可用的 conflict record；本預覽不自行補寫衝突。")
    lines.append("Supplemental issue_package 未納入本次 projection；graph markers 已保留，避免以缺少的舊檔案替代目前的服務藍圖。" if sbp_workflow
                 else "Supplemental issue_package 未納入本次 projection；graph markers 已保留，避免以缺少的舊檔案替代 current As-is authority。")

    lines.extend(["", "## Availability", ""])
    for key, state in availability.items():
        item = state if isinstance(state, Mapping) else {}
        label = {"as_is_graph": "服務藍圖" if sbp_workflow else "As-is graph", "selected_solutions": "研究者選取的 Solution", "confirmed_journey": "Confirmed Journey", "evidence": "Evidence", "operative_goal": "Operative Improvement Goal", "issue_package": "Supplemental issue package", "to_be": "To-be"}.get(key, key)
        status = _STATUS_LABELS.get(_text(item.get("status")), _text(item.get("status"), "未提供"))
        lines.append(f"- {label}：{status}。" + (f" {_text(item.get('reason'))}" if item.get("reason") else ""))
    for notice in notices:
        if notice.get("detail"):
            lines.append(f"- 提醒：{_inline(notice.get('detail'))}")

    lines.extend(["", "## 附錄：Evidence Source Index", ""])
    evidence_sources = _records((payload.get("provenance") or {}).get("evidence_sources") if isinstance(payload.get("provenance"), Mapping) else [])
    if evidence_sources:
        lines.extend(["| Evidence ID | 來源 | 定位 |", "| --- | --- | --- |"])
        for evidence in evidence_sources:
            lines.append(f"| {_cell(evidence.get('evidence_id'))} | {_cell(evidence.get('source'))} | {_cell(evidence.get('locator'))} |")
    else:
        lines.append("尚未提供可追溯的 Evidence source index。")
    _render_source_snapshot(lines, source_snapshot)
    return "\n".join(line.rstrip() for line in lines).strip() + "\n"
