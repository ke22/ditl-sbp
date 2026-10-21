"""formation-time-basis-display: what an existing artifact was formed from.

A screen must not present the researcher's current upstream choice as the
basis of something formed earlier. This module answers, from stored data only,
whether the formation-time basis can be verified -- and says "unknown" when it
cannot. It never reconstructs an earlier choice, never reads backup files, and
never writes. Digests are the ones the writers recorded, computed by the same
functions (`narrative_framing`, `hmw_scope`), so "the same" can never drift.
"""
from __future__ import annotations

import json
from pathlib import Path

import hmw_scope
import narrative_framing


def hmw_jtbd_basis_verified(run_dir: Path, hmw_doc: object) -> bool:
    """True only when the HMW output's recorded JTBD-selection digest equals
    the current selection's digest, so its positional `JTBD-n` refs still
    point at the JTBDs it was formed from."""
    recorded = narrative_framing.framed_digest(hmw_doc)
    return bool(recorded) and recorded == narrative_framing.current_digest(Path(run_dir))


def _scope_of(source: object) -> dict | None:
    """The HMW scope of an `hmw.yaml` entry or a researcher-written selection,
    with the same field rule as `hmw_scope.resolve`."""
    if not isinstance(source, dict) or not str(source.get("statement") or "").strip():
        return None
    return {field: str(source.get(field) or "").strip()
            for field in hmw_scope.SCOPE_FIELDS if str(source.get(field) or "").strip()}


def _recorded_journey_hmw_digest(run_dir: Path) -> str | None:
    try:
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    lens = state.get("journey_persona_lens") if isinstance(state, dict) else None
    digest = lens.get("hmw_digest") if isinstance(lens, dict) else None
    return digest if isinstance(digest, str) and digest else None


def journey_formation_hmw(run_dir: Path) -> dict:
    """{"state": "same" | "verified" | "unknown", "formation", "current"}.

    `same`: the journey was built for the currently selected HMW.
    `verified`: exactly one current `hmw.yaml` entry (or the current
    researcher-written HMW) has the recorded digest.
    `unknown`: nothing recorded, nothing or more than one matched, or the
    files cannot be read."""
    run_dir = Path(run_dir)
    try:
        current_scope = hmw_scope.resolve(run_dir)
    except (TypeError, AttributeError, KeyError):  # malformed hmw.yaml or selection
        return {"state": "unknown", "formation": None, "current": None}
    current = (current_scope or {}).get("statement")
    recorded = _recorded_journey_hmw_digest(run_dir)
    if recorded is None:
        return {"state": "unknown", "formation": None, "current": current}
    if recorded == hmw_scope.digest(current_scope):
        return {"state": "same", "formation": current, "current": current}
    doc = hmw_scope._load(run_dir / hmw_scope.HMW_FILE)
    entries = doc.get("entries") if isinstance(doc, dict) else None
    candidates = [_scope_of(entry) for entry in entries] if isinstance(entries, list) else []
    selection = hmw_scope._load(run_dir / hmw_scope.SELECTION_FILE)
    if isinstance(selection, dict) and not selection.get("hmw_id"):
        candidates.append(_scope_of(selection))
    matches = [scope for scope in candidates if scope and hmw_scope.digest(scope) == recorded]
    if len(matches) != 1:
        return {"state": "unknown", "formation": None, "current": current}
    return {"state": "verified", "formation": matches[0]["statement"], "current": current}
