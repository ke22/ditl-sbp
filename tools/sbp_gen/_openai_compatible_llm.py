"""Shared HTTP client for OpenAI-compatible chat-completions providers.

Groq, OpenRouter, and OpenAI itself all expose the same wire protocol (POST
`/chat/completions` with an OpenAI-shaped body). This module holds the one
implementation `groq_llm.py`, `openrouter_llm.py`, and `openai_llm.py` all
call into, so a transport bug only needs fixing once. No SDK dependency — all
three answer to plain HTTP and the repo already carries `httpx` transitively
via `google-genai`.

`max_tokens_param` exists because OpenAI's own newer chat-completions models
reject the legacy `max_tokens` field and require `max_completion_tokens`
instead (hit live 2026-08-27); Groq/OpenRouter's routed models still accept
the legacy name, so they keep the default rather than both being forced onto
whichever provider changed first.
"""
from __future__ import annotations

import os
import time

import httpx

from .llm import CallResult, LLMError


def call(
    *,
    provider_label: str,
    base_url: str,
    api_key_env: str,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    extra_headers: dict[str, str] | None = None,
    max_tokens_param: str = "max_tokens",
) -> CallResult:
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise LLMError(f"使用 {provider_label} provider 前必須設定 {api_key_env}。")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens_param: max_tokens,
        # 不設 response_format——OpenAI 相容的 `{"type": "json_object"}` 模式會
        # 強制頂層一定要是物件，align/compare 這種頂層是陣列的 schema 永遠無法
        # 滿足這個限制，重試幾次都一樣（2026-08-27 用 OpenRouter 驗證時撞到）。
        # 跟 Gemini 的 response_mime_type 一樣，只靠 system prompt 要求輸出
        # JSON，完整契約交給本地驗證器，見 llm.py 檔頭的設計理由。
    }
    started = time.perf_counter()
    try:
        response = httpx.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=body,
            timeout=180.0,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise LLMError(
            f"{provider_label} API 呼叫失敗：{exc.response.status_code} {exc.response.text[:500]}"
        ) from exc
    except Exception as exc:
        raise LLMError(f"{provider_label} API 呼叫失敗：{type(exc).__name__}: {exc}") from exc
    seconds = time.perf_counter() - started

    choices = payload.get("choices") or []
    if not choices:
        raise LLMError(f"{provider_label} 回了空結果，沒有 choices。")
    message = choices[0].get("message") or {}
    text = message.get("content")
    if not isinstance(text, str) or not text.strip():
        raise LLMError(f"{provider_label} 回了空字串")

    usage = payload.get("usage") or {}
    tokens = int(usage.get("total_tokens") or 0)

    return CallResult(text=text, tokens=tokens, seconds=seconds)
