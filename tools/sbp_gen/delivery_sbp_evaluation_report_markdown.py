"""Deterministic Markdown for ``sbp_evaluation_report`` (new workflow only).

Renders only the normalized view.  Sections with no underlying record show an
honest empty state; nothing is filled in from general service knowledge, no
Improvement Goal or To-be proposal appears, and no verdict on the SBP is given.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .delivery import DeliveryError, DeliveryViewModel
from .delivery_sbp_evaluation_report import SBP_EVALUATION_REPORT, SBP_EVALUATION_REPORT_TITLE

_CHANNEL_LABELS = {"people": "人員", "physical_environment": "實體環境",
                   "digital_tools": "數位工具", "digital_environments": "數位環境"}
_BASIS_LABELS = {"evidence": "證據支持", "inference": "推論", "researcher": "研究者輸入"}
_PROVENANCE_LABELS = {"observed": "參與者證據", "inferred": "服務系統推論", "mixed": "混合：證據與推論分開",
                      "researcher_input": "研究者輸入", "unresolved": "未確認來源"}
_LAYER_LABELS = {"顧客行動": "顧客行動（Customer Action）", "前台行動": "前台（Frontstage）",
                 "後台行動": "後台（Backstage）", "支援流程": "支援流程（Support Process）",
                 "實體證據": "實體／數位元素（Physical / digital elements）"}
_DECISION_LABELS = {"select": "採用", "hold": "保留", "reject": "排除"}
_UNCERTAINTY_LABELS = {"bounded": "有界", "uncertain": "不確定", "disputed": "有爭議"}
_SOURCE_CLASS_LABELS = {"academic": "學術", "government": "政府", "professional_body": "專業機構",
                        "first_party_case": "第一方案例"}
_REVIEW_STATE_LABELS = {
    "current": "研究者審查對應目前這份調研結果。",
    "not_reviewed": "研究者還沒有審查這份調研結果。",
    "stale": "現有的審查紀錄對應的不是目前這份調研結果，本報告不採用它。",
    "invalid": "審查紀錄無法驗證，本報告不採用它。",
}


def _records(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _text(value: Any, fallback: str = "") -> str:
    return value if isinstance(value, str) and value.strip() else fallback


def _cell(value: Any, fallback: str = "未提供") -> str:
    text = value if isinstance(value, str) and value.strip() else (str(value) if value not in (None, "") else fallback)
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _refs(values: Any) -> str:
    return "、".join(f"`{value}`" for value in values) if isinstance(values, list) and values else "無"


def _channel_lines(channels: Any, indent: str = "  ") -> list[str]:
    channels = channels if isinstance(channels, Mapping) else {}
    lines = []
    for key, label in _CHANNEL_LABELS.items():
        for role in _records(channels.get(key)):
            basis = _BASIS_LABELS.get(_text(role.get("basis")), "未確認來源")
            lines.append(f"{indent}- 管道・{label}：{_cell(role.get('text'))}（{basis}）")
    return lines or [f"{indent}- 管道：CJM 沒有記錄。"]


def render_sbp_evaluation_report_markdown(view_model: DeliveryViewModel) -> str:
    if not isinstance(view_model, DeliveryViewModel):
        raise DeliveryError("renderer_invalid_input", "delivery_view", "expected DeliveryViewModel")
    payload = view_model.to_dict()
    if payload.get("export_id") != SBP_EVALUATION_REPORT or payload.get("delivery_type") != SBP_EVALUATION_REPORT:
        raise DeliveryError("renderer_unsupported_delivery_type", "delivery_view", payload.get("export_id"))
    metadata = payload.get("metadata") or {}
    steps = _records((payload.get("scope") or {}).get("steps"))
    layers = (payload.get("service_model") or {}).get("layers") or {}
    questions = _records(payload.get("evaluation_questions"))
    claims = _records(payload.get("findings"))
    evidence = payload.get("research_evidence") or {}
    sources = _records(evidence.get("sources"))
    queries = _records(evidence.get("queries"))
    review = payload.get("researcher_review") or {}
    directives = review.get("directives") or {}
    unresolved = payload.get("unresolved") or {}

    lines = [f"# {SBP_EVALUATION_REPORT_TITLE}", ""]
    lines.append(f"- 個案：`{_cell(metadata.get('case_id'))}`；分析執行：`{_cell(metadata.get('run_ref'))}`")
    lines.append(f"- 評估的服務藍圖版本：`{_cell(metadata.get('sbp_version_ref'))}`")
    lines.append("- 這份報告記錄研究如何評估目前這份服務藍圖能不能實際運作。它不是 To-be 方案，"
                 "不提出新的 Solution，也不替研究者判定服務藍圖是否可接受。")
    lines.append("- 來源分成五種：參與者證據、綜整（CJM 的 Solution 與管道）、服務系統推論、外部研究、研究者決定。"
                 "外部研究是設計參考，不是參與者證據。")

    # 1. Evaluation Scope
    lines.extend(["", "## 1. 評估範圍（Evaluation Scope）", ""])
    selected_steps = [step for step in steps if _records(step.get("selected_solutions"))]
    if not selected_steps:
        lines.append("研究者沒有在顧客旅程選取任何 Solution；評估範圍只有服務藍圖本身。")
    for step in selected_steps:
        lines.extend(["", f"### {step.get('position', '?')} · {_cell(step.get('journey'))}", ""])
        for solution in _records(step.get("selected_solutions")):
            basis = _BASIS_LABELS.get(_text(solution.get("basis")), "未確認來源")
            lines.append(f"- 研究者選取的 Solution：{_cell(solution.get('text'))}（綜整・{basis}；選取：研究者）")
            lines.extend(_channel_lines(solution.get("channels")))

    # 2. Evaluated Service Model
    lines.extend(["", "## 2. 被評估的服務模型（Evaluated Service Model）", ""])
    lines.append("只列服務藍圖裡已經有的內容。")
    for layer, label in _LAYER_LABELS.items():
        nodes = _records(layers.get(layer))
        lines.extend(["", f"### {label}", ""])
        if not nodes:
            lines.append("服務藍圖在這一層沒有節點。")
            continue
        lines.extend(["| 節點 | 來源 | 實現的 Solution | 證據 |", "| --- | --- | --- | --- |"])
        for node in nodes:
            lines.append(
                f"| {_cell(node.get('label'))} | {_cell(_PROVENANCE_LABELS.get(_text(node.get('provenance')), '未確認來源'))} | "
                f"{_cell(node.get('selected_solution_text'), '—')} | {_refs(node.get('evidence_refs'))} |"
            )

    # 3. Evaluation Findings
    lines.extend(["", "## 3. 評估發現（Evaluation Findings）", ""])
    if not questions and not claims:
        lines.append("沒有評估問題，也沒有研究發現。")
    groups = (
        ("選取的 Solution：可行性、管道、資源與後台支援", lambda q: q.get("kind") == "selected_solution"),
        ("後台需求（推定的後台行動）", lambda q: q.get("kind") == "service_assumption" and q.get("layer") == "後台行動"),
        ("支援流程需求（推定的支援流程）", lambda q: q.get("kind") == "service_assumption" and q.get("layer") == "支援流程"),
    )
    if questions:
        lines.append("研究被要求回答的評估問題：")
        for heading, belongs in groups:
            members = [q for q in questions if belongs(q)]
            if members:
                lines.extend(["", f"### {heading}", ""])
                lines.extend(f"- {_cell(q.get('question'))}" for q in members)
    lines.extend(["", "### 研究發現", ""])
    if claims:
        lines.append("調研紀錄沒有把每一條發現標到特定問題或類別（成本、複雜度等），這裡照紀錄原樣列出，不另行分類。")
        for claim in claims:
            uncertainty = claim.get("uncertainty") or {}
            decision = claim.get("researcher_review") or {}
            decision_text = (f"{_DECISION_LABELS.get(_text(decision.get('decision')), '未知')}：{_cell(decision.get('researcher_rationale'))}"
                             if decision else "尚未審查")
            lines.extend([
                "",
                f"- **{_cell(claim.get('id'))}**（外部研究・假設）：{_cell(claim.get('text'))}",
                f"  - 適用情境：{_cell(claim.get('applicability_context'))}",
                f"  - 不確定性：{_UNCERTAINTY_LABELS.get(_text(uncertainty.get('state')), '未提供')}"
                + (f"；{_cell(uncertainty.get('explanation'))}" if uncertainty.get("explanation") else ""),
                f"  - 來源：" + "；".join(
                    f"`{_cell(link.get('source_ref'))}` {'支持' if link.get('relationship') == 'supports' else '限制'}（{_cell(link.get('locator'))}）"
                    for link in _records(claim.get("source_links"))
                ),
                f"  - 研究者決定：{decision_text}",
            ])
    else:
        lines.append("調研沒有留下任何發現。")

    # 4. Research Evidence
    lines.extend(["", "## 4. 研究證據（Research Evidence）", ""])
    lines.append("以下是為這些評估問題實際蒐集到的外部資料。它們是設計參考，不是參與者證據。")
    if sources:
        lines.extend(["", "| 來源 | 類型 | 發布者 | 位置 | 取用日 | 收錄理由 |", "| --- | --- | --- | --- | --- | --- |"])
        for source in sources:
            link = source.get("url") or source.get("doi")
            lines.append(
                f"| `{_cell(source.get('id'))}` {_cell(source.get('title'))} | "
                f"{_cell(_SOURCE_CLASS_LABELS.get(_text(source.get('source_class')), source.get('source_class')))} | "
                f"{_cell(source.get('authors_or_publisher'))} | {_cell(link)}（{_cell(source.get('locator'))}） | "
                f"{_cell(source.get('accessed_at'))} | {_cell(source.get('admission_reason'))} |"
            )
    else:
        lines.append("")
        lines.append("沒有收錄任何外部來源。")

    # 5. Researcher Review
    lines.extend(["", "## 5. 研究者審查（Researcher Review）", ""])
    lines.append(_REVIEW_STATE_LABELS.get(_text(review.get("state")), "審查狀態不明。"))
    lines.append("每一條發現的決定照研究者的紀錄呈現（見第 3 節）；本報告不自行做採用或排除的判斷。")
    mechanisms = _records(directives.get("selected_mechanisms"))
    if mechanisms:
        lines.extend(["", "研究者記下的機制："])
        for item in mechanisms:
            lines.append(f"- {_cell(item.get('name'))}：{_cell(item.get('description'))}（依據 {_refs(item.get('derived_from_claim_refs'))}）")

    # 6. Unresolved Issues
    lines.extend(["", "## 6. 未解決的問題（Unresolved Issues）", ""])
    unresolved_lines = []
    for item in _records(unresolved.get("hold_conditions")):
        unresolved_lines.append(f"- 研究者保留的條件：{_cell(item.get('text'))}（依據 {_refs(item.get('derived_from_claim_refs'))}）")
    for item in _records(unresolved.get("hold_questions")):
        unresolved_lines.append(f"- 研究者保留的問題：{_cell(item.get('text'))}（依據 {_refs(item.get('derived_from_claim_refs'))}）")
    if unresolved.get("held_claim_refs"):
        unresolved_lines.append(f"- 研究者標為保留的發現：{_refs(unresolved.get('held_claim_refs'))}")
    if unresolved.get("unreviewed_claim_refs"):
        unresolved_lines.append(f"- 還沒有審查的發現：{_refs(unresolved.get('unreviewed_claim_refs'))}")
    if unresolved.get("uncertain_claim_refs"):
        unresolved_lines.append(f"- 標示為不確定或有爭議的發現：{_refs(unresolved.get('uncertain_claim_refs'))}")
    for query in _records(unresolved.get("queries_without_admitted_sources")):
        unresolved_lines.append(f"- 查了但沒有收錄來源（證據不足）：{_cell(query.get('purpose'))}")
    for node in _records(unresolved.get("inferred_service_assumptions")):
        unresolved_lines.append(
            f"- 服務藍圖中仍是推論的{_cell(node.get('layer'))}：{_cell(node.get('label'))}（{_PROVENANCE_LABELS.get(_text(node.get('provenance')), '未確認來源')}）"
        )
    lines.extend(unresolved_lines or ["紀錄裡沒有未解決的項目。"])
    return "\n".join(line.rstrip() for line in lines).strip() + "\n"
