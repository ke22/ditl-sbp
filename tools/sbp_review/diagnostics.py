"""Local diagnostics: one allowlisted line per finished job.

When something goes wrong on a researcher's own machine there should be a record
that is safe to hand to a developer. Research content is therefore never a
diagnostic field: every field is named here, every value must match a short
code format, and anything else is dropped rather than cleaned
(desktop-runtime-foundation, local-runtime-diagnostics).

The record stays on this machine. Nothing here sends, syncs or exports it.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import platform as _platform
import re
import string
import sys
import tomllib
from pathlib import Path
from typing import Any, Callable, Mapping

DIAGNOSTICS_DIRNAME = "diagnostics"

_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
_APP_VERSION = re.compile(r"^\d+(?:\.\d+){0,3}(?:[-+][0-9A-Za-z.]{1,20})?$")
_PYTHON_VERSION = re.compile(r"^\d+(?:\.\d+){1,3}[0-9A-Za-z+.]{0,10}$")
_PLATFORM = re.compile(r"^[a-z0-9]{2,20}$")
_ARCHITECTURE = re.compile(r"^[a-z0-9_]{2,20}$")
# Belt and braces: whatever a field's own format allows, a string that looks like a
# credential never gets through.
_SECRETISH = re.compile(r"(?i)sk-|bearer|eyj[a-z0-9_-]{6,}|-----begin|api[_-]?key|token")
_STAGE = re.compile(r"^[a-z0-9-]{1,40}$")
_PROVIDER = re.compile(r"^[a-z0-9_]{1,30}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:\+00:00|Z)$")


def _matches(pattern: re.Pattern) -> Callable[[Any], bool]:
    return lambda value: isinstance(value, str) and bool(pattern.fullmatch(value))


def _one_of(*allowed: str) -> Callable[[Any], bool]:
    return lambda value: isinstance(value, str) and value in allowed


def _non_negative_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= 10_000_000)


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100_000


# The allowlist. A field that is not a key here can never be written.
FIELDS: dict[str, Callable[[Any], bool]] = {
    "recorded_at": _matches(_TIMESTAMP),
    "app_version": _matches(_APP_VERSION),
    "platform": _matches(_PLATFORM),
    "architecture": _matches(_ARCHITECTURE),
    "python_version": _matches(_PYTHON_VERSION),
    "job_kind": _matches(_CODE),
    "stage": _matches(_STAGE),
    "execution_mode": _one_of("cli", "api", "transport"),
    "executor": _one_of("codex", "claude"),
    "provider": _matches(_PROVIDER),
    "status": _one_of("done", "failed", "cancelled"),
    "duration_seconds": _non_negative_number,
    "retry_count": _count,
    "error_code": _matches(_CODE),
}

# Which workbench station a job belongs to, by job kind. A kind that is not
# listed leaves `stage` out; it is never guessed.
JOB_KIND_STAGES: dict[str, str] = {
    "fresh_coding": "project-home",
    "selective_themes_generation": "selective-coding",
    "jtbd_candidates_generation": "selective-coding",
    "hmw_formation": "primary-hmw",
    "persona_content_generation": "persona-target-selection",
    "journey_generation": "journey-map",
    "journey_solutions": "journey-map",
    "journey_recheck": "journey-map",
    "generation": "review-as-is",
    "as_is_blueprint_generation": "review-as-is",
    "implementation_research": "review-as-is",
    "as_is_research": "review-to-be",
    "to_be_generation": "review-to-be",
}

# The line the generation stages print when they retry; supplied by the stages so
# the counter and the printer cannot drift apart.
def retry_marker(notice: str) -> re.Pattern:
    """Compile a regex that matches `notice` (a str.format template with
    `{stage}` and `{attempt}` placeholders) as it is printed."""
    pieces = []
    for literal, field, _spec, _conv in string.Formatter().parse(notice):
        pieces.append(re.escape(literal))
        if field == "attempt":
            pieces.append(r"\d+")
        elif field is not None:
            pieces.append(r"[^\]\n]+")
    return re.compile("".join(pieces))


def _app_version() -> str | None:
    try:
        text = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
        return tomllib.loads(text).get("project", {}).get("version")
    except (OSError, tomllib.TOMLDecodeError):
        return None


def sanitize(event: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only allowlisted fields whose values pass their validator."""
    clean: dict[str, Any] = {}
    for name, validator in FIELDS.items():
        value = event.get(name)
        if isinstance(value, str) and _SECRETISH.search(value):
            continue
        if name in event and validator(event[name]):
            value = event[name]
            clean[name] = round(value, 3) if isinstance(value, float) else value
    return clean


def record(event: Mapping[str, Any], data_root: Path) -> dict[str, Any] | None:
    """Append one sanitized line to `<data_root>/diagnostics/<YYYY-MM>.jsonl`."""
    clean = sanitize(event)
    if not clean:
        return None
    stamp = clean.get("recorded_at") or dt.datetime.now(dt.timezone.utc).isoformat()
    path = Path(data_root) / DIAGNOSTICS_DIRNAME / f"{stamp[:7]}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(clean, sort_keys=True, ensure_ascii=True) + "\n")
    return clean


def build_job_event(snapshot: Mapping[str, Any], *, now_monotonic: float, retry: re.Pattern | None,
                    cause_of: Callable[[list], Any] | None) -> dict[str, Any]:
    """The raw event for a finished job; `sanitize` decides what survives."""
    kind = snapshot.get("kind")
    status = snapshot.get("state")
    summary = snapshot.get("execution")
    summary = summary if isinstance(summary, Mapping) else {}
    event: dict[str, Any] = {
        "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "app_version": _app_version(),
        "platform": sys.platform,
        "architecture": _platform.machine(),
        "python_version": _platform.python_version(),
        "job_kind": kind,
        "stage": JOB_KIND_STAGES.get(kind) if isinstance(kind, str) else None,
        "execution_mode": summary.get("mode"),
        "executor": summary.get("executor"),
        "provider": summary.get("provider"),
        "status": status,
    }
    started = snapshot.get("started_monotonic")
    if isinstance(started, (int, float)) and not isinstance(started, bool):
        event["duration_seconds"] = max(0.0, now_monotonic - started)
    log = snapshot.get("log")
    if retry is not None and isinstance(log, list):
        event["retry_count"] = sum(1 for line in log if isinstance(line, str) and retry.search(line))
    if status == "cancelled":
        event["error_code"] = "cancelled"
    elif status == "failed":
        error = snapshot.get("error")
        code = error.get("code") if isinstance(error, Mapping) else None
        if not code and cause_of is not None and isinstance(log, list):
            cause = cause_of(log)
            code = cause.get("cause") if isinstance(cause, Mapping) else cause
        event["error_code"] = code
    return event
