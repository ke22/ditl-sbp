"""purpose-aware-research-calibration: read-only audits of existing runs and the
controlled rerun wrapper. Audits never call a model and never write into a run."""
from __future__ import annotations

from pathlib import Path

AFTER_HEADING = "## After（受控重跑）"


def write_report(path: Path, text: str) -> None:
    """Write an audit report, keeping the controlled-rerun section if one was
    already appended, so regenerating the before table never erases the after."""
    path = Path(path)
    if path.is_file():
        old = path.read_text(encoding="utf-8")
        if AFTER_HEADING in old:
            text = text.rstrip("\n") + "\n\n" + old[old.index(AFTER_HEADING):]
    path.write_text(text, encoding="utf-8")


def replace_after_section(path: Path, body: str) -> None:
    """Replace (or append) the controlled-rerun section of a report."""
    path = Path(path)
    old = path.read_text(encoding="utf-8") if path.is_file() else ""
    head = old[:old.index(AFTER_HEADING)].rstrip("\n") if AFTER_HEADING in old else old.rstrip("\n")
    path.write_text((head + "\n\n" if head else "") + AFTER_HEADING + "\n\n" + body.rstrip("\n") + "\n", encoding="utf-8")
