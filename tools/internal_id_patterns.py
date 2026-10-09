"""The one place that knows what an internal id looks like.

CLAUDE.md bans internal node/relation ids, run directory names, and raw
schema enum values from ever reaching the screen. Before 2026-09-08 that
rule was enforced by three independent, hand-written regexes that agreed on
nothing: tools/sbp_review/ui.html's `withoutEvidenceIds` matched only
`E-\\d+`; tools/tests/browser_jargon_leak_smoke.py matched `[A-Z]{1,2}-\\d+`
(1-2 letter prefixes); tools/tests/browser_framework_point_selection.py
matched only `(CAT|HMW|PERSONA)-\\d+`. None of the three could catch a
3-or-more-letter prefix the other two didn't already special-case by name,
and a scan of every real run in runs/ found 8 such prefixes -- AXIS (240
occurrences), CAT, HMW, JOB, PERSONA, QUADRANT, SRC, STEP -- that all three
detectors missed simultaneously.

MULTICHAR_ID_PREFIXES below is generated FROM the schemas, not hand-typed:
every `"pattern": "^PREFIX-..."` declared across schema/*.json for a
3-or-more-letter all-caps PREFIX. test_internal_id_patterns.py re-derives
this list from the schema files on every test run and fails if it drifts
from what's hardcoded here -- the same "generate, then reconcile against a
hardcoded copy" discipline analysis_chain.py already applies to prompt
files, chosen over a live filesystem scan on every import so this module
has no import-time dependency on schema/ being present or well-formed.

1-2 letter prefixes (E, A, P, N, X, I, R, T, O, RC, TB, PV, VO, AV, JR, D,
AE, ...) are deliberately NOT enumerated here: ID_PATTERN's own
`[A-Z]{1,2}-\\d+` branch already catches any of them generically, and
enumerating them by name would only reintroduce the same "a new one won't
be recognized" gap this module exists to close for the multi-letter case.

Journey/scenario step ids (S-01, SC1-1, S2-02, ...) are a separate, KNOWN
gap this module does not close: journey.yaml's step `id` field has no
schema pattern at all (`{"type": "string", "minLength": 1}`), so different
runs use different free-form conventions and there is nothing to generate a
pattern from. `STEP-` (the prefix used by service_graph / annotations'
step_ref, which IS schema-patterned) is covered; the journey artifact's own
step ids are not.
"""
from __future__ import annotations

import re

# Schema-derived (see test_internal_id_patterns.py) -- 3+ letter prefixes.
MULTICHAR_ID_PREFIXES: tuple[str, ...] = (
    "AXIS", "CAT", "HMW", "JOB", "JTBD", "PERSONA", "PROP", "QUADRANT", "SEG", "SRC", "STEP",
)

# One prefix's alternation could be a substring of another's (there is no
# such collision today, but sort longest-first so a future addition can't
# silently shadow an existing one inside the alternation).
_MULTICHAR_ALTERNATION = "|".join(
    sorted(MULTICHAR_ID_PREFIXES, key=len, reverse=True)
)

# `[A-Z]{1,2}-\d+` for the generic short-prefix case (E-404, A-01, CAT
# already excluded since CAT is 3 letters and matched by the branch above
# instead), unioned with the named multi-letter prefixes. Suffix is
# `[0-9]` rather than `[A-Za-z0-9_-]` deliberately: widening the suffix
# shape catches more but also risks matching ordinary prose fragments
# ("D-Day", "R-rated") that happen to contain a hyphen after a capital
# letter -- this is a leak *scrubber*, so a false negative on an unusual
# id shape is a smaller cost than a false positive that mangles ordinary
# text or expected-original quotes.
#
# Exposed unbounded (no \b, no compiled form) as ID_SHAPE so a caller that
# needs its own surrounding context -- e.g. CLAUDE.md's one sanctioned
# exception, an id used as a locator directly beside its role/context
# ("E-4 · 支援窗口") -- can wrap it in a lookaround without having to keep a
# second copy of the alternation in sync with this one.
ID_SHAPE = rf"(?:[A-Z]{{1,2}}|{_MULTICHAR_ALTERNATION})-[0-9]+"
ID_PATTERN = re.compile(rf"\b{ID_SHAPE}\b")


def contains_internal_id(text: str) -> bool:
    return bool(ID_PATTERN.search(text or ""))


# persona-segmentation-reasoning-contract (2026-10-01): stored rationales from
# before the no-ids rule read "E-037說是路過" or "E-052–E-056顯示⋯". The
# artifact keeps them as generated; the researcher-facing projection swaps
# each run of ids (with its list/range joiners and a trailing 等) for one
# readable stand-in instead of leaving a broken sentence.
_ID_RUN = re.compile(
    rf"(?<![A-Za-z0-9]){ID_SHAPE}(?:\s*(?:[、，,/～~–—-]|至|到|和|與|及)\s*{ID_SHAPE})*(?:\s*等)?(?![A-Za-z0-9])"
)


def readable_without_ids(text: str, stand_in: str = "原話") -> str:
    """Researcher-facing copy of `text` with every run of internal ids
    replaced by `stand_in`; the stored text is never rewritten."""
    replaced = _ID_RUN.sub(stand_in, text or "")
    return re.sub(rf"(?:{re.escape(stand_in)})(?:\s*[、，,]?\s*{re.escape(stand_in)})+", stand_in, replaced)
