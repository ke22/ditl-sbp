"""Researcher context (purpose-aware-research-calibration, 2026-10-22).

What a human research team carries into a study and the system had no place
for: the project purpose (what the client wants to change), the research
purpose (what this study must find out) and business inputs that are not
participant evidence -- survey results, word of mouth, desk research,
stakeholder briefs. A concern the researcher cares about (e.g. 永續, 品牌) is
written inside a purpose; stages report on it quoting the purpose verbatim
(2026-10-23 researcher ruling: no separate issues list).

- The master copy is `project_context.yaml` in the case folder; each coding run
  copies it into the run directory, beside `research_purpose.yaml`.
- The research purpose is mirrored to `research_purpose.yaml` so every earlier
  reader keeps working unchanged.
- Downstream stages read the context as framing and priority, never as
  evidence: `framing_block()` is the one place the section is assembled, and
  `validate_sbp.context_ids_never_in_evidence_refs` rejects a `BI-` (or a
  legacy `ISS-`) id cited as evidence.
- An `issues` list in a document written before 2026-10-23 is compatibility
  only: `load()` ignores it, no stage receives it, the next save drops it.
- A run without `project_context.yaml` behaves exactly as before: `load()`
  falls back to the research purpose alone and `framing_block()` is empty.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import lifecycle

FILE = "project_context.yaml"
PURPOSE_FILE = "research_purpose.yaml"  # the legacy mirror, owned by selective_themes
PROJECT_PURPOSE_MAX_CHARS = 200
RESEARCH_PURPOSE_MAX_CHARS = 120
SUMMARY_MAX_CHARS = 300
BUSINESS_KINDS = ("survey", "word_of_mouth", "desk_research", "stakeholder_brief", "other")
HEADING = "## 研究者帶進場的目的（不是證據；決定優先順序與範圍，不能引用為證據）"
KIND_LABELS = {"survey": "問卷", "word_of_mouth": "口碑", "desk_research": "桌面研究",
               "stakeholder_brief": "利害關係人要求", "other": "其他"}
CONTEXT_ID_RE = re.compile(r"^(ISS|BI)-[1-9][0-9]*$")  # ISS- only in legacy documents


class ContextError(ValueError):
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


def empty(research_purpose: str = "") -> dict:
    return {"schema_version": 1, "project_purpose": "", "research_purpose": research_purpose,
            "business_inputs": [], "source": "researcher", "updated_at": ""}


def exists(folder: str | Path) -> bool:
    return (Path(folder) / FILE).is_file()


def _legacy_purpose(folder: Path) -> str:
    doc = _load(folder / PURPOSE_FILE) or {}
    return str(doc.get("text") or "").strip() if isinstance(doc, dict) else ""


def load(folder: str | Path) -> dict:
    """The folder's researcher context; without the file, only the legacy
    research purpose, every other field empty."""
    folder = Path(folder)
    doc = _load(folder / FILE)
    if not isinstance(doc, dict):
        return empty(_legacy_purpose(folder))
    merged = empty()
    merged.update({k: v for k, v in doc.items() if k in merged})
    return merged


def _next_id(prefix: str, items: list[dict]) -> str:
    taken = [int(m.group(1)) for item in items
             for m in [re.match(rf"^{prefix}-([1-9][0-9]*)$", str(item.get("id") or ""))] if m]
    return f"{prefix}-{max(taken, default=0) + 1}"


def _clean(doc: dict) -> dict:
    """Validate and normalise a researcher-written document; ids are kept once
    given, so outputs that cite them stay resolvable."""
    project = str(doc.get("project_purpose") or "").strip()
    research = str(doc.get("research_purpose") or "").strip()
    if len(project) > PROJECT_PURPOSE_MAX_CHARS:
        raise ContextError(f"專案目的請寫在 {PROJECT_PURPOSE_MAX_CHARS} 字以內。")
    if len(research) > RESEARCH_PURPOSE_MAX_CHARS:
        raise ContextError(f"研究目的請寫在 {RESEARCH_PURPOSE_MAX_CHARS} 字以內。")
    inputs: list[dict] = []
    for raw in doc.get("business_inputs") or []:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "other")
        if kind not in BUSINESS_KINDS:
            raise ContextError(f"業務輸入的類型不正確：{kind}")
        summary = str(raw.get("summary") or "").strip()
        if len(summary) > SUMMARY_MAX_CHARS:
            raise ContextError(f"業務輸入的摘要請寫在 {SUMMARY_MAX_CHARS} 字以內。")
        given = str(raw.get("id") or "")
        inputs.append({"id": given if re.match(r"^BI-[1-9][0-9]*$", given) else "", "kind": kind,
                       "summary": summary, "source_file": str(raw.get("source_file") or ""),
                       "added_at": str(raw.get("added_at") or "") or _now()})
    for item in inputs:
        if not item["id"]:
            item["id"] = _next_id("BI", [i for i in inputs if i["id"]])
    return {"schema_version": 1, "project_purpose": project, "research_purpose": research,
            "business_inputs": inputs, "source": "researcher", "updated_at": _now()}


def save(folder: str | Path, doc: dict) -> dict:
    """Write the researcher context and mirror the research purpose to the
    legacy file with the same timestamp. An empty research purpose removes the
    legacy file, so `selective_themes.read_purpose` reads it as empty."""
    folder = Path(folder)
    clean = _clean(doc)
    _write(folder / FILE, clean)
    legacy = folder / PURPOSE_FILE
    if clean["research_purpose"]:
        # Rewritten only when the text changes, so the purpose's identity on
        # screen is not invalidated by saving some other field.
        if _legacy_purpose(folder) != clean["research_purpose"]:
            _write(legacy, {"schema_version": 1, "text": clean["research_purpose"], "source": "researcher",
                            "updated_at": clean["updated_at"]})
    elif legacy.is_file():
        legacy.unlink()
    return clean


def update(folder: str | Path, **fields) -> dict:
    """Change some fields of the folder's context and save it."""
    doc = load(folder)
    doc.update({k: v for k, v in fields.items() if k in doc})
    return save(folder, doc)


def add_business_input(folder: str | Path, kind: str, source_file: str) -> dict:
    """A non-evidence upload becomes a business input awaiting the researcher's
    summary. Adding the same file twice keeps one item."""
    if kind not in BUSINESS_KINDS:
        raise ContextError(f"業務輸入的類型不正確：{kind}")
    doc = load(folder)
    if any(item.get("source_file") == source_file for item in doc["business_inputs"]):
        return doc
    doc["business_inputs"].append({"kind": kind, "summary": "", "source_file": source_file, "added_at": _now()})
    return save(folder, doc)


def register_business_inputs(case_folder: str | Path) -> dict:
    """Every upload marked with a non-evidence kind has a business input,
    awaiting its summary. Idempotent; a case without kinds is left untouched."""
    from . import ingest
    folder = Path(case_folder)
    pending = {name: kind for name, kind in ingest.load_source_kinds(folder).items()
               if kind not in ingest.EVIDENCE_KINDS}
    if not pending:
        return load(folder)
    doc = load(folder)
    known = {i.get("source_file") for i in doc["business_inputs"]}
    added = [{"kind": ingest.BUSINESS_KINDS.get(kind, "other"), "summary": "", "source_file": name, "added_at": _now()}
             for name, kind in sorted(pending.items()) if name not in known]
    if not added:
        return doc
    doc["business_inputs"] += added
    return save(folder, doc)


def copy_into_run(case_folder: str | Path, run_dir: str | Path) -> bool:
    """At the start of coding, beside the research purpose. Nothing is written
    when the case has no context file."""
    source = Path(case_folder) / FILE
    if not source.is_file():
        return False
    Path(run_dir).mkdir(parents=True, exist_ok=True)
    (Path(run_dir) / FILE).write_bytes(source.read_bytes())
    return True


def digest(run_dir: str | Path) -> str | None:
    """What a downstream step depends on: the context file's content; without
    it the legacy research purpose; without either, None. `updated_at` is
    left out, so saving unchanged text does not stale anything."""
    run = Path(run_dir)
    doc = _load(run / FILE)
    if isinstance(doc, dict):
        return lifecycle.canonical_content_digest({k: v for k, v in doc.items() if k != "updated_at"})
    purpose = _legacy_purpose(run)
    return lifecycle.canonical_content_digest(purpose) if purpose else None


def summarised_inputs(doc: dict) -> list[dict]:
    return [i for i in doc.get("business_inputs") or [] if str(i.get("summary") or "").strip()]


def has_framing(doc: dict, *, include_research_purpose: bool = True) -> bool:
    return bool(doc.get("project_purpose") or summarised_inputs(doc)
                or (include_research_purpose and doc.get("research_purpose")))


def framing_block(run_dir: str | Path, *, include_research_purpose: bool = True) -> str:
    """The one section every framed stage receives. Empty when the run has no
    context file, or nothing in it to frame with -- so the stage input is
    byte-for-byte what it was before this capability. Stages that already print
    the research purpose pass include_research_purpose=False."""
    run = Path(run_dir)
    if not exists(run):
        return ""
    doc = load(run)
    if not has_framing(doc, include_research_purpose=include_research_purpose):
        return ""
    lines = [HEADING, ""]
    if doc["project_purpose"]:
        lines.append(f"專案目的：{doc['project_purpose']}")
    if include_research_purpose and doc["research_purpose"]:
        lines.append(f"研究目的：{doc['research_purpose']}")
    inputs = summarised_inputs(doc)
    if inputs:
        lines.append("")
        lines.append("業務輸入（研究者寫的摘要，不是受訪者說的話）：")
        lines += [f"- {i['id']}（{KIND_LABELS.get(i['kind'], i['kind'])}）：{i['summary']}" for i in inputs]
    return "\n".join(lines) + "\n\n"


def purposes(folder: str | Path) -> list[str]:
    """The non-empty purposes, project first -- what a reported `focus` must
    quote verbatim. Falls back to the legacy research purpose like `load()`."""
    doc = load(folder)
    return [text for text in (doc["project_purpose"], doc["research_purpose"]) if text]


def business_input_ids(run_dir: str | Path) -> set[str]:
    run = Path(run_dir)
    return {i["id"] for i in summarised_inputs(load(run))} if exists(run) else set()


def is_context_id(value: object) -> bool:
    return bool(CONTEXT_ID_RE.match(str(value or "")))
