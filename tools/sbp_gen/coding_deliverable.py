"""coding-deliverable-three-layers (C4, researcher ruling 2026-10-03): the
「開放／主軸／選擇編碼」 client deliverable as the three DITL coding layers.

One read-only export model, built from the canonical artifacts only
(open_coding.yaml, axial_coding.yaml, selective_themes.yaml, evidence.yaml),
feeds both the printed PDF and the XLSX, so rows, memberships, quotes and
numbering cannot diverge. It never reads the JTBD selection or any storyline.

O-01 / A-01 / S-01 are export-local reading numbers. They come from one
deterministic mapping of canonical ids per layer; memberships are resolved
through the canonical refs first and only then shown by number. Nothing parses
a reading number back into provenance.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml

from . import selective_themes, semantic_segments, source_attribution

LEGACY_NOTICE = "這份分析使用舊方法，沒有選擇編碼；重新編碼後才會有。"
NOT_FORMED_NOTICE = "還沒有形成選擇編碼。"
# representative-quotes-standalone: the cell when no recorded quote reads on its own.
NO_STANDALONE_QUOTE = "這個主題沒有能單獨讀懂的原話。"
UNKNOWN_SPEAKER = "說話者未記錄"

SHEET_OPEN = "01_開放編碼"
SHEET_AXIAL = "02_主軸編碼"
SHEET_SELECTIVE = "03_選擇編碼"
# coding-deliverable-analysis-view (researcher ruling 2026-10-05): open coding
# shows one readable source quote; axial shows the relationship and why the
# theme formed; representative quotes are at most two per theme.
OPEN_HEADERS = ["編號", "開放編碼", "描述", "原始證據", "角色・出處"]
AXIAL_HEADERS = ["編號", "主題名稱", "定義", "關係陳述", "支持摘要", "代表語句", "包含的開放編碼", "形成說明"]
SELECTIVE_HEADERS = ["編號", "核心／次要", "主題名稱", "核心一句", "洞察敘事", "支持摘要",
                     "包含的主軸編碼", "包含的開放編碼", "代表語句", "關鍵洞察"]
REPRESENTATIVE_QUOTES_SHOWN = 2
TIER_LABELS = {"core": "核心", "secondary": "次要"}


class CodingDeliverableError(RuntimeError):
    pass


def _load(path: Path):
    if not path.is_file():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _numbers(ids: list, prefix: str) -> dict:
    """One deterministic reading number per canonical id, in the given order."""
    width = max(2, len(str(len(ids))))
    return {cid: f"{prefix}-{index:0{width}d}" for index, cid in enumerate(ids, start=1)}


def quote_text(evidence: dict) -> str:
    """「原話」— 說話者・locator, the locator exactly as recorded. A time is
    printed only when the locator itself holds one; none is ever derived."""
    speaker = str(evidence.get("role") or "").strip() or UNKNOWN_SPEAKER
    locator = str(evidence.get("locator") or "").strip()
    source = f"{speaker}・{locator}" if locator else speaker
    return f"「{str(evidence.get('text') or '').strip()}」— {source}"


def _quotes(refs, evidence_by_id: dict) -> list[str]:
    # A ref with no evidence behind it is skipped, never filled in.
    return [quote_text(evidence_by_id[ref]) for ref in refs or [] if ref in evidence_by_id]


def _representative(item: dict, concepts_by_id: dict, segments: dict, evidence_by_id: dict,
                    sessions: dict | None = None) -> list[str]:
    """converge-coding-contracts: the chosen open codes' readable quotes,
    unchanged, in their recorded order and at most REPRESENTATIVE_QUOTES_SHOWN
    of them; an older theme falls back to its chosen evidence texts.

    representative-quotes-standalone: a quote that does not read on its own is
    skipped, never replaced, and each is printed 「⋯」（角色，S 編號）; the
    locator stays in the open-coding layer."""
    if not item.get("representative_evidence_refs") and (
            "representative_quote_refs" in item or item.get("concept_refs")):
        quotes = []
        for ref in item.get("representative_quote_refs") or []:
            code = concepts_by_id.get(ref) or {}
            if semantic_segments.standalone_quote_problem(code, evidence_by_id):
                continue
            view = semantic_segments.quote_view(code, segments, evidence_by_id, sessions)
            if view:
                quotes.append(representative_quote_text(view))
        return quotes[:REPRESENTATIVE_QUOTES_SHOWN] or [NO_STANDALONE_QUOTE]
    return _quotes(item.get("representative_evidence_refs"), evidence_by_id)[:REPRESENTATIVE_QUOTES_SHOWN]


def representative_quote_text(view: dict) -> str:
    """「⋯」（角色，S 編號）; without a resolvable source, the speaker and locator
    line it had before -- no code is invented."""
    if not view.get("session"):
        return semantic_segments.quote_text(view)
    return f"「{view['answer']}」（{view.get('role') or UNKNOWN_SPEAKER}，{view['session']}）"


def _source_evidence(code: dict, segments: dict, evidence_by_id: dict,
                     sessions: dict | None = None) -> tuple[str, str]:
    """(原始證據, 角色・出處) for an open code: its readable quote, or for an
    older code without one, the first evidence it cites. The source column
    carries the source code (representative-quotes-standalone), so a
    representative quote printed as （角色，S 編號） is found here with its
    locator."""
    view = semantic_segments.quote_view(code, segments, evidence_by_id, sessions)
    if view:
        answer = f"「{view['answer']}」"
        text = f"問：{view['question']}　答：{answer}" if view.get("question") else answer
        speaker, locator = view.get("role") or UNKNOWN_SPEAKER, view.get("locator") or ""
        speaker = f"{speaker}・{view['session']}" if view.get("session") else speaker
    else:
        first = next((evidence_by_id[ref] for ref in code.get("evidence_refs") or [] if ref in evidence_by_id), None)
        if first is None:
            return "", ""
        text = f"「{str(first.get('text') or '').strip()}」"
        speaker = str(first.get("role") or "").strip() or UNKNOWN_SPEAKER
        locator = str(first.get("locator") or "").strip()
    return text, f"{speaker}・{locator}" if locator else speaker


def _labelled(refs, numbers: dict, names: dict) -> list[str]:
    return [f"{numbers[ref]} {names[ref]}".rstrip() for ref in refs or [] if ref in numbers]


def build(run_dir: str | Path) -> dict:
    """The export model: {open, axial: {rows, formation_note}, selective:
    {rows, notice}, legacy}. Every value is the text to print."""
    run = Path(run_dir)
    open_doc = _load(run / "open_coding.yaml") or {}
    axial_doc = _load(run / "axial_coding.yaml") or {}
    evidence = _load(run / "evidence.yaml") or []
    evidence_by_id = {e["id"]: e for e in evidence if isinstance(e, dict) and e.get("id")}
    sessions = source_attribution.session_codes(list(evidence_by_id.values()))

    concepts = [c for c in open_doc.get("concepts") or [] if isinstance(c, dict) and c.get("id")]
    concepts_by_id = {c["id"]: c for c in concepts}
    segments = semantic_segments.segments_by_id(semantic_segments.load(run))
    categories = [c for c in axial_doc.get("categories") or [] if isinstance(c, dict) and c.get("id")]
    open_numbers = _numbers([c["id"] for c in concepts], "O")
    open_names = {c["id"]: str(c.get("name") or "") for c in concepts}
    axial_numbers = _numbers([c["id"] for c in categories], "A")
    axial_names = {c["id"]: str(c.get("category_name") or c.get("label") or "") for c in categories}

    open_rows = []
    for c in concepts:
        evidence_text, source = _source_evidence(c, segments, evidence_by_id, sessions)
        open_rows.append({"no": open_numbers[c["id"]], "name": open_names[c["id"]],
                          "description": str(c.get("description") or ""), "evidence": evidence_text, "source": source})
    # An old-method category has no definition or explicit membership: those
    # cells stay empty; membership is never inferred from shared evidence.
    axial_rows = [{"no": axial_numbers[c["id"]], "theme": axial_names[c["id"]],
                   "definition": str(c.get("definition") or ""),
                   "relation": str(c.get("label") or ""),
                   "support": str(c.get("support_summary") or ""),
                   "quotes": _representative(c, concepts_by_id, segments, evidence_by_id, sessions),
                   "codes": _labelled(c.get("concept_refs"), open_numbers, open_names),
                   # The PDF prints numbers only; the workbook keeps number and name.
                   "code_numbers": [open_numbers[ref] for ref in c.get("concept_refs") or [] if ref in open_numbers],
                   "formation": str(c.get("discriminating_basis") or "")} for c in categories]

    themes_doc = selective_themes.load_document(run)
    legacy = selective_themes.freshness(run).get("state") == "legacy"
    selective: dict = {"rows": [], "notice": None}
    if themes_doc is None:
        selective["notice"] = LEGACY_NOTICE if legacy else NOT_FORMED_NOTICE
    else:
        themes = [t for t in themes_doc.get("themes") or [] if isinstance(t, dict) and t.get("id")]
        ordered = [t for t in themes if t.get("tier") == "core"] + [t for t in themes if t.get("tier") != "core"]
        selective_numbers = _numbers([t["id"] for t in ordered], "S")
        selective["rows"] = [{
            "no": selective_numbers[t["id"]],
            "tier": TIER_LABELS.get(t.get("tier"), str(t.get("tier") or "")),
            "name": str(t.get("name") or ""),
            "core_sentence": str(t.get("core_sentence") or ""),
            "insight": str(t.get("insight") or ""),
            "support": str(t.get("support_summary") or ""),
            "axial": _labelled(t.get("axial_refs"), axial_numbers, axial_names),
            "codes": _labelled(t.get("concept_refs"), open_numbers, open_names),
            "quotes": _representative(t, concepts_by_id, segments, evidence_by_id, sessions),
            "key_insights": [str(item) for item in t.get("key_insights") or []],
        } for t in ordered]

    return {"open": open_rows,
            "axial": {"rows": axial_rows, "formation_note": str(axial_doc.get("formation_note") or "")},
            "selective": selective, "legacy": legacy}


def sheet_rows(model: dict) -> list[tuple[str, list[str], list[list[str]]]]:
    """The three sheets as (name, headers, rows); the same rows the PDF prints."""
    lines = "\n".join
    open_rows = [[r["no"], r["name"], r["description"], r["evidence"], r["source"]] for r in model["open"]]
    axial_rows = [[r["no"], r["theme"], r["definition"], r["relation"], r["support"], lines(r["quotes"]),
                   lines(r["codes"]), r["formation"]] for r in model["axial"]["rows"]]
    selective = model["selective"]
    if selective["notice"]:
        selective_rows = [[selective["notice"]] + [""] * (len(SELECTIVE_HEADERS) - 1)]
    else:
        selective_rows = [[r["no"], r["tier"], r["name"], r["core_sentence"], r["insight"], r["support"], lines(r["axial"]),
                           lines(r["codes"]), lines(r["quotes"]), lines(f"・{item}" for item in r["key_insights"])]
                          for r in selective["rows"]]
    return [(SHEET_OPEN, OPEN_HEADERS, open_rows), (SHEET_AXIAL, AXIAL_HEADERS, axial_rows),
            (SHEET_SELECTIVE, SELECTIVE_HEADERS, selective_rows)]


_WIDTHS = {"編號": 8, "開放編碼": 22, "描述": 40, "原始證據": 50, "角色・出處": 26, "主題名稱": 18, "定義": 36,
           "關係陳述": 36, "支持摘要": 44, "代表語句": 60, "形成說明": 50, "核心／次要": 10, "核心一句": 36, "洞察敘事": 60,
           "包含的主軸編碼": 28, "包含的開放編碼": 32, "關鍵洞察": 44}


def render_xlsx(model: dict, target: str | Path) -> Path:
    """Write the three sheets; the axial sheet ends with its formation note."""
    try:
        import xlsxwriter
    except ImportError as exc:
        raise CodingDeliverableError("這台機器沒有安裝輸出 Excel 需要的 XlsxWriter。") from exc
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.stem}.", suffix=".tmp.xlsx", dir=target.parent)
    os.close(descriptor)
    try:
        with xlsxwriter.Workbook(temporary, {"strings_to_formulas": False, "strings_to_urls": False}) as workbook:
            header = workbook.add_format({"bold": True, "bg_color": "#EAF0F4", "border": 1,
                                          "text_wrap": True, "valign": "top"})
            cell = workbook.add_format({"text_wrap": True, "valign": "top"})
            for name, headers, rows in sheet_rows(model):
                sheet = workbook.add_worksheet(name)
                sheet.freeze_panes(1, 0)
                for column, title in enumerate(headers):
                    sheet.write_string(0, column, title, header)
                    sheet.set_column(column, column, _WIDTHS.get(title, 18), cell)
                for row_index, values in enumerate(rows, start=1):
                    for column, value in enumerate(values):
                        sheet.write_string(row_index, column, value, cell)
                if name == SHEET_AXIAL and model["axial"]["formation_note"]:
                    note_row = len(rows) + 2
                    sheet.write_string(note_row, 0, "形成說明", header)
                    sheet.write_string(note_row, 1, model["axial"]["formation_note"], cell)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target
