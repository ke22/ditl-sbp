"""Rejoin a two-column table cell that a text reflow split across rows.

In some walkthrough PDFs the record column (欄位 2) of a question table is
one tall cell that spans several question rows. A plain reflow emits it in
pieces, each paired with the next question row, so a sentence is cut:

    [欄位 1] 回收規格 回饋點數 活動目的
    [欄位 2] 看頁面知道可以回收。 整體頁面資訊清楚，個 人會希望需要的資訊要

    [欄位 1] 請問您目前所持有的 honeywell
    [欄位 2] 網上找。會希望理解理

A piece is joined to the next one only when (1) it does not end a sentence
and (2) the source PDF shows its last line as a wrapped line (at least four
characters, ending at most about one character short of the column's right edge) directly followed, in the same
column, by the next piece's first line (same page, or the foot of one page
and the top of the next). The question lines stay where they are. Nothing
is rewritten: the file's non-whitespace characters are checked unchanged.

    python3 tools/table_cell_rejoin.py <analysis_input.md> <pdf_dir> <out.md>
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from journey_grid_reflow import pdf_pages, source_chars

SENTENCE_END = tuple("。！？!?）」』…")
CELL1, CELL2 = "[欄位 1] ", "[欄位 2] "
DECLARATION = re.compile(r"^- source: `([^`\n]+)`[ \t]*$")
NEXT_LINE_MAX, SAME_COLUMN = 23.0, 15.0  # wrapped lines sit ~19-21pt apart; a new cell adds padding (28+)


SHORT_BY_CHARS = 1.2


def _wrapped(n: int, wa, words) -> bool:
    """`wa` reaches its column's right edge (or stops about one character
    short, as when the next line must not start with punctuation), so the
    text was wrapped, not ended."""
    column = [w for m, w in words if m == n and abs(w[0] - wa[0]) <= SAME_COLUMN]
    right = max(w[2] for w in column)
    char = (wa[2] - wa[0]) / max(1, len(wa[4]))
    return right - wa[2] <= SHORT_BY_CHARS * char


def consecutive_lines(pages: list[tuple[int, list]], a: str, b: str) -> bool:
    """Is a PDF word `a` a wrapped line directly followed, in its column, by
    a word `b`? Short tokens (scores, single words) never qualify."""
    if len(a) < 4 or len(b) < 2:
        return False
    words = [(n, w) for n, ws in pages for w in ws]
    for n, wa in words:
        if wa[4] != a or not _wrapped(n, wa, words):
            continue
        for m, wb in words:
            if wb[4] != b or abs(wb[0] - wa[0]) > SAME_COLUMN:
                continue
            if m == n and 0 < wb[1] - wa[1] <= NEXT_LINE_MAX:
                return True
            if m == n + 1 and wa[1] > 600 and wb[1] < 150:
                return True
    return False


CELL = re.compile(r"^\[欄位 (\d+)\] ")


def _blocks(lines: list[str]) -> list[list[int]]:
    """Line indexes of each run of non-blank lines."""
    blocks, current = [], []
    for k, line in enumerate(lines):
        if line.strip():
            current.append(k)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _table(lines: list[str], block: list[int]) -> bool:
    return lines[block[0]].startswith(CELL1) and all(CELL.match(lines[k]) for k in block)


def rejoin(text: str, pages_by_source: dict[str, list]) -> tuple[str, list[str]]:
    """Join a record piece (a block's last cell, column 2 or later) to the
    cell in a following table block that the PDF shows as its wrapped
    continuation. A block holding only a question row does not end the cell."""
    lines = text.split("\n")
    sources: dict[int, str] = {}
    current = None
    for k, line in enumerate(lines):
        match = DECLARATION.match(line)
        if match:
            current = match.group(1)
        sources[k] = current
    blocks = _blocks(lines)
    report: list[str] = []
    drop: set[int] = set()
    b = 0
    while b < len(blocks):
        block = blocks[b]
        pages = pages_by_source.get(sources[block[0]] or "")
        last = block[-1]
        if not (pages and _table(lines, block) and len(block) > 1 and int(CELL.match(lines[last]).group(1)) >= 2):
            b += 1
            continue
        head, c = last, b + 1
        while c < len(blocks) and _table(lines, blocks[c]):
            piece = lines[head].rstrip()
            if piece.endswith(SENTENCE_END):
                break
            later = [k for k in blocks[c][1:] if k not in drop]
            if not later:
                c += 1  # a question row with no record piece: the cell runs past it
                continue
            follow = next((k for k in later if consecutive_lines(
                pages, piece.split()[-1], CELL.sub("", lines[k]).split()[0] if CELL.sub("", lines[k]).split() else "")),
                None)
            if follow is None:
                break
            lines[head] = f"{piece} {CELL.sub('', lines[follow]).strip()}"
            drop.add(follow)
            report.append(f"{sources[head]}: joined line {follow + 1} onto line {head + 1}")
            if follow != blocks[c][-1]:
                break  # the cell ended inside this block
            c += 1
        b += 1
    new = "\n".join(line for k, line in enumerate(lines) if k not in drop)
    if source_chars(new) != source_chars(text):
        raise ValueError("rejoin changed source characters")
    return new, report


def main(argv: list[str]) -> int:
    source, pdf_dir, out = Path(argv[0]), Path(argv[1]), Path(argv[2])
    pages = {pdf.name: list(pdf_pages(pdf)) for pdf in sorted(pdf_dir.glob("*.pdf"))}
    new, report = rejoin(source.read_text(encoding="utf-8"), pages)
    out.write_text(new, encoding="utf-8")
    print("\n".join(report) or "nothing joined")
    print(f"wrote {out}; source characters unchanged (layout markers excluded)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
