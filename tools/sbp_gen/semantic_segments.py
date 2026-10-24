"""open-coding-on-semantic-segments (researcher ruling 2026-10-03): each
transcript is divided, in time order, into meaningful segments, and open coding
codes segments instead of evidence fragments.

The model returns only line ranges and analysis judgements (summary,
interviewer lines, role, scope, contained evidence). The passage is cut by code
from the numbered transcript, so it is the transcript's own words. Coverage and
evidence placement are checked by code.

A segment is an analysis unit, not evidence: evidence units and every
downstream citation stay unchanged, and the summary and interviewer-line
judgement are analysis metadata that nothing downstream cites as evidence.
"""
from __future__ import annotations

import concurrent.futures
import os
import re
import tempfile
import threading
import time
from pathlib import Path

import yaml

from . import answer_kind, source_attribution, stages

SEGMENTS_FILE = "semantic_segments.yaml"
SUMMARY_MAX_CHARS = 100
_TIMECODE_RE = re.compile(r"^(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})")


class SegmentError(RuntimeError):
    pass


# ── transcripts and numbered lines ─────────────────────────────────────

def transcripts_from_raw(raw_text: str) -> list[tuple[str, str]]:
    """(source name, content) per transcript, in upload order -- the canonical
    source scopes (stages.source_scopes): a file's blocks joined in order, a
    merged upload split at each declared document, the file name keeping only
    the text before its first declaration."""
    return stages.source_scopes(raw_text)


def number_lines(content: str) -> list[dict]:
    """One numbered line per subtitle cue (its spoken text with its start and
    end time) or per non-blank line of any other text. Nothing is rewritten."""
    raw = content.split("\n")
    lines: list[dict] = []
    i = 0
    while i < len(raw):
        stripped = raw[i].strip()
        timecode = _TIMECODE_RE.match(raw[i + 1].strip()) if stripped.isdigit() and i + 1 < len(raw) else None
        if timecode:
            j, spoken = i + 2, []
            while j < len(raw) and raw[j].strip():
                spoken.append(raw[j].strip())
                j += 1
            if spoken:
                lines.append({"n": len(lines) + 1, "text": " ".join(spoken),
                              "start": timecode.group(1), "end": timecode.group(2)})
            i = j + 1
            continue
        if stripped:
            lines.append({"n": len(lines) + 1, "text": raw[i].rstrip()})
        i += 1
    return lines


def render_numbered(lines: list[dict]) -> str:
    out = []
    for line in lines:
        stamp = f" [{line['start']}–{line['end']}]" if line.get("start") else ""
        out.append(f"L{line['n']}{stamp} {line['text']}")
    return "\n".join(out)


def user_message(source: str, lines: list[dict], evidence: list[dict], case_id: str) -> str:
    units = [{key: e.get(key) for key in ("id", "text", "question_context", "locator") if e.get(key)}
             for e in evidence]
    return (f"## 案例\ncase_id: {case_id}\n\n## 逐字稿（單一來源）：{source}\n"
            f"共 {len(lines)} 行。每行開頭是行號；字幕檔的行附有時間。\n```\n{render_numbered(lines)}\n```\n\n"
            "## 這份逐字稿已整理出的證據單位（每一條都要歸到恰好一個分析範圍內的段落）\n```yaml\n"
            + stages._yaml_dump(units) + "```")


# ── validation ─────────────────────────────────────────────────────────

def _passage(lines: list[dict], start: int, end: int, exclude: set | None = None, only: set | None = None) -> str:
    return "\n".join(line["text"] for line in lines[start - 1:end]
                     if (exclude is None or line["n"] not in exclude) and (only is None or line["n"] in only))


def segment_errors(draft: object, lines: list[dict], evidence: list[dict]) -> list[str]:
    """Coverage, order, scope reasons and evidence placement for one transcript."""
    errors = stages.V.structural_errors(draft, stages.V.load_schema("semantic-segments-draft"))
    if errors or not isinstance(draft, dict):
        return errors or ["[semantic_segments] 回覆要是一個物件"]
    segments = [s for s in draft.get("segments") or [] if isinstance(s, dict)]
    total = len(lines)
    expected = 1
    for index, segment in enumerate(segments, start=1):
        start, end = segment.get("start_line"), segment.get("end_line")
        if start > end:
            errors.append(f"[semantic_segments] 第 {index} 段的起始行 {start} 在結束行 {end} 之後")
            continue
        if start > expected:
            errors.append(f"[semantic_segments] 第 {expected}–{start - 1} 行沒有落在任何段落")
        elif start < expected:
            errors.append(f"[semantic_segments] 第 {start}–{min(end, expected - 1)} 行被兩個段落重複涵蓋")
        expected = max(expected, end + 1)
        if end > total:
            errors.append(f"[semantic_segments] 第 {index} 段結束在第 {end} 行，但逐字稿只有 {total} 行")
        for n in segment.get("question_lines") or []:
            if not start <= n <= end:
                errors.append(f"[semantic_segments] 第 {index} 段標的訪員行 {n} 不在第 {start}–{end} 行之內")
        if not segment.get("in_scope") and not str(segment.get("out_of_scope_reason") or "").strip():
            errors.append(f"[semantic_segments] 第 {index} 段（第 {start}–{end} 行）不在分析範圍，要寫原因")
    if expected <= total:
        errors.append(f"[semantic_segments] 第 {expected}–{total} 行沒有落在任何段落")
    by_id = {e["id"]: e for e in evidence if isinstance(e, dict) and e.get("id")}
    placed: dict[str, int] = {}
    for index, segment in enumerate(segments, start=1):
        for ref in segment.get("evidence_refs") or []:
            if ref not in by_id:
                errors.append(f"[semantic_segments] 第 {index} 段列了不屬於這份逐字稿的證據 {ref}")
                continue
            if not segment.get("in_scope"):
                errors.append(f"[semantic_segments] 證據 {ref} 被放在不在分析範圍的第 {index} 段")
            if ref in placed:
                errors.append(f"[semantic_segments] 證據 {ref} 同時列在第 {placed[ref]} 段與第 {index} 段")
                continue
            placed[ref] = index
            start, end = segment.get("start_line"), segment.get("end_line")
            if isinstance(start, int) and isinstance(end, int) and start <= end <= total \
                    and not stages._is_verbatim_excerpt(by_id[ref].get("text"), _passage(lines, start, end)):
                errors.append(f"[semantic_segments] 證據 {ref} 的原文不在第 {index} 段（第 {start}–{end} 行）裡")
    for ref in by_id:
        if ref not in placed:
            errors.append(f"[semantic_segments] 證據 {ref} 沒有歸到任何段落")
    return errors


# ── assembly ──────────────────────────────────────────────────────────

def _locator(lines: list[dict], start: int, end: int) -> str:
    timed = [line for line in lines[start - 1:end] if line.get("start")]
    if timed:
        return f"{timed[0]['start']}–{timed[-1]['end']}"
    return f"第 {start}–{end} 行"


def assemble(source: str, lines: list[dict], draft: dict, first_id: int) -> dict:
    """The stored transcript: passages cut by code from the numbered lines."""
    segments = []
    for offset, item in enumerate(draft["segments"]):
        start, end = item["start_line"], item["end_line"]
        questions = set(item.get("question_lines") or [])
        segment = {
            "id": f"SEG-{first_id + offset}", "start_line": start, "end_line": end,
            "locator": _locator(lines, start, end), "summary": item["summary"],
            "question_lines": sorted(questions), "role": item["role"], "in_scope": bool(item["in_scope"]),
            "passage": _passage(lines, start, end),
            "respondent_passage": _passage(lines, start, end, exclude=questions),
            "question_passage": _passage(lines, start, end, only=questions),
            "evidence_refs": list(item.get("evidence_refs") or []),
        }
        if not segment["in_scope"]:
            segment["out_of_scope_reason"] = item["out_of_scope_reason"]
        segments.append(segment)
    return {"source": source, "line_count": len(lines), "segments": segments}


def evidence_by_transcript(transcripts: list[tuple[str, str]], evidence: list) -> dict[str, list]:
    """Each transcript's evidence; evidence from a transcript not uploaded is refused."""
    evidence_by_source: dict[str, list] = {}
    for record in evidence or []:
        if isinstance(record, dict):
            evidence_by_source.setdefault(record.get("source") or "", []).append(record)
    known = {source for source, _content in transcripts}
    stray = sorted({source for source in evidence_by_source if source not in known})
    if stray:
        raise SegmentError(f"這些證據的來源不在上傳的逐字稿裡：{'、'.join(stray)}")
    return evidence_by_source


def segment_transcript(source: str, content: str, evidence_here: list, case_id: str,
                       opts: dict) -> tuple[list, dict, dict]:
    """One transcript's segmentation call (validated, retried): (lines, draft, call)."""
    lines = number_lines(content)
    data, usage = stages._generate(
        "semantic_segments", stages._system("semantic_segments"),
        user_message(source, lines, evidence_here, case_id),
        lambda d: segment_errors(d, lines, evidence_here), opts)
    call = {"source": source, **{key: usage.get(key) for key in (
        "seconds", "wall_seconds", "started_at", "finished_at", "executor", "tokens", "attempts",
        "model_requested", "effort_requested", "model_source", "model_effective") if usage.get(key) is not None}}
    return lines, data, call


def build_document(results: list[tuple[str, list, dict, dict]], case_id: str,
                   wall_seconds: float, concurrency: int | None = None) -> tuple[dict, dict]:
    """Assemble transcripts in upload order, numbering SEG ids across them."""
    stored, next_id, calls = [], 1, []
    for source, lines, data, call in results:
        transcript = assemble(source, lines, data, next_id)
        next_id += len(transcript["segments"])
        stored.append(transcript)
        calls.append(call)
    doc = {"schema_version": 1, "case_id": case_id, "transcripts": stored}
    usage = {"tokens": sum(c.get("tokens") or 0 for c in calls), "seconds": sum(c.get("seconds") or 0 for c in calls),
             "wall_seconds": round(wall_seconds), "calls": calls}
    if concurrency is not None:
        # fixed-coding-concurrency-five: the calls this run allowed at once.
        usage["concurrency"] = concurrency
    return doc, usage


def concurrency(opts: dict) -> int:
    workers = max(1, int(opts.get("semantic_segments_concurrency")
                         or os.environ.get("SBP_SEMANTIC_SEGMENTS_CONCURRENCY") or stages.CODING_CONCURRENCY_DEFAULT))
    if (os.environ.get("SBP_LLM_PROVIDER") or "").strip().lower() == "transport":
        workers = 1
    return workers


def generate(raw_text: str, evidence: list, opts: dict) -> tuple[dict, dict]:
    """Segment every transcript (one call each, up to CODING_CONCURRENCY_DEFAULT at once) and
    return (document, usage). A transcript with no evidence is still segmented."""
    case_id = opts.get("case_id") or "case"
    transcripts = transcripts_from_raw(raw_text)
    if not transcripts:
        raise SegmentError("找不到可以分段的逐字稿。")
    evidence_by_source = evidence_by_transcript(transcripts, evidence)
    progress_lock, finished = threading.Lock(), [0]

    def one(item: tuple[str, str]) -> tuple[str, list, dict, dict]:
        source, content = item
        lines, data, call = segment_transcript(source, content, evidence_by_source.get(source, []), case_id, opts)
        with progress_lock:
            finished[0] += 1
            stages._report_progress("語意分段", finished[0], len(transcripts), "份")
        return source, lines, data, call

    workers = concurrency(opts)
    wall_started = time.monotonic()
    if len(transcripts) > 1 and workers > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(transcripts))) as pool:
            results = stages.map_largest_first(pool, one, transcripts, lambda item: len(item[1]))
    else:
        results = [one(item) for item in transcripts]
    return build_document(results, case_id, time.monotonic() - wall_started, min(workers, len(transcripts)))


# ── reading ───────────────────────────────────────────────────────────

def load(run_dir: str | Path) -> dict | None:
    path = Path(run_dir) / SEGMENTS_FILE
    if not path.is_file():
        return None
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return doc if isinstance(doc, dict) else None


def segments_by_source(doc: dict | None) -> dict[str, list[dict]]:
    return {t["source"]: [s for s in t.get("segments") or [] if isinstance(s, dict)]
            for t in (doc or {}).get("transcripts") or [] if isinstance(t, dict) and t.get("source")}


def segments_by_id(doc: dict | None) -> dict[str, dict]:
    return {s["id"]: s for t in (doc or {}).get("transcripts") or [] if isinstance(t, dict)
            for s in t.get("segments") or [] if isinstance(s, dict) and s.get("id")}


def quote_speaker(code: dict, segment_role: str, evidence_by_id: dict | None) -> str:
    """readable-quote-presentation: the label every cited evidence unit shares,
    when it is more specific than the segment's role; else the segment's role."""
    if evidence_by_id:
        labels = {str(evidence_by_id[ref].get("role") or "").strip()
                  for ref in code.get("evidence_refs") or [] if isinstance(evidence_by_id.get(ref), dict)} - {""}
        if len(labels) == 1 and next(iter(labels)) != segment_role:
            return next(iter(labels))
    return segment_role


def quote_view(code: dict, segments: dict[str, dict], evidence_by_id: dict | None = None,
               sessions: dict[str, str] | None = None) -> dict | None:
    """converge-coding-contracts: an open code's readable quote as shown --
    answer, optional interviewer question, the speaker (the cited evidence's
    shared label when more specific, else the segment's role) and the
    segment's location. The quote object itself is never rewritten.

    representative-quotes-standalone: plus `session`, the source code(s) of the
    cited evidence (S1..Sn, as the CJM stages number sources), when any cited
    evidence records a source. `sessions` may be passed precomputed."""
    quote = code.get("quote") if isinstance(code, dict) else None
    if not isinstance(quote, dict) or not quote.get("answer"):
        return None
    segment = next((segments[ref] for ref in code.get("segment_refs") or [] if ref in segments), {})
    view = {"answer": quote["answer"], "role": quote_speaker(code, segment.get("role") or "", evidence_by_id),
            "locator": segment.get("locator") or ""}
    if quote.get("question"):
        view["question"] = quote["question"]
    session = quote_session(code, evidence_by_id, sessions)
    if session:
        view["session"] = session
    return view


def quote_session(code: dict, evidence_by_id: dict | None, sessions: dict[str, str] | None = None) -> str:
    """The cited evidence's source codes, each once, in code order; '' when no
    cited evidence records a source. Never invented."""
    if not evidence_by_id:
        return ""
    if sessions is None:
        sessions = source_attribution.session_codes(list(evidence_by_id.values()))
    codes = {sessions[str(evidence_by_id[ref].get("source"))]
             for ref in code.get("evidence_refs") or []
             if isinstance(evidence_by_id.get(ref), dict) and str(evidence_by_id[ref].get("source")) in sessions
             and evidence_by_id[ref].get("source")}
    return "、".join(sorted(codes, key=lambda c: int(c[1:])))


# ── representative quotes (representative-quotes-standalone) ──────────
# Researcher ruling 2026-10-22: a representative quote reads on its own, like a
# cited interview quotation. One decision, shared by display, export,
# generation input and validation.

# Calibrated on the MUJI v3benchmark and Henglong v3E2E2 runs (2026-10-22):
# under six characters the answers were single words or needed the question
# (「人少。」「捐贈」); from six they were whole judgements (「幾乎沒有等待」).
REPRESENTATIVE_QUOTE_MIN_CHARS = 6
_QUOTE_PUNCTUATION_RE = re.compile(r"[\s　，。、．,.!！?？~～…:：;；「」『』\"'（）()\-—]")


def _answer_problem(text: str) -> str | None:
    if answer_kind.classify({"text": text}) != answer_kind.SUBSTANTIVE:
        return "只有回答詞，單獨讀不出在回答什麼"
    if len(_QUOTE_PUNCTUATION_RE.sub("", text)) < REPRESENTATIVE_QUOTE_MIN_CHARS:
        return f"不到 {REPRESENTATIVE_QUOTE_MIN_CHARS} 個字，單獨讀不出在說什麼"
    return None


def standalone_quote_problem(code: dict, evidence_by_id: dict | None) -> str | None:
    """Why an open code's readable quote cannot be a representative quote, or
    None when it can."""
    quote = code.get("quote") if isinstance(code, dict) else None
    answer = str((quote or {}).get("answer") or "").strip() if isinstance(quote, dict) else ""
    if not answer:
        return "沒有整理後引言"
    if quote.get("question"):
        return "附了訪員問題，要對照問題才讀得懂"
    problem = _answer_problem(answer)
    if problem:
        return problem
    if not any(answer_kind.classify(evidence_by_id[ref]) == answer_kind.SUBSTANTIVE
               for ref in code.get("evidence_refs") or [] if isinstance((evidence_by_id or {}).get(ref), dict)):
        return "引用的證據沒有一則是受訪者自己的實質陳述"
    return None


def standalone_evidence_quote_problem(text: str, evidence: dict | None) -> str | None:
    """The same rule for a quotation that cites one evidence unit directly (a
    Persona's representative quotation)."""
    answer = str(text or "").strip()
    if not answer:
        return "沒有引言"
    problem = _answer_problem(answer)
    if problem:
        return problem
    if not isinstance(evidence, dict) or answer_kind.classify(evidence) != answer_kind.SUBSTANTIVE:
        return "引用的證據不是受訪者自己的實質陳述"
    return None


def quote_text(view: dict) -> str:
    """問：⋯　答：「⋯」— 說話者・定位"""
    speaker = view.get("role") or "說話者未記錄"
    source = f"{speaker}・{view['locator']}" if view.get("locator") else speaker
    answer = f"「{view['answer']}」— {source}"
    return f"問：{view['question']}　答：{answer}" if view.get("question") else answer


# ── review workbook (acceptance only; not a deliverable) ──────────────

REVIEW_HEADERS = ["段落", "行號範圍", "定位", "分析範圍", "簡述", "訪員問題", "完整原文", "受訪者原文", "證據", "開放編碼"]
_SHEET_FORBIDDEN = re.compile(r"[\[\]:*?/\\]")


def _sheet_name(source: str, used: set) -> str:
    base = _SHEET_FORBIDDEN.sub("_", Path(source).stem)[:28] or "逐字稿"
    name, n = base, 2
    while name in used:
        name = f"{base[:26]}_{n}"
        n += 1
    used.add(name)
    return name


def review_rows(doc: dict, open_coding: dict | None) -> list[tuple[str, list[list[str]]]]:
    codes_by_segment: dict[str, list[str]] = {}
    for concept in (open_coding or {}).get("concepts") or []:
        if isinstance(concept, dict):
            for ref in concept.get("segment_refs") or []:
                codes_by_segment.setdefault(ref, []).append(str(concept.get("name") or ""))
    sheets = []
    for transcript in doc.get("transcripts") or []:
        rows = []
        for s in transcript.get("segments") or []:
            rows.append([s["id"], f"第 {s['start_line']}–{s['end_line']} 行", s["locator"],
                         "範圍內" if s["in_scope"] else f"範圍外：{s.get('out_of_scope_reason', '')}",
                         s["summary"], s["question_passage"], s["passage"], s["respondent_passage"],
                         "、".join(s.get("evidence_refs") or []), "\n".join(codes_by_segment.get(s["id"], []))])
        sheets.append((transcript["source"], rows))
    return sheets


def render_review(doc: dict, open_coding: dict | None, target: str | Path) -> Path:
    try:
        import xlsxwriter
    except ImportError as exc:
        raise SegmentError("這台機器沒有安裝輸出 Excel 需要的 XlsxWriter。") from exc
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.stem}.", suffix=".tmp.xlsx", dir=target.parent)
    os.close(descriptor)
    widths = {"段落": 9, "行號範圍": 12, "定位": 22, "分析範圍": 14, "簡述": 36, "訪員問題": 36,
              "完整原文": 60, "受訪者原文": 50, "證據": 14, "開放編碼": 28}
    try:
        with xlsxwriter.Workbook(temporary, {"strings_to_formulas": False, "strings_to_urls": False}) as book:
            header = book.add_format({"bold": True, "bg_color": "#EAF0F4", "border": 1, "text_wrap": True, "valign": "top"})
            cell = book.add_format({"text_wrap": True, "valign": "top"})
            used: set = set()
            for source, rows in review_rows(doc, open_coding):
                sheet = book.add_worksheet(_sheet_name(source, used))
                sheet.freeze_panes(1, 0)
                for column, title in enumerate(REVIEW_HEADERS):
                    sheet.write_string(0, column, title, header)
                    sheet.set_column(column, column, widths[title], cell)
                for row_index, values in enumerate(rows, start=1):
                    for column, value in enumerate(values):
                        sheet.write_string(row_index, column, str(value), cell)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target
