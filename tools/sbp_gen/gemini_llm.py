"""Optional Google Gemini provider for the sbp-gen pipeline.

This adapter deliberately returns plain JSON text and lets the existing local
validators decide whether the evidence contract is actually satisfied. The
repo's evidence schema contains cross-field rules that are stricter than the
subset supported by Gemini structured output.
"""
from __future__ import annotations

import os
import time

from .llm import CallResult, LLMError


# Pinned to an explicit dated model id, not the `-latest` alias: the alias can
# silently swap underlying versions between runs, which would make the
# per-stage model provenance this pipeline records untrustworthy.
_GEMINI_FRONTIER_PRO = "gemini-3.1-pro-preview"

DEFAULT_MODELS = {
    # 萃取與比對：整理性工作，維持 flash（跟 llm.py 的 sonnet 量級對應）
    "align": "gemini-3.7-flash",
    "journey": "gemini-3.7-flash",
    "journey_recheck": "gemini-3.7-flash",
    "as_is_recheck": "gemini-3.7-flash",
    "issues": "gemini-3.7-flash",
    "issue_grounding": "gemini-3.7-flash",
    "research_support": "gemini-3.7-flash",
    "compare": "gemini-3.7-flash",
    "reference": "gemini-3.7-flash",
    # 詮釋層：跟上面同一個 sonnet 量級對應（2026-09-08 補齊——這批 stage 加進
    # llm.py 的時間晚於這份 map，之前落在 default_model() 的 align 後備值）。
    "axial_coding": "gemini-3.7-flash",
    "open_coding": "gemini-3.7-flash",
    "category_properties": "gemini-3.7-flash",
    "persona_matrix": "gemini-3.7-flash",
    "persona_content": "gemini-3.7-flash",
    "persona_dimension_candidates": "gemini-3.7-flash",
    "jtbd": "gemini-3.7-flash",
    "primary_job_candidates": "gemini-3.7-flash",
    "job_map": "gemini-3.7-flash",
    "how_might_we": "gemini-3.7-flash",
    "primary_hmw_candidates": "gemini-3.7-flash",
    "journey_annotations": "gemini-3.7-flash",
    "chart_guides": "gemini-3.7-flash",
    "selective_themes": "gemini-3.7-flash",
    "semantic_segments": "gemini-3.7-flash",
    "segmentation_axis_candidates": "gemini-3.7-flash",
    "target_segmentation": "gemini-3.7-flash",
    "core_category_candidates": "gemini-3.7-flash",
    "narrative_job_candidates": "gemini-3.7-flash",
    "selective_coding_narrative": "gemini-3.7-flash",
    # 建模與推論：跟 llm.py 的 opus 量級對應，換前沿 pro
    "structure": _GEMINI_FRONTIER_PRO,
    "reconcile": _GEMINI_FRONTIER_PRO,
    # to_be 是 opus 量級（最高風險的合成步驟）——之前沒列進這份 map 就會落到
    # 上面 flash 那個後備值，risk 最高的 stage 拿到最便宜的模型。
    "to_be": _GEMINI_FRONTIER_PRO,
    "brief": _GEMINI_FRONTIER_PRO,
}


def _client():
    try:
        from google import genai
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise LLMError(
            "缺少 Gemini SDK。請先 pip install google-genai，並設定 GEMINI_API_KEY。"
        ) from exc
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise LLMError("使用 Gemini provider 前必須設定 GEMINI_API_KEY。")
    return genai.Client(api_key=api_key)


def _config(system: str, max_tokens: int):
    try:
        from google.genai import types
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise LLMError(
            "缺少 Gemini SDK。請先 pip install google-genai，並設定 GEMINI_API_KEY。"
        ) from exc
    # Keep JSON formatting at the API boundary, but leave the full semantic
    # contract to the repo's validator and retry loop.
    return types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        max_output_tokens=max_tokens,
    )


def _response_text(response) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text
    raise LLMError("Gemini 回傳空結果，可能是被安全政策擋下或沒有候選回覆。")


def _usage_tokens(response) -> int:
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return 0
    total = getattr(usage, "total_token_count", None)
    if isinstance(total, int):
        return total
    return sum(
        value or 0
        for value in (
            getattr(usage, "prompt_token_count", 0),
            getattr(usage, "candidates_token_count", 0),
            getattr(usage, "thoughts_token_count", 0),
        )
        if isinstance(value, int)
    )


def call(
    *,
    stage: str,
    system: str,
    user: str,
    model: str | None = None,
    effort: str | None = None,
    max_tokens: int = 32000,
) -> CallResult:
    """Run one Gemini request and normalize it to the existing CallResult."""
    del effort  # Gemini thinking controls are model-specific; do not guess.
    client = _client()
    started = time.perf_counter()
    try:
        response = client.models.generate_content(
            model=model or DEFAULT_MODELS.get(stage, DEFAULT_MODELS["align"]),
            contents=user,
            config=_config(system, max_tokens),
        )
        text = _response_text(response)
        tokens = _usage_tokens(response)
    except Exception as exc:
        if isinstance(exc, LLMError):
            raise
        raise LLMError(f"Gemini API 呼叫失敗：{type(exc).__name__}: {exc}") from exc
    return CallResult(
        text=text,
        tokens=tokens,
        seconds=time.perf_counter() - started,
    )
