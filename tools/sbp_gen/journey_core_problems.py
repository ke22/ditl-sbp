"""cjm-as-is-first-core-problems (2026-10-21 researcher ruling): the third CJM
layer, kept apart from the as-is journey.

The as-is journey (`journey_proposal.yaml`, `generation_basis: as_is_all_evidence`)
is reconstructed from all evidence. This module supplies what the core-problem
call reads and records:

- the Persona member evidence ids, computed by code -- never judged by the
  model: the target Persona's `member_subject_ids`, through `persona_matrix.yaml`
  subjects, widened to every evidence item of the same confirmed person when
  `source_attribution.yaml` confirms who spoke;
- the basis the candidates were drafted from (journey, Persona, JTBD, HMW
  digests), so a later upstream change marks only this layer for recheck;
- reading, writing and freshness of `journey_core_problems.yaml`.

Nothing here writes the journey. Researcher emotion and satisfaction edits do
not enter `journey_digest`, so moving an emotion point never stales candidates.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import yaml

import hmw_scope
import narrative_framing

from . import run_files, source_attribution, stages

FILENAME = "journey_core_problems.yaml"
JOURNEY_DIGEST_FIELDS = ("id", "label", "pain_points", "experience")
# What the model sees of the target Persona: who they are, not its provenance.
PERSONA_VIEW_FIELDS = ("label", "description", "context", "motivation_goal", "decision_pattern",
                       "behavior_strategy", "challenge", "need")
BASIS_KEYS = ("persona", "jtbd", "hmw")


def _load(path: Path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, yaml.YAMLError):
        return None


def is_as_is_journey(journey_data: object) -> bool:
    return isinstance(journey_data, dict) and journey_data.get("generation_basis") == stages.JOURNEY_GENERATION_BASIS


def journey_digest(journey_data: dict) -> str:
    view = [{field: step.get(field) for field in JOURNEY_DIGEST_FIELDS if step.get(field) is not None}
            for step in stages._primary_steps(journey_data) if isinstance(step, dict)]
    payload = json.dumps(view, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def confirmed_persona(run_dir: Path) -> tuple[dict | None, str | None]:
    """The confirmed target Persona and its digest, from `persona_confirmed.yaml`."""
    snapshot = _load(Path(run_dir) / "persona_confirmed.yaml")
    if not isinstance(snapshot, dict):
        return None, None
    content = snapshot.get("persona_content")
    personas = content.get("personas") if isinstance(content, dict) else None
    persona = personas[0] if isinstance(personas, list) and personas and isinstance(personas[0], dict) else None
    return persona, snapshot.get("persona_digest") if persona else None


def group_evidence(run_dir: Path, persona: dict | None, evidence: list) -> tuple[set[str] | None, bool]:
    """(Persona member evidence ids, limited). None without a confirmed Persona.

    `limited` is True when no confirmed attribution widens the members to all
    of their evidence, so only the evidence the segmentation cited counts."""
    if not isinstance(persona, dict):
        return None, False
    members = set(persona.get("member_subject_ids") or [])
    matrix = _load(Path(run_dir) / "persona_matrix.yaml")
    subjects = [s for s in (matrix or {}).get("subjects") or []
                if isinstance(s, dict) and s.get("id") in members]
    cited = {ref for subject in subjects for ref in subject.get("evidence_refs") or [] if isinstance(ref, str)}
    doc = source_attribution.load(Path(run_dir))
    if not isinstance(doc, dict):
        return cited, True
    by_id = {item.get("id"): item for item in evidence if isinstance(item, dict)}
    persons = {source_attribution.person_key(by_id[ref], doc) for ref in cited if ref in by_id} - {None}
    if not persons:
        return cited, True
    widened = {item["id"] for item in evidence
               if isinstance(item, dict) and item.get("id") and source_attribution.person_key(item, doc) in persons}
    return cited | widened, False


def persona_view(persona: dict | None) -> dict | None:
    if not isinstance(persona, dict):
        return None
    return {field: persona[field] for field in PERSONA_VIEW_FIELDS if persona.get(field)}


def current_basis(run_dir: Path, persona_digest: str | None) -> dict:
    """The upstream the candidates would be drafted from now. The Persona digest
    is passed in: the CLI reads the confirmed snapshot, the workbench its own
    "confirmed and still current" digest."""
    return {"persona_digest": persona_digest,
            "jtbd_digest": narrative_framing.current_digest(Path(run_dir)),
            "hmw_digest": hmw_scope.current_digest(Path(run_dir))}


def load(run_dir: Path) -> dict | None:
    doc = _load(Path(run_dir) / FILENAME)
    return doc if isinstance(doc, dict) else None


def candidate_id(problem: dict) -> str:
    """CP-<8 hex> over the candidate's content: a redraft never reuses the id of a
    candidate the researcher adopted earlier."""
    content = json.dumps([problem.get("problem"), sorted(problem.get("step_ids") or []),
                          sorted(problem.get("evidence_refs") or [])], ensure_ascii=False)
    return "CP-" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:8]


def build_document(journey_data: dict, draft: dict, group_ids: set[str] | None, limited: bool,
                   basis: dict, now: str | None = None) -> dict:
    """The stored v2 document (cjm-core-problem-synthesis): relevance per step and the
    journey-level core problem candidates, plus the ids, digests and the
    `group_evidence` flag only the system writes."""
    group = set(group_ids or ())
    steps = [{k: entry[k] for k in ("step_id", "persona_relevance", "relevance_note", "relevance_refs") if k in entry}
             for entry in draft.get("steps") or [] if isinstance(entry, dict)]
    problems = []
    for problem in draft.get("core_problems") or []:
        if not isinstance(problem, dict):
            continue
        stored = {"id": candidate_id(problem), **{k: problem[k] for k in (
            "problem", "title", "step_ids", "basis", "why_core", "jtbd_note", "evidence_refs",
            "inferred", "hmw_relation", "hmw_note") if k in problem}}
        # core-problem-evidence-elicitation: drafted as a list, stored as {evidence id: kind};
        # the candidate-level flag is derived, never asserted for the whole candidate.
        stored["evidence_elicitation"] = {item["evidence_ref"]: item["elicitation"]
                                          for item in problem.get("evidence_elicitation") or []
                                          if isinstance(item, dict) and item.get("evidence_ref")}
        stored["research_prompted"] = "research_prompted" in stored["evidence_elicitation"].values()
        stored["group_evidence"] = any(ref in group for ref in problem.get("evidence_refs") or [])
        problems.append(stored)
    doc = {"schema_version": 2, "journey_digest": journey_digest(journey_data), "basis": dict(basis),
           "steps": steps, "core_problems": problems,
           "generated_at": now or dt.datetime.now().isoformat(timespec="seconds")}
    if draft.get("hmw_scope_note"):
        doc["hmw_scope_note"] = draft["hmw_scope_note"]
    if group_ids is not None:
        doc["group_evidence_limited"] = bool(limited)
    return doc


def save(run_dir: Path, doc: dict) -> None:
    run_files.write_yaml_atomic(Path(run_dir) / FILENAME, doc)


def freshness(run_dir: Path, journey_data: dict | None, persona_digest: str | None) -> dict:
    """`absent`, `current`, or `stale` with the list of what changed
    (`journey`, `persona`, `jtbd`, `hmw`). A value the researcher kept
    (`retained_basis`) counts as unchanged."""
    doc = load(run_dir)
    if doc is None or not isinstance(journey_data, dict):
        return {"state": "absent", "changed": []}
    recorded = doc.get("basis") or {}
    retained = doc.get("retained_basis") or {}
    now = current_basis(run_dir, persona_digest)
    changed = []
    journey_now = journey_digest(journey_data)
    if journey_now not in (doc.get("journey_digest"), retained.get("journey_digest")):
        changed.append("journey")
    for key in BASIS_KEYS:
        field = f"{key}_digest"
        if now.get(field) != recorded.get(field) and not (field in retained and retained.get(field) == now.get(field)):
            changed.append(key)
    return {"state": "stale" if changed else "current", "changed": changed}


def retain(run_dir: Path, journey_data: dict, persona_digest: str | None) -> dict:
    """「保留目前核心問題」: record the current upstream as accepted; no model call,
    candidates unchanged."""
    doc = load(run_dir)
    if doc is None:
        raise ValueError("還沒有核心問題候選，沒有可以保留的內容。")
    doc["retained_basis"] = {**current_basis(run_dir, persona_digest), "journey_digest": journey_digest(journey_data)}
    save(run_dir, doc)
    return doc


def candidate(run_dir: Path, wanted_id: str) -> dict | None:
    """A candidate by id: a v2 journey-level candidate, or a v1 per-step one
    (returned with its `step_id`, for the legacy per-step adoption)."""
    doc = load(run_dir) or {}
    for problem in doc.get("core_problems") or []:
        if isinstance(problem, dict) and problem.get("id") == wanted_id:
            return dict(problem)
    for entry in doc.get("steps") or []:
        item = entry.get("candidate") if isinstance(entry, dict) else None
        if isinstance(item, dict) and item.get("id") == wanted_id:
            return {**item, "step_id": entry.get("step_id")}
    return None
