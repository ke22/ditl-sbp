"""Shared evidence-location logic for both interface servers
(tools/workbench/server.py and, from project-existing-graph-without-model-call
onward, tools/sbp_gen/__main__.py's `project` subcommand).

Moved out of tools/workbench/server.py rather than cross-imported: this
module's behavior was originally written and proven there, but
tools/sbp_gen/ (a generation package) must not import an interface module to
reach it -- that would put the generation package's dependency direction
backwards. tools/pathnames.py is the precedent for this move: a rule two
servers both need, lifted to a shared module instead of one importing the
other.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from yaml_loader import safe_load as load_safe_yaml

REPO = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO / "tools"
CASES_DIR = REPO / "cases"
DATA_ROOT = REPO
RUNS_DIR = REPO / "runs"


def configure_data_root(data_root: str | Path | None = None) -> None:
    """Point shared evidence discovery at the active runtime data root."""
    global DATA_ROOT, CASES_DIR, RUNS_DIR
    DATA_ROOT = REPO if data_root is None else Path(data_root).expanduser().resolve()
    CASES_DIR = DATA_ROOT / "cases"
    RUNS_DIR = DATA_ROOT / "runs"


def _data_root_for_run(run_dir: Path) -> Path:
    """Infer a relocated root for CLI subprocesses from an absolute run path."""
    if DATA_ROOT != REPO:
        return DATA_ROOT
    run_path = run_dir.expanduser().resolve()
    for candidate in (run_path, *run_path.parents):
        if candidate.name == "runs":
            return candidate.parent
    return DATA_ROOT


# The three field names real runs actually use to record which evidence
# document they were generated from. Measured across every run in runs/:
# no single name covers them all, and no run carries two of them.
EVIDENCE_POINTER_KEYS = (
    ("from_evidence",),            # 2026-09-04-manual-transport-muji-full
    ("inputs", "evidence_yaml"),   # CafeMealMUJI-2026-08-29-1787965225706
    ("inputs", "evidence"),        # older runs predating the inputs rename
)


def _dig(data, path: tuple) -> object:
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _load_yaml_safe(path: Path):
    """A malformed document is a missing one rather than an exception --
    every caller here is on a read path that must degrade to an honest
    "cannot locate" state, never a 500."""
    if not path.is_file():
        return None
    try:
        return load_safe_yaml(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        return None


def _load_json_safe(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def _resolve_recorded_path(value: str, run_dir: Path | None = None) -> Path | None:
    """A recorded evidence path appears in three forms across runs/:
    repo-relative (`cases/...`), tools-relative (`../cases/...`, from a run
    generated with tools/ as the working directory), and absolute. Relative
    case paths are rooted at the active data root; an explicit relocated root
    never falls back to the source checkout."""
    raw = Path(value)
    if raw.is_absolute():
        candidates = [raw]
    else:
        root = _data_root_for_run(run_dir) if run_dir is not None else DATA_ROOT
        posix = raw.as_posix()
        if posix == "cases" or posix.startswith("cases/") or posix == "../cases" or posix.startswith("../cases/"):
            candidates = [root / Path(posix.removeprefix("../"))]
        elif root != REPO:
            # An explicit/relocated root must not silently fall back to the
            # source checkout.  Unknown relative forms are simply missing.
            candidates = [root / raw]
        else:
            candidates = [REPO / raw, (TOOLS_DIR / raw).resolve()]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _case_dir_for_run(run_dir: Path) -> Path | None:
    """The run's case directory, found from the run's own case.yaml when it
    has one and otherwise from run.json's case_id -- 2026-09-04-manual-
    transport-henglong-full has no run-local case.yaml, so the case_id
    fallback is what makes that run resolvable at all."""
    case_id = None
    local_case = _load_yaml_safe(run_dir / "case.yaml")
    if isinstance(local_case, dict):
        case_id = local_case.get("case_id")
    if not case_id:
        run_json = _load_json_safe(run_dir / "run.json")
        if isinstance(run_json, dict):
            case_id = run_json.get("case_id")
    if not case_id:
        return None
    case_root = _data_root_for_run(run_dir) / "cases"
    for case_yaml in sorted(case_root.glob("*/case.yaml")):
        data = _load_yaml_safe(case_yaml)
        if isinstance(data, dict) and data.get("case_id") == case_id:
            return case_yaml.parent
    return None


def locate_evidence(run_dir: Path) -> tuple[list | None, str]:
    """The run's evidence records and how they were found, or None and why
    they were not. Only 6 of the runs on disk carry their own evidence.yaml
    -- the flagship 2026-09-04-manual-transport-muji-full does not -- so a
    single lookup rule cannot reach the primary real case.

    Order: the run's own copy; then the path recorded in run.json under each
    name real runs use; then a uniquely-named evidence document in the case
    directory.

    Two candidates in the case directory return `ambiguous` with no records,
    never a chosen file: cases/長庚富康巴士/ holds evidence_chongzheng.yaml
    and evidence_chongzheng_verbatim.yaml, same 37 ids and different text,
    and attaching the wrong verbatim line to a claim is worse than showing
    no line at all."""
    def _records(path: Path) -> list | None:
        data = _load_yaml_safe(path)
        return data if isinstance(data, list) else None

    local = run_dir / "evidence.yaml"
    if local.is_file():
        records = _records(local)
        if records is not None:
            return records, "run_local"

    run_json = _load_json_safe(run_dir / "run.json")
    if isinstance(run_json, dict):
        for key_path in EVIDENCE_POINTER_KEYS:
            value = _dig(run_json, key_path)
            if not isinstance(value, str) or not value.strip():
                continue
            resolved = _resolve_recorded_path(value, run_dir)
            if resolved is None:
                continue
            records = _records(resolved)
            if records is not None:
                return records, "run_json_pointer"

    case_dir = _case_dir_for_run(run_dir)
    if case_dir is not None:
        candidates = sorted(case_dir.glob("evidence*.yaml"))
        if len(candidates) > 1:
            return None, "ambiguous"
        if len(candidates) == 1:
            records = _records(candidates[0])
            if records is not None:
                return records, "case_unique"

    return None, "not_found"
