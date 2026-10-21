"""speaker-attribution-for-persona: who said an excerpt, and how many people a
source holds, kept apart from the evidence role.

`role` is a role category ("顧客", "司機"); it never says which person spoke.
This module reads speakers only from explicit marks in the source text
(`受訪者A：`, `受訪者女：`, `同行友人：`), never from a model, and keeps them in
a side file keyed by evidence id -- evidence.yaml (text, role, source,
provenance) is not touched, so nothing downstream goes stale.

Side file `source_attribution.yaml` (one per run):

    schema_version: 1
    sources:
      - source: 信義松高店_雙人姊妹.txt
        participants: multiple        # single | multiple | unknown
        count: 2                      # null when unknown
        individual_labels: [受訪者A, 受訪者B]   # labels that each denote one person
        status: confirmed             # draft | confirmed
        labels_found: [受訪者, 受訪者A, 受訪者B]
        header: "受訪者：A、B 兩位（在附近上班／假日逛街）"
    speakers:
      E-012: 受訪者A

A respondent in the Persona Matrix is one confirmed person: evidence of a
confirmed `single` source, or of one individual speaker of a confirmed
`multiple` source. Everything else stays in the evidence for themes.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from . import run_files, stages

FILENAME = "source_attribution.yaml"
PARTICIPANTS = ("single", "multiple", "unknown")

# A labelled line: optional bullet, a short label, a full- or half-width colon.
_LABEL_LINE_RE = re.compile(r"^\s*(?:[-*]\s*)?([^\s：:，,。]{1,12})[：:](.*)$")
_INTERVIEWER_PREFIXES = ("組員", "訪談人", "訪員", "主持人", "研究者", "研究員", "訪問者", "訪談者")
_METADATA_LABELS = ("來源",)
_GENERIC_LABELS = ("受訪者", "受訪人", "顧客")


def label_kind(label: str | None) -> str:
    """interviewer / metadata / joint / generic / individual (None: no label)."""
    if not label or re.fullmatch(r"[\d\s,.:]+", label):
        return "none"  # no label, or a subtitle timecode such as 00:00:01,000
    if label.startswith(_INTERVIEWER_PREFIXES):
        return "interviewer"
    if label in _METADATA_LABELS:
        return "metadata"
    if "/" in label or "／" in label or "、" in label or "和" in label:
        return "joint"
    if label in _GENERIC_LABELS:
        return "generic"
    return "individual"


def speaking_turns(scope: str) -> list[tuple[str | None, str]]:
    """(label, text) per speaking turn. A labelled line starts a turn; the
    lines after it belong to it until the next labelled line. Text before any
    label is one unlabelled turn (subtitle transcripts are all unlabelled)."""
    turns: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in (scope or "").splitlines():
        match = _LABEL_LINE_RE.match(line)
        if match and label_kind(match.group(1)) != "none":
            turns.append((match.group(1), [match.group(2)]))
        else:
            turns[-1][1].append(line)
    return [(label, "\n".join(lines)) for label, lines in turns if "".join(lines).strip()]


def speaker_label_for(text: str, turns: list[tuple[str | None, str]]) -> str | None:
    """The one speaker whose turn holds the excerpt, or None when that is not
    certain: no turn holds it whole, turns of several labels hold it, or the
    turn is unlabelled, an interviewer's, or names several people."""
    if not str(text or "").strip():
        return None
    labels = {label for label, body in turns if stages._is_verbatim_excerpt(text, body)}
    if len(labels) != 1:
        return None
    label = next(iter(labels))
    return label if label_kind(label) in ("individual", "generic") else None


def derive_speakers(evidence: list, raw_text: str) -> dict[str, str]:
    """{evidence id: speaker label}, only where the source marks the speaker."""
    scopes = stages.source_scope_map(raw_text)
    turns_by_source = {name: speaking_turns(scope) for name, scope in scopes.items()}
    out: dict[str, str] = {}
    for item in evidence or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        label = speaker_label_for(item.get("text"), turns_by_source.get(item.get("source"), []))
        if label:
            out[item["id"]] = label
    return out


def _header(scope: str) -> str:
    for line in (scope or "").splitlines()[:6]:
        match = _LABEL_LINE_RE.match(line)
        if match and match.group(1) in _GENERIC_LABELS and not line.lstrip().startswith(("-", "*")):
            return line.strip()
    return ""


def draft_sources(evidence: list, raw_text: str) -> list[dict]:
    """One draft entry per evidence source, from explicit marks only: two or
    more individual labels suggest `multiple`; anything else is `unknown`.
    A source is never drafted as `single`."""
    scopes = stages.source_scope_map(raw_text)
    names = list(dict.fromkeys(e.get("source") for e in evidence or [] if isinstance(e, dict) and e.get("source")))
    entries = []
    for name in names:
        turns = speaking_turns(scopes.get(name, ""))
        found = sorted({label for label, _ in turns if label and label_kind(label) not in ("interviewer", "metadata")})
        individual = [label for label in found if label_kind(label) == "individual"]
        multiple = len(individual) >= 2
        entries.append({
            "source": name,
            "participants": "multiple" if multiple else "unknown",
            "count": len(individual) if multiple else None,
            "individual_labels": individual if multiple else [],
            "status": "draft",
            "labels_found": found,
            "header": _header(scopes.get(name, "")),
        })
    return entries


def merge_draft(previous: dict | None, drafted: list[dict], speakers: dict[str, str], case_id: str) -> dict:
    """A new side file: the fresh draft and speakers, with every entry the
    researcher already confirmed kept as it was."""
    confirmed = {s["source"]: s for s in (previous or {}).get("sources") or []
                 if isinstance(s, dict) and s.get("status") == "confirmed"}
    sources = []
    for entry in drafted:
        kept = confirmed.get(entry["source"])
        sources.append({**entry, **{k: kept[k] for k in ("participants", "count", "individual_labels", "status") if k in kept}}
                       if kept else entry)
    return {"schema_version": 1, "case_id": case_id, "sources": sources, "speakers": dict(sorted(speakers.items()))}


def confirm(doc: dict, source: str, participants: str, count: int | None = None,
            individual_labels: list[str] | None = None) -> dict:
    """The researcher's confirmation of one source."""
    if participants not in PARTICIPANTS:
        raise ValueError(f"participants 只能是 {'／'.join(PARTICIPANTS)}")
    entries = doc.get("sources") or []
    entry = next((s for s in entries if s.get("source") == source), None)
    if entry is None:
        raise ValueError(f"這個 run 的證據裡沒有來源「{source}」")
    labels = list(individual_labels or [])
    unknown = [label for label in labels if label not in (entry.get("labels_found") or [])]
    if unknown:
        raise ValueError(f"「{source}」的原文沒有這些講者標記：{'、'.join(unknown)}")
    entry.update({"participants": participants, "count": count if participants == "multiple" else (1 if participants == "single" else None),
                  "individual_labels": labels if participants == "multiple" else [], "status": "confirmed"})
    return doc


def load(run_dir: Path) -> dict | None:
    path = Path(run_dir) / FILENAME
    if not path.is_file():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def save(run_dir: Path, doc: dict) -> None:
    run_files.write_yaml_atomic(Path(run_dir) / FILENAME, doc)


def person_key(item: dict, doc: dict) -> str | None:
    """The confirmed person an evidence item belongs to, or None."""
    source = item.get("source")
    entry = next((s for s in doc.get("sources") or [] if s.get("source") == source), None)
    if not entry or entry.get("status") != "confirmed":
        return None
    if entry.get("participants") == "single":
        return source
    if entry.get("participants") == "multiple":
        label = (doc.get("speakers") or {}).get(item.get("id"))
        return f"{source}::{label}" if label and label in (entry.get("individual_labels") or []) else None
    return None


def _subject_keys(subject: dict, evidence_by_id: dict, doc: dict) -> tuple[list[str], set[str]]:
    unattributed, keys = [], set()
    for ref in subject.get("evidence_refs") or []:
        item = evidence_by_id.get(ref)
        key = person_key(item, doc) if isinstance(item, dict) else None
        if key is None:
            unattributed.append(ref)
        else:
            keys.add(key)
    return unattributed, keys


def _mixes_speakers(keys: set[str]) -> bool:
    """Two speakers of a multiple-participant source, or one with another person."""
    split = [key for key in keys if "::" in key]
    return bool(split) and len(keys) > 1


def persona_attribution_errors(persona_doc: object, evidence: list, doc: dict | None) -> list[str]:
    """Respondents that cite non-attributable evidence or mix speakers. No
    side file: no check (legacy runs behave as before)."""
    if doc is None or not isinstance(persona_doc, dict):
        return []
    evidence_by_id = {e["id"]: e for e in evidence or [] if isinstance(e, dict) and e.get("id")}
    errors = []
    for subject in persona_doc.get("subjects") or []:
        if not isinstance(subject, dict):
            continue
        unattributed, keys = _subject_keys(subject, evidence_by_id, doc)
        if unattributed:
            sources = sorted({str(evidence_by_id.get(r, {}).get("source")) for r in unattributed})
            errors.append(f"[persona_attribution] subject {subject.get('id')} 引用了無法確認是誰說的證據："
                          f"{'、'.join(unattributed[:8])}（來源：{'、'.join(sources)}）。"
                          "多人來源沒有個人講者標記、或來源尚未確認的證據，保留給主題分析，不放進任何受訪者")
        if _mixes_speakers(keys):
            errors.append(f"[persona_attribution] subject {subject.get('id')} 混入了不同講者的證據："
                          f"{'、'.join(sorted(keys))}；一位受訪者只能是一個人")
    return errors


def confirmed_persons(doc: dict) -> list[str]:
    persons = []
    for entry in doc.get("sources") or []:
        if entry.get("status") != "confirmed":
            continue
        if entry.get("participants") == "single":
            persons.append(entry["source"])
        elif entry.get("participants") == "multiple":
            persons += [f"{entry['source']}::{label}" for label in entry.get("individual_labels") or []]
    return persons


def attribution_report(persona_doc: dict | None, evidence: list, doc: dict | None) -> dict:
    """What the Persona station shows: confirmed-person coverage against the
    confirmed persons, non-attributable evidence by source, and respondents
    that may merge several people. Never changes any file."""
    if doc is None or not any(s.get("status") == "confirmed" for s in doc.get("sources") or []):
        # Nothing confirmed yet: every respondent would look suspect, which says
        # nothing about the Persona -- say what is missing instead.
        return {"state": "unconfirmed", "message": "這個專案還沒有確認各來源的受訪人數與講者，無法判斷 Persona 是否把多人當成一人。",
                **({"drafted_sources": len(doc.get("sources") or [])} if doc else {})}
    evidence_by_id = {e["id"]: e for e in evidence or [] if isinstance(e, dict) and e.get("id")}
    persons = confirmed_persons(doc)
    unconfirmed = [s["source"] for s in doc.get("sources") or [] if s.get("status") != "confirmed"]
    unattributed: dict[str, int] = {}
    for item in evidence_by_id.values():
        if person_key(item, doc) is None:
            unattributed[str(item.get("source"))] = unattributed.get(str(item.get("source")), 0) + 1
    placed, suspect = set(), []
    for subject in (persona_doc or {}).get("subjects") or []:
        if not isinstance(subject, dict):
            continue
        bad, keys = _subject_keys(subject, evidence_by_id, doc)
        if bad or _mixes_speakers(keys):
            suspect.append({"subject": subject.get("id"), "unattributed_evidence": len(bad),
                            "sources": sorted({str(evidence_by_id.get(r, {}).get("source")) for r in bad} | {k.split("::")[0] for k in keys})})
            continue
        if any(p.get("position") not in ("no_data", None) for p in subject.get("placements") or [] if isinstance(p, dict)):
            placed |= keys
    return {
        "state": "confirmed" if not unconfirmed else "partly_confirmed",
        "confirmed_persons": len(persons),
        "placed_persons": len(placed & set(persons)),
        "unattributed_evidence": sum(unattributed.values()),
        "unattributed_by_source": dict(sorted(unattributed.items())),
        "suspect_subjects": suspect,
        "unconfirmed_sources": unconfirmed,
    }


def generation_block(evidence: list, doc: dict | None) -> tuple[str, dict[str, dict]]:
    """For Persona Matrix input: a section stating each source's confirmed
    participants, and per evidence id the extra unit fields (`speaker`, or
    `attributable: false`)."""
    if doc is None:
        return "", {}
    lines = []
    for entry in doc.get("sources") or []:
        state = (entry.get("participants") if entry.get("status") == "confirmed" else "unknown")
        lines.append({"source": entry.get("source"), "participants": state,
                      **({"individual_speakers": entry.get("individual_labels")} if state == "multiple" else {})})
    extra: dict[str, dict] = {}
    for item in evidence or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        key = person_key(item, doc)
        if key is None:
            extra[item["id"]] = {"attributable": False}
        elif "::" in key:
            extra[item["id"]] = {"speaker": key.split("::", 1)[1]}
    block = ("## 來源受訪者資訊（研究者確認；participants 為 unknown 的來源視為無法確認是誰）\n```yaml\n"
             + stages._yaml_dump(lines) + "```\n\n")
    return block, extra
