"""框架點的候選與選擇邏輯。

原本是 tools/workbench/ 那支獨立伺服器（8791）的內容。2026-09-15 起框架點的
決定全部在審閱工作台裡完成，研究者只會看到一個網址、只需要啟動一支程式，
所以那支的 HTTP 外殼與畫面都刪掉，只留這一層邏輯給 sbp_review 直接呼叫。
見 openspec/TARGET-ARCHITECTURE.md 決定十三。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import narrative_framing  # noqa: E402
from sbp_gen import hmw_formation  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO / "tools"
CASES_DIR = REPO / "cases"
RUNS_DIR = REPO / "runs"


def configure_data_root(data_root: str | Path | None = None) -> None:
    """Point framework-point discovery at the active runtime data root."""
    global CASES_DIR, RUNS_DIR
    root = REPO if data_root is None else Path(data_root).expanduser().resolve()
    CASES_DIR = root / "cases"
    RUNS_DIR = root / "runs"
# Same default port tools/sbp_review/server.py's own argparse default uses.
# Deliberately NOT named SBP_REVIEW_URL: ~18 existing browser_*.py test
# files already use that name for "the URL under test" (whichever app a
# given test drives, sbp_review or this one) -- reusing it here for a
# cross-server counterpart address would silently redirect gate-row routing
# whenever one of those tests set it for its own unrelated purpose.
SBP_REVIEW_COUNTERPART_URL = os.environ.get("SBP_REVIEW_COUNTERPART_URL", "http://127.0.0.1:8765")
# The case overview's three inert gate rows (expand-workbench-to-nine-stages),
# each naming the tools/sbp_review/ stage where that work actually happens.
GATE_STAGES = (
    ("顧客旅程", "confirm-journey"),
    ("現況服務藍圖", "review-as-is"),
    ("改善目標", "review-to-be"),
)

sys.path.insert(0, str(TOOLS_DIR))
import pathnames  # noqa: E402
import evidence_locator  # noqa: E402
import internal_id_patterns  # noqa: E402
from sbp_gen import decisions as sbp_decisions  # noqa: E402

# One entry per framework point. `cli` is the real `python3 -m sbp_gen <cli>`
# subcommand name (verified directly against tools/sbp_gen/__main__.py's
# dispatch table — this is not the same string as the URL mechanism key for
# every mechanism, they happen to coincide here). `self_authoring` is False
# for the mechanism whose selection schema has no self-authored-answer
# branch (core-category shows the full category list instead — see
# design.md's "Four shared-shape screens; two structurally distinct
# exceptions"; the other exception, primary-persona, was removed by
# quadrant-members-are-designer-assigned).
MECHANISMS = {
    "selective-coding": {
        "cli": "selective-coding",
        "point": "core_category",
        "candidates_file": "core_category_candidates.yaml",
        "selection_file": "selective_coding.yaml",
        "self_authoring": False,
        "prerequisite": "axial_coding.yaml",
        "prerequisite_hint": "先跑過 `sbp-gen axial-coding --run {run_dir}`",
    },
    "persona-dimensions": {
        "cli": "persona-dimensions",
        "point": "persona_dimensions",
        "candidates_file": "persona_dimension_candidates.yaml",
        "selection_file": "persona_dimensions_selection.yaml",
        "self_authoring": True,
        "prerequisite": "axial_coding.yaml",
        "prerequisite_hint": "先跑過 `sbp-gen axial-coding --run {run_dir}`",
    },
    "primary-job": {
        "cli": "primary-job",
        "point": "primary_job",
        "candidates_file": "primary_job_candidates.yaml",
        "selection_file": "primary_job_selection.yaml",
        "self_authoring": True,
        "prerequisite": "axial_coding.yaml",
        "prerequisite_hint": "先跑過 `sbp-gen axial-coding --run {run_dir}`",
    },
    "primary-hmw": {
        "cli": "primary-hmw",
        "point": "primary_hmw",
        "candidates_file": "primary_hmw_candidates.yaml",
        "selection_file": "primary_hmw_selection.yaml",
        "self_authoring": True,
        "prerequisite": "hmw.yaml",
        "prerequisite_hint": "先跑過 `sbp-gen how-might-we --run {run_dir}`",
    },
    # segmentation-axes and target-segmentation (expose-remaining-framework-
    # points-in-workbench): the fifth and sixth framework points
    # decisions.py already tracks. Neither offers AI-ranked candidates at
    # all -- CLAUDE.md's 助理的界線 forbids ranking here (business value, not
    # evidence, decides), so both CLI commands only have an unranked
    # briefing mode (--propose) and a selection mode (--selection-file);
    # `self_authoring: False` here means "no self-authored-answer branch",
    # the same meaning it already has for selective-coding.
    "segmentation-axes": {
        "cli": "segmentation-axes",
        "point": "segmentation_axes",
        "candidates_file": "segmentation_axis_candidates.yaml",
        "selection_file": "segmentation_axis_selection.yaml",
        "self_authoring": False,
        "prerequisite": "persona_matrix.yaml",
        "prerequisite_hint": "先跑過 `sbp-gen persona-matrix --run {run_dir}`",
    },
    "target-segmentation": {
        "cli": "target-segmentation",
        "point": "primary_target",
        "candidates_file": "target_segmentation_candidates.yaml",
        "selection_file": "target_segmentation_selection.yaml",
        "self_authoring": False,
        # Two required files, not one -- see resolve_run_response()'s
        # generalized prerequisite check. segmentation_axis_selection.yaml
        # is a researcher-recorded selection, not something a copy-paste
        # CLI command produces, so its hint points at the workbench screen
        # that produces it rather than a shell invocation.
        "prerequisite": ("persona_matrix.yaml", "segmentation_axis_selection.yaml"),
        "prerequisite_hint": (
            "先跑過 `sbp-gen persona-matrix --run {run_dir}`",
            "先在工作台完成「目標族群軸」的選擇",
        ),
    },
}


def _load_yaml(path: Path):
    if not path.is_file():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def discover_cases() -> list[dict]:
    """Minimal case listing for the case picker dropdown, added after the
    researcher pointed out typing a folder name by hand is worse than
    picking from a list. Deliberately much smaller than
    tools/sbp_review/server.py's own `_discover_cases()` (this file never
    imports from tools/sbp_review/ — see the module docstring) since all
    this needs is a human-readable label and the folder to route on;
    unlike the run picker, showing a case's folder name here is not a
    CLAUDE.md violation — case folders are researcher-chosen names in the
    first place (e.g. "Café&Meal MUJI"), not internal run identifiers.

    Excludes `browser-test-*` folders — this project's own established
    naming convention for disposable test fixtures (matching the run
    picker's exclusion of engineering-only runs for the same reason: a
    researcher never needs to see them as a real option).
    """
    out = []
    if not CASES_DIR.is_dir():
        return out
    for case_yaml in sorted(CASES_DIR.glob("*/case.yaml")):
        if case_yaml.parent.name.startswith("browser-test-"):
            continue
        data = _load_yaml(case_yaml)
        if not isinstance(data, dict) or not data.get("case_id"):
            continue
        out.append({
            "folder": case_yaml.parent.name,
            "label": data.get("label") or case_yaml.parent.name,
        })
    return out


# Evidence location lives in tools/evidence_locator.py -- shared with
# tools/sbp_gen/'s `project` subcommand, which cannot import this interface
# module. Aliased here (not just `from evidence_locator import
# locate_evidence`) because several other functions in this file
# (_category_labels, _hmw_labels et al.) already call `_load_yaml_safe` /
# `_load_json_safe` directly; aliasing keeps every existing call site in
# this file working unchanged.
_load_yaml_safe = evidence_locator._load_yaml_safe
_load_json_safe = evidence_locator._load_json_safe
_locate_evidence = evidence_locator.locate_evidence


def _evidence_for(refs, records) -> tuple[list[dict], int]:
    """The verbatim records behind one candidate, plus how many referenced
    ids could not be found.

    Returns only role / locator / text / note. `id` is exactly the class of
    internal identifier CLAUDE.md keeps off the screen, and `source` is a
    filesystem path (`統一時代百貨店/店外路人/....txt`), not an attribution --
    `role` is what names the speaker. Neither is sent to the browser at all,
    so no render site can leak one by accident.

    An id with no matching record is counted, never rendered as a
    placeholder or as the bare id itself: a screen that says its evidence is
    partial is honest, one that shows an id is both useless and banned."""
    by_id = {r["id"]: r for r in (records or []) if isinstance(r, dict) and r.get("id")}
    resolved, missing = [], 0
    seen = set()
    for ref in refs or []:
        if ref in seen:
            continue
        seen.add(ref)
        record = by_id.get(ref)
        if record is None:
            missing += 1
            continue
        resolved.append({
            "role": record.get("role", ""),
            "locator": record.get("locator", ""),
            "text": record.get("text", ""),
            "note": record.get("note", ""),
        })
    return resolved, missing


BASIS_EVIDENCE_LIMIT = 3


def basis_evidence(refs, records, limit: int = BASIS_EVIDENCE_LIMIT) -> list[dict]:
    """evidence-basis-inspection: the first `limit` resolvable records behind
    a basis panel, in stored order, as {text, role, locator, question?}.

    Stored order is the only ordering used: the run records no "which of
    these is representative" field, so picking would be a judgement the
    panel must not make. Like `_evidence_for`, neither `id` nor `source`
    leaves this function; a reference with no record is skipped."""
    by_id = {r["id"]: r for r in (records or []) if isinstance(r, dict) and r.get("id")}
    out, seen = [], set()
    for ref in refs or []:
        if len(out) >= limit:
            break
        if ref in seen:
            continue
        seen.add(ref)
        record = by_id.get(ref)
        if record is None:
            continue
        item = {"text": record.get("text", ""), "role": record.get("role", ""),
                "locator": record.get("locator", "")}
        question = str(record.get("question_context") or "").strip()
        if question:
            item["question"] = question
        out.append(item)
    return out


def _category_evidence_refs(run_dir: Path) -> dict[str, list]:
    """Category id -> its evidence_refs, from the same axial_coding.yaml
    `_category_labels()` already reads."""
    axial = _load_yaml_safe(run_dir / "axial_coding.yaml") or {}
    return {
        c["id"]: list(c.get("evidence_refs") or [])
        for c in (axial.get("categories") or [])
        if isinstance(c, dict) and c.get("id")
    }


def _subject_evidence_refs(run_dir: Path) -> dict[str, list]:
    """Subject id -> its evidence_refs, from persona_matrix.yaml."""
    matrix = _load_yaml_safe(run_dir / "persona_matrix.yaml") or {}
    return {
        s["id"]: list(s.get("evidence_refs") or [])
        for s in (matrix.get("subjects") or [])
        if isinstance(s, dict) and s.get("id")
    }


def _refs_via_categories(grounded_in, category_refs: dict) -> list:
    """Union of the evidence_refs of every category a candidate is grounded
    in, in first-seen order (a candidate names 1-3 categories, which
    routinely share evidence)."""
    out, seen = [], set()
    for cid in grounded_in or []:
        for ref in category_refs.get(cid) or []:
            if ref not in seen:
                seen.add(ref)
                out.append(ref)
    return out


def _refs_via_subjects(subject_ids, subject_refs: dict) -> list:
    out, seen = [], set()
    for sid in subject_ids or []:
        for ref in subject_refs.get(sid) or []:
            if ref not in seen:
                seen.add(ref)
                out.append(ref)
    return out


def _supporting_jtbds(refs, selected: list) -> list[dict]:
    """JTBD-n refers to the n-th selected JTBD; returns their words, no ids."""
    out = []
    for ref in refs or []:
        try:
            item = selected[int(str(ref).removeprefix("JTBD-")) - 1]
        except (ValueError, IndexError):
            continue
        out.append({key: item.get(key) or "" for key in ("situation", "progress", "motivation", "outcome")})
    return out


def _with_evidence(item: dict, refs, records) -> dict:
    resolved, missing = _evidence_for(refs, records)
    return {**item, "evidence": resolved, "unresolved_evidence_count": missing}


def _category_labels(run_dir: Path) -> dict[str, str]:
    axial = _load_yaml(run_dir / "axial_coding.yaml") or {}
    return {
        c["id"]: c.get("label", c["id"])
        for c in (axial.get("categories") or [])
        if isinstance(c, dict) and c.get("id")
    }


def _hmw_labels(run_dir: Path) -> dict[str, str]:
    # hmw.yaml entries carry no `label` field (only researchers' HMW
    # statements) — `statement` is the human-readable text to resolve to.
    hmw = _load_yaml(run_dir / "hmw.yaml") or {}
    return {
        e["id"]: e.get("statement", e["id"])
        for e in (hmw.get("entries") or [])
        if isinstance(e, dict) and e.get("id")
    }


def _axis_labels(run_dir: Path) -> dict[str, str]:
    """Persona Matrix axis id -> its human-readable `label` -- mirrors
    `_category_labels()`/`_hmw_labels()` exactly. Used to resolve
    segmentation-axis briefs' `axis_id` for display; the missing-file/
    missing-id fallback (empty dict / bare id) should never actually
    surface for segmentation-axes or target-segmentation, since both
    mechanisms' prerequisite already guarantees persona_matrix.yaml exists."""
    matrix = _load_yaml(run_dir / "persona_matrix.yaml") or {}
    return {
        a["id"]: a.get("label", a["id"])
        for a in (matrix.get("axes") or [])
        if isinstance(a, dict) and a.get("id")
    }


def _persona_labels(run_dir: Path) -> dict[str, dict]:
    """Persona id -> {label, member_count} -- same shape as
    `_category_labels()`/`_hmw_labels()`/`_axis_labels()`, extended with how
    many interviewees the group holds. Unlike the other label helpers this
    one does NOT fall back to the bare id: a persona id is a banned
    on-screen token, so an unresolvable reference is left without a label
    for the caller to render name-less rather than showing the id. Kept for
    `_persona_by_quadrant()` below (quadrant-members-are-designer-assigned
    removed this helper's original caller, the primary-persona screen, but
    the four quadrant personas still need the same label/member_count
    shape)."""
    content = _load_yaml(run_dir / "persona_content.yaml") or {}
    return {
        p["id"]: {
            "label": p.get("label", ""),
            "member_count": len(p.get("member_subject_ids") or []),
        }
        for p in (content.get("personas") or [])
        if isinstance(p, dict) and p.get("id")
    }


WORKBENCH_CONTRACT = REPO / "design-system" / "desktop-workbench.yaml"
CLAIM_BADGE_COMPONENT_ID = "ClaimBadge"
# evidence-aware-persona-provenance: the four provenance types, in the order a
# reader travels away from the evidence. `label` and `symbol` travel with the
# colour because the declared component forbids meaning carried by colour
# alone; `owes` says what activating the badge will show, so the page does not
# decide per render site what a type is entitled to be asked for.
CLAIM_BADGE_STATES = (
    ("research_evidence", "evidence", "研究資料", "◆", "evidence"),
    ("evidence_synthesis", "synthesis", "資料綜合", "◈", "evidence"),
    ("evidence_inference", "inference", "研究推論", "⚑", "bridge"),
    ("illustrative_identity", "illustrative", "人物化設定", "○", "disclaimer"),
)


def _claim_badge_projection(path: Path = WORKBENCH_CONTRACT) -> dict:
    """The four provenance badges, read from the design system document
    tools/sbp_review/server.py already reads.

    `desktop-workbench-interface-system` requires the epistemic style rule to
    apply wherever the label renders rather than inside one container, so both
    interfaces take these colours from the one declaration. Hardcoding a hex
    at a render site is the drift that requirement exists to prevent.

    Returns `{"available": False, "reason": ...}` rather than a fallback
    palette when the document cannot be read or does not declare every state:
    the page must then decline to render marked content rather than show it
    unmarked, because an unmarked inference is the failure this whole change
    exists to prevent.
    """
    if not path.is_file():
        return {"available": False, "reason": "找不到設計系統文件"}
    try:
        contract = _load_yaml(path)
    except Exception as exc:  # malformed YAML
        return {"available": False, "reason": f"設計系統文件讀不到：{exc}"}
    if not isinstance(contract, dict):
        return {"available": False, "reason": "設計系統文件的內容不是一份設定"}
    component = next(
        (c for c in contract.get("components") or []
         if isinstance(c, dict) and c.get("id") == CLAIM_BADGE_COMPONENT_ID),
        None,
    )
    if component is None:
        return {"available": False, "reason": f"設計系統沒有宣告 {CLAIM_BADGE_COMPONENT_ID}"}
    declared = component.get("states") or []
    colours = (contract.get("tokens") or {}).get("claim_source") or {}
    states = {}
    for provenance, state, label, symbol, owes in CLAIM_BADGE_STATES:
        if state not in declared:
            return {"available": False,
                    "reason": f"{CLAIM_BADGE_COMPONENT_ID} 沒有宣告「{label}」這個狀態"}
        if not colours.get(state):
            return {"available": False, "reason": f"設計系統沒有宣告「{label}」的顏色"}
        states[provenance] = {"state": state, "label": label,
                              "symbol": symbol, "color": colours[state], "owes": owes}
    return {"available": True, "component": CLAIM_BADGE_COMPONENT_ID,
            "interaction": "open-basis", "states": states}


PERSONA_CONTENT_FIELDS = ("motivation", "context", "behavior", "challenge", "need")
PERSONA_IDENTITY_FIELDS = ("person_name", "age", "profession", "income", "household_structure")


def _provenance_by_field(persona: dict, records) -> dict[str, dict]:
    """Field name -> what kind of statement it is, and whatever that kind owes.

    evidence-aware-persona-provenance: four kinds, each owing something
    different. An interpretation carries its bridging statement and the
    verbatim evidence it was read from; a synthesis carries its evidence; an
    illustrative identity carries neither and says only that it makes no
    research claim. A document written before these types reads as recorded
    evidence throughout, with its superseded inference records read as
    interpretations -- the same reading the validator applies, so a card and
    a check never disagree about what a document says.
    """
    entries = persona.get("field_provenance")
    if entries is None:
        entries = [
            {"field": e.get("field"), "type": "evidence_inference",
             "reasoning_summary": e.get("derivation"), "evidence_refs": e.get("evidence_refs")}
            for e in persona.get("inferred_fields") or [] if isinstance(e, dict)
        ]
        default_type = "research_evidence"
    else:
        default_type = None
    out: dict[str, dict] = {}
    for entry in entries or []:
        if not isinstance(entry, dict) or not entry.get("field"):
            continue
        evidence, missing = _evidence_for(entry.get("evidence_refs"), records)
        out[entry["field"]] = {
            "type": entry.get("type"),
            "reasoning_summary": entry.get("reasoning_summary") or "",
            "evidence": evidence,
            "unresolved_evidence_count": missing,
        }
    if default_type:
        for name in PERSONA_CONTENT_FIELDS + PERSONA_IDENTITY_FIELDS + ("avatar",):
            if persona.get(name) and name not in out:
                out[name] = {"type": default_type, "reasoning_summary": "",
                             "evidence": [], "unresolved_evidence_count": 0}
    return out


def _persona_by_quadrant(run_dir: Path, records=None) -> dict[str, dict]:
    """Quadrant id -> the persona to show for that quadrant, joined via each
    persona's `quadrant_id` (one persona per quadrant). Absent entirely when
    persona-content hasn't been generated yet for this run, or for a quadrant
    persona-content hasn't covered.

    mark-and-show-inferred-persona-fields: carries the five content fields
    and, for each field the persona recorded as inferred, that record's
    derivation with the verbatim evidence it was read from. `records` is
    optional only so a caller with no evidence loaded still gets the fields;
    without it a derivation arrives with no evidence behind it, which the
    page renders as partial rather than as an unsourced claim.

    Evidence goes through `_evidence_for`, the same resolver every other
    candidate on these screens uses -- a second lookup here is exactly the
    bug fixed in the axial-coding review a day earlier."""
    content = _load_yaml(run_dir / "persona_content.yaml") or {}
    labels = _persona_labels(run_dir)
    out: dict[str, dict] = {}
    for p in content.get("personas") or []:
        if not isinstance(p, dict):
            continue
        quadrant_id = p.get("quadrant_id")
        if not quadrant_id:
            continue
        resolved = labels.get(p.get("id")) or {}
        provenance = _provenance_by_field(p, records)
        quote = p.get("representative_quote")
        quote_evidence, quote_missing = _evidence_for(
            [quote.get("evidence_ref")] if isinstance(quote, dict) else [], records
        )

        def _entry(name):
            return {"name": name, "text": p.get(name) or "", "provenance": provenance.get(name)}

        out[quadrant_id] = {
            "label": resolved.get("label", ""),
            "description": p.get("description", ""),
            "identity": [_entry(name) for name in PERSONA_IDENTITY_FIELDS if p.get(name)],
            "fields": [_entry(name) for name in PERSONA_CONTENT_FIELDS],
            "quote": {
                "text": quote.get("text", ""),
                "evidence": quote_evidence,
                "unresolved_evidence_count": quote_missing,
            } if isinstance(quote, dict) else None,
        }
    return out


def _subject_identity(run_dir: Path, records) -> dict[str, dict]:
    """Subject id -> {role, locator, text} from that subject's first
    evidence_ref, resolved against the run's evidence records -- never the
    subject id itself. Same identification method the seven existing
    framework-point screens already use (speaker description + one
    verbatim line), applied here to interviewees rather than candidates."""
    subject_refs = _subject_evidence_refs(run_dir)
    by_id = {r["id"]: r for r in (records or []) if isinstance(r, dict) and r.get("id")}
    out: dict[str, dict] = {}
    for subject_id, refs in subject_refs.items():
        first_ref = refs[0] if refs else None
        record = by_id.get(first_ref) if first_ref else None
        if record is None:
            continue
        out[subject_id] = {
            "role": record.get("role", ""),
            "locator": record.get("locator", ""),
            "text": record.get("text", ""),
        }
    return out


def _subject_index_map(run_dir: Path) -> dict[str, int]:
    """Subject id -> a stable integer index, in persona_matrix.yaml's own
    subject order -- the only subject-referencing token the browser ever
    sees (CLAUDE.md bans the real subject id from the screen). Deterministic
    across a GET (building the index) and a later POST (translating the
    index back) as long as persona_matrix.yaml itself hasn't changed."""
    matrix = _load_yaml_safe(run_dir / "persona_matrix.yaml") or {}
    return {
        s["id"]: i for i, s in enumerate(matrix.get("subjects") or [])
        if isinstance(s, dict) and s.get("id")
    }


def _axis_poles(run_dir: Path) -> dict[str, dict]:
    """Persona Matrix axis id -> {label, pole_low, pole_high} -- same source
    file as `_axis_labels()`, extended with the prose pole text a quadrant's
    label needs (a quadrant has no label of its own; see
    `_quadrant_label()`)."""
    matrix = _load_yaml(run_dir / "persona_matrix.yaml") or {}
    return {
        a["id"]: {
            "label": a.get("label", a["id"]),
            "pole_low": a.get("pole_low", ""),
            "pole_high": a.get("pole_high", ""),
        }
        for a in (matrix.get("axes") or [])
        if isinstance(a, dict) and a.get("id")
    }


def _quadrant_label(quadrant: dict, selected_axes: list, axis_poles: dict) -> str:
    """A quadrant carries no label of its own (schema: `id`, `axis_1_pole`,
    `axis_2_pole`, `member_subject_ids`, `portrait` only) -- its human
    identity is composed from both selected axes' real label and the prose
    pole text (`pole_low`/`pole_high`) its own `axis_1_pole`/`axis_2_pole`
    enum value (`near_high`/`near_low`/`mid`/`no_data`) selects. The enum
    value itself is never rendered (banned literal, CLAUDE.md) -- only the
    prose it points to."""
    def _side(axis_id: str, pole_value: str) -> str:
        axis = axis_poles.get(axis_id, {})
        label = axis.get("label", axis_id)
        text = axis.get("pole_high") if pole_value == "near_high" else axis.get("pole_low")
        return f"{label}：{text}" if text else label

    axis_1_id = selected_axes[0] if len(selected_axes) > 0 else None
    axis_2_id = selected_axes[1] if len(selected_axes) > 1 else None
    sides = []
    if axis_1_id:
        sides.append(_side(axis_1_id, quadrant.get("axis_1_pole")))
    if axis_2_id:
        sides.append(_side(axis_2_id, quadrant.get("axis_2_pole")))
    return "｜".join(sides) if sides else quadrant.get("id", "")


# scrub-internal-ids-from-quadrant-portraits: a run generated before the
# prompt/validator guard existed may already have a persisted portrait
# naming a subject by internal id. This never rewrites the persisted
# target_segmentation_candidates.yaml -- only what gets served -- so a
# leaked id doesn't need a real model call (and real cost) to stop showing
# on screen.
PORTRAIT_INTERNAL_ID_LEAK_STATEMENT = (
    "這一格的族群畫像文字裡意外留下受訪者的內部編號，已先隱藏——內容需要重新產生。"
)


def _scrubbed_portrait(portrait: object) -> str | None:
    if not isinstance(portrait, str):
        return portrait
    if internal_id_patterns.contains_internal_id(portrait):
        return PORTRAIT_INTERNAL_ID_LEAK_STATEMENT
    return portrait


def is_researcher_facing(run_dir: Path) -> bool:
    """False for a run the system itself already marks as not real research
    output — an engineering/dev artifact, not a hidden-from-the-user
    implementation guess. Two existing, honest signals, neither invented
    for this filter:

    - `run.json`'s own `not_a_baseline_notice` field: several stages
      already write this to say "this run cannot be treated as a baseline
      or candidate" (smoke tests, deliberately-scoped manual-verification
      stubs). If the system itself says not to treat it as real, the
      researcher-facing picker should not offer it as if it were.
    - A `README.md` in the run directory: real pipeline output is never
      accompanied by a hand-written README — its presence here means a
      developer authored this run directly as a documented experiment
      (e.g. an A/B test comparing two framework-point choices), not
      something a researcher asked the pipeline to produce.

    Found directly from a researcher's own confusion at seeing 6 "分析結果"
    for one case with no way to tell which were ever meant for them —
    CLAUDE.md's own rule ("系統用、使用者不需要的東西不要放") applies here as
    much as it does to a bare internal id.
    """
    run_json_path = run_dir / "run.json"
    if run_json_path.is_file():
        try:
            data = json.loads(run_json_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            data = {}
        if isinstance(data, dict) and data.get("not_a_baseline_notice"):
            return False
    if (run_dir / "README.md").is_file():
        return False
    return True


def run_summary(run_dir: Path) -> dict:
    """Human-meaningful, non-directory-name info about a run, for the run
    picker. `name` stays in the payload (the frontend needs it to route
    subsequent requests, as a hidden value — not rendered as page text,
    matching how `category_id` etc. work elsewhere), but the picker's
    *visible* label is built from `created_at` (the run's own case.yaml
    mtime, not a parse of the directory name string — CLAUDE.md's ban is on
    showing the directory name itself, not on showing when a run was
    produced) and `candidates_ready` (how many of the five mechanisms have
    a candidates file in this run, so a researcher can tell an empty
    early-stage run apart from a fully-developed one without needing the
    directory name to do it).

    Bug found directly by the researcher on first use: without this, the
    run picker showed nothing but "分析結果 1".."分析結果 N", indistinguishable
    from one another — technically compliant with the no-directory-names
    rule, but unusable, the exact failure mode CLAUDE.md's "研究者驗收情境"
    requirement exists to catch and this feature's own walkthrough hadn't
    reached yet.
    """
    case_yaml = run_dir / "case.yaml"
    created_at = None
    if case_yaml.is_file():
        from datetime import datetime
        created_at = datetime.fromtimestamp(case_yaml.stat().st_mtime).strftime("%Y-%m-%d")
    ready = sum(
        1 for spec in MECHANISMS.values()
        if (run_dir / spec["candidates_file"]).is_file()
    )
    return {"name": run_dir.name, "created_at": created_at, "candidates_ready": ready}


def _as_tuple(value: str | tuple[str, ...]) -> tuple[str, ...]:
    return value if isinstance(value, tuple) else (value,)


def _sbp_review_reachable() -> bool:
    """Fast, side-effect-free liveness probe -- a TCP connect, not a full request."""
    parsed = urlparse(SBP_REVIEW_COUNTERPART_URL)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def gate_entries(case_id: str) -> list[dict]:
    """The three gate rows' routing state -- read-only, computed per request.

    Mirrors tools/sbp_review/server.py's _framework_point_runtime_entries():
    same reachability-probe-then-build-a-url shape, same "state the
    counterpart isn't running" failure mode, deliberately not shared code
    (the two servers don't import each other).
    """
    reachable = _sbp_review_reachable()
    unreachable_reason = (
        None if reachable else
        f"服務藍圖工作台目前沒有在跑（預期位址：{SBP_REVIEW_COUNTERPART_URL}）。請先啟動 tools/sbp_review/server.py。"
    )
    entries = []
    for label, stage_id in GATE_STAGES:
        if reachable:
            route_url = f"{SBP_REVIEW_COUNTERPART_URL}/?case={quote(case_id)}&stage={quote(stage_id)}"
            reason = None
        else:
            route_url, reason = None, unreachable_reason
        entries.append({"label": label, "stage_id": stage_id, "route_url": route_url, "route_unavailable_reason": reason})
    return entries


ARTIFACT_PLAIN_NAMES = {
    "axial_coding.yaml": "主軸編碼",
    "open_coding.yaml": "開放編碼",
    "hmw.yaml": "HMW 張力清單",
    "persona_matrix.yaml": "Persona 矩陣",
    "segmentation_axis_selection.yaml": "選定的目標族群軸",
    "jtbd.yaml": "顧客任務",
    "job_map.yaml": "Job Map",
    "segmentation_axis_candidates.yaml": "目標族群軸候選",
    "primary_hmw_candidates.yaml": "主要 HMW 候選",
    "core_category_candidates.yaml": "核心類別候選",
    "narrative_job_candidates.yaml": "顧客任務候選",
    "persona_dimension_candidates.yaml": "Persona 維度候選",
    "target_segmentation_candidates.yaml": "目標族群候選",
    "target_segmentation_selection.yaml": "主要與次要目標",
    "persona_content.yaml": "Persona 內容",
}


def _hmw_formation_view(run_dir: Path) -> dict:
    """The HMW station's Form HMW state, in researcher words only."""
    formation = hmw_formation.formation_state(run_dir)
    refusal = hmw_formation.readiness(run_dir)
    state = formation.get("state")
    stage = formation.get("active_stage") or formation.get("failed_stage")
    return {
        "state": state,
        "stage_label": hmw_formation.STAGE_LABELS.get(stage),
        "failure": formation.get("failure") if state == "failed" else None,
        "can_start": refusal is None,
        "blocked_reason": refusal["error"] if refusal else None,
    }


def resolve_run_response(mechanism: str, run_dir: Path) -> dict:
    """Everything a framework-point screen needs for one mechanism/run.

    Every internal id this function's callers might otherwise expose is
    resolved to a human-readable label here — category ids, HMW ids, and
    (for core-category) the grounded_in category refs nested inside
    persona-dimension and primary-job candidates. Never returns a run
    directory name or a bare candidates-file case_id (design.md: a
    mechanism file's own case_id is never trusted as authoritative).
    """
    spec = MECHANISMS[mechanism]
    if mechanism == "primary-hmw" and not (run_dir / spec["candidates_file"]).is_file():
        # form-hmw-from-confirmed-narrative: HMW candidates come only from the
        # explicit Form HMW action, never from a per-file run or a copied
        # command. A formation that found no tension is complete: the
        # researcher can still write their own HMW.
        formation = _hmw_formation_view(run_dir)
        # hmw-lenses-diversify-confirmed-framing: an empty result formed under
        # a superseded HMW method is not final -- offer Form HMW again.
        superseded = any(item["stage"] == "how_might_we" and item["action"] != "reuse"
                         for item in hmw_formation.plan(run_dir))
        if formation["state"] == "awaiting_hmw_choice" and hmw_formation._no_tensions(run_dir) and not superseded:
            records, evidence_status = _locate_evidence(run_dir)
            hmw_doc = _load_yaml_safe(run_dir / "hmw.yaml") or {}
            reason = ((hmw_doc.get("insufficient_evidence") or {}).get("reason") or "").strip() or None
            return {"status": "ready", "candidates": [], "selected": None, "no_tensions": True,
                    "insufficient_evidence_reason": reason,
                    "evidence_status": evidence_status, "framing_stale": False, "formation": formation}
        return {"status": "not_formed", "formation": formation}
    # `prerequisite`/`prerequisite_hint` are a single string for every
    # mechanism except target-segmentation (two required files: the persona
    # matrix and the segmentation-axis selection) — normalize both to
    # same-length tuples and report the first missing one, in order, so the
    # message always names one concrete file and one concrete next step,
    # never a combined or generic message (expose-remaining-framework-
    # points-in-workbench: "A multi-file prerequisite mechanism SHALL name
    # the first specific missing file").
    prereqs = _as_tuple(spec["prerequisite"])
    hints = _as_tuple(spec["prerequisite_hint"])
    for prereq_name, hint in zip(prereqs, hints):
        prereq_path = run_dir / prereq_name
        if not prereq_path.is_file():
            # 訊息裡不放絕對路徑，也不放 run 目錄名——CLAUDE.md 明列那是不得
            # 出現在畫面上的東西。研究者要知道的是「少了哪一段分析」，不是
            # 檔案在磁碟的哪裡；真正要執行的指令留在終端機那一側。
            missing = ARTIFACT_PLAIN_NAMES.get(prereq_name, prereq_name)
            return {
                "status": "missing_prerequisite",
                "message": f"這份產出還沒有{missing}，要先產生它才能在這裡做決定。",
                "missing_artifact": prereq_name,
                "missing_label": missing,
            }

    candidates_path = run_dir / spec["candidates_file"]
    # Located once per request and shared by every branch: CLAUDE.md's
    # founding requirement is that any judgment on screen is one click from
    # the verbatim line behind it, and until now no branch returned evidence
    # at all, so no screen could offer that click.
    records, evidence_status = _locate_evidence(run_dir)

    if mechanism == "selective-coding":
        cat_labels = _category_labels(run_dir)
        cat_refs = _category_evidence_refs(run_dir)
        data = _load_yaml(candidates_path) or {}
        rationale_by_id = {
            c.get("category_id"): c.get("rationale")
            for c in (data.get("candidates") or [])
            if isinstance(c, dict)
        }
        categories = [
            _with_evidence({
                "category_id": cid,
                "label": label,
                "ai_proposed": cid in rationale_by_id,
                "rationale": rationale_by_id.get(cid),
            }, cat_refs.get(cid), records)
            for cid, label in cat_labels.items()
        ]
        selected = None
        selection_path = run_dir / spec["selection_file"]
        if selection_path.is_file():
            selection = _load_yaml(selection_path) or {}
            selected_id = selection.get("core_category_id")
            if selected_id in cat_labels:
                selected = {
                    "category_id": selected_id,
                    "label": cat_labels[selected_id],
                    "rationale": selection.get("rationale") or "",
                }
        return {"status": "ready", "categories": categories,
                "selected": selected,
                "evidence_status": evidence_status}

    if not candidates_path.is_file():
        # 帶上「還缺哪一份、它叫什麼」，介面才給得出那顆可以直接跑的按鈕，
        # 而不是只丟一行要研究者自己去終端機打的指令。
        return {
            "status": "not_generated",
            "propose_command": propose_command(mechanism, run_dir),
            "missing_artifact": spec["candidates_file"],
            "missing_label": ARTIFACT_PLAIN_NAMES.get(spec["candidates_file"], "這一步的候選"),
            "message": "這份產出還沒有{}，要先產生它才能在這裡做決定。".format(
                ARTIFACT_PLAIN_NAMES.get(spec["candidates_file"], "這一步的候選")),
        }

    data = _load_yaml(candidates_path) or {}

    if mechanism == "persona-dimensions":
        cat_labels = _category_labels(run_dir)
        cat_refs = _category_evidence_refs(run_dir)
        candidates = []
        for c in data.get("candidates") or []:
            axes = []
            for axis in c.get("axes") or []:
                # Evidence hangs on the axis, not the candidate set: each
                # axis names its own grounded_in categories.
                axes.append(_with_evidence({
                    **axis,
                    "grounded_in_labels": [cat_labels.get(cid, cid) for cid in axis.get("grounded_in") or []],
                }, _refs_via_categories(axis.get("grounded_in"), cat_refs), records))
            candidates.append({**c, "axes": axes})
        return {"status": "ready", "candidates": candidates,
                "evidence_status": evidence_status}

    if mechanism == "primary-job":
        cat_labels = _category_labels(run_dir)
        cat_refs = _category_evidence_refs(run_dir)
        candidates = [
            _with_evidence(
                {**c, "grounded_in_labels": [cat_labels.get(cid, cid) for cid in c.get("grounded_in") or []]},
                _refs_via_categories(c.get("grounded_in"), cat_refs), records)
            for c in (data.get("candidates") or [])
        ]
        return {"status": "ready", "candidates": candidates,
                "evidence_status": evidence_status}

    if mechanism == "primary-hmw":
        hmw_doc = _load_yaml_safe(run_dir / "hmw.yaml") or {}
        hmw_entries = {
            entry["id"]: entry
            for entry in (hmw_doc.get("entries") or [])
            if isinstance(entry, dict) and entry.get("id")
        }
        # An HMW entry carries its own tension and evidence refs. Candidate
        # files intentionally only reference that canonical entry by id, so
        # this read projection joins the canonical pair for the workbench
        # instead of leaving a blank tension field for the frontend to guess.
        import narrative_framing as _framing
        selected_jtbds = _framing.load_selection(run_dir)
        hmw_refs = {
            entry_id: list(entry.get("evidence_refs") or [])
            for entry_id, entry in hmw_entries.items()
        }
        candidates = [
            _with_evidence(
                {
                    **c,
                    # Keep these machine references available only for the
                    # selection payload; the UI renders labels and evidence,
                    # never the ids themselves.
                    "label": (hmw_entries.get(c.get("hmw_id")) or {}).get("statement", c.get("hmw_id")),
                    "statement": (hmw_entries.get(c.get("hmw_id")) or {}).get("statement"),
                    # hmw-framings-from-problem-structures: problem_structure on
                    # new framings, tension on older ones.
                    "tension": ((hmw_entries.get(c.get("hmw_id")) or {}).get("problem_structure")
                                or (hmw_entries.get(c.get("hmw_id")) or {}).get("tension")),
                    "problem_structure": (hmw_entries.get(c.get("hmw_id")) or {}).get("problem_structure"),
                    "grounded_in": (hmw_entries.get(c.get("hmw_id")) or {}).get("grounded_in"),
                    "evidence_refs": hmw_refs.get(c.get("hmw_id"), []),
                    # 2026-10-03 (HMW v12): which selected JTBDs this framing was
                    # induced from, as the jobs' own words for the card.
                    "supporting_jtbds": _supporting_jtbds(
                        (hmw_entries.get(c.get("hmw_id")) or {}).get("supporting_jtbd_refs"), selected_jtbds),
                },
                hmw_refs.get(c.get("hmw_id")), records)
            for c in (data.get("candidates") or [])
        ]
        # evidence-basis-inspection: the 依據 panel's projection -- what the
        # card already says plus the first stored records, nothing chosen.
        for candidate in candidates:
            basis = {
                "problem_structure": str(candidate.get("problem_structure") or "").strip(),
                "supporting_jtbds": candidate.get("supporting_jtbds") or [],
                "evidence": basis_evidence(candidate.get("evidence_refs"), records),
            }
            if any(basis.values()):
                candidate["basis"] = basis
        # wire-primary-hmw-selection-submit: an existing selection is shown
        # on first render, not only right after a same-session submission.
        # A selection referencing an id absent from the current candidates
        # (or an invalid self-authored branch) is never surfaced as a chosen
        # pair. A valid self-authored selection carries its own complete
        # tension/HMW/evidence record and is therefore safe to show back.
        selection_doc = _load_yaml_safe(run_dir / "primary_hmw_selection.yaml") or {}
        selected_id = selection_doc.get("hmw_id")
        selected = None
        if selected_id and any(c.get("hmw_id") == selected_id for c in candidates):
            selected = {"hmw_id": selected_id, "rationale": selection_doc.get("rationale")}
        elif all(selection_doc.get(field) for field in ("tension", "statement", "rationale", "grounded_in")) and selection_doc.get("evidence_refs"):
            selected = {
                "custom": True,
                "tension": selection_doc.get("tension"),
                "statement": selection_doc.get("statement"),
                "rationale": selection_doc.get("rationale"),
                "grounded_in": selection_doc.get("grounded_in"),
                "evidence_refs": list(selection_doc.get("evidence_refs") or []),
            }
        # frame-hmw-from-narrative: candidates built before the researcher's
        # current stage-2 pair say so; rebuilding them is the researcher's call.
        framing_stale = narrative_framing.is_stale(run_dir, data) or narrative_framing.is_stale(run_dir, hmw_doc)
        return {"status": "ready", "candidates": candidates, "selected": selected,
                "evidence_status": evidence_status, "framing_stale": framing_stale,
                # form-hmw-from-confirmed-narrative: a re-form changed the
                # selected candidate's content, so the old choice was retired.
                "selection_retired": bool(hmw_formation.formation_state(run_dir).get("selection_retired"))
                                     and not (run_dir / "primary_hmw_selection.yaml").is_file(),
                # hmw-seek-three-distinct-framings: candidates formed by a
                # superseded HMW method can be re-formed from this station.
                # hmw-candidate-rationale-boundary: either HMW step being
                # method-stale offers the same re-form; the planner decides what reruns.
                "method_superseded": any(item["stage"] in ("how_might_we", "primary_hmw_candidates")
                                         and item["action"] == "regenerate" and item["reason"] == "method_version"
                                         for item in hmw_formation.plan(run_dir)),
                "formation": _hmw_formation_view(run_dir)}

    if mechanism == "segmentation-axes":
        axis_labels = _axis_labels(run_dir)
        # merge-persona-matrix-selection-into-one-screen: the review app's
        # axis card shows a two-pole label by default now (reasoning moved
        # behind a disclosure) -- pole_low/pole_high already exist on every
        # axis (persona-matrix.schema.json requires both), and _axis_poles()
        # already resolves them for target-segmentation's own quadrant
        # labels, so this reuses that instead of parsing pole text out of
        # evidence_summary.
        axis_poles = _axis_poles(run_dir)
        # An axis has no evidence_refs of its own -- what grounds it is the
        # subjects it actually separated. A subject whose placement on this
        # axis is `no_data` was checked and found silent on it, so its
        # evidence is not evidence *for* this axis.
        matrix = _load_yaml_safe(run_dir / "persona_matrix.yaml") or {}
        refs_by_axis: dict[str, list] = {}
        for subject in matrix.get("subjects") or []:
            if not isinstance(subject, dict):
                continue
            for placement in subject.get("placements") or []:
                if not isinstance(placement, dict):
                    continue
                if placement.get("position") == "no_data":
                    continue
                bucket = refs_by_axis.setdefault(placement.get("axis_id"), [])
                for ref in subject.get("evidence_refs") or []:
                    if ref not in bucket:
                        bucket.append(ref)
        briefs = [
            _with_evidence(
                {
                    **b,
                    "label": axis_labels.get(b.get("axis_id"), b.get("axis_id")),
                    "pole_low": axis_poles.get(b.get("axis_id"), {}).get("pole_low", ""),
                    "pole_high": axis_poles.get(b.get("axis_id"), {}).get("pole_high", ""),
                },
                refs_by_axis.get(b.get("axis_id")), records)
            for b in (data.get("briefs") or [])
        ]
        return {"status": "ready", "briefs": briefs,
                "evidence_status": evidence_status}

    if mechanism == "target-segmentation":
        # quadrant-members-are-designer-assigned: member lists on this
        # screen are never the raw subject-id arrays -- every interviewee
        # (suggested member or unplaced) is resolved to a speaker
        # description + one verbatim line, plus an opaque index the
        # frontend echoes back on submission (see `_subject_index_map()`
        # and the POST-time translation in `do_POST`). Confirmed membership
        # (from target_segmentation_selection.yaml, when it exists) is
        # shown alongside the suggestion so the researcher can see what
        # they've already decided without re-deciding it.
        axis_poles = _axis_poles(run_dir)
        selected_axes = data.get("axis_ids") or []
        subject_refs = _subject_evidence_refs(run_dir)
        identity = _subject_identity(run_dir, records)
        index_by_subject = _subject_index_map(run_dir)
        persona_by_quadrant = _persona_by_quadrant(run_dir, records)
        selection_doc = _load_yaml_safe(run_dir / "target_segmentation_selection.yaml") or {}
        confirmed_by_quadrant = {
            m.get("quadrant_id"): list(m.get("subject_ids") or [])
            for m in (selection_doc.get("quadrant_members") or [])
            if isinstance(m, dict) and m.get("quadrant_id")
        }
        dispositions_by_subject = {
            d.get("subject_id"): d
            for d in (selection_doc.get("unplaced_dispositions") or [])
            if isinstance(d, dict) and d.get("subject_id")
        }

        def _member_entries(subject_ids):
            entries = []
            for subject_id in subject_ids or []:
                entry = {"index": index_by_subject.get(subject_id)}
                entry.update(identity.get(subject_id, {"role": "", "locator": "", "text": ""}))
                entries.append(entry)
            return entries

        quadrants = []
        for q in data.get("quadrants") or []:
            quadrant_id = q.get("id")
            # Read either field name -- a candidates document written
            # before quadrant-members-are-designer-assigned used
            # member_subject_ids; new ones only ever carry
            # suggested_subject_ids (see stages._quadrant_suggested_ids()).
            suggested_ids = q.get("suggested_subject_ids")
            if suggested_ids is None:
                suggested_ids = q.get("member_subject_ids") or []
            confirmed_ids = confirmed_by_quadrant.get(quadrant_id)
            quadrant = {
                "id": quadrant_id,
                "label": _quadrant_label(q, selected_axes, axis_poles),
                "portrait": _scrubbed_portrait(q.get("portrait")),
                "suggested_members": _member_entries(suggested_ids),
                "confirmed_members": _member_entries(confirmed_ids) if confirmed_ids is not None else None,
                "persona": persona_by_quadrant.get(quadrant_id),
            }
            quadrants.append(_with_evidence(
                quadrant, _refs_via_subjects(confirmed_ids or suggested_ids, subject_refs), records))

        unplaced = []
        for subject_id in data.get("unclassified_subject_ids") or []:
            entry = {"index": index_by_subject.get(subject_id)}
            entry.update(identity.get(subject_id, {"role": "", "locator": "", "text": ""}))
            disposition = dispositions_by_subject.get(subject_id)
            entry["disposition"] = disposition.get("disposition") if disposition else None
            entry["assigned_quadrant_id"] = disposition.get("assigned_quadrant_id") if disposition else None
            entry["reason"] = disposition.get("reason") if disposition else None
            unplaced.append(entry)

        return {
            "status": "ready",
            "quadrants": quadrants,
            "unplaced": unplaced,
            "has_confirmed_selection": bool(confirmed_by_quadrant),
            # merge-persona-matrix-selection-into-one-screen: the review
            # app's "生成 persona" trigger needs to know which quadrant was
            # already confirmed primary on a past visit (client-side
            # designation state is ephemeral, reset on every fresh load) --
            # echoing the persisted selection's own field rather than
            # inventing a new one to carry the same fact.
            "primary_quadrant_id": selection_doc.get("primary_quadrant_id"),
            "evidence_status": evidence_status,
            "claim_badge": _claim_badge_projection(),
        }

    raise ValueError(f"unknown mechanism: {mechanism}")  # pragma: no cover


def propose_command(mechanism: str, run_dir: Path) -> str:
    """The real, verified `--propose` invocation for this mechanism/run —
    `python3 -m sbp_gen` only resolves when run from tools/ (no
    tools/__init__.py makes it importable from the repo root; confirmed by
    direct invocation before writing this), so the command changes into
    tools/ first rather than assuming a nonexistent installed `sbp-gen`
    binary or an unverified repo-root invocation."""
    cli = MECHANISMS[mechanism]["cli"]
    return f"cd {TOOLS_DIR} && python3 -m sbp_gen {cli} --run {run_dir} --propose"


def _translate_target_segmentation_indices(run_dir: Path, answer: dict) -> dict | None:
    """quadrant-members-are-designer-assigned: the browser never sees a real
    subject id (see `_subject_index_map()`), so a target-segmentation
    submission references interviewees by the opaque index that mapping
    handed out on the matching GET. Translate those indices back to real
    subject ids here -- a mechanical lookup, not a validity judgement; the
    CLI's own `--selection-file` path still does all the actual validation
    (route-through-existing-cli-path, tasks.md 3.2). Returns `None` if any
    index does not resolve to a known subject, so the caller can surface
    that as a rejection instead of silently dropping a member.
    """
    index_by_subject = _subject_index_map(run_dir)
    subject_by_index = {index: subject_id for subject_id, index in index_by_subject.items()}

    translated = {k: v for k, v in answer.items() if k not in ("quadrant_members", "unplaced_dispositions")}

    quadrant_members = []
    for entry in answer.get("quadrant_members") or []:
        if not isinstance(entry, dict):
            return None
        subject_ids = []
        for index in entry.get("subject_indices") or []:
            subject_id = subject_by_index.get(index)
            if subject_id is None:
                return None
            subject_ids.append(subject_id)
        quadrant_members.append({"quadrant_id": entry.get("quadrant_id"), "subject_ids": subject_ids})
    if quadrant_members:
        translated["quadrant_members"] = quadrant_members

    unplaced_dispositions = []
    for entry in answer.get("unplaced_dispositions") or []:
        if not isinstance(entry, dict):
            return None
        subject_id = subject_by_index.get(entry.get("subject_index"))
        if subject_id is None:
            return None
        translated_entry = {"subject_id": subject_id, "disposition": entry.get("disposition"),
                             "reason": entry.get("reason")}
        if entry.get("assigned_quadrant_id") is not None:
            translated_entry["assigned_quadrant_id"] = entry.get("assigned_quadrant_id")
        unplaced_dispositions.append(translated_entry)
    if unplaced_dispositions:
        translated["unplaced_dispositions"] = unplaced_dispositions

    return translated


def run_selection_cli(mechanism: str, run_dir: Path, answer: dict) -> dict:
    """Write `answer` to a temp file and invoke the existing
    `--selection-file` CLI validation path unchanged — this function does
    not itself decide whether `answer` is valid."""
    spec = MECHANISMS[mechanism]
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as tmp:
        yaml.dump(answer, tmp, allow_unicode=True)
        tmp_path = tmp.name
    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "sbp_gen", spec["cli"],
                "--run", str(run_dir),
                "--selection-file", tmp_path,
            ],
            cwd=str(TOOLS_DIR),
            capture_output=True,
            text=True,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return {
        "ok": result.returncode == 0,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
