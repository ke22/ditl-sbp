"""Record a researcher's backlog decisions into axial coding output.

Real runs leave roughly half their evidence in no category at all (measured
51% on `2026-08-21-manual-transport-chongzheng`), and until now nothing let a
researcher act on that — the categorized half looked complete because the
uncategorized half was invisible. This module is the write path for the three
actions the backlog offers: assign a record into an existing category, start
a new category from it, or exclude it.

Same shape as `tools/sbp_gen/researcher_edits.py`: every function takes an
already-loaded document, `copy.deepcopy()`s it, mutates the copy, and returns
it — never writes a file, never called with I/O. Assign and create-category
operate on `axial_coding.yaml`'s own document, using fields that schema
already defines (a new category's `discriminating_basis` is exactly the
researcher's own stated reason — that field's whole purpose is "why these
evidence units belong together"). Exclude operates on a separate small
adjustments document instead of `axial_coding.yaml`, because
`schema/axial-coding.schema.json` is `additionalProperties: false` and
recording an exclusion should not require changing that schema.
"""
from __future__ import annotations

import copy

# Same exception `researcher_edits.py` already defines and `tools/sbp_review/
# server.py` already aliases and catches everywhere -- reusing it here means
# a caller handling both graph judgements and axial-coding judgements only
# ever needs one except clause, not two classes with the same name and shape.
from .researcher_edits import JudgementRejected


def _category(doc: dict, category_id: str) -> dict:
    for category in doc.get("categories") or []:
        if category.get("id") == category_id:
            return category
    raise JudgementRejected(f"這個案子沒有這個類別（{category_id}）")


def assign_evidence(doc: dict, category_id: str, evidence_ref: str) -> dict:
    """Add `evidence_ref` to an existing category's `evidence_refs`.

    Idempotent: assigning a ref already present in the category is a no-op,
    not an error — a researcher retrying after an uncertain network response
    should not be punished for it.
    """
    result = copy.deepcopy(doc)
    category = _category(result, category_id)
    refs = category.setdefault("evidence_refs", [])
    if evidence_ref not in refs:
        refs.append(evidence_ref)
    return result


def _next_category_id(doc: dict) -> str:
    # Real runs disagree on padding (muji-full: CAT-01..CAT-09; chongzheng:
    # CAT-1..CAT-6) -- ids never reach visible text either way, so this picks
    # the next unused plain integer past the highest one already used,
    # rather than assuming any particular width.
    highest = 0
    for category in doc.get("categories") or []:
        cid = str(category.get("id") or "")
        if cid.startswith("CAT-") and cid[4:].isdigit():
            highest = max(highest, int(cid[4:]))
    return f"CAT-{highest + 1}"


def create_category(doc: dict, label: str, rationale: str, evidence_ref: str) -> dict:
    """Start a new category from one backlog record.

    `label` and `rationale` are both required — a category with no stated
    reason is exactly the thing `schema/axial-coding.schema.json`'s
    `discriminating_basis` field exists to prevent (CLAUDE.md「不要把推論寫得
    像事實」applies here too: an unreasoned category is a guess dressed as a
    finding).
    """
    if not label or not label.strip():
        raise JudgementRejected("新類別需要一個主題名稱")
    if not rationale or not rationale.strip():
        raise JudgementRejected("新類別需要一句理由，說明為什麼這則證據自成一類")
    result = copy.deepcopy(doc)
    result.setdefault("categories", []).append({
        "id": _next_category_id(result),
        "label": label.strip(),
        "discriminating_basis": rationale.strip(),
        "evidence_refs": [evidence_ref],
    })
    return result


def exclude_evidence(adjustments_doc: dict, evidence_ref: str) -> dict:
    """Record one evidence ref as excluded from the backlog.

    Operates on the adjustments document, not `axial_coding.yaml` — excluding
    a record is a statement that it doesn't belong anywhere, not a judgement
    about any category's content, so no category's `evidence_refs` changes.
    """
    result = copy.deepcopy(adjustments_doc)
    refs = result.setdefault("excluded_evidence_refs", [])
    if evidence_ref not in refs:
        refs.append(evidence_ref)
    return result
