"""Runner-neutral stdin/stdout protocol shared by every CLI runner wrapper.

`sbp_gen.cli_runner.invoke` writes one JSON request to the wrapper's stdin and
reads one JSON object from its stdout. This module owns that exchange so a
wrapper only has to send a prompt to its CLI and hand back the final text.

A `responses` turn (external research) reuses the manual-relay reply protocol
already specified for research: the model either names URLs for the
application to fetch or delivers the structured result. It never supplies a
source's content -- the envelope schema below makes that structurally
impossible and `parse_reply` refuses it anyway, so runner-side search can only
ever surface URLs that then go through `research_fetch`.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

_TOOLS = Path(__file__).resolve().parents[1]
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

from sbp_gen import research_agent  # noqa: E402

DEFAULT_TIMEOUT_SECONDS = 600.0
# The CLI adapter's placeholder model name means "use the runner's own default".
RUNNER_DEFAULT_MODEL = "authenticated-cli"
# Keys that would carry source content on a result's source item.
SOURCE_CONTENT_KEYS = ("body", "fetched_body", "content", "text", "html", "excerpt", "excerpts")

ENVELOPE_NOTE = """## 回覆格式

回覆必須符合這一輪附上的輸出 schema：一個有 `fetch` 與 `result` 兩個欄位的 JSON 物件，
兩者恰好一個不是 null。要系統抓來源時填 `fetch`（網址陣列）、`result` 填 null；
交出最終結果時填 `result`、`fetch` 填 null。來源項目裡不能放任何來源內容。
"""


class RunnerError(RuntimeError):
    """A researcher-safe failure code raised by a wrapper or its runner, with
    an optional diagnostic already cleaned by `clean_diagnostic`."""

    def __init__(self, code: str, diagnostic: str | None = None):
        super().__init__(code)
        self.code = code
        self.diagnostic = diagnostic or None


from sbp_gen.cli_runner import clean_diagnostic  # noqa: E402,F401  (one cleaning rule for both ends)


@dataclass(frozen=True)
class RunnerReply:
    text: str
    tokens: int = 0
    model: str | None = None
    # What the executor itself reported, per token kind; None when it reports no split.
    usage: dict | None = None
    effort: str | None = None
    # cli-model-override-reaches-runner: where `model` came from -- "workbench"
    # (the researcher chose it), "executor_settings" (inherited from the
    # researcher's own executor settings) or None (the executor's own default)
    # -- the protocol reports "policy" when the runner's per-stage table chose
    # it (cli-model-by-stage) -- and whether the executor itself reported that this model ran.
    model_source: str | None = None
    model_confirmed: bool = False


# runner(prompt, *, output_schema, web_search, model, timeout) -> RunnerReply
Runner = Callable[..., RunnerReply]


def build_completion_prompt(request: Mapping[str, Any]) -> str:
    system = str(request.get("system") or "")
    user = str(request.get("user") or "")
    return f"## 系統提示詞\n\n{system}\n\n## 使用者訊息\n\n{user}\n"


def build_responses_prompt(body: Mapping[str, Any]) -> str:
    return (
        research_agent.RELAY_INSTRUCTIONS
        + "\n"
        + ENVELOPE_NOTE
        + "\n## 使用者訊息\n\n"
        + research_agent._canonical_json(body)
        + "\n"
    )


def _is_free_object(schema: Mapping[str, Any]) -> bool:
    return schema.get("type") == "object" and not schema.get("properties")


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _strict(schema: Mapping[str, Any]) -> dict[str, Any]:
    """A schema valid under both strict and non-strict structured output.

    Every object closes additional properties and lists every property as
    required; properties that were optional become nullable. An object with no
    declared properties cannot be expressed that way, so it travels as a JSON
    string and `_restore` decodes it.
    """
    if _is_free_object(schema):
        return {"type": "string", "description": "A JSON-encoded object."}
    kind = schema.get("type")
    if kind == "object":
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or ())
        strict_properties = {}
        for name, child in properties.items():
            converted = _strict(child)
            strict_properties[name] = converted if name in required else _nullable(converted)
        return {
            "type": "object",
            "additionalProperties": False,
            "required": list(properties),
            "properties": strict_properties,
        }
    if kind == "array":
        items = schema.get("items")
        return {"type": "array", "items": _strict(items) if isinstance(items, Mapping) else {}}
    return {key: value for key, value in schema.items() if key != "additionalProperties"}


def _result_schema(body: Mapping[str, Any]) -> Mapping[str, Any]:
    fmt = ((body.get("text") or {}).get("format") or {}) if isinstance(body, Mapping) else {}
    schema = fmt.get("schema") if isinstance(fmt, Mapping) else None
    return schema if isinstance(schema, Mapping) else {"type": "object"}


def reply_schema(body: Mapping[str, Any]) -> dict[str, Any]:
    """Envelope the runner must answer a responses turn with."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["fetch", "result"],
        "properties": {
            "fetch": _nullable({"type": "array", "items": {"type": "string"}}),
            "result": _nullable(_strict(_result_schema(body))),
        },
    }


def _restore(schema: Mapping[str, Any], value: Any) -> Any:
    """Undo `_strict`: drop nulls for optional fields, decode JSON strings."""
    if _is_free_object(schema):
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except ValueError as exc:
                raise RunnerError("runner_reply_malformed") from exc
            if not isinstance(decoded, dict):
                raise RunnerError("runner_reply_malformed")
            return decoded
        return value
    kind = schema.get("type")
    if kind == "object" and isinstance(value, Mapping):
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or ())
        restored = {}
        for name, child in properties.items():
            if name not in value:
                continue
            if value[name] is None and name not in required:
                continue
            restored[name] = _restore(child, value[name])
        return restored
    if kind == "array" and isinstance(value, list):
        items = schema.get("items")
        return [_restore(items, item) for item in value] if isinstance(items, Mapping) else value
    return value


def _reply_object(text: str) -> Mapping[str, Any]:
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[: -len("```")]
    try:
        reply = json.loads(stripped)
    except ValueError as exc:
        raise RunnerError("runner_reply_malformed") from exc
    if not isinstance(reply, Mapping):
        raise RunnerError("runner_reply_malformed")
    return reply


def parse_reply(text: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """Turn the runner's final text into the response the research loop reads."""
    reply = _reply_object(text)
    fetch, result = reply.get("fetch"), reply.get("result")
    if (fetch is None) == (result is None):
        raise RunnerError("runner_reply_malformed")
    if fetch is not None:
        if not isinstance(fetch, list) or not fetch or not all(isinstance(u, str) and u.strip() for u in fetch):
            raise RunnerError("runner_reply_malformed")
        relay = {"fetch": [u.strip() for u in fetch]}
    else:
        if not isinstance(result, Mapping):
            raise RunnerError("runner_reply_malformed")
        for source in result.get("sources") or []:
            if isinstance(source, Mapping) and any(key in source for key in SOURCE_CONTENT_KEYS):
                raise RunnerError("runner_reply_supplies_source_content")
        relay = {"result": _restore(_result_schema(body), result)}
    try:
        return research_agent.response_from_relay(json.dumps(relay, ensure_ascii=False))
    except research_agent.RelayReplyError as exc:
        raise RunnerError("runner_reply_malformed") from exc


def _declares_web_search(body: Mapping[str, Any]) -> bool:
    return any(
        isinstance(tool, Mapping) and tool.get("type") == research_agent.SEARCH_TOOL
        for tool in body.get("tools") or []
    )


def _requested_model(request: Mapping[str, Any]) -> str | None:
    """The model to send to the executor: the request's own stage model, else
    the model the researcher chose for this execution (SBP_CLI_MODEL, set by
    the Workbench only when the researcher chose one), else None so the
    executor applies its existing default."""
    for model in (request.get("model"), os.environ.get("SBP_CLI_MODEL")):
        if isinstance(model, str) and model.strip() and model != RUNNER_DEFAULT_MODEL:
            return model.strip()
    return None


def _policy_model(runner: Runner, request: Mapping[str, Any]) -> str | None:
    """cli-model-by-stage: the runner's own per-stage model, asked only when
    the researcher named none. A runner without `stage_model` (Codex) keeps
    its existing default."""
    choose = getattr(runner, "stage_model", None)
    stage = request.get("stage")
    if not callable(choose) or not isinstance(stage, str):
        return None
    model = choose(stage)
    return model if isinstance(model, str) and model.strip() else None


def handle(request: Mapping[str, Any], runner: Runner) -> dict[str, Any]:
    """Serve one request. Raises RunnerError; never retries."""
    try:
        timeout = float(request.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    except (TypeError, ValueError) as exc:
        raise RunnerError("runner_request_malformed") from exc
    if timeout <= 0:
        raise RunnerError("runner_timed_out")
    kind = request.get("kind")
    if kind == "completion":
        model = _requested_model(request)
        policy_model = None if model else _policy_model(runner, request)
        reply = runner(
            build_completion_prompt(request),
            output_schema=None,
            web_search=False,
            model=model or policy_model,
            timeout=timeout,
        )
        if not reply.text.strip():
            raise RunnerError("runner_empty_reply")
        result = {"status": "ok", "content": reply.text, "tokens": int(reply.tokens), "model": reply.model}
        if policy_model:
            result["model_source"] = "policy"
        elif reply.model_source:
            result["model_source"] = reply.model_source
        if reply.model_confirmed and reply.model:
            result["model_effective"] = reply.model
        if isinstance(reply.usage, dict) and reply.usage:
            result["usage"] = dict(reply.usage)
        if reply.effort:
            result["effort"] = reply.effort
        return result
    if kind == "responses":
        body = request.get("request")
        if not isinstance(body, Mapping):
            raise RunnerError("runner_request_malformed")
        wants_search = _declares_web_search(body)
        can_search = bool(getattr(runner, "supports_web_search", False))
        reply = runner(
            build_responses_prompt(body),
            output_schema=reply_schema(body),
            web_search=wants_search and can_search,
            # The research body names an API model id, which a CLI runner never
            # uses; the researcher's chosen CLI model still applies.
            model=_requested_model({}),
            timeout=timeout,
        )
        if not reply.text.strip():
            raise RunnerError("runner_empty_reply")
        return {
            "status": "ok",
            "response": parse_reply(reply.text, body),
            "tokens": int(reply.tokens),
            "search": ("enabled" if can_search else "unavailable") if wants_search else "disabled",
        }
    raise RunnerError("runner_request_malformed")


@contextmanager
def forward_termination(process: Any):
    """While `process` (the executor) runs, a SIGTERM or SIGINT sent to this
    wrapper is passed on to the executor's own process group, and the wrapper
    exits with a non-zero status.

    The executor runs in its own session, so the server's group signal does not
    reach it; without this it would keep running (and spending quota) after the
    server stopped or the researcher cancelled (desktop-runtime-foundation).
    The previous handlers are restored when the executor has exited. A signal
    that arrives in the instant between starting the executor and entering this
    block is not forwarded -- a known, accepted limit.
    """
    def forward(signum: int, _frame: Any) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        raise SystemExit(128 + signum)

    saved: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            saved[signum] = signal.signal(signum, forward)
    try:
        yield
    finally:
        for signum, previous in saved.items():
            signal.signal(signum, previous)


def run_wrapper(runner: Runner, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """Read one request, write one JSON object, return the process exit code."""
    source = stdin or sys.stdin
    sink = stdout or sys.stdout
    try:
        request = json.loads(source.read())
        if not isinstance(request, Mapping):
            raise RunnerError("runner_request_malformed")
        result = handle(request, runner)
        code = 0
    except RunnerError as exc:
        result, code = {"status": "error", "error": exc.code}, 1
        if exc.diagnostic:
            result["diagnostic"] = exc.diagnostic
    except ValueError:
        result, code = {"status": "error", "error": "runner_request_malformed"}, 1
    except Exception:  # noqa: BLE001 - never leak a traceback to the adapter
        result, code = {"status": "error", "error": "runner_failed"}, 1
    sink.write(json.dumps(result, ensure_ascii=False))
    sink.flush()
    return code
