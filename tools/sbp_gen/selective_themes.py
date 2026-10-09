"""Selective Coding (change add-selective-coding-layer, 2026-10-03 rulings).

The run's axial themes are synthesised, in relation to a researcher-written
research purpose, into core and secondary selective themes. Every theme traces
back through its axial themes and open codes to the original evidence.

- The research purpose is one researcher-written line. Its master copy lives in
  the case folder; each coding run copies it into the run directory.
- `selective_themes.yaml` is the new artifact; the legacy `selective_coding.yaml`
  is compatibility-only and never written here.
- While a document is current, researcher edits are its content. Re-forming
  after an upstream change is a new analysis: the whole previous document is
  archived under `selective_themes.history/` and no earlier edit is carried
  onto the new themes.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import lifecycle

PURPOSE_FILE = "research_purpose.yaml"
THEMES_FILE = "selective_themes.yaml"
HISTORY_DIR = "selective_themes.history"
PURPOSE_MAX_CHARS = 120
NAME_MAX_CHARS = 15
INSIGHT_ASK = (150, 200)
INSIGHT_ACCEPT = (120, 240)
KEY_INSIGHTS = (3, 5)
KEY_INSIGHT_MAX_CHARS = 60
REPRESENTATIVE_MAX = 5
EDITABLE_FIELDS = ("name", "core_sentence", "insight", "key_insights")
LEGACY_MESSAGE = "這份編碼是舊方法產生的，沒有選擇編碼；重新編碼後才會有。"


class SelectiveError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(path)


def _chars(text) -> int:
    return sum(1 for ch in str(text or "") if not ch.isspace())


# ── research purpose ────────────────────────────────────────────────────

def read_purpose(folder: str | Path) -> str:
    doc = _load(Path(folder) / PURPOSE_FILE) or {}
    return str(doc.get("text") or "").strip()


def write_purpose(folder: str | Path, text: str) -> dict:
    value = str(text or "").strip()
    if not value:
        raise SelectiveError("研究目的不能是空白。")
    if len(value) > PURPOSE_MAX_CHARS:
        raise SelectiveError(f"研究目的請寫在 {PURPOSE_MAX_CHARS} 字以內。")
    doc = {"schema_version": 1, "text": value, "source": "researcher", "updated_at": _now()}
    _write(Path(folder) / PURPOSE_FILE, doc)
    return doc


def copy_purpose_into_run(case_folder: str | Path, run_dir: str | Path) -> bool:
    """At the start of coding: the case's researcher-written purpose becomes the
    run's. Nothing is written when the case has none."""
    source = Path(case_folder) / PURPOSE_FILE
    if not source.is_file():
        return False
    (Path(run_dir) / PURPOSE_FILE).write_bytes(source.read_bytes())
    return True


# ── input ───────────────────────────────────────────────────────────────

def _digest(value) -> str:
    return lifecycle.canonical_content_digest(value)


def framing(run_dir: str | Path) -> dict | None:
    """The two upstream digests a selective document is framed by, from what
    the run holds now; None when either input is missing."""
    run = Path(run_dir)
    axial = _load(run / "axial_coding.yaml")
    purpose = read_purpose(run)
    if not isinstance(axial, dict) or not purpose:
        return None
    return {"axial_coding": _digest(axial), "research_purpose": _digest(purpose)}


def structure_input(run_dir: str | Path) -> dict:
    """What Selective Coding reads: the research purpose, the axial themes
    (with definition, open codes and representative quotes), the open codes
    they cite, and the theme-formation note. Refuses legacy axial coding."""
    run = Path(run_dir)
    purpose = read_purpose(run)
    if not purpose:
        raise SelectiveError("請先寫研究目的，才能形成選擇編碼。")
    axial = _load(run / "axial_coding.yaml") or {}
    categories = [c for c in axial.get("categories") or [] if isinstance(c, dict)]
    if not categories:
        raise SelectiveError("這份 run 還沒有主軸編碼。")
    if not all(c.get("concept_refs") for c in categories):
        raise SelectiveError("這份主軸編碼是舊方法產生的，沒有記下包含的開放編碼；請重新編碼後再形成選擇編碼。")
    concepts = {c.get("id"): c for c in (_load(run / "open_coding.yaml") or {}).get("concepts") or []
                if isinstance(c, dict)}
    evidence = {e.get("id"): e for e in _load(run / "evidence.yaml") or [] if isinstance(e, dict)}
    cited = sorted({ref for c in categories for ref in c.get("concept_refs") or []})
    return {
        "quoted": bool(cited) and all((concepts.get(cid) or {}).get("quote") for cid in cited if cid in concepts),
        "research_purpose": purpose,
        "axial_themes": [{
            "id": c.get("id"), "name": c.get("category_name") or c.get("label"), "summary": c.get("label"),
            "definition": c.get("definition"), "discriminating_basis": c.get("discriminating_basis"),
            "concept_refs": list(c.get("concept_refs") or []),
            "representative_quotes": _representative_quotes(c, concepts, evidence),
        } for c in categories],
        "open_codes": [{"id": cid, "name": concepts[cid].get("name"), "description": concepts[cid].get("description"),
                        "evidence_refs": list(concepts[cid].get("evidence_refs") or []),
                        **({"quote": dict(concepts[cid]["quote"])} if concepts[cid].get("quote") else {})}
                       for cid in cited if cid in concepts],
        "formation_note": axial.get("formation_note") or "",
    }


def _representative_quotes(item: dict, concepts: dict, evidence: dict) -> list[dict]:
    """converge-coding-contracts: a theme's representative quotes as the model
    reads them -- the chosen open codes' readable quotes unchanged, or, for an
    older theme, the chosen evidence texts."""
    if item.get("representative_quote_refs"):
        return [{"id": ref, **{k: v for k, v in (concepts.get(ref) or {}).get("quote", {}).items() if k in ("question", "answer")}}
                for ref in item["representative_quote_refs"] if (concepts.get(ref) or {}).get("quote")]
    return [{"id": ref, "role": (evidence.get(ref) or {}).get("role"), "text": (evidence.get(ref) or {}).get("text")}
            for ref in item.get("representative_evidence_refs") or [] if ref in evidence]


def candidates_input(run_dir: str | Path) -> dict:
    """What JTBD candidates read (multi-jtbd-single-job-source): the research
    purpose and the selective themes as the primary synthesis, with the axial
    themes they cite (and their representative quotes) as support only.
    Refuses without Selective Coding -- there is no axial-only fallback."""
    run = Path(run_dir)
    doc = load_document(run)
    if doc is None:
        raise SelectiveError("這份 run 還沒有選擇編碼；請先形成選擇編碼，才能形成顧客任務候選。")
    axial = _load(run / "axial_coding.yaml") or {}
    categories = {c.get("id"): c for c in axial.get("categories") or [] if isinstance(c, dict)}
    evidence = {e.get("id"): e for e in _load(run / "evidence.yaml") or [] if isinstance(e, dict)}
    concepts = {c.get("id"): c for c in (_load(run / "open_coding.yaml") or {}).get("concepts") or []
                if isinstance(c, dict)}
    themes = [t for t in doc.get("themes") or [] if isinstance(t, dict)]
    cited = sorted({ref for t in themes for ref in t.get("axial_refs") or [] if ref in categories})
    # converge-coding-contracts (2026-10-03): a new-method run gives JTBD only
    # the Selective synthesis and its quotes -- no Axial list, no Axial ids --
    # so Axial themes cannot become the candidate template. Provenance stays
    # derivable: candidate -> selective_refs -> axial_refs -> codes -> evidence.
    if themes and all(t.get("representative_quote_refs") for t in themes):
        return {
            "new_method": True,
            "research_purpose": doc.get("research_purpose") or read_purpose(run),
            "selective_themes": [{**{k: t.get(k) for k in ("id", "tier", "name", "core_sentence", "insight", "key_insights")},
                                  "representative_quotes": [{k: v for k, v in q.items() if k != "id"}
                                                            for q in _representative_quotes(t, concepts, evidence)]}
                                 for t in themes],
            "axial_support": [],
            "selective_ids": {t.get("id") for t in themes},
            "axial_ids": set(categories),
            "framed_by": {"selective_themes": _digest(doc)},
        }
    return {
        "research_purpose": doc.get("research_purpose") or read_purpose(run),
        "selective_themes": [{k: t.get(k) for k in ("id", "tier", "name", "core_sentence", "insight", "key_insights", "axial_refs")}
                             for t in themes],
        "axial_support": [{
            "id": cid, "name": categories[cid].get("category_name") or categories[cid].get("label"),
            "summary": categories[cid].get("label"), "definition": categories[cid].get("definition"),
            "conditions": categories[cid].get("conditions"), "actions": categories[cid].get("actions"),
            "consequences": categories[cid].get("consequences"),
            "representative_quotes": [{k: v for k, v in q.items() if k != "id"}
                                      for q in _representative_quotes(categories[cid], concepts, evidence)],
        } for cid in cited],
        "selective_ids": {t.get("id") for t in themes},
        "axial_ids": set(categories),
        "framed_by": {"selective_themes": _digest(doc)},
    }


def themes_digest(run_dir: str | Path) -> str | None:
    doc = load_document(run_dir)
    return _digest(doc) if doc is not None else None


# ── validation ──────────────────────────────────────────────────────────

def theme_errors(draft: object, view: dict) -> list[str]:
    """Rules beyond the schema: the provenance chain, lengths, and a core theme."""
    if not isinstance(draft, dict):
        return ["[selective_themes] 回覆要是一個物件"]
    axial = {t["id"]: set(t.get("concept_refs") or []) for t in view["axial_themes"]}
    concept_evidence = {c["id"]: set(c.get("evidence_refs") or []) for c in view["open_codes"]}
    themes = [t for t in draft.get("themes") or [] if isinstance(t, dict)]
    errors: list[str] = []
    if not any(t.get("tier") == "core" for t in themes):
        errors.append("[selective_themes] 至少要有一個核心選擇性編碼主題")
    for theme in themes:
        tid = theme.get("id")
        refs = list(theme.get("axial_refs") or [])
        for ref in refs:
            if ref not in axial:
                errors.append(f"[selective_themes] {tid} 的 axial_refs 引用不存在的主軸主題：{ref}")
        allowed_codes = set().union(*(axial.get(ref, set()) for ref in refs)) if refs else set()
        for code in theme.get("concept_refs") or []:
            if code not in allowed_codes:
                errors.append(f"[selective_themes] {tid} 的開放編碼 {code} 不屬於它引用的主軸主題")
        allowed_evidence = set().union(*(concept_evidence.get(code, set()) for code in theme.get("concept_refs") or [])) \
            if theme.get("concept_refs") else set()
        if view.get("quoted"):
            # converge-coding-contracts: pick the theme's own open codes'
            # readable quotes; the pool is every code the theme contains.
            quoted = {c["id"] for c in view["open_codes"] if c.get("quote")}
            members = set(theme.get("concept_refs") or [])
            if theme.get("representative_evidence_refs"):
                errors.append(f"[selective_themes] {tid} 用 representative_quote_refs 挑代表語句，不寫 representative_evidence_refs")
            if not theme.get("representative_quote_refs"):
                errors.append(f"[selective_themes] {tid} 缺少代表語句（representative_quote_refs）")
            for ref in theme.get("representative_quote_refs") or []:
                if ref not in members:
                    errors.append(f"[selective_themes] {tid} 的代表語句 {ref} 不是這個主題包含的開放編碼")
                elif ref not in quoted:
                    errors.append(f"[selective_themes] {tid} 的代表語句 {ref} 沒有整理後引言")
        else:
            if not theme.get("representative_evidence_refs"):
                errors.append(f"[selective_themes] {tid} 缺少代表語句（representative_evidence_refs）")
            for ref in theme.get("representative_evidence_refs") or []:
                if ref not in allowed_evidence:
                    errors.append(f"[selective_themes] {tid} 的代表語句 {ref} 不是它的開放編碼引用的證據")
        from .stages import support_summary_errors  # local: stages imports this module lazily too
        errors += support_summary_errors(f"[selective_themes] {tid}", theme.get("support_summary"))
        length = _chars(theme.get("insight"))
        if not INSIGHT_ACCEPT[0] <= length <= INSIGHT_ACCEPT[1]:
            errors.append(f"[selective_themes] {tid} 洞察敘事 {length} 字，要在 {INSIGHT_ASK[0]}–{INSIGHT_ASK[1]} 字左右")
        insights = theme.get("key_insights") or []
        if not KEY_INSIGHTS[0] <= len(insights) <= KEY_INSIGHTS[1]:
            errors.append(f"[selective_themes] {tid} 關鍵洞察要 {KEY_INSIGHTS[0]}–{KEY_INSIGHTS[1]} 條")
    return errors


# ── documents ───────────────────────────────────────────────────────────

def load_document(run_dir: str | Path) -> dict | None:
    doc = _load(Path(run_dir) / THEMES_FILE)
    return doc if isinstance(doc, dict) else None


def compose_document(draft: dict, case_id: str, purpose: str, frame: dict, now: str | None = None) -> dict:
    """A newly formed analysis, not yet written: empty researcher edits."""
    return {"schema_version": 1, "case_id": case_id, "research_purpose": purpose,
            "themes": copy.deepcopy(draft["themes"]), "framed_by": dict(frame), "formed_at": now or _now(),
            "researcher_edits": []}


def new_document(run_dir: str | Path, doc: dict) -> dict:
    """Write a validated new analysis. The current document, with its
    researcher edits, is archived first; nothing of it is carried over."""
    run = Path(run_dir)
    previous = load_document(run)
    doc = copy.deepcopy(doc)
    if previous is not None:
        stamp = str(previous.get("formed_at") or doc["formed_at"]).replace(":", "").replace("+", "_")
        history = Path(HISTORY_DIR) / f"{stamp}.yaml"
        _write(run / history, previous)
        doc["supersedes"] = {"digest": _digest(previous), "history_file": str(history)}
    _write(run / THEMES_FILE, doc)
    return doc


def apply_edit(document: dict, theme_id: str, field: str, text, now: str | None = None) -> dict:
    if field not in EDITABLE_FIELDS:
        raise SelectiveError("這個欄位不能修改。")
    edited = copy.deepcopy(document)
    theme = next((t for t in edited.get("themes") or [] if t.get("id") == theme_id), None)
    if theme is None:
        raise SelectiveError("找不到這個選擇編碼主題。")
    if field == "key_insights":
        items = text if isinstance(text, list) else str(text or "").split("\n")
        value = [str(item).strip() for item in items if str(item).strip()]
        if not value:
            raise SelectiveError("關鍵洞察至少要留一條。")
    else:
        value = str(text or "").strip()
        if not value:
            raise SelectiveError("這一格不能是空白。")
    if value == theme.get(field):
        return edited
    edits = edited.setdefault("researcher_edits", [])
    earlier = next((e for e in edits if e["theme_id"] == theme_id and e["field"] == field), None)
    if earlier is None:
        edits.append({"theme_id": theme_id, "field": field, "ai_text": theme.get(field),
                      "researcher_text": value, "edited_at": now or _now()})
    else:
        earlier.update(researcher_text=value, edited_at=now or _now())
    theme[field] = value
    return edited


def write_document(run_dir: str | Path, document: dict) -> None:
    _write(Path(run_dir) / THEMES_FILE, document)


def freshness(run_dir: str | Path) -> dict:
    """none | needs_purpose | legacy | ready | stale, for the first station."""
    run = Path(run_dir)
    doc = load_document(run)
    axial = _load(run / "axial_coding.yaml")
    categories = [c for c in (axial or {}).get("categories") or [] if isinstance(c, dict)]
    if doc is None:
        if not categories:
            return {"state": "none"}
        if not all(c.get("concept_refs") for c in categories):
            return {"state": "legacy", "message": LEGACY_MESSAGE}
        if not read_purpose(run):
            return {"state": "needs_purpose"}
        return {"state": "none"}
    frame = framing(run)
    if frame is None or frame != doc.get("framed_by"):
        return {"state": "stale"}
    return {"state": "ready"}


def state_from_run(run_dir: str | Path) -> str | None:
    path = Path(run_dir) / "run.json"
    try:
        return (json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}).get("selective_themes_state")
    except (OSError, ValueError):
        return None
