"""Deterministic local document ingestion with a complete source manifest."""
from __future__ import annotations

import mimetypes
import re
import subprocess
import zipfile
from pathlib import Path
from xml.etree import ElementTree


TEXT_SUFFIXES = {".txt", ".md", ".vtt", ".srt"}
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | {".pdf", ".docx", ".xlsx"}


def _files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return [p for p in sorted(path.rglob("*")) if p.is_file()]


PDF_READER_UNAVAILABLE = "pdf_reader_unavailable"


class _ReaderUnavailable(Exception):
    """A tool the reader needs (Poppler) is not installed on this machine."""


def _run(args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise _ReaderUnavailable(args[0]) from exc


def _pdf(path: Path) -> tuple[list[tuple[str, str]], int, list[str]]:
    try:
        return _pdf_pages(path)
    except _ReaderUnavailable as exc:
        return [], 0, [f"{PDF_READER_UNAVAILABLE}: {exc} not found (Poppler is not installed)"]


def _pdf_pages(path: Path) -> tuple[list[tuple[str, str]], int, list[str]]:
    info = _run(["pdfinfo", str(path)])
    if info.returncode:
        return [], 0, [f"pdfinfo failed: {info.stderr.strip() or info.stdout.strip()}"]
    match = re.search(r"^Pages:\s*(\d+)", info.stdout, re.MULTILINE)
    pages = int(match.group(1)) if match else 0
    units, errors = [], []
    for page in range(1, pages + 1):
        result = _run(["pdftotext", "-f", str(page), "-l", str(page), "-layout", str(path), "-"])
        text = result.stdout.strip() if result.returncode == 0 else ""
        if text:
            units.append((f"page {page}", text))
        else:
            reason = result.stderr.strip() if result.returncode else "no extractable text; OCR required"
            errors.append(f"page {page}: {reason}")
    return units, pages, errors


def _docx(path: Path) -> tuple[list[tuple[str, str]], int, list[str]]:
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
        root = ElementTree.fromstring(xml)
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        paragraphs = []
        for paragraph in root.iter(ns + "p"):
            text = "".join(node.text or "" for node in paragraph.iter(ns + "t")).strip()
            if text:
                paragraphs.append(text)
        content = "\n".join(paragraphs)
        return ([("document", content)] if content else []), 1, ([] if content else ["document: no extractable text"])
    except Exception as exc:
        return [], 1, [f"document: {type(exc).__name__}: {exc}"]


def _xlsx(path: Path) -> tuple[list[tuple[str, str]], int, list[str]]:
    try:
        import openpyxl
    except ImportError:
        return [], 0, ["openpyxl is required for spreadsheet extraction"]
    try:
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        units = []
        for sheet in book.worksheets:
            rows = []
            for row in sheet.iter_rows(values_only=True):
                values = [str(value).strip() for value in row if value is not None and str(value).strip()]
                if values:
                    rows.append("\t".join(values))
            if rows:
                units.append((f"sheet {sheet.title}", "\n".join(rows)))
        total = len(book.worksheets)
        book.close()
        errors = []
        if not units and total:
            errors.append("workbook: no extractable values")
        return units, total, errors
    except Exception as exc:
        return [], 1, [f"workbook: {type(exc).__name__}: {exc}"]


def _text(path: Path) -> tuple[list[tuple[str, str]], int, list[str]]:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").strip()
        return ([("text", content)] if content else []), 1, ([] if content else ["text: empty file"])
    except Exception as exc:
        return [], 1, [f"text: {type(exc).__name__}: {exc}"]


def unavailable_sources(manifest: dict) -> list[str]:
    """File names whose reader is missing on this machine (status reader_unavailable)."""
    return [str(s.get("filename")) for s in (manifest or {}).get("sources") or []
            if isinstance(s, dict) and s.get("status") == "reader_unavailable"]


def ingest(path: Path) -> tuple[str, dict]:
    sources, blocks = [], []
    for index, file in enumerate(_files(path), 1):
        suffix = file.suffix.lower()
        media = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if suffix not in SUPPORTED_SUFFIXES:
            units, total, errors, status = [], 0, [f"unsupported file type: {suffix or '(none)'}"], "unsupported"
        else:
            units, total, errors = (_pdf(file) if suffix == ".pdf" else _docx(file) if suffix == ".docx" else _xlsx(file) if suffix == ".xlsx" else _text(file))
            status = "extracted" if units and not errors else "partial" if units else "empty" if errors and all("empty" in e or "OCR" in e or "no extractable" in e for e in errors) else "error"
            if any(e.startswith(PDF_READER_UNAVAILABLE) for e in errors):
                status = "reader_unavailable"
        sources.append({"id": f"SRC-{index:03d}", "filename": file.name, "media_type": media, "status": status, "units_total": total, "units_extracted": len(units), "errors": errors})
        for locator, content in units:
            blocks.append(f"# {file.name} · {locator}\n\n{content}")
    manifest = {
        "schema_version": 2,
        "sources": sources,
        "summary": {
            "files_total": len(sources),
            "files_extracted": sum(s["status"] in {"extracted", "partial"} for s in sources),
            "units_total": sum(s["units_total"] for s in sources),
            "units_extracted": sum(s["units_extracted"] for s in sources),
        },
    }
    return "\n\n---\n\n".join(blocks), manifest
