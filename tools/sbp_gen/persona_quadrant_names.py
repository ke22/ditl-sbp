"""persona-matrix-comparison-first (2026-10-22 researcher ruling): names for
the four cells of every pair of Persona Matrix axes.

The AI drafts, in one call, a short name and one line for each cell of every
axis pair, so switching axes never waits; the researcher can rewrite any of
them. Only persona_quadrant_names.yaml is written. A pair is stored once
whichever axis the researcher puts first: its key is the two axis ids in
sorted order, and each cell says which end of each axis it is. A cell the
researcher rewrote keeps the researcher's words when the AI drafts again, and
the AI's draft is kept beside it. A cell with nobody in it is named from the
two axis ends only, and is recorded as such.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import re
from pathlib import Path

import yaml

FILE = "persona_quadrant_names.yaml"
NAME_MAX_CHARS = 10          # the researcher's own wording
DESCRIPTION_MAX_CHARS = 40
# 2026-10-22 researcher ruling: an AI name is a persona role in the shape of
# 質感避難者／效率追求者, and its line says what these people want.
DRAFT_NAME_MAX_CHARS = 6
DRAFT_NAME_ENDINGS = ("者", "族", "派", "客")
DRAFT_DESCRIPTION_MAX_CHARS = 30
DRAFT_DESCRIPTION_OPENINGS = ("想", "視", "期待")
HEDGES = ("若有人", "可能")
BASES = ("members", "poles_only")


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None


def ordered(axis_ids) -> list[str]:
    return sorted(axis_ids, key=lambda a: (len(a), a))


def pair_key(axis_ids) -> str:
    return "|".join(ordered(axis_ids))


def _matrix(run_dir: Path) -> dict | None:
    matrix = _load(run_dir / "persona_matrix.yaml")
    return matrix if isinstance(matrix, dict) else None


def all_pairs(run_dir: str | Path) -> list[list[str]]:
    """Every pair of axes that have both ends, in stored (sorted) order."""
    matrix = _matrix(Path(run_dir)) or {}
    ids = [a.get("id") for a in matrix.get("axes") or [] if isinstance(a, dict) and a.get("pole_low") and a.get("pole_high")]
    return [ordered(p) for p in itertools.combinations(ordered(ids), 2)]


def _cells(matrix: dict, axis_ids: list[str]) -> list[dict]:
    from . import stages
    quadrants, _ = stages.assign_target_segmentation_quadrants(matrix, list(axis_ids))
    return [{"quadrant_id": q.get("id"),
             "poles": {axis_ids[0]: q.get("axis_1_pole"), axis_ids[1]: q.get("axis_2_pole")},
             "members": list(q.get("suggested_subject_ids") or [])} for q in quadrants or []]


def input_projection(run_dir: str | Path, axis_ids: list[str]) -> dict | None:
    """One pair as the drafting call and the staleness check see it: the two
    axes, each cell's two ends, and its members' placements on these axes."""
    run_dir = Path(run_dir)
    matrix = _matrix(run_dir)
    if matrix is None or len(axis_ids or []) != 2:
        return None
    axis_ids = ordered(axis_ids)
    axes = {a.get("id"): a for a in matrix.get("axes") or [] if isinstance(a, dict)}
    if any(a not in axes for a in axis_ids):
        return None
    subjects = {s.get("id"): s for s in matrix.get("subjects") or [] if isinstance(s, dict)}

    def rationale(sid, aid):
        for p in (subjects.get(sid) or {}).get("placements") or []:
            if p.get("axis_id") == aid:
                return p.get("rationale")
        return None

    def end(aid, pole):
        return axes[aid].get("pole_high") if pole == "near_high" else axes[aid].get("pole_low")

    return {
        "axis_ids": axis_ids,
        "axes": [{k: axes[a].get(k) for k in ("label", "pole_low", "pole_high", "axis_type", "design_implication")
                  if axes[a].get(k)} for a in axis_ids],
        "quadrants": [{
            "quadrant_id": c["quadrant_id"],
            "ends": [end(a, c["poles"][a]) for a in axis_ids],
            "members": [{"subject": sid, "why": [rationale(sid, a) for a in axis_ids]} for sid in c["members"]],
        } for c in _cells(matrix, axis_ids)],
    }


def projection_digest(projection: dict) -> str:
    return hashlib.sha256(json.dumps(projection, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def draft_errors(draft: object, projection: dict) -> list[str]:
    """One pair's cells: one name each; a cell with members is named from
    them, an empty one from the axis ends only; no internal ids."""
    import internal_id_patterns
    # Not \b-bounded: an id written straight after Chinese text has no word
    # boundary (the shared \b pattern misses "名稱P-1").
    id_in_text = re.compile(rf"(?<![A-Za-z0-9]){internal_id_patterns.ID_SHAPE}(?![0-9])")
    label = "＋".join(a.get("label") or "" for a in projection["axes"])
    if not isinstance(draft, dict) or not isinstance(draft.get("quadrants"), list):
        return [f"[quadrant_names] {label}：要輸出 quadrants 陣列"]
    expected = {q["quadrant_id"]: bool(q["members"]) for q in projection["quadrants"]}
    errors, seen = [], []
    for item in draft["quadrants"]:
        if not isinstance(item, dict):
            continue
        qid = item.get("quadrant_id")
        seen.append(qid)
        if qid not in expected:
            errors.append(f"[quadrant_names] {label}：不認得這一格 {qid}")
            continue
        name, line = str(item.get("name") or "").strip(), str(item.get("description") or "").strip()
        if not name or len(name) > DRAFT_NAME_MAX_CHARS or not name.endswith(DRAFT_NAME_ENDINGS):
            errors.append(f"[quadrant_names] {label} {qid} 的名稱「{name}」要是一個角色名稱，{DRAFT_NAME_MAX_CHARS} 字以內，"
                          f"以{'、'.join(DRAFT_NAME_ENDINGS)}其中一字結尾，例如「質感避難者」")
        if not line or len(line) > DRAFT_DESCRIPTION_MAX_CHARS or not line.startswith(DRAFT_DESCRIPTION_OPENINGS):
            errors.append(f"[quadrant_names] {label} {qid} 的描述要以{'、'.join(DRAFT_DESCRIPTION_OPENINGS)}開頭，"
                          f"{DRAFT_DESCRIPTION_MAX_CHARS} 字以內，講這群人想要什麼")
        elif any(h in line for h in HEDGES):
            errors.append(f"[quadrant_names] {label} {qid} 的描述不要寫推測語氣（{'、'.join(HEDGES)}）")
        want = "members" if expected[qid] else "poles_only"
        if item.get("basis") != want:
            errors.append(f"[quadrant_names] {label} {qid} "
                          + ("有受訪者，要依他們的落點命名（basis: members）" if expected[qid]
                             else "沒有受訪者，只能依兩端命名（basis: poles_only）"))
        if id_in_text.search(name) or id_in_text.search(line):
            errors.append(f"[quadrant_names] {label} {qid} 的名稱或描述寫了內部編號")
    for qid in expected:
        if seen.count(qid) != 1:
            errors.append(f"[quadrant_names] {label} {qid} 要恰好有一筆（目前 {seen.count(qid)} 筆）")
    return errors


def drafts_errors(drafts: object, projections: list[dict]) -> list[str]:
    """The whole answer: one entry per requested pair, each passing draft_errors."""
    if not isinstance(drafts, dict) or not isinstance(drafts.get("pairs"), list):
        return ["[quadrant_names] 要輸出 pairs 陣列"]
    by_key = {}
    for entry in drafts["pairs"]:
        if isinstance(entry, dict) and isinstance(entry.get("axis_ids"), list) and len(entry["axis_ids"]) == 2:
            by_key.setdefault(pair_key(entry["axis_ids"]), []).append(entry)
    errors = []
    for projection in projections:
        key = pair_key(projection["axis_ids"])
        found = by_key.pop(key, [])
        if len(found) != 1:
            errors.append(f"[quadrant_names] 兩條軸 {key} 要恰好有一筆（目前 {len(found)} 筆）")
            continue
        errors += draft_errors(found[0], projection)
    for key in by_key:
        errors.append(f"[quadrant_names] 沒有要求這組軸：{key}")
    return errors


def load_document(run_dir: str | Path) -> dict:
    doc = _load(Path(run_dir) / FILE)
    return doc if isinstance(doc, dict) else {"schema_version": 1, "pairs": {}}


def write_document(run_dir: str | Path, document: dict) -> Path:
    path = Path(run_dir) / FILE
    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def merge_draft(document: dict, axis_ids: list[str], draft: dict, digest: str, now: str) -> dict:
    """One pair's AI names replace earlier AI names; a cell the researcher
    named keeps the researcher's words and records the new AI draft beside them."""
    pairs = dict(document.get("pairs") or {})
    entry = dict(pairs.get(pair_key(axis_ids)) or {})
    cells = dict(entry.get("quadrants") or {})
    for item in draft.get("quadrants") or []:
        qid = item["quadrant_id"]
        ai = {"ai_name": item["name"].strip(), "ai_description": item["description"].strip(), "basis": item["basis"]}
        current = cells.get(qid) or {}
        if current.get("source") == "researcher":
            cells[qid] = {**current, **ai}
        else:
            cells[qid] = {"name": ai["ai_name"], "description": ai["ai_description"], "source": "ai-draft", **ai}
    pairs[pair_key(axis_ids)] = {"digest": digest, "drafted_at": now, "quadrants": cells}
    return {"schema_version": 1, "pairs": pairs}


def apply_edit(document: dict, axis_ids: list[str], quadrant_id: str, name, description, now: str) -> dict:
    """The researcher's name and line for one cell."""
    name, description = str(name or "").strip(), str(description or "").strip()
    if not name or len(name) > NAME_MAX_CHARS:
        raise ValueError(f"名稱要寫，{NAME_MAX_CHARS} 字以內。")
    if len(description) > DESCRIPTION_MAX_CHARS:
        raise ValueError(f"描述請寫在 {DESCRIPTION_MAX_CHARS} 字以內。")
    if quadrant_id not in {f"QUADRANT-{n}" for n in range(1, 5)}:
        raise ValueError("不認得這一格。")
    pairs = dict(document.get("pairs") or {})
    entry = dict(pairs.get(pair_key(axis_ids)) or {})
    cells = dict(entry.get("quadrants") or {})
    cells[quadrant_id] = {**(cells.get(quadrant_id) or {}), "name": name, "description": description,
                          "source": "researcher", "edited_at": now}
    pairs[pair_key(axis_ids)] = {**entry, "quadrants": cells}
    return {"schema_version": 1, "pairs": pairs}


def missing_pairs(run_dir: str | Path) -> list[list[str]]:
    stored = load_document(run_dir).get("pairs") or {}
    return [p for p in all_pairs(run_dir) if pair_key(p) not in stored]


def view(run_dir: str | Path) -> dict:
    """Every named pair, keyed by its sorted axis ids: each cell with the end
    of each axis it stands for, and whether the matrix changed since drafting."""
    run_dir = Path(run_dir)
    matrix = _matrix(run_dir)
    out = {}
    for key, entry in (load_document(run_dir).get("pairs") or {}).items():
        axis_ids = key.split("|")
        if key != pair_key(axis_ids):
            # Stored in the order the axes were ticked, before pairs were kept
            # once in sorted order: its cell numbers follow that older order.
            continue
        projection = input_projection(run_dir, axis_ids)
        if projection is None or matrix is None:
            continue
        poles = {c["quadrant_id"]: c["poles"] for c in _cells(matrix, axis_ids)}
        out[key] = {
            "state": "current" if entry.get("digest") == projection_digest(projection) else "stale",
            "quadrants": {qid: {**cell, "poles": poles.get(qid)} for qid, cell in (entry.get("quadrants") or {}).items()},
        }
    return out


def cell_name(run_dir: str | Path, axis_ids: list[str], poles: dict) -> str | None:
    """The current name of the cell at these axis ends, or None."""
    entry = view(run_dir).get(pair_key(axis_ids)) or {}
    for cell in (entry.get("quadrants") or {}).values():
        if cell.get("poles") == poles and cell.get("name"):
            return cell["name"]
    return None
