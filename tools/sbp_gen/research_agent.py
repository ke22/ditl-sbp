"""Restricted OpenAI Responses adapter for de-identified As-is research.

The session declares exactly two capabilities: the provider's hosted web search
for discovery, and one client-executed ``research_fetch`` function for source
bodies.  Because capability on this API comes only from the request, a tool the
request does not declare cannot be called — there is no ambient inventory to
deny.  A call naming anything undeclared is therefore treated as a policy
rejection rather than ignored, so a provider-side surprise fails closed.

Only ``research_fetch`` crosses this application's policy boundary; the hosted
search runs on the provider's side and its results reach the model directly.
The audit trail covers fetches performed here and no others.  That was equally
true of the ``WebSearch`` tool this replaces, so the trail's coverage is
unchanged.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from . import cli_runner, research_privacy, transport_llm
from .source_excerpt import excerpt_source
from .source_snapshot import SnapshotBackedFetcher, SourceSnapshotStore


SEARCH_TOOL = "web_search"
FETCH_TOOL = "research_fetch"
PERMITTED_TOOLS = frozenset({SEARCH_TOOL, FETCH_TOOL})
DEFAULT_MODEL = "gpt-5"
# 12 是為「來源拿不到就整次中止」的舊行為訂的。改成「拿不到就換一個來源繼續」
# 之後，measure-henglong 的真實紀錄顯示：12 回合內模型已經送出 23 次
# research_fetch（部分回合一次送兩個），卻連一次都還沒輪到最終答案就撞上限——
# 不是調研判斷証據不足，是回合數配額不夠讓它做完更嚴格來源契約要求的搜尋。這次
# 換成 OpenAI 之後 30 也不夠：move-external-research-to-openai 的真實驗收（task
# 4.4）跟 validate-integrated-desktop-workbench-experience 真實跑 muji-cafe-
# meal-001 的 as-is-research（task 4.2）都在 30 回合內撞上限而中止；同一個議題
# 重跑到約 42 回合才收斂到最終答案，累計花費遠低於舊的 8 美元上限（token 總量的
# 瓶頸從來不是預算，是回合數）——gpt-5 搭配代管搜尋，在這個嚴格來源契約下傾向
# 一次只驗證一個候選來源就送出一輪，不是舊模型那種一次多來源批次處理。
DEFAULT_MAX_TURNS = 50
DEFAULT_MAX_BUDGET_USD = 20.0
# add-codex-cli-runner: an authenticated CLI runner is not billed per token, so
# a CLI session is bounded by the turn ceiling and this wall-clock deadline,
# which covers every runner call in the session (excerpt selection included).
CLI_RESEARCH_DEADLINE_SECONDS = 30 * 60
RESPONSES_URL = "https://api.openai.com/v1/responses"
# 成本上限在這裡自己算，因為 Responses API 沒有預算上限這個參數（被取代的
# runtime 是把上限交給 provider 執行）。單價這個 repo 不追蹤，所以用一個
# 設定值估算——回合數才是主要的界限，成本上限是防暴衝的第二層。
DEFAULT_USD_PER_MTOK = 5.0
# `items: {"type": "object"}` said nothing about what a source or a claim needs,
# while research_policy rejected anything missing its fields. Naming them here
# as well as in the prompt makes the structured-output constraint carry the
# contract, rather than leaving it to be inferred.
SOURCE_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "source_class", "title", "authors_or_publisher", "locator",
        "url", "authoritative", "fetched",
    ],
    "properties": {
        "source_class": {
            "type": "string",
            "enum": ["academic", "government", "professional_body", "first_party_case"],
        },
        "title": {"type": "string"},
        "authors_or_publisher": {"type": "string"},
        "locator": {"type": "string"},
        "url": {"type": "string"},
        "doi": {"type": "string"},
        "authoritative": {"type": "boolean"},
        "fetched": {"type": "boolean"},
        "scholarly_metadata": {"type": "boolean"},
        "describes_own_implementation": {"type": "boolean"},
        "observable_result": {"type": "string"},
    },
}

# fix-research-applicability-context-contract: the research catalog requires a
# non-empty applicability context, so the contract asks for it; when the source
# states none, this fixed sentence says so instead of an inferred context.
APPLICABILITY_NOT_STATED = "來源沒有說明特定的適用條件。"
CLAIM_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["text", "applicability_context", "source_links"],
    "properties": {
        "text": {"type": "string"},
        "summary": {"type": "string"},
        "applicability_context": {"type": "string"},
        "question_refs": {"type": "array", "items": {"type": "string"}},
        "source_links": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["source_ref", "locator", "passage"],
                "properties": {
                    "source_ref": {"type": "string"},
                    "locator": {"type": "string"},
                    "passage": {"type": "string"},
                },
            },
        },
    },
}

RESEARCH_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sources", "claims", "query_audit"],
    "properties": {
        "sources": {"type": "array", "items": SOURCE_ITEM_SCHEMA},
        "claims": {"type": "array", "items": CLAIM_ITEM_SCHEMA},
        "query_audit": {"type": "array", "items": {"type": "object"}},
    },
}

# Defense in depth. ``tools=[WebSearch]`` removes all other built-ins from the
# model context; strict MCP configuration removes ambient MCP servers; this
# list documents and explicitly denies the capabilities this workflow must
# never acquire.
DISALLOWED_TOOLS = (
    "Bash",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "NotebookEdit",
    "Task",
    "Agent",
    "Computer",
    "WebFetch",
    "mcp__filesystem__read_file",
    "mcp__filesystem__write_file",
    "mcp__github__*",
    "mcp__browser__*",
)


class ResearchAgentError(RuntimeError):
    """Stable failure raised across the SDK and network policy boundary."""

    def __init__(self, code: str, *, audit: Iterable[Mapping[str, Any]] = ()):
        super().__init__(code)
        self.code = code
        self.audit = [dict(event) for event in audit]


def failure_causes(exc: BaseException, *, limit: int = 8) -> list[dict[str, str]]:
    """record-research-failure-trace: the chain of errors behind a failed
    research step, outermost first, for the operation's local record only.
    Each message is whitespace-collapsed and at most 300 characters."""
    causes: list[dict[str, str]] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(causes) < limit:
        seen.add(id(current))
        causes.append({"type": type(current).__name__, "message": " ".join(str(current).split())[:300]})
        current = current.__cause__ or current.__context__
    return causes


class ToolPolicyError(ResearchAgentError):
    """This application refused the request. Fail closed."""

    def __init__(self, *, audit: Iterable[Mapping[str, Any]] = ()):
        super().__init__("tool_policy_rejected", audit=audit)


class ResearchTimeoutError(ResearchAgentError):
    """The research call did not answer in time. Says nothing about the data."""

    def __init__(self, *, audit: Iterable[Mapping[str, Any]] = ()):
        super().__init__("research_timed_out", audit=audit)


class ResearchServiceError(ResearchAgentError):
    """The research service refused or errored. Carries only the status code."""

    def __init__(self, *, status: int | None = None, audit: Iterable[Mapping[str, Any]] = ()):
        super().__init__("research_service_error", audit=audit)
        self.status = status


class SourceUnavailableError(ResearchAgentError):
    """The remote source would not serve us. Nothing to do with our policy.

    Kept separate from ToolPolicyError because the two demand opposite
    handling. A policy rejection is a security decision and must end the
    operation. A publisher answering 403 to a crawler — ScienceDirect does,
    which is how this distinction was found — says nothing about safety; the
    right response is to tell the agent that source did not work so it can
    cite a different one. Collapsing them meant one unreachable URL killed a
    whole research run and told the researcher it had stopped 因安全限制.
    """

    def __init__(self, *, audit: Iterable[Mapping[str, Any]] = (), reason: str | None = None):
        super().__init__("source_unavailable", audit=audit)
        # Why the source was skipped, when it is worth telling apart in the
        # audit (e.g. https_downgrade); None for the ordinary cases.
        self.reason = reason


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _redacted_error(value: object) -> str:
    return _digest_bytes(repr(value).encode("utf-8"))


def _normalized_urls(value: Any) -> list[str]:
    found: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, str):
            for token in item.replace('"', " ").replace("'", " ").split():
                candidate = token.rstrip(".,);]}")
                if candidate.startswith("https://"):
                    try:
                        found.add(normalize_public_url(candidate))
                    except ToolPolicyError:
                        continue
        elif isinstance(item, Mapping):
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return sorted(found)


def _deidentified_input(tool_name: str, tool_input: Mapping[str, Any]) -> dict[str, Any]:
    if tool_name == SEARCH_TOOL:
        return {"query": str(tool_input.get("query") or "")}
    if tool_name == FETCH_TOOL:
        return {"url": str(tool_input.get("url") or "")}
    return {"input_digest": _digest_bytes(_canonical_json(tool_input).encode("utf-8"))}


@dataclass
class ToolAuditTrail:
    """Append-only ordered tool audit with matching sequence identities."""

    clock: Callable[[], str] = _now
    _events: list[dict[str, Any]] = field(default_factory=list)
    _sequence: int = 0
    _open: dict[str, int] = field(default_factory=dict)
    _closed: set[int] = field(default_factory=set)

    def events(self) -> list[dict[str, Any]]:
        return [dict(event) for event in self._events]

    def start(
        self,
        tool_name: str,
        tool_input: Mapping[str, Any],
        *,
        decision: str,
        tool_use_id: str | None = None,
    ) -> int:
        self._sequence += 1
        sequence = self._sequence
        key = tool_use_id or f"sequence:{sequence}"
        self._open[key] = sequence
        self._events.append({
            "sequence": sequence,
            "event": "pre_use",
            "tool": tool_name,
            "policy_decision": decision,
            "input": _deidentified_input(tool_name, tool_input),
            "at": self.clock(),
        })
        return sequence

    def finish(
        self,
        tool_name: str,
        *,
        tool_use_id: str | None = None,
        sequence: int | None = None,
        outcome: str,
        response: Any = None,
        metadata: Mapping[str, Any] | None = None,
        error: object | None = None,
    ) -> int:
        if sequence is None and tool_use_id:
            sequence = self._open.pop(tool_use_id, None)
        if sequence is None:
            sequence = self.start(tool_name, {}, decision="observed", tool_use_id=tool_use_id)
        if sequence in self._closed:
            return sequence
        self._closed.add(sequence)
        body = _canonical_json(response).encode("utf-8") if response is not None else b""
        event: dict[str, Any] = {
            "sequence": sequence,
            "event": "terminal",
            "tool": tool_name,
            "outcome": outcome,
            "at": self.clock(),
            "returned_urls": _normalized_urls(response),
            "response_metadata": dict(metadata or {}),
            "content_digest": _digest_bytes(body),
        }
        if error is not None:
            event["error_digest"] = _redacted_error(error)
        self._events.append(event)
        return sequence

    def deny(
        self,
        tool_name: str,
        tool_input: Mapping[str, Any],
        *,
        tool_use_id: str | None = None,
        reason: object = "denied",
    ) -> None:
        sequence = self._open.get(tool_use_id or "")
        if sequence is None:
            sequence = self.start(tool_name, tool_input, decision="denied", tool_use_id=tool_use_id)
        self.finish(
            tool_name,
            tool_use_id=tool_use_id,
            sequence=sequence,
            outcome="denied",
            error=reason,
        )


@dataclass(frozen=True)
class FetchPolicy:
    redirect_limit: int = 5
    max_bytes: int = 2_000_000
    timeout_seconds: float = 15.0


@dataclass(frozen=True)
class FetchResponse:
    status: int
    url: str
    headers: Mapping[str, str]
    body: bytes


def normalize_public_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ToolPolicyError() from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ToolPolicyError()
    if parsed.username is not None or parsed.password is not None:
        raise ToolPolicyError()
    hostname = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    if not hostname or hostname == "localhost":
        raise ToolPolicyError()
    netloc = hostname
    if ":" in hostname:
        netloc = f"[{hostname}]"
    if port is not None and port != 443:
        netloc += f":{port}"
    path = parsed.path or "/"
    return urllib.parse.urlunsplit(("https", netloc, path, parsed.query, ""))


def validate_public_https_url(
    url: str,
    *,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
) -> str:
    normalized = normalize_public_url(url)
    parsed = urllib.parse.urlsplit(normalized)
    # A resolution failure (unknown host, no DNS record, transient lookup
    # error) is left to propagate as the plain OSError it is, not rewrapped
    # into ToolPolicyError -- discovered live: gpt-5's hosted search
    # occasionally cites a URL whose hostname does not resolve at all, and
    # collapsing that into a policy rejection killed the whole research run
    # over one dead link, the exact mistake SourceUnavailableError's
    # docstring already describes for the HTTP layer, just one layer
    # earlier. fetch()'s own exception handler already reclassifies a bare
    # OSError from this call as SourceUnavailableError; only a resolution
    # that *succeeds* and returns a private or otherwise non-global address
    # is the security decision that belongs to ToolPolicyError.
    addresses = resolver(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    if not addresses:
        raise ToolPolicyError()
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address[4][0])
        except (IndexError, TypeError, ValueError) as exc:
            raise ToolPolicyError() from exc
        if not ip.is_global:
            raise ToolPolicyError()
    return normalized


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):  # noqa: ANN001
        return None


def _request_once(url: str, timeout: float, max_bytes: int) -> FetchResponse:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "ditl-sbp-research/1.0", "Accept": "text/html,application/pdf,text/plain"},
        method="GET",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            # 太大就是拿不動，換一個來源；不是安全事件。
            raise SourceUnavailableError()
        return FetchResponse(
            status=int(response.status),
            url=str(response.geturl()),
            headers={str(key).lower(): str(value) for key, value in response.headers.items()},
            body=body,
        )


class ResearchFetcher:
    """SSRF-resistant, bounded fetcher used by the in-process MCP tool."""

    def __init__(
        self,
        *,
        audit: ToolAuditTrail,
        policy: FetchPolicy = FetchPolicy(),
        resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
        request_once: Callable[[str, float, int], FetchResponse] = _request_once,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.audit = audit
        self.policy = policy
        self.resolver = resolver
        self.request_once = request_once
        self.monotonic = monotonic

    def fetch(self, url: str) -> dict[str, Any]:
        sequence = self.audit.start(FETCH_TOOL, {"url": url}, decision="allowed")
        started = self.monotonic()
        current = url
        redirects: list[str] = []
        last_validated_url: str | None = None
        try:
            for redirect_count in range(self.policy.redirect_limit + 1):
                elapsed = self.monotonic() - started
                remaining = self.policy.timeout_seconds - elapsed
                if remaining <= 0:
                    # 資源上限（時間／大小／轉址次數）跟安全邊界是兩回事：這些
                    # 只是說「這個來源我們拿不動」，不是說它有危險。仍然中止這
                    # 一次抓取、不放寬任何限制，但讓代理人去換下一個來源，而不
                    # 是整次調研陪葬。SSRF 那組檢查（協定、主機、內網位址、帶
                    # 帳密）維持 ToolPolicyError，失敗關門。
                    raise SourceUnavailableError(audit=self.audit.events())
                current = validate_public_https_url(current, resolver=self.resolver)
                last_validated_url = current
                response = self.request_once(
                    current,
                    min(remaining, self.policy.timeout_seconds),
                    self.policy.max_bytes,
                )
                if self.monotonic() - started > self.policy.timeout_seconds:
                    raise SourceUnavailableError(audit=self.audit.events())
                if len(response.body) > self.policy.max_bytes:
                    raise SourceUnavailableError(audit=self.audit.events())
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location or redirect_count >= self.policy.redirect_limit:
                        raise SourceUnavailableError(audit=self.audit.events())
                    current = urllib.parse.urljoin(current, location)
                    target = urllib.parse.urlsplit(current)
                    if target.scheme.lower() == "http":
                        # skip-source-on-https-downgrade-redirect: a public
                        # site sending us to its own http page is a source we
                        # will not read, not an attack. Run every other check
                        # on it as https first -- private address space or
                        # credentials still fail closed -- then skip the
                        # source without ever requesting the http URL.
                        validate_public_https_url(urllib.parse.urlunsplit(("https", *target[1:])),
                                                  resolver=self.resolver)
                        raise SourceUnavailableError(audit=self.audit.events(), reason="https_downgrade")
                    # Validate before a body can ever be requested from this target.
                    current = validate_public_https_url(current, resolver=self.resolver)
                    last_validated_url = current
                    redirects.append(current)
                    continue
                if not 200 <= response.status < 300:
                    # 對方拒絕或出錯（403/404/5xx）——換一個來源就好，不是安全事件。
                    raise SourceUnavailableError(audit=self.audit.events())
                content_type = response.headers.get("content-type", "application/octet-stream")
                charset = "utf-8"
                if "charset=" in content_type:
                    charset = content_type.rsplit("charset=", 1)[-1].split(";", 1)[0].strip()
                text = response.body.decode(charset, errors="replace")
                result = {
                    "url": current,
                    "status": response.status,
                    "content_type": content_type,
                    "body": text,
                    "body_digest": _digest_bytes(response.body),
                    "redirects": redirects,
                }
                self.audit.finish(
                    FETCH_TOOL,
                    sequence=sequence,
                    outcome="success",
                    response={"url": current, "redirects": redirects},
                    metadata={
                        "status": response.status,
                        "content_type": content_type,
                        "bytes": len(response.body),
                        "elapsed_ms": round((self.monotonic() - started) * 1000),
                    },
                )
                return result
            raise SourceUnavailableError(audit=self.audit.events())
        except Exception as exc:
            reason = exc.reason if isinstance(exc, SourceUnavailableError) else None
            self.audit.finish(
                FETCH_TOOL,
                sequence=sequence,
                outcome="failure",
                response={"url": last_validated_url, "redirects": redirects},
                metadata={"skip_reason": reason} if reason else None,
                error=exc,
            )
            if isinstance(exc, ToolPolicyError):
                raise ToolPolicyError(audit=self.audit.events()) from exc
            if isinstance(exc, SourceUnavailableError):
                raise SourceUnavailableError(audit=self.audit.events(), reason=reason) from exc
            # 連不上、逾時、對方送回壞掉的內容——都是那個來源的問題。未知的例外
            # 仍然當成政策拒絕（失敗關門），只有明確認得出來的網路錯誤才放行換源。
            if isinstance(exc, (urllib.error.URLError, TimeoutError, socket.timeout, OSError)):
                raise SourceUnavailableError(audit=self.audit.events()) from exc
            raise ToolPolicyError(audit=self.audit.events()) from exc


@dataclass
class ResearchRuntime:
    """One bounded session's declared request plus the state it accumulates."""

    request: dict[str, Any]
    audit: ToolAuditTrail
    fetcher: ResearchFetcher
    policy_rejected: bool = False
    turns: int = 0
    usd_spent: float = 0.0
    # 抓到的完整內容留在這裡，不出去也不靠模型抄回來。引用查核要對照的是這一
    # 份，而不是模型自己交出來的 body——那份沒有跟實際抓到的位元組核對過，等
    # 於讓被引用的一方自己提供證物。
    fetched_bodies: dict[str, str] = field(default_factory=dict)
    excerpt_usage: dict[str, Any] = field(default_factory=lambda: {"tokens": 0, "seconds": 0})
    # research-admissibility-diagnostics: per fetched URL, where the excerpts
    # shown to the model came from.
    excerpt_provenance: dict[str, dict[str, Any]] = field(default_factory=dict)
    runner_usage: dict[str, Any] = field(default_factory=lambda: {"tokens": 0, "search": []})


def _default_transport(url: str, headers: Mapping[str, str], body: Mapping[str, Any]) -> dict[str, Any]:
    """POST one Responses request.  Injected in tests; never called by them.

    Transport failures are raised as themselves, not swallowed: the caller
    classifies them. Collapsing a timeout into "the evidence did not support
    the judgement" hid the single most common failure this stage has.
    """
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method="POST")
    for name, value in headers.items():
        request.add_header(name, value)
    opener = urllib.request.build_opener()
    try:
        with opener.open(request, timeout=DEFAULT_HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ResearchServiceError(status=exc.code) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise ResearchTimeoutError() from exc
    except urllib.error.URLError as exc:
        # URLError wraps a socket timeout on some platforms rather than
        # raising TimeoutError directly.
        if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
            raise ResearchTimeoutError() from exc
        raise ResearchServiceError() from exc


# 一次 research 呼叫不是一般的 API 往返：模型會在回應之前真的去做代管網頁搜尋，
# 所以單次回合就可能跑好幾分鐘。原本設 120 秒，實測 measure-henglong 正好卡在
# 120.0 秒逾時，MUJI 那幾次也只差 12 秒就中；而逾時又被歸類成「外部資料未能支持
# 研究判斷」，於是畫面上講的是資料不夠格，真正發生的卻是我們自己等不夠久。
DEFAULT_HTTP_TIMEOUT = 600.0

RESEARCH_FETCH_TOOL_SPEC: dict[str, Any] = {
    "type": "function",
    "name": FETCH_TOOL,
    "description": "Fetch one public HTTPS research source through the application policy boundary.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["url"],
        "properties": {"url": {"type": "string"}},
    },
}

HOSTED_SEARCH_TOOL_SPEC: dict[str, Any] = {"type": SEARCH_TOOL}


def build_research_runtime(
    *,
    known_values: Mapping[str, str] | Iterable[str] | None = None,
    evidence_spans: Iterable[str] = (),
    api_key: str | None = None,
    model: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_budget_usd: float = DEFAULT_MAX_BUDGET_USD,
    audit: ToolAuditTrail | None = None,
    fetcher: ResearchFetcher | None = None,
    requires_key: bool = True,
    snapshots: SourceSnapshotStore | None = None,
) -> ResearchRuntime:
    """Build one session's request body.  No network call is made.

    `requires_key` is False only where the session's turns will be relayed by
    hand (relay-external-research-through-manual-transport). The request body
    is wanted on that path too -- it carries the declared tools and the output
    schema the relay is shown -- but nothing is sent, so there is no request
    to authorize and no key to demand.

    `snapshots`, when given a `source_snapshot.SourceSnapshotStore`, wraps
    whichever fetcher this call would otherwise use (the default one built
    here, or one a caller injected) so a URL already snapshotted for this
    operation is reconstructed rather than fetched again
    (persist-external-research-source-snapshots-for-replay). Wrapping happens
    regardless of where the fetcher came from, which is what lets a test's
    injected fetcher be covered by the same replay guarantee production gets.
    """
    key = api_key or os.environ.get("OPENAI_API_KEY")
    if not key and requires_key:
        raise ResearchAgentError("research_configuration_missing")
    if max_turns <= 0 or max_budget_usd <= 0:
        raise ResearchAgentError("research_configuration_missing")
    trail = audit or ToolAuditTrail()
    network = fetcher or ResearchFetcher(audit=trail)
    if snapshots is not None:
        network = SnapshotBackedFetcher(network, snapshots)
    request = {
        "model": model or os.environ.get("SBP_RESEARCH_MODEL") or DEFAULT_MODEL,
        # 只宣告這兩個能力。這個 API 上「沒宣告就叫不到」，所以不需要（也不能）
        # 列一份禁用清單——列了反而像是有一道其實不存在的檢查。
        "tools": [RESEARCH_FETCH_TOOL_SPEC, HOSTED_SEARCH_TOOL_SPEC],
        "tool_choice": "auto",
        "text": {
            "format": {
                "type": "json_schema",
                "name": "as_is_research",
                "strict": False,
                "schema": RESEARCH_OUTPUT_SCHEMA,
            }
        },
        "store": False,
    }
    return ResearchRuntime(request=request, audit=trail, fetcher=network)


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _tool_calls(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Function calls the model is asking this code to satisfy.

    Hosted-search items run on the provider's side and appear in the output for
    transparency; they are not requests to this code, so they are skipped here.
    """
    calls = []
    for item in response.get("output") or []:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("type") or "") in ("function_call", "tool_call"):
            calls.append(dict(item))
    return calls


def _structured_output(response: Mapping[str, Any]) -> dict[str, Any] | None:
    """The schema-constrained answer, if this response carries one."""
    for item in response.get("output") or []:
        if not isinstance(item, Mapping) or str(item.get("type") or "") != "message":
            continue
        for part in item.get("content") or []:
            if not isinstance(part, Mapping):
                continue
            parsed = part.get("parsed")
            if isinstance(parsed, Mapping):
                return dict(parsed)
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                try:
                    value = json.loads(text)
                except ValueError:
                    continue
                if isinstance(value, Mapping):
                    return dict(value)
    return None


def _usage_usd(response: Mapping[str, Any], usd_per_mtok: float) -> float:
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        return 0.0
    total = usage.get("total_tokens")
    if not isinstance(total, (int, float)):
        total = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
    try:
        return (float(total) / 1_000_000.0) * float(usd_per_mtok)
    except (TypeError, ValueError):
        return 0.0


def _call_arguments(call: Mapping[str, Any]) -> dict[str, Any]:
    raw = call.get("arguments")
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError:
            return {}
        return dict(value) if isinstance(value, Mapping) else {}
    return {}


# 這份契約是 research_policy 真的會執行的規則，不是建議。少填一個欄位，那筆來源
# 就會被退掉，而且退件理由只留雜湊——實測 measure-henglong 七筆來源全部因為沒有
# source_class 被退，模型從頭到尾不知道有這個欄位。跟 issues.md 那次是同一個病：
# 驗證器在檢查一條沒人告訴模型的規則。
SOURCE_CONTRACT = """Every object in `sources` must carry all of:

- `source_class`: exactly one of "academic", "government", "professional_body",
  "first_party_case". A source that fits none of these cannot be used — do not
  guess a value; drop the source instead.
- `title`
- `authors_or_publisher`
- `locator`: where in the document the material sits (page, section, table).
- `url` (https) or `doi` (a real 10.xxxx/... identifier).
- `authoritative`: true — set it only if the identity above is verified.
- `fetched`: true — every source must have been retrieved with the fetch tool.

Conditional, and enforced:
- `source_class: "academic"` also needs `scholarly_metadata: true` and a stable
  `doi` or `url`.
- `source_class: "first_party_case"` also needs
  `describes_own_implementation: true` and a non-empty `observable_result`.

Prefer sources whose full text the fetch tool can read: government and
standards bodies, professional associations, and open-access papers. A
publisher landing page or paywalled abstract gives nothing to quote, so every
claim resting on it is dropped.

Do not include a source you could not fetch, and do not include news coverage,
personal blogs, aggregators, or vendor marketing without a first-party result:
each of those is rejected outright.
"""

CLAIM_CONTRACT = """Every object in `claims` must carry:

- `text`: the bounded hypothesis itself.
- `summary` (optional): one sentence of at most 30 characters that tells the
  researcher what the claim says, adding no fact the claim does not hold.
- `applicability_context`: where the source says this holds -- the kind of
  service and the conditions it was observed under, as the source itself
  states them. Do not infer a context the source does not support. When the
  source states no specific applicability conditions, write exactly:
  """ + APPLICABILITY_NOT_STATED + """
- `question_refs` (optional): the `alias` of each question in the payload
  this claim answers, for example `Q-001`. Leave it out when unsure.
- `source_links`: at least one object, each with
  - `source_ref` (the matching source's `url` or `doi`),
  - `locator`,
  - `passage`: **copied character-for-character** from that source's excerpts.

`passage` is checked as a literal substring of the document this application
fetched. Quote only from the `excerpts` returned by the fetch tool — those are
verbatim spans of the source. A rewritten, translated, tidied, or joined
passage resolves against nothing and the claim is dropped.

Write `text`, `summary` and `applicability_context` in Traditional Chinese as used in
Taiwan, even when the source is in English or another language; the researcher
reads them as written. Keep `passage`, source titles and publisher names exactly
as the source has them.
"""


def research_prompt(projection: Mapping[str, Any]) -> str:
    mode = projection.get("research_mode")
    if mode == "sbp_evaluation":
        task = ("Evaluate the de-identified questions about the reviewed service blueprint and researcher-selected solutions below. "
                "Check feasibility, channel delivery, operational support, resources and weak assumptions. "
                "Treat these as design decisions and model inferences, not participant observations. ")
    elif mode == "implementation":
        # pre-blueprint-implementation-research: before the blueprint exists.
        task = ("Research how comparable services implemented the de-identified practices in the questions below: "
                "the approaches they used, the conditions those approaches required, and the limits they met. "
                "The practices are design directions a researcher adopted, not participant observations. ")
    else:
        task = "Research only the de-identified present-state service issues below. "
    return (
        task + f"Use {SEARCH_TOOL} for discovery and {FETCH_TOOL} for every source body. "
        "Return candidate sources and bounded hypotheses; do not propose a To-be solution.\n\n"
        + SOURCE_CONTRACT + "\n" + CLAIM_CONTRACT + "\n"
        + research_privacy.projection_json(projection)
    )


def run_research_agent(projection: Mapping[str, Any], **options: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run one bounded research session; a CLI session also gets a wall-clock deadline."""
    if options.get("transport") is None and not _relay_selected() and _cli_selected():
        with cli_runner.deadline(CLI_RESEARCH_DEADLINE_SECONDS):
            return _run_research_session(projection, **options)
    return _run_research_session(projection, **options)


def _run_research_session(
    projection: Mapping[str, Any],
    *,
    known_values: Mapping[str, str] | Iterable[str] | None = None,
    evidence_spans: Iterable[str] = (),
    api_key: str | None = None,
    model: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_budget_usd: float = DEFAULT_MAX_BUDGET_USD,
    transport: Callable[[str, Mapping[str, str], Mapping[str, Any]], Mapping[str, Any]] | None = None,
    usd_per_mtok: float = DEFAULT_USD_PER_MTOK,
    excerpt_opts: Mapping[str, Any] | None = None,
    excerpt: Callable[..., tuple[list[str], dict[str, Any]]] | None = None,
    source_snapshots: SourceSnapshotStore | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run one bounded research session and return structured candidates.

    The loop is owned here rather than by the provider:每一次工具呼叫的政策判斷、
    審計的開始與結束、回合與成本的累計，都發生在這個 repo 自己測得到的位置。
    """
    prompt = research_prompt(projection)
    issues = list((projection or {}).get("issues") or [])
    # 隱私邊界在任何一次請求之前就檢查，跟被取代的 runtime 一樣。
    research_privacy.validate_external_payload(
        prompt,
        known_values=known_values,
        evidence_spans=evidence_spans,
        field_prefix="agent_prompt",
    )
    key = api_key or os.environ.get("OPENAI_API_KEY")
    # relay-external-research-through-manual-transport: an injected transport
    # still wins, so every test that supplies one is unaffected. With none
    # injected the provider decides, the same way the rest of the pipeline
    # resolves one.
    relaying = transport is None and _relay_selected()
    cli_mode = transport is None and _cli_selected()
    runtime = build_research_runtime(
        known_values=known_values,
        evidence_spans=evidence_spans,
        api_key=key,
        requires_key=not relaying and not cli_mode,
        model=model,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        snapshots=source_snapshots,
    )
    # Excerpt selection is its own model call inside this session, and it
    # resolves its own provider. Left alone it defaults to a live one, which on
    # the relay path fails for a missing key and is then swallowed by
    # excerpt_source's degrade-don't-die fallback — every source silently comes
    # back as the document's opening span instead of quotable prose. The same
    # `relaying` decision that picks the transport above governs it.
    excerpt_options = dict(excerpt_opts or {})
    if not excerpt_options.get("excerpt_provider") and (relaying or cli_mode):
        excerpt_options["excerpt_provider"] = "transport" if relaying else "cli"
    send = transport or (relay_transport if relaying else cli_transport if cli_mode else _default_transport)
    # Nothing is sent on the relay path, so there is nothing to authorize.
    headers: dict[str, str] = {} if relaying or cli_mode else _auth_headers(str(key))
    conversation: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

    def _budget_exhausted() -> ResearchAgentError:
        return ResearchAgentError("research_budget_exhausted", audit=runtime.audit.events())

    while True:
        if runtime.turns >= int(max_turns):
            raise _budget_exhausted()
        runtime.turns += 1
        body = {**runtime.request, "input": list(conversation)}
        try:
            response = send(RESPONSES_URL, headers, body)
        except ResearchAgentError as exc:
            # Re-raise carrying this session's audit, so a transport failure
            # still shows what had been fetched before it happened.
            exc.audit = runtime.audit.events()
            raise
        except (TimeoutError, socket.timeout) as exc:
            # Classified here as well as in the default transport: a timeout is
            # a timeout wherever it surfaces, including from an injected one.
            raise ResearchTimeoutError(audit=runtime.audit.events()) from exc
        except Exception as exc:
            # Anything the transport did not already classify. Still not a
            # statement about the evidence — that verdict belongs to the
            # citation checks further down, not to a failed round trip.
            raise ResearchServiceError(audit=runtime.audit.events()) from exc
        if not isinstance(response, Mapping):
            raise ResearchServiceError(audit=runtime.audit.events())
        # A relayed reply carries no token counts. Reporting no spend rather
        # than an invented one leaves the turn ceiling as what bounds the
        # session -- which is the bound that actually ends a runaway one.
        # A CLI runner reports tokens but is not billed per token; converting
        # them into dollars would invent a cost, so CLI adds nothing here either.
        runtime.usd_spent += 0.0 if relaying or cli_mode else _usage_usd(response, usd_per_mtok)
        if cli_mode:
            reported = response.get("runner_usage") or {}
            runtime.runner_usage["tokens"] += int(reported.get("tokens") or 0)
            if reported.get("search"):
                runtime.runner_usage["search"].append(reported["search"])
        if runtime.usd_spent > float(max_budget_usd):
            raise _budget_exhausted()

        calls = _tool_calls(response)
        if not calls:
            result = _structured_output(response)
            if result is None:
                raise ResearchAgentError(
                    "citation_support_rejected", audit=runtime.audit.events()
                )
            # 帶回本地保留的完整內容，供引用查核使用（見 research_policy
            # 那邊改成優先採用這一份，而不是模型自己交的 body）。
            result = dict(result)
            result["fetched_bodies"] = dict(runtime.fetched_bodies)
            result["excerpt_usage"] = dict(runtime.excerpt_usage)
            result["excerpt_provenance"] = dict(runtime.excerpt_provenance)
            if cli_mode:
                result["runner_usage"] = {
                    "tokens": runtime.runner_usage["tokens"],
                    "search": sorted(set(runtime.runner_usage["search"])),
                }
            return result, runtime.audit.events()

        for call in calls:
            name = str(call.get("name") or "")
            call_id = str(call.get("call_id") or call.get("id") or "")
            arguments = _call_arguments(call)
            if name != FETCH_TOOL:
                # 沒宣告過的能力：失敗關門，不是忽略。
                runtime.policy_rejected = True
                runtime.audit.deny(
                    name or "unknown", arguments,
                    tool_use_id=call_id or None,
                    reason="tool not declared by this session",
                )
                raise ToolPolicyError(audit=runtime.audit.events())
            conversation.append(dict(call))
            try:
                value = runtime.fetcher.fetch(str(arguments.get("url") or ""))
            except SourceUnavailableError:
                # 這個來源拿不到，不是這次調研出事。把結果告訴模型讓它改引別的
                # 來源；輪數與預算上限本來就在外層擋著，不會無限試下去。
                conversation.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": "This source could not be retrieved. Cite a different source.",
                })
                continue
            except ToolPolicyError:
                runtime.policy_rejected = True
                conversation.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": "Fetch rejected by network policy.",
                })
                raise ToolPolicyError(audit=runtime.audit.events())
            # 只把逐字節錄送進對話，完整內容留在本地。以前是整份文件進對話、
            # 模型還要把 body 原樣寫回結構化輸出，同一份文字來回搬兩趟——十一
            # 份來源累積 5.8 MB，請求被回 400。節錄由 Gemini 挑、逐字比對過才
            # 送出（見 source_excerpt）。
            url = str(value.get("url") or arguments.get("url") or "")
            body = str(value.get("body") or "")
            if url and body:
                # Keyed by the URL the model asked for and the one the fetch
                # ended at: both name content this application retrieved.
                runtime.fetched_bodies[url] = body
                requested = str(arguments.get("url") or "")
                if requested:
                    runtime.fetched_bodies[requested] = body
            # 注入點：節錄這一步會呼叫模型，測試必須能換掉它。沒有注入點的話，
            # 只要環境裡剛好有金鑰，單元測試就會真的打網路——實測發生過。
            try:
                excerpts, usage = (excerpt or excerpt_source)(body, issues, excerpt_options)
            except transport_llm.TransportStale as exc:
                # persist-external-research-source-snapshots-for-replay:
                # excerpt_source() now re-raises this instead of swallowing it
                # into a degraded excerpt (a mismatch here means the snapshot
                # this operation relies on did not actually keep the prompt
                # byte-identical — a real inconsistency worth stopping for,
                # not a single awkward source). Wrapped the same way
                # relay_transport() wraps the same exception from the main
                # session loop, so it fails the operation loudly instead of
                # being read as an ordinary excerpt-extraction failure.
                raise RelayReplyError(str(exc), audit=runtime.audit.events()) from exc
            for key in ("tokens", "seconds"):
                runtime.excerpt_usage[key] = runtime.excerpt_usage.get(key, 0) + usage.get(key, 0)
            provenance = {"provenance": usage.get("provenance") or "unrecorded", "excerpts": len(excerpts)}
            if usage.get("fallback_reason"):
                provenance["fallback_reason"] = usage["fallback_reason"]
            runtime.excerpt_provenance[url] = provenance
            conversation.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": _canonical_json({
                    "url": url,
                    "status": value.get("status"),
                    "content_type": value.get("content_type"),
                    "body_digest": value.get("body_digest"),
                    "redirects": value.get("redirects"),
                    "excerpts": excerpts,
                    "excerpts_are_verbatim": True,
                    "note": (
                        "Verbatim spans of the source. Quote only from these; "
                        "a quotation that is not a span of the source is rejected."
                    ),
                }),
            })


# --- relay-external-research-through-manual-transport -----------------------
#
# One bounded session can run without a research API key by relaying each turn
# by hand. Only `send` differs: the loop above still owns the fetch policy, the
# audit trail, the turn ceiling and the tool-policy rejection, so a change to
# any of those applies to both paths by construction rather than by being
# copied into a second loop.

RELAY_STAGE = "as_is_research"
# `transport_llm` numbers envelopes per call within a process, which is per
# turn here, and refuses a reply whose recorded envelope does not match a
# freshly rebuilt one. Both are exactly what a relayed turn needs, so this
# reuses that mechanism rather than writing a second staleness rule.
RELAY_INSTRUCTIONS = """# 受限外部調研：手動接力

你現在扮演這次調研的模型。下面「使用者訊息」是這一輪真的會送出去的請求，
裡面包含這次調研的任務、可用的能力宣告，以及目前為止的對話。

## 你只能回兩種東西，寫成一個 JSON 物件

**一、請系統去抓某幾個來源**

```json
{"fetch": ["https://example.org/a", "https://example.org/b"]}
```

只寫網址。**不要自己去抓網頁、也不要把網頁內容貼回來。** 抓取由系統執行：
它會套用網址政策、把這次抓取記進稽核軌跡、並且保留抓到的原始位元組。引用
查核比對的是系統自己抓到的那一份，不是你交回來的內容——所以你貼回來的內容
不會被採用，只會讓這一輪被退掉。下一輪的請求裡你會看到系統抓到的逐字節錄。

**二、交出最終結果**

```json
{"result": { ...符合這次請求裡宣告的輸出 schema... }}
```

## 搜尋這一步不在稽核範圍內

原本這個位置是服務商代管的網頁搜尋，搜尋結果直接進到模型、不經過這個程式的
政策邊界，稽核軌跡也只涵蓋抓取、不涵蓋搜尋。換成你來做，這件事沒有改變：
**你怎麼找到候選來源不會被記錄，被記錄的是系統照你給的網址抓了什麼。**
這裡寫出來是為了讓之後讀稽核紀錄的人不會誤以為搜尋也被驗過。

## 其他

想用上面兩種之外的能力，就照實寫 `{"tool": "<名稱>", "arguments": {...}}`——
這次請求沒有宣告的能力會被當成政策拒絕擋下來，那是正確的結果，不要為了讓它
通過而改寫成別的形式。
"""

_RELAY_BODY_KEYS = ("body", "content", "text", "html", "excerpt", "excerpts")


class RelayReplyError(ResearchAgentError):
    """A relayed reply this code will not act on.

    Uses the existing service-error code rather than a new one: from the
    interface's point of view a reply it cannot act on is the same outcome as
    a response it cannot act on, and the error codes the interface renders are
    deliberately out of this change's scope.
    """

    def __init__(self, detail: str, *, audit: Iterable[Mapping[str, Any]] = ()):
        super().__init__("research_service_failed", audit=audit)
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - message plumbing
        return self.detail


def _relay_fetch_call(url: str, index: int) -> dict[str, Any]:
    return {
        "type": "function_call",
        "name": FETCH_TOOL,
        "call_id": f"relay-{index:02d}",
        "arguments": json.dumps({"url": url}),
    }


def response_from_relay(text: str) -> dict[str, Any]:
    """Shape one relayed reply into the response mapping the loop consumes.

    Refusals are raised here rather than returned as an empty response: a
    reply this code cannot act on must not be mistaken for a session that
    produced no tool call, which is how the loop recognises a final answer.
    """
    stripped = (text or "").strip()
    if not stripped:
        raise RelayReplyError("回覆是空的。要嘛給 `fetch`（網址清單），要嘛給 `result`（最終結果）。")
    # A relay may wrap JSON in a fenced block; that is a formatting habit, not
    # a different reply, so it is unwrapped rather than refused.
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[: -len("```")]
    try:
        reply = json.loads(stripped)
    except ValueError as exc:
        raise RelayReplyError(
            "回覆不是一個 JSON 物件。只接受 `{\"fetch\": [網址]}` 或 `{\"result\": {...}}`。"
        ) from exc
    if not isinstance(reply, Mapping):
        raise RelayReplyError(
            "回覆不是一個 JSON 物件。只接受 `{\"fetch\": [網址]}` 或 `{\"result\": {...}}`。"
        )

    if "fetch" in reply:
        raw = reply.get("fetch")
        urls = [raw] if isinstance(raw, str) else list(raw or [])
        if not urls or not all(isinstance(url, str) and url.strip() for url in urls):
            raise RelayReplyError("`fetch` 要是一個網址、或一串網址，不能是別的東西。")
        # The rule the whole relay path rests on: name a source, never supply
        # one. research_policy checks quotations against the bytes this code
        # retrieved precisely so a cited source cannot supply its own
        # evidence, and a pasted body would defeat that invisibly.
        present = sorted(k for k in _RELAY_BODY_KEYS if k in reply)
        if present:
            raise RelayReplyError(
                f"回覆裡帶了來源內容（{'、'.join(present)}）。抓取要由系統做——"
                f"只給網址，不要自己抓、也不要貼內容回來。"
            )
        return {"output": [_relay_fetch_call(url.strip(), i + 1) for i, url in enumerate(urls)]}

    if "result" in reply:
        result = reply.get("result")
        if not isinstance(result, Mapping):
            raise RelayReplyError("`result` 要是一個物件，裡面是符合這次請求輸出 schema 的內容。")
        return {"output": [{"type": "message",
                            "content": [{"type": "output_text", "text": _canonical_json(result)}]}]}

    if "tool" in reply:
        # Passed through as a call rather than refused here, so the loop's own
        # policy rejection is what stops it -- one place decides what this
        # session declared, not two.
        name = str(reply.get("tool") or "").strip() or "unknown"
        arguments = reply.get("arguments")
        return {"output": [{
            "type": "function_call", "name": name, "call_id": "relay-tool",
            "arguments": json.dumps(arguments if isinstance(arguments, Mapping) else {}),
        }]}

    raise RelayReplyError(
        "回覆裡沒有 `fetch` 也沒有 `result`。這兩種是這一輪唯一接受的形式。"
    )


def relay_transport(url: str, headers: Mapping[str, str], body: Mapping[str, Any]) -> dict[str, Any]:
    """Record one turn for hand relay and, once answered, return its response.

    `headers` is ignored: nothing is sent, so there is nothing to authorize.

    Resuming replays the session from its first turn: the loop is rebuilt from
    the recorded replies, so turns already answered run again, including their
    fetches. The audit trail the completing invocation returns therefore still
    holds exactly one event per fetch that session performed -- an abandoned
    invocation's trail is discarded with its runtime -- but the same request
    really is made to the source again on each resume. That is inherent to
    resuming a stateful loop from disk and is why the turn ceiling, not a
    per-source counter, is the bound that matters here.
    """
    del url, headers
    from . import transport_llm

    try:
        result = transport_llm.call(
            stage=RELAY_STAGE,
            system=RELAY_INSTRUCTIONS,
            user=_canonical_json(body),
        )
    except transport_llm.TransportStale as exc:
        # Raised as a ResearchAgentError so the loop re-raises it with this
        # session's audit rather than folding it into a generic service
        # failure. `TransportStale` is a plain RuntimeError, so the loop's
        # `except Exception` around `send` would otherwise swallow the one
        # message that says which turn the reply was prepared against.
        raise RelayReplyError(str(exc)) from exc
    return response_from_relay(result.text)


def _relay_selected() -> bool:
    """Whether the configured provider relays model turns by hand.

    Read at call time, not at import: the environment is set per invocation,
    and the rest of the pipeline resolves its provider the same way.
    """
    return (os.environ.get("SBP_LLM_PROVIDER") or "").strip().lower() == "transport"


def _cli_selected() -> bool:
    """Whether model turns use the configured authenticated CLI wrapper."""
    return (
        (os.environ.get("SBP_EXECUTION_MODE") or "").strip().lower() == "cli"
        or (os.environ.get("SBP_LLM_PROVIDER") or "").strip().lower() == "cli"
    )


def cli_transport(url: str, headers: Mapping[str, str], body: Mapping[str, Any]) -> dict[str, Any]:
    """Route the provider-neutral Responses request through the CLI adapter."""
    del url, headers
    try:
        return cli_runner.responses(body)
    except cli_runner.CLIRunnerDeadlineExceeded as exc:
        raise ResearchTimeoutError() from exc
