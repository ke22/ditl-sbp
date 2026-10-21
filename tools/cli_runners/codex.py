"""Codex CLI reference wrapper for the generic CLI runner contract.

Configure with `SBP_CLI_RUNNER_COMMAND="python3 /app/tools/cli_runners/codex.py"`.
Everything protocol-related lives in `protocol`; this module only runs
`codex exec` non-interactively against the authenticated state in CODEX_HOME.

Every option used here is documented for the pinned Codex version
(deploy/cli-runner/codex.lock). Command execution tools are disabled because
the wrapper only needs text back; web search is enabled only when the request
declares it and is explicitly disabled otherwise.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Mapping

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli_runners.protocol import RunnerError, RunnerReply, clean_diagnostic, forward_termination, run_wrapper  # noqa: E402

# Credentials that would move Codex off its authenticated CLI state and onto an
# API-key path. The generic adapter already strips *_API_KEY; these are the
# Codex-specific ones.
CODEX_KEY_ENVIRONMENTS = ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN")

# Selective, read-only inheritance of exactly these two settings from the
# researcher's own Codex CLI configuration (2026-09-29 研究者裁定). Workbench
# always runs Codex with --ignore-user-config, so nothing else in that file
# (MCP servers, personality, service_tier, approvals, ...) reaches a Workbench
# execution; these two are the one explicit, narrow exception a researcher can
# opt into via 使用 Codex 設定. Never written to.
CODEX_SELECTED_SETTINGS = ("model", "model_reasoning_effort")


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured) if configured else Path.home() / ".codex"


def read_selected_settings() -> dict[str, str | None]:
    """{"model": ..., "model_reasoning_effort": ...}; each None when the
    config file is absent, unreadable, or the key itself is absent."""
    out: dict[str, str | None] = {key: None for key in CODEX_SELECTED_SETTINGS}
    path = codex_home() / "config.toml"
    try:
        raw = path.read_bytes()
    except OSError:
        return out
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return out
    if not isinstance(data, dict):
        return out
    for key in CODEX_SELECTED_SETTINGS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    return out


def is_authenticated() -> bool:
    """Presence check only: the file is never read."""
    auth = codex_home() / "auth.json"
    try:
        return auth.is_file() and auth.stat().st_size > 0
    except OSError:
        return False


def _usage_tokens(events: str) -> int:
    total = 0
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, Mapping) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if isinstance(usage, Mapping):
            for field in ("input_tokens", "output_tokens"):
                value = usage.get(field)
                if isinstance(value, int) and value >= 0:
                    total += value
    return total


USAGE_DETAIL_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


def _usage_detail(events: str) -> dict[str, int]:
    """Codex's own per-kind token counts, summed over its turn.completed
    events. Only fields Codex actually reported appear; nothing is estimated."""
    detail: dict[str, int] = {}
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, Mapping) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if isinstance(usage, Mapping):
            for field in USAGE_DETAIL_FIELDS:
                value = usage.get(field)
                if isinstance(value, int) and value >= 0:
                    detail[field] = detail.get(field, 0) + value
    return detail


# A failed `codex exec` is classified from Codex's own report, most specific
# first; `runner_failed` only when nothing matches. Matching is lower-case.
FAILURE_CLASSES = (
    ("runner_cli_usage_error", ("unexpected argument", "unrecognized", "usage:", "invalid value",
                                "error parsing", "unknown feature")),
    ("runner_not_authenticated", ("401", "unauthorized", "not logged in", "login required", "authentication")),
    ("runner_rate_limited", ("429", "rate limit", "usage limit", "quota", "too many requests")),
    ("runner_model_unavailable", ("model not found", "model does not exist", "unsupported model",
                                  "model is not supported", "not supported with", "model_not_found")),
    ("runner_request_rejected", ("400", "invalid_request", "context length", "context window",
                                 "too long", "bad request")),
    ("runner_network_unavailable", ("error sending request", "connection", "dns", "network",
                                    "stream disconnected", "could not resolve")),
)


def _reported_error(events: str) -> str | None:
    """The last error message Codex wrote to its `--json` event stream."""
    message = None
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, Mapping):
            continue
        if event.get("type") == "error" and isinstance(event.get("message"), str):
            message = event["message"]
        elif event.get("type") == "turn.failed":
            error = event.get("error")
            if isinstance(error, Mapping) and isinstance(error.get("message"), str):
                message = error["message"]
    return message


def _mentions(text: str, marker: str) -> bool:
    # An HTTP status is a whole number, never part of a longer one ("14000 tokens").
    if marker.isdigit():
        return re.search(rf"(?<!\d){marker}(?!\d)", text) is not None
    return marker in text


def classify_failure(events: str, stderr: str, returncode: int, prompt: str) -> RunnerError:
    """A non-zero `codex exec` as the most specific code plus a safe diagnostic."""
    stderr_tail = "\n".join(line for line in (stderr or "").splitlines() if line.strip())[-2000:]
    report = _reported_error(events or "") or stderr_tail
    text = report.lower()
    code = "runner_cli_usage_error" if returncode == 2 else None
    if code is None:
        code = next((name for name, markers in FAILURE_CLASSES if any(_mentions(text, m) for m in markers)),
                    "runner_failed")
    return RunnerError(code, clean_diagnostic(report, prompt))


def build_command(
    executable: str, workdir: Path, last_message: Path, *,
    schema_path: Path | None, web_search: bool, model: str | None, effort: str | None = None,
) -> list[str]:
    command = [
        executable, "exec",
        "--json",
        "--ephemeral",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--sandbox", "read-only",
        "--disable", "shell_tool",
        "--disable", "unified_exec",
        "-c", 'approval_policy="never"',
        "-c", 'web_search="live"' if web_search else 'web_search="disabled"',
        "-C", str(workdir),
        "-o", str(last_message),
    ]
    if schema_path is not None:
        command += ["--output-schema", str(schema_path)]
    if model:
        command += ["-m", model]
    if effort:
        command += ["-c", f'model_reasoning_effort="{effort}"']
    command.append("-")
    return command


# 2026-09-29 研究者裁定：--ignore-user-config 保留（MCP／personality／service_tier
# 這些一律不帶進 Workbench），只選擇性繼承使用者 Codex 設定裡的 model 與
# model_reasoning_effort 這兩項——研究者在工作台明確選的值優先，其次是使用者
# 自己 config.toml 裡的這兩項，最後才是既有的「不覆寫」行為。
def _resolve(explicit: str | None, key: str) -> str | None:
    if explicit:
        return explicit
    return read_selected_settings().get(key)


def runner_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    for name in CODEX_KEY_ENVIRONMENTS:
        env.pop(name, None)
    return env


class CodexRunner:
    supports_web_search = True

    def __call__(
        self, prompt: str, *, output_schema: dict[str, Any] | None,
        web_search: bool, model: str | None, timeout: float,
    ) -> RunnerReply:
        executable = shutil.which(os.environ.get("SBP_CODEX_BIN") or "codex")
        if not executable:
            raise RunnerError("runner_missing")
        if not is_authenticated():
            raise RunnerError("runner_not_authenticated")
        with tempfile.TemporaryDirectory(prefix="sbp-codex-") as temp:
            workdir = Path(temp) / "work"
            workdir.mkdir()
            last_message = Path(temp) / "last-message.txt"
            schema_path = None
            if output_schema is not None:
                schema_path = Path(temp) / "output-schema.json"
                schema_path.write_text(json.dumps(output_schema), encoding="utf-8")
            # model: an explicit request (SBP_CLI_MODEL, already resolved by
            # protocol._requested_model) wins; otherwise fall back to the
            # researcher's own Codex model, if 使用 Codex 設定 opted into it.
            # effort has no protocol-level plumbing (it would force every
            # other, non-Codex wrapper to accept and ignore it) -- Workbench's
            # explicit choice travels as SBP_CLI_EFFORT, read directly here.
            resolved_model = _resolve(model, "model")
            resolved_effort = _resolve(os.environ.get("SBP_CLI_EFFORT"), "model_reasoning_effort")
            command = build_command(
                executable, workdir, last_message,
                schema_path=schema_path, web_search=web_search,
                model=resolved_model, effort=resolved_effort,
            )
            try:
                process = subprocess.Popen(
                    command,
                    cwd=workdir,
                    env=runner_environment(),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    shell=False,
                    start_new_session=True,
                )
            except OSError as exc:
                raise RunnerError("runner_missing") from exc
            try:
                with forward_termination(process):
                    events, errors = process.communicate(input=prompt, timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    process.kill()
                process.communicate()
                raise RunnerError("runner_timed_out") from exc
            if process.returncode != 0:
                raise classify_failure(events or "", errors or "", process.returncode, prompt)
            try:
                text = last_message.read_text(encoding="utf-8")
            except OSError as exc:
                raise RunnerError("runner_empty_reply") from exc
            # model here is what was requested of this execution -- the
            # resolved value, not runtime-confirmed telemetry Codex handed
            # back (it doesn't; _usage_tokens is the one field that is real
            # per-call telemetry, read straight from Codex's own JSON events).
            # Codex never reports which model ran, so the model is never confirmed.
            source = "workbench" if model else ("executor_settings" if resolved_model else None)
            return RunnerReply(text=text, tokens=_usage_tokens(events or ""), model=resolved_model,
                               usage=_usage_detail(events or "") or None, effort=resolved_effort,
                               model_source=source)


def main() -> int:
    return run_wrapper(CodexRunner())


if __name__ == "__main__":
    sys.exit(main())
