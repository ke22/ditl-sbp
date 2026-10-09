"""Deterministic XLSX renderer for the Evidence & Decision Appendix."""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .delivery import DeliveryError, DeliveryViewModel
from .delivery_evidence_appendix import EVIDENCE_APPENDIX
from .delivery_xlsx import _resolve_output_path, _write_sheet


class EvidenceAppendixXLSXError(RuntimeError):
    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _records(value: Any, field: str) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise DeliveryError("view_model_invalid", field, "expected array of objects")
    return value


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _record_headers(records: list[Mapping[str, Any]], leading: tuple[str, ...]) -> list[str]:
    extras = sorted({key for record in records for key in record if key not in leading and key != "record_json"})
    return [*leading, *extras, "record_json"]


def _record_rows(records: list[Mapping[str, Any]], headers: list[str]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for record in records:
        rows.append([
            _json_text(record) if header == "record_json" else record.get(header)
            for header in headers
        ])
    return rows


def _sheet_data(payload: Mapping[str, Any]) -> list[tuple[str, list[str], list[list[Any]], dict[str, int]]]:
    evidence = _records(payload.get("evidence_index"), "evidence_index")
    evidence_leading = ("id", "source", "locator", "role", "tier", "content_type", "text")
    evidence_headers = _record_headers(evidence, evidence_leading)

    findings = _records(payload.get("finding_to_evidence"), "finding_to_evidence")
    finding_headers = [
        "record_type", "delivery_type", "finding_ref", "finding_path", "evidence_id", "evidence_refs",
        "provenance", "claim_state", "status", "source_ref", "version_ref", "details",
    ]
    decisions = _records(payload.get("decision_log"), "decision_log")
    decision_headers = [
        "decision_type", "decision_ref", "decision", "state", "rationale", "source_ref", "version_ref",
        "provenance", "claim_state", "details",
    ]
    artifacts = _records(payload.get("artifact_traceability"), "artifact_traceability")
    artifact_headers = [
        "record_type", "delivery_type", "artifact_type", "source_ref", "status", "version_ref",
        "content_digest", "versioning", "resolution_method", "recorded_state", "reason", "key", "detail",
        "resolved_path", "details",
    ]
    external = _records(payload.get("external_research"), "external_research")
    external_headers = [
        "record_type", "ref", "decision", "support_eligibility", "text", "researcher_rationale", "source_ref",
        "external_not_user_evidence", "status", "derived_from", "details",
    ]
    widths = {
        "id": 16, "source": 34, "locator": 34, "role": 34, "text": 60, "details": 54,
        "record_json": 54, "rationale": 60, "researcher_rationale": 60, "content_digest": 42,
        "source_ref": 42, "resolved_path": 42, "finding_path": 42, "evidence_refs": 30, "derived_from": 30,
    }
    return [
        ("Evidence_Index", evidence_headers, _record_rows(evidence, evidence_headers), widths),
        ("Finding_to_Evidence", finding_headers, _record_rows(findings, finding_headers), widths),
        ("Decision_Log", decision_headers, _record_rows(decisions, decision_headers), widths),
        ("Artifact_Traceability", artifact_headers, _record_rows(artifacts, artifact_headers), widths),
        ("External_Research", external_headers, _record_rows(external, external_headers), widths),
    ]


def render_evidence_appendix_xlsx(
    view_model: DeliveryViewModel,
    output_path: str | Path,
    *,
    data_root: str | Path | None = None,
) -> Path:
    if not isinstance(view_model, DeliveryViewModel):
        raise DeliveryError("renderer_invalid_input", "evidence_appendix_xlsx", "expected DeliveryViewModel")
    if view_model.export_id != EVIDENCE_APPENDIX or view_model.delivery_type != EVIDENCE_APPENDIX:
        raise DeliveryError("renderer_invalid_input", "evidence_appendix_xlsx", "wrong delivery view")
    path = _resolve_output_path(output_path, data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import xlsxwriter
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise EvidenceAppendixXLSXError("xlsxwriter_unavailable") from exc

    fd, temp_name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".xlsx", dir=path.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        workbook = xlsxwriter.Workbook(str(temp_path), {"constant_memory": False})
        workbook.set_properties({"title": "Evidence & Decision Traceability", "comments": "Read-only Local V1 delivery projection"})
        for name, headers, rows, widths in _sheet_data(view_model.to_dict()):
            _write_sheet(workbook, name, headers, rows, widths=widths)
        workbook.close()
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return path
