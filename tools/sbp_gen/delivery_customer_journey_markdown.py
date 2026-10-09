"""Deterministic human-facing Markdown preview for Customer Journey delivery."""
from __future__ import annotations

from typing import Any, Mapping

from .delivery import DeliveryError, DeliveryViewModel


CUSTOMER_JOURNEY = "customer_journey"


def _text(value: Any, fallback: str = "") -> str:
    return value if isinstance(value, str) else fallback


def _inline(value: Any, fallback: str = "未提供") -> str:
    text = _text(value, fallback)
    return text.replace("`", "\\`").replace("\n", " ")


def _refs(values: Any) -> str:
    if not isinstance(values, list) or not values:
        return "尚未記錄"
    return "、".join(f"`{_inline(value)}`" for value in values)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _records(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _status_text(status: Any) -> str:
    return {
        "ready": "可用",
        "unresolved": "尚未確認",
        "unsupported": "未支持",
        "unavailable": "未提供",
        "insufficient_evidence": "證據不足",
    }.get(_text(status), _text(status, "未提供"))


def _cell(value: Any) -> str:
    return _inline(value).replace("|", "\\|")


def _content_provenance(step: Mapping[str, Any]) -> str:
    provenance = _mapping(step.get("provenance"))
    return _text(provenance.get("content_status") or provenance.get("status"), "未提供")


def render_customer_journey_markdown(view_model: DeliveryViewModel) -> str:
    """Render only the normalized Customer Journey view; no source reads/writes."""
    if not isinstance(view_model, DeliveryViewModel):
        raise DeliveryError("renderer_invalid_input", "delivery_view", "expected DeliveryViewModel")
    payload = view_model.to_dict()
    if payload.get("export_id") != CUSTOMER_JOURNEY or payload.get("delivery_type") != CUSTOMER_JOURNEY:
        raise DeliveryError("renderer_unsupported_delivery_type", "delivery_view", payload.get("export_id"))

    metadata = _mapping(payload.get("metadata"))
    scenarios = _records(payload.get("scenarios"))
    has_v3_branch_surface = "branches" in payload
    branches = _records(payload.get("branches"))
    availability = _mapping(payload.get("availability"))
    unresolved = _mapping(_mapping(payload.get("unresolved_mappings")).get("channel"))
    conflicts = _records(payload.get("conflicts"))
    evidence_sources = _records(_mapping(payload.get("provenance")).get("evidence_sources"))
    source_snapshot = _records(payload.get("source_snapshot"))
    notices = _records(payload.get("notices"))

    lines = ["# Customer Journey", "", "## Journey Overview", ""]
    lines.append(f"- 個案：`{_inline(metadata.get('case_id'))}`")
    lines.append(f"- 分析執行：`{_inline(metadata.get('run_ref'))}`")
    lines.append(f"- Journey 狀態：`{_inline(metadata.get('journey_state'))}`；確認場景：{len(scenarios)} 個。")
    counts = metadata.get("confirmed_step_counts") or {}
    if counts:
        lines.append("- 確認步驟：" + "；".join(
            f"`{_inline(key)}` {value} 步" for key, value in counts.items()
        ) + "。")
    lines.append("- 這份交付保留完整用餐路徑與未進店替代路徑；不把替代路徑攤平成主要流程。")
    lines.append("- 觸點／渠道分類尚未全部確認，但依現行 Journey 契約不阻擋閱讀或繼續下游工作。")

    for scenario in scenarios:
        scenario_id = _inline(scenario.get("scenario_id"))
        heading = "Scenario 1" if scenario.get("is_primary") else "Scenario 2"
        lines.extend(["", f"## {heading}：{_inline(scenario.get('label'))}", ""])
        relation = "主要路徑" if scenario.get("is_primary") else "替代／離開路徑"
        lines.append(f"- 路徑性質：{relation}（`{scenario_id}`；狀態：{_inline(scenario.get('status'))}）。")
        goal = _text(scenario.get("customer_goal"))
        if goal:
            lines.append(f"- 顧客目標：{goal}")
        description = _text(scenario.get("description"))
        if description:
            lines.append(f"- 路徑說明：{description}")
        if has_v3_branch_surface:
            lines.extend([
                "", "| 步驟 | 行動 | 觀察條件／研究描述 | 觸點／渠道 | 內容 provenance | 證據 |",
                "| --- | --- | --- | --- | --- | --- |",
            ])
        else:
            lines.extend(["", "| 步驟 | 行動 | 觀察條件／研究描述 | 觸點／渠道 | 證據 |", "| --- | --- | --- | --- | --- |"])
        for step in _records(scenario.get("steps")):
            channel = _mapping(step.get("touchpoint_channel"))
            channel_text = _status_text(channel.get("status"))
            if channel.get("blocking") is False:
                channel_text += "（不阻擋）"
            step_title = f"{step.get('order', '?')} · {_text(step.get('label'))}"
            row = (
                f"| {_cell(step_title)} "
                f"| {_cell(step.get('label'))} "
                f"| {_cell(step.get('description'))} "
                f"| {_cell(channel_text)} "
            )
            if has_v3_branch_surface:
                row += f"| {_cell(_content_provenance(step))} "
            lines.append(row + f"| {_refs(step.get('evidence_refs'))} |")

    if has_v3_branch_surface:
        lines.extend(["", "## Branches", ""])
        if branches:
            lines.append("Branch 保留為 Journey 內獨立的條件路徑；不攤平到主要路徑，也不轉寫成替代情境。")
            for branch in branches:
                lines.extend([
                    "",
                    f"### {_inline(branch.get('label'), '未命名 Branch')}",
                    "",
                    f"- Branch ID：`{_inline(branch.get('branch_id'))}`",
                    f"- divergence anchor：`{_inline(branch.get('divergence_step_id'))}`（primary step_id）",
                    f"- merge anchor：`{_inline(branch.get('merge_step_id')) if branch.get('merge_step_id') else '無；為有效的自然結束'}`",
                    f"- topology provenance：{_inline(branch.get('topology_provenance'), '未提供')}",
                    "",
                    "| Branch 步驟 | 行動 | 研究描述 | 內容 provenance | 證據 |",
                    "| --- | --- | --- | --- | --- |",
                ])
                for step in _records(branch.get("steps")):
                    branch_step_title = f"{step.get('order', '?')} · {step.get('step_id')}"
                    lines.append(
                        f"| {_cell(branch_step_title)} "
                        f"| {_cell(step.get('label'))} "
                        f"| {_cell(step.get('description'))} "
                        f"| {_cell(_content_provenance(step))} "
                        f"| {_refs(step.get('evidence_refs'))} |"
                    )
        else:
            lines.append("目前沒有 canonical Branch；v3 的 Branch read surface 保留為空。")

    lines.extend(["", "## Unresolved Mapping", ""])
    lines.append(
        f"渠道／觸點尚未確認：{unresolved.get('count', 0)} 個步驟。"
        "這是交付時保留的未決分類，不是 Journey 失敗，也不要求系統代填。"
    )
    lines.append("- 狀態：尚未確認；blocking：否。")
    reason = _text(unresolved.get("reason"))
    if reason:
        lines.append(f"- 原因：{reason}")

    lines.extend(["", "## Evidence / Provenance Notes", ""])
    lines.append("每個步驟保留 canonical evidence IDs；Journey 描述與證據引用來自 `journey.yaml`。本預覽不把其他站點的 framing 當成 Journey 證據。")
    lines.append(f"本次 Journey 引用 {len(evidence_sources)} 則證據；完整來源定位列於附錄。")

    lines.extend(["", "## Limitations / Conflicts", ""])
    if conflicts:
        lines.append("以下衝突維持原狀，不在交付層替兩種說法裁定：")
        for conflict in conflicts:
            lines.append(f"### {_inline(conflict.get('id'))}")
            lines.append(_text(conflict.get("description"), "未提供衝突描述。"))
            lines.append(f"- 相關證據：{_refs(conflict.get('evidence_refs'))}")
    else:
        lines.append("目前沒有可用的 Journey conflict record；本預覽不自行補寫衝突。")
    lines.append("- 情緒曲線、NPS、機會與方案欄位：未支持；本交付不補寫。")
    lines.append("- 步驟層級 goal/intention 與獨立 pain/friction 欄位：confirmed Journey 未分開記錄；保留原始研究描述，不拆成新判斷。")

    lines.extend(["", "## Availability", ""])
    for key, state in availability.items():
        item = _mapping(state)
        label = {
            "journey": "Confirmed Journey",
            "evidence": "Evidence",
            "touchpoint_channel": "觸點／渠道",
            "conflicts": "衝突記錄",
            "emotion": "情緒",
            "nps": "NPS",
            "opportunity": "機會欄位",
        }.get(key, key)
        suffix = "（不阻擋）" if item.get("blocking") is False else ""
        classification = "；分類：尚未確認" if item.get("classification") == "unresolved" else ""
        lines.append(f"- {label}：{_status_text(item.get('status'))}{classification}{suffix}。")
        if item.get("reason"):
            lines.append(f"  - {_inline(item.get('reason'))}")
    for notice in notices:
        if notice.get("detail"):
            lines.append(f"- 提醒：{_inline(notice.get('detail'))}")

    lines.extend(["", "## 附錄：Evidence Source Index", ""])
    if evidence_sources:
        lines.extend(["| Evidence ID | 來源 | 定位 |", "| --- | --- | --- |"])
        for evidence in evidence_sources:
            lines.append(
                f"| {_cell(evidence.get('evidence_id'))} | {_cell(evidence.get('source'))} | {_cell(evidence.get('locator'))} |"
            )
    else:
        lines.append("尚未提供可追溯的 Evidence source index。")

    lines.extend(["", "## 附錄：Source Snapshot", ""])
    lines.extend(["| Artifact | Source | Status | Version / Digest |", "| --- | --- | --- | --- |"])
    for source in source_snapshot:
        version = source.get("version_ref") or source.get("content_digest") or "未提供"
        lines.append(
            f"| {_cell(source.get('artifact_type'))} | {_cell(source.get('source_ref'))} | "
            f"{_cell(_status_text(source.get('status')))} | {_cell(version)} |"
        )
    return "\n".join(line.rstrip() for line in lines).strip() + "\n"
