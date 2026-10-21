"""Request-scoped execution configuration shared by Workbench workers.

The canonical pipeline does not branch on provider names.  The server selects
one adapter for a job, then gives the worker a sanitized environment.  This
module deliberately contains no persistence or authentication storage.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


class ExecutionConfigError(ValueError):
    """A product execution request is incomplete or unsupported."""


API_KEY_ENVIRONMENTS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
)
SUPPORTED_API_PROVIDERS = {"openai": "OPENAI_API_KEY"}
# GUI execution override, originally for the Codex CLI executor only
# (add-codex-effort-control, 2026-09-29 研究者裁定). Not a general
# reasoning-effort framework -- reused as-is from the vocabulary
# sbp_gen/__main__.py's own --effort already uses, minus "max" (the
# researcher's spec named exactly these four for Codex).
#
# add-claude-code-cli-runner discovered these are already executor-agnostic
# plumbing: both `codex.py` and `claude.py` read the very same
# SBP_CLI_MODEL/SBP_CLI_EFFORT environment variables (llm.default_model("cli")
# reads SBP_CLI_MODEL regardless of which wrapper eventually runs). The
# `codex_model`/`codex_effort` field names stay as-is to avoid an unrelated
# rename across existing call sites and tests, but they now mean "the CLI
# model/effort override for whichever executor is selected", and validation
# is against that executor's own confirmed value set.
CODEX_EFFORT_CHOICES = ("low", "medium", "high", "xhigh")
# Verified via the installed Claude Code CLI's own --help (task 1.2, design.md);
# "max" is real for Claude Code and not for Codex -- the two sets are not
# unified into one, because that would misrepresent Codex as supporting a
# level it does not.
CLAUDE_EFFORT_CHOICES = ("low", "medium", "high", "xhigh", "max")
EFFORT_CHOICES_BY_EXECUTOR = {"codex": CODEX_EFFORT_CHOICES, "claude": CLAUDE_EFFORT_CHOICES}
CODEX_SETTING_ENVIRONMENTS = ("SBP_CLI_MODEL", "SBP_CLI_EFFORT")
# Executors with a shipped wrapper (add-claude-code-cli-runner). Only these
# names may be selected; naming any other executor is refused before a
# worker starts, a job state changes, or an artifact is written.
KNOWN_EXECUTORS = ("codex", "claude")



def _clean_api_key(value: object) -> str:
    """A pasted key often carries invisible characters (zero-width spaces,
    non-breaking spaces); they would only surface later as an encoding error
    in the HTTP header, so refuse them here with a readable message."""
    if not isinstance(value, str) or not value.strip():
        raise ExecutionConfigError("API 執行需要你提供 API key。")
    key = value.strip()
    if not key.isascii() or any(ch.isspace() or not ch.isprintable() for ch in key):
        raise ExecutionConfigError("API key 裡有看不見的字元或空白，請重新複製貼上一次。")
    return key


@dataclass(frozen=True)
class ExecutionConfig:
    mode: str
    provider: str | None = None
    api_key: str | None = None
    executor: str = "codex"
    codex_model: str | None = None
    codex_effort: str | None = None

    @classmethod
    def from_payload(cls, payload: object, *, allow_transport: bool = False) -> "ExecutionConfig":
        if not isinstance(payload, dict):
            raise ExecutionConfigError("請選擇執行方式。")
        mode = payload.get("mode")
        if mode not in {"cli", "api"} and not (allow_transport and mode == "transport"):
            raise ExecutionConfigError("執行方式必須是 CLI 或 API。")
        if mode == "cli":
            if payload.get("api_key"):
                raise ExecutionConfigError("CLI 執行不需要 API key。")
            # No executor is assumed here: the researcher chose one, or the request is refused
            # (provider-setup). The dataclass default below serves the developer command line only.
            executor = payload.get("executor")
            if executor is None or executor == "":
                raise ExecutionConfigError("請先選擇 AI 執行方式。")
            if executor not in KNOWN_EXECUTORS:
                raise ExecutionConfigError("不認得這個執行器。")
            codex_model = payload.get("codex_model")
            codex_model = codex_model.strip() if isinstance(codex_model, str) and codex_model.strip() else None
            codex_effort = payload.get("codex_effort") or None
            if codex_effort is not None and codex_effort not in EFFORT_CHOICES_BY_EXECUTOR[executor]:
                raise ExecutionConfigError("不認得這個 effort 等級。")
            return cls(mode="cli", executor=executor, codex_model=codex_model, codex_effort=codex_effort)
        if mode == "transport":
            return cls(mode="transport")
        provider = payload.get("provider")
        if provider not in SUPPORTED_API_PROVIDERS:
            raise ExecutionConfigError("目前 API 執行只支援 OpenAI。")
        return cls(mode="api", provider=provider, api_key=_clean_api_key(payload.get("api_key")))

    @classmethod
    def from_environment(cls) -> "ExecutionConfig":
        mode = (os.environ.get("SBP_EXECUTION_MODE") or "").strip().lower()
        if mode == "cli" or (not mode and os.environ.get("SBP_LLM_PROVIDER") == "cli"):
            return cls(mode="cli")
        if mode == "transport" or os.environ.get("SBP_LLM_PROVIDER") == "transport":
            return cls(mode="transport")
        if mode == "api":
            provider = (os.environ.get("SBP_API_PROVIDER") or "openai").strip().lower()
            key_name = SUPPORTED_API_PROVIDERS.get(provider)
            key = os.environ.get(key_name or "") if key_name else None
            if not key:
                raise ExecutionConfigError("API 執行缺少工作所需的 API key。")
            return cls(mode="api", provider=provider, api_key=_clean_api_key(key))
        return cls(mode="legacy")

    def child_environment(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """Build a worker environment without leaking unrelated credentials."""
        env = dict(os.environ if base is None else base)
        for name in API_KEY_ENVIRONMENTS:
            env.pop(name, None)
        # Never let an ambient SBP_CLI_MODEL/SBP_CLI_EFFORT on the server
        # process itself leak into a run the researcher did not choose an
        # override for -- absent-in-this-request must always mean "existing
        # no-override behaviour", the same guarantee API_KEY_ENVIRONMENTS
        # gives credentials above.
        for name in CODEX_SETTING_ENVIRONMENTS:
            env.pop(name, None)
        env["SBP_EXECUTION_MODE"] = self.mode
        if self.mode == "cli":
            env["SBP_LLM_PROVIDER"] = "cli"
            env.pop("SBP_API_PROVIDER", None)
            env["SBP_CLI_EXECUTOR"] = self.executor
            if self.codex_model:
                env["SBP_CLI_MODEL"] = self.codex_model
            if self.codex_effort:
                env["SBP_CLI_EFFORT"] = self.codex_effort
        elif self.mode == "api":
            env["SBP_LLM_PROVIDER"] = self.provider or "openai"
            env["SBP_API_PROVIDER"] = self.provider or "openai"
            env[SUPPORTED_API_PROVIDERS[self.provider or "openai"]] = self.api_key or ""
        elif self.mode == "transport":
            env["SBP_LLM_PROVIDER"] = "transport"
            env["SBP_TRANSPORT_MODEL"] = env.get("SBP_TRANSPORT_MODEL", "claude-opus-5")
        return env

    def public_summary(self) -> dict[str, str]:
        if self.mode == "api":
            return {"mode": "api", "provider": self.provider or "openai"}
        if self.mode == "cli":
            summary = {"mode": "cli", "executor": self.executor}
            if self.codex_model:
                summary["codex_model"] = self.codex_model
            if self.codex_effort:
                summary["codex_effort"] = self.codex_effort
            return summary
        return {"mode": self.mode}
