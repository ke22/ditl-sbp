"""Shared path-component sanitizing for the two HTTP servers under tools/.

tools/sbp_review/server.py and tools/workbench/server.py both accept
user-supplied run/case names over HTTP and join them onto a base directory
(RUNS_DIR / run_name, CASES_DIR / case_name). A join is not containment:
"../../etc" or an absolute path component silently escapes the base
directory instead of raising. tools/sbp_review/server.py has always guarded
this (see its `_safe_component`); tools/workbench/server.py — written later,
and by its own module docstring deliberately never importing from
tools/sbp_review/ — did not inherit the guard. Rather than have
tools/workbench/ import from tools/sbp_review/ (which would violate that
documented boundary) or re-implement its own copy (which is how the gap
opened in the first place), both import this module instead.

Use `reject(value)` at a request boundary: a name that traverses (contains a
path separator, is absolute, or is "."/"..") is a client error, not something
to silently rewrite into one (a client asking for run "../../etc" should get
400, not read of some other run entirely). This checks structural safety
only, not character shape -- a real case or run name can legitimately
contain spaces, accented letters, or punctuation (e.g. "Café&Meal MUJI") and
must still be accepted. Use `safe_component(value, fallback)` only when
building an internal path from a value that has already been validated or
is locally-generated (e.g. a version id this process minted) -- that
function's narrower character-class allowlist is appropriate there because
it is normalizing a value into a *new* filename, not validating an existing
one a client is merely referencing.
"""
from __future__ import annotations

import re
from pathlib import Path

# CJK Unified Ideographs + Extension A, alongside ASCII alnum/._- . This is
# the allowlist tools/sbp_review/server.py has used since add-run-watch-mode;
# kept as-is rather than widened here (a case named with Hiragana/Katakana,
# Extension B, or an accented Latin letter still gets mangled -- e.g.
# "Café&Meal MUJI" -> "Caf-Meal-MUJI" -- that is a separate, tracked gap, not
# something to silently change as part of moving the function).
_ALLOWED = re.compile(r"[^A-Za-z0-9._\-㐀-鿿]+")


def safe_component(value: str, fallback: str) -> str:
    """Sanitize `value` into a single safe path component, or `fallback`.

    Strips any directory part first (Path(...).name), so "../x" becomes "x"
    rather than being rejected outright — use this only for values you are
    willing to have silently coerced. For a request boundary where a mismatch
    should be a client error instead, use `reject()`.
    """
    value = Path(value or "").name
    value = _ALLOWED.sub("-", value).strip(".-")
    return value[:120] or fallback


def reject(value: object) -> bool:
    """True if `value` is unsafe to join onto a base directory as-is —
    empty, not a string, a bare "." or "..", or containing a path separator
    or absolute-path component (`Path(value).name != value` catches both:
    a join is not containment, but a *component* -- no separators at all --
    always is). Callers use this to return 400/404 instead of joining an
    unsafe value onto a base directory.

    Deliberately NOT the same check as `safe_component()`'s character-class
    allowlist: that allowlist is CJK + ASCII alnum/._- only, which rejects
    ordinary values this project already has on disk -- the real case
    folder "Café&Meal MUJI" contains a space, `é`, and `&`, none of which
    are traversal-related. A reject boundary this narrow would 400 the
    real MUJI case itself (found live, 2026-09-08, the first time this
    function actually ran against it end-to-end). Traversal safety comes
    entirely from the structural check below; `safe_component()`'s
    allowlist remains appropriate for its own, different job -- normalizing
    a value this process itself mints into a new filename."""
    if not isinstance(value, str) or not value.strip():
        return True
    if value in (".", ".."):
        return True
    return Path(value).name != value
