"""Deterministic source-admission and citation-support policy.

Claude Agent output is treated as a candidate set.  Only this module can admit
sources or construct a canonical research catalog.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import validate_sbp as V

from . import llm, research_privacy


ALLOWED_SOURCE_CLASSES = frozenset(
    {"academic", "government", "professional_body", "first_party_case"}
)
MAX_VERIFIER_ITEMS = 100
MAX_VERIFIER_INPUT_CHARS = 120_000
MAX_VERIFIER_OUTPUT_TOKENS = 8_000
PROMPT_VERSION = "research-support-v1"
POLICY_VERSION = "research-policy-v1"
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "research_support.md"
DOI_PATTERN = re.compile(r"^10\.\d{4,9}/\S+$", re.I)


class ResearchPolicyError(RuntimeError):
    def __init__(self, code: str, *, decisions: Iterable[Mapping[str, Any]] = (),
                 diagnostics: Mapping[str, Any] | None = None):
        super().__init__(code)
        self.code = code
        self.decisions = [dict(item) for item in decisions]
        # research-admissibility-diagnostics: why nothing was admitted, kept
        # so a failed operation can still be diagnosed.
        self.diagnostics = dict(diagnostics) if diagnostics else None


# research-admissibility-diagnostics: bounded so a run's record stays small.
DIAGNOSTIC_QUOTE_CHARS = 300
DIAGNOSTIC_SNIPPET_CHARS = 160
DIAGNOSTIC_MAX_LINKS = 60


def _admissibility_links(
    claims: list[Mapping[str, Any]],
    sources: list[dict[str, Any]],
    fetched: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """One record per cited link: where it pointed, what it quoted, and
    whether that quote is in the fetched text. Reads only; decides nothing."""
    urls = {source["id"]: _text(source.get("url") or source.get("doi")) for source in sources}
    records: list[dict[str, Any]] = []
    for claim_index, claim in enumerate(claims):
        for link_index, raw_link in enumerate(claim.get("source_links") or []):
            if not isinstance(raw_link, Mapping) or len(records) >= DIAGNOSTIC_MAX_LINKS:
                continue
            quote = _text(raw_link.get("passage"))
            record: dict[str, Any] = {
                "claim_index": claim_index,
                "link_index": link_index,
                "cited_source": _text(raw_link.get("source_ref") or raw_link.get("source_url")
                                      or raw_link.get("url") or raw_link.get("doi")),
                "quote": quote[:DIAGNOSTIC_QUOTE_CHARS],
            }
            source_ref = _source_ref(raw_link, sources)
            if not source_ref:
                record["status"] = "source_not_admitted"
            elif not quote or not _text(raw_link.get("locator")):
                record.update(source_ref=source_ref, source_url=urls.get(source_ref, ""),
                              status="missing_quote_or_locator")
            else:
                body = _text((fetched.get(source_ref) or {}).get("normalized_body"))
                offset = body.casefold().find(quote.casefold())
                record.update(source_ref=source_ref, source_url=urls.get(source_ref, ""))
                if offset < 0:
                    record["status"] = "quote_not_found"
                else:
                    record.update(status="quote_found", match_offset=offset,
                                  match_snippet=body[offset:offset + DIAGNOSTIC_SNIPPET_CHARS])
            records.append(record)
    return records


@dataclass(frozen=True)
class AdmissionResult:
    sources: list[dict[str, Any]]
    fetched: dict[str, dict[str, Any]]
    decisions: list[dict[str, Any]]


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text(value: object) -> str:
    return " ".join(str(value or "").split())


def _url_key(url: str) -> str:
    """Compare URLs the way research_fetch normalizes them; keep unparseable ones verbatim."""
    if not url:
        return ""
    from .research_agent import ToolPolicyError, normalize_public_url

    try:
        return normalize_public_url(url)
    except ToolPolicyError:
        return url


def _normalized_bodies(fetched_bodies: Mapping[str, str] | None) -> dict[str, str]:
    bodies: dict[str, str] = {}
    for url, body in (fetched_bodies or {}).items():
        if isinstance(url, str) and isinstance(body, str) and body.strip():
            bodies[_url_key(url)] = body
    return bodies


def _source_identity(candidate: Mapping[str, Any]) -> str:
    return _text(candidate.get("doi") or candidate.get("url") or candidate.get("title"))


def _decision(index: int, candidate: Mapping[str, Any], admission: str, reason: str) -> dict[str, Any]:
    return {
        "candidate_index": index,
        "candidate_digest": _digest(_source_identity(candidate)),
        "source_class": candidate.get("source_class") if candidate.get("source_class") in ALLOWED_SOURCE_CLASSES else None,
        "admission": admission,
        "reason": reason,
    }


def _rejection_reason(candidate: Mapping[str, Any], retrieved_body: str = "") -> str | None:
    declared = _text(candidate.get("reason") or candidate.get("rejection_reason"))
    if declared in {
        "snippet_only",
        "inaccessible",
        "secondary_news",
        "personal_blog",
        "aggregator",
        "vendor_marketing_without_first_party_result",
    }:
        return declared
    source_class = candidate.get("source_class")
    if source_class not in ALLOWED_SOURCE_CLASSES:
        return "source_class_not_admitted"
    if not _text(candidate.get("title")) or not _text(candidate.get("authors_or_publisher")):
        return "authoritative_identity_missing"
    locator = _text(candidate.get("locator"))
    if not locator:
        return "locator_missing"
    url = _text(candidate.get("url"))
    doi = _text(candidate.get("doi"))
    if not url and not DOI_PATTERN.match(doi):
        return "resolvable_identity_missing"
    if url:
        try:
            from .research_agent import normalize_public_url

            normalize_public_url(url)
        except Exception:
            return "resolvable_identity_missing"
    # `retrieved_body` is what this application fetched through research_fetch.
    # A body the model put on the candidate is never enough: only content this
    # application retrieved (and, on the main path, snapshotted) can make a
    # source eligible -- in every execution mode, not only the ones whose
    # transport happens to refuse echoed content.
    if candidate.get("fetched") is False or not retrieved_body.strip():
        return "fetched_body_missing"
    if candidate.get("authoritative") is not True:
        return "authoritative_identity_unverified"
    if source_class == "academic":
        scholarly = candidate.get("scholarly_metadata", True)
        stable = bool(doi or url)
        if scholarly is False or not stable:
            return "academic_identity_unverified"
    if source_class == "first_party_case" and not (
        candidate.get("describes_own_implementation") is True
        and _text(candidate.get("observable_result"))
    ):
        return "first_party_result_missing"
    if candidate.get("content_origin") in {"secondary_news", "personal_blog", "aggregator"}:
        return str(candidate["content_origin"])
    if candidate.get("marketing_only") is True:
        return "vendor_marketing_without_first_party_result"
    return None


def admit_sources(
    candidates: Iterable[Mapping[str, Any]],
    *,
    accessed_at: str | None = None,
    fetched_bodies: Mapping[str, str] | None = None,
) -> AdmissionResult:
    """Apply the four-class gate and preserve every decision outside canonical data.

    `fetched_bodies` is what this application actually retrieved, keyed by URL
    (requested and final). Only a source whose URL is present there can be
    admitted, and that text -- never a copy the model returned -- becomes the
    body every passage is checked against. The check itself is a literal
    substring test, so whoever supplies the body decides what counts as
    supported: letting the model supply it meant a claim could be "verified"
    against a rewrite of the source that nobody kept.
    """
    day = accessed_at or date.today().isoformat()
    admitted: list[dict[str, Any]] = []
    fetched: dict[str, dict[str, Any]] = {}
    decisions: list[dict[str, Any]] = []
    seen: set[str] = set()
    retrieved_by_url = _normalized_bodies(fetched_bodies)
    for index, raw in enumerate(candidates):
        candidate = dict(raw) if isinstance(raw, Mapping) else {}
        retrieved = retrieved_by_url.get(_url_key(_text(candidate.get("url")))) or ""
        reason = _rejection_reason(candidate, retrieved if isinstance(retrieved, str) else "")
        identity = _source_identity(candidate).lower()
        if not reason and identity in seen:
            reason = "duplicate_source"
        if reason:
            decisions.append(_decision(index, candidate, "rejected", reason))
            continue
        seen.add(identity)
        source_id = f"R-{len(admitted) + 1:03d}"
        source = {
            "id": source_id,
            "source_class": candidate["source_class"],
            "title": _text(candidate["title"]),
            "authors_or_publisher": _text(candidate["authors_or_publisher"]),
            "locator": _text(candidate["locator"]),
            "accessed_at": day,
            "admission": "admitted",
            "admission_reason": _text(candidate.get("admission_reason"))
            or "Fetched authoritative source passed the application source policy.",
        }
        if _text(candidate.get("url")):
            from .research_agent import normalize_public_url

            source["url"] = normalize_public_url(_text(candidate["url"]))
        if _text(candidate.get("doi")):
            source["doi"] = _text(candidate["doi"])
        body = retrieved
        admitted.append(source)
        fetched[source_id] = {
            "normalized_body": body,
            "body_digest": _digest(body),
            "source_identity_digest": _digest(identity),
        }
        decisions.append(_decision(index, candidate, "admitted", "source_policy_passed"))
    return AdmissionResult(admitted, fetched, decisions)


def _source_ref(link: Mapping[str, Any], sources: list[dict[str, Any]]) -> str | None:
    """Resolve a citation to an admitted source.

    fix-research-citation-verification: the research output contract puts the
    source's URL or DOI in `source_ref`; older callers use `source_url`, `url`
    or `doi`, or an admitted id. Only admitted sources can match, URLs compare
    after the admission normalization, DOIs case-insensitively."""
    direct = link.get("source_ref")
    if isinstance(direct, str) and any(source["id"] == direct for source in sources):
        return direct
    named = [_text(link.get(field)) for field in ("source_ref", "source_url", "url", "doi")]
    urls = {_url_key(value) for value in named if value}
    dois = {value.lower() for value in named if value}
    for source in sources:
        url, doi = _text(source.get("url")), _text(source.get("doi")).lower()
        if (url and _url_key(url) in urls) or (doi and doi in dois):
            return source["id"]
    index = link.get("source_index")
    if isinstance(index, int) and 0 <= index < len(sources):
        return sources[index]["id"]
    return None


def _resolvable_passage(link: Mapping[str, Any], body: str) -> str | None:
    locator = _text(link.get("locator"))
    passage = _text(link.get("passage"))
    if not locator or not passage:
        return None
    if passage.casefold() not in body.casefold():
        return None
    return passage


def _verifier_payload(
    claims: list[Mapping[str, Any]],
    sources: list[dict[str, Any]],
    fetched: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[tuple[int, int], dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    links: dict[tuple[int, int], dict[str, Any]] = {}
    for claim_index, claim in enumerate(claims):
        for link_index, raw_link in enumerate(claim.get("source_links") or []):
            if not isinstance(raw_link, Mapping):
                continue
            source_ref = _source_ref(raw_link, sources)
            if not source_ref:
                continue
            passage = _resolvable_passage(
                raw_link, _text((fetched.get(source_ref) or {}).get("normalized_body"))
            )
            if not passage:
                continue
            row = {
                "claim_index": claim_index,
                "link_index": link_index,
                "claim": _text(claim.get("text")),
                "locator": _text(raw_link.get("locator")),
                "passage": passage,
            }
            rows.append(row)
            links[(claim_index, link_index)] = {
                "source_ref": source_ref,
                "locator": row["locator"],
            }
    if len(rows) > MAX_VERIFIER_ITEMS:
        rows = rows[:MAX_VERIFIER_ITEMS]
        links = {(row["claim_index"], row["link_index"]): links[(row["claim_index"], row["link_index"])] for row in rows}
    return rows, links


def _relay_selected() -> bool:
    """Whether the configured provider relays model turns by hand.

    Mirrors `research_agent._relay_selected()` exactly (same env var, same
    check) instead of importing it: neither module imports the other today,
    and reaching across just to save this one-line check would add an edge
    for no benefit. Read at call time, not at import, for the same reason as
    the original — the environment is set per invocation.
    """
    return (os.environ.get("SBP_LLM_PROVIDER") or "").strip().lower() == "transport"


def _cli_selected() -> bool:
    """Same rule research_agent uses: an authenticated CLI answers model turns."""
    return (
        (os.environ.get("SBP_EXECUTION_MODE") or "").strip().lower() == "cli"
        or (os.environ.get("SBP_LLM_PROVIDER") or "").strip().lower() == "cli"
    )


def _verifier_provider() -> str:
    # A CLI research session must verify through the same CLI; defaulting to
    # "anthropic" here made every CLI session fail at this one step on a
    # machine without Anthropic credentials.
    if _relay_selected():
        return "transport"
    return "cli" if _cli_selected() else "anthropic"


def _default_verifier(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    user = json.dumps({"items": rows}, ensure_ascii=False, separators=(",", ":"))
    if len(user) > MAX_VERIFIER_INPUT_CHARS:
        raise ResearchPolicyError("citation_support_rejected")
    # `provider="anthropic"` was unconditional here, so a manual-transport
    # research session still made one live Anthropic call at this one step —
    # the only stage a no-API-key relay could not get past. `llm.call()`
    # dispatches "transport" to `transport_llm.call()`, which raises
    # `TransportSuspended` (suspend, not a fallback call) when this round has
    # no recorded answer yet, and `TransportStale` when a recorded answer
    # does not match this rebuild; neither is caught here, so both propagate
    # exactly as they do from every other relayed stage — this only changes
    # which provider answers, not what counts as a valid verdict.
    response = llm.call(
        stage="research_support",
        system=PROMPT_PATH.read_text(encoding="utf-8"),
        user=user,
        provider=_verifier_provider(),
        max_tokens=MAX_VERIFIER_OUTPUT_TOKENS,
    )
    try:
        value = llm.extract_json(response.text)
    except llm.LLMError as exc:
        raise ResearchPolicyError("citation_support_rejected") from exc
    verdicts = value.get("verdicts") if isinstance(value, dict) else None
    if not isinstance(verdicts, list):
        raise ResearchPolicyError("citation_support_rejected")
    return verdicts


def _validated_verdicts(
    rows: list[dict[str, Any]], verdicts: Iterable[Mapping[str, Any]]
) -> dict[tuple[int, int], str]:
    expected = {(row["claim_index"], row["link_index"]) for row in rows}
    resolved: dict[tuple[int, int], str] = {}
    for raw in verdicts:
        if not isinstance(raw, Mapping):
            continue
        key = (raw.get("claim_index"), raw.get("link_index"))
        relationship = raw.get("relationship")
        if key not in expected or relationship not in {"supports", "limits", "unsupported"}:
            raise ResearchPolicyError("citation_support_rejected")
        if key in resolved:
            raise ResearchPolicyError("citation_support_rejected")
        resolved[key] = str(relationship)
    if set(resolved) != expected:
        raise ResearchPolicyError("citation_support_rejected")
    return resolved


def _has_chinese(value: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in value)


def build_research_catalog(
    *,
    case_id: str,
    scenario_id: str,
    source_candidates: Iterable[Mapping[str, Any]],
    claim_candidates: Iterable[Mapping[str, Any]],
    query_candidates: Iterable[Mapping[str, Any]],
    verifier: Callable[[list[dict[str, Any]]], Iterable[Mapping[str, Any]]] | None = None,
    accessed_at: str | None = None,
    fetched_bodies: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Admit sources, verify passages once, and construct canonical content."""
    admission = admit_sources(
        source_candidates, accessed_at=accessed_at, fetched_bodies=fetched_bodies)
    claims = [dict(item) for item in claim_candidates if isinstance(item, Mapping)]
    rows, links = _verifier_payload(claims, admission.sources, admission.fetched)
    diagnostics: dict[str, Any] = {
        "links": _admissibility_links(claims, admission.sources, admission.fetched)}
    if not admission.sources or not rows:
        diagnostics["reason"] = "no_admitted_sources" if not admission.sources else "quote_not_found"
        raise ResearchPolicyError("no_admissible_research", decisions=admission.decisions,
                                  diagnostics=diagnostics)
    verdicts = list((verifier or _default_verifier)(rows))
    resolved = _validated_verdicts(rows, verdicts)
    for record in diagnostics["links"]:
        verdict = resolved.get((record["claim_index"], record["link_index"]))
        if verdict:
            record["verdict"] = verdict
    canonical_claims: list[dict[str, Any]] = []
    not_chinese = 0
    for claim_index, claim in enumerate(claims):
        source_links = []
        for key, relationship in resolved.items():
            if key[0] != claim_index or relationship == "unsupported":
                continue
            link = links[key]
            source_links.append({**link, "relationship": relationship})
        if not any(link["relationship"] == "supports" for link in source_links):
            continue
        # research-claims-in-traditional-chinese: the contract asks for Chinese; a verified
        # claim that is not is kept (frozen replay corpora are English) and counted.
        if any(_text(claim.get(field)) and not _has_chinese(_text(claim.get(field)))
               for field in ("text", "applicability_context")):
            not_chinese += 1
        uncertainty = claim.get("uncertainty")
        if not isinstance(uncertainty, dict) or uncertainty.get("state") not in {"bounded", "uncertain", "disputed"}:
            uncertainty = {"state": "uncertain", "explanation": "Transferability to this service context has not been tested."}
        if uncertainty.get("state") in {"uncertain", "disputed"} and not _text(uncertainty.get("explanation")):
            uncertainty = {"state": uncertainty["state"], "explanation": "Transferability remains uncertain."}
        canonical = {
            "id": f"RC-{len(canonical_claims) + 1:03d}",
            "claim_state": "hypothesis",
            "text": _text(claim.get("text")),
            "applicability_context": _text(claim.get("applicability_context")),
            "uncertainty": uncertainty,
            "source_links": source_links,
        }
        # external-reference-summary: a one-sentence summary, kept when the claim has one.
        if _text(claim.get("summary")):
            canonical["summary"] = _text(claim.get("summary"))
        # bind-external-reference-provenance: which questions the session said this answers.
        question_refs = [_text(ref) for ref in claim.get("question_refs") or [] if isinstance(ref, str) and _text(ref)]
        if question_refs:
            canonical["question_refs"] = question_refs
        canonical_claims.append(canonical)
    if not canonical_claims:
        diagnostics["reason"] = "quote_found_but_not_supporting_claim"
        raise ResearchPolicyError("no_admissible_research", decisions=admission.decisions,
                                  diagnostics=diagnostics)

    identity_to_ref = {
        _text(source.get("url") or source.get("doi")).lower(): source["id"]
        for source in admission.sources
    }
    queries: list[dict[str, Any]] = []
    for candidate in query_candidates:
        if not isinstance(candidate, Mapping):
            continue
        query = _text(candidate.get("deidentified_query") or candidate.get("query"))
        purpose = _text(candidate.get("purpose"))
        executed_at = _text(candidate.get("executed_at"))
        if not query or not purpose or not executed_at:
            continue
        research_privacy.validate_external_payload(query, field_prefix="query_audit")
        returned = []
        for identity in candidate.get("returned_urls") or candidate.get("returned_source_urls") or []:
            ref = identity_to_ref.get(_text(identity).lower())
            if ref and ref not in returned:
                returned.append(ref)
        queries.append({
            "deidentified_query": query,
            "purpose": purpose,
            "executed_at": executed_at,
            "returned_source_refs": returned,
        })
    catalog = {
        "schema_version": 1,
        "artifact_type": "research_catalog",
        "case_id": case_id,
        "scenario_id": scenario_id,
        "sources": admission.sources,
        "claims": canonical_claims,
        "query_audit": queries,
    }
    errors = V.structural_errors(catalog, V.load_schema("research-catalog"))
    errors += V.research_catalog_errors(catalog)
    if errors:
        # record-research-failure-trace: the errors travel with the failure.
        raise ResearchPolicyError("content_validation_failed", decisions=admission.decisions,
                                  diagnostics={**diagnostics, "validation_errors": errors[:50]})
    if not_chinese:
        diagnostics["claims_not_in_chinese"] = not_chinese
    trace = {
        "policy_version": POLICY_VERSION,
        "verifier_prompt_version": PROMPT_VERSION,
        "source_decisions": admission.decisions,
        "fetched_sources": admission.fetched,
        "verdicts": [dict(item) for item in verdicts],
        "admissibility": diagnostics,
    }
    return catalog, trace
