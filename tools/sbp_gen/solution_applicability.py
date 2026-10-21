"""solution-reconfirmation: is an adopted Solution still applicable now?

Three things stay apart:

- history: how the Solution was formed (`solution_basis` on the step and, for
  Solutions developed from now on, `journey_solution_formation` in run.json);
  never rewritten here;
- current applicability: whether the Solution has been reviewed against the
  current journey step and the current HMW, decided by exact digests only;
- reconfirmation: the researcher's explicit decision to keep it, appended to
  `solution_reconfirmations` in run.json and bound to the exact step digest,
  HMW digest, Solution ids and Solution content it was made for.

No similarity, punctuation rule or model judgement is used, and the journey's
formation-time HMW is only an uncertainty signal for legacy Solutions -- never
presented as the Solution's own formation-time HMW.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

import formation_basis
import hmw_scope

from . import lifecycle, stages

FORMATION_KEY = "journey_solution_formation"
RECONFIRMATIONS_KEY = "solution_reconfirmations"
REASONS = ("step_changed", "hmw_changed", "hmw_unverified")


def adopted_solution_digest(step: dict) -> str:
    """Digest of the adopted Solution content (the candidates the researcher adopted)."""
    chosen = set(step.get("selected_solution_ids") or [])
    adopted = [c for c in step.get("solution_candidates") or [] if isinstance(c, dict) and c.get("id") in chosen]
    return lifecycle.canonical_content_digest(adopted)


def has_adopted_solution(step: dict) -> bool:
    return isinstance(step, dict) and step.get("develop_solution") is True and bool(step.get("selected_solution_ids")) \
        and bool(step.get("solution_candidates"))


def _applicable_reconfirmation(step: dict, run_state: dict) -> dict | None:
    """The latest reconfirmation made for exactly the Solutions adopted now."""
    records = ((run_state or {}).get(RECONFIRMATIONS_KEY) or {}).get(step.get("id")) or []
    ids, digest = list(step.get("selected_solution_ids") or []), adopted_solution_digest(step)
    for record in reversed(records if isinstance(records, list) else []):
        if isinstance(record, dict) and record.get("solution_ids") == ids and record.get("solution_digest") == digest:
            return record
    return None


def _formation(step: dict, run_state: dict) -> dict | None:
    """The formation record of the Solution adopted now, or None for a legacy Solution."""
    record = ((run_state or {}).get(FORMATION_KEY) or {}).get(step.get("id"))
    if isinstance(record, dict) and record.get("solution_ids") == list(step.get("selected_solution_ids") or []) \
            and record.get("solution_digest") == adopted_solution_digest(step):
        return record
    return None


def step_applicability(step: dict, run_state: dict, current_hmw_digest: str | None, journey_hmw_state: str) -> dict:
    """{"state": "current" | "needs_reconfirmation", "reasons", "step_digest",
    "hmw_digest", "solution_ids"} for a step with an adopted Solution."""
    step_digest = stages.journey_step_digest(step)
    reconfirmed = _applicable_reconfirmation(step, run_state)
    reasons = []
    if stages.journey_step_solutions_stale(step) and not (reconfirmed and reconfirmed.get("step_digest") == step_digest):
        reasons.append("step_changed")
    if current_hmw_digest:
        reconfirmed_hmw = (reconfirmed or {}).get("hmw_digest")
        formed = _formation(step, run_state)
        if formed and formed.get("hmw_digest"):
            if current_hmw_digest not in (formed["hmw_digest"], reconfirmed_hmw):
                reasons.append("hmw_changed")
        elif journey_hmw_state != "same" and current_hmw_digest != reconfirmed_hmw:
            reasons.append("hmw_unverified")
    return {"state": "needs_reconfirmation" if reasons else "current", "reasons": reasons,
            "step_digest": step_digest, "hmw_digest": current_hmw_digest,
            "solution_ids": list(step.get("selected_solution_ids") or [])}


def _load_yaml(path: Path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, yaml.YAMLError):
        return None


def run_state(run_dir: Path) -> dict:
    try:
        state = json.loads((Path(run_dir) / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def primary_steps(journey: object) -> list[dict]:
    if not isinstance(journey, dict):
        return []
    scenario = next((s for s in journey.get("scenarios") or []
                     if isinstance(s, dict) and s.get("id") == journey.get("primary_scenario")), {}) or {}
    return [step for step in scenario.get("steps") or [] if isinstance(step, dict)]


def current_hmw_digest(run_dir: Path) -> str | None:
    try:
        return hmw_scope.current_digest(Path(run_dir))
    except (TypeError, AttributeError, KeyError):
        return None


def journey_applicability(run_dir: Path, journey: dict | None = None) -> dict:
    """{step_id: applicability} for every selected moment with an adopted Solution,
    read from the journey the researcher is working on (the proposal, else the
    confirmed journey)."""
    run_dir = Path(run_dir)
    if journey is None:
        journey = _load_yaml(run_dir / "journey_proposal.yaml") or _load_yaml(run_dir / "journey.yaml")
    state = run_state(run_dir)
    hmw_now = current_hmw_digest(run_dir)
    journey_hmw = formation_basis.journey_formation_hmw(run_dir)["state"]
    return {step["id"]: step_applicability(step, state, hmw_now, journey_hmw)
            for step in primary_steps(journey) if step.get("id") and has_adopted_solution(step)}


def unresolved(run_dir: Path, journey: dict | None = None) -> list[str]:
    """Step ids whose adopted Solution needs reconfirmation."""
    return [sid for sid, a in journey_applicability(run_dir, journey).items() if a["state"] != "current"]


def formation_record(step: dict, hmw_digest: str | None, now: str) -> dict:
    return {"hmw_digest": hmw_digest, "step_digest": step.get("solution_basis"),
            "solution_ids": list(step.get("selected_solution_ids") or []),
            "solution_digest": adopted_solution_digest(step), "at": now}


def reconfirmation_record(step: dict, hmw_digest: str | None, now: str) -> dict:
    return {"step_digest": stages.journey_step_digest(step), "hmw_digest": hmw_digest,
            "solution_ids": list(step.get("selected_solution_ids") or []),
            "solution_digest": adopted_solution_digest(step), "at": now}


def latest_reconfirmation_at(run_dir: Path, journey: dict | None = None) -> datetime | None:
    """When the latest still-applicable reconfirmation was made, if any."""
    run_dir = Path(run_dir)
    if journey is None:
        journey = _load_yaml(run_dir / "journey_proposal.yaml") or _load_yaml(run_dir / "journey.yaml")
    state, latest = run_state(run_dir), None
    for step in primary_steps(journey):
        if not has_adopted_solution(step):
            continue
        record = _applicable_reconfirmation(step, state)
        when = parse_time((record or {}).get("at"))
        if when and (latest is None or when > latest):
            latest = when
    return latest


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    return when if when.tzinfo else when.astimezone()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
