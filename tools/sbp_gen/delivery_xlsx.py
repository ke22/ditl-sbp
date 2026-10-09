"""Structured, read-only XLSX renderer for coding_synthesis delivery views."""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import storage_locations

from .delivery import CODING_SYNTHESIS, DeliveryError, DeliveryViewModel


_MAX_CELL_CHARS = 32_767
_MAX_EXCEL_ROWS = 1_048_576
_MAX_EXCEL_COLUMNS = 16_384


class XLSXRenderError(RuntimeError):
    """Stable error raised when a structured workbook cannot be emitted safely."""

    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _resolve_output_path(output_path: str | Path, data_root: str | Path | None = None) -> Path:
    path = Path(output_path).expanduser().resolve()
    if path.suffix.lower() != ".xlsx":
        raise XLSXRenderError("xlsx_output_must_have_xlsx_extension")

    protected_name = storage_locations.protected_canonical_data_kind(
        path, data_root if data_root is not None else Path(__file__).resolve().parents[2]
    )
    if protected_name:
        raise XLSXRenderError("xlsx_output_cannot_target_canonical_data", protected_name)
    if path.exists() and path.is_dir():
        raise XLSXRenderError("xlsx_output_path_is_directory")
    return path


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


def _json_text(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _cell_value(value: Any) -> Any:
    if isinstance(value, (Mapping, list, tuple)):
        return _json_text(value)
    return value


def _write_cell(worksheet: Any, row: int, column: int, value: Any, cell_format: Any) -> None:
    value = _cell_value(value)
    if value is None:
        worksheet.write_blank(row, column, None, cell_format)
    elif isinstance(value, bool):
        worksheet.write_boolean(row, column, value, cell_format)
    elif isinstance(value, (int, float)):
        worksheet.write_number(row, column, value, cell_format)
    elif isinstance(value, str):
        if len(value) > _MAX_CELL_CHARS:
            raise XLSXRenderError("xlsx_cell_text_exceeds_excel_limit", f"row {row + 1}, column {column + 1}")
        # Explicit string writes prevent source text beginning with '=' from
        # becoming a formula and preserve URLs as ordinary research data.
        worksheet.write_string(row, column, value, cell_format)
    else:
        worksheet.write_string(row, column, str(value), cell_format)


def _write_sheet(
    workbook: Any,
    name: str,
    headers: list[str],
    rows: list[list[Any]],
    *,
    widths: Mapping[str, int] | None = None,
) -> None:
    if len(headers) > _MAX_EXCEL_COLUMNS:
        raise XLSXRenderError("xlsx_column_limit_exceeded", name)
    if len(rows) + 1 > _MAX_EXCEL_ROWS:
        raise XLSXRenderError("xlsx_row_limit_exceeded", name)
    worksheet = workbook.add_worksheet(name)
    header_format = workbook.add_format({
        "bold": True,
        "font_name": "Arial",
        "font_color": "#17324D",
        "bg_color": "#EAF0F4",
        "border": 1,
        "text_wrap": True,
        "valign": "top",
    })
    cell_format = workbook.add_format({
        "font_name": "Arial",
        "text_wrap": True,
        "valign": "top",
    })
    worksheet.freeze_panes(1, 0)
    worksheet.set_default_row(30)
    for column, header in enumerate(headers):
        worksheet.write_string(0, column, header, header_format)
        worksheet.set_column(
            column,
            column,
            (widths or {}).get(header, min(max(len(header) + 3, 18), 36)),
            cell_format,
        )
    for row_index, values in enumerate(rows, start=1):
        for column, value in enumerate(values):
            _write_cell(worksheet, row_index, column, value, cell_format)
    if headers:
        worksheet.autofilter(0, 0, max(0, len(rows)), len(headers) - 1)


def _ordered_headers(records: list[Mapping[str, Any]], leading: tuple[str, ...]) -> list[str]:
    extras = sorted({key for record in records for key in record if key not in leading})
    return [*leading, *extras]


def _record_rows(records: list[Mapping[str, Any]], headers: list[str]) -> list[list[Any]]:
    return [[record.get(header) for header in headers] for record in records]


def _workbook_sheets(payload: Mapping[str, Any]) -> list[tuple[str, list[str], list[list[Any]], dict[str, int]]]:
    """Project the complete view into the five contract-defined workbook tabs."""
    metadata = _mapping(payload.get("metadata"), "metadata")
    evidence = _records(payload.get("evidence"), "evidence")
    open_coding = _mapping(payload.get("open_coding"), "open_coding")
    axial_coding = _mapping(payload.get("axial_coding"), "axial_coding")
    selective_coding = _mapping(payload.get("selective_coding"), "selective_coding")
    evidence_mapping = _records(payload.get("evidence_mapping"), "evidence_mapping")
    source_snapshot = _records(payload.get("source_snapshot"), "source_snapshot")
    provenance = _mapping(payload.get("provenance"), "provenance")
    optional_context = _mapping(payload.get("optional_context"), "optional_context")

    open_concepts = _records(open_coding.get("concepts"), "open_coding.concepts")
    axial_categories = _records(axial_coding.get("categories"), "axial_coding.categories")
    evidence_leading = ("id", "tier", "role", "source", "locator", "content_type", "text")
    open_headers = ["id", "name", "description", "source", "evidence_refs_json"]
    axial_headers = [
        "id", "label", "category_name", "discriminating_basis", "conditions_json",
        "actions_json", "consequences_json", "evidence_refs_json",
    ]
    selective_headers = [
        "schema_version", "case_id", "core_category_id", "rationale", "secondary_themes_json",
        "narrative", "narrative_presence", "narrative_composed_by", "key_insights_json",
        "decision_source_ref", "decision_state",
    ]
    selective_decision = _mapping(provenance.get("selective_core_decision"), "provenance.selective_core_decision")
    narrative = selective_coding.get("narrative")
    selective_row = [[
        selective_coding.get("schema_version"),
        selective_coding.get("case_id"),
        selective_coding.get("core_category_id"),
        selective_coding.get("rationale"),
        selective_coding.get("secondary_themes"),
        narrative,
        "present" if isinstance(narrative, str) and narrative.strip() else "not_generated",
        selective_coding.get("narrative_composed_by"),
        selective_coding.get("key_insights"),
        selective_decision.get("source_ref"),
        selective_decision.get("decision_state"),
    ]] if selective_coding else []
    mapping_headers = [
        "record_type", "evidence_id", "open_code_refs_json", "axial_category_refs_json",
        "selective_core_category_refs_json", "selective_insight_refs_json", "open_code_ref",
        "axial_category_ref", "relationship", "evidence_refs_json", "artifact_type",
        "source_ref", "status", "version_ref", "content_digest", "resolution_method",
        "recorded_state", "versioning", "key", "reason", "value_json",
    ]

    mapping_rows: list[list[Any]] = []

    def append_mapping_record(record_type: str, **values: Any) -> None:
        mapping_rows.append([record_type, *(values.get(header) for header in mapping_headers[1:])])

    for item in evidence_mapping:
        append_mapping_record(
            "evidence_mapping",
            evidence_id=item.get("evidence_id"),
            open_code_refs_json=item.get("open_code_refs"),
            axial_category_refs_json=item.get("axial_category_refs"),
            selective_core_category_refs_json=item.get("selective_core_category_refs"),
            selective_insight_refs_json=item.get("selective_insight_refs"),
        )

    shared_links = _records(
        provenance.get("open_code_to_axial_category"), "provenance.open_code_to_axial_category"
    )
    for link in shared_links:
        append_mapping_record(
            "shared_evidence_link",
            open_code_ref=link.get("open_code_ref"),
            axial_category_ref=link.get("axial_category_ref"),
            relationship=link.get("relationship"),
            evidence_refs_json=link.get("evidence_refs"),
            value_json=link,
        )

    # Evidence source identity is already present in full in 01_Evidence. Keep
    # the view-model snapshot, statuses, notices, and optional context beside
    # the evidence mappings so the workbook stays at the five contract tabs.
    for source in source_snapshot:
        append_mapping_record(
            "source_snapshot",
            artifact_type=source.get("artifact_type"),
            source_ref=source.get("source_ref"),
            status=source.get("status"),
            version_ref=source.get("version_ref"),
            content_digest=source.get("content_digest"),
            resolution_method=source.get("resolution_method"),
            recorded_state=source.get("recorded_state"),
            versioning=source.get("versioning"),
            value_json=source,
        )

    availability = _mapping(payload.get("availability"), "availability")
    for key, raw_state in sorted(availability.items()):
        state = _mapping(raw_state, f"availability.{key}")
        append_mapping_record(
            "availability",
            key=key,
            status=state.get("status"),
            reason=state.get("reason"),
            value_json=state,
        )

    notices = payload.get("notices", [])
    if not isinstance(notices, list):
        raise DeliveryError("view_model_invalid", "notices", "expected array")
    for index, notice in enumerate(notices):
        append_mapping_record("notice", key=str(index), value_json=notice)

    for key, value in sorted(optional_context.items()):
        state = _mapping(availability.get(key), f"availability.{key}")
        append_mapping_record(
            "optional_context",
            artifact_type=key,
            status=state.get("status"),
            value_json=value,
        )

    coding_states = _mapping(metadata.get("coding_states"), "metadata.coding_states")
    metadata_values: list[tuple[str, Any]] = [
        ("export_id", payload.get("export_id")),
        ("delivery_type", payload.get("delivery_type")),
        ("delivery_status", payload.get("status")),
        ("view_model_schema_version", payload.get("view_model_schema_version")),
        ("project_id", metadata.get("project_id")),
        ("case_id", metadata.get("case_id")),
        ("run_ref", metadata.get("run_ref")),
    ]
    metadata_values.extend((f"coding_state.{key}", value) for key, value in sorted(coding_states.items()))
    for artifact_name, artifact in (
        ("open_coding", open_coding),
        ("axial_coding", axial_coding),
        ("selective_coding", selective_coding),
    ):
        for key in ("schema_version", "case_id"):
            if key in artifact:
                metadata_values.append((f"{artifact_name}.{key}", artifact[key]))
    for key, value in sorted(metadata.items()):
        if key not in {"project_id", "case_id", "run_ref", "coding_states"}:
            metadata_values.append((f"metadata.{key}", value))
    for key, value in metadata_values:
        append_mapping_record("delivery_metadata", key=key, value_json=value)

    for key, value in sorted(provenance.items()):
        if key in {"evidence_sources", "open_code_to_axial_category", "selective_core_decision", "selective_narrative_composed_by"}:
            continue
        append_mapping_record("provenance", key=key, value_json=value)

    sheets = [
        (
            "01_Evidence",
            _ordered_headers(evidence, evidence_leading),
            _record_rows(evidence, _ordered_headers(evidence, evidence_leading)),
            {"source": 30, "locator": 30, "text": 64},
        ),
        (
            "02_Open_Coding",
            open_headers,
            [[item.get("id"), item.get("name"), item.get("description"), item.get("source"), item.get("evidence_refs")] for item in open_concepts],
            {"name": 24, "description": 52, "source": 30, "evidence_refs_json": 36},
        ),
        (
            "03_Axial_Coding",
            axial_headers,
            [[
                item.get("id"), item.get("label"), item.get("category_name"), item.get("discriminating_basis"),
                item.get("conditions"), item.get("actions"), item.get("consequences"), item.get("evidence_refs"),
            ] for item in axial_categories],
            {"label": 30, "category_name": 22, "discriminating_basis": 58},
        ),
        (
            "04_Selective_Coding",
            selective_headers,
            selective_row,
            {"rationale": 60, "narrative": 60, "key_insights_json": 60},
        ),
        ("05_Evidence_Mapping", mapping_headers, mapping_rows, {"value_json": 60, "content_digest": 42}),
    ]
    return sheets


def render_coding_synthesis_xlsx(
    view_model: DeliveryViewModel,
    output_path: str | Path,
    *,
    data_root: str | Path | None = None,
) -> Path:
    """Write complete structured coding/evidence data from one normalized view.

    This renderer performs no source resolution, model call, or canonical write.
    All source strings are emitted as literal cell values; array/object fields
    use stable JSON text so reference identity and nested data remain intact.
    """
    if not isinstance(view_model, DeliveryViewModel):
        raise DeliveryError("renderer_invalid_input", "delivery_view", "expected DeliveryViewModel")
    payload = view_model.to_dict()
    if payload.get("export_id") != CODING_SYNTHESIS or payload.get("delivery_type") != CODING_SYNTHESIS:
        raise DeliveryError("renderer_unsupported_delivery_type", "delivery_view", payload.get("export_id"))

    try:
        import xlsxwriter
    except ImportError as exc:
        raise XLSXRenderError("xlsx_runtime_missing", "XlsxWriter is required for XLSX output") from exc

    target = _resolve_output_path(output_path, data_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.stem}.", suffix=".tmp.xlsx", dir=target.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        options = {
            "constant_memory": True,
            "strings_to_formulas": False,
            "strings_to_urls": False,
        }
        with xlsxwriter.Workbook(str(temporary_path), options) as workbook:
            workbook.set_properties({
                "title": "Coding & Synthesis structured export",
                "subject": "Normalized Local V1 coding_synthesis delivery view",
                "author": "Local V1 Research Delivery",
            })
            for name, headers, rows, widths in _workbook_sheets(payload):
                _write_sheet(workbook, name, headers, rows, widths=widths)
        if not temporary_path.is_file() or temporary_path.stat().st_size < 100:
            raise XLSXRenderError("xlsx_render_invalid_output")
        os.replace(temporary_path, target)
    except XLSXRenderError:
        raise
    except Exception as exc:
        raise XLSXRenderError("xlsx_render_failed", type(exc).__name__) from exc
    finally:
        temporary_path.unlink(missing_ok=True)
    return target
