"""Deterministic, read-only Markdown preview for coding_synthesis views."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from .delivery import CODING_SYNTHESIS, DeliveryError, DeliveryViewModel


OPEN_CODE_PREVIEW_LIMIT = 8
AXIAL_CATEGORY_PREVIEW_LIMIT = 8
AXIAL_DETAIL_VALUE_LIMIT = 4

_STATUS_LABELS = {
    "ready": "可用",
    "unsupported": "不支援",
    "insufficient_evidence": "證據不足",
    "unavailable": "未提供",
    "not_applicable": "不適用",
    "error": "錯誤",
}
_OPTIONAL_CONTEXT_LABELS = {
    "narrative_job_candidates": "顧客任務候選",
    "narrative_job_selection": "顧客任務選擇",
    "jtbd": "JTBD",
    "primary_hmw_selection": "主要 HMW 選擇",
}


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
    rendered = str(value).strip()
    return rendered or None


def _inline(value: Any) -> str:
    return "`" + str(value).replace("`", "\\`").replace("\n", " ").replace("\r", " ") + "`"


def _quote(value: Any) -> list[str]:
    text = str(value)
    return [f"> {line}" for line in text.splitlines()] or ["> "]


def _refs(values: Any) -> str:
    if not isinstance(values, list) or not values:
        return "未提供"
    return "、".join(_inline(value) for value in values)



_PARADIGM_BASIS_LABEL = {"evidence": "證據", "synthesis": "綜合", "inference": "推論"}
_PARADIGM_KIND_LABEL = {"situational": "情境條件", "user": "使用者條件"}


def _paradigm_item_text(value) -> str:
    """purpose-aware-research-calibration: a paradigm item with its basis (and,
    for an inference, its reasoning); older string items print as they are."""
    if not isinstance(value, dict):
        return str(value)
    marks = "・".join(x for x in (_PARADIGM_KIND_LABEL.get(value.get("kind"), ""),
                                  _PARADIGM_BASIS_LABEL.get(value.get("basis"), "")) if x)
    text = f"{value.get('text') or ''}（{marks}）" if marks else str(value.get("text") or "")
    return f"{text}；推論依據：{value['reasoning']}" if value.get("basis") == "inference" and value.get("reasoning") else text

def _limited_note(shown: int, total: int, subject: str) -> str | None:
    if total <= shown:
        return None
    return (
        f"預覽列出 {shown} / {total} 筆{subject}；其餘 {total - shown} 筆未展開。"
        "完整記錄仍保留在正規化 view model 與來源資料中；本預覽不是完整機器匯出。"
    )


def _spread_positions(length: int, count: int) -> list[int]:
    if count <= 0 or length <= 0:
        return []
    if count == 1:
        return [(length - 1) // 2]
    if count >= length:
        return list(range(length))
    return [index * (length - 1) // (count - 1) for index in range(count)]


def _source_balanced_open_code_preview(
    concepts: list[Mapping[str, Any]], limit: int,
) -> tuple[list[Mapping[str, Any]], int, int, bool]:
    """Select a deterministic, bounded sample across existing source groups.

    Source groups are ordered by their recorded source labels. If there are
    more groups than slots, evenly-spaced groups are selected; otherwise slots
    are allocated round-robin across groups. Records within each selected
    group are also evenly spaced. The returned records remain in canonical
    input order. This is a source-spread reading sample, not a statistical
    representativeness claim or importance ranking.
    """
    groups: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    has_unlabelled_source = False
    for index, concept in enumerate(concepts):
        source = _text(concept.get("source"))
        if source is None:
            source = ""
            has_unlabelled_source = True
        groups.setdefault(source, []).append((index, concept))

    if not concepts or limit <= 0:
        return [], 0, len(groups), has_unlabelled_source

    ordered_sources = sorted(groups, key=lambda source: (source == "", source))
    sample_size = min(limit, len(concepts))
    if len(ordered_sources) > sample_size:
        selected_sources = [
            ordered_sources[index]
            for index in _spread_positions(len(ordered_sources), sample_size)
        ]
        quotas = {source: 1 for source in selected_sources}
    else:
        selected_sources = list(ordered_sources)
        quotas = {source: 1 for source in selected_sources}
        remaining = sample_size - len(selected_sources)
        while remaining:
            allocated = False
            for source in selected_sources:
                if quotas[source] >= len(groups[source]):
                    continue
                quotas[source] += 1
                remaining -= 1
                allocated = True
                if remaining == 0:
                    break
            if not allocated:
                break

    chosen: list[tuple[int, Mapping[str, Any]]] = []
    for source in selected_sources:
        records = groups[source]
        chosen.extend(records[index] for index in _spread_positions(len(records), quotas[source]))
    chosen.sort(key=lambda pair: pair[0])
    return [record for _, record in chosen], len(selected_sources), len(groups), has_unlabelled_source


def _open_code_sample_note(
    shown: int,
    total: int,
    covered_groups: int,
    total_groups: int,
    has_unlabelled_source: bool,
) -> str | None:
    if total <= shown:
        return None
    coverage = f"涵蓋 {covered_groups} / {total_groups} 個來源群組。" if total_groups else ""
    unlabelled = "未標示來源的編碼合併為一組。" if has_unlabelled_source else ""
    return (
        f"本節列出 {shown} / {total} 筆開放編碼，{coverage}"
        "依 view model 的 source 分組分散抽樣，不按重要性排序，也不代表統計代表性樣本。"
        f"{unlabelled}其餘 {total - shown} 筆未展開；完整記錄仍保留在 view model 與來源資料中。"
    )


def _render_source_manifest(lines: list[str], source_snapshot: list[Mapping[str, Any]]) -> None:
    lines.extend(["", "### 來源快照與版本摘要", ""])
    if not source_snapshot:
        lines.append("view model 未提供來源快照。")
        return
    for source in source_snapshot:
        artifact = _text(source.get("artifact_type")) or "未命名來源"
        source_ref = _text(source.get("source_ref")) or "未提供"
        source_state = _text(source.get("status")) or "unavailable"
        details = [f"狀態：{_STATUS_LABELS.get(source_state, source_state)}"]
        version_ref = _text(source.get("version_ref"))
        digest = _text(source.get("content_digest"))
        if version_ref:
            details.append(f"版本：{_inline(version_ref)}")
        if digest:
            details.append(f"digest：{_inline(digest)}")
        resolution = _text(source.get("resolution_method"))
        if resolution:
            details.append(f"定位方式：{_inline(resolution)}")
        lines.append(f"- {artifact}：{_inline(source_ref)}（{'；'.join(details)}）")


def _render_evidence_locator_index(
    lines: list[str], evidence_sources: list[Mapping[str, Any]],
) -> None:
    lines.extend(["", "### Evidence 定位索引", ""])
    if not evidence_sources:
        lines.append("view model 未提供證據來源定位記錄。")
        return
    for item in evidence_sources:
        evidence_id = _text(item.get("evidence_id")) or "未提供 ID"
        details = []
        for key, label in (
            ("role", "角色"),
            ("source", "來源"),
            ("locator", "定位"),
            ("tier", "層級"),
            ("content_type", "內容類型"),
        ):
            value = _text(item.get(key))
            if value:
                details.append(f"{label}：{value}")
        lines.append(f"- {_inline(evidence_id)}：{'；'.join(details) if details else 'view model 未提供來源定位欄位。'}")


def _render_shared_evidence_links(
    lines: list[str], shared_links: list[Mapping[str, Any]],
) -> None:
    lines.extend(["", "### Open Coding–Axial shared-evidence links", ""])
    if not shared_links:
        lines.append("view model 未提供 shared-evidence 連結。")
        return
    lines.append("相同 evidence refs 只表示記錄有共享證據，不代表新增的因果或分析關係。")
    for link in shared_links:
        relationship = _text(link.get("relationship")) or "未提供關係標籤"
        lines.append(
            f"- {_inline(link.get('open_code_ref', ''))} → "
            f"{_inline(link.get('axial_category_ref', ''))}（{_inline(relationship)}）；"
            f"證據：{_refs(link.get('evidence_refs'))}"
        )


def _render_evidence_mapping(
    lines: list[str], evidence_mapping: list[Mapping[str, Any]], availability: Mapping[str, Any],
) -> None:
    lines.extend(["", "### 完整 Evidence Mapping", ""])
    if not evidence_mapping:
        lines.append(_empty_message("evidence", "證據映射", availability))
        return
    for item in evidence_mapping:
        evidence_id = _text(item.get("evidence_id")) or "未提供 ID"
        lines.extend(["", f"#### {_inline(evidence_id)}", ""])
        for key, label in (
            ("open_code_refs", "開放編碼"),
            ("axial_category_refs", "軸心類別"),
            ("selective_core_category_refs", "核心類別"),
            ("selective_insight_refs", "選擇編碼洞察引用"),
        ):
            values = item.get(key)
            if isinstance(values, list) and values:
                lines.append(f"- {label}：{_refs(values)}")
        if not any(
            isinstance(item.get(key), list) and item.get(key)
            for key in (
                "open_code_refs", "axial_category_refs", "selective_core_category_refs", "selective_insight_refs"
            )
        ):
            lines.append("- view model 未提供分析連結。")


def _format_markdown(lines: list[str]) -> str:
    formatted: list[str] = []
    for line in lines:
        previous = formatted[-1] if formatted else ""
        is_heading = line.startswith("#")
        is_quote = line.startswith("> ")
        is_ordered_item = line[:1].isdigit() and ". " in line[:5]
        is_list_item = line.lstrip().startswith("- ") or is_ordered_item
        previous_is_list = previous.lstrip().startswith("- ") or (
            previous[:1].isdigit() and ". " in previous[:5]
        )
        if line and previous and (
            is_heading
            or (is_quote and not previous.startswith("> "))
            or (is_list_item and not previous_is_list)
            or (not is_list_item and previous_is_list and not is_heading)
        ):
            formatted.append("")
        if line == "" and formatted and formatted[-1] == "":
            continue
        formatted.append(line)
    return "\n".join(formatted).strip() + "\n"


def _empty_message(field: str, label: str, availability: Mapping[str, Any]) -> str:
    state = _mapping(availability.get(field), f"availability.{field}")
    status = state.get("status")
    reason = _text(state.get("reason"))
    if status == "insufficient_evidence":
        return f"{label}標示為證據不足；本預覽不補寫內容或研究結論。"
    if status in {"unavailable", "unsupported", "not_applicable", "error"}:
        detail = f"（{_STATUS_LABELS.get(status, status)}；{_inline(reason)}）" if reason else f"（{_STATUS_LABELS.get(status, status)}）"
        return f"{label}目前{detail}；本預覽不補寫內容。"
    return f"view model 未提供{label}記錄；本預覽不補寫內容。"


def render_coding_synthesis_markdown(view_model: DeliveryViewModel) -> str:
    """Render a human-facing preview from one normalized coding_synthesis view.

    This function accepts no paths and performs no source resolution, model
    calls, or writes. It presents canonical text as supplied and keeps every
    displayed record connected to its existing IDs and provenance references.
    """
    if not isinstance(view_model, DeliveryViewModel):
        raise DeliveryError("renderer_invalid_input", "delivery_view", "expected DeliveryViewModel")
    payload = view_model.to_dict()
    if payload.get("export_id") != CODING_SYNTHESIS or payload.get("delivery_type") != CODING_SYNTHESIS:
        raise DeliveryError("renderer_unsupported_delivery_type", "delivery_view", payload.get("export_id"))

    metadata = _mapping(payload.get("metadata"), "metadata")
    availability = _mapping(payload.get("availability"), "availability")
    evidence = _records(payload.get("evidence"), "evidence")
    open_coding = _mapping(payload.get("open_coding"), "open_coding")
    axial_coding = _mapping(payload.get("axial_coding"), "axial_coding")
    selective_coding = _mapping(payload.get("selective_coding"), "selective_coding")
    provenance = _mapping(payload.get("provenance"), "provenance")
    evidence_mapping = _records(payload.get("evidence_mapping"), "evidence_mapping")
    source_snapshot = _records(payload.get("source_snapshot"), "source_snapshot")
    evidence_sources = _records(provenance.get("evidence_sources"), "provenance.evidence_sources")
    shared_links = _records(
        provenance.get("open_code_to_axial_category"), "provenance.open_code_to_axial_category"
    )

    concepts = _records(open_coding.get("concepts"), "open_coding.concepts")
    categories = _records(axial_coding.get("categories"), "axial_coding.categories")
    category_by_id = {
        record.get("id"): record for record in categories if isinstance(record.get("id"), str)
    }
    concepts_shown, sampled_source_groups, source_group_count, has_unlabelled_source = (
        _source_balanced_open_code_preview(concepts, OPEN_CODE_PREVIEW_LIMIT)
    )
    categories_shown = categories[:AXIAL_CATEGORY_PREVIEW_LIMIT]
    source_counts = Counter(
        _text(item.get("source")) for item in evidence_sources if _text(item.get("source"))
    )

    lines = ["# Coding & Synthesis Report", "", "## 分析概覽", ""]
    for label, key in (("專案", "project_id"), ("個案", "case_id"), ("分析執行", "run_ref")):
        value = _text(metadata.get(key))
        if value:
            lines.append(f"- {label}：{_inline(value)}")
    status = _text(payload.get("status")) or "unavailable"
    status_label = _STATUS_LABELS.get(status, status)
    lines.append(f"- view model 狀態：{status_label}（{_inline(status)}）")
    lines.append(
        f"- 記錄數：{len(evidence)} 筆證據、{len(concepts)} 個開放編碼、"
        f"{len(categories)} 個軸心類別。"
    )
    coding_states = _mapping(metadata.get("coding_states"), "metadata.coding_states")
    for key in ("open_coding", "axial_coding", "selective_coding"):
        state = _text(coding_states.get(key))
        if state:
            lines.append(f"- {key} 狀態：{_inline(state)}")

    lines.extend(["", "## 證據概況", ""])
    lines.append(f"本次分析包含 {len(evidence)} 筆證據記錄。預覽不重複呈現逐字稿全文。")
    if source_counts:
        lines.append(f"view model 記錄了 {len(source_counts)} 個來源；證據 ID 與定位索引列於文末附錄。")
    elif evidence:
        lines.append("view model 未提供來源名稱；可用的證據 refs 仍保留於編碼與附錄。")

    selected_job = _mapping(selective_coding.get("selected_job"), "selective_coding.selected_job")
    if selective_coding.get("source") == "narrative_job_selection":
        # Current workflow: the synthesis decision is the researcher's selected
        # JTBD, not a selective-coding core category (2026-10-01).
        lines.extend(["", "## 研究者選定的顧客任務", ""])
        clause = lambda key: _text(selected_job.get(key)) or "（未記錄）"  # noqa: E731
        lines.append(f"當{clause('situation')}時，我想要{clause('progress')}，因為{clause('motivation')}，"
                     f"所以我可以{clause('outcome')}。")
        grounded = [ref for ref in selective_coding.get("grounded_in") or [] if isinstance(ref, str)]
        if grounded:
            lines.append("- 依據的主軸類別：" + "、".join(
                f"{_inline(ref)}（{_text((category_by_id.get(ref) or {}).get('label')) or '未命名'}）" for ref in grounded))
        rationale = _text(selective_coding.get("rationale"))
        if rationale:
            lines.append("選擇理由（來源欄位）：")
            lines.extend(_quote(rationale))
        narrative = _text(selective_coding.get("narrative"))
        if narrative:
            lines.extend(["", "### 核心現象", ""])
            lines.extend(_quote(narrative))
        selective_coding = {}
    else:
        lines.extend(["", "## 選擇編碼／綜合", ""])
    core_id = _text(selective_coding.get("core_category_id"))
    core_decision = _mapping(provenance.get("selective_core_decision"), "provenance.selective_core_decision")
    if core_id:
        core_category = category_by_id.get(core_id, {})
        core_label = _text(core_category.get("label")) if isinstance(core_category, Mapping) else None
        title = f"研究者選定的核心類別：{_inline(core_id)}"
        if core_label:
            title += f"（{core_label}）"
        lines.append(title)
        if isinstance(core_category, Mapping):
            core_refs = core_category.get("evidence_refs")
            if isinstance(core_refs, list) and core_refs:
                lines.append(f"- 核心類別既有證據：{_refs(core_refs)}")
        decision_source = _text(core_decision.get("source_ref"))
        decision_state = _text(core_decision.get("decision_state"))
        if decision_source or decision_state:
            details = []
            if decision_source:
                details.append(f"來源：{_inline(decision_source)}")
            if decision_state:
                details.append(f"記錄狀態：{_inline(decision_state)}")
            lines.append("- 決策紀錄：" + "；".join(details))
        rationale = _text(selective_coding.get("rationale"))
        if rationale:
            lines.append("選擇理由（來源欄位）：")
            lines.extend(_quote(rationale))
    elif not selected_job:
        lines.append(_empty_message("selective_coding", "選擇編碼", availability))

    secondary_themes = selective_coding.get("secondary_themes")
    if isinstance(secondary_themes, list) and secondary_themes:
        lines.extend(["", "### 既有次要主題", ""])
        lines.extend(f"- {theme}" for theme in secondary_themes)
    narrative = _text(selective_coding.get("narrative"))
    if selected_job:
        pass  # the core phenomenon is already shown with the selected JTBD
    elif narrative:
        author = _text(selective_coding.get("narrative_composed_by"))
        lines.extend(["", "### 既有敘事", ""])
        if author:
            author_label = "研究者" if author == "researcher" else "assistant" if author == "assistant" else author
            lines.append(f"來源標記：{author_label}。以下文字照 view model 呈現，未改寫。")
        lines.extend(_quote(narrative))
    else:
        run_ref = _text(metadata.get("run_ref"))
        run_arg = run_ref if run_ref and run_ref.startswith("runs/") else f"runs/{run_ref}" if run_ref else "<run>"
        lines.extend([
            "",
            "尚未產生 Selective Coding narrative。",
            "可使用以下指令產生；本預覽只顯示指令，不會執行：",
            _inline(f"sbp-gen selective-coding-narrative --run {run_arg}"),
        ])
    insights = _records(selective_coding.get("key_insights"), "selective_coding.key_insights")
    if insights:
        lines.extend(["", "### 既有關鍵洞察", ""])
        for index, insight in enumerate(insights, start=1):
            text = _text(insight.get("text"))
            if text:
                lines.extend([f"{index}. {text}", f"   - 證據：{_refs(insight.get('evidence_refs'))}"])

    lines.extend(["", "## 開放編碼", ""])
    if not concepts:
        lines.append(_empty_message("open_coding", "開放編碼", availability))
    else:
        note = _open_code_sample_note(
            len(concepts_shown), len(concepts), sampled_source_groups, source_group_count,
            has_unlabelled_source,
        )
        if note:
            lines.append(note)
        for concept in concepts_shown:
            concept_id = _text(concept.get("id")) or "未提供 ID"
            name = _text(concept.get("name")) or "未提供名稱"
            lines.extend(["", f"### {_inline(concept_id)}｜{name}", ""])
            description = _text(concept.get("description"))
            if description:
                lines.extend(_quote(description))
            source = _text(concept.get("source"))
            if source:
                lines.append(f"- 來源：{source}")
            lines.append(f"- 證據：{_refs(concept.get('evidence_refs'))}")

    lines.extend(["", "## 軸心編碼", ""])
    if not categories:
        lines.append(_empty_message("axial_coding", "軸心編碼", availability))
    else:
        note = _limited_note(len(categories_shown), len(categories), "軸心類別")
        if note:
            lines.append(note)
        for category in categories_shown:
            category_id = _text(category.get("id")) or "未提供 ID"
            label = _text(category.get("label")) or "未提供標籤"
            lines.extend(["", f"### {_inline(category_id)}｜{label}", ""])
            for field, field_label in (
                ("category_name", "研究主題"),
                ("discriminating_basis", "區辨依據"),
            ):
                value = _text(category.get(field))
                if value:
                    lines.append(f"- {field_label}：{value}")
            for field, field_label in (
                ("conditions", "條件"),
                ("actions", "行動／互動"),
                ("consequences", "結果"),
            ):
                values = category.get(field)
                if isinstance(values, list) and values:
                    lines.append(f"- {field_label}：")
                    lines.extend(f"  - {_paradigm_item_text(value)}" for value in values[:AXIAL_DETAIL_VALUE_LIMIT])
                    note = _limited_note(
                        min(len(values), AXIAL_DETAIL_VALUE_LIMIT), len(values), f"{field_label}項目"
                    )
                    if note:
                        lines.append(f"  - {note}")
            lines.append(f"- 證據：{_refs(category.get('evidence_refs'))}")

    lines.extend(["", "## 可用性與限制", ""])
    unavailable = []
    for artifact, state_value in sorted(availability.items()):
        state = _mapping(state_value, f"availability.{artifact}")
        state_status = _text(state.get("status"))
        if state_status and state_status != "ready":
            unavailable.append((artifact, state_status, _text(state.get("reason"))))
    if unavailable:
        for artifact, state_status, reason in unavailable:
            status_text = f"{_STATUS_LABELS.get(state_status, state_status)}（{_inline(state_status)}）"
            if reason:
                status_text += f"；原因：{_inline(reason)}"
            lines.append(f"- {artifact}：{status_text}")
    elif status != "ready":
        lines.append(f"- 整體狀態：{status_label}（{_inline(status)}）。")
    if not evidence:
        lines.append("- 證據記錄為 0；未產生研究發現或補寫推論。")
    notices = _records(payload.get("notices"), "notices")
    known_notice_pairs = {
        (artifact, reason)
        for artifact, _state, reason in unavailable
        if reason is not None
    }
    for notice in notices:
        code = _text(notice.get("code")) or "未提供代碼"
        artifact = _text(notice.get("artifact")) or "未提供來源"
        if (artifact, code) in known_notice_pairs:
            continue
        lines.append(f"- 通知：{_inline(artifact)}／{_inline(code)}")
    if not unavailable and status == "ready" and not notices and evidence:
        lines.append("view model 未記錄其他 unavailable／unsupported notices。")

    lines.extend(["", "## 附錄：證據與來源追溯", ""])
    lines.append("以下完整索引供查核使用；主要報告僅保留與分析內容直接相連的證據 refs。")
    _render_evidence_mapping(lines, evidence_mapping, availability)
    _render_shared_evidence_links(lines, shared_links)
    _render_evidence_locator_index(lines, evidence_sources)
    _render_source_manifest(lines, source_snapshot)

    optional_context = _mapping(payload.get("optional_context"), "optional_context")
    available_context = [key for key in _OPTIONAL_CONTEXT_LABELS if key in optional_context]
    if available_context:
        lines.extend(["", "### 已記錄的延伸脈絡來源", ""])
        lines.append("以下資料已存在於 view model；本預覽不重新解讀或擴寫其內容。")
        lines.extend(f"- {_OPTIONAL_CONTEXT_LABELS[key]}（{_inline(key)}）" for key in available_context)

    return _format_markdown(lines)
