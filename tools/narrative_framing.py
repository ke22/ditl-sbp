"""frame-hmw-from-narrative: which stage-2 JTBD selection an HMW output was
framed by. Since select-jtbd-from-selective-synthesis (2026-10-01) the
selection is the JTBD alone; selections recorded earlier also carry the
narrative the researcher chose then.

The selection frames HMW generation; it is never evidence (agent-pack
method-dependency `jtbd_to_hmw: frames_scope_not_evidence_whitelist`). Each HMW
output records the digest of the pair it read, so the workbench can tell the
researcher when the candidates were built from a pair they have since
replaced. Read by `sbp_gen` (writing) and `framework_points` (reading); one
definition so the two can never disagree about what "the same pair" means.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

SELECTION_FILE = "narrative_job_selection.yaml"
FRAMING_FIELDS = ("narrative", "situation", "progress", "motivation", "outcome", "grounded_in")
# 動機是 2026-09-29 才加的；沒有動機的舊選擇要算出跟以前一樣的指紋，
# 不然每一份既有的 HMW 都會被誤報成「讀的是舊的配對」。
# 敘事是 2026-10-01 起不再記錄的欄位（第 ② 站只選顧客任務）；舊選擇有
# 敘事，指紋照舊；新選擇沒有敘事，指紋就不含它。
OPTIONAL_FRAMING_FIELDS = ("motivation", "narrative")


LIST_FRAMING_FIELDS = ("situation", "progress", "motivation", "outcome", "selective_refs", "grounded_in")


def selections(document: object) -> list[dict]:
    """multi-jtbd-single-job-source: the recorded JTBDs as a list. A
    single-object (schema_version 1) selection reads as a one-item list."""
    if not isinstance(document, dict):
        return []
    if isinstance(document.get("selections"), list):
        return [item for item in document["selections"] if isinstance(item, dict)]
    return [document] if any(document.get(f) for f in ("situation", "progress", "outcome")) else []


def load_selection(run_dir: Path) -> list[dict]:
    path = Path(run_dir) / SELECTION_FILE
    if not path.is_file():
        return []
    try:
        return selections(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError):
        return []


def selection_digest(selection: dict | None) -> str | None:
    """The digest of the fields that frame HMW, or None without a selection.
    A list selection's digest covers every recorded JTBD; a single-object
    selection keeps the digest it always had, so older HMW stays matched."""
    if not isinstance(selection, dict):
        return None
    if isinstance(selection.get("selections"), list):
        items = [{field: item.get(field) for field in LIST_FRAMING_FIELDS if item.get(field) not in (None, "", [])}
                 for item in selection["selections"] if isinstance(item, dict)]
        if not items:
            return None
        encoded = json.dumps(items, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()
    framing = {field: selection.get(field) for field in FRAMING_FIELDS}
    for field in OPTIONAL_FRAMING_FIELDS:
        if framing.get(field) in (None, ""):
            framing.pop(field)
    if not any(framing.values()):
        return None
    encoded = json.dumps(framing, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def current_digest(run_dir: Path) -> str | None:
    """The digest of the run's recorded pair, or None when none is readable."""
    path = Path(run_dir) / SELECTION_FILE
    if not path.is_file():
        return None
    try:
        return selection_digest(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError):
        return None


def framed_digest(document: object) -> str | None:
    """The digest an HMW output says it was framed by."""
    if not isinstance(document, dict):
        return None
    framed_by = document.get("framed_by")
    return framed_by.get("narrative_job_selection") if isinstance(framed_by, dict) else None


def is_stale(run_dir: Path, document: object) -> bool:
    """True when a pair is recorded and the output was not framed by it."""
    current = current_digest(run_dir)
    return current is not None and framed_digest(document) != current
