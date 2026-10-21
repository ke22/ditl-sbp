"""Machine-readable adapter for an authenticated local/server AI CLI."""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import shlex
import subprocess
import time
from typing import Any, Callable, Iterator, Mapping

from .execution import API_KEY_ENVIRONMENTS
from .llm import CallResult, LLMError


class CLIRunnerError(LLMError):
    pass


class CLIRunnerDeadlineExceeded(CLIRunnerError):
    """The enclosing job deadline left no time for another runner call."""


# Monotonic timestamp after which no runner call may start or continue.
_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar("sbp_cli_runner_deadline", default=None)


@contextlib.contextmanager
def deadline(seconds: float) -> Iterator[None]:
    """Bound every runner call made inside this block by one wall-clock deadline.

    A nested deadline never extends an outer one.
    """
    limit = time.monotonic() + float(seconds)
    outer = _DEADLINE.get()
    token = _DEADLINE.set(limit if outer is None else min(outer, limit))
    try:
        yield
    finally:
        _DEADLINE.reset(token)


# stage-aware-runner-timeout (2026-10-01): a large generation stage needs
# more than the global per-call timeout. Persona Matrix on a 298-unit case
# with a high-effort model ran past 600s and left nothing on disk. This is
# stability, not speed: the slow call itself is recorded as performance debt
# (docs/operations/performance-debt.md).
# Data preparation (align) segments of dense transcript ran past 600s at
# xhigh on 恆隆行 (2026-10-02) and were thrown away whole; same remedy. The
# CJM journey, now at least 10 primary steps, ran past 8 minutes the same
# day; researcher asked for the same 1200s. The SBP structure call took 753s
# at V1 freeze and timed out at 600s on the 14-step MUJI CJM the same day.
# The first new-method MUJI fresh run (2026-10-03) lost Axial Coding to the
# 600s limit at xhigh: C1 made its input (every code's quotes) and output
# (definition, members, representative quotes per theme) much larger. Selective
# Coding and JTBD candidates read the same enlarged material. Stability only;
# the speed work is separate (docs/operations/performance-debt.md).
STAGE_MIN_TIMEOUT_SECONDS: dict[str, float] = {"persona_matrix": 1800.0, "align": 1200.0, "journey": 1200.0,
                                               "structure": 1800.0, "axial_coding": 1200.0,
                                               "selective_themes": 1200.0, "narrative_job_candidates": 1200.0}
DEFAULT_TIMEOUT_SECONDS = 600.0

# A per-call timeout chosen by the caller for one kind of call (e.g. a short
# item repair); unset means the stage timeout below applies.
_CALL_TIMEOUT: contextvars.ContextVar[float | None] = contextvars.ContextVar("sbp_cli_runner_call_timeout", default=None)


@contextlib.contextmanager
def call_timeout(seconds: float) -> Iterator[None]:
    """Use `seconds` as the per-call timeout for runner calls in this block.
    Each call (including a transient retry) gets the full value; an enclosing
    job deadline still cuts it."""
    token = _CALL_TIMEOUT.set(float(seconds))
    try:
        yield
    finally:
        _CALL_TIMEOUT.reset(token)


def stage_timeout(stage: str | None) -> float:
    """Per-call timeout for `stage`.

    Precedence: a stage-specific `SBP_CLI_RUNNER_TIMEOUT_<STAGE>` wins
    outright; otherwise the larger of the global `SBP_CLI_RUNNER_TIMEOUT`
    (600 when unset) and the stage's floor. A global override can lengthen
    every stage, but cannot cut a large stage below the time it needs.
    A caller-chosen `call_timeout()` overrides all of these.
    """
    chosen = _CALL_TIMEOUT.get()
    if chosen is not None:
        return chosen
    if stage:
        specific = os.environ.get(f"SBP_CLI_RUNNER_TIMEOUT_{stage.upper()}")
        if specific:
            return float(specific)
    general = float(os.environ.get("SBP_CLI_RUNNER_TIMEOUT") or DEFAULT_TIMEOUT_SECONDS)
    return max(general, STAGE_MIN_TIMEOUT_SECONDS.get(stage or "", 0.0))


def effective_timeout(requested: float | None = None) -> float:
    """Per-call timeout clipped to the remaining deadline; raises when none remains."""
    per_call = float(requested or os.environ.get("SBP_CLI_RUNNER_TIMEOUT") or DEFAULT_TIMEOUT_SECONDS)
    limit = _DEADLINE.get()
    if limit is None:
        return per_call
    remaining = limit - time.monotonic()
    if remaining <= 0:
        raise CLIRunnerDeadlineExceeded("CLI runner 已超過這次工作的時限。")
    return min(per_call, remaining)


# Executors with a shipped wrapper. Only "codex" has a legacy single-variable
# fallback (`SBP_CLI_RUNNER_COMMAND`); a new executor never gets one, so a
# deployment that has not configured it fails closed instead of silently
# reusing whichever wrapper the legacy variable happens to name.
KNOWN_EXECUTORS = ("codex", "claude")


def _command(executor: str | None = None) -> list[str]:
    """Resolve the wrapper command for `executor`.

    An explicit `executor` is used as-is (the server's pre-admission
    readiness check passes the request's own `ExecutionConfig.executor` this
    way, since that check runs before any worker environment exists). With no
    argument, falls back to `SBP_CLI_EXECUTOR`, which `invoke()` always finds
    set in its own subprocess environment once `ExecutionConfig.child_environment`
    has run.
    """
    executor = executor or os.environ.get("SBP_CLI_EXECUTOR") or "codex"
    raw = (os.environ.get(f"SBP_CLI_RUNNER_COMMAND_{executor.upper()}") or "").strip()
    if not raw and executor == "codex":
        raw = (os.environ.get("SBP_CLI_RUNNER_COMMAND") or "").strip()
    if not raw:
        raise CLIRunnerError("CLI runner 尚未設定。")
    try:
        command = shlex.split(raw)
    except ValueError as exc:
        raise CLIRunnerError("CLI runner 設定無法解析。") from exc
    if not command:
        raise CLIRunnerError("CLI runner 尚未設定。")
    return command


DIAGNOSTIC_LIMIT = 300
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]+)*"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\s*[=:]\s*\S+"),
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_HOME = re.compile(r"/(?:Users|home)/[^/\s]+")
_PROMPT_WINDOW = 12


def _mask_prompt_text(line: str, prompt: str) -> str:
    """Replace every stretch of `line` that also occurs in the prompt."""
    if not prompt or len(line) < _PROMPT_WINDOW:
        return line
    covered = [False] * len(line)
    for start in range(len(line) - _PROMPT_WINDOW + 1):
        if line[start:start + _PROMPT_WINDOW] in prompt:
            for index in range(start, start + _PROMPT_WINDOW):
                covered[index] = True
    out, masking = [], False
    for char, hidden in zip(line, covered):
        if hidden:
            if not masking:
                out.append("[…]")
            masking = True
        else:
            out.append(char)
            masking = False
    return "".join(out)


def clean_diagnostic(text: object, prompt: str = "") -> str | None:
    """One safe line from a runner's own error report, or None.

    Any 12-character stretch that also occurs in the prompt is masked, so
    participant transcript text cannot ride out on an error message; then
    credentials, email addresses and home-directory paths are masked."""
    if not isinstance(text, str):
        return None
    line = " / ".join(" ".join(part.split()) for part in text.splitlines() if part.strip())
    line = _mask_prompt_text(line, prompt)
    for pattern in _SECRET_PATTERNS:
        line = pattern.sub("[redacted]", line)
    line = _EMAIL.sub("[redacted]", line)
    line = _HOME.sub("~", line)
    line = "".join(ch for ch in line if ch.isprintable()).strip(" /")
    if not line or line == "[…]":
        return None
    return line if len(line) <= DIAGNOSTIC_LIMIT else line[:DIAGNOSTIC_LIMIT - 1] + "…"


RUNNER_ERROR_CODES = frozenset({
    "runner_missing", "runner_not_authenticated", "runner_timed_out",
    "runner_empty_reply", "runner_reply_malformed",
    "runner_reply_supplies_source_content", "runner_request_malformed",
    "runner_cli_usage_error", "runner_rate_limited", "runner_model_unavailable",
    "runner_request_rejected", "runner_network_unavailable", "runner_failed",
})


def _request_prompt(request: Mapping[str, Any]) -> str:
    """Everything this request sent to the runner, for diagnostic masking."""
    return "\n".join(str(request.get(key) or "") for key in ("system", "user")) + "\n" + json.dumps(
        request.get("request") or {}, ensure_ascii=False)


def invoke(request: Mapping[str, Any], *, timeout: float | None = None, executor: str | None = None,
           environ: Mapping[str, str] | None = None,
           run: Callable[..., "subprocess.CompletedProcess"] | None = None) -> dict[str, Any]:
    """Send one JSON request to the configured wrapper and fail closed.

    The three optional arguments let a caller that is not itself a worker process (the
    Workbench server's connection check) use this same protocol: `executor` names which
    executor's command to resolve instead of reading SBP_CLI_EXECUTOR, `environ` is the
    environment the wrapper runs in (default: this process's), and `run` starts the
    wrapper process (default: `subprocess.run`) so the caller can track and end it.
    Left out, they change nothing.
    """
    command = _command(executor)
    effective = effective_timeout(timeout)
    env = dict(os.environ if environ is None else environ)
    for name in API_KEY_ENVIRONMENTS:
        env.pop(name, None)
    # The wrapper gets a slightly shorter budget so it stops (and reaps) its own
    # runner before this process gives up on it; neither exceeds the deadline.
    payload = {**dict(request), "timeout_seconds": round(effective - min(5.0, effective * 0.1), 3)}
    started = time.perf_counter()
    try:
        completed = (run if run is not None else subprocess.run)(
            command,
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            check=False,
            shell=False,
            env=env,
            timeout=effective,
        )
    except subprocess.TimeoutExpired as exc:
        if _DEADLINE.get() is not None and _DEADLINE.get() <= time.monotonic():
            raise CLIRunnerDeadlineExceeded("CLI runner 已超過這次工作的時限。") from exc
        raise CLIRunnerError("CLI runner 無法完成這次工作。") from exc
    except OSError as exc:
        raise CLIRunnerError("CLI runner 無法完成這次工作。") from exc
    if completed.returncode != 0:
        # The wrapper always writes its {"status":"error","error":<code>} object
        # to stdout on the way out (protocol.run_wrapper), even on a nonzero
        # exit; reading it here is the only way the real reason survives past
        # this boundary instead of collapsing into one indistinguishable
        # "failed" message that reads like "never logged in" no matter what
        # actually went wrong.
        code = diagnostic = None
        try:
            parsed = json.loads(completed.stdout)
            if isinstance(parsed, dict):
                code, diagnostic = parsed.get("error"), parsed.get("diagnostic")
        except (TypeError, ValueError):
            pass
        # Only the wrapper's bounded protocol tokens may cross this boundary,
        # plus its diagnostic line cleaned again here against this request's
        # own prompt. Never surface an arbitrary error string from stdout.
        if not (isinstance(code, str) and code in RUNNER_ERROR_CODES):
            raise CLIRunnerError("CLI runner 回報這次工作失敗。")
        diagnostic = clean_diagnostic(diagnostic, _request_prompt(request))
        report = f"執行器回報：{diagnostic}" if diagnostic else ""
        raise CLIRunnerError(f"CLI runner 回報這次工作失敗（{code}）。{report}")
    try:
        response = json.loads(completed.stdout)
    except (TypeError, ValueError) as exc:
        raise CLIRunnerError("CLI runner 回傳的結果無法讀取。") from exc
    if not isinstance(response, dict):
        raise CLIRunnerError("CLI runner 回傳的結果格式不正確。")
    if response.get("status", "ok") not in {"ok", "success", "completed"}:
        raise CLIRunnerError("CLI runner 沒有完成這次工作。")
    if "elapsed_seconds" not in response:
        response["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return response


def call(*, stage: str, system: str, user: str, model: str | None = None,
         effort: str | None = None, max_tokens: int = 32000) -> CallResult:
    response = invoke({
        "kind": "completion",
        "stage": stage,
        "system": system,
        "user": user,
        "model": model,
        "effort": effort,
        "max_tokens": max_tokens,
    }, timeout=stage_timeout(stage))
    text = response.get("content", response.get("text"))
    if not isinstance(text, str) or not text.strip():
        raise CLIRunnerError("CLI runner 沒有回傳可用內容。")
    usage = response.get("usage")
    return CallResult(
        text=text,
        tokens=int(response.get("tokens") or 0),
        seconds=float(response.get("elapsed_seconds") or 0),
        model=response.get("model") if isinstance(response.get("model"), str) else None,
        effort=response.get("effort") if isinstance(response.get("effort"), str) else None,
        usage={k: v for k, v in usage.items() if isinstance(v, int) and v >= 0} if isinstance(usage, dict) else None,
        model_source=response.get("model_source") if isinstance(response.get("model_source"), str) else None,
        model_effective=response.get("model_effective") if isinstance(response.get("model_effective"), str) else None,
    )


def responses(body: Mapping[str, Any]) -> dict[str, Any]:
    response = invoke({"kind": "responses", "request": dict(body)})
    nested = response.get("response")
    if isinstance(nested, dict):
        result = dict(nested)
    elif isinstance(response.get("output"), list):
        result = dict(response)
    else:
        raise CLIRunnerError("CLI runner 沒有回傳可用研究結果。")
    # What the runner reported about this turn, kept beside (not inside) the
    # Responses payload so the research loop can record it.
    tokens = response.get("tokens")
    result["runner_usage"] = {
        "tokens": tokens if isinstance(tokens, int) and tokens >= 0 else 0,
        "search": response.get("search") if isinstance(response.get("search"), str) else None,
    }
    return result
