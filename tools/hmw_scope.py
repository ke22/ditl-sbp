"""persona-matrix-hmw-scope-default: the selected HMW as Persona segmentation scope.

The researcher's selected HMW (station 3, `primary_hmw_selection.yaml`) is the
design-relevance background for Persona segmentation (2026-10-01 ruling). It
is never evidence, never an evidence whitelist and never a segmentation rule.
`resolve` reads the selection -- a reference to an `hmw.yaml` entry, or an
HMW the researcher wrote -- into its statement and tension; `digest` is what
a segmentation output records so the station can tell when the HMW changed.
Only the statement and tension count, so editing the rationale alone does
not mark segmentation as framed by another HMW.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

SELECTION_FILE = "primary_hmw_selection.yaml"
HMW_FILE = "hmw.yaml"
# hmw-framings-from-problem-structures: new framings carry problem_structure;
# older entries carry tension. A digest only counts the fields present, so an
# older selection keeps the digest it had.
SCOPE_FIELDS = ("statement", "problem_structure", "tension")


def _load(path: Path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None


def resolve(run_dir: Path) -> dict | None:
    """The selected HMW's statement and tension, or None when none is readable."""
    run_dir = Path(run_dir)
    selection = _load(run_dir / SELECTION_FILE)
    if not isinstance(selection, dict):
        return None
    if selection.get("hmw_id"):
        doc = _load(run_dir / HMW_FILE)
        entries = (doc or {}).get("entries") if isinstance(doc, dict) else None
        entry = next((e for e in entries or [] if isinstance(e, dict) and e.get("id") == selection["hmw_id"]), None)
        source = entry
    else:
        source = selection
    if not isinstance(source, dict) or not str(source.get("statement") or "").strip():
        return None
    return {field: str(source.get(field) or "").strip() for field in SCOPE_FIELDS if str(source.get(field) or "").strip()}


def digest(scope: dict | None) -> str | None:
    if not scope:
        return None
    payload = {field: scope.get(field) for field in SCOPE_FIELDS if scope.get(field)}
    return "sha256:" + hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def current_digest(run_dir: Path) -> str | None:
    return digest(resolve(run_dir))


def framed_digest(document: object) -> str | None:
    if not isinstance(document, dict):
        return None
    framed_by = document.get("framed_by")
    return framed_by.get("primary_hmw_selection") if isinstance(framed_by, dict) else None


def framing_state(run_dir: Path, document: object) -> str:
    """current / stale / unframed for a segmentation output."""
    recorded = framed_digest(document)
    now = current_digest(run_dir)
    if not recorded or not now:
        return "unframed"
    return "current" if recorded == now else "stale"
