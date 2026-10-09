"""Optional OpenRouter provider for the sbp-gen pipeline.

OpenRouter aggregates many vendors' models behind one OpenAI-compatible
endpoint; models with an id ending in `:free` cost nothing but carry tight
rate limits (roughly 20 requests/minute, 200/day as of 2026-08). The free
lineup is a moving target — check https://openrouter.ai/collections/free-models
before assuming the pinned model below is still served.
"""
from __future__ import annotations

from . import _openai_compatible_llm
from .llm import CallResult

BASE_URL = "https://openrouter.ai/api/v1"
API_KEY_ENV = "OPENROUTER_API_KEY"

# One model for every stage: unlike Gemini/Groq's paid tiers, there is no
# clearly-tiered "flash vs pro" pair in the free lineup worth splitting on
# right now — pick the strongest available free model for all stages instead
# of guessing at a weaker one for no real benefit.
_DEFAULT_FREE_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"

DEFAULT_MODELS = {
    stage: _DEFAULT_FREE_MODEL
    for stage in (
        "align", "journey", "journey_recheck", "as_is_recheck", "issues",
        "issue_grounding", "research_support", "compare", "reference",
        "structure", "reconcile", "brief",
        # 詮釋層＋to_be：2026-09-08 補齊——這批 stage 加進 llm.py 的時間晚於
        # 這份 map，之前落在 default_model() 的 align 後備值。一個 provider
        # 一個模型，補齊不需要分量級。
        "axial_coding", "open_coding", "category_properties", "persona_matrix", "persona_content",
        "persona_dimension_candidates", "jtbd", "primary_job_candidates",
        "job_map", "how_might_we", "primary_hmw_candidates", "journey_annotations", "chart_guides", "selective_themes", "semantic_segments",
        "segmentation_axis_candidates",
        "target_segmentation", "core_category_candidates", "narrative_job_candidates", "to_be",
        "selective_coding_narrative",
    )
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
    del effort  # Free-tier routed models don't expose a thinking-budget knob.
    return _openai_compatible_llm.call(
        provider_label="OpenRouter",
        base_url=BASE_URL,
        api_key_env=API_KEY_ENV,
        model=model or DEFAULT_MODELS.get(stage, _DEFAULT_FREE_MODEL),
        system=system,
        user=user,
        max_tokens=max_tokens,
        extra_headers={
            # OpenRouter asks for these for its own routing/analytics; both
            # are optional but recommended in their docs.
            "HTTP-Referer": "https://github.com/ke22/ditl_sbp",
            "X-Title": "ditl_sbp sbp-gen",
        },
    )
