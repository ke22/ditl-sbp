"""Re-split journey-grid tables in a lossless analysis input by stage column.

Some walkthrough PDFs hold a journey grid: stage headers across the top
(查詢資訊 … 事後追蹤) and Time / Emo / Desc rows down the left. A plain text
reflow can still run several narrow stage cells together, so one cell's
words end up interleaved with its neighbours'. This tool reads each word's
position from the source PDF (poppler `pdftotext -bbox`), assigns it to its
stage column, and rewrites only the Time/Emo/Desc band of that grid in the
analysis input.

It never rewrites text: a band is replaced only when the replaced span holds
exactly the same non-whitespace characters as the PDF band (layout markers
`[欄位 N]` excluded); otherwise that grid is left as it is and reported.

    python3 tools/journey_grid_reflow.py <analysis_input.md> <pdf_dir> <out.md>
"""
from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

STAGES = ("查詢資訊", "告知情", "告知情境", "前往櫃點", "等待服", "等待服務", "開始服務", "結束服務", "事後追蹤")
ROW_LABELS = ("Time", "Emo", "Desc")
MARKER = re.compile(r"\[欄位 \d+\]")
CJK = re.compile(r"[　-鿿＀-￯]")
_WORD = re.compile(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)</word>')
_PAGE = re.compile(r"<page [^>]*>(.*?)</page>", re.S)

Word = tuple  # (x_min, y_min, x_max, y_max, text)


def source_chars(text: str) -> Counter:
    """Non-whitespace source characters; layout markers are not content."""
    return Counter(ch for ch in MARKER.sub("", text) if not ch.isspace())


def pdf_pages(pdf: Path):
    html = subprocess.run(["pdftotext", "-bbox", str(pdf), "-"], capture_output=True, text=True, check=True).stdout
    for number, body in enumerate(_PAGE.findall(html), 1):
        yield number, [(float(a), float(b), float(c), float(d), w) for a, b, c, d, w in _WORD.findall(body)]


def _join_lines(lines: list[str]) -> str:
    out = ""
    for line in lines:
        if out and not (CJK.match(out[-1]) or CJK.match(line[0])):
            out += " "
        out += line
    return out


def _touchpoint_row(words: list[Word], time_y: float) -> tuple[Word, list[Word]] | None:
    """The `TP` touchpoint row just above the band: its label word and the
    words to its right (henglong v3, 2026-10-04)."""
    tp = next((w for w in words if w[4] == "TP" and time_y - 60 < w[1] < time_y), None)
    if tp is None:
        return None
    return tp, [w for w in words if tp[1] - 5 <= w[1] < time_y - 2 and w[0] > tp[2]]


def _column_anchors(row: list[Word]) -> list[Word]:
    """One pseudo-header per column: touchpoint words that overlap
    horizontally (a label wrapped onto a second line) belong together."""
    columns: list[list[float]] = []
    for w in sorted(row, key=lambda w: w[0]):
        if columns and w[0] <= columns[-1][2] + 2:
            columns[-1][2] = max(columns[-1][2], w[2])
        else:
            columns.append([w[0], w[1], w[2]])
    return [(x0, y, x1, y + 10, "") for x0, y, x1 in columns]


def _continuation(next_words: list[Word] | None, label_edge: float) -> list[Word]:
    """A grid that runs off the page continues at the top of the next page,
    in its own columns, up to the first line that starts in the left margin
    (henglong v3, 2026-10-04). Its y is moved below the band so it reads last."""
    if not next_words:
        return []
    body = [w for w in next_words if not w[4].startswith("DITLDESIGN") and not re.fullmatch(r"20\d\d", w[4])]
    stop = min((w[1] for w in body if w[0] < label_edge), default=10 ** 6)
    return [(w[0], w[1] + 10 ** 5, w[2], w[3] + 10 ** 5, w[4]) for w in body if w[1] < stop and w[0] >= label_edge]


def grid_band(words: list[Word], next_words: list[Word] | None = None) -> tuple[list[list[str]], list[Word]] | None:
    """(rows of cell text, the band's words) for a journey grid, else None.
    Rows are Time/Emo/Desc, preceded by the touchpoint row when the page has
    one; cell 0 is the row label, cell k the k-th stage. Columns come from the
    stage headers, or from the touchpoint labels when the headers are absent."""
    labels = {w[4]: w for w in words if w[4] in ROW_LABELS}
    if set(labels) != set(ROW_LABELS):
        return None
    headers = sorted((w for w in words if w[4] in STAGES), key=lambda w: w[0])
    touchpoints = _touchpoint_row(words, labels["Time"][1])
    if len(headers) < 5 and touchpoints and len(_column_anchors(touchpoints[1])) >= 5:
        headers = _column_anchors(touchpoints[1])
    if len(headers) < 5:
        return None
    # Cell text starts at the cell's left edge, left of its centred header, so
    # a word belongs to the first stage whose header has not ended before it.
    rights = [h[2] for h in headers]
    label_edge = headers[0][0] - 10
    desc_y = labels["Desc"][1]
    # The grid ends at the page footer or at the first line below it that
    # starts in the left margin (prose after the table), whichever is first.
    bottom = min((w[1] for w in words if w[4].startswith("DITLDESIGN")
                  or (w[1] > desc_y + 3 and w[0] < label_edge)), default=10 ** 6)
    tops = [labels["Time"][1] - 2, labels["Emo"][1] - 2, desc_y - 2, bottom - 2]
    if touchpoints:
        tops.insert(0, touchpoints[0][1] - 5)
    band = [w for w in words if tops[0] <= w[1] < tops[-1]]
    if bottom == 10 ** 6 or any(w[4].startswith("DITLDESIGN") and w[1] == bottom for w in words):
        # The grid reached the page foot: its cells may continue overleaf.
        overleaf = _continuation(next_words, label_edge)
        band += overleaf
        tops[-1] = max(tops[-1], max((w[1] for w in overleaf), default=0) + 1)
    rows = []
    for r in range(len(tops) - 1):
        cells: list[list[Word]] = [[] for _ in range(len(headers) + 1)]
        for w in band:
            if tops[r] <= w[1] < tops[r + 1]:
                column = 0 if w[0] < label_edge else 1 + sum(1 for right in rights[:-1] if right < w[0] - 2)
                cells[column].append(w)
        rendered = []
        for cell in cells:
            lines: list[list[Word]] = []
            for w in sorted(cell, key=lambda w: w[1]):
                if lines and abs(w[1] - lines[-1][0][1]) <= 5:
                    lines[-1].append(w)
                else:
                    lines.append([w])
            rendered.append(_join_lines([" ".join(x[4] for x in sorted(line, key=lambda x: x[0])) for line in lines]))
        rows.append(rendered)
    return rows, band


def render(rows: list[list[str]]) -> str:
    out: list[str] = []
    for cells in rows:
        out += [f"[欄位 {i}] {text}" for i, text in enumerate(cells, 1) if text]
        out.append("")
    return "\n".join(out).rstrip("\n")


def band_spans(lines: list[str]) -> list[tuple[int, int]]:
    """Each `[欄位 1] Time…` block up to the next blank line."""
    spans = []
    for i, line in enumerate(lines):
        if line.startswith("[欄位 1] Time"):
            j = i
            while j < len(lines) and lines[j].strip():
                j += 1
            spans.append((i, j))
    return spans


def character_windows(lines: list[str], chars: Counter, longest: int = 60) -> list[tuple[int, int]]:
    """Every run of whole lines, starting and ending on a non-blank line,
    whose source characters equal `chars` exactly."""
    total = sum(chars.values())
    found = []
    for i, first in enumerate(lines):
        if not first.strip():
            continue
        acc: Counter = Counter()
        for j in range(i, min(i + longest, len(lines))):
            acc += source_chars(lines[j])
            if sum(acc.values()) > total:
                break
            if lines[j].strip() and acc == chars:
                found.append((i, j + 1))
    return found


def reflow(text: str, grids: list[tuple[str, list[list[str]], list[Word]]]) -> tuple[str, list[str]]:
    """Replace each grid's band where exactly one span holds the same source
    characters. Returns (new text, one report line per grid)."""
    lines = text.split("\n")
    spans = band_spans(lines)
    replacements, report = {}, []
    for name, rows, band in grids:
        chars = source_chars("".join(w[4] for w in band))
        match = [s for s in spans if source_chars("\n".join(lines[s[0]:s[1]])) == chars]
        if not match:
            # henglong v3: the band may not start with `[欄位 1] Time` (touchpoint
            # labels run into it); find the one window with the same characters.
            match = character_windows(lines, chars)
        if len(match) == 1 and match[0] not in replacements:
            replacements[match[0]] = render(rows)
            report.append(f"{name}: replaced lines {match[0][0] + 1}-{match[0][1]}")
        else:
            report.append(f"{name}: left as is ({len(match)} span(s) with the same characters)")
    out, i = [], 0
    while i < len(lines):
        span = next((s for s in replacements if s[0] == i), None)
        if span:
            out.append(replacements[span])
            i = span[1]
        else:
            out.append(lines[i])
            i += 1
    new = "\n".join(out)
    if source_chars(new) != source_chars(text):
        raise ValueError("reflow changed source characters")
    return new, report


def main(argv: list[str]) -> int:
    source, pdf_dir, out = Path(argv[0]), Path(argv[1]), Path(argv[2])
    grids = []
    for pdf in sorted(pdf_dir.glob("*.pdf")):
        pages = list(pdf_pages(pdf))
        for index, (number, words) in enumerate(pages):
            following = pages[index + 1][1] if index + 1 < len(pages) else None
            found = grid_band(words, following)
            if found:
                grids.append((f"{pdf.name} p.{number}", *found))
    new, report = reflow(source.read_text(encoding="utf-8"), grids)
    out.write_text(new, encoding="utf-8")
    print("\n".join(report))
    print(f"wrote {out}; source characters unchanged (layout markers excluded)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
