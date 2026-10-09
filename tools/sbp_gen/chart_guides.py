"""Chart guides (導讀) for the Persona, customer journey and service blueprint
stations (2026-10-02 researcher ruling, change add-chart-guides).

A guide is a short reading of one chart: a summary paragraph and a few points.
AI drafts it on the researcher's request from a fixed, read-only projection of
that station's content, citing only evidence ids found in the projection; the
researcher edits it in place. The screen shows no sources; this module keeps
the AI draft and its evidence ids in ``chart_guides.yaml`` beside the
researcher's current text.

The station's own files are never written here.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import yaml

KINDS = ("persona", "journey", "blueprint")
BLOCKS = ("summary", "points")
GUIDES_FILE = "chart_guides.yaml"

# One table for the prompt and the validator. The prompt asks for `ask`; the
# validator accepts `accept` (the asked range widened by a fifth, because the
# model cannot count Chinese characters exactly) and exactly `points` points.
LENGTH_RULES = {
    "persona": {"ask": (100, 150), "accept": (80, 180), "points": (3, 5)},
    "journey": {"ask": (120, 180), "accept": (96, 216), "points": (3, 3)},
    "blueprint": {"ask": (120, 180), "accept": (96, 216), "points": (3, 5)},
}
POINT_MAX_CHARS = 60


class GuideError(ValueError):
    pass


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None


def _chars(text: str) -> int:
    return sum(1 for ch in str(text or "") if not ch.isspace())


def _refs(*groups) -> list[str]:
    out: list[str] = []
    for group in groups:
        for ref in group or []:
            if isinstance(ref, str) and ref and ref not in out:
                out.append(ref)
    return out


# ── input projections ────────────────────────────────────────────────────

_PERSONA_FIELDS = ("description", "context", "motivation_goal", "decision_pattern",
                   "behavior_strategy", "challenge", "need", "rationale")


def _persona_projection(run_dir: Path) -> dict | None:
    content = _load(run_dir / "persona_content.yaml") or {}
    personas = [p for p in content.get("personas") or [] if isinstance(p, dict)]
    if not personas:
        return None
    persona = personas[0]
    provenance = {item.get("field"): item.get("evidence_refs") or []
                  for item in persona.get("field_provenance") or [] if isinstance(item, dict)}
    quote = persona.get("representative_quote") or {}
    matrix = _load(run_dir / "persona_matrix.yaml") or {}
    axes_by_id = {a.get("id"): a for a in matrix.get("axes") or [] if isinstance(a, dict)}
    picked = (_load(run_dir / "segmentation_axis_selection.yaml") or {}).get("axis_ids") or []
    target = _load(run_dir / "target_segmentation_selection.yaml") or {}
    return {
        "persona": {
            "label": persona.get("label"),
            "fields": [{"field": name, "text": persona.get(name), "evidence_refs": list(provenance.get(name) or [])}
                       for name in _PERSONA_FIELDS if persona.get(name)],
            "representative_quote": {"text": quote.get("text"), "evidence_refs": _refs([quote.get("evidence_ref")])}
            if quote.get("text") else None,
        },
        "matrix": {
            "axes": [{"label": axes_by_id[a].get("label"), "pole_low": axes_by_id[a].get("pole_low"),
                      "pole_high": axes_by_id[a].get("pole_high")} for a in picked if a in axes_by_id],
            "chosen_quadrant": target.get("primary_quadrant_id") or persona.get("quadrant_id"),
        },
    }


def _journey_document(run_dir: Path) -> dict:
    # The customer journey station reads the editable proposal when there is one.
    return _load(run_dir / "journey_proposal.yaml") or _load(run_dir / "journey.yaml") or {}


def _primary_steps(document: dict) -> list[dict]:
    scenarios = [s for s in document.get("scenarios") or [] if isinstance(s, dict)]
    primary = next((s for s in scenarios if s.get("id") == document.get("primary_scenario")),
                   scenarios[0] if scenarios else {})
    return [s for s in primary.get("steps") or [] if isinstance(s, dict)]


def _adopted_solutions(step: dict) -> list[dict]:
    chosen = set(step.get("selected_solution_ids") or [])
    return [c for c in step.get("solution_candidates") or [] if isinstance(c, dict) and c.get("id") in chosen]


def _journey_projection(run_dir: Path) -> dict | None:
    steps = _primary_steps(_journey_document(run_dir))
    if not steps:
        return None
    out = []
    for step in steps:
        satisfaction = [s for s in step.get("satisfaction") or [] if isinstance(s, dict)]
        solutions = _adopted_solutions(step)
        out.append({
            "label": step.get("label"),
            "pain_points": [p for p in step.get("pain_points") or [] if isinstance(p, str)],
            "emotion": step.get("emotion") if isinstance(step.get("emotion"), int) else 3,
            "satisfaction": [s.get("evaluation") for s in satisfaction if s.get("evaluation")],
            "chosen_for_solution": bool(step.get("develop_solution")),
            "solutions": [{"text": c.get("text"),
                           "channels": {row: [i.get("text") for i in items if isinstance(i, dict)]
                                        for row, items in (c.get("delivery") or {}).items() if items}}
                          for c in solutions],
            "evidence_refs": _refs(step.get("evidence_refs"), *(s.get("evidence_refs") for s in satisfaction),
                                   *(c.get("evidence_refs") for c in solutions)),
        })
    return {"steps": out}


def _blueprint_projection(run_dir: Path) -> dict | None:
    graph = _load(run_dir / "service_graph.yaml") or {}
    nodes = [n for n in graph.get("nodes") or [] if isinstance(n, dict)]
    if not nodes:
        return None
    adopted = []
    for step in _primary_steps(_journey_document(run_dir)):
        for candidate in _adopted_solutions(step):
            adopted.append({"step": step.get("label"), "text": candidate.get("text"),
                            "evidence_refs": _refs(candidate.get("evidence_refs"))})
    return {
        "nodes": [{"label": n.get("label"), "layer": n.get("layer"), "step": n.get("step"),
                   "evidence_refs": _refs([c.get("evidence_ref") for c in n.get("claims") or [] if isinstance(c, dict)],
                                          n.get("evidence_refs"))}
                  for n in nodes],
        "adopted_solutions": adopted,
    }


_PROJECTIONS = {"persona": _persona_projection, "journey": _journey_projection, "blueprint": _blueprint_projection}


def input_projection(run_dir: str | Path, kind: str) -> dict | None:
    """The read-only content a guide of this kind is drafted from, or None
    when the station has nothing to read yet."""
    if kind not in KINDS:
        raise GuideError(f"不認得這種導讀：{kind}")
    return _PROJECTIONS[kind](Path(run_dir))


def projection_digest(projection: dict) -> str:
    raw = json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def allowed_evidence(projection: dict) -> set[str]:
    found: set[str] = set()

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "evidence_refs" and isinstance(item, list):
                    found.update(ref for ref in item if isinstance(ref, str))
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(projection)
    return found


# ── validation ──────────────────────────────────────────────────────────

def guide_errors(draft: object, projection: dict, kind: str) -> list[str]:
    """Rules beyond the schema: evidence only from the projection, every
    sentence and point cited, lengths and point counts from LENGTH_RULES."""
    if not isinstance(draft, dict):
        return ["[chart_guides] 回覆要是一個物件"]
    rules, allowed, errs = LENGTH_RULES[kind], allowed_evidence(projection), []
    sentences = [s for s in draft.get("summary_sentences") or [] if isinstance(s, dict)]
    points = [p for p in draft.get("points") or [] if isinstance(p, dict)]
    for label, items, key in (("摘要", sentences, "sentence"), ("重點", points, "point")):
        for index, item in enumerate(items, 1):
            refs = item.get("evidence_refs") or []
            if not refs:
                errs.append(f"[chart_guides] {label}第 {index} {'句' if key == 'sentence' else '點'}沒有引用 evidence id")
            for ref in refs:
                if ref not in allowed:
                    errs.append(f"[chart_guides] {label}第 {index} {'句' if key == 'sentence' else '點'}引用的 {ref} 不在這張圖的內容裡")
    length = _chars("".join(str(s.get("sentence") or "") for s in sentences))
    low, high = rules["accept"]
    if not low <= length <= high:
        errs.append(f"[chart_guides] 摘要 {length} 字，要在 {rules['ask'][0]}–{rules['ask'][1]} 字左右（接受 {low}–{high}）")
    fewest, most = rules["points"]
    if not fewest <= len(points) <= most:
        wanted = f"恰好 {fewest}" if fewest == most else f"{fewest}–{most}"
        errs.append(f"[chart_guides] 重點有 {len(points)} 點，要 {wanted} 點")
    for index, item in enumerate(points, 1):
        if _chars(item.get("point")) > POINT_MAX_CHARS:
            errs.append(f"[chart_guides] 重點第 {index} 點超過 {POINT_MAX_CHARS} 字")
    return errs


# ── document operations ─────────────────────────────────────────────────

def load_document(run_dir: str | Path) -> dict:
    document = _load(Path(run_dir) / GUIDES_FILE) or {}
    return {"schema_version": 1, "guides": dict(document.get("guides") or {})}


def write_document(run_dir: str | Path, document: dict) -> Path:
    path = Path(run_dir) / GUIDES_FILE
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(path)
    return path


def merge_draft(document: dict, kind: str, draft: dict, digest: str, now: str) -> dict:
    """Write a fresh draft into ``kind``; a block the researcher wrote keeps
    the researcher's text (its ai_draft is refreshed underneath)."""
    merged = copy.deepcopy(document)
    guides = merged.setdefault("guides", {})
    current = guides.get(kind) or {}
    sentences = [{"sentence": s["sentence"].strip(), "evidence_refs": list(s["evidence_refs"])}
                 for s in draft["summary_sentences"]]
    points = [{"point": p["point"].strip(), "evidence_refs": list(p["evidence_refs"])} for p in draft["points"]]
    fresh = {
        "summary": {"text": "".join(s["sentence"] for s in sentences), "source": "ai-draft", "updated_at": now,
                    "ai_draft": sentences, "basis": draft["basis"]},
        "points": {"text": [p["point"] for p in points], "source": "ai-draft", "updated_at": now,
                   "ai_draft": points, "basis": draft["basis"]},
    }
    guide = {}
    for block in BLOCKS:
        old = current.get(block) or {}
        if old.get("source") == "researcher":
            guide[block] = {**old, "ai_draft": fresh[block]["ai_draft"], "basis": fresh[block]["basis"]}
        else:
            guide[block] = fresh[block]
    guide["drafted_from"] = digest
    guide["drafted_at"] = now
    guides[kind] = guide
    return merged


def apply_edit(document: dict, kind: str, block: str, text, now: str, current_digest: str | None = None) -> dict:
    """The researcher's words for one block. Saving real text makes the guide
    current (`drafted_from` becomes `current_digest`): the researcher has read
    the chart as it is now (2026-10-02 ruling). An empty block is handed back
    to AI for the next draft and leaves `drafted_from` alone."""
    if kind not in KINDS or block not in BLOCKS:
        raise GuideError("不認得要修改的導讀。")
    edited = copy.deepcopy(document)
    guide = (edited.get("guides") or {}).get(kind)
    if not guide:
        raise GuideError("這段導讀還沒有起草。")
    if block == "summary":
        value = str(text or "").strip()
        empty = not value
    else:
        items = text if isinstance(text, list) else str(text or "").split("\n")
        value = [str(item).strip() for item in items if str(item).strip()]
        empty = not value
    entry = dict(guide[block])
    entry["text"] = value if not empty else ("" if block == "summary" else [])
    entry["source"] = "ai-draft" if empty else "researcher"
    entry["updated_at"] = now
    guide[block] = entry
    if not empty and current_digest:
        guide["drafted_from"] = current_digest
    return edited


def guide_view(run_dir: str | Path, kind: str) -> dict:
    """What a station shows: the current text, and whether the chart changed
    since the guide was drafted. No sources."""
    projection = input_projection(run_dir, kind)
    guide = load_document(run_dir)["guides"].get(kind)
    if not guide:
        return {"kind": kind, "available": projection is not None, "guide": None, "stale": False}
    stale = projection is None or projection_digest(projection) != guide.get("drafted_from")
    return {"kind": kind, "available": projection is not None, "stale": stale,
            "guide": {"summary": guide["summary"]["text"], "points": list(guide["points"]["text"])}}
