"""form-hmw-from-confirmed-narrative: the explicit Form HMW action.

One job runs the HMW method steps -- category properties, the HMW framings,
the primary HMW candidates -- and stops at the researcher's HMW choice
(multi-jtbd-single-job-source, HMW method v9). Category properties are method
work: inspectable, not gated, and never read the selected JTBDs, which frame
only the two HMW steps (agent-pack transition `form_hmw`).

Reuse follows what each step actually reads, not where it sits in the order:
a step is regenerated only when its own output is missing or incomplete, its
method version moved on, or one of its declared dependencies changed since it
was produced. A step that does not read a changed input is never invalidated
by it.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Callable

import yaml

import narrative_framing
import validate_sbp as V

from . import run_state, stages

# multi-jtbd-single-job-source (2026-10-03): one JTBD source. The separate
# jtbd and job_map steps of the v8 implementation are retired; the HMW step
# (v9) keeps three analytical functions -- observed behaviour from Axial
# relationships and category properties, the researcher-defined user-need
# scope from Selective themes and the selected JTBD set, and tension framing.
STEPS = ("category_properties", "how_might_we", "primary_hmw_candidates")
STEP_FILES = {
    "category_properties": "category_properties.yaml",
    "how_might_we": "hmw.yaml",
    "primary_hmw_candidates": "primary_hmw_candidates.yaml",
}
# Each single-step command's own state and failure fields in run.json.
STATE_KEYS = {
    "category_properties": "category_properties_state",
    "how_might_we": "hmw_state",
    "primary_hmw_candidates": "primary_hmw_candidates_state",
}
FAILURE_KEYS = {step: key.removesuffix("_state") + "_failure" for step, key in STATE_KEYS.items()}
# Scheduling follows DEPENDENCIES below (see run()).

FRAMING = "confirmed_jtbd"
# What each step actually reads. Axial Coding and evidence are the factual
# backbone; the selected JTBD set is framing and reaches only the two HMW steps.
DEPENDENCIES = {
    "category_properties": ("axial_coding", "evidence"),
    "how_might_we": ("axial_coding", "category_properties", "selective_themes", "evidence", FRAMING),
    "primary_hmw_candidates": ("how_might_we", "axial_coding", FRAMING),
}
# Raise a step's version when its prompt, schema or validator changes so that
# earlier outputs no longer satisfy the method. A prompt digest is not used:
# a wording tweak must not silently spend quota re-forming old projects.
METHOD_VERSIONS = {step: 1 for step in STEPS}
# how_might_we history -- 2: hmw-lenses-diversify-confirmed-framing grounds the
# confirmed framing in its categories' evidence and no longer lets a missing
# lens force zero entries; 3: hmw-seek-three-distinct-framings aims for up to
# three materially different framings and explains when it returns fewer;
# 4: hmw-exactly-three-framing-roles makes a successful result exactly one
# entry per framing role, with researcher-readable text; 5: form-hmw-tension-first
# (2026-10-01) finds tensions first within the selected JTBD's scope, retires the
# framing roles, the category whitelist, priority and convergence;
# 6: hmw-framings-from-problem-structures synthesizes tensions into problem
# structures and writes one to three project-level framings;
# 7: hmw-statement-user-action-outcome makes every statement carry who, the
# intended action or judgment, and the desired outcome;
# 8: hmw-statement-realization keeps the three parts as semantics, not a
# clause template, and dedupes the instruction;
# 9: multi-jtbd-single-job-source (2026-10-03) -- the selected JTBD set is one
# design scope; the v8 implementation of separate jtbd/job_map lens artifacts
# is retired, and the three analytical functions (current behaviour, user-need
# scope, tension framing) are performed inside this step.
# 10: the same change's follow-up (researcher ruling 2026-10-03, after the
# first v9 formation wrote one framing per selected JTBD) -- the selected set
# is one design scope: compare the JTBDs for shared conditions and tensions
# first, self-check against one-to-one framings; guidance, no validator.
# 12: researcher correction of v10 (2026-10-03), closer to the original DITL
# step 「歸納並轉為 HMW」: induce framings from the selected JTBDs, allow but
# never force integration, one main action per framing (no chaining), and
# record supporting_jtbd_refs. (11 is left to the parked
# hmw-design-scope-synthesis change; this is not that design.)
METHOD_VERSIONS["how_might_we"] = 12
# primary_hmw_candidates history -- 2: present all three framings, unranked;
# 3: hmw-candidate-rationale-boundary stops the rationale at evidence and scope;
# 4: the same change also forbids raw internal ids in the rationale;
# 5: form-hmw-tension-first drops the priority ordering and role wording;
# 6: hmw-framings-from-problem-structures makes it a deterministic projection.
METHOD_VERSIONS["primary_hmw_candidates"] = 6
# Outputs written before this action existed carry no version; they can only
# be adopted while a step is still at this version.
LEGACY_BASELINE_VERSION = 1

STAGE_LABELS = {
    "category_properties": "屬性與面向",
    "how_might_we": "HMW 張力",
    "primary_hmw_candidates": "HMW 候選",
}

# 2026-10-03 researcher ruling: an old-method run stays readable, but the
# new-method selection and HMW formation are not offered until re-coding.
LEGACY_METHOD_MESSAGE = "這份分析使用舊方法。重新編碼後才能使用多選 JTBD 與新版 HMW。"

Runner = Callable[[list], int]


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _load(path: Path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None


def _read_state(run_dir: Path) -> dict:
    path = Path(run_dir) / "run.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def formation_state(run_dir: Path) -> dict:
    state = _read_state(run_dir).get("hmw_formation")
    return state if isinstance(state, dict) else {}


def update_state(run_dir: Path, **fields) -> dict:
    """Merge fields into run.json's `hmw_formation`, rereading first: the
    single-step commands rewrite run.json between our writes."""
    merged: dict = {}

    def mutate(state: dict) -> None:
        formation = state.get("hmw_formation") if isinstance(state.get("hmw_formation"), dict) else {}
        formation.update(fields)
        state["hmw_formation"] = formation
        merged.update(formation)

    run_state.update(Path(run_dir) / "run.json", mutate)
    return merged


def _record_step(run_dir: Path, step: str, record: dict) -> None:
    def mutate(state: dict) -> None:
        formation = state.get("hmw_formation") if isinstance(state.get("hmw_formation"), dict) else {}
        steps = dict(formation.get("steps") or {})
        steps[step] = record
        formation["steps"] = steps
        state["hmw_formation"] = formation

    run_state.update(Path(run_dir) / "run.json", mutate)


def dependency_digest(run_dir: Path, name: str) -> str | None:
    run_dir = Path(run_dir)
    if name == FRAMING:
        return narrative_framing.current_digest(run_dir)
    file_name = {"axial_coding": "axial_coding.yaml", "evidence": "evidence.yaml",
                 "selective_themes": "selective_themes.yaml"}.get(name) or STEP_FILES.get(name)
    path = run_dir / file_name
    return _sha(path.read_bytes()) if path.is_file() else None


def _dependency_digests(run_dir: Path, step: str) -> dict:
    return {name: dependency_digest(run_dir, name) for name in DEPENDENCIES[step]}


def _no_tensions(run_dir: Path) -> bool:
    hmw = _load(Path(run_dir) / STEP_FILES["how_might_we"])
    return isinstance(hmw, dict) and not (hmw.get("entries") or [])


def _legacy_errors(run_dir: Path, step: str) -> list[str]:
    """The step's current validator, checked against the current dependencies."""
    run_dir = Path(run_dir)
    doc = _load(run_dir / STEP_FILES[step])
    axial = _load(run_dir / "axial_coding.yaml") or {}
    evidence = _load(run_dir / "evidence.yaml") or []
    category_ids = {c["id"] for c in axial.get("categories") or [] if isinstance(c, dict) and c.get("id")}
    evidence_ids = {e["id"] for e in evidence if isinstance(e, dict) and e.get("id")}
    if step == "category_properties":
        return V.category_properties_errors(doc, category_ids, evidence_ids)
    if step == "how_might_we":
        return stages._how_might_we_errors(doc, category_ids, evidence_ids)
    hmw = _load(run_dir / STEP_FILES["how_might_we"]) or {}
    hmw_ids = {e["id"] for e in hmw.get("entries") or [] if isinstance(e, dict) and e.get("id")}
    return stages._primary_hmw_candidates_errors(doc, hmw_ids)


def assess(run_dir: Path, step: str, regenerated: set) -> tuple[str, str]:
    """(`reuse` | `adopt` | `regenerate`, reason) for one step, given the steps
    already regenerated in this formation."""
    run_dir = Path(run_dir)
    if step == "primary_hmw_candidates" and "how_might_we" not in regenerated and _no_tensions(run_dir):
        return "reuse", "no_tensions"
    if not (run_dir / STEP_FILES[step]).is_file():
        return "regenerate", "missing"
    if _read_state(run_dir).get(STATE_KEYS[step]) != "complete":
        return "regenerate", "incomplete"
    record = (formation_state(run_dir).get("steps") or {}).get(step)
    if isinstance(record, dict) and isinstance(record.get("dependencies"), dict):
        if record.get("adopted_legacy"):
            if METHOD_VERSIONS[step] != LEGACY_BASELINE_VERSION:
                return "regenerate", "method_version"
        elif record.get("method_version") != METHOD_VERSIONS[step]:
            return "regenerate", "method_version"
        for name in DEPENDENCIES[step]:
            if record["dependencies"].get(name) != dependency_digest(run_dir, name):
                return "regenerate", f"dependency:{name}"
        return "reuse", "fresh"
    # Written before this action existed: no version, no dependency digests.
    if METHOD_VERSIONS[step] != LEGACY_BASELINE_VERSION:
        return "regenerate", "method_version"
    for name in DEPENDENCIES[step]:
        if name in regenerated:
            return "regenerate", f"dependency:{name}"
    if FRAMING in DEPENDENCIES[step]:
        doc = _load(run_dir / STEP_FILES[step])
        if narrative_framing.framed_digest(doc) != dependency_digest(run_dir, FRAMING):
            return "regenerate", f"dependency:{FRAMING}"
    if _legacy_errors(run_dir, step):
        return "regenerate", "legacy_invalid"
    return "adopt", "legacy_valid"


def plan(run_dir: Path) -> list[dict]:
    """What a formation would do now, without running anything. A step whose
    own record is fresh but that reads a step about to be regenerated is
    `recheck`: it is judged again against that step's new output."""
    upcoming: set = set()
    result = []
    for step in STEPS:
        verdict, reason = assess(run_dir, step, upcoming)
        if verdict == "regenerate":
            action = "regenerate"
            upcoming.add(step)
        elif any(name in upcoming for name in DEPENDENCIES[step]):
            action = "recheck"
            upcoming.add(step)
        else:
            action = "reuse"
        result.append({"stage": step, "label": STAGE_LABELS[step], "action": action, "reason": reason})
    return result


SELECTION_FILE = "primary_hmw_selection.yaml"


def _entry_content(entry: dict) -> str:
    """What the researcher saw and chose: the statement and its tension."""
    return _sha(json.dumps({key: entry.get(key) for key in ("statement", "tension")},
                           ensure_ascii=False, sort_keys=True).encode("utf-8"))


def selected_generated_content(run_dir: Path) -> dict | None:
    """The selected AI-candidate HMW as {hmw_id, content}, or None when there is
    no selection or the researcher wrote their own (never retired by a re-form)."""
    selection = _load(Path(run_dir) / SELECTION_FILE)
    if not isinstance(selection, dict) or not selection.get("hmw_id"):
        return None
    hmw = _load(Path(run_dir) / STEP_FILES["how_might_we"]) or {}
    entry = next((e for e in hmw.get("entries") or [] if isinstance(e, dict) and e.get("id") == selection["hmw_id"]), None)
    return {"hmw_id": selection["hmw_id"], "content": _entry_content(entry) if entry else None}


def retire_changed_selection(run_dir: Path, before: dict | None) -> bool:
    """Retire a generated HMW selection whose content changed or disappeared.

    A researcher-confirmed HMW must not silently become a different statement;
    the same id with different content is a different choice. Unchanged
    content keeps the selection. The retired file is kept as a recovery copy."""
    run_dir = Path(run_dir)
    if not before or before.get("content") is None:
        return False
    selection_path = run_dir / SELECTION_FILE
    if not selection_path.is_file():
        return False
    hmw = _load(run_dir / STEP_FILES["how_might_we"]) or {}
    entry = next((e for e in hmw.get("entries") or [] if isinstance(e, dict) and e.get("id") == before["hmw_id"]), None)
    if entry is not None and _entry_content(entry) == before["content"]:
        return False
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    selection_path.rename(selection_path.with_name(f"{SELECTION_FILE}.bak-{stamp}-retired-after-hmw-formation"))
    state = _read_state(run_dir)
    state.pop("primary_hmw_selection_state", None)
    (run_dir / "run.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    update_state(run_dir, selection_retired=True)
    return True


def readiness(run_dir: Path) -> dict | None:
    """The first deterministic, file-level reason Form HMW cannot start, or
    None. Runner availability and job exclusivity are the server's checks."""
    run_dir = Path(run_dir)
    not_confirmed = {"reason": "narrative_not_confirmed", "error": "還沒有選定顧客任務，先回到資料與編碼選一則或多則。"}
    selection = _load(run_dir / narrative_framing.SELECTION_FILE)
    if not isinstance(selection, dict):
        return not_confirmed
    axial = _load(run_dir / "axial_coding.yaml")
    if not isinstance(axial, dict) or not (run_dir / "evidence.yaml").is_file():
        return {"reason": "coding_incomplete", "error": "這份產出的編碼還沒有完成，先回到資料與編碼完成編碼。"}
    # multi-jtbd-single-job-source: new-method HMW needs Selective Coding; a
    # legacy run keeps its existing HMW and cannot be re-formed this way.
    themes = _load(run_dir / "selective_themes.yaml")
    if not isinstance(themes, dict):
        from . import selective_themes as selective_module
        if selective_module.freshness(run_dir).get("state") == "legacy":
            return {"reason": "selective_missing", "error": LEGACY_METHOD_MESSAGE}
        return {"reason": "selective_missing", "error": "還沒有選擇編碼；先回到資料與編碼形成選擇編碼，再選顧客任務。"}
    category_ids = {c["id"] for c in axial.get("categories") or [] if isinstance(c, dict) and c.get("id")}
    selective_ids = {t.get("id") for t in themes.get("themes") or [] if isinstance(t, dict)}
    if selection.get("schema_version") != 2 or stages._jtbd_selection_list_errors(selection, category_ids, selective_ids):
        return not_confirmed
    return None


def _run_step(run_dir: Path, runners: dict[str, Runner], passthrough: list[str], step: str):
    """(status, cause, paused_exception) for one step; never raises."""
    argv = ["--run", str(run_dir)] + (["--propose"] if step == "primary_hmw_candidates" else list(passthrough))
    try:
        status = runners[step](argv)
    except Exception as exc:  # the step's own guard should have caught this
        return 1, str(exc), None
    except BaseException as exc:  # a manual-transport pause
        return 1, None, exc
    return status, _read_state(run_dir).get(FAILURE_KEYS[step]), None


def _step_dependencies(step: str) -> tuple[str, ...]:
    """The method steps (not files) this step reads, from DEPENDENCIES."""
    return tuple(name for name in DEPENDENCIES[step] if name in STEP_FILES)


def run(run_dir: Path, runners: dict[str, Runner], passthrough: list[str]) -> int:
    """Form HMW. Returns 0 at the HMW gate, non-zero on failure; a manual-
    transport pause propagates after the state is recorded as waiting.

    A step starts as soon as every step it reads (DEPENDENCIES) is settled.
    After a failure or a pause nothing new starts; steps already running
    finish and are recorded."""
    run_dir = Path(run_dir)
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    update_state(run_dir, state="running", active_stage=None, failed_stage=None, failure=None)
    selected_before = selected_generated_content(run_dir)
    regenerated: set = set()
    settled: set = set()
    # A hand-relayed model turn is answered one prompt at a time, in step order.
    transport = (os.environ.get("SBP_LLM_PROVIDER") or "").strip().lower() == "transport"
    running: dict = {}
    prepared: dict = {}
    failed: list = []
    paused = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=1 if transport else len(STEPS)) as pool:
        while True:
            if not failed and paused is None:
                for step in STEPS:
                    if step in settled or step in running:
                        continue
                    if transport and running:
                        break
                    if any(name not in settled for name in _step_dependencies(step)):
                        continue
                    verdict, _reason = assess(run_dir, step, regenerated)
                    if verdict == "reuse":
                        settled.add(step)
                        continue
                    if verdict == "adopt":
                        _record_step(run_dir, step, {"adopted_legacy": True,
                                                     "dependencies": _dependency_digests(run_dir, step)})
                        settled.add(step)
                        continue
                    prepared[step] = _dependency_digests(run_dir, step)
                    output = run_dir / STEP_FILES[step]
                    if output.is_file():
                        shutil.copy2(output, output.with_name(f"{output.name}.bak-{stamp}-before-hmw-formation"))
                        if step == "primary_hmw_candidates" and _no_tensions(run_dir):
                            # Nothing to rank now; the kept copy is the only trace of the old ranking.
                            output.unlink()
                    # Persisted before the step starts, so the status never lags it.
                    active = [name for name in STEPS if name in running or name == step]
                    update_state(run_dir, state="running", active_stage=active[0], active_stages=active,
                                 failed_stage=None, failure=None)
                    running[step] = pool.submit(_run_step, run_dir, runners, passthrough, step)
            # STEPS lists every step after the steps it reads, so one pass
            # settles a whole chain of reused steps.
            if not running:
                break
            done, _pending = concurrent.futures.wait(running.values(), return_when=concurrent.futures.FIRST_COMPLETED)
            for step in [name for name in STEPS if name in running and running[name] in done]:
                status, cause, pause = running.pop(step).result()
                if pause is not None:
                    paused = paused or (step, pause)
                elif status != 0:
                    failed.append((step, status, cause))
                else:
                    _record_step(run_dir, step, {"method_version": METHOD_VERSIONS[step], "dependencies": prepared[step]})
                    regenerated.add(step)
                    settled.add(step)
                    if step == "how_might_we":
                        retire_changed_selection(run_dir, selected_before)
            if running:
                active = [name for name in STEPS if name in running]
                update_state(run_dir, active_stage=active[0], active_stages=active)
    if paused is not None:
        # A manual-transport pause is not a failure: the next Form HMW
        # resumes here because the paused step never reached `complete`.
        waiting, exception = paused
        update_state(run_dir, state="waiting_for_model", active_stage=waiting, active_stages=[waiting])
        raise exception
    if failed:
        step, status, cause = min(failed, key=lambda item: STEPS.index(item[0]))
        update_state(run_dir, state="failed", active_stage=None, active_stages=[], failed_stage=step,
                     failure=str(cause or f"{STAGE_LABELS[step]}沒有完成（exit {status}）。"))
        return status
    update_state(run_dir, state="awaiting_hmw_choice", active_stage=None, active_stages=[], failed_stage=None, failure=None,
                 finished_at=dt.datetime.now().isoformat(timespec="seconds"))
    return 0
