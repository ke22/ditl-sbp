"""Optional Groq provider for the sbp-gen pipeline.

Groq runs open-weight models on dedicated inference hardware (LPUs), which
finishes a request far faster than a "thinking"-budget hosted model. Added
specifically because the higher-effort Gemini structure calls kept dropping
the connection mid-request on 2026-08-27 — a short, fast call sidesteps
whatever was timing out the long-held one, regardless of whether that theory
turns out to be the real cause.
"""
from __future__ import annotations

from . import _openai_compatible_llm
from .llm import CallResult

BASE_URL = "https://api.groq.com/openai/v1"
API_KEY_ENV = "GROQ_API_KEY"

# gpt-oss-20b/120b are Groq's current recommended replacements for the
# 2026-06-17-deprecated llama-3.1-8b-instant / llama-3.3-70b-versatile —
# check https://console.groq.com/docs/deprecations before assuming these
# are still current.
DEFAULT_MODELS = {
    "align": "openai/gpt-oss-20b",
    "journey": "openai/gpt-oss-20b",
    "journey_recheck": "openai/gpt-oss-20b",
    "as_is_recheck": "openai/gpt-oss-20b",
    "issues": "openai/gpt-oss-20b",
    "issue_grounding": "openai/gpt-oss-20b",
    "research_support": "openai/gpt-oss-20b",
    "compare": "openai/gpt-oss-20b",
    "reference": "openai/gpt-oss-20b",
    # 詮釋層：跟上面同一個 sonnet 量級對應（2026-09-08 補齊——這批 stage 加進
    # llm.py 的時間晚於這份 map，之前落在 default_model() 的 align 後備值）。
    "axial_coding": "openai/gpt-oss-20b",
    "open_coding": "openai/gpt-oss-20b",
    "category_properties": "openai/gpt-oss-20b",
    "persona_matrix": "openai/gpt-oss-20b",
    "persona_content": "openai/gpt-oss-20b",
    "persona_dimension_candidates": "openai/gpt-oss-20b",
    "jtbd": "openai/gpt-oss-20b",
    "primary_job_candidates": "openai/gpt-oss-20b",
    "job_map": "openai/gpt-oss-20b",
    "how_might_we": "openai/gpt-oss-20b",
    "primary_hmw_candidates": "openai/gpt-oss-20b",
    "journey_annotations": "openai/gpt-oss-20b",
    "chart_guides": "openai/gpt-oss-20b",
    "selective_themes": "openai/gpt-oss-20b",
    "semantic_segments": "openai/gpt-oss-20b",
    "segmentation_axis_candidates": "openai/gpt-oss-20b",
    "target_segmentation": "openai/gpt-oss-20b",
    "core_category_candidates": "openai/gpt-oss-20b",
    "narrative_job_candidates": "openai/gpt-oss-20b",
    "selective_coding_narrative": "openai/gpt-oss-20b",
    "structure": "openai/gpt-oss-120b",
    "reconcile": "openai/gpt-oss-120b",
    # to_be 是最高風險的合成步驟——之前沒列進這份 map 就會落到上面 20b 那個
    # 後備值。
    "to_be": "openai/gpt-oss-120b",
    "brief": "openai/gpt-oss-120b",
}


def call(
    *,
    stage: str,
    system: str,
    user: str,
    model: str | None = None,
    effort: str | None = None,
    max_tokens: int = 32000,
) -> CallResult:
    del effort  # Groq's hosted models don't expose a thinking-budget knob.
    return _openai_compatible_llm.call(
        provider_label="Groq",
        base_url=BASE_URL,
        api_key_env=API_KEY_ENV,
        model=model or DEFAULT_MODELS.get(stage, DEFAULT_MODELS["align"]),
        system=system,
        user=user,
        max_tokens=max_tokens,
    )
