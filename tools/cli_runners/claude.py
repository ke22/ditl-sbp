"""Claude Code CLI wrapper for the generic CLI runner contract.

Configure with `SBP_CLI_RUNNER_COMMAND_CLAUDE="python3 /app/tools/cli_runners/claude.py"`.
Everything protocol-related lives in `protocol`; this module only runs
`claude -p` non-interactively against the researcher's existing `claude.ai`
login state, using the executable discovery order documented below.

Every flag used here was empirically verified against a real, running Claude
Code CLI install (`add-claude-code-cli-runner` design.md, "task 1.1/1.2 查證
結果"); nothing here is asserted from documentation alone. `--bare` is
deliberately not used: it forces `ANTHROPIC_API_KEY`/`apiKeyHelper` auth and
never reads the OAuth/keychain login state this wrapper depends on. Isolation
instead comes from `--setting-sources`, `--strict-mcp-config`,
`--disable-slash-commands`, and `--tools`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli_runners.protocol import RunnerError, RunnerReply, clean_diagnostic, forward_termination, run_wrapper  # noqa: E402

# The one credential environment variable this CLI's own --help documents as
# an alternative to its login state (see `--bare`'s description). Stripped so
# a request-scoped or ambient key can never substitute for the deployment's
# own login.
CLAUDE_KEY_ENVIRONMENTS = ("ANTHROPIC_API_KEY",)

# Verified via `--help` on the installed version (2.1.284): legal values are
# exactly these five; "ultracode" and similar do not exist and are never used.
EFFORT_CHOICES = ("low", "medium", "high", "xhigh", "max")

_VERSION_PROBE_TIMEOUT = 10.0


def _probe(executable: str) -> bool:
    """True only when `<executable> --version` actually runs and answers."""
    try:
        result = subprocess.run(
            [executable, "--version"], capture_output=True, text=True,
            timeout=_VERSION_PROBE_TIMEOUT, shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and bool((result.stdout or "").strip())


def _desktop_app_candidates() -> list[str]:
    """Known Claude Desktop install locations, newest version first.

    Never hardcodes a specific version directory as the product contract —
    only used as a last-resort discovery step after `SBP_CLAUDE_BIN` and
    `$PATH` have already been tried.
    """
    if sys.platform != "darwin":
        return []
    base = Path.home() / "Library" / "Application Support" / "Claude" / "claude-code"
    try:
        version_dirs = sorted((p for p in base.iterdir() if p.is_dir()), reverse=True)
    except OSError:
        return []
    return [str(p / "claude.app" / "Contents" / "MacOS" / "claude") for p in version_dirs]


def find_executable() -> str | None:
    """First candidate, in order, that passes a real `--version` probe.

    `SBP_CLAUDE_BIN`, when set, is authoritative: it is the only candidate
    tried, and a probe failure means missing, never a silent fall-through to
    `$PATH` or a known install location (mirrors the Codex wrapper's
    `shutil.which(os.environ.get("SBP_CODEX_BIN") or "codex")`, which the same
    way never searches further once a specific path is configured). Only when
    it is unset does discovery continue: whatever `claude` resolves to on
    `$PATH`, then a known Claude Desktop install location. Never repairs or
    installs anything; a candidate that fails the probe is skipped, not fixed.
    """
    configured = os.environ.get("SBP_CLAUDE_BIN")
    if configured:
        return configured if _probe(configured) else None
    candidates = []
    on_path = shutil.which("claude")
    if on_path:
        candidates.append(on_path)
    candidates.extend(_desktop_app_candidates())
    for candidate in candidates:
        if _probe(candidate):
            return candidate
    return None


def is_authenticated(executable: str | None = None) -> bool:
    """`claude auth status --json`'s `loggedIn` field, and nothing else.

    Every other field in that reply (email, org id/name, subscription type)
    is read and discarded here; it is never logged, stored, or returned.
    """
    binary = executable or find_executable()
    if not binary:
        return False
    try:
        result = subprocess.run(
            [binary, "auth", "status", "--json"], capture_output=True, text=True,
            timeout=_VERSION_PROBE_TIMEOUT, shell=False, env=runner_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    try:
        status = json.loads(result.stdout or "{}")
    except ValueError:
        return False
    return bool(isinstance(status, Mapping) and status.get("loggedIn") is True)


# A failed invocation is classified from its own `result` text, most specific
# first; `runner_failed` only when nothing matches. Matching is lower-case.
# Only the `runner_not_authenticated` row is confirmed by a live probe (task
# 1.2); the rest are conservative keyword heuristics in the same spirit as
# the Codex wrapper's own table, not independently verified against a live
# failure of each kind, and fall through to `runner_failed` when they miss.
FAILURE_CLASSES = (
    ("runner_not_authenticated", ("not logged in", "please run /login", "unauthorized", "401")),
    ("runner_rate_limited", ("rate limit", "usage limit", "429", "quota", "too many requests")),
    ("runner_model_unavailable", ("model not found", "unsupported model", "unknown model", "model_not_found")),
    ("runner_cli_usage_error", ("unknown option", "unrecognized", "usage:", "invalid value")),
    ("runner_request_rejected", ("400", "invalid request", "context window", "too long", "bad request")),
    ("runner_network_unavailable", ("network", "connection", "dns", "econnrefused", "could not resolve")),
)


def _mentions(text: str, marker: str) -> bool:
    if marker.isdigit():
        return re.search(rf"(?<!\d){marker}(?!\d)", text) is not None
    return marker in text


def classify_failure(report: str, stderr: str, returncode: int, prompt: str) -> RunnerError:
    """A failed invocation as the most specific code plus a safe diagnostic."""
    stderr_tail = "\n".join(line for line in (stderr or "").splitlines() if line.strip())[-2000:]
    text = (report or stderr_tail).lower()
    code = next((name for name, markers in FAILURE_CLASSES if any(_mentions(text, m) for m in markers)),
                "runner_failed")
    return RunnerError(code, clean_diagnostic(report or stderr_tail, prompt))


def _actual_model(envelope: Mapping[str, Any]) -> str | None:
    """The model `modelUsage` says actually ran, when exactly one did."""
    usage = envelope.get("modelUsage")
    if isinstance(usage, Mapping) and len(usage) == 1:
        return next(iter(usage))
    return None


def _usage_tokens(envelope: Mapping[str, Any]) -> int:
    usage = envelope.get("usage")
    if not isinstance(usage, Mapping):
        return 0
    total = 0
    for field in ("input_tokens", "output_tokens"):
        value = usage.get(field)
        if isinstance(value, int) and value >= 0:
            total += value
    return total


def build_command(
    executable: str, *, output_schema: dict[str, Any] | None, model: str | None, effort: str | None,
) -> list[str]:
    command = [
        executable, "-p",
        "--output-format", "json",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--tools", "",
    ]
    if output_schema is not None:
        command += ["--json-schema", json.dumps(output_schema)]
    if model:
        command += ["--model", model]
    if effort:
        command += ["--effort", effort]
    return command


def runner_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    for name in CLAUDE_KEY_ENVIRONMENTS:
        env.pop(name, None)
    return env


class ClaudeRunner:
    # Not verified for this CLI (task 1.2 scope did not confirm a web-search
    # tool name/flag equivalent to Codex's `web_search="live"`); a `responses`
    # request that declares web search gets `search: "unavailable"` rather
    # than a guessed flag.
    supports_web_search = False

    def __call__(
        self, prompt: str, *, output_schema: dict[str, Any] | None,
        web_search: bool, model: str | None, timeout: float,
    ) -> RunnerReply:
        executable = find_executable()
        if not executable:
            raise RunnerError("runner_missing")
        if not is_authenticated(executable):
            raise RunnerError("runner_not_authenticated")
        effort = os.environ.get("SBP_CLI_EFFORT")
        if effort is not None and effort not in EFFORT_CHOICES:
            effort = None
        command = build_command(executable, output_schema=output_schema, model=model, effort=effort)
        try:
            process = subprocess.Popen(
                command,
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
                stdout, stderr = process.communicate(input=prompt, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                process.kill()
            process.communicate()
            raise RunnerError("runner_timed_out") from exc
        try:
            envelope = json.loads((stdout or "").strip())
        except ValueError as exc:
            raise classify_failure(stdout or "", stderr or "", process.returncode, prompt) from exc
        if not isinstance(envelope, Mapping):
            raise classify_failure(stdout or "", stderr or "", process.returncode, prompt)
        if process.returncode != 0 or envelope.get("is_error"):
            report = envelope.get("result") if isinstance(envelope.get("result"), str) else ""
            raise classify_failure(report, stderr or "", process.returncode, prompt)
        text = envelope.get("result")
        if not isinstance(text, str):
            raise RunnerError("runner_empty_reply")
        return RunnerReply(
            text=text,
            tokens=_usage_tokens(envelope),
            model=_actual_model(envelope) or model,
        )


def main() -> int:
    return run_wrapper(ClaudeRunner())


if __name__ == "__main__":
    sys.exit(main())
