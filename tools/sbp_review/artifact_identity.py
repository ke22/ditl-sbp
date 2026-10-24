"""cross-project-state-isolation: the identity of the content a research decision
was made on.

A decision write carries the identity the screen read (`basis`); the server
recomputes it under the project lock and refuses the write when they differ.
Item ids such as `R-2`, `HMW-1` or `step-1-1` repeat across projects, so they
never identify content on their own: the digest covers the run name, the
artifact name and the artifact's bytes.

Identities are per operation (researcher ruling, 2026-10-13): each covers the
content a decision is made on plus the stored decision state that the write
itself changes, and nothing unrelated -- so a second tab's judgement on the same
item is refused, while an unrelated update elsewhere does not block an edit.

An artifact whose files do not exist yet has an explicit expected-absence
identity, so creating it is a stated precondition, never the fallback for a
missing basis.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

from sbp_gen import implementation_research as _implementation_research
from sbp_gen import improvement_goal_review as _goal_review
from sbp_gen import research_catalog_review as _research_review
from sbp_gen import selective_themes as _selective_themes

_BLUEPRINT = ("sbp_draft.yaml", "service_graph.yaml")
# The journey working copy, read the same way the server reads it.
_JOURNEY = ("journey_proposal.yaml", "journey.yaml")

# Artifact name -> files (relative to the run directory) whose bytes define it.
ARTIFACTS: dict[str, tuple[str, ...]] = {
    "blueprint": _BLUEPRINT,
    "inference-reviews": _BLUEPRINT + ("inference_reviews.yaml",),
    "primary-hmw": ("primary_hmw_candidates.yaml", "primary_hmw_selection.yaml"),
    # Selective themes and the JTBD candidates live in the same document.
    "selective-themes": ("selective_themes.yaml",),
    "jtbd-selection": ("selective_themes.yaml", "narrative_job_selection.yaml"),
    "persona": ("persona_content.yaml",),
    "segmentation-axes": ("segmentation_axis_candidates.yaml", "segmentation_axis_selection.yaml"),
    "target-segmentation": ("target_segmentation_candidates.yaml", "target_segmentation_selection.yaml",
                            "segmentation_axis_selection.yaml"),
    "journey": _JOURNEY,
    "journey-annotations": _JOURNEY + ("journey_annotations.yaml",),
    "chart-guides": ("chart_guides.yaml",),
    # The research catalog's path depends on the current research operation; the
    # server registers a resolver for it (see RESOLVERS).
    "research-review": (_research_review.REVIEW_FILENAME,),
    # Axial coding's direct edits (assign, exclude, create a category, narrative).
    "axial-coding": ("axial_coding.yaml", "axial_coding_adjustments.yaml", "selective_coding.yaml"),
    "core-category": ("core_category_candidates.yaml", "selective_coding.yaml"),
    # Staged journey-map judgements and what applying them rewrites.
    "journey-map": (".pending_adjustments.yaml", "service_graph.yaml") + _JOURNEY,
    "improvement-goal": (_goal_review.REVIEW_FILENAME,),
    # Case-level research input: computed on the case folder (its name is the identity's "run").
    "research-purpose": (_selective_themes.PURPOSE_FILE,),
    # purpose-aware-research-calibration: the researcher context, which mirrors the purpose.
    "project-context": (_selective_themes.PURPOSE_FILE, "project_context.yaml"),
    # Implementation research before 建立 SBP: the questions come from the journey's
    # adopted Solutions; ticking, skipping and the claim review are its decisions. The
    # current operation's record and catalog are added by a resolver (see RESOLVERS).
    "implementation-research": ("journey.yaml",
                                f"{_implementation_research.DIRNAME}/{_implementation_research.SELECTION_FILENAME}",
                                f"{_implementation_research.DIRNAME}/{_implementation_research.SKIP_FILENAME}",
                                _implementation_research.REVIEW_FILENAME),
}

# Artifacts whose file list depends on the run's state: name -> run_dir -> files.
RESOLVERS: dict[str, Callable[[Path], tuple[str, ...]]] = {}

_MISSING = b"\x00cross-project-state-isolation:missing\x00"


class StaleBasis(Exception):
    """The decision's basis does not match the target content."""


def files(run_dir: Path, artifact: str) -> tuple[str, ...]:
    if artifact not in ARTIFACTS:
        raise ValueError(f"unknown artifact: {artifact!r}")
    resolver = RESOLVERS.get(artifact)
    return tuple(resolver(Path(run_dir))) if resolver else ARTIFACTS[artifact]


def compute(run_dir: Path, artifact: str) -> dict:
    run_dir = Path(run_dir)
    names = files(run_dir, artifact)
    present = [(run_dir / name).is_file() for name in names]
    if not any(present):
        return {"run": run_dir.name, "artifact": artifact, "absent": True, "group": group(artifact)}
    digest = hashlib.sha256()
    for part in (run_dir.name.encode("utf-8"), artifact.encode("utf-8")):
        digest.update(len(part).to_bytes(8, "big") + part)
    for name, exists in zip(names, present):
        data = (run_dir / name).read_bytes() if exists else _MISSING
        digest.update(name.encode("utf-8") + b"\x00" + len(data).to_bytes(8, "big") + data)
    return {"run": run_dir.name, "artifact": artifact, "digest": "sha256:" + digest.hexdigest(),
            "group": group(artifact)}


def group(artifact: str) -> str:
    """Artifacts linked by a shared file form one group: their writes share one sending
    queue on the page, so a write never leaves with an identity a queued write is about
    to change. Computed from the static file lists."""
    parent = {name: name for name in ARTIFACTS}

    def find(name):
        while parent[name] != name:
            name = parent[name]
        return name
    names = list(ARTIFACTS)
    for i, first in enumerate(names):
        for second in names[i + 1:]:
            if set(ARTIFACTS[first]) & set(ARTIFACTS[second]):
                parent[find(second)] = find(first)
    root = find(artifact)
    return "+".join(sorted(name for name in names if find(name) == root))


def compute_many(run_dir: Path, artifacts) -> dict:
    return {artifact: compute(run_dir, artifact) for artifact in artifacts}


def sharing(run_dir: Path, artifact: str) -> tuple[str, ...]:
    """The artifact plus every other artifact that shares one of its files: a write
    to it changes all of their identities, so its reply returns all of them."""
    mine = set(files(run_dir, artifact))
    return (artifact,) + tuple(other for other in ARTIFACTS
                               if other != artifact and mine & set(files(run_dir, other)))


def check(run_dir: Path, artifact: str, basis: object) -> dict:
    """Return the current identity when `basis` matches it; raise StaleBasis otherwise.

    A missing basis is never read as expected absence."""
    current = compute(run_dir, artifact)
    if not isinstance(basis, dict):
        raise StaleBasis("no basis")
    if basis.get("run") != current["run"] or basis.get("artifact") != artifact:
        raise StaleBasis("basis names another project or artifact")
    if basis.get("absent") is True:
        if "digest" in basis:  # both "absent" and a version: not an identity this server issued
            raise StaleBasis("malformed basis")
        if not current.get("absent"):
            raise StaleBasis("expected absent, but the artifact exists")
        return current
    if current.get("absent"):
        raise StaleBasis("the artifact does not exist")
    if not isinstance(basis.get("digest"), str) or basis["digest"] != current["digest"]:
        raise StaleBasis("digest differs")
    return current


STALE_MESSAGE = "畫面上的內容不是這個專案目前的版本，請重新載入後再做一次。"


def refusal() -> tuple[dict, int]:
    return {"error": STALE_MESSAGE, "reason": "stale_basis"}, 409
