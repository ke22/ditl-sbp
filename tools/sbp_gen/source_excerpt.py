"""Reduce a fetched source to verbatim excerpts before it reaches the analyst.

Every fetched document used to travel twice: once into the research model's
conversation in full, and once back out again, because the passage check reads
the body the model returns. Eleven sources came to 5.8 MB and the request was
refused with HTTP 400.

Researcher's decision (2026-08-29): let Gemini do the fetching and processing
and leave the analysis to OpenAI. This is that step — with one constraint that
is not negotiable for this project. The citation check in
``research_policy._resolvable_passage`` is a literal substring test:

    if passage.casefold() not in body.casefold(): ...

A summariser breaks it. Paraphrase the source and the analyst quotes the
paraphrase, which appears nowhere in the original — the claim reads as
supported while its trail stops at a rewrite nobody kept. So this asks only
for verbatim spans, and then *checks* that each returned span really is one,
discarding anything that is not. Whatever a model does upstream, only text
that survives that check is passed on.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from . import llm, transport_llm

MAX_EXCERPTS = 8
MAX_EXCERPT_CHARS = 1_200
# What a source is allowed to contribute to the analyst's context. Well under
# any single-request limit even when every admitted source contributes its
# maximum, which is the property the previous design lacked entirely.
MAX_TOTAL_CHARS = MAX_EXCERPTS * MAX_EXCERPT_CHARS

SYSTEM = """You select verbatim passages from one fetched document.

Return JSON only: {"excerpts": ["...", "..."]}

Each excerpt MUST be copied character-for-character from the document. Do not
summarise, translate, correct, join separated sentences, or alter spacing or
punctuation. An excerpt that is not an exact substring of the document is
discarded, and a discarded excerpt helps nobody.

Select passages that bear on the listed present-state issues: findings,
measurements, described mechanisms, stated limits. Prefer a few substantial
passages over many fragments. Return an empty array when the document has
nothing to say about these issues — that is a useful answer, not a failure.
"""


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def verbatim_excerpts(candidates: Iterable[Any], body: str) -> list[str]:
    """Keep only candidates that really are spans of ``body``.

    Matching tolerates whitespace differences, because a model copying out of
    wrapped or PDF-extracted text reflows it almost every time, and a run of
    spaces is not the kind of alteration this guard exists to catch. The text
    kept is the model's — after the whitespace-insensitive match confirms the
    same characters appear, in the same order, in the source.
    """
    if not isinstance(body, str) or not body.strip():
        return []
    flattened = _normalize_whitespace(body).casefold()
    kept: list[str] = []
    seen: set[str] = set()
    for candidate in candidates or []:
        if not isinstance(candidate, str):
            continue
        excerpt = candidate.strip()[:MAX_EXCERPT_CHARS]
        probe = _normalize_whitespace(excerpt).casefold()
        if len(probe) < 40 or probe in seen or probe not in flattened:
            continue
        seen.add(probe)
        kept.append(excerpt)
        if len(kept) >= MAX_EXCERPTS:
            break
    return kept


def _issue_lines(issues: Iterable[Mapping[str, Any]]) -> str:
    lines = []
    for issue in issues or []:
        if isinstance(issue, Mapping):
            statement = str(issue.get("statement") or "").strip()
            if statement:
                lines.append(f"- {statement}")
    return "\n".join(lines[:12]) or "- (no issue statements supplied)"


def excerpt_source(
    body: str,
    issues: Iterable[Mapping[str, Any]],
    opts: Mapping[str, Any] | None = None,
    *,
    call: Any = None,
) -> tuple[list[str], dict[str, Any]]:
    """Return verbatim excerpts of ``body``, plus this call's usage.

    Falls back to the document's opening span when the model call fails or
    returns nothing usable. A degraded excerpt is recoverable — the analyst
    simply has less to work with — whereas failing here would end a research
    run over one awkward source, which is the failure mode this whole stage
    has been climbing out of.
    """
    options = dict(opts or {})
    usage: dict[str, Any] = {"tokens": 0, "seconds": 0}
    if not isinstance(body, str) or not body.strip():
        # research-admissibility-diagnostics: say where the excerpts came from.
        return [], {**usage, "provenance": "none", "fallback_reason": "empty_body"}
    fallback_reason = "no_verbatim_span"
    invoke = call or llm.call
    user = (
        "## Present-state issues\n" + _issue_lines(issues)
        + "\n\n## Document\n" + body[:400_000]
    )
    try:
        result = invoke(
            stage="issue_grounding",
            system=SYSTEM,
            user=user,
            model=options.get("excerpt_model"),
            effort=options.get("excerpt_effort"),
            max_tokens=4_000,
            provider=options.get("excerpt_provider") or "gemini",
        )
        usage = {"tokens": getattr(result, "tokens", 0), "seconds": round(getattr(result, "seconds", 0))}
        data = llm.extract_json(getattr(result, "text", "") or "")
        candidates = data.get("excerpts") if isinstance(data, Mapping) else None
        excerpts = verbatim_excerpts(candidates or [], body)
        if excerpts:
            return excerpts, {**usage, "provenance": "model_selected"}
    except transport_llm.TransportStale:
        # persist-external-research-source-snapshots-for-replay: a stale
        # manual-transport response here means the prompt this operation
        # rebuilt no longer matches what a prior turn's envelope was answered
        # against. Within one operation's lifecycle that should not happen —
        # the source body is snapshotted precisely so replay rebuilds the
        # same bytes — so a mismatch is a real inconsistency the researcher
        # needs to see, not an awkward source to degrade past. Letting it
        # propagate is what makes it visible: the `except Exception` below
        # would otherwise fold it into the same silent opening-span fallback
        # that originally hid this defect.
        raise
    except Exception:  # noqa: BLE001 - see docstring: never fatal
        fallback_reason = "model_call_failed"
    opening = body.strip()[:MAX_EXCERPT_CHARS]
    if len(opening) < 40:
        return [], {**usage, "provenance": "none", "fallback_reason": "opening_too_short"}
    return [opening], {**usage, "provenance": "fallback_head", "fallback_reason": fallback_reason}
