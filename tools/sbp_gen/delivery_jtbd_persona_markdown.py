"""Deterministic human-facing Markdown preview for ``jtbd_persona`` views."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .delivery import DeliveryError, DeliveryViewModel
from .delivery_jtbd_persona import JTBD_PERSONA


_STATUS_LABELS = {
    "ready": "可用",
    "unsupported": "未達交付條件",
    "insufficient_evidence": "證據不足",
    "unavailable": "未提供",
    "not_applicable": "不適用",
    "error": "錯誤",
}
_FIELD_LABELS = {
    "context": "情境",
    "motivation_goal": "動機／目標",
    "decision_pattern": "決策模式",
    "behavior_strategy": "行為／策略",
    "motivation": "動機",
    "behavior": "行為",
    "challenge": "阻礙／困難",
    "need": "需求",
    "user_story": "使用者故事",
    "worldview": "世界觀",
    "emotional_tendencies": "情緒傾向",
    "behavioral_patterns": "行為模式",
    "linguistic_style": "語言風格",
    "person_name": "示意稱呼",
    "age": "年齡",
    "profession": "職業",
    "income": "收入",
    "household_structure": "家庭結構",
    "avatar": "頭像",
}
_PROVENANCE_LABELS = {
    "research_evidence": "直接研究證據",
    "evidence_synthesis": "綜合證據模式",
    "evidence_inference": "推論",
    "illustrative_identity": "示意身份（非研究主張）",
    "researcher": "研究者改寫",
}
# persona-nine-canonical-fields (2026-10-01): canonical order first; the older
# keys and the four retired understanding fields are listed only when an older
# Persona carries them, so legacy projects still export what they hold.
_CONTENT_FIELDS = ("context", "motivation_goal", "decision_pattern", "behavior_strategy", "challenge", "need",
                   "motivation", "behavior")
_UNDERSTANDING_FIELDS = ("worldview", "emotional_tendencies", "behavioral_patterns", "linguistic_style")


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DeliveryError("view_model_invalid", field, "expected object")
    return value


def _records(value: Any, field: str) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise DeliveryError("view_model_invalid", field, "expected array of objects")
    return value


def _text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _inline(value: Any) -> str:
    return "`" + str(value).replace("`", "\\`").replace("\n", " ").replace("\r", " ") + "`"


def _refs(values: Any) -> str:
    if not isinstance(values, list) or not values:
        return "未提供 evidence refs"
    return "、".join(_inline(value) for value in values)


def _provenance_line(entry: Mapping[str, Any] | None) -> str:
    if not entry:
        return "原始 Persona 欄位未另列 field-level provenance；本預覽不補判來源類型。"
    ptype = _text(entry.get("type")) or "未標示"
    label = _PROVENANCE_LABELS.get(ptype, ptype)
    refs = entry.get("evidence_refs")
    if ptype == "illustrative_identity":
        return f"來源：{label}。這不是受訪者姓名或人口特徵。"
    if refs:
        return f"來源：{label}；證據：{_refs(refs)}。"
    if ptype == "research_evidence":
        return f"來源：{label}；原始欄位未附 evidence refs。"
    return f"來源：{label}；原始欄位未附 evidence refs。"


def _render_job_layer(lines: list[str], job: Mapping[str, Any], field: str, label: str) -> None:
    layer = job.get(field)
    if not isinstance(layer, Mapping):
        return
    state = _text(layer.get("status"))
    value = _text(layer.get("text"))
    if state == "not_supported":
        lines.append(f"- {label}：資料未支持此層描述。" + (f"{value}" if value else ""))
    elif value:
        lines.append(f"- {label}：{value}")


def _render_job(lines: list[str], job: Mapping[str, Any]) -> None:
    label = _text(job.get("label")) or "未命名 Job"
    job_id = _text(job.get("id"))
    lines.extend([f"### {label}" + (f" {_inline(job_id)}" if job_id else ""), ""])
    for key, heading in (
        ("situation", "When／情境"),
        ("progress", "I want to／想取得的進展"),
        ("outcome", "So I can／想達成的結果"),
    ):
        value = _text(job.get(key))
        if value:
            lines.append(f"- **{heading}：**{value}")
    grounded_in = job.get("grounded_in") or []
    if grounded_in:
        lines.append(f"- 對應類別：{_refs(grounded_in)}")
    evidence_refs = job.get("evidence_refs") or []
    if evidence_refs:
        lines.append(f"- 證據：{_refs(evidence_refs)}")
    strength = _text(job.get("strength"))
    strength_basis = _text(job.get("strength_basis"))
    if strength:
        lines.append(f"- 原始支持強度標記：{strength}" + (f"；依據：{strength_basis}" if strength_basis else ""))
    _render_job_layer(lines, job, "emotional_layer", "情緒層")
    _render_job_layer(lines, job, "social_layer", "社會層")
    forces = job.get("forces")
    if isinstance(forces, Mapping):
        if forces.get("status") == "not_supported":
            reason = _text(forces.get("rationale"))
            lines.append("- 推力／拉力：資料未支持此層描述。" + (f"{reason}" if reason else ""))
        else:
            for key, label in (("push", "推力"), ("pull", "拉力"), ("anxiety", "顧慮"), ("habit", "習慣")):
                value = _text(forces.get(key))
                if value:
                    lines.append(f"- {label}：{value}")
    lines.append("")


def _render_axis(lines: list[str], axis: Mapping[str, Any]) -> None:
    axis_id = _text(axis.get("id")) or "未提供軸 ID"
    label = _text(axis.get("label")) or "未命名軸"
    low = _text(axis.get("pole_low")) or "未提供"
    high = _text(axis.get("pole_high")) or "未提供"
    # persona-axis-representation: a contrast axis has two orientations, not a low and a high end.
    if _is_contrast(axis):
        lines.extend([f"### {label} {_inline(axis_id)}", "", f"- 一種取向：{low}", f"- 另一種取向：{high}"])
    else:
        lines.extend([f"### {label} {_inline(axis_id)}", "", f"- 低端：{low}", f"- 高端：{high}"])
    basis = _text(axis.get("discriminating_basis"))
    if basis:
        lines.append(f"- 軸的區辨依據：{basis}")
    grounded_categories = _records(axis.get("grounded_categories"), f"axis.{axis_id}.grounded_categories")
    if grounded_categories:
        labels = [
            f"{_text(category.get('label')) or '未提供類別名稱'}（{_inline(category.get('id'))}）"
            for category in grounded_categories
        ]
        lines.append(f"- 主軸編碼依據：{'、'.join(labels)}")
    elif axis.get("grounded_in"):
        lines.append(f"- 主軸編碼依據：{_refs(axis.get('grounded_in'))}")
    lines.append("")


def _is_contrast(axis: Mapping[str, Any]) -> bool:
    return axis.get("representation") == "contrast"


def _pole_text(axis: Mapping[str, Any], position: str) -> str:
    if position == "near_low":
        return _text(axis.get("pole_low")) or ("一種取向（未提供描述）" if _is_contrast(axis) else "低端（未提供描述）")
    if position == "near_high":
        return _text(axis.get("pole_high")) or ("另一種取向（未提供描述）" if _is_contrast(axis) else "高端（未提供描述）")
    if position == "mid":
        return "兩種都出現" if _is_contrast(axis) else "兩端之間"
    if position == "no_data":
        return "資料不足"
    return position


_ORDINALS = ("第一", "第二", "第三", "第四", "第五", "第六", "第七", "第八", "第九", "第十")


def _reference_labels(axes: list[Mapping[str, Any]], target_id: str | None,
                      target_members: list[Any]) -> dict[str, str]:
    """rationale-export-readable-refs (2026-10-01): researcher-readable words
    for the internal ids a stored Persona rationale may name, resolved from
    the export's own structured data -- not by stripping id patterns. Stored
    provenance keeps its ids; only this rendering changes."""
    labels: dict[str, str] = {}
    for axis in axes:
        axis_id, label = _text(axis.get("id")), _text(axis.get("label"))
        if axis_id and label:
            labels[axis_id] = f"「{label}」"
    for index, subject_id in enumerate(target_members):
        sid = _text(subject_id)
        if sid:
            labels[sid] = f"{_ORDINALS[index]}位成員" if index < len(_ORDINALS) else "另一位成員"
    if target_id:
        labels[target_id] = "這一格"
    return labels


def _is_cjk(char: str) -> bool:
    return ord(char) >= 0x2E80


def _with_readable_refs(text: str, labels: Mapping[str, str]) -> str:
    """Replace each known id with its label; an id is matched whole, so
    AXIS-1 never matches inside AXIS-10."""
    out = text
    for ref in sorted(labels, key=len, reverse=True):
        pieces, start = [], 0
        while True:
            at = out.find(ref, start)
            if at < 0:
                pieces.append(out[start:])
                break
            end = at + len(ref)
            whole = (at == 0 or not out[at - 1].isalnum()) and (end == len(out) or not out[end].isalnum())
            if not whole:
                pieces.append(out[start:end])
                start = end
                continue
            before = out[start:at]
            # An id written with spaces around it inside Chinese prose leaves
            # a stray space once it becomes Chinese words.
            if before.endswith(" ") and len(before) >= 2 and _is_cjk(before[-2]):
                before = before[:-1]
            pieces.append(before + labels[ref])
            if end + 1 < len(out) and out[end] == " " and _is_cjk(out[end + 1]):
                end += 1
            start = end
        out = "".join(pieces)
    return out


def _render_persona(lines: list[str], persona: Mapping[str, Any],
                    reference_labels: Mapping[str, str] | None = None) -> None:
    persona_id = _text(persona.get("id")) or "未提供 Persona ID"
    label = _text(persona.get("label")) or "未命名 Persona"
    lines.extend([f"### {label} {_inline(persona_id)}", ""])
    if persona.get("quadrant_id"):
        lines.append(f"- 對應象限：{_inline(persona['quadrant_id'])}")
    if persona.get("member_subject_ids"):
        lines.append(f"- 研究者確認的成員：{_refs(persona.get('member_subject_ids'))}")

    entries = {
        str(entry.get("field")): entry
        for entry in _records(persona.get("field_provenance"), f"persona.{persona_id}.field_provenance")
        if _text(entry.get("field"))
    }
    for field in ("person_name", "age", "profession", "income", "household_structure"):
        value = _text(persona.get(field))
        if not value:
            continue
        label_text = _FIELD_LABELS.get(field, field)
        if field == "person_name":
            lines.append(f"- {label_text}：{value}（非研究主張）")
        else:
            lines.append(f"- {label_text}：{value}")
        lines.append(f"  - {_provenance_line(entries.get(field))}")

    quote = persona.get("representative_quote")
    if isinstance(quote, Mapping) and _text(quote.get("text")):
        ref = _text(quote.get("evidence_ref"))
        lines.extend(["", "#### 代表性原文", "", f"> {quote['text']}" + (f"  \n> 證據：{_inline(ref)}" if ref else "")])

    description = _text(persona.get("description"))
    if description:
        lines.extend(["", "#### 描述", "", description,
                      "", "原始 Persona 欄位未另列 field-level provenance；本預覽不補判來源類型。"])
    rationale = _text(persona.get("rationale"))
    if rationale:
        rationale = _with_readable_refs(rationale, reference_labels or {})
        lines.extend(["", "#### 分群理由", "", rationale,
                      "", "原始 Persona 欄位未另列 field-level provenance；本預覽不補判來源類型。"])

    def _field_block(field: str, heading: str) -> None:
        value = _text(persona.get(field))
        if not value:
            return
        lines.extend(["", f"{heading} {_FIELD_LABELS[field]}", "", value, "", _provenance_line(entries.get(field))])
        reasoning = _text(entries.get(field, {}).get("reasoning_summary")) if entries.get(field) else None
        if reasoning:
            lines.append(f"- 推導橋接：{reasoning}")

    _field_block("user_story", "####")
    for field in _CONTENT_FIELDS:
        _field_block(field, "####")
    if any(_text(persona.get(field)) for field in _UNDERSTANDING_FIELDS):
        lines.extend(["", "#### 人物理解"])
        for field in _UNDERSTANDING_FIELDS:
            _field_block(field, "#####")
    lines.append("")


def _render_evidence_index(lines: list[str], evidence_sources: list[Mapping[str, Any]]) -> None:
    lines.extend(["", "### 證據來源索引", ""])
    if not evidence_sources:
        lines.append("本 view model 未提供可追溯的 evidence source 記錄。")
        return
    lines.extend(["| Evidence ID | 來源 | 定位 |", "| --- | --- | --- |"])
    for record in evidence_sources:
        evidence_id = str(record.get("evidence_id") or "未提供")
        source = str(record.get("source") or "未提供")
        locator = str(record.get("locator") or "未提供")
        cells = [value.replace("|", "\\|").replace("\n", " ") for value in (evidence_id, source, locator)]
        lines.append(f"| {_inline(cells[0])} | {cells[1]} | {cells[2]} |")


def _render_source_snapshot(lines: list[str], sources: list[Mapping[str, Any]]) -> None:
    lines.extend(["", "### 來源 artifact 摘要", ""])
    if not sources:
        lines.append("本 view model 未提供來源快照。")
        return
    for source in sources:
        artifact = _text(source.get("artifact_type")) or "未命名來源"
        ref = _text(source.get("source_ref")) or "未提供"
        status = _text(source.get("status")) or "unavailable"
        details = [_STATUS_LABELS.get(status, status)]
        version = _text(source.get("version_ref"))
        if version:
            details.append(f"版本 {_inline(version)}")
        gate_state = _text(source.get("gate_state"))
        origin = _text(source.get("confirmation_origin"))
        if gate_state:
            details.append(f"human gate：{gate_state}" + (f"（{origin}）" if origin else ""))
        lines.append(f"- {artifact}：{_inline(ref)}；{'；'.join(details)}")


def render_jtbd_persona_markdown(view_model: DeliveryViewModel) -> str:
    """Render an immutable ``jtbd_persona`` view without source reads/writes."""
    if not isinstance(view_model, DeliveryViewModel):
        raise DeliveryError("renderer_invalid_input", "delivery_view", "expected DeliveryViewModel")
    payload = view_model.to_dict()
    if payload.get("export_id") != JTBD_PERSONA or payload.get("delivery_type") != JTBD_PERSONA:
        raise DeliveryError("renderer_unsupported_delivery_type", "delivery_view", payload.get("export_id"))

    metadata = _mapping(payload.get("metadata"), "metadata")
    availability = _mapping(payload.get("availability"), "availability")
    jtbd = _mapping(payload.get("jtbd"), "jtbd")
    segmentation = _mapping(payload.get("segmentation"), "segmentation")
    provenance = _mapping(payload.get("provenance"), "provenance")
    persona_content = _mapping(payload.get("persona_content"), "persona_content")
    jobs = _records(jtbd.get("jobs"), "jtbd.jobs")
    axes = _records(segmentation.get("confirmed_axes"), "segmentation.confirmed_axes")
    quadrants = _records(segmentation.get("quadrants"), "segmentation.quadrants")
    personas = _records(persona_content.get("personas"), "persona_content.personas")
    notices = _records(payload.get("notices"), "notices")
    evidence_sources = _records(provenance.get("evidence_sources"), "provenance.evidence_sources")
    source_snapshot = _records(payload.get("source_snapshot"), "source_snapshot")

    lines = ["# JTBD + Persona", "", "## 概覽", ""]
    if payload.get("status") != "ready":
        status = _text(payload.get("status")) or "unsupported"
        lines.append(
            f"> 交付狀態：{_STATUS_LABELS.get(status, status)}。必要資訊未齊全；本預覽保留缺口，不代填研究內容。"
        )
        lines.append("")
    for label, key in (("個案", "case_id"), ("專案", "project_id"), ("分析執行", "run_ref")):
        value = _text(metadata.get(key))
        if value:
            lines.append(f"- {label}：{_inline(value)}")
    jtbd_provenance = _mapping(provenance.get("jtbd"), "provenance.jtbd")
    lines.append(f"- JTBD 記錄狀態：{_inline(jtbd_provenance.get('run_state') or '未提供')}")
    lines.append(f"- JTBD 研究者確認狀態：{_inline(jtbd_provenance.get('confirmation_state') or '未記錄')}")
    lines.append(f"- 本 run 收錄 {len(jobs)} 個 JTBD；Persona Matrix 包含 {segmentation.get('matrix_subject_count', 0)} 位受訪者記錄。")

    lines.extend(["", "## JTBD", ""])
    if jobs:
        for job in jobs:
            _render_job(lines, job)
    else:
        lines.append("目前沒有可用的 JTBD 記錄；本預覽不補寫工作或動機。")

    lines.extend(["", "## Persona Segmentation", ""])
    lines.append(
        "以下兩條軸是研究者確認後用於象限的軸。Persona Matrix 保留更完整的候選軸；"
        "JTBD 不作為 Persona 證據篩選條件。象限成員取自研究者確認的選擇檔，不以系統建議取代。"
    )
    for axis in axes:
        _render_axis(lines, axis)
    if quadrants:
        lines.extend(["### 確認成員分布", ""])
        for quadrant in quadrants:
            qid = _text(quadrant.get("id")) or "未提供象限 ID"
            axis_1_position = _text(quadrant.get("axis_1_pole")) or "未提供"
            axis_2_position = _text(quadrant.get("axis_2_pole")) or "未提供"
            axis_1 = axes[0] if len(axes) > 0 else {}
            axis_2 = axes[1] if len(axes) > 1 else {}
            axis_1_label = _text(axis_1.get("label")) or "第一條軸"
            axis_2_label = _text(axis_2.get("label")) or "第二條軸"
            axis_1_value = _pole_text(axis_1, axis_1_position) if axis_1 else axis_1_position
            axis_2_value = _pole_text(axis_2, axis_2_position) if axis_2 else axis_2_position
            lines.append(
                f"- **{_inline(qid)}**：{axis_1_label}為「{axis_1_value}」；"
                f"{axis_2_label}為「{axis_2_value}」；"
                f"研究者確認 {quadrant.get('member_count', 0)} 位成員；"
                f"涉及 {quadrant.get('evidence_count', 0)} 則證據。"
            )
            refs = quadrant.get("evidence_refs") or []
            if refs:
                lines.append(f"  - 成員：{_refs(quadrant.get('member_subject_ids'))}；證據：{_refs(refs)}")
        lines.append(
            f"矩陣共有 {segmentation.get('matrix_subject_count', 0)} 位受訪者；其中 "
            f"{segmentation.get('confirmed_membership_subject_count', 0)} 位有研究者確認的象限指派。"
        )
        unassigned = segmentation.get("subjects_without_confirmed_quadrant") or []
        if unassigned:
            lines.append(
                f"另有 {len(unassigned)} 位未列入確認成員分布；選擇檔未記錄他們的象限，不把系統建議當成確認決定。"
            )

    lines.extend(["", "## Researcher-Confirmed Target", ""])
    target_id = _text(segmentation.get("target_quadrant_id")) or "未記錄"
    target_members = segmentation.get("target_member_subject_ids") or []
    target_confirmation = _mapping(segmentation.get("target_confirmation"), "segmentation.target_confirmation")
    lines.append(f"研究者確認的主要目標為 {_inline(target_id)}，成員：{_refs(target_members)}。")
    lines.append(
        f"確認狀態：{_inline(target_confirmation.get('state') or '未確認')}；"
        f"記錄來源：{_inline(target_confirmation.get('source_ref') or '未提供')}。"
    )
    rationale = _text(segmentation.get("target_rationale"))
    if rationale:
        lines.append(f"選擇理由：{rationale}")
    else:
        lines.append("選擇理由：研究者未記錄選擇理由。")

    lines.extend(["", "## Persona", ""])
    if personas:
        labels = _reference_labels(axes, _text(segmentation.get("target_quadrant_id")), list(target_members))
        for persona in personas:
            _render_persona(lines, persona, labels)
    else:
        outcomes = _records(persona_content.get("quadrant_outcomes"), "persona_content.quadrant_outcomes")
        target_outcome = next((x for x in outcomes if x.get("quadrant_id") == target_id), None)
        if target_outcome:
            outcome_status = _text(target_outcome.get("status")) or "未提供"
            if outcome_status == "insufficient_evidence":
                lines.append("目標象限的 Persona 標記為證據不足；不建立人物描述。")
            elif outcome_status == "researcher_deferred":
                lines.append("研究者已將目標象限的 Persona 延後處理；不建立人物描述。")
            note = _text(target_outcome.get("note"))
            if note:
                lines.append(f"原始說明：{note}")
        else:
            lines.append("目前沒有 Persona 記錄，也沒有目標象限結果；本預覽不補寫人物描述。")

    lines.extend(["", "## Evidence / Provenance Notes", ""])
    lines.append(
        "Persona 欄位沿用來源檔的分類：直接研究證據、綜合證據模式、推論、示意身份。"
        "示意身份不代表受訪者；推論只保留原始推導橋接與 evidence refs。"
    )
    field_ref_notice = next((n for n in notices if n.get("code") == "persona_direct_evidence_refs_not_recorded"), None)
    if field_ref_notice:
        fields = [
            _FIELD_LABELS.get(item, item)
            for item in str(field_ref_notice.get("detail") or "").split(",") if item
        ]
        lines.append(
            "來源檔將下列欄位標為直接研究證據，但沒有附 evidence refs："
            + ("、".join(fields) if fields else "未命名欄位")
            + "。本預覽保留標記並揭露缺少的引用，不自行配對證據。"
        )
    _render_evidence_index(lines, evidence_sources)

    lines.extend(["", "## Availability / Limitations", ""])
    for key, state in availability.items():
        state_obj = _mapping(state, f"availability.{key}")
        status = _text(state_obj.get("status")) or "unavailable"
        reason = _text(state_obj.get("reason"))
        label = {
            "jtbd": "JTBD",
            "segmentation_axes": "確認軸",
            "target_selection": "目標選擇",
            "persona_matrix": "Persona Matrix",
            "persona": "Persona",
            "target_rationale": "目標選擇理由",
            "illustrative_identity": "示意身份欄位",
        }.get(key, key)
        lines.append(f"- {label}：{_STATUS_LABELS.get(status, status)}" + (f"（{_inline(reason)}）" if reason else ""))
    for notice in notices:
        code = _text(notice.get("code")) or "未命名提醒"
        detail = _text(notice.get("detail"))
        if code == "target_rationale_not_recorded":
            continue  # already presented beside the selected target
        if code == "persona_direct_evidence_refs_not_recorded":
            continue  # already placed with provenance notes
        if detail:
            lines.append(f"- {code}：{detail}")

    lines.extend(["", "## 附錄：來源快照", ""])
    _render_source_snapshot(lines, source_snapshot)
    return "\n".join(line.rstrip() for line in lines).strip() + "\n"
