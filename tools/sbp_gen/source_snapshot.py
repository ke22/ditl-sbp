"""Operation-scoped snapshots of fetched external-research sources.

Manual-transport replay reruns a research session from its first turn,
including every fetch a prior turn already made
(``research_agent.relay_transport``'s docstring records this as inherent to
resuming a stateful loop from disk). A semantically unchanged page whose
per-request nonce or session token rotates between requests then produces
different prompt bytes on each replay. `transport_llm.call()` compares the
recorded envelope against the freshly rebuilt prompt byte-for-byte and raises
`TransportStale` on any mismatch — correctly, since it cannot otherwise tell a
rotated nonce from a genuinely different document. Before this module existed,
that `TransportStale` reached `source_excerpt.excerpt_source()`'s
degrade-don't-die fallback (`except Exception: pass`) and was swallowed,
silently discarding a researcher-validated excerpt selection in favour of the
document's opening 1,200 characters.

The fix is not to weaken that byte-exact comparison — it is the only thing
standing between a researcher's approved citation and an unnoticed
degradation. Instead, the body a fetch returns is fixed the first time an
operation sees it. Every later replay of the same operation reconstructs the
same body from this store instead of asking the network again, so the prompt
bytes a replay rebuilds are the same ones a prior successful turn recorded.

Scope: this store is keyed to one research operation's `request_digest`
(`as_is_research.request_digest()`), not to a URL in general. A new operation
(different baseline, different research question) starts with an empty store
and fetches fresh — that is correct, not a cache the store is trying to defeat.
`execute_supplemental_research()` is deliberately not wired to this module: its
operation directory is named from the research *result*'s digest, so no stable
identity exists to key a store on before the session that would populate it
has already run.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import transport_llm

SNAPSHOT_DIRNAME = "sources"

# Mirrors source_excerpt.py's inline `body[:400_000]` cap on what a fetched
# document contributes to the excerpt prompt. Duplicated rather than imported
# from there: nothing about that constant is specific to this module's job of
# storing bodies, and importing it would add an edge to a private detail of a
# module this one otherwise has no reason to depend on. If that cap becomes a
# named, shared constant, this should follow it.
DOCUMENT_EMBED_CAP = 400_000


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _digest_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(value)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            json.dump(dict(value), temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


class SourceSnapshotStore:
    """An append-only, per-operation cache of fetched source bodies.

    Two files per source: `<key>.meta.json` for everything except the body,
    and `<key>.body.txt` for the verbatim text — kept apart so the body stays
    directly comparable text rather than living escaped inside a JSON string.
    `<key>` is the sha256 of the requested URL, truncated to 16 hex
    characters; the store does not attempt URL normalisation beyond
    stripping surrounding whitespace, because the same code path always
    passes the same request URL string to `get()` and `put()` for a given
    fetch, which is the only consistency this key needs.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def _key(self, url: str) -> str:
        normalized = url.strip()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

    def _meta_path(self, key: str) -> Path:
        return self._root / f"{key}.meta.json"

    def _body_path(self, key: str) -> Path:
        return self._root / f"{key}.body.txt"

    def get(self, url: str) -> dict[str, Any] | None:
        """Reconstruct a prior fetch's result, or ``None`` if never snapshotted."""
        key = self._key(url)
        meta_path = self._meta_path(key)
        body_path = self._body_path(key)
        if not meta_path.is_file() or not body_path.is_file():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(meta, dict):
            return None
        body = body_path.read_text(encoding="utf-8")
        return {
            "url": meta.get("url", url),
            "status": meta.get("status"),
            "content_type": meta.get("content_type"),
            "body": body,
            "body_digest": meta.get("body_digest"),
            "redirects": list(meta.get("redirects") or []),
        }

    def put(self, url: str, result: Mapping[str, Any]) -> dict[str, Any]:
        """Record ``result`` as the fetch outcome for ``url``, once.

        Immutable: if this URL already has a snapshot in this store, that
        snapshot is returned unchanged and ``result`` is discarded. The first
        successful fetch (or import) of an operation is the fact of record —
        a later fetch producing different bytes (a rotated nonce, a changed
        session token) must not overwrite what a prior turn already committed
        a manual-transport envelope against.
        """
        existing = self.get(url)
        if existing is not None:
            return existing
        key = self._key(url)
        body = str(result.get("body") or "")
        meta: dict[str, Any] = {
            "url": str(result.get("url") or url),
            "status": result.get("status"),
            "content_type": result.get("content_type"),
            "body_digest": str(result.get("body_digest") or _digest_text(body)),
            "redirects": list(result.get("redirects") or []),
            "fetched_at": str(result.get("fetched_at") or _now()),
            "body_chars": len(body),
            "truncated": bool(result.get("truncated", False)),
        }
        if result.get("imported_from"):
            meta["imported_from"] = str(result["imported_from"])
        _write_text_atomic(self._body_path(key), body)
        _write_json_atomic(self._meta_path(key), meta)
        reconstructed = self.get(url)
        assert reconstructed is not None, "just-written snapshot must read back"
        return reconstructed

    def import_from_envelope(self, prompt_path: Path | str, *, url: str) -> dict[str, Any]:
        """Seed a snapshot from a document body already embedded in an
        answered manual-transport envelope, without fetching anything.

        One-off recovery, not a general tool: this reads exactly one envelope
        the caller names and writes exactly one snapshot for the ``url`` the
        caller supplies. It does not scan `_transport/` and does not guess a
        URL from the envelope's content — the excerpt prompt this reads from
        never carries the source URL (`research_privacy`'s boundary only lets
        issue statements and the fetched document text cross into it), so
        guessing would mean inventing a fact this module has no basis for.

        The document text recovered here is capped at whatever
        `source_excerpt.excerpt_source()` embedded when the envelope was
        first written (`DOCUMENT_EMBED_CAP` characters) — not necessarily the
        original page's full length, since that was never recorded anywhere.
        A recovered document at or over that cap is marked `truncated` so a
        later reader does not mistake it for the complete original page.
        """
        text = Path(prompt_path).read_text(encoding="utf-8")
        _system, user = transport_llm.parse_envelope(text)
        marker = "\n\n## Document\n"
        if marker not in user:
            raise ValueError(
                f"信封沒有 '## Document' 區段，無法匯入：{prompt_path}"
            )
        document = user.split(marker, 1)[1]
        return self.put(url, {
            "url": url,
            "status": 200,
            "content_type": "text/plain; charset=utf-8",
            "body": document,
            "body_digest": _digest_text(document),
            "redirects": [],
            "truncated": len(document) >= DOCUMENT_EMBED_CAP,
            "imported_from": str(prompt_path),
        })


class SnapshotBackedFetcher:
    """Wraps a fetcher so a URL already snapshotted for this operation is
    reconstructed instead of fetched again.

    Duck-typed on `.fetch(url) -> dict`, the same contract
    `research_agent.ResearchFetcher.fetch()` returns, rather than a subclass
    of it: `research_agent` imports this module (to bind a store into
    `build_research_runtime`), so this module importing `research_agent` back
    for a base class would be circular. Wrapping by contract instead of by
    inheritance also means it wraps a test's injected fetcher exactly the
    same way it wraps the production default.
    """

    def __init__(self, inner: Any, store: SourceSnapshotStore) -> None:
        self._inner = inner
        self._store = store

    def fetch(self, url: str) -> dict[str, Any]:
        cached = self._store.get(url)
        if cached is not None:
            return dict(cached)
        result = self._inner.fetch(url)
        self._store.put(url, result)
        return result


def store_for(run_dir: Path | str, request_digest: str) -> SourceSnapshotStore:
    """The snapshot store for one research operation.

    Mirrors the directory shape `as_is_research.operation_path()` uses
    (`<run_dir>/.as_is_research/<hex-digest>/`) without importing that
    module: `as_is_research` imports this module to bind a store into
    `execute_operation`, so the reverse import would be circular.
    """
    digest = str(request_digest).removeprefix("sha256:")
    root = Path(run_dir) / ".as_is_research" / digest / SNAPSHOT_DIRNAME
    return SourceSnapshotStore(root)
