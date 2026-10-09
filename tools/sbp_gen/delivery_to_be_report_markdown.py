"""Deterministic human-facing Markdown preview for ``to_be_report``."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .delivery import DeliveryError, DeliveryViewModel
from .delivery_to_be_report import TO_BE_REPORT


_STATUS_LABELS = {
    "ready": "可用",
    "unsupported": "未支持",
    "unavailable": "未提供",
    "insufficient_evidence": "證據不足",
    "not_applicable": "不適用",
    "error": "錯誤",
}
_DECISION_LABELS = {"select": "SELECT／可支持", "hold": "HOLD／條件或問題", "reject": "REJECT／排除"}
_OPERATION_LABELS = {"add": "新增", "modify": "修改", "retain": "保留", "remove": "移除"}


def _text(value: Any, fallback: str = "") -> str:
    return value if isinstance(value, str) else fallback


def _inline(value: Any, fallback: str = "未提供") -> str:
    if value is None:
        return fallback
    return str(value).replace("`", "\\`").replace("\n", " ").replace("\r", " ")


def _cell(value: Any, fallback: str = "未提供") -> str:
    return _inline(value, fallback).replace("|", "\\|")


def _records(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _refs(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return "尚未記錄"
    return "、".join(f"`{_inline(item)}`" for item in value)


def _status(value: Any) -> str:
    return _STATUS_LABELS.get(_text(value), _text(value, "未提供"))


def _render_validation(lines: list[str], plan: Mapping[str, Any]) -> None:
    if not plan:
        lines.append("尚未提供 validation plan。")
        return
    lines.extend(["| 欄位 | Canonical 內容 |", "| --- | --- |"])
    for key, label in (
        ("decision_rule", "決策規則"),
        ("method", "方法"),
        ("metric", "指標"),
        ("pilot_scope", "試行範圍"),
        ("success_condition", "成功條件"),
        ("target_outcome", "目標結果"),
    ):
        if key in plan:
            lines.append(f"| {_cell(label)} | {_cell(plan.get(key))} |")


def _render_source_snapshot(lines: list[str], sources: list[Mapping[str, Any]]) -> None:
    lines.extend(["", "## 附錄：Source Snapshot", "", "| Artifact | Source | Status | Version / Digest |", "| --- | --- | --- | --- |"])
    for source in sources:
        version = source.get("version_ref") or source.get("content_digest") or "未提供"
        lines.append(
            f"| {_cell(source.get('artifact_type'))} | {_cell(source.get('source_ref'))} | "
            f"{_cell(_status(source.get('status')))} | {_cell(version)} |"
        )


def render_to_be_report_markdown(view_model: DeliveryViewModel) -> str:
    """Render only the normalized To-be view; no source reads or writes."""
    if not isinstance(view_model, DeliveryViewModel):
        raise DeliveryError("renderer_invalid_input", "delivery_view", "expected DeliveryViewModel")
    payload = view_model.to_dict()
    if payload.get("export_id") != TO_BE_REPORT or payload.get("delivery_type") != TO_BE_REPORT:
        raise DeliveryError("renderer_unsupported_delivery_type", "delivery_view", payload.get("export_id"))

    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    goal = payload.get("operative_goal") if isinstance(payload.get("operative_goal"), Mapping) else {}
    direction = payload.get("direction") if isinstance(payload.get("direction"), Mapping) else {}
    external = payload.get("external_research") if isinstance(payload.get("external_research"), Mapping) else {}
    availability = payload.get("availability") if isinstance(payload.get("availability"), Mapping) else {}
    notices = _records(payload.get("notices"))
    proposals = _records(payload.get("proposals"))
    issue_coverage = _records(payload.get("issue_coverage"))
    changes = _records(payload.get("blueprint_changes"))
    revision = payload.get("revision_context") if isinstance(payload.get("revision_context"), Mapping) else {}

    lines = ["# To-be Service Report", "", "## Overview", ""]
    lines.append(f"- 個案：`{_inline(metadata.get('case_id'))}`；情境：`{_inline(metadata.get('scenario_id'))}`。")
    lines.append(f"- Current To-be bundle：`{_inline(metadata.get('to_be_version_ref'))}`。")
    lines.append(f"- To-be review state：`{_inline(metadata.get('to_be_review_state'))}`。")
    lines.append(f"- 方案狀態：`{_inline(metadata.get('proposal_state'))}`；提案不是觀察結果，也不是已核准成效。")
    if goal.get("text"):
        lines.append(f"- Operative Improvement Goal：{_text(goal.get('text'))}")
        lines.append("  - 來源：`improvement_goal_review.yaml`；不回退到原始 `case.yaml.improvement_goal`。")
    if direction.get("title"):
        lines.extend(["", f"### 方向：{_text(direction.get('title'))}", "", _text(direction.get("summary"), "尚未提供方向摘要。")])
    lines.append("本報告描述一份仍需驗證的 To-be 提案；不把外部研究前例或預期改變寫成 MUJI 使用者已發生的證據。")

    lines.extend(["", "## Design Drivers（SELECT）", ""])
    mechanisms = _records(external.get("selected_mechanisms"))
    selected_claims = _records(external.get("selected_claims"))
    if mechanisms:
        lines.extend(["| Mechanism | 研究者確認的轉移方式 | SELECT claims |", "| --- | --- | --- |"])
        for item in mechanisms:
            lines.append(
                f"| {_cell(item.get('name'))}（`{_cell(item.get('id'))}`） | {_cell(item.get('description'))} | "
                f"{_refs(item.get('derived_from_claim_refs'))} |"
            )
    else:
        lines.append("目前沒有可用的 SELECT mechanism；不自行補寫 design driver。")
    if selected_claims:
        lines.extend(["", "SELECT claim 的內容僅作為可轉移機制支持，不是 MUJI 使用者證據：", ""])
        for claim in selected_claims:
            lines.append(f"- `{_inline(claim.get('id'))}`：{_text(claim.get('statement') or claim.get('text'), '未提供 claim 內容。')}")

    hold_conditions = _records(external.get("hold_conditions"))
    hold_questions = _records(external.get("hold_questions"))
    lines.extend(["", "### HOLD conditions / questions", ""])
    if hold_conditions or hold_questions:
        for item in [*hold_conditions, *hold_questions]:
            lines.append(
                f"- `{_inline(item.get('id'))}`（HOLD／不作 proposal support）：{_text(item.get('text'), '未提供條件或問題。')}；來源 claims：{_refs(item.get('derived_from_claim_refs'))}"
            )
    else:
        lines.append("沒有 HOLD-only condition/question。")
    lines.append("REJECT claims 不進入 design driver 或 proposal support；外部研究不等於 MUJI 使用者證據。")

    lines.extend(["", "## Proposal Cards", ""])
    if not proposals:
        lines.append("目前沒有 proposal；不為完整性發明方案。")
    for proposal in proposals:
        proposal_id = _inline(proposal.get("id"), "未命名提案")
        lines.extend([f"### {proposal_id}：{_text(proposal.get('statement'), '未提供提案內容。')}", ""])
        lines.append(f"- Claim state：`{_inline(proposal.get('claim_state'), 'proposal')}`。")
        lines.append(f"- Addressed issue：{_refs(proposal.get('issue_refs'))}")
        lines.append(f"- Research support：SELECT claims {_refs(proposal.get('research_claim_refs'))}；source refs {_refs(proposal.get('source_refs'))}。")
        lines.append(f"- Expected change：{_text(proposal.get('expected_change'), '未提供預期改變。')}")
        lines.extend(["", "#### Assumptions（hypothesis）", ""])
        for item in _records(proposal.get("assumptions")):
            lines.append(f"- {_text(item.get('text'), '未提供假設。')}")
        lines.extend(["", "#### Risks / limitations", ""])
        risks = proposal.get("risks") or []
        if risks:
            lines.extend(f"- {_text(item)}" for item in risks)
        else:
            lines.append("未提供風險或限制。")
        lines.extend(["", "#### Validation plan", ""])
        _render_validation(lines, proposal.get("validation_plan") if isinstance(proposal.get("validation_plan"), Mapping) else {})

    lines.extend(["", "## Intervention Map / To-be Blueprint", ""])
    if changes:
        lines.extend(["| Change | Operation | As-is ref | Step / layer | Description | Proposal refs |", "| --- | --- | --- | --- | --- | --- |"])
        for change in changes:
            step_layer = "／".join(str(value) for value in (change.get("step"), change.get("layer")) if value is not None) or "未提供"
            lines.append(
                f"| {_cell(change.get('id'))} | {_cell(_OPERATION_LABELS.get(_text(change.get('operation')), _text(change.get('operation'), '未提供')))} | "
                f"{_cell(change.get('as_is_element_ref'))} | {_cell(step_layer)} | {_cell(change.get('description'))} | {_refs(change.get('proposal_refs'))} |"
            )
    else:
        lines.append("目前沒有 blueprint change；不自行補寫 To-be 元件。")
    lines.append("Operation 的 retain／modify／add 保留 canonical 區分；這裡的改變仍是 proposal，不是已部署服務。")

    lines.extend(["", "## Issue Coverage / Unsupported Areas", ""])
    if issue_coverage:
        lines.extend(["| Issue | Status | Proposal refs | Explanation |", "| --- | --- | --- | --- |"])
        for item in issue_coverage:
            issue = item.get("issue") if isinstance(item.get("issue"), Mapping) else {}
            issue_label = _text(issue.get("statement") or issue.get("title") or issue.get("description"), "未提供 issue 描述")
            status = _text(item.get("status"), "未提供")
            reason = item.get("explanation") or item.get("reason") or ""
            lines.append(f"| {_cell(item.get('issue_ref'))}：{_cell(issue_label)} | {_cell(status)} | {_refs(item.get('proposal_refs'))} | {_cell(reason)} |")
    else:
        lines.append("尚未提供 issue coverage。")
    lines.append("沒有足夠 SELECT 支持的 issue 維持 not_addressed；不為完整性建立 unsupported proposal。")

    lines.extend(["", "## Proposal Decision / Validation Lineage", ""])
    if revision:
        lines.append(f"- Revision kind：`{_inline(revision.get('kind'))}`。")
        if revision.get("prior_to_be_version_ref"):
            lines.append(f"- Previous To-be：`{_inline(revision.get('prior_to_be_version_ref'))}`；本報告只呈現 current bundle。")
        decisions = _records(revision.get("decisions"))
        if decisions:
            lines.extend(["", "| Proposal | Decision | Researcher instruction |", "| --- | --- | --- |"])
            for decision in decisions:
                lines.append(f"| {_cell(decision.get('proposal_ref'))} | {_cell(decision.get('decision'))} | {_cell(decision.get('instruction'))} |")
    else:
        lines.append("沒有額外的 revision decision lineage。")
    lines.append("Review state 與 proposal claim state 必須分開理解：accepted_pending_validation 仍不是已驗證成效或已核准執行。")

    lines.extend(["", "## Risks / Limitations / Unresolved", ""])
    lines.append("- Validation plan 的 success condition 是未來試行的判準，不是現況成效。");
    lines.append("- HOLD 條件與問題保留為檢查項，不得被當成 proposal support。");
    lines.append("- As-is、issue、external research 與 To-be 的 source identity 保留在下方附錄；本交付不改寫上游證據。");

    lines.extend(["", "## Availability", ""])
    for key, value in availability.items():
        item = value if isinstance(value, Mapping) else {}
        lines.append(f"- `{_inline(key)}`：{_status(item.get('status'))}。" + (f" {_text(item.get('reason'))}" if item.get('reason') else ""))
    for notice in notices:
        if notice.get("detail"):
            lines.append(f"- 提醒：{_inline(notice.get('detail'))}")

    lines.extend(["", "## 附錄：External Research Decisions", ""])
    decisions = _records(external.get("decisions"))
    if decisions:
        lines.extend(["| Claim | Decision | Researcher rationale |", "| --- | --- | --- |"])
        for item in decisions:
            label = _DECISION_LABELS.get(_text(item.get('decision')), _text(item.get('decision'), '未提供'))
            lines.append(f"| {_cell(item.get('claim_ref'))} | {_cell(label)} | {_cell(item.get('researcher_rationale'))} |")
    else:
        lines.append("尚未提供 external research decision records。")

    _render_source_snapshot(lines, _records(payload.get("source_snapshot")))
    return "\n".join(line.rstrip() for line in lines).strip() + "\n"
